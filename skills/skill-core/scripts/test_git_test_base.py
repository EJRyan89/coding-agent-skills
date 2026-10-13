"""Regression tests for the test base that isolates every suite's git from the shell that started it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import git_test_base
from git_test_base import GitHome, GitTestCase, config_text, isolate_module, isolated_git

SCRIPTS = Path(__file__).resolve().parent
# What the shell that starts a suite may hold: command-scope configuration, a hook's repository variables, an
# identity, transports, tracing, an editor, and a user configuration directory.
INHERITED = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "safe.bareRepository",
    "GIT_CONFIG_VALUE_0": "explicit",
    "GIT_CONFIG_PARAMETERS": "'core.hookspath'='elsewhere'",
    "GIT_CONFIG": "elsewhere.gitconfig",
    "GIT_CONFIG_SYSTEM": "system.gitconfig",
    "GIT_DIR": "elsewhere/.git",
    "GIT_WORK_TREE": "elsewhere",
    "GIT_INDEX_FILE": "elsewhere/.git/index",
    "GIT_OBJECT_DIRECTORY": "elsewhere/.git/objects",
    "GIT_COMMON_DIR": "elsewhere/.git",
    "GIT_CEILING_DIRECTORIES": "elsewhere",
    "GIT_TEMPLATE_DIR": "templates",
    "GIT_AUTHOR_NAME": "Someone",
    "GIT_AUTHOR_EMAIL": "someone@example.invalid",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00",
    "GIT_COMMITTER_NAME": "Someone",
    "GIT_COMMITTER_EMAIL": "someone@example.invalid",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00",
    "GIT_SSH_COMMAND": "ssh -i key",
    "GIT_ASKPASS": "askpass",
    "GIT_SEQUENCE_EDITOR": "editor",
    "GIT_TRACE": "1",
    "GIT_LITERAL_PATHSPECS": "1",
    "XDG_CONFIG_HOME": "config home",
}
# Variables the base sets, against the values it gives each, the home aside.
SET = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_EDITOR": ":",
}
# Variables the shell's values reach git through unchanged.
KEPT = {
    "GIT_EXEC_PATH": "exec path",
    "GIT_BASH": "C:/Program Files/Git/bin/bash.exe",
    "EDITOR": "editor",
    "VISUAL": "visual",
    "PAGER": "pager",
    "EMAIL": "someone@example.invalid",
    "SSH_ASKPASS": "askpass",
    "USERPROFILE": "profile folder",
    "HOMEDRIVE": "drive",
    "HOMEPATH": "home path",
}
HOME_VARIABLES = ("HOME", "GIT_CONFIG_GLOBAL")
# The settings that turn git's automatic maintenance off, as the global configuration of an isolated home holds them.
MAINTENANCE_OFF = {
    "maintenance.auto": "false",
    "maintenance.autoDetach": "false",
    "gc.auto": "0",
    "gc.autoDetach": "false",
}
MAINTENANCE_OFF_TEXT = (
    '[maintenance]\n\tauto = "false"\n\tautoDetach = "false"\n[gc]\n\tauto = "0"\n\tautoDetach = "false"\n'
)


def environment_of(names: list[str]) -> dict[str, str | None]:
    return {name: os.environ.get(name) for name in names}


def git(*arguments: str, environment: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, **environment} if environment else None,
        check=False,
    )


def maintenance_started(trace: Path) -> list[list[str]]:
    """The command line of each maintenance or gc process git started, from a GIT_TRACE2_EVENT trace."""
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    started = [event["argv"] for event in events if event.get("event") == "child_start"]
    return [argv for argv in started if "maintenance" in argv or "gc" in argv]


class ConfigTextTests(GitTestCase):
    def test_settings_are_written_quoted_by_section_and_subsection(self) -> None:
        text = config_text(
            {
                "user.name": "Fixture",
                "core.hooksPath": "C:\\no hooks\\",
                "remote.origin.url": 'a "quoted" url',
                "user.email": "fixture@example.invalid",
                "url.C:\\base.insteadOf": "x",
            }
        )
        self.assertEqual(
            '[user]\n\tname = "Fixture"\n\temail = "fixture@example.invalid"\n'
            '[core]\n\thooksPath = "C:\\\\no hooks\\\\"\n'
            '[remote "origin"]\n\turl = "a \\"quoted\\" url"\n'
            '[url "C:\\\\base"]\n\tinsteadOf = "x"\n',
            text,
        )

    def test_git_reads_each_value_back_unchanged(self) -> None:
        values = {"core.hooksPath": 'C:\\a b\\"c"\\', "remote.my.origin.url": "x\\y", "user.name": "Fix ture"}
        config = self.git_home.home / "written"
        config.write_text(config_text(values), encoding="utf-8")
        for name, value in values.items():
            read = git("config", "--file", str(config), "--get", name)
            self.assertEqual((0, f"{value}\n"), (read.returncode, read.stdout), name)

    def test_a_name_without_a_section_and_key_or_a_value_with_a_line_break_is_refused(self) -> None:
        for name in ("user", ".name", "user.", "1user.name", "user.na_me", "user..name"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "not a git setting name"):
                config_text({name: "x"})
        for value in ("a\nb", "a\rb", "a\0b"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "line break or NUL"):
                config_text({"user.name": value})


class IsolatedGitTests(GitTestCase):
    def test_each_variable_is_set_removed_or_kept_and_restored_afterwards(self) -> None:
        names = [*INHERITED, *SET, *KEPT, *HOME_VARIABLES]
        with mock.patch.dict(os.environ, {**INHERITED, **KEPT}):
            before = environment_of(names)
            with isolated_git() as isolated:
                for name in INHERITED:
                    self.assertNotIn(name, os.environ)
                self.assertEqual(SET, {name: os.environ.get(name) for name in SET})
                self.assertEqual(KEPT, {name: os.environ.get(name) for name in KEPT})
                self.assertEqual(
                    [str(isolated.home), str(isolated.home / ".gitconfig")],
                    [os.environ.get(name) for name in HOME_VARIABLES],
                )
            self.assertEqual(before, environment_of(names))
        self.assertFalse(isolated.home.exists())

    def test_variables_the_shell_did_not_set_are_removed_again(self) -> None:
        for name in [*SET, *HOME_VARIABLES]:
            os.environ.pop(name, None)
        with isolated_git():
            pass
        self.assertEqual({}, {name: os.environ[name] for name in [*SET, *HOME_VARIABLES] if name in os.environ})

    def test_the_home_holds_the_fixture_identity_an_empty_hooks_folder_and_added_settings(self) -> None:
        with isolated_git({"init.defaultBranch": "main"}) as isolated:
            self.assertEqual(Path(tempfile.gettempdir()).resolve(), isolated.home.parent)
            self.assertTrue(isolated.home.name.startswith("git-test-home."))
            self.assertEqual(GitHome(isolated.home, isolated.home / ".gitconfig", isolated.home / "no-hooks"), isolated)
            self.assertEqual([], list(isolated.hooks.iterdir()))
            self.assertEqual(
                '[user]\n\tname = "Fixture"\n\temail = "fixture@example.invalid"\n'
                f"[core]\n\thooksPath = {git_test_base._quoted(str(isolated.hooks))}\n"
                f'{MAINTENANCE_OFF_TEXT}[init]\n\tdefaultBranch = "main"\n',
                isolated.config.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                ["Fixture", "fixture@example.invalid", str(isolated.hooks), "main"],
                [
                    git("config", "--get", name).stdout.rstrip("\n")
                    for name in ("user.name", "user.email", "core.hooksPath", "init.defaultBranch")
                ],
            )

    def test_command_scope_configuration_no_longer_reaches_git(self) -> None:
        with mock.patch.dict(os.environ, {name: INHERITED[name] for name in list(INHERITED)[:3]}):
            self.assertEqual("explicit\n", git("config", "--get", "safe.bareRepository").stdout)
            with isolated_git():
                read = git("config", "--get", "safe.bareRepository")
                self.assertEqual((1, ""), (read.returncode, read.stdout))
            self.assertEqual("explicit\n", git("config", "--get", "safe.bareRepository").stdout)

    def test_the_environment_is_restored_and_the_home_removed_when_the_block_fails(self) -> None:
        before = dict(os.environ)
        with self.assertRaisesRegex(LookupError, "inside"), isolated_git() as isolated:
            raise LookupError("inside")
        self.assertEqual(before, dict(os.environ))
        self.assertFalse(isolated.home.exists())


class AutomaticMaintenanceTests(GitTestCase):
    """Nothing git starts in a fixture outlives the command that started it, so a suite sees the same tree each time."""

    class_fixture_reads: ClassVar[dict[str, str]]

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.class_fixture_reads = {name: git("config", "--global", "--get", name).stdout for name in MAINTENANCE_OFF}

    def test_the_global_configuration_turns_automatic_maintenance_off_for_a_test_and_a_class_fixture(self) -> None:
        expected = {name: f"{value}\n" for name, value in MAINTENANCE_OFF.items()}
        self.assertEqual(expected, {name: git("config", "--global", "--get", name).stdout for name in MAINTENANCE_OFF})
        self.assertEqual(expected, self.class_fixture_reads)

    def test_a_fixture_commit_and_fetch_start_no_maintenance_and_leave_no_lock(self) -> None:
        root = self.git_home.home / "fixture"
        origin, clone = root / "origin", root / "clone"
        trace = self.git_home.home / "trace.json"
        traced = {"GIT_TRACE2_EVENT": str(trace)}
        self.assertEqual(0, git("init", "-q", str(origin)).returncode)
        for message in ("first", "second"):
            (origin / "file.txt").write_text(f"{message}\n", encoding="utf-8")
            self.assertEqual(0, git("-C", str(origin), "add", "file.txt").returncode)
            self.assertEqual(0, git("-C", str(origin), "commit", "-q", "-m", message, environment=traced).returncode)
            if not clone.exists():
                self.assertEqual(0, git("clone", "-q", str(origin), str(clone), environment=traced).returncode)
        self.assertEqual(0, git("-C", str(clone), "fetch", "-q", environment=traced).returncode)
        self.assertEqual([], maintenance_started(trace))
        left = [path.name for path in root.rglob("*") if path.name in ("maintenance.lock", "gc.pid", "gc.log")]
        self.assertEqual([], left)

    def test_the_trace_sees_the_maintenance_a_commit_starts_when_it_is_on(self) -> None:
        repository = self.git_home.home / "fixture"
        trace = self.git_home.home / "trace.json"
        self.assertEqual(0, git("init", "-q", str(repository)).returncode)
        on = ("-c", "maintenance.auto=true", "-c", "maintenance.autoDetach=false", "-c", "gc.autoDetach=false")
        committed = git(
            "-C",
            str(repository),
            *on,
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "m",
            environment={"GIT_TRACE2_EVENT": str(trace)},
        )
        self.assertEqual(0, committed.returncode, committed.stderr)
        started = maintenance_started(trace)
        self.assertTrue(started)
        self.assertEqual(["maintenance", "run"], started[0][1:3])
        self.assertIn("--auto", started[0])


class GitTestCaseTests(GitTestCase):
    def test_each_test_and_its_set_up_run_in_a_home_of_their_own_without_a_parent_set_up_call(self) -> None:
        seen: dict[str, str | None] = {}

        class Case(GitTestCase):
            # Deliberately does not call super().setUp().
            def setUp(self) -> None:
                seen["setUp"] = os.environ.get("HOME")

            def test_one(self) -> None:
                seen["test"] = os.environ.get("HOME")
                seen["home"] = str(self.git_home.home)

            def tearDown(self) -> None:
                seen["tearDown"] = os.environ.get("HOME")

        outer = os.environ.get("HOME")
        result = unittest.TestResult()
        Case("test_one").run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertEqual([seen["home"]] * 3, [seen["setUp"], seen["test"], seen["tearDown"]])
        self.assertNotEqual(outer, seen["home"])
        self.assertEqual(outer, os.environ.get("HOME"))
        self.assertFalse(Path(str(seen["home"])).exists())

    def test_two_tests_get_two_homes(self) -> None:
        homes: list[Path] = []

        class Case(GitTestCase):
            def test_one(self) -> None:
                homes.append(self.git_home.home)

            def test_two(self) -> None:
                homes.append(self.git_home.home)

        unittest.TestSuite([Case("test_one"), Case("test_two")]).run(unittest.TestResult())
        self.assertEqual(2, len(set(homes)))

    def test_the_class_fixture_runs_isolated_with_the_class_settings(self) -> None:
        seen: dict[str, str] = {}

        class Case(GitTestCase):
            git_config: ClassVar[Mapping[str, str]] = {"init.defaultBranch": "trunk"}

            @classmethod
            def setUpClass(cls) -> None:
                super().setUpClass()
                seen["class"] = os.environ["GIT_CONFIG_GLOBAL"]
                seen["class branch"] = git("config", "--get", "init.defaultBranch").stdout.strip()

            def test_one(self) -> None:
                seen["test"] = os.environ["GIT_CONFIG_GLOBAL"]
                seen["test branch"] = git("config", "--get", "init.defaultBranch").stdout.strip()

        outer = os.environ["GIT_CONFIG_GLOBAL"]
        result = unittest.TestResult()
        unittest.TestSuite([Case("test_one")]).run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertEqual(["trunk", "trunk"], [seen["class branch"], seen["test branch"]])
        self.assertEqual(3, len({outer, seen["class"], seen["test"]}))
        self.assertEqual(outer, os.environ["GIT_CONFIG_GLOBAL"])
        self.assertFalse(Path(seen["class"]).exists())

    def test_debug_runs_isolated_too(self) -> None:
        seen: list[str] = []

        class Case(GitTestCase):
            def test_one(self) -> None:
                seen.append(os.environ["GIT_CONFIG_GLOBAL"])

        Case("test_one").debug()
        self.assertNotEqual(os.environ["GIT_CONFIG_GLOBAL"], seen[0])
        self.assertFalse(Path(seen[0]).exists())

    def test_a_module_fixture_stays_isolated_until_the_module_cleanups_run(self) -> None:
        outer = os.environ["GIT_CONFIG_GLOBAL"]
        isolated = isolate_module({"init.defaultBranch": "main"})
        self.assertEqual(str(isolated.config), os.environ["GIT_CONFIG_GLOBAL"])
        self.assertEqual("main\n", git("config", "--get", "init.defaultBranch").stdout)
        unittest.case.doModuleCleanups()
        self.assertEqual(outer, os.environ["GIT_CONFIG_GLOBAL"])
        self.assertFalse(isolated.home.exists())


# A suite that commits in a fixture repository and reads a bare clone of it without naming it explicitly.
CHILD_SUITE = """
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, {scripts!r})

from git_test_base import GitTestCase

BASE = {base}


class Case(BASE):
    def test_commit_and_read_a_bare_clone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root / "work")], check=True)
            (root / "work" / "file.txt").write_text("text\\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root / "work"), "add", "file.txt"], check=True)
            subprocess.run(["git", "-C", str(root / "work"), "commit", "-q", "-m", "fixture"], check=True)
            subprocess.run(["git", "clone", "-q", "--bare", str(root / "work"), str(root / "bare.git")], check=True)
            logged = subprocess.run(
                ["git", "-C", str(root / "bare.git"), "log", "-1", "--format=%s"],
                capture_output=True, text=True, check=True,
            )
            self.assertEqual("fixture\\n", logged.stdout)


if __name__ == "__main__":
    unittest.main()
"""


class AcceptanceTests(GitTestCase):
    """A suite that inherits the base passes the same under the leaks the base closes, and one that does not fails."""

    def setUp(self) -> None:
        self.root = self.git_home.home / "acceptance"
        hooks = self.root / "failing-hooks"
        hooks.mkdir(parents=True)
        for name in ("pre-commit", "commit-msg", "post-commit"):
            (hooks / name).write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            (hooks / name).chmod(0o755)  # git runs a hook only when it is executable, off Windows
        self.hooked = self.root / "hooked.gitconfig"
        self.hooked.write_text(
            config_text({"user.name": "User", "user.email": "user@example.invalid", "core.hooksPath": str(hooks)}),
            encoding="utf-8",
        )
        self.command_scope = {name: INHERITED[name] for name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0")}
        self.command_scope["GIT_CONFIG_VALUE_0"] = "explicit"

    def outcome(self, base: str, environment: dict[str, str]) -> tuple[int, str]:
        suite = self.root / f"test_child_{base.replace('.', '_')}.py"
        suite.write_text(CHILD_SUITE.format(scripts=str(SCRIPTS), base=base), encoding="utf-8")
        ran = subprocess.run(
            [sys.executable, "-B", str(suite)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "GIT_CONFIG_GLOBAL": str(self.hooked), **environment},
            check=False,
        )
        return ran.returncode, ran.stderr.strip().splitlines()[-1]

    def test_an_inheriting_suite_passes_the_same_under_command_scope_settings_and_a_failing_global_hook(self) -> None:
        passed = (0, "OK")
        hooks_only = {"GIT_CONFIG_GLOBAL": str(self.hooked)}
        self.assertEqual(passed, self.outcome("GitTestCase", {"GIT_CONFIG_GLOBAL": os.environ["GIT_CONFIG_GLOBAL"]}))
        self.assertEqual(passed, self.outcome("GitTestCase", {**self.command_scope, **hooks_only}))

    def test_a_suite_that_does_not_inherit_it_fails_under_each_leak(self) -> None:
        self.assertEqual((0, "OK"), self.outcome("unittest.TestCase", {"GIT_CONFIG_GLOBAL": str(self.unhooked())}))
        self.assertEqual(
            (1, "FAILED (errors=1)"),
            self.outcome("unittest.TestCase", {**self.command_scope, "GIT_CONFIG_GLOBAL": str(self.unhooked())}),
        )
        self.assertEqual((1, "FAILED (errors=1)"), self.outcome("unittest.TestCase", {}))

    def unhooked(self) -> Path:
        config = self.root / "unhooked.gitconfig"
        config.write_text(config_text({"user.name": "User", "user.email": "user@example.invalid"}), encoding="utf-8")
        return config


if __name__ == "__main__":
    unittest.main()
