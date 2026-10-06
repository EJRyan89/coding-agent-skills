"""review_pipeline.main, pinned: for each subcommand, the exact call it makes, the lines it prints, and its exit code;
which errors become a FAILED line and which escape as a traceback; and the usage errors that exit 2 before any work.
Every function main calls is replaced by a recording stub, so these tests exercise main alone."""

from __future__ import annotations

import contextlib
import io
import sys
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import review_pipeline as rp
from review_archive import ArchiveError
from review_config import ConfigurationError
from review_flags import FlagError
from review_github import GitHubError
from review_hosts import HostSuperseded
from review_io import PersistenceError
from review_operation import ReviewOperationError
from review_records import RecordError
from review_runtime import RuntimeContractError
from review_specialists import SpecialistError
from review_state import StateError

Call = tuple[tuple[Any, ...], dict[str, Any]]

SERVICES_TYPE = rp.Services  # setUp replaces rp.Services, so the class is kept here
SERVICES = SERVICES_TYPE()
CONFIG = "review config.json"
EXPECTED = (
    rp.PipelineError,
    ConfigurationError,
    GitHubError,
    RuntimeContractError,
    SpecialistError,
    ReviewOperationError,
    RecordError,
    ArchiveError,
    PersistenceError,
    StateError,
    FlagError,
    OSError,
    UnicodeError,
)
# Everything main calls that does work; setUp makes each one fail the test unless a test installs its own stub.
STUBBED = (
    "inspect_reviewer",
    "validate_reviewer",
    "working_path",
    "enumerate_batch",
    "prepare",
    "mark_dispatched",
    "validate_result",
    "workflow_script",
    "wait_for_reviewers",
    "unfinalized_selector",
    "dispatch_copilot",
    "run_host",
    "wait_for_host",
    "check_run",
    "finalize",
    "advance_watermarks",
    "Services",
)


class Stub:
    """Records every call under a lock, because prepare's calls run on worker threads."""

    def __init__(self, result: Any = None, effect: Callable[..., Any] | None = None) -> None:
        self.result = result
        self.effect = effect
        self.calls: list[Call] = []
        self._lock = threading.Lock()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            self.calls.append((args, kwargs))
        if self.effect is not None:
            return self.effect(*args, **kwargs)
        return self.result


def raising(error: BaseException) -> Callable[..., Any]:
    def effect(*args: Any, **kwargs: Any) -> Any:
        raise error

    return effect


def by_first_argument(outcomes: dict[Any, Any]) -> Callable[..., Any]:
    """A stub effect that answers by its first argument, raising the outcome when it is an exception."""

    def effect(first: Any, *args: Any, **kwargs: Any) -> Any:
        outcome = outcomes[first]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return effect


class MainCase(unittest.TestCase):
    def setUp(self) -> None:
        for name in STUBBED:
            self.stub(name, effect=raising(AssertionError(f"main called {name} unexpectedly")))

    def stub(self, name: str, result: Any = None, *, effect: Callable[..., Any] | None = None) -> Stub:
        stub = Stub(result, effect)
        patcher = mock.patch.object(rp, name, stub)
        patcher.start()
        self.addCleanup(patcher.stop)
        return stub

    def run_main(self, *arguments: str, services: rp.Services | None = SERVICES) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rp.main(list(arguments), services=services)
        return code, out.getvalue(), err.getvalue()

    def usage_error(self, *arguments: str) -> str:
        """The stderr of a call that must stop with argparse's exit 2, printing nothing on stdout."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop:
            rp.main(list(arguments), services=SERVICES)
        self.assertEqual(2, stop.exception.code)
        self.assertEqual("", out.getvalue())
        return err.getvalue()

    def assert_usage(self, message: str, *arguments: str) -> None:
        self.assertTrue(self.usage_error(*arguments).endswith(f"error: {message}\n"))


class ReviewerCommandTests(MainCase):
    def test_inspect_reviewer_prints_its_lines(self) -> None:
        stub = self.stub("inspect_reviewer", ["MODE single", "SKILL review"])
        result = self.run_main("--config", CONFIG, "inspect-reviewer", "--repository", "example/app", "--ref", "abc123")
        self.assertEqual((0, "MODE single\nSKILL review\n", ""), result)
        self.assertEqual(
            [(("example/app",), {"ref": "abc123", "config_path": Path(CONFIG), "services": SERVICES})], stub.calls
        )

    def test_inspect_reviewer_defaults_and_no_lines(self) -> None:
        stub = self.stub("inspect_reviewer", [])
        self.assertEqual((0, "\n", ""), self.run_main("inspect-reviewer", "--repository", "example/app"))
        self.assertEqual([(("example/app",), {"ref": None, "config_path": None, "services": SERVICES})], stub.calls)

    def test_validate_reviewer_passes_pull_numbers(self) -> None:
        stub = self.stub("validate_reviewer", ["VALID example/app"])
        result = self.run_main(
            "--config", CONFIG, "validate-reviewer", "--repository", "example/app", "--pull", "3", "--pull", "5"
        )
        self.assertEqual((0, "VALID example/app\n", ""), result)
        self.assertEqual(
            [
                (
                    ("example/app",),
                    {"pulls": [3, 5], "ref": None, "config_path": Path(CONFIG), "services": SERVICES},
                )
            ],
            stub.calls,
        )

    def test_validate_reviewer_with_ref(self) -> None:
        stub = self.stub("validate_reviewer", ["VALID example/app"])
        self.assertEqual(
            (0, "VALID example/app\n", ""),
            self.run_main("validate-reviewer", "--repository", "example/app", "--ref", "abc123"),
        )
        self.assertEqual(
            [(("example/app",), {"pulls": [], "ref": "abc123", "config_path": None, "services": SERVICES})],
            stub.calls,
        )


class EnumerateTests(MainCase):
    BATCH = {
        "repositories": {
            "example/app": {"complete": True, "eligible": [{"number": 3}, {"number": 5}]},
            "example/lib": {"complete": False, "error": "gh failed", "eligible": [{"number": 7}]},
            "example/web": {"complete": True, "eligible": []},
        }
    }

    def test_prints_each_repository_then_the_batch(self) -> None:
        location = self.stub("working_path", Path("batch.json"))
        batch = self.stub("enumerate_batch", self.BATCH)
        result = self.run_main(
            "--config",
            CONFIG,
            "enumerate",
            "--repository",
            "example/app",
            "--repository",
            "example/lib",
            "--force",
            "--output",
            "my batch.json",
        )
        self.assertEqual(
            (
                0,
                "PULL example/app#3\n"
                "PULL example/app#5\n"
                "REPOSITORY_FAILED example/lib gh failed\n"
                "PULL example/lib#7\n"
                "BATCH batch.json\n",
                "",
            ),
            result,
        )
        self.assertEqual([((Path("my batch.json"), "review-prs-batch-", "batch.json"), {})], location.calls)
        self.assertEqual(
            [
                (
                    (Path("batch.json"),),
                    {
                        "repositories": ["example/app", "example/lib"],
                        "repository_set": None,
                        "force": True,
                        "config_path": Path(CONFIG),
                        "services": SERVICES,
                    },
                )
            ],
            batch.calls,
        )

    def test_defaults(self) -> None:
        location = self.stub("working_path", Path("batch.json"))
        batch = self.stub("enumerate_batch", {"repositories": {}})
        self.assertEqual((0, "BATCH batch.json\n", ""), self.run_main("enumerate", "--repository-set", "work"))
        self.assertEqual([((None, "review-prs-batch-", "batch.json"), {})], location.calls)
        self.assertEqual(
            [
                (
                    (Path("batch.json"),),
                    {
                        "repositories": None,
                        "repository_set": "work",
                        "force": False,
                        "config_path": None,
                        "services": SERVICES,
                    },
                )
            ],
            batch.calls,
        )

    def test_repository_and_repository_set_exclude_each_other(self) -> None:
        self.assert_usage(
            "argument --repository-set: not allowed with argument --repository",
            "enumerate",
            "--repository",
            "example/app",
            "--repository-set",
            "work",
        )


def ready(selector: str, run: str, runtime: str = "claude-code") -> dict[str, Any]:
    return {
        "status": "ready",
        "selector": selector,
        "run": Path(run),
        "notes": ["first note", "second note"],
        "runtime": runtime,
        "roles": [
            {"id": "generic-review", "prompt_file": "generic.md", "model": "opus"},
            {"id": "security", "prompt_file": "security.md"},
            {"id": "style", "prompt_file": "style.md", "model": ""},
        ],
    }


def prepare_call(
    selector: str,
    *,
    re_review: bool = False,
    scope: str | None = None,
    force: bool = False,
    canary: bool = False,
    host: str | None = None,
    config_path: Path | None = None,
) -> Call:
    return (
        (selector,),
        {
            "re_review": re_review,
            "scope": scope,
            "force": force,
            "canary": canary,
            "host": host,
            "config_path": config_path,
            "services": SERVICES,
        },
    )


class PrepareTests(MainCase):
    def test_ready_skip_and_copilot_runs(self) -> None:
        prepare = self.stub(
            "prepare",
            effect=by_first_argument(
                {
                    "example/app#3": ready("example/app#3", "run three"),
                    "example/app#5": {"status": "skip", "selector": "example/app#5", "reason": "already reviewed"},
                    "example/lib#7": ready("example/lib#7", "run seven", "copilot-cli"),
                }
            ),
        )
        dispatched = self.stub("mark_dispatched")
        result = self.run_main(
            "--config",
            CONFIG,
            "prepare",
            "--pull",
            "example/app#3",
            "--re-review",
            "example/app#5",
            "--re-review",
            "example/lib#7",
            "--scope",
            "incremental",
            "--force",
            "--host",
            "copilot-cli",
        )
        self.assertEqual(
            (
                0,
                "RUN example/app#3 run three\n"
                "NOTE example/app#3 first note\n"
                "NOTE example/app#3 second note\n"
                "ROLE generic-review generic.md\n"
                "MODEL example/app#3 generic-review opus\n"
                "ROLE security security.md\n"
                "ROLE style style.md\n"
                "SKIP example/app#5 already reviewed\n"
                "RUN example/lib#7 run seven\n"
                "NOTE example/lib#7 first note\n"
                "NOTE example/lib#7 second note\n"
                "HOST copilot-cli run seven\n",
                "",
            ),
            result,
        )
        common: dict[str, Any] = {"force": True, "host": "copilot-cli", "config_path": Path(CONFIG)}
        self.assertEqual(
            [
                prepare_call("example/app#3", **common),
                prepare_call("example/app#5", re_review=True, scope="incremental", **common),
                prepare_call("example/lib#7", re_review=True, scope="incremental", **common),
            ],
            sorted(prepare.calls, key=lambda call: call[0][0]),
        )
        self.assertEqual([((Path("run three"),), {}), ((Path("run seven"),), {})], dispatched.calls)

    def test_canary_is_passed_through(self) -> None:
        prepare = self.stub("prepare", effect=by_first_argument({"example/app#3": ready("example/app#3", "run three")}))
        self.stub("mark_dispatched")
        code, _, _ = self.run_main("prepare", "--pull", "example/app#3", "--canary")
        self.assertEqual(0, code)
        self.assertEqual([prepare_call("example/app#3", canary=True)], prepare.calls)

    def test_failures_are_reported_per_pull_request_in_order(self) -> None:
        self.stub(
            "prepare",
            effect=by_first_argument(
                {
                    "example/app#3": ConfigurationError("bad config"),
                    "example/app#5": None,
                    "example/app#7": {"status": "skip", "selector": "example/app#7", "reason": "draft"},
                    "example/app#9": OSError("disk gone"),
                }
            ),
        )
        result = self.run_main(
            "prepare",
            "--pull",
            "example/app#3",
            "--pull",
            "example/app#5",
            "--pull",
            "example/app#7",
            "--pull",
            "example/app#9",
        )
        self.assertEqual(
            (
                1,
                "FAILED example/app#3 bad config\n"
                "FAILED example/app#5 None\n"
                "SKIP example/app#7 draft\n"
                "FAILED example/app#9 disk gone\n",
                "",
            ),
            result,
        )

    def test_every_expected_error_fails_only_its_pull_request(self) -> None:
        for error_class in EXPECTED:
            with self.subTest(error=error_class.__name__):
                self.stub(
                    "prepare",
                    effect=by_first_argument(
                        {
                            "example/app#3": error_class("boom"),
                            "example/app#5": {"status": "skip", "selector": "example/app#5", "reason": "draft"},
                        }
                    ),
                )
                self.assertEqual(
                    (1, "FAILED example/app#3 boom\nSKIP example/app#5 draft\n", ""),
                    self.run_main("prepare", "--pull", "example/app#3", "--pull", "example/app#5"),
                )

    def test_an_unexpected_error_escapes(self) -> None:
        error = RuntimeError("bug")
        self.stub(
            "prepare",
            effect=by_first_argument(
                {
                    "example/app#3": error,
                    "example/app#5": {"status": "skip", "selector": "example/app#5", "reason": "x"},
                }
            ),
        )
        with self.assertRaises(RuntimeError) as caught:
            self.run_main("prepare", "--pull", "example/app#3", "--pull", "example/app#5")
        self.assertIs(error, caught.exception)

    def test_an_expected_error_while_marking_ends_the_call(self) -> None:
        self.stub(
            "prepare",
            effect=by_first_argument(
                {
                    "example/app#3": {"status": "skip", "selector": "example/app#3", "reason": "draft"},
                    "example/app#5": ready("example/app#5", "run five"),
                    "example/app#7": ready("example/app#7", "run seven"),
                }
            ),
        )
        dispatched = self.stub("mark_dispatched", effect=raising(PersistenceError("disk full")))
        self.assertEqual(
            (1, "SKIP example/app#3 draft\nFAILED disk full\n", ""),
            self.run_main("prepare", "--pull", "example/app#3", "--pull", "example/app#5", "--pull", "example/app#7"),
        )
        self.assertEqual([((Path("run five"),), {})], dispatched.calls)

    def test_a_malformed_selector_is_not_a_duplicate(self) -> None:
        prepare = self.stub(
            "prepare", effect=raising(ReviewOperationError("Pull selector must be owner/repository#number"))
        )
        self.assertEqual(
            (
                1,
                "FAILED not-a-pull Pull selector must be owner/repository#number\n"
                "FAILED not-a-pull Pull selector must be owner/repository#number\n",
                "",
            ),
            self.run_main("prepare", "--pull", "not-a-pull", "--pull", "not-a-pull"),
        )
        self.assertEqual(2, len(prepare.calls))

    def test_usage_errors(self) -> None:
        cases = [
            ("prepare takes at least one --pull or --re-review", ["prepare"]),
            ("prepare takes at least one --pull or --re-review", ["prepare", "--canary", "--force", "--scope", "full"]),
            (
                "prepare takes at most 4 pull requests",
                ["prepare", *[part for number in (1, 2, 3, 4, 5) for part in ("--pull", f"example/app#{number}")]],
            ),
            (
                "prepare takes at most 4 pull requests",
                [
                    "prepare",
                    "--canary",
                    "--pull",
                    "example/app#1",
                    "--pull",
                    "example/app#1",
                    "--pull",
                    "example/app#2",
                    "--re-review",
                    "example/app#3",
                    "--re-review",
                    "example/app#4",
                ],
            ),
            (
                "--canary takes only --pull selectors and no --force",
                ["prepare", "--canary", "--pull", "example/app#1", "--force"],
            ),
            (
                "--canary takes only --pull selectors and no --force",
                ["prepare", "--canary", "--re-review", "example/app#1"],
            ),
            (
                "--scope is required with --re-review and taken only with it",
                ["prepare", "--re-review", "example/app#1"],
            ),
            (
                "--scope is required with --re-review and taken only with it",
                ["prepare", "--pull", "example/app#1", "--pull", "example/app#1", "--scope", "full"],
            ),
            (
                "example/app#1 is named more than once",
                ["prepare", "--pull", "example/app#1", "--re-review", "example/app#1", "--scope", "auto"],
            ),
            (
                "example/app#1 is named more than once",
                ["prepare", "--pull", "Example/App#1", "--pull", "example/app#1"],
            ),
            (
                "example/app#2 is named more than once",
                ["prepare", "--pull", "example/app#1", "--pull", "example/app#2", "--pull", "example/app#2"],
            ),
        ]
        for message, arguments in cases:
            with self.subTest(arguments=arguments):
                self.assert_usage(message, *arguments)

    def test_choices_are_enforced_by_argparse(self) -> None:
        self.assertIn(
            "argument --scope: invalid choice: 'everything'",
            self.usage_error("prepare", "--re-review", "example/app#1", "--scope", "everything"),
        )
        self.assertIn(
            "argument --host: invalid choice: 'vim'",
            self.usage_error("prepare", "--pull", "example/app#1", "--host", "vim"),
        )


class ResultCommandTests(MainCase):
    def test_validate_result(self) -> None:
        for answer, expected in (
            ("bad json", (1, "INVALID bad json\n", "")),
            (None, (0, "VALID\n", "")),
            ("", (0, "VALID\n", "")),
        ):
            with self.subTest(answer=answer):
                stub = self.stub("validate_result", answer)
                self.assertEqual(
                    expected, self.run_main("validate-result", "--run", "run one", "--role", "generic-review")
                )
                self.assertEqual([((Path("run one"), "generic-review"), {})], stub.calls)

    def test_workflow_prints_the_script_between_markers(self) -> None:
        stub = self.stub("workflow_script", (Path("flow.js"), "line one\nline two\n", 3))
        self.assertEqual(
            (0, "WORKFLOW flow.js roles=3\nBEGIN_WORKFLOW_SCRIPT\nline one\nline two\nEND_WORKFLOW_SCRIPT\n", ""),
            self.run_main("workflow", "--run", "run one", "--run", "run two"),
        )
        self.assertEqual([(([Path("run one"), Path("run two")],), {})], stub.calls)

    def test_workflow_text_is_printed_without_an_added_newline(self) -> None:
        self.stub("workflow_script", (Path("flow.js"), "text", 1))
        self.assertEqual(
            (0, "WORKFLOW flow.js roles=1\nBEGIN_WORKFLOW_SCRIPT\ntextEND_WORKFLOW_SCRIPT\n", ""),
            self.run_main("workflow", "--run", "run one"),
        )


class WaitTests(MainCase):
    PROGRESS = [
        ("example/app#3", {"generic-review": ("ready", 10), "security": ("running", 42)}),
        ("example/app#5", {"generic-review": ("ready", 5)}),
        ("example/app#7", {}),
        ("example/app#9", {"generic-review": ("overdue", 3700), "security": ("ready", 8)}),
    ]

    def test_wait_reviewers_with_failures(self) -> None:
        stub = self.stub("wait_for_reviewers", (self.PROGRESS, {Path("run x"): "not prepared"}))
        self.assertEqual(
            (
                1,
                "FAILED run x not prepared\n"
                "RUNNING example/app#3 security 42s\n"
                "READY example/app#5\n"
                "READY example/app#7\n"
                "OVERDUE example/app#9 generic-review 3700s\n",
                "",
            ),
            self.run_main("wait-reviewers", "--run", "run one", "--run", "run two", "--timeout", "120"),
        )
        self.assertEqual([(([Path("run one"), Path("run two")], 120, SERVICES), {})], stub.calls)

    def test_wait_reviewers_running_is_not_a_failure(self) -> None:
        self.stub("wait_for_reviewers", (self.PROGRESS[:1], {}))
        self.assertEqual(
            (0, "RUNNING example/app#3 security 42s\n", ""),
            self.run_main("wait-reviewers", "--run", "run one", "--timeout", "1"),
        )

    def test_dispatch(self) -> None:
        stub = self.stub("dispatch_copilot", Path("host.log"))
        self.assertEqual((0, "STARTED host.log\n", ""), self.run_main("dispatch", "--run", "run one"))
        self.assertEqual([((Path("run one"), SERVICES), {})], stub.calls)

    def test_host(self) -> None:
        stub = self.stub("run_host", "completed")
        self.assertEqual(
            (0, "OUTCOME completed\n", ""), self.run_main("host", "--run", "run one", "--token", "a token")
        )
        self.assertEqual([((Path("run one"), "a token", SERVICES), {})], stub.calls)

    def test_wait(self) -> None:
        for answer, expected in (
            ((None, 12), (0, "RUNNING 12s\n", "")),
            ((Path("result.json"), 30), (0, "DISPATCHED result.json\n", "")),
        ):
            with self.subTest(answer=answer):
                stub = self.stub("wait_for_host", answer)
                self.assertEqual(expected, self.run_main("wait", "--run", "run one", "--timeout", "300"))
                self.assertEqual([((Path("run one"), 300, SERVICES), {})], stub.calls)

    def test_timeout_bounds(self) -> None:
        for command in (["wait", "--run", "run one"], ["wait-reviewers", "--run", "run one"]):
            for value in ("0", "301"):
                with self.subTest(command=command[0], value=value):
                    self.assert_usage("argument --timeout: must be from 1 to 300 seconds", *command, "--timeout", value)
            with self.subTest(command=command[0], value="x"):
                self.assert_usage("argument --timeout: invalid wait_seconds value: 'x'", *command, "--timeout", "x")


class RunListTests(MainCase):
    def test_unfinalized(self) -> None:
        stub = self.stub(
            "unfinalized_selector",
            effect=by_first_argument(
                {Path("run a"): "example/app#3", Path("run b"): None, Path("run c"): RecordError("broken run")}
            ),
        )
        self.assertEqual(
            (1, f"UNFINALIZED example/app#3 {Path('run a').resolve()}\nFAILED run c broken run\n", ""),
            self.run_main("unfinalized", "--run", "run a", "--run", "run b", "--run", "run c"),
        )
        self.assertEqual([((Path("run a"),), {}), ((Path("run b"),), {}), ((Path("run c"),), {})], stub.calls)

    def test_unfinalized_outcomes(self) -> None:
        cases: list[tuple[dict[Path, Any], tuple[int, str, str]]] = [
            ({Path("run a"): None, Path("run b"): None}, (0, "ALL_FINALIZED\n", "")),
            (
                {Path("run a"): "example/app#3", Path("run b"): None},
                (1, f"UNFINALIZED example/app#3 {Path('run a').resolve()}\n", ""),
            ),
            ({Path("run a"): None, Path("run b"): StateError("gone")}, (1, "FAILED run b gone\n", "")),
        ]
        for outcomes, expected in cases:
            with self.subTest(outcomes=outcomes):
                self.stub("unfinalized_selector", effect=by_first_argument(outcomes))
                self.assertEqual(expected, self.run_main("unfinalized", "--run", "run a", "--run", "run b"))

    def test_check(self) -> None:
        stub = self.stub(
            "check_run",
            effect=by_first_argument(
                {
                    Path("run a"): {
                        "selector": "example/app#3",
                        "retry": [
                            {"id": "generic-review", "prompt_file": "generic.md", "model": "opus"},
                            {"id": "security", "prompt_file": "security.md"},
                        ],
                        "errors": {"generic-review": "bad json", "security": "missing", "style": "gave up"},
                        "failed": {"style": "gave up"},
                        "running": {"perf": 15},
                    },
                    Path("run b"): {
                        "selector": "example/app#5",
                        "retry": [],
                        "errors": {},
                        "failed": {},
                        "running": {},
                    },
                    Path("run c"): GitHubError("gh down"),
                }
            ),
        )
        self.assertEqual(
            (
                1,
                "RETRY example/app#3 generic-review generic.md bad json\n"
                "MODEL example/app#3 generic-review opus\n"
                "RETRY example/app#3 security security.md missing\n"
                "FAILED example/app#3 style gave up\n"
                "RUNNING example/app#3 perf 15s\n"
                "ALL_VALID example/app#5\n"
                "FAILED run c gh down\n",
                "",
            ),
            self.run_main("check", "--run", "run a", "--run", "run b", "--run", "run c"),
        )
        self.assertEqual(
            [((Path("run a"), SERVICES), {}), ((Path("run b"), SERVICES), {}), ((Path("run c"), SERVICES), {})],
            stub.calls,
        )

    def test_check_exit_codes(self) -> None:
        role = {"id": "security", "prompt_file": "security.md"}
        cases: list[tuple[dict[str, Any], tuple[int, str]]] = [
            ({"retry": [], "errors": {}, "failed": {}, "running": {"perf": 3}}, (0, "RUNNING example/app#3 perf 3s\n")),
            (
                {"retry": [role], "errors": {"security": "bad"}, "failed": {}, "running": {}},
                (1, "RETRY example/app#3 security security.md bad\n"),
            ),
            (
                {"retry": [], "errors": {"security": "bad"}, "failed": {"security": "bad"}, "running": {}},
                (1, "FAILED example/app#3 security bad\n"),
            ),
            ({"retry": [], "errors": {"security": "bad"}, "failed": {}, "running": {}}, (0, "")),
        ]
        for outcome, (code, out) in cases:
            with self.subTest(outcome=outcome):
                self.stub("check_run", {"selector": "example/app#3", **outcome})
                self.assertEqual((code, out, ""), self.run_main("check", "--run", "run a"))

    def test_finalize(self) -> None:
        stub = self.stub(
            "finalize",
            effect=by_first_argument(
                {
                    Path("run a"): {
                        "selector": "example/app#3",
                        "notes": ["first note"],
                        "canary_root": None,
                        "hashes": {},
                        "verdict": "approve",
                        "findings": 2,
                        "markdown": "review.md",
                    },
                    Path("run b"): {
                        "selector": "example/app#5",
                        "notes": [],
                        "canary_root": "canary root",
                        "hashes": {"record.json": "d1", "review.md": "d2"},
                        "verdict": "request-changes",
                        "findings": 0,
                        "markdown": "canary.md",
                    },
                    Path("run c"): StateError("state locked"),
                }
            ),
        )
        self.assertEqual(
            (
                1,
                "NOTE example/app#3 first note\n"
                "RECORDED example/app#3 verdict=approve findings=2 review.md\n"
                "CANARY example/app#5 canary root\n"
                "SHA256 d1 record.json\n"
                "SHA256 d2 review.md\n"
                "RECORDED example/app#5 verdict=request-changes findings=0 canary.md\n"
                "FAILED run c state locked\n",
                "",
            ),
            self.run_main("finalize", "--run", "run a", "--run", "run b", "--run", "run c"),
        )
        self.assertEqual([((Path("run a"),), {}), ((Path("run b"),), {}), ((Path("run c"),), {})], stub.calls)

    def test_finalize_success_exits_0(self) -> None:
        self.stub(
            "finalize",
            {
                "selector": "example/app#3",
                "notes": [],
                "canary_root": "",
                "hashes": {"ignored.json": "d1"},
                "verdict": "approve",
                "findings": 0,
                "markdown": "review.md",
            },
        )
        self.assertEqual(
            (0, "RECORDED example/app#3 verdict=approve findings=0 review.md\n", ""),
            self.run_main("finalize", "--run", "run a"),
        )

    def test_every_expected_error_fails_only_its_run(self) -> None:
        commands = {
            "check": (
                "check_run",
                {"selector": "example/app#3", "retry": [], "errors": {}, "failed": {}, "running": {}},
            ),
            "finalize": (
                "finalize",
                {
                    "selector": "example/app#3",
                    "notes": [],
                    "canary_root": None,
                    "hashes": {},
                    "verdict": "approve",
                    "findings": 0,
                    "markdown": "review.md",
                },
            ),
            "unfinalized": ("unfinalized_selector", None),
        }
        second = {
            "check": "ALL_VALID example/app#3\n",
            "finalize": "RECORDED example/app#3 verdict=approve findings=0 review.md\n",
            "unfinalized": "",
        }
        for command, (name, success) in commands.items():
            for error_class in EXPECTED:
                with self.subTest(command=command, error=error_class.__name__):
                    self.stub(
                        name, effect=by_first_argument({Path("run a"): error_class("boom"), Path("run b"): success})
                    )
                    self.assertEqual(
                        (1, f"FAILED run a boom\n{second[command]}", ""),
                        self.run_main(command, "--run", "run a", "--run", "run b"),
                    )

    def test_an_unexpected_error_in_a_run_escapes(self) -> None:
        for command, name in (
            ("check", "check_run"),
            ("finalize", "finalize"),
            ("unfinalized", "unfinalized_selector"),
        ):
            with self.subTest(command=command):
                error = KeyError("bug")
                self.stub(name, effect=raising(error))
                with self.assertRaises(KeyError) as caught:
                    self.run_main(command, "--run", "run a", "--run", "run b")
                self.assertIs(error, caught.exception)


class AdvanceTests(MainCase):
    def test_advance(self) -> None:
        stub = self.stub(
            "advance_watermarks",
            {
                "example/app": ("2026-01-01", "2026-02-01"),
                "example/lib": ("2026-01-01", None),
                "example/web": ("2026-01-01", ""),
            },
        )
        self.assertEqual(
            (
                0,
                "WATERMARK example/app 2026-01-01 -> 2026-02-01\n"
                "WATERMARK example/lib unchanged: enumeration failed\n"
                "WATERMARK example/web unchanged: enumeration failed\n",
                "",
            ),
            self.run_main("--config", CONFIG, "advance", "--batch", "batch.json"),
        )
        self.assertEqual([((Path("batch.json"),), {"config_path": Path(CONFIG)})], stub.calls)

    def test_advance_with_nothing_to_move(self) -> None:
        stub = self.stub("advance_watermarks", {})
        self.assertEqual((0, "", ""), self.run_main("advance", "--batch", "batch.json"))
        self.assertEqual([((Path("batch.json"),), {"config_path": None})], stub.calls)


# Each subcommand whose work can raise outside a per-item catch, with the stub that raises and a stub set that
# lets the call reach it.
TOP_LEVEL: list[tuple[str, list[str], str, dict[str, Any]]] = [
    ("inspect-reviewer", ["inspect-reviewer", "--repository", "example/app"], "inspect_reviewer", {}),
    ("validate-reviewer", ["validate-reviewer", "--repository", "example/app"], "validate_reviewer", {}),
    ("enumerate path", ["enumerate"], "working_path", {}),
    ("enumerate", ["enumerate"], "enumerate_batch", {"working_path": Path("batch.json")}),
    ("prepare", ["prepare", "--pull", "example/app#3"], "mark_dispatched", {"prepare": ready("example/app#3", "run")}),
    ("validate-result", ["validate-result", "--run", "run a", "--role", "security"], "validate_result", {}),
    ("workflow", ["workflow", "--run", "run a"], "workflow_script", {}),
    ("wait-reviewers", ["wait-reviewers", "--run", "run a", "--timeout", "5"], "wait_for_reviewers", {}),
    ("dispatch", ["dispatch", "--run", "run a"], "dispatch_copilot", {}),
    ("host", ["host", "--run", "run a", "--token", "token"], "run_host", {}),
    ("wait", ["wait", "--run", "run a", "--timeout", "5"], "wait_for_host", {}),
    ("advance", ["advance", "--batch", "batch.json"], "advance_watermarks", {}),
]


class ErrorTranslationTests(MainCase):
    def test_the_expected_errors_are_these(self) -> None:
        self.assertEqual(EXPECTED, rp.EXPECTED_ERRORS)

    def test_every_expected_error_becomes_one_failed_line(self) -> None:
        errors: list[BaseException] = [error_class("boom") for error_class in EXPECTED]
        errors.append(HostSuperseded("boom"))
        for label, arguments, name, others in TOP_LEVEL:
            for error in errors:
                with self.subTest(command=label, error=type(error).__name__):
                    for other, result in others.items():
                        self.stub(other, result)
                    self.stub(name, effect=raising(error))
                    self.assertEqual((1, "FAILED boom\n", ""), self.run_main(*arguments))

    def test_an_errno_error_prints_as_python_formats_it(self) -> None:
        self.stub("advance_watermarks", effect=raising(FileNotFoundError(2, "No such file", "batch.json")))
        self.assertEqual(
            (1, "FAILED [Errno 2] No such file: 'batch.json'\n", ""),
            self.run_main("advance", "--batch", "batch.json"),
        )

    def test_an_unexpected_error_escapes_as_a_traceback(self) -> None:
        for label, arguments, name, others in TOP_LEVEL:
            for error in (RuntimeError("bug"), KeyError("bug"), TypeError("bug")):
                with self.subTest(command=label, error=type(error).__name__):
                    for other, result in others.items():
                        self.stub(other, result)
                    self.stub(name, effect=raising(error))
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out), self.assertRaises(type(error)) as caught:
                        rp.main(arguments, services=SERVICES)
                    self.assertIs(error, caught.exception)
                    self.assertEqual("", out.getvalue())


class ServicesTests(MainCase):
    def test_host_commands_build_services_when_none_are_given(self) -> None:
        cases = [
            (["dispatch", "--run", "run a"], "dispatch_copilot", Path("host.log"), (Path("run a"),)),
            (["host", "--run", "run a", "--token", "token"], "run_host", "done", (Path("run a"), "token")),
            (["wait", "--run", "run a", "--timeout", "5"], "wait_for_host", (None, 1), (Path("run a"), 5)),
            (
                ["wait-reviewers", "--run", "run a", "--timeout", "5"],
                "wait_for_reviewers",
                ([], {}),
                ([Path("run a")], 5),
            ),
        ]
        for arguments, name, result, leading in cases:
            with self.subTest(command=arguments[0]):
                built = SERVICES_TYPE()
                factory = self.stub("Services", built)
                stub = self.stub(name, result)
                self.assertEqual(0, self.run_main(*arguments, services=None)[0])
                self.assertEqual([((), {})], factory.calls)
                self.assertEqual([((*leading, built), {})], stub.calls)
                self.assertIsNot(SERVICES, built)

    def test_other_commands_pass_none_through(self) -> None:
        self.stub("inspect_reviewer", [])
        self.stub("validate_reviewer", [])
        self.stub("working_path", Path("batch.json"))
        self.stub("enumerate_batch", {"repositories": {}})
        self.stub("prepare", {"status": "skip", "selector": "example/app#3", "reason": "draft"})
        self.stub("check_run", {"selector": "example/app#3", "retry": [], "errors": {}, "failed": {}, "running": {}})
        cases = [
            (["inspect-reviewer", "--repository", "example/app"], "inspect_reviewer"),
            (["validate-reviewer", "--repository", "example/app"], "validate_reviewer"),
            (["enumerate"], "enumerate_batch"),
            (["prepare", "--pull", "example/app#3"], "prepare"),
            (["check", "--run", "run a"], "check_run"),
        ]
        for arguments, name in cases:
            with self.subTest(command=arguments[0]):
                stub = getattr(rp, name)
                self.assertEqual(0, self.run_main(*arguments, services=None)[0])
                args, kwargs = stub.calls[-1]
                self.assertIsNone(kwargs["services"] if "services" in kwargs else args[-1])


class UsageTests(MainCase):
    def test_a_command_is_required(self) -> None:
        self.assert_usage("the following arguments are required: command")

    def test_an_unknown_command_is_refused(self) -> None:
        self.assertIn("argument command: invalid choice: 'frobnicate'", self.usage_error("frobnicate"))

    def test_required_options(self) -> None:
        cases = [
            (["dispatch"], "the following arguments are required: --run"),
            (["wait", "--run", "run a"], "the following arguments are required: --timeout"),
            (["host", "--run", "run a"], "the following arguments are required: --token"),
            (["workflow"], "the following arguments are required: --run"),
            (["wait-reviewers", "--timeout", "5"], "the following arguments are required: --run"),
            (["validate-result", "--run", "run a"], "the following arguments are required: --role"),
            (["check"], "the following arguments are required: --run"),
            (["finalize"], "the following arguments are required: --run"),
            (["unfinalized"], "the following arguments are required: --run"),
            (["advance"], "the following arguments are required: --batch"),
            (["inspect-reviewer"], "the following arguments are required: --repository"),
            (["validate-reviewer"], "the following arguments are required: --repository"),
        ]
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                self.assert_usage(message, *arguments)

    def test_a_pull_number_must_be_an_integer(self) -> None:
        self.assert_usage(
            "argument --pull: invalid int value: 'x'", "validate-reviewer", "--repository", "example/app", "--pull", "x"
        )


if __name__ == "__main__":
    unittest.main()
