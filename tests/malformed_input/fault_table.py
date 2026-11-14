"""The shape of the fault table test_faults.py runs: the environment faults, and one driver per command of an entry
point that the harness runs under them.

An entry point is what tests/validation/exit_tables.py finds: a module whose `__main__` block calls run_main. A
driver names one of its commands, the arguments that run it in a throwaway fixture, the files of that fixture the
command reads, which the file faults are placed at one at a time, and whether it runs git or gh, which the git and gh
faults replace. faults.py gathers the drivers, grouped by area in the faults_*.py modules beside it, and names each
entry point or command the harness cannot drive, with the reason, in FAULTS_NOT_RUN.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]

# Every environment fault the harness places or injects, with what it is. A fault placed at a file is run once for each
# file a driver names; git's and gh's are run once for a driver that runs that program.
UNREADABLE = "unreadable"
DIRECTORY = "directory"
MISSING = "missing"
LINK_TO_NOTHING = "link_to_nothing"
STAT_RAISES = "stat_raises"
GIT_EXIT = "git_exit_128"
GIT_TIMEOUT = "git_timeout"
GH_FAILURE = "gh_failure"
FILE_FAULTS = (UNREADABLE, DIRECTORY, MISSING, LINK_TO_NOTHING, STAT_RAISES)
GIT_FAULTS = (GIT_EXIT, GIT_TIMEOUT)
GH_FAULTS = (GH_FAILURE,)
FAULTS = {
    UNREADABLE: "a file the command reads, there but not readable: held by a handle that shares nothing on Windows, "
    "given no permissions elsewhere",
    DIRECTORY: "a directory in the place of a file the command reads",
    MISSING: "a file the command reads, removed",
    LINK_TO_NOTHING: "a symbolic link to nothing in the place of a file the command reads, which reads as the file "
    "missing: refused as a missing one is, or, where the command reads a missing one as absent, read so, and never "
    "written through to create the file it leads to",
    STAT_RAISES: "a file the command reads whose every stat raises PermissionError",
    GIT_EXIT: "every git command exits 128 with the reason git gives for a broken checkout",
    GIT_TIMEOUT: "every git command runs past its time limit",
    GH_FAILURE: "every gh command fails with an answer GitHub gives for a request it refuses",
}
# The reason a stat fault does not apply to a file a command opens and reads without a stat.
OPENED_ONLY = "the command opens and reads the file without a stat, so a stat that raises never reaches it"
# A fault the content corpus holds instead, since it is a record's shape, not the environment's.
CONTENT_FAULTS = {
    "wrong_type": "a member of a record of another type: test_malformed_input.py's field cases, for every reader of "
    "a record",
}


@dataclass(frozen=True)
class Fixture:
    """A throwaway fixture: its root, the home the command runs with, the folder it runs in, and the copy of this
    checkout a command run from it reads as its repository, one git commit, shared by the runs of one suite process
    and put back as it was after each."""

    root: Path
    home: Path
    work: Path
    checkout: Path

    def values(self) -> dict[str, str]:
        return {"root": str(self.root), "home": str(self.home), "work": str(self.work), "checkout": str(self.checkout)}

    def git(self, *arguments: str, folder: Path | None = None) -> None:
        """Run git in the fixture, as a suite builds a repository: not through the fault's runner."""
        subprocess.run(
            ["git", *arguments], cwd=folder or self.work, check=True, capture_output=True, env=git_environment(self)
        )

    def repository(self, files: Mapping[str, str] | None = None, folder: Path | None = None) -> Path:
        """A git repository at folder, the work folder by default, with files committed on main."""
        where = folder or self.work
        where.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main", folder=where)
        for name, text in (files or {"README.md": "# Fixture\n"}).items():
            path = where / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.git("add", "-A", folder=where)
        self.git("commit", "-q", "-m", "fixture", folder=where)
        return where


def git_environment(fixture: Fixture) -> dict[str, str]:
    """The environment git runs with in a fixture: its home, no system or global configuration, and an identity."""
    import os

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key not in {"HOME", "USERPROFILE", "XDG_CONFIG_HOME"}
    }
    return {
        **environment,
        "HOME": str(fixture.home),
        "USERPROFILE": str(fixture.home),
        "APPDATA": str(fixture.home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(fixture.home / "AppData" / "Local"),
        "XDG_CONFIG_HOME": str(fixture.home / ".config"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(fixture.home / ".gitconfig"),
        # No automatic maintenance, which a commit can start in the background and which writes in .git while a run
        # is compared with how it found its folders.
        "GIT_CONFIG_PARAMETERS": "'maintenance.auto'='false' 'gc.auto'='0'",
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_TERMINAL_PROMPT": "0",
    }


@dataclass(frozen=True)
class Driver:
    """One command of an entry point, run under faults.

    `entry` is the entry point's path from the repository root, and `command` its subcommand, or "" for one that has
    none. `arguments` are its command line, each formatted with the fixture's `{root}`, `{home}`, `{work}`, and
    `{checkout}`, the copy of this checkout a `from_checkout` driver runs from. `prepare` builds the fixture before each
    run; `inputs` are the files below the fixture root the command reads, each run under every file fault; `git` and
    `gh` say it runs that program, so it is run under its faults. `not_run` names each fault of its kinds it is not run
    under, or `<fault> <input>` for one input alone, with the reason. A driver `from_checkout` runs the entry point's
    copy in the fixture's checkout, for a command that reads the repository it lies in; `outputs` are the files and
    folders it would write, beside the home and the work folder, which a refused run leaves as it found them. `inputs`
    and `outputs` are formatted as the arguments are.
    """

    entry: str
    command: str
    arguments: tuple[str, ...]
    prepare: Callable[[Fixture], None] | None = None
    inputs: tuple[str, ...] = ()
    git: bool = False
    gh: bool = False
    not_run: Mapping[str, str] = field(default_factory=dict)
    # Variables the command runs with beside the fixture's home, each formatted as the arguments are.
    environment: Mapping[str, str] = field(default_factory=dict)
    from_checkout: bool = False
    outputs: tuple[str, ...] = ()
    # Each run whose refusal is a line led by a word its docstring does not name, by its fault, or `<fault> <input>`
    # for one input alone, with the open issue that gives it a FAILED or documented line.
    refusal_gaps: Mapping[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return f"{self.entry} {self.command}".strip()

    def refusal_gap(self, fault: str, path: str | None) -> str:
        """The refusal gap refusal_gaps names for one run, by its input or its fault, or "" for none. A link to nothing
        reads as the file missing, so its run has the missing file's gap."""
        if fault == LINK_TO_NOTHING:
            fault = MISSING
        return self.refusal_gaps.get(f"{fault} {path}", "") or self.refusal_gaps.get(fault, "")

    def absent_is_an_answer(self, path: str) -> bool:
        """Whether the command reads the input missing as an answer rather than a fault: `not_run` names the missing
        fault for it, with the reason. A link to nothing at such an input is held only to never being written
        through."""
        return MISSING in self.not_run or f"{MISSING} {path}" in self.not_run

    def runs(self) -> list[tuple[str, str | None]]:
        """Each fault the driver runs, with the input it is placed at, or None for git's and gh's."""
        found: list[tuple[str, str | None]] = [
            (fault, path)
            for path in self.inputs
            for fault in FILE_FAULTS
            if fault not in self.not_run and f"{fault} {path}" not in self.not_run
        ]
        found += [(fault, None) for fault in GIT_FAULTS if self.git and fault not in self.not_run]
        found += [(fault, None) for fault in GH_FAULTS if self.gh and fault not in self.not_run]
        return found
