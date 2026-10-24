"""Regression tests for the root conftest.py, which isolates every suite's git from the shell that started it and deals
a suite's shards."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import GitHome, config_text, git_settings, isolated_git, quoted

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
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
# Variables the fixtures set, against the values they give each, the home aside.
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


def run_child(suite: Path, plugin: bool = True, environment: Mapping[str, str] | None = None) -> int:
    """Run a child suite through pytest from the repository root, with the root conftest.py as a plugin or without it,
    since a suite outside the repository is outside the root that loads it, and return pytest's exit code. A pytest.ini
    beside the suite makes its folder the run's root, so pytest reads no folder above it, where other tests' temporary
    folders come and go."""
    (suite.parent / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    plugin_options = ["-p", "conftest"] if plugin else []
    ran = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", *plugin_options, str(suite)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPOSITORY_ROOT,
        env={**os.environ, **(environment or {})},
        check=False,
    )
    return ran.returncode


class ConfigTextTests(unittest.TestCase):
    git_home: GitHome

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


class IsolatedGitTests(unittest.TestCase):
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
                f"[core]\n\thooksPath = {quoted(str(isolated.hooks))}\n"
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

    def test_a_class_settings_take_the_place_of_its_modules(self) -> None:
        module = mock.Mock(git_config={"init.defaultBranch": "main"})
        case = mock.Mock(git_config={"init.defaultBranch": "trunk"})
        self.assertEqual({"init.defaultBranch": "trunk"}, git_settings(case, module))
        self.assertEqual({"init.defaultBranch": "main"}, git_settings(None, module))
        self.assertEqual({"init.defaultBranch": "main"}, git_settings(mock.Mock(git_config={}), module))
        self.assertEqual({}, git_settings(None, object()))


class AutomaticMaintenanceTests(unittest.TestCase):
    """Nothing git starts in a fixture outlives the command that started it, so a suite sees the same tree each time."""

    git_home: GitHome
    class_fixture_reads: ClassVar[dict[str, str]]

    @classmethod
    def setUpClass(cls) -> None:
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


# A child suite's header: it records what each fixture and test sees as JSON lines in the file the run names.
CHILD_HEADER = """
import json
import os
import subprocess
import unittest
from pathlib import Path

LOG = Path({log!r})


def record(where, **seen):
    seen.update(where=where, home=os.environ.get("HOME"), config=os.environ.get("GIT_CONFIG_GLOBAL"))
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(seen) + "\\n")


def branch():
    read = subprocess.run(["git", "config", "--get", "init.defaultBranch"], capture_output=True, text=True)
    return read.stdout.strip()
"""
CHILD_SUITE = """
git_config = {{"init.defaultBranch": "main"}}


def setUpModule():
    record("module", branch=branch())


class Case(unittest.TestCase):
    # Deliberately does not call super().setUp().
    def setUp(self):
        record("setUp")

    def test_one(self):
        record("test", fixture=str(self.git_home.home), branch=branch())

    def test_two(self):
        record("test", fixture=str(self.git_home.home), branch=branch())

    def tearDown(self):
        record("tearDown")


class Trunk(unittest.TestCase):
    git_config = {{"init.defaultBranch": "trunk"}}

    @classmethod
    def setUpClass(cls):
        record("class", branch=branch())

    def test_trunk(self):
        record("trunk", branch=branch())


def test_function(git_home):
    record("function", fixture=str(git_home.home), branch=branch())
"""


class FixtureTests(unittest.TestCase):
    """The autouse fixtures isolate each test, its setUp and tearDown, a class fixture, and a module fixture, each in a
    home of its own with its settings, and remove every home afterwards."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="conftest-fixtures.")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.log = self.directory / "log.jsonl"
        self.suite = self.directory / "test_child.py"
        self.suite.write_text(CHILD_HEADER.format(log=str(self.log)) + CHILD_SUITE.format(), encoding="utf-8")

    def entries(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_each_test_class_and_module_runs_in_a_home_of_its_own_with_its_settings(self) -> None:
        self.assertEqual(0, run_child(self.suite))
        entries = self.entries()
        by_place = {place: [entry for entry in entries if entry["where"] == place] for place in {"module", "test"}}
        module, tests = by_place["module"][0], by_place["test"]
        self.assertEqual("main", module["branch"])
        # A test, its setUp, and its tearDown share one home, which its fixture names, and each test has its own.
        for test in tests:
            index = entries.index(test)
            self.assertEqual([test["home"]] * 3, [entries[index - 1]["home"], test["home"], entries[index + 1]["home"]])
            self.assertEqual(test["home"], test["fixture"])
            self.assertEqual("main", test["branch"])
        self.assertEqual(2, len({test["home"] for test in tests}))
        trunk_class = next(entry for entry in entries if entry["where"] == "class")
        trunk = next(entry for entry in entries if entry["where"] == "trunk")
        self.assertEqual(["trunk", "trunk"], [trunk_class["branch"], trunk["branch"]])
        function = next(entry for entry in entries if entry["where"] == "function")
        self.assertEqual((function["home"], "main"), (function["fixture"], function["branch"]))
        homes = [module["home"], trunk_class["home"], trunk["home"], function["home"], *(t["home"] for t in tests)]
        self.assertEqual(len(homes), len(set(homes)))
        self.assertNotIn(os.environ["HOME"], homes)
        self.assertEqual([], [home for home in homes if Path(home).exists()])

    def test_without_the_conftest_nothing_is_isolated(self) -> None:
        self.assertEqual(1, run_child(self.suite, plugin=False))
        self.assertTrue(all(entry["config"] == os.environ["GIT_CONFIG_GLOBAL"] for entry in self.entries()))


# A child suite that registers a cleanup and enters a context at each scope unittest offers, and records each as it
# runs; its module cleanup fails when the suite names a failure.
CLEANUP_SUITE = """
from contextlib import contextmanager

FAIL = {fail!r}


@contextmanager
def context(where):
    record(where + " context entered")
    yield
    record(where + " context exited")


def module_cleanup():
    record("module cleanup")
    if FAIL:
        raise RuntimeError("the module cleanup failed")


def setUpModule():
    record("setUpModule")
    unittest.addModuleCleanup(module_cleanup)
    unittest.enterModuleContext(context("module"))


def tearDownModule():
    record("tearDownModule")


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addClassCleanup(record, "class cleanup")
        cls.enterClassContext(context("class"))

    def setUp(self):
        self.addCleanup(record, "test cleanup")
        self.enterContext(context("test"))

    def test_one(self):
        record("test")
"""


class CleanupTests(unittest.TestCase):
    """pytest runs a test's and a class's cleanups but never unittest's module cleanups, so the root conftest.py runs
    those at the module's teardown, after tearDownModule and in the module's isolated home, as unittest would."""

    def run_suite(self, fail: bool) -> tuple[int, list[dict[str, Any]]]:
        temporary = tempfile.TemporaryDirectory(prefix="conftest-cleanups.")
        self.addCleanup(temporary.cleanup)
        log = Path(temporary.name) / "log.jsonl"
        suite = Path(temporary.name) / "test_child.py"
        suite.write_text(CHILD_HEADER.format(log=str(log)) + CLEANUP_SUITE.format(fail=fail), encoding="utf-8")
        exit_code = run_child(suite)
        return exit_code, [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def test_every_scope_runs_its_cleanups_and_contexts_in_unittest_order(self) -> None:
        exit_code, entries = self.run_suite(fail=False)
        self.assertEqual(0, exit_code)
        self.assertEqual(
            [
                "setUpModule",
                "module context entered",
                "class context entered",
                "test context entered",
                "test",
                "test context exited",
                "test cleanup",
                "class context exited",
                "class cleanup",
                "tearDownModule",
                "module context exited",
                "module cleanup",
            ],
            [entry["where"] for entry in entries],
        )
        module = [entry["home"] for entry in entries if entry["where"].startswith(("setUpModule", "module", "tear"))]
        self.assertEqual([module[0]] * 5, module)

    def test_a_failing_module_cleanup_fails_the_run(self) -> None:
        exit_code, entries = self.run_suite(fail=True)
        self.assertEqual(1, exit_code)
        self.assertEqual("module cleanup", entries[-1]["where"])


# A suite that commits in a fixture repository and reads a bare clone of it without naming it explicitly.
ACCEPTANCE_SUITE = """
import subprocess
import tempfile
from pathlib import Path


def test_commit_and_read_a_bare_clone():
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
        assert logged.stdout == "fixture\\n"
"""


class AcceptanceTests(unittest.TestCase):
    """A suite run with the root conftest.py passes the same under the leaks it closes, and one run without it fails."""

    git_home: GitHome

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
        self.suite = self.root / "test_child.py"
        self.suite.write_text(textwrap.dedent(ACCEPTANCE_SUITE), encoding="utf-8")

    def unhooked(self) -> Path:
        config = self.root / "unhooked.gitconfig"
        config.write_text(config_text({"user.name": "User", "user.email": "user@example.invalid"}), encoding="utf-8")
        return config

    def test_a_suite_run_with_the_conftest_passes_the_same_under_command_scope_settings_and_a_failing_hook(
        self,
    ) -> None:
        hooked = {"GIT_CONFIG_GLOBAL": str(self.hooked)}
        self.assertEqual(0, run_child(self.suite, environment={"GIT_CONFIG_GLOBAL": str(self.unhooked())}))
        self.assertEqual(0, run_child(self.suite, environment={**self.command_scope, **hooked}))

    def test_a_suite_run_without_it_fails_under_each_leak(self) -> None:
        unhooked = {"GIT_CONFIG_GLOBAL": str(self.unhooked())}
        self.assertEqual(0, run_child(self.suite, plugin=False, environment=unhooked))
        self.assertEqual(1, run_child(self.suite, plugin=False, environment={**self.command_scope, **unhooked}))
        self.assertEqual(1, run_child(self.suite, plugin=False, environment={"GIT_CONFIG_GLOBAL": str(self.hooked)}))
