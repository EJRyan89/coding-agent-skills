"""Isolated fixture repositories and homes for deployer tests."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))


from deployer import cli, configure, fsops, pipeline, platform_support
from deployer.paths import Paths

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

SOURCE_ID = "test/skills"
# The fixture skills' shell grant, written un-rendered with the interpreter by hand, since make_skill declares no
# PYTHON in required_vars; the deployer reads no shell grant, so its form changes no test. A shipped skill grants each
# script it runs by name, as {{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/<script>.py" *, which the deployer renders with
# the platform's Python command; see "Granting tools" in docs/adding-a-skill.md.
OWN_SCRIPTS = 'python -B "${CLAUDE_SKILL_DIR}/scripts/*'
ALLOWED_TOOLS = json.dumps([f"Bash({OWN_SCRIPTS})", f"PowerShell({OWN_SCRIPTS})"])
REPORT_GROUP = re.compile(r"([A-Z][A-Z ]*[A-Z]) \(([0-9]+)\):")
# Why a failure the deployer reports after it began changing the home is expected to change it; see deploy_fails.
RECOVERS = "recovery of an earlier run changes what it can before it stops"
FAILS_PARTWAY = "the deployment fails after it began changing the home and keeps its evidence"
TAKES_THE_LOCK = "the lock is taken, or a stale one reclaimed, before the failure"
# The command line every deploy.py hint and usage line names, pinned as each platform's literal: Windows runs
# Python as python, and Linux as python3, where a distribution may install no python command.
DEPLOY_COMMAND = "python deploy.py" if platform_support.WINDOWS else "python3 deploy.py"
# A well-formed hash that matches no tree, for a journal or manifest entry whose item is never hashed.
ZERO_HASH = "sha256:" + "0" * 64
SEE_RECOVERY_FAILED = 'See "When recovery fails" in docs/recovery.md.'
# Each journal root's destination prefix and staging folder, as the deployer writes them.
JOURNAL_ROOTS = {
    "": ("skills", "staging"),
    "agents": ("agents", "staging/.agent-adapters"),
    "claude-agents": ("claude-agents", "staging/.claude-agents"),
}


def forward(path: Path) -> str:
    return str(path).replace("\\", "/")


def make_directory_link(link: Path, target: Path) -> None:
    """A link to a directory, of the kind each platform makes without privileges: a junction on Windows, a symbolic
    link elsewhere. The deployer refuses both as a link."""
    if platform_support.WINDOWS:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def remove_directory_link(link: Path) -> None:
    """Remove a link make_directory_link made, never the directory it points to."""
    if platform_support.WINDOWS:
        link.rmdir()
    else:
        link.unlink()


def tree_state(root: Path) -> dict[str, str]:
    """Each directory, file, and link under root by relative path: a file by a hash of its bytes, a link by its target.

    A link, a junction included, is recorded and not followed, so a change through it is not counted twice.
    """
    state: dict[str, str] = {}
    for directory, directories, files in os.walk(root):
        base = Path(directory)
        followed = []
        for name in sorted([*directories, *files]):
            path = base / name
            if platform_support.is_link_or_junction(path):
                state[path.relative_to(root).as_posix()] = f"link {path.readlink()}"
            elif name in files:
                state[path.relative_to(root).as_posix()] = f"file {hashlib.sha256(path.read_bytes()).hexdigest()}"
            else:
                state[path.relative_to(root).as_posix()] = "directory"
                followed.append(name)
        directories[:] = followed
    return state


def git(directory: Path, *arguments: str) -> str:
    """Run git in a fixture repository; DeployerTestCase gives it the fixture identity and no user hooks or signing."""
    completed = subprocess.run(
        ["git", "-C", str(directory), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def journal_line(entry: dict[str, Any]) -> str:
    return json.dumps(entry, separators=(",", ":")) + "\n"


def backup_entry(item: str, backup_hash: str, retain: bool = False, run_id: str = "", root: str = "") -> dict[str, Any]:
    """The journal entry a run writes before it moves item in root aside, retained under .backups when retain."""
    prefix = JOURNAL_ROOTS[root][0]
    entry: dict[str, Any] = {"op": "backup", **({"root": root} if root else {})}
    entry.update(
        {
            "item": item,
            "from": f"{prefix}/{item}",
            "to": f"{prefix}/{item}.deploying-bak",
            "retain": retain,
            "backup_hash": backup_hash,
        }
    )
    if retain:
        entry["backup_dest"] = f".backups/{run_id}/{item}"
    return entry


def install_entry(item: str, staged_hash: str, root: str = "") -> dict[str, Any]:
    """The journal entry a run writes before it moves item's staged copy into root."""
    prefix, staging = JOURNAL_ROOTS[root]
    return {
        "op": "install",
        **({"root": root} if root else {}),
        "item": item,
        "from": f"{staging}/{item}",
        "to": f"{prefix}/{item}",
        "staged_hash": staged_hash,
    }


def probe_returning(alive: bool, start_time: int | None) -> Callable[[int], platform_support.ProcessStatus]:
    """A process probe that reports every lock holder alive or gone, with this start time."""
    return lambda pid: platform_support.ProcessStatus(alive, start_time)


def interrupting() -> io.StringIO:
    """Standard input whose next read behaves as if the user pressed Ctrl+C."""
    # A mock rather than a subclass: typeshed's StringIO.readline cannot be overridden without breaking the override
    # rule against io's binary base class.
    return mock.Mock(io.StringIO, readline=mock.Mock(side_effect=KeyboardInterrupt))


def recorded_temporary_directories() -> tuple[mock._patch[Callable[..., str]], list[Path]]:
    """A patch of --at's mkdtemp that records each directory the export makes, and the list it records them in."""
    made: list[Path] = []
    real = tempfile.mkdtemp

    def mkdtemp(*positional: Any, **keywords: Any) -> str:
        made.append(Path(real(*positional, **keywords)))
        return str(made[-1])

    return mock.patch.object(tempfile, "mkdtemp", mkdtemp), made


def failing_lock_metadata_write() -> mock._patch[mock.MagicMock | mock.AsyncMock]:
    """Make writing the lock's info.json fail, leaving every other atomic write alone."""
    real_write = fsops.write_atomic

    def failing_write(path: Path, content: bytes) -> None:
        if path.name == "info.json":
            raise OSError("synthetic metadata write failure")
        real_write(path, content)

    return mock.patch("deployer.fsops.write_atomic", side_effect=failing_write)


def contended_lock(lock_dir: Path) -> mock._patch[mock.MagicMock | mock.AsyncMock]:
    """Let another deployment take the lock at lock_dir just before this one's second attempt to make it."""
    real_make_directory = fsops.make_directory
    calls = {"count": 0}

    def contended(path: Path) -> None:
        if path == lock_dir:
            calls["count"] += 1
            if calls["count"] == 2:
                real_make_directory(path)
                raise FileExistsError(str(path))
        real_make_directory(path)

    return mock.patch("deployer.fsops.make_directory", side_effect=contended)


@dataclass
class Result:
    """A run's exit code and its output as the user saw it, with each stream apart for a test that tells them apart."""

    code: int
    output: str
    stdout: str = ""
    stderr: str = ""


class _Tee(io.StringIO):
    """One stream of a run, kept apart and copied into the output both streams share, in the order they wrote it."""

    def __init__(self, shared: io.StringIO) -> None:
        super().__init__()
        self.shared = shared

    def write(self, text: str) -> int:
        self.shared.write(text)
        return super().write(text)


class Deployment:
    """A fixture source, a throwaway home, and the commands that deploy between them, for one test: a pytest test takes
    one from the `deployer` fixture, and every DeployerTestCase is one."""

    root: Path
    home: Path
    source: Path
    repos: Path

    def start(self, root: Path) -> None:
        """Lay the fixture source and the empty home out under root, a folder this test alone uses."""
        # Expand 8.3 short names such as RUNNER~1, which configured paths reject.
        self.root = root.resolve()
        self.home = self.root / "home"
        self.source = self.root / "source"
        self.repos = self.root / "repos"
        # The home starts as a fresh profile folder holds it, empty, so a refusal that makes a root the deployer
        # manages is seen changing it.
        for directory in (self.home, self.source / "skills", self.source / "deploy-meta"):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def paths(self) -> Paths:
        return Paths(self.source, self.home)

    @property
    def skills_dir(self) -> Path:
        return self.home / ".claude" / "skills"

    @property
    def agents_dir(self) -> Path:
        return self.home / ".agents" / "skills"

    @property
    def claude_agents_dir(self) -> Path:
        return self.home / ".claude" / "agents"

    @property
    def manifest_file(self) -> Path:
        return self.skills_dir / ".deploy-manifest.json"

    @property
    def lock_dir(self) -> Path:
        return self.home / ".claude" / "deployer" / ".deploy.lock.d"

    def staging_run(self, run_id: str) -> Path:
        directory = self.home / ".claude" / "deployer" / "staging" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def assert_lines(self, output: str, *lines: str) -> None:
        """Each line appears whole, so a message cannot be cut short or run into the next one."""
        printed = output.splitlines()
        for line in lines:
            assert line in printed, output

    def make_source_json(
        self,
        source_id: str = SOURCE_ID,
        shared_assets: dict[str, str] | None = None,
        bundles: dict[str, Any] | None = None,
        renamed: dict[str, str] | None = None,
    ) -> None:
        document = {
            "id": source_id,
            "name": "Test Skills",
            "shared_assets": shared_assets or {},
            "bundles": bundles or {},
            **({"renamed": renamed} if renamed is not None else {}),
        }
        (self.source / "source.json").write_text(json.dumps(document, indent=4), encoding="utf-8")

    def make_skill(
        self,
        name: str,
        content: str = "# Test skill",
        required_vars: tuple[str, ...] | list[str] = (),
        shared_deps: tuple[str, ...] | list[str] = (),
        skill_deps: tuple[str, ...] | list[str] = (),
        selectable: bool = True,
        opt_in: bool = False,
        agent_deps: tuple[str, ...] | list[str] = (),
        description: str | None = None,
        user_only: bool = False,
        argument_hint: str | None = None,
    ) -> Path:
        """A skill whose description and argument hint are the raw YAML scalars given; user-only when user_only."""
        directory = self.source / "skills" / name
        directory.mkdir(parents=True, exist_ok=True)
        description = description or f'"Test skill {name}"'
        flags = (f"argument-hint: {argument_hint}\n" if argument_hint else "") + (
            "disable-model-invocation: true\n" if user_only else ""
        )
        (directory / "SKILL.md").write_bytes(
            (
                f"---\nname: {name}\ndescription: {description}\nallowed-tools: {ALLOWED_TOOLS}\n{flags}---\n\n"
                f"{content}\n"
            ).encode()
        )
        metadata = {
            "required_vars": list(required_vars),
            "shared_deps": list(shared_deps),
            "skill_deps": list(skill_deps),
            "selectable": selectable,
            **({"opt_in": True} if opt_in else {}),
            **({"agent_deps": list(agent_deps)} if agent_deps else {}),
        }
        (self.source / "deploy-meta" / f"{name}.json").write_text(json.dumps(metadata), encoding="utf-8")
        return directory

    def make_agent(self, name: str, body: str = "Review the change.", declared: str | None = None) -> bytes:
        """A Claude Code subagent definition under agents/; returns its exact bytes."""
        content = (
            f"---\nname: {declared or name}\ndescription: Test agent {name}\ntools: Read, Write\n"
            f"model: inherit\n---\n\n{body}\n"
        ).encode()
        target = self.source / "agents" / f"{name}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return content

    def make_shared_asset(self, name: str, content: str = "# Shared asset") -> None:
        (self.source / "skills" / name).write_bytes(f"{content}\n".encode())

    def config_file(self, source_id: str = SOURCE_ID) -> Path:
        digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:12]
        return self.home / ".claude" / "deployer" / "config" / f"{digest}.config"

    def make_config(self, source_id: str = SOURCE_ID, repos_root: Path | None = None, extra: str = "") -> Path:
        repos = repos_root or self.repos
        repos.mkdir(parents=True, exist_ok=True)
        path = self.config_file(source_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"_source_id={source_id}\nREPOS_ROOT={forward(repos)}\n{extra}".encode())
        return path

    def deploy(
        self,
        *arguments: str,
        stdin: str = "",
        source: Path | None = None,
        home: Path | None = None,
        refusal_home: Path | None = None,
        changes_home: str = "",
        copilot_home: Path | None = None,
        **options: Any,
    ) -> Result:
        """Deploy from source (the fixture source by default) into home, failing the test if the source changed.

        Templates are immutable: the deployer renders in memory and writes only under the home, so the source tree is
        compared, file by file and byte for byte, before and after every deployment a test runs. refusal_home checks
        a --canary-home destination without changing the Paths passed to the entry point. copilot_home is the
        COPILOT_HOME deploy.py would read.
        """
        paths = Paths(source or self.source, home or self.home, copilot_home=copilot_home)
        return self.run_command(
            lambda: pipeline.run(list(arguments), paths, stdin=io.StringIO(stdin), **options),
            source=source,
            home=refusal_home or home,
            changes_home=changes_home,
        )

    def assert_run_unchanged(
        self,
        result: Result,
        source: Path,
        source_before: dict[str, str],
        home: Path,
        home_before: dict[str, str],
        changes_home: str = "",
    ) -> None:
        """Every run preserves its source; a nonzero exit preserves its home unless it states why it changes it."""
        assert source_before == tree_state(source), f"the deployment changed its source:\n{result.output}"
        if result.code != 0 and not changes_home:
            assert home_before == tree_state(home), f"the refusal changed the home:\n{result.output}"

    def run_command(
        self,
        command: Callable[[], int],
        *,
        source: Path | None = None,
        home: Path | None = None,
        changes_home: str = "",
    ) -> Result:
        """Run a deployer entry point under the same source and refusal guards, including usage and cancellation.

        A run that raises, as a killed deployment does, is a failure: its source is compared, and its home unless
        changes_home says why it changes it, before the exception reaches the test.
        """
        source, home = source or self.source, home or self.home
        source_before, home_before = tree_state(source), tree_state(home)
        output = io.StringIO()
        stdout, stderr = _Tee(output), _Tee(output)
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = command()
        except BaseException as raised:
            failed = Result(1, f"{output.getvalue()}\nThe run raised {raised!r}.")
            self.assert_run_unchanged(failed, source, source_before, home, home_before, changes_home)
            raise
        result = Result(code, output.getvalue(), stdout.getvalue(), stderr.getvalue())
        self.assert_run_unchanged(result, source, source_before, home, home_before, changes_home)
        return result

    def run_entry_point(self, *arguments: str, environment: dict[str, str], changes_home: str = "") -> Result:
        """Run deploy.py as a program, from a copy of this checkout, under the harness's source and refusal guards.

        The copy is the source the program deploys and the tree compared; the checkout itself is never a fixture.
        """
        checkout = self.entry_point_source()
        program = [sys.executable, "-B", str(checkout / "deploy.py"), *arguments]

        def run() -> int:
            completed = platform_support.run_tool(program, environment)
            print(completed.output, end="")
            return completed.returncode

        return self.run_command(run, source=checkout, changes_home=changes_home)

    def run_in_checkout(
        self, program: str, *arguments: str, environment: dict[str, str], changes_home: str = ""
    ) -> Result:
        """Run Python code in a child process against a copy of this checkout, under the harness's source and refusal
        guards, as run_entry_point runs deploy.py: the code takes the copy's path as its first argument, then the
        arguments given, and runs with exactly this environment and no input, its streams kept apart in the result.
        A test's later runs reuse the copy its first made, which each run compares again.
        """
        copied = self.root / "repository"
        checkout = copied if (copied / "deploy.py").is_file() else self.entry_point_source()

        def run() -> int:
            completed = subprocess.run(
                [sys.executable, "-B", "-c", program, str(checkout), *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                stdin=subprocess.DEVNULL,
                env=environment,
                timeout=120,
                check=False,
            )
            print(completed.stdout, end="")
            print(completed.stderr, end="", file=sys.stderr)
            return completed.returncode

        return self.run_command(run, source=checkout, changes_home=changes_home)

    def run_cli(
        self,
        *arguments: str,
        stdin: str | io.StringIO = "",
        source: Path | None = None,
        home: Path | None = None,
        refusal_home: Path | None = None,
        changes_home: str = "",
    ) -> Result:
        """Run cli.main with its routing and input unchanged, enforcing the harness's deployment invariants."""
        reader = stdin if isinstance(stdin, io.StringIO) else io.StringIO(stdin)
        return self.run_command(
            lambda: cli.main(list(arguments), Paths(source or self.source, home or self.home), reader),
            source=source,
            home=refusal_home or home,
            changes_home=changes_home,
        )

    def repository_source(self) -> Path:
        """Copy this repository's deployable source, so a test deploys it even when run from a linked worktree."""
        destination = self.root / "repository"
        destination.mkdir()
        shutil.copy2(REPOSITORY_ROOT / "source.json", destination / "source.json")
        for directory in ("skills", "deploy-meta", "agents"):
            shutil.copytree(
                REPOSITORY_ROOT / directory,
                destination / directory,
                ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]"),
            )
        return destination

    def entry_point_source(self) -> Path:
        """Copy this repository's deployable source with deploy.py and the deployer beside it, a checkout to run."""
        destination = self.repository_source()
        shutil.copy2(REPOSITORY_ROOT / "deploy.py", destination / "deploy.py")
        shutil.copytree(
            REPOSITORY_ROOT / "deployer",
            destination / "deployer",
            ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]"),
        )
        return destination

    def snapshot_source(self, name: str) -> Path:
        """Copy the current fixture source so it can be deployed later as a separate source."""
        destination = self.root / name
        shutil.copytree(self.source, destination)
        return destination

    def deploy_from(self, source: Path, *arguments: str, stdin: str = "") -> Result:
        return self.deploy_ok(*arguments, stdin=stdin, source=source)

    def remove_skill(self, name: str) -> None:
        shutil.rmtree(self.source / "skills" / name)
        (self.source / "deploy-meta" / f"{name}.json").unlink()

    def configure(self, *arguments: str, stdin: str = "") -> Result:
        return self.run_command(lambda: configure.run(list(arguments), self.paths, stdin=io.StringIO(stdin)))

    def deploy_ok(self, *arguments: str, stdin: str = "", **options: Any) -> Result:
        result = self.deploy(*arguments, stdin=stdin, **options)
        assert result.code == 0, result.output
        return result

    def deploy_fails(
        self,
        *arguments: str,
        pattern: str,
        code: int = 1,
        stdin: str = "",
        home: Path | None = None,
        changes_home: str = "",
        **options: Any,
    ) -> Result:
        """Deploy, expecting exit code, output matching pattern, and, unless changes_home says why, no change.

        code is the exit the documentation states for the failure: 1 for a refusal or a failed run, the default, and 2
        for a command line the deployer rejects, which a test passes, so a usage error is never held as merely nonzero.
        The deployer finds every reason to stop before it changes a file, so the home is compared, file by file and
        byte for byte, before and after a refusal. changes_home states why a failure is expected to change it: one that
        comes after the deployment began, such as a recovery, a lock reclaim, or a run that fails partway.
        """
        result = self.deploy(*arguments, stdin=stdin, home=home, changes_home=changes_home, **options)
        assert code == result.code, result.output
        assert re.search(pattern, result.output), result.output
        return result

    def report_groups(self, output: str, title: str) -> dict[str, list[str]]:
        """Parse one '=== title ===' report into its action groups, checking each header's count."""
        lines = output.replace("\r\n", "\n").split("\n")
        groups: dict[str, list[str]] = {}
        counts: dict[str, int] = {}
        current: list[str] | None = None
        in_extra = False
        for line in lines[lines.index(f"=== {title} ===") + 1 :]:
            header = REPORT_GROUP.fullmatch(line)
            if header:
                current = groups.setdefault(header.group(1), [])
                counts[header.group(1)] = int(header.group(2))
                in_extra = False
            elif not line:
                current = None
            elif current is None or in_extra:
                continue
            elif line.startswith("    ") and current:
                current[-1] += " " + line.strip()  # a wrapped continuation of the previous item
            elif line.startswith("  "):
                current.append(line[2:])
            else:
                in_extra = True  # a diff or other detail printed under the previous item
        for action, items in groups.items():
            assert counts[action] == len(items), f"{action} header count in:\n{output}"
        return groups

    def manifest(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.manifest_file.read_text(encoding="utf-8"))
        return data

    def write_manifest(self, data: dict[str, Any]) -> None:
        self.manifest_file.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_file.write_text(json.dumps(data), encoding="utf-8")

    def rewrite_as_version_7(self, requested_bundles: list[str], source_id: str = SOURCE_ID) -> None:
        """Rewrite the manifest as the last deployer to write version 7 left it, with these bundle requests recorded
        for the source: no skill records its skill dependencies and no entry the members of its bundle requests.
        """
        data = self.manifest()
        data["manifest_version"] = 7
        for entry in data["sources"].values():
            entry.pop("requested_bundle_members", None)
            for details in entry.get("skills", {}).values():
                details.pop("skill_deps", None)
        data["sources"][source_id]["requested_bundles"] = requested_bundles
        self.write_manifest(data)

    def rewrite_as_preserved_through_the_upgrade(
        self,
        preserved: str,
        source_id: str = SOURCE_ID,
        installed_since: tuple[str, ...] = (),
        deselected: bool = False,
    ) -> None:
        """Rewrite the manifest as the first deployer to write version 8, which recorded no bundle members, left a
        version 7 manifest when it deployed the source and preserved its locally modified skill `preserved`: that skill
        keeps the skill dependencies the upgrade filled in, every other skill its source owned then, beside the exact
        ones the deployment recorded for the skills it installed, and no entry records the members of its bundle
        requests. The skills `installed_since` names are ones that deployment installed and the version 7 manifest did
        not own, so the filled-in list does not name them. With `deselected`, that deployment preserved the skill
        although the user did not select it, so the entry does not request it on its own.
        """
        data = self.manifest()
        for entry in data["sources"].values():
            entry.pop("requested_bundle_members", None)
        entry = data["sources"][source_id]
        entry["skills"][preserved]["skill_deps"] = sorted(set(entry["skills"]) - {preserved, *installed_since})
        if deselected:
            entry["requested_skills"] = [name for name in entry["requested_skills"] if name != preserved]
        self.write_manifest(data)

    def owned(self, kind: str, source_id: str = SOURCE_ID) -> dict[str, Any]:
        items: dict[str, Any] = self.manifest()["sources"].get(source_id, {}).get(kind, {})
        return items

    def skill_text(self, name: str) -> str:
        return (self.skills_dir / name / "SKILL.md").read_text(encoding="utf-8")

    def write(self, path: Path, text: str) -> None:
        """Write fixture text with LF line endings, which read_text would not preserve on Windows."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def append(self, path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def killed_while_journaling(self, op: str, item: str) -> mock._patch[mock.MagicMock | mock.AsyncMock]:
        """Kill the deployment partway through writing the journal entry for op on item.

        The patched fsops.append_line writes the first half of that entry with no newline, as a process killed
        mid-write leaves it, then raises SystemExit, which the pipeline does not reconcile, so its staging run remains.
        """
        real_append = fsops.append_line

        def append_line(path: Path, line: str) -> None:
            entry = json.loads(line)
            if entry["op"] == op and entry["item"] == item:
                self.append(path, line[: len(line) // 2])
                raise SystemExit(1)
            real_append(path, line)

        return mock.patch("deployer.fsops.append_line", side_effect=append_line)

    def unreadable(self, path: Path) -> mock._patch[Callable[..., str]]:
        """Make reading one file fail as a file another process holds does, leaving every other read alone."""
        read_text = Path.read_text

        def read(candidate: Path, *arguments: Any, **keywords: Any) -> str:
            if candidate == path:
                raise PermissionError(13, "Permission denied", str(candidate))
            return read_text(candidate, *arguments, **keywords)

        return mock.patch.object(Path, "read_text", read)

    def selection_number(self, name: str, bundle: bool = False) -> str:
        output = self.deploy("--dry-run", stdin="\n").output
        match = re.search(rf"^  \[[ *]\] ([0-9]+)\. {re.escape(name)}(?: \(([^)]*)\))?$", output, re.MULTILINE)
        if match is None:
            raise AssertionError(output)
        labels = (match.group(2) or "").split(", ")
        assert bundle == ("bundle" in labels), output
        return match.group(1)


@contextlib.contextmanager
def deployment() -> Iterator[Deployment]:
    """A Deployment in a temporary folder of its own, removed with everything in it once the block ends."""
    with tempfile.TemporaryDirectory(prefix="deploy-test.") as directory:
        deployed = Deployment()
        deployed.start(Path(directory))
        yield deployed


@pytest.fixture
def deployer() -> Iterator[Deployment]:
    """A Deployment for one pytest test, removed after it; a suite imports this fixture to use it."""
    with deployment() as deployed:
        yield deployed


class DeployerTestCase(unittest.TestCase, Deployment):
    """The unittest form of a Deployment: each test starts in a fresh one, removed after its cleanups run."""

    def setUp(self) -> None:
        self.start(Path(self.enterContext(tempfile.TemporaryDirectory(prefix="deploy-test."))))
