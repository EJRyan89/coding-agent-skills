"""A deployment killed outright, with no Python cleanup, leaves state a fresh process recovers.

Each test runs a real deployment in a child Python against a fixture home, stops it just after one named filesystem
change, and kills its process tree: SIGKILL to its process group on Linux and macOS, TerminateJobObject, which ends each
process as TerminateProcess does, on Windows. No exception handler, finally block, or context manager of the child runs.
Fresh child processes then recover the home, and the test holds the home and the manifest to what the journal promises:
a run killed before its manifest commit leaves the home as it found it, and one killed after it is finished.

The child stops by fsops, the one write point every test replaces to inject a failure: a wrapped function makes the real
change, then names the boundary on its standard output and waits on its standard input, which the parent never writes.
The parent waits for that line within BOUNDARY_SECONDS, so a child that never reaches its boundary fails the test rather
than hanging it. They hold on Windows, Linux, and macOS alike. They concern a process's death, not a power loss: the
kill leaves what the child wrote in the operating system's cache, which the next process reads.
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import job_tree
import pytest
from harness import REPOSITORY_ROOT, Deployment, Result, deployer, tree_state

from deployer import hashing, journal
from deployer.kinds import KINDS
from deployer.names import JOURNAL_FILE, deletion_name, transient_name

__all__ = ["deployer"]

# How long a child may take to reach its boundary, or to end once killed, before the test fails: far longer than a run
# of a few fixture skills takes, so only a child that never gets there reaches it.
BOUNDARY_SECONDS = 120.0
# The line a child prints on its standard output once it has made the change it stops after.
STOPPED = "STOPPED"
# What a child killed by its tree reports as its exit: TerminateJobObject's exit code on Windows, SIGKILL elsewhere.
KILLED = 1 if sys.platform == "win32" else -signal.SIGKILL  # type: ignore[attr-defined, unused-ignore]  # Unix only
# The exit of a deployment cancelled at its menu, which ends input reaches after recovery and before anything else.
CANCELLED_EXIT = 130
# Why a killed child, and the recovery after it, change the home.
KILLED_PARTWAY = "the child is killed after it began changing the home"
RECOVERS = "recovery finishes or undoes the killed run before the cancellation"

# A child deployment: argv[1] is this checkout, argv[2] the fixture source, argv[3] its home, argv[4] the fsops function
# to stop after, or "none", argv[5] the pattern its call must match, and the rest deploy.py's arguments. The deployer's
# own output goes to standard error, so standard output carries only the STOPPED line. A call is described as the
# pattern reads it: a journal entry by its op, root, and item, and a path by its POSIX form.
CHILD = r"""
import io, json, os, re, sys
from contextlib import redirect_stdout
from pathlib import Path
checkout, source, home, function, pattern, *arguments = sys.argv[1:]
sys.path.insert(0, checkout)
from deployer import fsops, pipeline
from deployer.paths import Paths

def describe(arguments):
    if function == "append_line":
        entry = json.loads(arguments[1])
        return f"{entry['op']} {entry.get('root', 'claude')} {entry['item']}"
    return " -> ".join(Path(argument).as_posix() for argument in arguments if isinstance(argument, Path))

def stopping(real):
    def wrapped(*arguments):
        result = real(*arguments)
        if re.search(pattern, describe(arguments)):
            sys.__stdout__.write("STOPPED\n")
            sys.__stdout__.flush()
            sys.stdin.buffer.read()
            os._exit(97)
        return result
    return wrapped

if function != "none":
    setattr(fsops, function, stopping(getattr(fsops, function)))
with redirect_stdout(sys.stderr):
    code = pipeline.run(arguments, Paths(Path(source), Path(home)), stdin=io.StringIO(""))
sys.stderr.flush()
os._exit(code)
"""


@dataclass(frozen=True)
class Boundary:
    """Where a child stops: just after the first call of fsops.<function> whose description the pattern matches."""

    function: str
    pattern: str


def journaled(op: str, item: str, root: str = "claude") -> Boundary:
    return Boundary("append_line", f"^{op} {root} {re.escape(item)}$")


def moved(source: str, destination: str) -> Boundary:
    """A move from a path ending in source to one ending in destination, each a POSIX path pattern."""
    return Boundary("move", f"{source} -> .*{destination}$")


# The killed run, from the home the setup leaves, in the order it makes its changes: it removes delta and its adapter,
# updates alpha, replaces the user's change to beta, whose copy it keeps under .backups, installs gamma and its adapter,
# and updates the agent.
KILLED_RUN = ("--all", "--force-item", "beta")
BETA_AWAY = re.escape(transient_name("beta"))
# Each boundary before the run's manifest commit, which recovery rolls back.
BEFORE_COMMIT = {
    "the replaced copy's backup entry is journaled": journaled("backup", "beta"),
    "the replaced copy is moved aside": moved(r"/\.claude/skills/beta", f"/\\.claude/skills/{BETA_AWAY}"),
    "an install entry is journaled": journaled("install", "gamma", "agents"),
    "the last item is installed": moved(r"/\.claude-agents/reviewer\.md", r"/\.claude/agents/reviewer\.md"),
}
# Each boundary from the manifest commit on, which recovery finishes.
AFTER_COMMIT = {
    "the manifest is committed": Boundary("write_atomic", r"/\.deploy-manifest\.json$"),
    "a displaced copy is half deleted": moved(re.escape(transient_name("alpha")), re.escape(deletion_name("alpha"))),
    "the kept copy's move is journaled": journaled("preserve", "beta"),
    "the kept copy is moved under .backups": moved(BETA_AWAY, r"/\.backups/[^/]+/beta"),
    "the journal is deleted": Boundary("remove", f"/{re.escape(JOURNAL_FILE)}$"),
}


@dataclass(frozen=True)
class Before:
    """The home as the killed run found it."""

    home: dict[str, str]
    earlier_run: str
    earlier_backup: dict[str, str]
    changed_beta: dict[str, str]


def prepare(deployed: Deployment) -> Before:
    """Deploy alpha, beta, delta, and the agent beta depends on; replace a change the user made to beta, which keeps
    its copy under .backups; then change the source and beta again for the killed run."""
    deployed.make_source_json()
    deployed.make_skill("alpha", "Alpha one")
    deployed.make_skill("beta", "Beta one", agent_deps=["reviewer"])
    deployed.make_skill("delta", "Delta one")
    deployed.make_agent("reviewer", "Review one.")
    deployed.make_config()
    deployed.deploy_ok("--all")
    deployed.append(deployed.skills_dir / "beta" / "SKILL.md", "# the user's first change\n")
    deployed.make_skill("beta", "Beta two", agent_deps=["reviewer"])
    deployed.deploy_ok(*KILLED_RUN)
    [earlier_run] = [path.name for path in (deployed.skills_dir / ".backups").iterdir()]
    deployed.make_skill("alpha", "Alpha two")
    deployed.make_skill("gamma", "Gamma one")
    deployed.remove_skill("delta")
    deployed.make_agent("reviewer", "Review two.")
    deployed.append(deployed.skills_dir / "beta" / "SKILL.md", "# the user's second change\n")
    deployed.make_skill("beta", "Beta three", agent_deps=["reviewer"])
    return Before(
        tree_state(deployed.home),
        earlier_run,
        tree_state(deployed.skills_dir / ".backups" / earlier_run / "beta"),
        tree_state(deployed.skills_dir / "beta"),
    )


def lines_of(stream: IO[str], read: queue.Queue[str]) -> None:
    """Hand each line the child prints to the test, then an empty string once its output ends."""
    for line in iter(stream.readline, ""):
        read.put(line)
    read.put("")


def run_child(deployed: Deployment, boundary: Boundary | None, *arguments: str, changes_home: str) -> Result:
    """Run a deployment in a child process; with a boundary, kill its process tree once it stops there.

    The child runs in a process tree of its own, which leaving job_tree's hold ends: on Windows the interpreter a
    virtual environment's launcher starts runs in the launcher's job, so it is ended with it.
    """
    environment = {
        **os.environ,
        "HOME": str(deployed.home),
        "USERPROFILE": str(deployed.home),
        "PYTHONIOENCODING": "utf-8",
    }
    deployed.assert_child_home(environment)
    function, pattern = (boundary.function, boundary.pattern) if boundary else ("none", "")
    program = [sys.executable, "-B", "-c", CHILD, str(REPOSITORY_ROOT), str(deployed.source), str(deployed.home)]
    log = deployed.root / f"child-{len(list(deployed.root.glob('child-*.log')))}.log"

    def run() -> int:
        tree = job_tree.process_tree()
        with (
            log.open("w", encoding="utf-8") as errors,
            subprocess.Popen(
                [*program, function, pattern, *arguments],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
                env=environment,
                text=True,
                encoding="utf-8",
                **tree.start_options(),
            ) as child,
        ):
            read: queue.Queue[str] = queue.Queue()
            assert child.stdout is not None
            with tree.hold(child):
                threading.Thread(target=lines_of, args=(child.stdout, read), daemon=True).start()
                try:
                    stopped = read.get(timeout=BOUNDARY_SECONDS) if boundary else ""
                except queue.Empty:
                    stopped = "timed out"
                if not boundary:
                    child.wait(timeout=BOUNDARY_SECONDS)
            child.wait(timeout=BOUNDARY_SECONDS)
        output = log.read_text(encoding="utf-8")
        print(output, end="")
        if boundary is not None:
            assert stopped == f"{STOPPED}\n", f"the child did not stop at {boundary} ({stopped!r}):\n{output}"
            assert child.returncode == KILLED, f"the child ended with {child.returncode}, not killed:\n{output}"
        return child.returncode

    return deployed.run_command(run, changes_home=changes_home)


def kill_at(deployed: Deployment, boundary: Boundary, *arguments: str) -> tuple[str, int]:
    """Kill a deployment at boundary and return its run's ID and the PID its lock record names, checking it left its
    staging run and that record, which its cleanup would have removed."""
    run_child(deployed, boundary, *arguments, changes_home=KILLED_PARTWAY)
    [run] = list(deployed.paths.staging_root.iterdir())
    return run.name, holder(deployed)


def holder(deployed: Deployment) -> int:
    """The PID in the lock record a killed child left: the interpreter that ran the deployment, which on Windows may
    be a virtual environment launcher's child rather than the process the test started."""
    assert deployed.lock_dir.is_dir(), "the killed run's lock record is gone, as if its cleanup ran"
    pid = json.loads((deployed.lock_dir / "info.json").read_text(encoding="utf-8"))["pid"]
    assert isinstance(pid, int)
    return pid


def recover(deployed: Deployment, killed: int | None = None) -> Result:
    """Run a fresh deployment that recovers the home, then ends at its menu, deploying nothing; with killed, the PID
    of the child killed holding the lock, it judges that lock stale, and without it finds none."""
    result = run_child(deployed, None, changes_home=RECOVERS)
    assert result.code == CANCELLED_EXIT, result.output
    stale = [line for line in result.output.splitlines() if "Stale lock" in line]
    assert stale == ([f"WARNING: Stale lock detected (PID {killed} not running)."] if killed else []), result.output
    return result


def assert_recovery_repeats(deployed: Deployment) -> None:
    """A second recovery finds no run to recover and changes nothing in the home."""
    recovered_home = tree_state(deployed.home)
    assert recover(deployed).output.count("Recovering") == 0
    assert tree_state(deployed.home) == recovered_home


def items(directory: Path) -> Iterator[str]:
    """Each name in a managed root that is an item, not the deployer's own record or kept backups."""
    return (path.name for path in directory.iterdir() if not path.name.startswith("."))


def assert_consistent(deployed: Deployment) -> None:
    """Every root holds exactly the items the manifest records, each with the hash it records, and nothing of a run:
    no displaced copy, no half-deleted one, no staging run, and no lock."""
    roots: dict[Path, set[str]] = {}
    for kind in KINDS:
        root = journal.root_directory(deployed.paths, kind.root)
        owned = deployed.owned(kind.key)
        roots.setdefault(root, set()).update(owned)
        for name, details in owned.items():
            assert hashing.hash_path(root / name) == details["hash"], f"{root / name} differs from the manifest"
    for root, names in roots.items():
        assert sorted(items(root)) == sorted(names), f"{root} holds items the manifest does not record"
        left = [path.name for path in root.iterdir() if "deploying" in path.name]
        assert not left, f"{root} still holds {left}"
    assert not list(deployed.paths.staging_root.iterdir()), "a staging run remains"
    assert not deployed.lock_dir.exists(), "the lock record remains"
    assert not deployed.paths.lock_file.exists(), "the lock file remains"


def assert_deployed(deployed: Deployment, before: Before) -> None:
    """The killed run's changes are all in place, the copy of beta it displaced kept under its own .backups, and
    the earlier run's backup untouched."""
    assert_consistent(deployed)
    assert sorted(items(deployed.skills_dir)) == ["alpha", "beta", "gamma"]
    assert sorted(items(deployed.agents_dir)) == ["alpha", "beta", "gamma"]
    assert "Alpha two" in deployed.skill_text("alpha")
    assert "Beta three" in deployed.skill_text("beta")
    assert "Review two." in (deployed.claude_agents_dir / "reviewer.md").read_text(encoding="utf-8")
    kept = {path.name: tree_state(path / "beta") for path in (deployed.skills_dir / ".backups").iterdir()}
    assert kept.pop(before.earlier_run) == before.earlier_backup
    assert list(kept.values()) == [before.changed_beta]


@pytest.mark.parametrize("name", list(BEFORE_COMMIT))
def test_a_run_killed_before_its_commit_is_rolled_back_and_deployed_again(deployer: Deployment, name: str) -> None:
    before = prepare(deployer)
    run, killed = kill_at(deployer, BEFORE_COMMIT[name], *KILLED_RUN)
    recovered = recover(deployer, killed)
    deployer.assert_lines(
        recovered.output, f"Recovering uncommitted run {run} (rolling back)...", "  Recovery complete."
    )
    assert tree_state(deployer.home) == before.home
    assert_recovery_repeats(deployer)
    redeployed = deployer.deploy_ok(*KILLED_RUN)
    groups = deployer.report_groups(redeployed.output, "DEPLOYED")
    assert {group: sorted(names) for group, names in groups.items() if group != "BACKED UP"} == {
        "REPLACED": ["beta (forced, previous copy backed up)"],
        "REMOVED": ["delta (deselected or absent from source)"],
        "UPDATED": ["alpha", "reviewer.md (agent)"],
        "INSTALLED": ["gamma"],
    }
    assert_deployed(deployer, before)


@pytest.mark.parametrize("name", list(AFTER_COMMIT))
def test_a_run_killed_after_its_commit_is_finished(deployer: Deployment, name: str) -> None:
    before = prepare(deployer)
    run, killed = kill_at(deployer, AFTER_COMMIT[name], *KILLED_RUN)
    assert deployer.manifest()["last_run_id"] == run
    committed = deployer.manifest_file.read_bytes()
    recovered = recover(deployer, killed)
    if AFTER_COMMIT[name].function != "remove":
        deployer.assert_lines(
            recovered.output, f"Recovering committed run {run} (completing finalization)...", "  Recovery complete."
        )
    assert deployer.manifest_file.read_bytes() == committed
    assert_deployed(deployer, before)
    assert (deployer.skills_dir / ".backups" / run / "beta").is_dir()
    assert_recovery_repeats(deployer)
    redeployed = deployer.deploy_ok(*KILLED_RUN)
    assert list(deployer.report_groups(redeployed.output, "DEPLOYED")) == ["UNCHANGED"]
    assert_deployed(deployer, before)


def test_a_rollback_killed_partway_is_finished_by_the_next_recovery(deployer: Deployment) -> None:
    before = prepare(deployer)
    run, _ = kill_at(deployer, BEFORE_COMMIT["the last item is installed"], *KILLED_RUN)
    # The rollback removes what the run installed under its deletion name, so this kill leaves beta half deleted.
    run_child(deployer, moved(r"/\.claude/skills/beta", re.escape(deletion_name("beta"))), changes_home=RECOVERS)
    assert not (deployer.skills_dir / "beta").exists()
    assert [path.name for path in deployer.paths.staging_root.iterdir()] == [run]
    recovered = recover(deployer, holder(deployer))
    deployer.assert_lines(
        recovered.output, f"Recovering uncommitted run {run} (rolling back)...", "  Recovery complete."
    )
    assert tree_state(deployer.home) == before.home
    assert_recovery_repeats(deployer)


def test_a_finalization_killed_partway_is_finished_by_the_next_recovery(deployer: Deployment) -> None:
    before = prepare(deployer)
    run, _ = kill_at(deployer, AFTER_COMMIT["the manifest is committed"], *KILLED_RUN)
    committed = deployer.manifest_file.read_bytes()
    # Recovery keeps the displaced copy of beta under .backups; this kill leaves it there with the journal unfinished.
    run_child(deployer, AFTER_COMMIT["the kept copy is moved under .backups"], changes_home=RECOVERS)
    assert [path.name for path in deployer.paths.staging_root.iterdir()] == [run]
    recovered = recover(deployer, holder(deployer))
    deployer.assert_lines(
        recovered.output, f"Recovering committed run {run} (completing finalization)...", "  Recovery complete."
    )
    assert deployer.manifest_file.read_bytes() == committed
    assert_deployed(deployer, before)
    assert_recovery_repeats(deployer)
