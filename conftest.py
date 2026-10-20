"""pytest's setup for every Python regression suite: git isolated from the shell for each test and each fixture, and
one shard of a suite when the runner splits it.

tests/run_validation.py starts each Python suite, or each shard of a large one, as `python -B -m pytest <suite>` from
the repository root, which pytest finds as its root by pyproject.toml, so this file is loaded for every suite under
tests/ and each skill's scripts/. pytest puts the suite's own directory first on sys.path, as `python <suite>` did.

Git takes configuration, hooks, and even the repository it works on from the environment: a command-scope setting in
`GIT_CONFIG_COUNT` outranks every file, a global `core.hooksPath` runs the user's hooks on a fixture's commit, and
`GIT_DIR` or `GIT_INDEX_FILE`, which a git hook exports, points every command at another repository. The autouse
fixtures below give git a temporary home of its own instead, for each module, each class, and each test, so a test,
its setUp, tearDown, and cleanups, a setUpClass, and a setUpModule all run isolated, whatever they build:

- `HOME` names that home, where git looks for the user's files when nothing else names them;
- `GIT_CONFIG_GLOBAL` names a `.gitconfig` in it holding the fixture identity, `core.hooksPath` set to an empty
  directory, git's automatic maintenance turned off, and the settings a test class or suite module names as its
  `git_config`, such as a default branch its fixtures name; `GIT_CONFIG_NOSYSTEM` and `GIT_ATTR_NOSYSTEM` keep the
  installation's configuration and attributes out;
- `GIT_TERMINAL_PROMPT` is 0 and `GIT_EDITOR` is `:`, which git takes as no editor, so nothing waits on a person;
- every other variable named `GIT_*` is removed, except `GIT_EXEC_PATH`, which picks the git installation, and
  `GIT_BASH`, this repository's own name for Git Bash; so is `XDG_CONFIG_HOME`, under which git finds its attributes
  and ignore files. The repository-selecting ones, `bounded_process.REPOSITORY_VARIABLES`, are removed whatever is
  kept, as every command a skill starts runs without them.

Variables git reads that stay as the shell gave them, and why: `EDITOR` and `VISUAL`, which `GIT_EDITOR` outranks;
`PAGER`, since git pages only to a terminal and a suite captures its output; `EMAIL`, since `user.email` is set;
`HOMEDRIVE`, `HOMEPATH`, and `USERPROFILE`, which git reads only without `HOME`, and the last of which also places the
profile folder where per-user installs such as PowerShell's modules live, so a suite still finds the tools it runs; and
`SSH_ASKPASS` and the proxy variables, since no suite reaches a network remote and no credential helper is configured.
A suite whose code writes under the profile folder gives that code a temporary home itself, as the deployer suites do.

A test reads the home its git uses as `self.git_home` on a test case, or through the `git_home` fixture.
tests/validation/suite_discovery.py holds that these fixtures exist here, autouse, and that no suite or other
conftest.py overrides one.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "skills" / "ejryan89-skill-core" / "scripts"))
# Appended rather than put first, so no policy module's name shadows a module a suite imports.
sys.path.append(str(Path(__file__).resolve().parent / "tests" / "validation"))

from bounded_process import REPOSITORY_VARIABLES
from dealing import deal, shard_of

# The deployer suites check each run through the plain asserts in tests/deployer/harness.py, which they import by its
# bare name; pytest rewrites an assert to show both sides only in a test module or one named to it before its import.
pytest.register_assert_rewrite("harness")

# The operating-system names this module states on purpose, with the reason, which the platform policy reads.
PLATFORM_ALLOWED = {
    "GIT_BASH": "names the variable the isolation keeps as the shell gave it, so a suite finds the Git Bash the "
    "deployer's platform_support finds; nothing here reads it or branches on the platform",
}
FIXTURE_NAME = "Fixture"
FIXTURE_EMAIL = "fixture@example.invalid"
# The GIT_* variables kept: the git installation's helper directory, and this repository's Git Bash, which is not git's.
KEPT_GIT_VARIABLES = frozenset({"GIT_EXEC_PATH", "GIT_BASH"})
# Variables outside GIT_* removed as well: the directory git finds its user attributes and ignore files under.
REMOVED_VARIABLES = frozenset({"XDG_CONFIG_HOME"})
SETTING_PART = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
# Git's automatic maintenance, off, so nothing git starts outlives the command that started it: after a commit or a
# fetch git otherwise starts `git maintenance run --auto`, detached where the platform allows, which writes
# .git/objects/maintenance.lock while a suite snapshots or removes the fixture. `maintenance.auto` stops that start,
# `gc.auto` stops the packing of `gc --auto` from any command that still runs it, and the two `autoDetach` settings
# keep whatever runs in the foreground: git reads `gc.autoDetach` when `maintenance.autoDetach` is unset or unknown.
NO_AUTOMATIC_MAINTENANCE = {
    "maintenance.auto": "false",
    "maintenance.autoDetach": "false",
    "gc.auto": "0",
    "gc.autoDetach": "false",
}
# The name a test class or a suite module gives the settings it adds to the global configuration, as
# {"section.key": value}; a class's settings take the place of its module's.
GIT_CONFIG = "git_config"
# The autouse fixtures that isolate git, by scope; suite_discovery.py holds that each exists here and is autouse.
ISOLATION_FIXTURES = {"module": "isolated_git_module", "class": "isolated_git_class", "function": "git_home"}
# The option that runs one shard of a suite, and the one that records a heavy test's seconds.
SHARD_OPTION = "--suite-shard"
SECONDS_OPTION = "--test-seconds"


@dataclass(frozen=True)
class GitHome:
    """The temporary home git runs in: the folder, its global configuration file, and its empty hooks folder."""

    home: Path
    config: Path
    hooks: Path


def quoted(value: str) -> str:
    """A git configuration value or subsection name in double quotes, its backslashes and quotes escaped."""
    if any(character in value for character in "\r\n\0"):
        raise ValueError(f"a git configuration value cannot hold a line break or NUL: {value!r}")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def config_text(settings: Mapping[str, str]) -> str:
    """The text of a git configuration file holding each `section.key` or `section.subsection.key` setting."""
    sections: dict[tuple[str, str | None], list[tuple[str, str]]] = {}
    for name, value in settings.items():
        parts = name.split(".")
        subsection = ".".join(parts[1:-1]) if len(parts) > 2 else None
        named = len(parts) > 1 and SETTING_PART.fullmatch(parts[0]) and SETTING_PART.fullmatch(parts[-1])
        if not named or subsection == "":
            raise ValueError(f"not a git setting name: {name!r}")
        sections.setdefault((parts[0], subsection), []).append((parts[-1], value))
    lines: list[str] = []
    for (section, subsection), entries in sections.items():
        lines.append(f"[{section}]" if subsection is None else f"[{section} {quoted(subsection)}]")
        lines += [f"\t{key} = {quoted(value)}" for key, value in entries]
    return "".join(f"{line}\n" for line in lines)


def _removed(name: str) -> bool:
    return (
        name.upper() in REPOSITORY_VARIABLES
        or (name.startswith("GIT_") and name not in KEPT_GIT_VARIABLES)
        or name in REMOVED_VARIABLES
    )


@contextmanager
def isolated_git(settings: Mapping[str, str] | None = None) -> Iterator[GitHome]:
    """Run the block with git isolated in a new temporary home, then restore each variable and remove the home."""
    with tempfile.TemporaryDirectory(prefix="git-test-home.") as directory:
        # Expand 8.3 short names such as RUNNER~1, so the paths a test compares are the ones git prints.
        home = Path(directory).resolve()
        hooks = home / "no-hooks"
        hooks.mkdir()
        config = home / ".gitconfig"
        values = {"user.name": FIXTURE_NAME, "user.email": FIXTURE_EMAIL, "core.hooksPath": str(hooks)}
        values.update(NO_AUTOMATIC_MAINTENANCE)
        config.write_text(config_text({**values, **(settings or {})}), encoding="utf-8")
        patch = {
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_EDITOR": ":",
        }
        removed = [name for name in os.environ if _removed(name)]
        saved = {name: os.environ.get(name) for name in [*removed, *patch]}
        try:
            for name in removed:
                del os.environ[name]
            os.environ.update(patch)
            yield GitHome(home, config, hooks)
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def git_settings(*owners: Any) -> Mapping[str, str]:
    """The settings the first of a test's class and its module that names any gives as its git_config."""
    for owner in owners:
        settings = getattr(owner, GIT_CONFIG, None) if owner is not None else None
        if settings:
            found: Mapping[str, str] = settings
            return found
    return {}


@pytest.fixture(autouse=True, scope="module")
def isolated_git_module(request: pytest.FixtureRequest) -> Iterator[GitHome]:
    """Isolate git for a suite module's setUpModule, its module fixtures, and its cleanups."""
    with isolated_git(git_settings(request.module)) as home:
        yield home


@pytest.fixture(autouse=True, scope="class")
def isolated_git_class(request: pytest.FixtureRequest) -> Iterator[GitHome]:
    """Isolate git for a test class's setUpClass, its class fixtures, and its class cleanups."""
    with isolated_git(git_settings(request.cls, request.module)) as home:
        yield home


@pytest.fixture(autouse=True)
def git_home(request: pytest.FixtureRequest) -> Iterator[GitHome]:
    """Isolate git for one test, its setUp, tearDown, and cleanups, and give a test case the home as self.git_home."""
    with isolated_git(git_settings(request.cls, request.module)) as home:
        if request.instance is not None:
            request.instance.git_home = home
        yield home


def recorded_seconds(arguments: list[str]) -> dict[str, float]:
    """The --test-seconds `<test>=<seconds>` arguments, each test named as pytest names it within its suite."""
    seconds: dict[str, float] = {}
    for argument in arguments:
        test, separator, value = argument.partition("=")
        try:
            seconds[test] = float(value)
        except ValueError:
            seconds[test] = 0
        if not separator or seconds[test] <= 0:
            raise pytest.UsageError(f"{SECONDS_OPTION} expects <test>=<seconds> above 0, found {argument!r}")
    return seconds


def name_in_suite(item: pytest.Item) -> str:
    """A test's name within its suite: its node ID without the suite's path, such as `Class::test`."""
    return item.nodeid.partition("::")[2]


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("suite shards", "the runner's split of one suite into shards")
    group.addoption(SHARD_OPTION, metavar="INDEX/COUNT", help="run shard INDEX of COUNT of the collected tests")
    group.addoption(
        SECONDS_OPTION,
        action="append",
        default=[],
        metavar="TEST=SECONDS",
        help="a recorded test's seconds, which the shards are dealt by; repeatable",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep only this shard's tests when the runner asks for one: every shard deals the suite's tests out the same way,
    so the shards of a suite run each of its tests exactly once, and a recorded test runs on a shard of its own while
    the suite's other tests fit on its other shards."""
    text = config.getoption(SHARD_OPTION.lstrip("-").replace("-", "_"))
    seconds = recorded_seconds(config.getoption(SECONDS_OPTION.lstrip("-").replace("-", "_")))
    if text is None:
        if seconds:
            raise pytest.UsageError(f"{SECONDS_OPTION} needs {SHARD_OPTION}")
        return
    try:
        index, count = shard_of(text)
    except ValueError as error:
        raise pytest.UsageError(f"{SHARD_OPTION} expects {error}") from error
    names = {name_in_suite(item) for item in items}
    unknown = sorted(set(seconds) - names)
    if unknown:
        raise pytest.UsageError(f"the suite defines no test {', '.join(unknown)}")
    shards = deal(sorted(names), count, seconds)
    kept = [item for item in items if shards[name_in_suite(item)] == index - 1]
    config.hook.pytest_deselected(items=[item for item in items if shards[name_in_suite(item)] != index - 1])
    items[:] = kept
