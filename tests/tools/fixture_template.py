"""Git fixtures built once per process and copied for each test, and the one removal of a fixture's folder.

Building a fixture repository starts a git process for each init, commit, tag, clone, and push, which costs seconds a
test on Windows, where copying the built folder costs a fraction of one. So a suite declares each fixture once, as a
FixtureTemplate over the function that builds it, and each test copies it into a folder of its own: every test still
gets its own repositories, so the tests stay isolated and independent of their order across shards.

A copy keeps the template's commits and tags, which share a tree and a message with a fresh build's but not its
timestamps, so no test pins a hash. A git config that names another repository of the fixture by absolute path, such
as a clone's `remote.origin.url` or a `url.<base>.insteadOf` section, is rewritten to name the copy's own, so a push
from a copy reaches the copy's remote and never the template's.

The repository-skill and tools suites that build a git fixture import it from beside them or put tests/tools on
sys.path; it holds no test, and tests/tools/test_fixture_template.py is its suite.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import threading
from collections.abc import Callable, Sequence
from pathlib import Path, PurePath


def remove_tree(path: Path) -> None:
    """Remove a folder and everything in it, git's read-only object files included, which Windows will not delete
    until they are made writable."""

    def writable(function: Callable[..., object], target: str, *_: object) -> None:
        Path(target).chmod(stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, onexc=writable)


def path_forms(root: PurePath) -> list[str]:
    """The ways a git config may spell an absolute path under `root`: as a value git escapes, with each backslash
    doubled; as the platform writes it; and with forward slashes, as in a `url.<base>` section name. The escaped form
    comes first, since replacing it leaves no other form behind; on POSIX the three are one."""
    native = str(root)
    return list(dict.fromkeys([native.replace("\\", "\\\\"), native, root.as_posix()]))


def git_configs(root: Path) -> list[Path]:
    """The config file of every repository under `root`: a work tree's `.git/config` and a bare repository's own."""
    return sorted(path for path in root.rglob("config") if path.is_file() and (path.parent / "HEAD").is_file())


def relocate_configs(root: Path, template: Path) -> None:
    """Rewrite each path under `template` that a git config under `root` names to the same path under `root`."""
    replacements = list(zip(path_forms(template), path_forms(root), strict=True))
    for config in git_configs(root):
        text = config.read_bytes()
        rewritten = text
        for old, new in replacements:
            rewritten = rewritten.replace(old.encode("utf-8"), new.encode("utf-8"))
        if rewritten != text:
            config.write_bytes(rewritten)


class FixtureTemplate:
    """A fixture `build` makes in an empty folder, built the first time a test copies it and once per process."""

    def __init__(self, name: str, build: Callable[[Path], object]) -> None:
        self.name = name
        self.build = build
        self._lock = threading.Lock()
        self._directory: tempfile.TemporaryDirectory[str] | None = None

    @property
    def root(self) -> Path:
        """The template's folder, built on the first call; its long path, the one git prints, where Windows gives the
        temporary folder a short one."""
        with self._lock:
            if self._directory is None:
                directory = tempfile.TemporaryDirectory(prefix=f"{self.name}-template.")
                built = Path(os.path.realpath(directory.name)) / self.name
                built.mkdir()
                try:
                    self.build(built)
                except BaseException:
                    directory.cleanup()
                    raise
                self._directory = directory
            return Path(os.path.realpath(self._directory.name)) / self.name

    def copy(self, destination: Path) -> Path:
        """Copy the template into `destination`, which may exist and be empty, and point each git config in the copy
        at the copy's own repositories. Returns `destination`."""
        template = self.root
        shutil.copytree(template, destination, symlinks=True, dirs_exist_ok=True)
        relocate_configs(destination, template)
        return destination


def repository_shape(root: Path, repositories: Sequence[str]) -> dict[str, str]:
    """What a fixture's repositories hold, with `root` written as `<root>`, for a test that a copy equals a fresh
    build: each ref's tree, peeled tree, subject, and upstream; each commit's tree and subject; HEAD; the local
    config; and a work tree's status. Hashes are left out, since a fresh build's commits carry other timestamps."""

    def output(repository: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments], capture_output=True, text=True, encoding="utf-8", check=False
        )
        if result.returncode != 0:
            raise AssertionError(f"git {' '.join(arguments)} in {repository} failed: {result.stderr}")
        text = result.stdout
        for form in path_forms(root):
            text = text.replace(form, "<root>")
        return text

    def commits(repository: Path) -> str:
        """Each commit's tree, parent count, and subject, sorted, since commits made in the same second have no
        order their dates settle."""
        lines = []
        for line in output(repository, "log", "--all", "--format=%T|%P|%s").splitlines():
            tree, parents, subject = line.split("|", 2)
            lines.append(f"{tree} {len(parents.split())} {subject}\n")
        return "".join(sorted(lines))

    shape: dict[str, str] = {}
    for name in repositories:
        repository = root / name
        shape[f"{name} refs"] = output(
            repository, "for-each-ref", "--format=%(refname) %(objecttype) %(tree) %(*tree) %(subject) %(upstream)"
        )
        shape[f"{name} commits"] = commits(repository)
        shape[f"{name} head"] = output(repository, "rev-parse", "--symbolic-full-name", "HEAD")
        shape[f"{name} config"] = output(repository, "config", "--local", "--list")
        if output(repository, "rev-parse", "--is-bare-repository").strip() == "false":
            shape[f"{name} status"] = output(repository, "status", "--porcelain", "--branch", "--untracked-files=all")
    return shape
