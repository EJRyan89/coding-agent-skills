"""review_pipeline.main, pinned: for each subcommand, the exact call it makes, the lines it prints, and its exit code;
which errors become a FAILED line and which escape as a traceback; and the usage errors that exit 2 before any work.
Every function main calls is replaced by a recording stub, so these tests exercise main alone."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import review_batches
import review_dispatch
import review_pipeline as rp
import review_run
from review_archive import ArchiveError
from review_canary import FixtureError
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
from test_review_run import prepared_state

Call = tuple[tuple[Any, ...], dict[str, Any]]

SERVICES_TYPE = review_run.Services  # setUp replaces review_run.Services, so the class is kept here
UNFINALIZED_SELECTOR = review_dispatch.unfinalized_selector  # and these functions, which some tests run unstubbed
WORKED_BY_SUBAGENTS = review_batches.worked_by_subagents
SERVICES = SERVICES_TYPE()
CONFIG = "review config.json"
EXPECTED = (
    review_run.PipelineError,
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
    FixtureError,
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
    "prepare_fixture",
    "mark_dispatched",
    "remove_run",
    "validate_result",
    "workflow_script",
    "wait_for_reviewers",
    "unfinalized_selector",
    "worked_by_subagents",
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

    def stub(
        self, name: str, result: Any = None, *, effect: Callable[..., Any] | None = None, module: ModuleType = rp
    ) -> Stub:
        """Replace `name` where `module`, the pipeline's command line unless given, looks it up."""
        stub = Stub(result, effect)
        patcher = mock.patch.object(module, name, stub)
        patcher.start()
        self.addCleanup(patcher.stop)
        return stub

    def run_main(self, *arguments: str, services: review_run.Services | None = SERVICES) -> tuple[int, str, str]:
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
                    {"pulls": [3, 5], "ref": None, "host": None, "config_path": Path(CONFIG), "services": SERVICES},
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
            [
                (
                    ("example/app",),
                    {"pulls": [], "ref": "abc123", "host": None, "config_path": None, "services": SERVICES},
                )
            ],
            stub.calls,
        )

    def test_validate_reviewer_passes_the_host(self) -> None:
        stub = self.stub("validate_reviewer", ["VALID example/app"])
        result = self.run_main(
            "validate-reviewer", "--repository", "example/app", "--pull", "3", "--host", "copilot-cli"
        )
        self.assertEqual((0, "VALID example/app\n", ""), result)
        self.assertEqual("copilot-cli", stub.calls[0][1]["host"])


class EnumerateTests(MainCase):
    BATCH: ClassVar[dict[str, Any]] = {
        "repositories": {
            "example/app": {
                "complete": True,
                "eligible": [{"number": 3}, {"number": 5}],
                "deferred": [{"number": 8}, {"number": 9}, {"number": 11}],
                "listing": {"scan": "watermark", "pages": 2, "pulls": 130, "read": 40},
            },
            "example/lib": {
                "complete": False,
                "error": "gh failed",
                "eligible": [{"number": 7}],
                "deferred": [],
                "listing": None,
            },
            "example/web": {
                "complete": True,
                "eligible": [],
                "deferred": [],
                "listing": {"scan": "full", "pages": 20, "pulls": 1803, "read": 30},
            },
        }
    }

    def test_prints_each_repository_then_the_batch_and_exits_1_after_a_failed_repository(self) -> None:
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
            "--batch-size",
            "2",
            "--output",
            "my batch.json",
        )
        # A failed repository is a batch item's failure, so the command exits 1, after still listing the others and
        # writing the batch, as the tracker's collect does.
        self.assertEqual(
            (
                1,
                "LISTED example/app scan=watermark pages=2 pulls=130 read=40 candidates=2 deferred=3\n"
                "PULL example/app#3\n"
                "PULL example/app#5\n"
                "REPOSITORY_FAILED example/lib gh failed\n"
                "PULL example/lib#7\n"
                "LISTED example/web scan=full pages=20 pulls=1803 read=30 candidates=0 deferred=0\n"
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
                        "batch_size": 2,
                        "config_path": Path(CONFIG),
                        "services": SERVICES,
                    },
                )
            ],
            batch.calls,
        )

    def test_exits_0_when_every_repository_listed(self) -> None:
        self.stub("working_path", Path("batch.json"))
        listed = {name: entry for name, entry in self.BATCH["repositories"].items() if entry["complete"]}
        self.stub("enumerate_batch", {"repositories": listed})
        code, out, _ = self.run_main("enumerate")
        self.assertEqual(0, code)
        self.assertNotIn("REPOSITORY_FAILED", out)
        self.assertEqual("BATCH batch.json", out.splitlines()[-1])

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
                        "batch_size": None,
                        "config_path": None,
                        "services": SERVICES,
                    },
                )
            ],
            batch.calls,
        )

    def test_batch_size_is_a_positive_integer(self) -> None:
        for value in ("0", "-1", "two", "2.5"):
            with self.subTest(value=value):
                self.assertIn("error: argument --batch-size:", self.usage_error("enumerate", "--batch-size", value))

    def test_repository_and_repository_set_exclude_each_other(self) -> None:
        self.assert_usage(
            "argument --repository-set: not allowed with argument --repository",
            "enumerate",
            "--repository",
            "example/app",
            "--repository-set",
            "work",
        )


def ready(selector: str, run: str, runtime: str = "claude-code", dispatch: str | None = None) -> dict[str, Any]:
    return {
        "status": "ready",
        "selector": selector,
        "run": Path(run),
        "notes": ["first note", "second note"],
        "runtime": runtime,
        "dispatch": dispatch or ("copilot-host" if runtime == "copilot-cli" else "subagents"),
        "roles": [
            {"id": "generic-review", "prompt_file": "generic.md", "model": "opus"},
            {"id": "security", "prompt_file": "security.md"},
            {"id": "style", "prompt_file": "style.md", "model": ""},
        ],
    }


def run_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("RUN ")]


def prepare_call(
    selector: str,
    *,
    re_review: bool = False,
    scope: str | None = None,
    force: bool = False,
    canary: bool = False,
    host: str | None = None,
    inline: bool = False,
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
            "inline": inline,
            "config_path": config_path,
            "services": SERVICES,
        },
    )


AT_LEAST_ONE = "prepare takes at least one --pull, --re-review, --canary selector, --batch, or --runs"
BATCH_ALONE = "--batch takes no other selector, no --scope, and no --canary"
RUNS_ALONE = "--runs takes no other option; the run list holds them"


class RunListCase(MainCase):
    """A case whose prepare writes its run list, or whose commands read one, in a temporary directory of its own."""

    def setUp(self) -> None:
        super().setUp()
        self.directory = Path(tempfile.mkdtemp(prefix="review-pipeline-main-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.run_list = self.directory / "runs list.json"
        self.stub("working_path", self.run_list)

    def runs_line(self, pending: int = 0) -> str:
        return f"RUNS {self.run_list} pending={pending}\n"

    def listed(self) -> dict[str, Any]:
        return dict(json.loads(self.run_list.read_text(encoding="utf-8")))

    def write_list(self, runs: list[tuple[str, str]], pending: list[dict[str, str]] | None = None) -> None:
        """A run list naming each (run, status) pair as a run of example/app#<its place in the list>."""
        options: dict[str, Any] = dict.fromkeys(review_batches.RUN_LIST_OPTIONS)
        options.update(inline=False, force=False, canary=False)
        value = review_batches.new_run_list(pending or [], options)
        value["runs"] = [
            {"run": run, "selector": f"example/app#{index}", "status": status}
            for index, (run, status) in enumerate(runs, 1)
        ]
        self.run_list.write_text(json.dumps(value), encoding="utf-8")

    def statuses(self) -> list[str]:
        return [entry["status"] for entry in self.listed()["runs"]]


class PrepareTests(RunListCase):
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
                "HOST copilot-cli run seven\n" + self.runs_line(),
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

    def test_an_inline_run_prints_its_directory_and_starts_no_clock(self) -> None:
        prepare = self.stub(
            "prepare",
            effect=by_first_argument({"example/app#3": ready("example/app#3", "run three", dispatch="inline")}),
        )
        dispatched = self.stub("mark_dispatched")
        result = self.run_main("--config", CONFIG, "prepare", "--pull", "example/app#3", "--inline")
        self.assertEqual(
            (
                0,
                "RUN example/app#3 run three\n"
                "NOTE example/app#3 first note\n"
                "NOTE example/app#3 second note\n"
                "INLINE run three\n" + self.runs_line(),
                "",
            ),
            result,
        )
        self.assertEqual([prepare_call("example/app#3", inline=True, config_path=Path(CONFIG))], prepare.calls)
        self.assertEqual([], dispatched.calls, "next-role starts each inline role's clock")

    def test_canary_is_passed_through(self) -> None:
        prepare = self.stub("prepare", effect=by_first_argument({"example/app#3": ready("example/app#3", "run three")}))
        self.stub("mark_dispatched")
        code, _, _ = self.run_main("prepare", "--pull", "example/app#3", "--canary")
        self.assertEqual(0, code)
        self.assertEqual([prepare_call("example/app#3", canary=True)], prepare.calls)

    def test_a_fixture_canary_is_passed_through_and_prints_as_a_pull_request_does(self) -> None:
        prepare = self.stub("prepare_fixture", effect=lambda *_, **__: ready("example/app#3", "run three"))
        dispatched = self.stub("mark_dispatched")
        cases = {
            "initial": ([], None),
            "re-review": (["--re-review", "--prior", "prior record.json"], Path("prior record.json")),
        }
        for name, (extra, prior) in cases.items():
            with self.subTest(name):
                prepare.calls.clear()
                dispatched.calls.clear()
                arguments = ["prepare", "--host", "claude-code", "--canary", "--fixture", "fixture dir", *extra]
                result = self.run_main("--config", CONFIG, *arguments)
                self.assertEqual(
                    (
                        0,
                        "RUN example/app#3 run three\n"
                        "NOTE example/app#3 first note\n"
                        "NOTE example/app#3 second note\n"
                        "ROLE generic-review generic.md\n"
                        "MODEL example/app#3 generic-review opus\n"
                        "ROLE security security.md\n"
                        "ROLE style style.md\n" + self.runs_line(),
                        "",
                    ),
                    result,
                )
                expected = {
                    "prior": prior,
                    "host": "claude-code",
                    "inline": False,
                    "config_path": Path(CONFIG),
                    "services": SERVICES,
                }
                self.assertEqual([((Path("fixture dir"),), expected)], prepare.calls)
                self.assertEqual([((Path("run three"),), {})], dispatched.calls)

    def test_a_failed_fixture_canary_is_one_line_naming_its_directory(self) -> None:
        self.stub("prepare_fixture", effect=raising(FixtureError("pull.json number must be a positive integer")))
        self.assertEqual(
            (1, "FAILED fixture dir pull.json number must be a positive integer\n" + self.runs_line(), ""),
            self.run_main("prepare", "--canary", "--fixture", "fixture dir"),
        )

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
                "FAILED example/app#9 disk gone\n" + self.runs_line(),
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
                    (1, "FAILED example/app#3 boom\nSKIP example/app#5 draft\n" + self.runs_line(), ""),
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

    def test_an_expected_error_while_marking_fails_only_its_pull_request(self) -> None:
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
        dispatched = self.stub(
            "mark_dispatched",
            effect=by_first_argument({Path("run five"): PersistenceError("disk full"), Path("run seven"): None}),
        )
        removed = self.stub("remove_run")
        self.assertEqual(
            (
                1,
                "SKIP example/app#3 draft\n"
                "FAILED example/app#5 disk full\n"
                "RUN example/app#7 run seven\n"
                "NOTE example/app#7 first note\n"
                "NOTE example/app#7 second note\n"
                "ROLE generic-review generic.md\n"
                "MODEL example/app#7 generic-review opus\n"
                "ROLE security security.md\n"
                "ROLE style style.md\n" + self.runs_line(),
                "",
            ),
            self.run_main("prepare", "--pull", "example/app#3", "--pull", "example/app#5", "--pull", "example/app#7"),
        )
        self.assertEqual([((Path("run five"),), {}), ((Path("run seven"),), {})], dispatched.calls)
        self.assertEqual([((Path("run five"),), {})], removed.calls)

    def test_every_expected_marking_error_is_selector_qualified_and_cleans_up(self) -> None:
        self.stub("prepare", ready("example/app#3", "run three"))
        removed = self.stub("remove_run")
        for error_class in EXPECTED:
            with self.subTest(error=error_class.__name__):
                removed.calls.clear()
                self.stub("mark_dispatched", effect=raising(error_class("boom")))
                self.assertEqual(
                    (1, "FAILED example/app#3 boom\n" + self.runs_line(), ""),
                    self.run_main("prepare", "--pull", "example/app#3"),
                )
                self.assertEqual([((Path("run three"),), {})], removed.calls)

    def test_a_fixture_marking_failure_names_its_directory_and_cleans_up(self) -> None:
        self.stub("prepare_fixture", ready("example/app#3", "run three"))
        self.stub("mark_dispatched", effect=raising(PersistenceError("disk full")))
        removed = self.stub("remove_run")
        self.assertEqual(
            (1, "FAILED fixture dir disk full\n" + self.runs_line(), ""),
            self.run_main("prepare", "--canary", "--fixture", "fixture dir"),
        )
        self.assertEqual([((Path("run three"),), {})], removed.calls)

    def test_an_unexpected_marking_error_escapes(self) -> None:
        error = RuntimeError("bug")
        self.stub("prepare", ready("example/app#3", "run three"))
        self.stub("mark_dispatched", effect=raising(error))
        with self.assertRaises(RuntimeError) as caught:
            self.run_main("prepare", "--pull", "example/app#3")
        self.assertIs(error, caught.exception)

    def test_a_malformed_selector_is_not_a_duplicate(self) -> None:
        prepare = self.stub(
            "prepare", effect=raising(ReviewOperationError("Pull selector must be owner/repository#number"))
        )
        self.assertEqual(
            (
                1,
                "FAILED not-a-pull Pull selector must be owner/repository#number\n"
                "FAILED not-a-pull Pull selector must be owner/repository#number\n" + self.runs_line(),
                "",
            ),
            self.run_main("prepare", "--pull", "not-a-pull", "--pull", "not-a-pull"),
        )
        self.assertEqual(2, len(prepare.calls))

    def test_more_than_four_pull_requests_are_prepared_four_at_a_time_from_the_run_list(self) -> None:
        selectors = [f"example/app#{number}" for number in range(1, 7)]
        prepare = self.stub(
            "prepare", effect=lambda selector, **_: ready(selector, f"run {selector[-1]}", dispatch="inline")
        )
        self.stub("mark_dispatched")
        arguments = [part for selector in selectors[:5] for part in ("--pull", selector)]
        code, out, _ = self.run_main(
            "--config", CONFIG, "prepare", *arguments, "--re-review", selectors[5], "--scope", "full", "--force"
        )
        self.assertEqual(0, code)
        self.assertEqual([f"RUN {selector} run {selector[-1]}" for selector in selectors[:4]], run_lines(out))
        self.assertTrue(out.endswith(self.runs_line(2)))
        common: dict[str, Any] = {"force": True, "config_path": Path(CONFIG)}
        self.assertEqual(
            [prepare_call(selector, **common) for selector in selectors[:4]],
            sorted(prepare.calls, key=lambda call: call[0][0]),
        )
        listed = self.listed()
        self.assertEqual(
            [{"kind": "pull", "target": selectors[4]}, {"kind": "re-review", "target": selectors[5]}],
            listed["pending"],
        )
        self.assertEqual(
            [{"run": f"run {selector[-1]}", "selector": selector, "status": "published"} for selector in selectors[:4]],
            listed["runs"],
        )
        # The next call takes the run list alone: the options are the first call's, and the list grows.
        prepare.calls.clear()
        code, out, _ = self.run_main("prepare", "--runs", str(self.run_list))
        self.assertEqual(0, code)
        self.assertEqual([f"RUN {selector} run {selector[-1]}" for selector in selectors[4:]], run_lines(out))
        self.assertTrue(out.endswith(self.runs_line()))
        self.assertEqual(
            [
                prepare_call(selectors[4], **common),
                prepare_call(selectors[5], re_review=True, scope="full", **common),
            ],
            sorted(prepare.calls, key=lambda call: call[0][0]),
        )
        self.assertEqual([], self.listed()["pending"])
        self.assertEqual(selectors, [entry["selector"] for entry in self.listed()["runs"]])
        # A list with nothing left to prepare prepares nothing.
        prepare.calls.clear()
        self.assertEqual((0, self.runs_line(), ""), self.run_main("prepare", "--runs", str(self.run_list)))
        self.assertEqual([], prepare.calls)

    def test_the_run_list_drops_a_group_before_preparing_it(self) -> None:
        seen: list[list[dict[str, str]]] = []

        def prepare(selector: str, **_: Any) -> dict[str, Any]:
            seen.append(self.listed()["pending"])
            raise GitHubError("gh down")

        self.stub("prepare", effect=prepare)
        arguments = [part for number in range(1, 6) for part in ("--pull", f"example/app#{number}")]
        code, out, _ = self.run_main("prepare", *arguments)
        self.assertEqual(1, code)
        self.assertEqual(
            "".join(f"FAILED example/app#{number} gh down\n" for number in range(1, 5)) + self.runs_line(1), out
        )
        # A call stopped mid-group never prepares that group again: the list already moved past it.
        self.assertEqual([[{"kind": "pull", "target": "example/app#5"}]] * 4, seen)
        self.assertEqual([], self.listed()["runs"], "a failed pull request publishes no run")

    def test_a_batch_file_is_prepared_in_the_order_enumerate_printed_it(self) -> None:
        batch = self.directory / "batch.json"
        value = {"schema_version": 2, **EnumerateTests.BATCH}
        batch.write_text(json.dumps(value), encoding="utf-8")
        prepare = self.stub(
            "prepare", effect=lambda selector, **_: {"status": "skip", "selector": selector, "reason": "x"}
        )
        code, out, _ = self.run_main("prepare", "--batch", str(batch), "--force", "--host", "codex")
        self.assertEqual(
            (0, "SKIP example/app#3 x\nSKIP example/app#5 x\nSKIP example/lib#7 x\n" + self.runs_line()), (code, out)
        )
        self.assertEqual(
            [
                prepare_call(selector, force=True, host="codex")
                for selector in ("example/app#3", "example/app#5", "example/lib#7")
            ],
            sorted(prepare.calls, key=lambda call: call[0][0]),
        )

    def test_an_empty_batch_writes_an_empty_run_list(self) -> None:
        batch = self.directory / "batch.json"
        batch.write_text(json.dumps({"schema_version": 2, "repositories": {}}), encoding="utf-8")
        self.assertEqual((0, self.runs_line(), ""), self.run_main("prepare", "--batch", str(batch)))
        self.assertEqual(([], []), (self.listed()["pending"], self.listed()["runs"]))
        self.assertEqual((0, "ALL_FINALIZED\n", ""), self.run_main("unfinalized", "--runs", str(self.run_list)))

    def test_a_batch_file_that_is_not_one_fails_before_any_run_list(self) -> None:
        batch = self.directory / "batch.json"
        for value, reason in (
            # Version 1 recorded no deferred pull requests, so reading one as this version would be a guess.
            ({"schema_version": 1}, f"{batch} is not a review batch file"),
            ({"schema_version": 3}, f"{batch} is not a review batch file"),
            ({"schema_version": 2, "repositories": {"example/app": {"eligible": [{"number": "3"}]}}}, "number"),
        ):
            with self.subTest(reason=reason):
                batch.write_text(json.dumps(value), encoding="utf-8")
                code, out, _ = self.run_main("prepare", "--batch", str(batch))
                self.assertEqual(1, code)
                self.assertTrue(out.startswith("FAILED ") and reason in out, out)
                self.assertFalse(self.run_list.exists())

    def test_canary_selectors_are_canaries_of_those_pull_requests(self) -> None:
        prepare = self.stub(
            "prepare", effect=lambda selector, **_: {"status": "skip", "selector": selector, "reason": "x"}
        )
        code, _, _ = self.run_main("prepare", "--canary", "example/app#3", "--canary", "example/lib#7")
        self.assertEqual(0, code)
        self.assertEqual(
            [prepare_call("example/app#3", canary=True), prepare_call("example/lib#7", canary=True)],
            sorted(prepare.calls, key=lambda call: call[0][0]),
        )
        self.assertTrue(self.listed()["options"]["canary"])

    def test_several_fixtures_are_prepared_as_one_run_list(self) -> None:
        directories = [f"fixture {number}" for number in range(1, 6)]
        prepare = self.stub(
            "prepare_fixture", effect=lambda directory, **_: ready("example/app#1", f"run of {directory.name}")
        )
        self.stub("mark_dispatched")
        arguments = [part for directory in directories for part in ("--fixture", directory)]
        code, out, _ = self.run_main("prepare", "--host", "claude-code", "--canary", *arguments)
        self.assertEqual(0, code)
        self.assertEqual([f"RUN example/app#1 run of {directory}" for directory in directories[:4]], run_lines(out))
        self.assertTrue(out.endswith(self.runs_line(1)))
        expected = {"prior": None, "host": "claude-code", "inline": False, "config_path": None, "services": SERVICES}
        self.assertEqual(
            [((Path(directory),), expected) for directory in directories[:4]],
            sorted(prepare.calls, key=lambda call: str(call[0][0])),
        )
        self.assertEqual([{"kind": "fixture", "target": directories[4]}], self.listed()["pending"])

    def test_a_run_list_that_is_not_one_is_refused(self) -> None:
        cases: list[tuple[Any, str]] = [
            ({"schema_version": 2}, "is not a review run list"),
            ([], "run list must be an object"),
            ({"schema_version": 1, "options": {}, "pending": [], "runs": []}, "run list.options"),
        ]
        for value, reason in cases:
            with self.subTest(reason=reason):
                self.run_list.write_text(json.dumps(value), encoding="utf-8")
                code, out, _ = self.run_main("prepare", "--runs", str(self.run_list))
                self.assertEqual(1, code)
                self.assertTrue(out.startswith("FAILED ") and reason in out, out)
                self.assertEqual(value, json.loads(self.run_list.read_text(encoding="utf-8")), "left as it was")

    def test_a_run_list_inside_a_skills_directory_is_refused(self) -> None:
        self.write_list([])
        holding = self.stub("skills_directory_holding", Path("skills root"), module=review_batches)
        self.assertEqual(
            (
                1,
                f"FAILED {self.run_list} is inside the skills directory skills root; "
                "pass the run list prepare printed\n",
                "",
            ),
            self.run_main("prepare", "--runs", str(self.run_list)),
        )
        self.assertEqual([((self.run_list,), {})], holding.calls)

    def test_usage_errors(self) -> None:
        cases = [
            (AT_LEAST_ONE, ["prepare"]),
            (AT_LEAST_ONE, ["prepare", "--canary", "--force", "--scope", "full"]),
            (AT_LEAST_ONE, ["prepare", "--canary", "--host", "codex", "--inline"]),
            (
                "--canary takes only --pull selectors and no --force",
                ["prepare", "--canary", "--pull", "example/app#1", "--force"],
            ),
            (
                "--canary takes only --pull selectors and no --force",
                ["prepare", "--canary", "--re-review", "example/app#1"],
            ),
            (
                "--canary takes only --pull selectors and no --force",
                ["prepare", "--canary", "example/app#1", "--force"],
            ),
            (
                "example/app#1 is named more than once",
                ["prepare", "--canary", "example/app#1", "--pull", "example/app#1"],
            ),
            (
                "example/app#1 is named more than once",
                ["prepare", "--canary", "example/app#1", "--canary", "example/app#1"],
            ),
            (BATCH_ALONE, ["prepare", "--batch", "batch.json", "--pull", "example/app#1"]),
            (BATCH_ALONE, ["prepare", "--batch", "batch.json", "--re-review", "example/app#1", "--scope", "full"]),
            (BATCH_ALONE, ["prepare", "--batch", "batch.json", "--canary"]),
            (BATCH_ALONE, ["prepare", "--batch", "batch.json", "--canary", "example/app#1"]),
            (BATCH_ALONE, ["prepare", "--batch", "batch.json", "--scope", "full"]),
            (RUNS_ALONE, ["prepare", "--runs", "runs.json", "--pull", "example/app#1"]),
            (RUNS_ALONE, ["prepare", "--runs", "runs.json", "--batch", "batch.json"]),
            (RUNS_ALONE, ["prepare", "--runs", "runs.json", "--host", "codex"]),
            (RUNS_ALONE, ["prepare", "--runs", "runs.json", "--inline"]),
            (RUNS_ALONE, ["prepare", "--runs", "runs.json", "--force"]),
            (RUNS_ALONE, ["prepare", "--runs", "runs.json", "--canary", "--fixture", "fixture"]),
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

    def test_fixture_usage_errors(self) -> None:
        only = "--fixture takes --canary, and a bare --re-review with --prior, and no other selector"
        paired = "--prior takes one bare --re-review, and a bare --re-review takes --prior"
        elsewhere = "--prior and a bare --re-review are taken only with --fixture"
        fixture = ["prepare", "--fixture", "fixture"]
        cases = [
            (only, fixture),
            (only, [*fixture, "--canary", "--pull", "example/app#1"]),
            (only, [*fixture, "--canary", "--re-review", "example/app#1", "--prior", "prior.json"]),
            (only, [*fixture, "--canary", "--re-review", "--prior", "prior.json", "--scope", "full"]),
            (only, [*fixture, "--canary", "--force"]),
            (paired, [*fixture, "--canary", "--re-review"]),
            (paired, [*fixture, "--canary", "--prior", "prior.json"]),
            (paired, [*fixture, "--canary", "--re-review", "--re-review", "--prior", "prior.json"]),
            (only, [*fixture, "--canary", "example/app#1"]),
            (only, [*fixture, "--canary", "--batch", "batch.json"]),
            (
                "--prior takes one --fixture",
                [*fixture, "--canary", "--fixture", "other", "--re-review", "--prior", "prior.json"],
            ),
            (elsewhere, ["prepare", "--canary", "--pull", "example/app#1", "--prior", "prior.json"]),
            (elsewhere, ["prepare", "--re-review", "--scope", "full"]),
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
    PROGRESS: ClassVar[list[tuple[str, dict[str, tuple[str, int]]]]] = [
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
                        "runtime": "codex",
                        "retry": [
                            {"id": "generic-review", "prompt_file": "generic.md", "model": "opus", "effort": None},
                            {"id": "security", "prompt_file": "security.md", "effort": "low"},
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
                "EFFORT example/app#3 security low\n"
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
                self.stub("check_run", {"selector": "example/app#3", "runtime": "claude-code", **outcome})
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
                        "stats": ["snapshot source=checkout files=2", "reviewer generic files_read=unknown"],
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
                "STATS example/app#5 snapshot source=checkout files=2\n"
                "STATS example/app#5 reviewer generic files_read=unknown\n"
                "RECORDED example/app#5 verdict=request-changes findings=0 canary.md\n"
                "FAILED run c state locked\n",
                "",
            ),
            self.run_main("finalize", "--run", "run a", "--run", "run b", "--run", "run c"),
        )
        self.assertEqual(
            [((Path("run a"), SERVICES), {}), ((Path("run b"), SERVICES), {}), ((Path("run c"), SERVICES), {})],
            stub.calls,
        )

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


class RunListCommandTests(RunListCase):
    """The commands that take a run list: which listed runs each works on, and the statuses it records."""

    def test_check_works_on_the_published_runs_and_records_what_it_found(self) -> None:
        self.write_list(
            [
                ("run a", "published"),
                ("run b", "valid"),
                ("run c", "failed"),
                ("run d", "published"),
                ("run e", "published"),
                ("run f", "recorded"),
                ("run g", "published"),
                ("run h", "published"),
            ]
        )
        valid: dict[str, Any] = {"retry": [], "errors": {}, "failed": {}, "running": {}}
        stub = self.stub(
            "check_run",
            effect=by_first_argument(
                {
                    Path("run a"): {"selector": "example/app#1", **valid},
                    Path("run d"): {
                        "selector": "example/app#4",
                        "retry": [],
                        "errors": {"x": "bad"},
                        "failed": {"x": "bad"},
                        "running": {},
                    },
                    Path("run e"): {
                        "selector": "example/app#5",
                        "runtime": "claude-code",
                        "retry": [{"id": "x", "prompt_file": "x.md", "effort": "high"}],
                        "errors": {"x": "bad"},
                        "failed": {},
                        "running": {},
                    },
                    Path("run g"): {
                        "selector": "example/app#7",
                        "retry": [],
                        "errors": {},
                        "failed": {},
                        "running": {"x": 4},
                    },
                    Path("run h"): review_run.PipelineError("run h is not a prepared review run"),
                }
            ),
        )
        self.assertEqual(
            (
                1,
                "ALL_VALID example/app#1\n"
                "FAILED example/app#4 x bad\n"
                "RETRY example/app#5 x x.md bad\n"
                "RUNNING example/app#7 x 4s\n"
                "FAILED run h run h is not a prepared review run\n",
                "",
            ),
            self.run_main("check", "--runs", str(self.run_list)),
        )
        self.assertEqual([Path(f"run {name}") for name in "adegh"], [call[0][0] for call in stub.calls])
        self.assertEqual(
            ["valid", "valid", "failed", "failed", "published", "recorded", "published", "failed"], self.statuses()
        )

    def test_finalize_works_on_the_runs_check_accepted(self) -> None:
        self.write_list([("run a", "valid"), ("run b", "published"), ("run c", "valid"), ("run d", "failed")])
        recorded: dict[str, Any] = {
            "selector": "example/app#1",
            "notes": [],
            "canary_root": None,
            "hashes": {},
            "verdict": "approve",
            "findings": 0,
            "markdown": "review.md",
        }
        stub = self.stub(
            "finalize", effect=by_first_argument({Path("run a"): recorded, Path("run c"): StateError("state locked")})
        )
        self.assertEqual(
            (1, "RECORDED example/app#1 verdict=approve findings=0 review.md\nFAILED run c state locked\n", ""),
            self.run_main("finalize", "--runs", str(self.run_list)),
        )
        self.assertEqual([Path("run a"), Path("run c")], [call[0][0] for call in stub.calls])
        self.assertEqual(["recorded", "published", "failed", "failed"], self.statuses())

    def test_unfinalized_names_every_listed_run_and_every_pull_request_never_prepared(self) -> None:
        self.write_list(
            [("run a", "recorded"), ("run b", "failed"), ("run c", "published")],
            pending=[{"kind": "pull", "target": "example/app#9"}, {"kind": "fixture", "target": "fixture dir"}],
        )
        stub = self.stub(
            "unfinalized_selector",
            effect=by_first_argument({Path("run a"): None, Path("run b"): "example/app#2", Path("run c"): None}),
        )
        self.assertEqual(
            (
                1,
                f"UNFINALIZED example/app#2 {Path('run b').resolve()}\n"
                "UNPREPARED example/app#9\n"
                "UNPREPARED fixture dir\n",
                "",
            ),
            self.run_main("unfinalized", "--runs", str(self.run_list)),
        )
        self.assertEqual([Path(f"run {name}") for name in "abc"], [call[0][0] for call in stub.calls])

    def test_unfinalized_over_an_empty_run_list_is_all_finalized(self) -> None:
        self.write_list([])
        self.assertEqual((0, "ALL_FINALIZED\n", ""), self.run_main("unfinalized", "--runs", str(self.run_list)))

    def test_a_listed_directory_that_is_not_a_prepared_run_fails_alone(self) -> None:
        self.stub("unfinalized_selector", effect=UNFINALIZED_SELECTOR)
        stray = self.directory / "not a run"
        stray.mkdir()
        gone = self.directory / "recorded run"
        self.write_list([(str(stray), "published"), (str(gone), "recorded")])
        code, out, _ = self.run_main("unfinalized", "--runs", str(self.run_list))
        self.assertEqual(1, code)
        self.assertEqual(1, len(out.splitlines()), out)
        self.assertTrue(out.startswith(f"FAILED {stray} "), out)

    def test_a_malformed_run_list_fails_every_command_that_reads_it(self) -> None:
        self.write_list([("run a", "published")])
        value = self.listed()
        value["runs"][0]["status"] = "done"
        self.run_list.write_text(json.dumps(value), encoding="utf-8")
        for arguments in (["check"], ["finalize"], ["unfinalized"], ["workflow"], ["wait-reviewers", "--timeout", "5"]):
            with self.subTest(command=arguments[0]):
                code, out, _ = self.run_main(*arguments, "--runs", str(self.run_list))
                self.assertEqual(1, code)
                self.assertTrue(out.startswith("FAILED run list.runs[0].status must be one of"), out)

    def test_workflow_and_wait_reviewers_take_the_published_runs_started_as_subagents(self) -> None:
        self.write_list([("run a", "published"), ("run b", "published"), ("run c", "valid"), ("run d", "published")])
        self.stub(
            "worked_by_subagents",
            effect=by_first_argument({Path("run a"): True, Path("run b"): False, Path("run d"): True}),
        )
        workflow = self.stub("workflow_script", (Path("flow.js"), "text\n", 2))
        self.assertEqual(
            (0, "WORKFLOW flow.js roles=2\nBEGIN_WORKFLOW_SCRIPT\ntext\nEND_WORKFLOW_SCRIPT\n", ""),
            self.run_main("workflow", "--runs", str(self.run_list)),
        )
        self.assertEqual([(([Path("run a"), Path("run d")],), {})], workflow.calls)
        waited = self.stub("wait_for_reviewers", ([], {}))
        self.assertEqual((0, "", ""), self.run_main("wait-reviewers", "--runs", str(self.run_list), "--timeout", "5"))
        self.assertEqual([(([Path("run a"), Path("run d")], 5, SERVICES), {})], waited.calls)

    def test_workflow_with_no_run_started_as_subagents_prints_none(self) -> None:
        self.write_list([("run a", "published"), ("run b", "valid")])
        self.stub("worked_by_subagents", False)
        self.assertEqual((0, "WORKFLOW none\n", ""), self.run_main("workflow", "--runs", str(self.run_list)))

    def test_a_run_list_and_run_directories_exclude_each_other(self) -> None:
        for command in ("workflow", "check", "finalize", "unfinalized"):
            with self.subTest(command=command):
                self.assert_usage(
                    "argument --runs: not allowed with argument --run",
                    command,
                    "--run",
                    "run a",
                    "--runs",
                    "runs.json",
                )

    def test_worked_by_subagents_reads_the_run(self) -> None:
        self.stub("worked_by_subagents", effect=WORKED_BY_SUBAGENTS)
        cases = {"subagents": True, "inline": False, "copilot-host": False}
        for dispatch, expected in cases.items():
            with self.subTest(dispatch=dispatch):
                run = self.directory / dispatch
                run.mkdir()
                (run / review_run.RUN_FILE).write_text(
                    json.dumps(prepared_state(dispatch=dispatch)),
                    encoding="utf-8",
                )
                self.assertIs(expected, review_batches.worked_by_subagents(run))
        self.assertTrue(
            review_batches.worked_by_subagents(self.directory / "missing"), "the command it was listed for reports it"
        )


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
        self.assertEqual([((Path("batch.json"),), {"config_path": Path(CONFIG), "services": SERVICES})], stub.calls)

    def test_advance_with_nothing_to_move(self) -> None:
        stub = self.stub("advance_watermarks", {})
        self.assertEqual((0, "", ""), self.run_main("advance", "--batch", "batch.json"))
        self.assertEqual([((Path("batch.json"),), {"config_path": None, "services": SERVICES})], stub.calls)


# Each subcommand whose work can raise outside a per-item catch, with the stub that raises and a stub set that
# lets the call reach it.
TOP_LEVEL: list[tuple[str, list[str], str, dict[str, Any]]] = [
    ("inspect-reviewer", ["inspect-reviewer", "--repository", "example/app"], "inspect_reviewer", {}),
    ("validate-reviewer", ["validate-reviewer", "--repository", "example/app"], "validate_reviewer", {}),
    ("enumerate path", ["enumerate"], "working_path", {}),
    ("prepare path", ["prepare", "--pull", "example/app#1"], "working_path", {}),
    ("enumerate", ["enumerate"], "enumerate_batch", {"working_path": Path("batch.json")}),
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
        self.assertEqual(EXPECTED, review_run.EXPECTED_ERRORS)

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


class ServicesTests(RunListCase):
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
            (["workflow"], "one of the arguments --run --runs is required"),
            (["wait-reviewers", "--timeout", "5"], "one of the arguments --run --runs is required"),
            (["validate-result", "--run", "run a"], "the following arguments are required: --role"),
            (["check"], "one of the arguments --run --runs is required"),
            (["finalize"], "one of the arguments --run --runs is required"),
            (["unfinalized"], "one of the arguments --run --runs is required"),
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
