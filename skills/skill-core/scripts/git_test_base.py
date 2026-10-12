"""The test base every regression suite that runs git inherits, so no suite's git reads the shell that started it.

Git takes configuration, hooks, and even the repository it works on from the environment: a command-scope setting
in `GIT_CONFIG_COUNT` outranks every file, a global `core.hooksPath` runs the user's hooks on a fixture's commit, and
`GIT_DIR` or `GIT_INDEX_FILE`, which a git hook exports, points every command at another repository. `isolated_git`
gives git a temporary home of its own instead:

- `HOME` names that home, where git looks for the user's files when nothing else names them;
- `GIT_CONFIG_GLOBAL` names a `.gitconfig` in it holding the fixture identity, `core.hooksPath` set to an empty
  directory, and any settings the caller adds; `GIT_CONFIG_NOSYSTEM` and `GIT_ATTR_NOSYSTEM` keep the installation's
  configuration and attributes out;
- `GIT_TERMINAL_PROMPT` is 0 and `GIT_EDITOR` is `:`, which git takes as no editor, so nothing waits on a person;
- every other variable named `GIT_*` is removed, except `GIT_EXEC_PATH`, which picks the git installation, and
  `GIT_BASH`, this repository's own name for Git Bash; so is `XDG_CONFIG_HOME`, under which git finds its attributes
  and ignore files.

Variables git reads that stay as the shell gave them, and why: `EDITOR` and `VISUAL`, which `GIT_EDITOR` outranks;
`PAGER`, since git pages only to a terminal and a suite captures its output; `EMAIL`, since `user.email` is set;
`HOMEDRIVE`, `HOMEPATH`, and `USERPROFILE`, which git reads only without `HOME`, and the last of which also places the
profile folder where per-user installs such as PowerShell's modules live, so a suite still finds the tools it runs; and
`SSH_ASKPASS` and the proxy variables, since no suite reaches a network remote and no credential helper is configured.
A suite whose code writes under the profile folder gives that code a temporary home itself, as the deployer suites do.

`GitTestCase` runs each test, its setUp, tearDown, and cleanups included, in a home of its own, whether or not a
subclass's setUp calls its parent, and its class fixture in another; a subclass that defines setUpClass starts it
with `super().setUpClass()`. A suite whose setUpModule builds fixtures with git starts it with `isolate_module()`.
`git_config` adds settings to the global file, such as a default branch a suite's fixtures name.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

FIXTURE_NAME = "Fixture"
FIXTURE_EMAIL = "fixture@example.invalid"
# The GIT_* variables kept: the git installation's helper directory, and this repository's Git Bash, which is not git's.
KEPT_GIT_VARIABLES = frozenset({"GIT_EXEC_PATH", "GIT_BASH"})
# Variables outside GIT_* removed as well: the directory git finds its user attributes and ignore files under.
REMOVED_VARIABLES = frozenset({"XDG_CONFIG_HOME"})
SETTING_PART = re.compile(r"[A-Za-z][A-Za-z0-9-]*")


@dataclass(frozen=True)
class GitHome:
    """The temporary home git runs in: the folder, its global configuration file, and its empty hooks folder."""

    home: Path
    config: Path
    hooks: Path


def _quoted(value: str) -> str:
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
        lines.append(f"[{section}]" if subsection is None else f"[{section} {_quoted(subsection)}]")
        lines += [f"\t{key} = {_quoted(value)}" for key, value in entries]
    return "".join(f"{line}\n" for line in lines)


def _removed(name: str) -> bool:
    return (name.startswith("GIT_") and name not in KEPT_GIT_VARIABLES) or name in REMOVED_VARIABLES


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


def isolate_module(settings: Mapping[str, str] | None = None) -> GitHome:
    """Isolate git for the rest of a suite's setUpModule and its tests, until the module's cleanups run."""
    return unittest.enterModuleContext(isolated_git(settings))


class GitTestCase(unittest.TestCase):
    """A test case whose every test, and whose class fixture, runs git isolated in a temporary home of its own."""

    # Settings added to the global configuration file, as {"section.key": value}.
    git_config: ClassVar[Mapping[str, str]] = {}
    # The home the running test's git uses.
    git_home: GitHome

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.enterClassContext(isolated_git(cls.git_config))

    def run(self, result: unittest.TestResult | None = None) -> unittest.TestResult | None:
        with isolated_git(self.git_config) as self.git_home:
            return super().run(result)

    def debug(self) -> None:
        with isolated_git(self.git_config) as self.git_home:
            super().debug()
