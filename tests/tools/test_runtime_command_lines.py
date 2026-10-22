"""Regression suite for tools/runtime_command_lines.py, with every runtime replaced by a stub.

No test starts Claude Code, Codex, or Copilot CLI: each check is given a stub runner that answers a command line as a
runtime would, from its arguments alone, and a lookup that names a program nobody runs.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from deployer import platform_support
from tools import runtime_command_lines as lines
from tools.runtime_canary import Completed

SIGNED_OUT = {
    "claude": Completed(1, '{"type":"result","result":"Not logged in · Please run /login"}\n', ""),
    "codex": Completed(-1, '{"type":"thread.started","thread_id":"1"}\n', "", timed_out=True),
    "copilot": Completed(1, "", "Error: No authentication information found.\n"),
}
VERSIONS = {
    "claude": "2.1.302 (Claude Code)\n",
    "codex": "codex-cli 0.163.0\n",
    "copilot": "GitHub Copilot CLI 1.0.103.\n",
}
Answer = Callable[[list[str]], Completed | None]


class StubRuntimes:
    """Answers --version and --help, then each command line by `answer`, or as a signed-out runtime does."""

    def __init__(self, answer: Answer = lambda arguments: None, help_text: str | None = None) -> None:
        self.answer = answer
        self.help_text = help_text
        self.started: list[tuple[list[str], str]] = []

    def __call__(
        self, arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float, stdin: str
    ) -> Completed:
        runtime = Path(arguments[0]).name
        if arguments[1:] == ["--version"]:
            return Completed(0, VERSIONS[runtime], "")
        if arguments[-1] == "--help":
            return Completed(0, self.help_text if self.help_text is not None else " ".join(self.started_flags()), "")
        self.started.append((arguments, stdin))
        return self.answer(arguments) or SIGNED_OUT[runtime]

    def started_flags(self) -> list[str]:
        # Every flag any command line passes, so a help listing holds them all.
        return ["-p", "-c", "-C", *(flag for flags in KNOWN_FLAGS.values() for flag in flags)]


# The flags each runtime's command lines pass on this platform, read from the builders through the check itself.
KNOWN_FLAGS: dict[str, list[str]] = {}


def refusing(refusal: Completed) -> Answer:
    """An answer that refuses every command line with `refusal`."""
    return lambda arguments: refusal


class RuntimeCommandLinesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="runtime-command-lines-test.")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Nothing here may start a real runtime or read the real PATH's GitHub CLI.
        patcher = mock.patch.object(lines.platform_support, "find_on_path", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        for runtime in lines.RUNTIMES:
            if runtime not in KNOWN_FLAGS:
                built = lines.command_lines(
                    runtime, runtime, self.root / f"flags-{runtime}", {}, lines.COPILOT_SYNC_SESSION_VERSION
                )
                KNOWN_FLAGS[runtime] = [flag for line in built for flag in line.flags]

    def check(self, stub: StubRuntimes, runtimes: tuple[str, ...] = lines.RUNTIMES) -> list[str]:
        printed: list[str] = []
        found = lines.run(runtimes, 5, self.root / "run", runner=stub, which=lambda name: name, say=printed.append)
        self.assertEqual(printed, found)
        return found


class AcceptedTests(RuntimeCommandLinesTestCase):
    def test_every_command_line_a_signed_out_runtime_reaches_is_accepted(self) -> None:
        found = self.check(StubRuntimes())
        self.assertEqual(
            ["VERSION claude 2.1.302", "VERSION codex 0.163.0", "VERSION copilot 1.0.103"],
            [line for line in found if line.startswith("VERSION ")],
        )
        outcomes = [line for line in found if not line.startswith("VERSION ")]
        self.assertTrue(outcomes)
        self.assertTrue(all(line.startswith("ACCEPTED ") and "UNLISTED" not in line for line in outcomes), outcomes)
        self.assertIn("ACCEPTED copilot review-host", outcomes)
        self.assertIn("ACCEPTED codex runtime-canary", outcomes)
        self.assertIn("ACCEPTED claude skill-evals-trigger", outcomes)
        self.assertFalse(any(line.startswith(lines.FAILING) for line in found))

    def test_a_command_that_finishes_with_status_zero_is_accepted(self) -> None:
        found = self.check(StubRuntimes(lambda arguments: Completed(0, "[]", "")), ("codex",))
        self.assertIn("ACCEPTED codex skill-listing", found)

    def test_a_flag_its_help_does_not_list_is_named_but_accepted_when_the_start_is(self) -> None:
        found = self.check(StubRuntimes(help_text="--output-format --verbose"), ("claude",))
        trigger = next(line for line in found if line.startswith("ACCEPTED claude skill-evals-trigger "))
        self.assertIn('UNLISTED "-p --model', trigger)
        self.assertIn("--max-turns", trigger)
        self.assertFalse(any(line.startswith(lines.FAILING) for line in found))

    def test_the_review_host_gets_its_prompt_on_stdin_and_the_others_nothing(self) -> None:
        stub = StubRuntimes()
        self.check(stub, ("copilot",))
        stdins = {tuple(arguments): stdin for arguments, stdin in stub.started}
        review = next(arguments for arguments in stdins if "--disallow-temp-dir" in arguments)
        self.assertEqual(lines.REQUEST, stdins[review])
        self.assertEqual({""}, {stdin for arguments, stdin in stdins.items() if arguments != review})


class RefusedTests(RuntimeCommandLinesTestCase):
    def test_a_removed_flag_is_named_and_fails(self) -> None:
        removed = Completed(1, "", "error: --no-remote was removed. Use /remote off in a session.\n")
        stub = StubRuntimes(lambda arguments: removed if "--sync-session=off" in arguments else None)
        found = self.check(stub, ("copilot",))
        self.assertIn(
            'REMOVED copilot review-host --no-remote "error: --no-remote was removed. Use /remote off in a session."',
            found,
        )
        self.assertIn("ACCEPTED copilot skill-listing", found)
        self.assertTrue(lines.failed(found))

    def test_an_unknown_option_is_named_whichever_parser_refuses_it(self) -> None:
        for runtime, message, flag in (
            ("claude", "error: unknown option '--bare-flag'\n", "--bare-flag"),
            ("codex", "error: unexpected argument '--ephemeral' found\n\nUsage: codex exec [OPTIONS]\n", "--ephemeral"),
        ):
            with self.subTest(runtime=runtime):
                found = self.check(StubRuntimes(refusing(Completed(2, "", message))), (runtime,))
                self.assertTrue(any(line.startswith(f"REMOVED {runtime} runtime-canary {flag} ") for line in found))
                self.assertTrue(lines.failed(found))

    def test_a_command_line_that_does_not_parse_for_another_reason_is_refused(self) -> None:
        refusal = Completed(2, "", "error: invalid value 'workspace-write' for '--sandbox <SANDBOX_MODE>'\n")
        stub = StubRuntimes(lambda arguments: refusal if "exec" in arguments else None)
        found = self.check(stub, ("codex",))
        self.assertIn(
            "REFUSED codex runtime-canary \"error: invalid value 'workspace-write' for '--sandbox <SANDBOX_MODE>'\"",
            found,
        )
        self.assertTrue(lines.failed(found))


class FailedTests(RuntimeCommandLinesTestCase):
    def test_a_runtime_that_cannot_start_fails_with_why(self) -> None:
        stub = StubRuntimes(lambda arguments: Completed(-1, "", "", error="cannot run copilot: Access is denied"))
        found = self.check(stub, ("copilot",))
        self.assertIn('FAILED copilot skill-listing "cannot run copilot: Access is denied"', found)

    def test_an_end_short_of_the_session_fails_with_the_runtimes_own_message(self) -> None:
        stub = StubRuntimes(lambda arguments: Completed(1, "", "Error: --agents file not found: x\n"))
        found = self.check(stub, ("claude",))
        self.assertIn('FAILED claude skill-evals-review "Error: --agents file not found: x"', found)

    def test_a_start_that_shows_no_session_by_the_time_limit_fails(self) -> None:
        stub = StubRuntimes(lambda arguments: Completed(-1, "", "", timed_out=True))
        found = self.check(stub, ("copilot",))
        self.assertIn(
            'FAILED copilot review-host "it showed no session start within 5 seconds: '
            'exited with status -1 and printed nothing"',
            found,
        )

    def test_a_runtime_that_is_not_installed_is_missing_and_fails(self) -> None:
        printed: list[str] = []
        found = lines.run(("codex",), 5, self.root, runner=StubRuntimes(), which=lambda name: None, say=printed.append)
        self.assertEqual(['MISSING codex "it is not installed where deploy.py check looks"'], found)
        self.assertTrue(lines.failed(found))


class BuilderTests(RuntimeCommandLinesTestCase):
    def test_each_command_line_is_the_one_its_builder_returns(self) -> None:
        sentinel = ["claude", "--from-the-builder"]
        with mock.patch.object(lines.runtime_canary, "run_command", return_value=sentinel) as built:
            found = lines.command_lines("claude", "claude", self.root / "c", {}, None)
        self.assertEqual(sentinel, next(line for line in found if line.name == "runtime-canary").arguments)
        built.assert_called_once()

    def test_copilot_lines_follow_the_version_the_runtime_prints(self) -> None:
        before = lines.command_lines("copilot", "copilot", self.root / "before", {}, (1, 0, 102))
        after = lines.command_lines("copilot", "copilot", self.root / "after", {}, (1, 0, 103))
        for line in before[:2]:
            self.assertIn("--no-remote", line.flags)
        for line in after[:2]:
            self.assertIn("--sync-session", line.flags)
            self.assertNotIn("--no-remote", line.flags)

    def test_the_deployer_commands_come_from_its_exported_builders(self) -> None:
        self.assertEqual(["codex", "app-server"], lines.discovery.listing_command("codex", "codex"))
        self.assertEqual(["copilot", "skill", "list", "--json"], lines.discovery.listing_command("copilot", "copilot"))
        with self.assertRaisesRegex(ValueError, "no skill listing for claude"):
            lines.discovery.listing_command("claude", "claude")
        self.assertEqual(["codex", "debug", "prompt-input"], lines.discovery.catalog_command("codex"))
        self.assertEqual(
            ["codex", "debug", "prompt-input", "-c", "sandbox_mode=workspace-write"],
            lines.deployer_tools.codex_sandbox_command("codex"),
        )
        found = {line.name: line.arguments for line in lines.command_lines("codex", "codex", self.root / "c", {}, None)}
        self.assertEqual(["codex", "app-server"], found["skill-listing"])
        self.assertEqual(["codex", "debug", "prompt-input"], found["skill-catalog"])

    def test_flags_and_subcommands_are_read_from_the_arguments(self) -> None:
        line = lines.CommandLine(
            "codex", "x", ["codex", "exec", "--json", "-C", "dir", "--allow-tool=shell(a)", "prompt"], self.root, {}
        )
        self.assertEqual(["exec"], line.subcommand)
        self.assertEqual(["--json", "-C", "--allow-tool"], line.flags)
        self.assertTrue(lines.listed("--json", "  --json  Print events"))
        self.assertFalse(lines.listed("--json", "  --json-schema <file>"))


class SignedOutTests(unittest.TestCase):
    def test_the_environment_keeps_no_credential_and_points_every_home_inside_the_throwaway_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            base = {"PATH": "bin", "GH_TOKEN": "t", "ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_USE_BEDROCK": "1"}
            with (
                mock.patch.dict(os.environ, base, clear=True),
                mock.patch.object(lines.platform_support, "find_on_path", return_value=None),
            ):
                environment = lines.signed_out(home, "runtime")
            self.assertEqual("bin", environment["PATH"])
            for name in ("GH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK"):
                self.assertNotIn(name, environment)
            for name in ("HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "COPILOT_HOME", "GH_CONFIG_DIR"):
                self.assertTrue(Path(environment[name]).is_relative_to(home), name)
            self.assertEqual("http://127.0.0.1:9", environment["HTTPS_PROXY"])
            self.assertEqual("false", environment["COPILOT_AUTO_UPDATE"])

    def test_platform_starting_variables_survive_and_others_do_not(self) -> None:
        base = {**dict.fromkeys(platform_support.STARTING_VARIABLES, "kept"), "OPENAI_API_KEY": "secret"}
        environment = platform_support.signed_out_environment(base, Path("home"))
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertEqual(str(Path("home")), environment["HOME"])
        for name in platform_support.STARTING_VARIABLES:
            self.assertEqual("kept", environment[name])

    def test_github_cli_is_left_off_the_path_and_refused_beside_the_runtime(self) -> None:
        separator = os.pathsep
        holds_gh = {"gh-dir", "shared"}

        def find(name: str, search_path: str) -> str | None:
            if name == "node":
                return None
            return f"{search_path}/gh" if name == "gh" and search_path in holds_gh else None

        with mock.patch.object(lines.platform_support, "find_on_path", side_effect=find):
            self.assertEqual(
                separator.join(["tools", "runtime-dir"]),
                lines.without_gh(separator.join(["tools", "gh-dir", "runtime-dir"]), "runtime-dir/copilot"),
            )
            with self.assertRaisesRegex(lines.Unsafe, "GitHub CLI is in shared"):
                lines.without_gh(separator.join(["tools", "shared"]), "shared/copilot")

    def test_a_runtime_that_cannot_be_signed_out_starts_nothing(self) -> None:
        stub = StubRuntimes()
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(lines, "signed_out", side_effect=lines.Unsafe("GitHub CLI is in shared")),
        ):
            found = lines.run(
                ("copilot",), 5, Path(temporary), runner=stub, which=lambda name: name, say=lambda _: None
            )
        self.assertEqual(['UNSAFE copilot "GitHub CLI is in shared"'], found)
        self.assertEqual([], stub.started)
        self.assertTrue(lines.failed(found))


class SummaryTests(unittest.TestCase):
    def test_the_summary_tables_each_fact_and_says_whether_any_failed(self) -> None:
        passed = lines.summary(["VERSION codex 0.163.0", "ACCEPTED codex skill-listing"])
        self.assertIn("| codex | version | 0.163.0 |", passed)
        self.assertIn("| codex | skill-listing | ACCEPTED |", passed)
        self.assertIn("Every runtime accepted every command line.", passed)
        failed = lines.summary(['REMOVED copilot review-host --no-remote "a | b"'])
        self.assertIn('| copilot | review-host | REMOVED --no-remote "a &#124; b" |', failed)
        self.assertIn("1 line(s) failed.", failed)

    def test_main_appends_the_summary_and_exits_by_whether_a_line_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            summary = Path(temporary) / "summary.md"
            summary.write_text("before\n", encoding="utf-8")
            with mock.patch.object(lines, "run", return_value=["ACCEPTED codex skill-listing"]):
                self.assertEqual(0, lines.main(["--summary", str(summary)]))
            with mock.patch.object(lines, "run", return_value=['FAILED codex skill-listing "x"']):
                self.assertEqual(1, lines.main(["--runtime", "codex"]))
            self.assertTrue(summary.read_text(encoding="utf-8").startswith("before\n## Runtime command lines"))
