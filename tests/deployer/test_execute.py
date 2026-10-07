"""pipeline.execute called directly, with every collaborator replaced: the calls it makes in order, where it releases
the lock, what it prints, and the exit code it returns, for each way a run ends (#91)."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from deployer import config, errors, journal, lock, pipeline, platform_support, source
from deployer.context import Options
from deployer.errors import Cancelled, DeployError
from deployer.paths import Paths

SOURCE_ID = "test/skills"
RUN_ID = "19700101-000000-beef"
CHECK = "Check that the path exists and that you can read it, then retry."
HINT = "Rerun with --debug to see the traceback."
RECOVERY_FAILED = [
    "",
    "ERROR: Recovery failed, so nothing was deployed.",
    'Reconcile the run named above by hand, then rerun with --dry-run. See "When recovery fails" in docs/recovery.md.',
    "",
]
RECONCILING = "Deployment failed; reconciling the current journal before exit..."
RETAINED = [
    "ERROR: Immediate recovery failed; deployment evidence and lock were retained.",
    "The next run reclaims the lock and retries recovery. If that fails too, reconcile the run by hand. "
    'See "When recovery fails" in docs/recovery.md.',
]


class Held:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def release(self) -> None:
        self.calls.append("release")


class ExecuteTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="execute.")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.paths = Paths(root / "source", root / "home")
        self.canary = root / "canary"
        self.stdin = io.StringIO()
        self.src = source.Source(SOURCE_ID)
        self.calls: list[str] = []

    def describe(self, value: Any) -> str:
        if isinstance(value, Paths):
            return f"paths({value.home.name})"
        if isinstance(value, Path):
            return value.name
        if isinstance(value, Options):
            return "options"
        if value is self.stdin:
            return "stdin"
        if value is sys.stdin:
            return "sys.stdin"
        if value is self.src:
            return "src"
        if value is ExecuteTests.probe:
            return "probe"
        if isinstance(value, argparse.Namespace):
            return "namespace"
        return repr(value)

    def step(self, name: str, outcome: Any) -> Callable[..., Any]:
        """A stand-in that records its call and returns outcome, raises it, or returns the next of a list."""
        outcomes = outcome if isinstance(outcome, list) else None

        def call(*arguments: Any) -> Any:
            self.calls.append(f"{name}({', '.join(self.describe(argument) for argument in arguments)})")
            result = outcomes.pop(0) if outcomes is not None else outcome
            if isinstance(result, BaseException):
                raise result
            return result

        return call

    @contextlib.contextmanager
    def replaced(self, options: Options, **outcomes: Any) -> Iterator[None]:
        held = Held(self.calls)
        steps = {
            (platform_support, "ensure_supported"): None,
            (source, "load_source_id"): SOURCE_ID,
            (pipeline, "parse_arguments"): options,
            (pipeline, "canary_home"): self.canary,
            (pipeline, "validate_managed_roots"): None,
            (config, "load"): {"VALUE": "configured"},
            (source, "discover"): self.src,
            (pipeline, "_print_source"): None,
            (pipeline, "_stop_for_pending_recovery"): False,
            (pipeline, "_deploy"): 0,
            (pipeline, "claim_canary_home"): None,
            (config, "canary"): {"VALUE": "canary"},
            (source, "reject_linked_worktree"): None,
            (lock, "acquire"): held,
            (pipeline, "_reject_other_checkout"): None,
            (journal, "recover_incomplete"): True,
            (pipeline, "print_traceback"): None,
            (errors, "print_traceback"): None,
        }
        with contextlib.ExitStack() as stack:
            for (module, name), default in steps.items():
                stack.enter_context(mock.patch.object(module, name, self.step(name, outcomes.get(name, default))))
            stack.enter_context(mock.patch.object(pipeline.secrets, "token_hex", return_value="beef"))
            stack.enter_context(mock.patch.object(pipeline.time, "gmtime", return_value=time.gmtime(0)))
            stack.enter_context(mock.patch.dict(os.environ, {"DEPLOYER_DEBUG": ""}))
            yield

    def execute(
        self, options: Options, debug: bool = False, stdin: io.StringIO | None = None, **outcomes: Any
    ) -> tuple[int, list[str], list[str]]:
        """The exit code, the calls in order, and stderr's lines."""
        output, error = io.StringIO(), io.StringIO()
        with self.replaced(options, **outcomes), contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = pipeline.execute(argparse.Namespace(debug=debug), self.paths, probe=self.probe, stdin=stdin)
        self.assertEqual("", output.getvalue())
        return code, self.calls, error.getvalue().split("\n")[:-1]

    @staticmethod
    def failing(name: str, failure: BaseException) -> dict[str, Any]:
        return {name: failure}

    @staticmethod
    def probe(pid: int) -> Any:
        raise AssertionError("the probe is only passed on")

    def prepared(self, home: str = "home") -> list[str]:
        """The calls of a run that prepares, discovers, and prints its source."""
        loaded = [] if home == "canary" else [f"load(1a979a102ce5.config, '{SOURCE_ID}', home, source)"]
        canary = ["canary_home('throwaway')"] if home == "canary" else []
        return [
            "ensure_supported()",
            "load_source_id(paths(home))",
            f"parse_arguments(namespace, '{SOURCE_ID}')",
            *canary,
            f"validate_managed_roots(paths({home}))",
            *loaded,
            f"discover(paths({home}), '{SOURCE_ID}')",
            f"_print_source(src, {home})",
        ]

    def deployed(self, values: str = "{'VALUE': 'configured'}", stdin: str = "stdin") -> list[str]:
        return [
            "recover_incomplete(paths(home))",
            f"_deploy(paths(home), options, src, {values}, {stdin}, '{RUN_ID}')",
        ]

    def held(self, take_over: bool = False) -> list[str]:
        return [
            "reject_linked_worktree(paths(home))",
            "acquire(paths(home), probe)",
            f"_reject_other_checkout(paths(home), '{SOURCE_ID}', {take_over})",
        ]

    def test_a_deployment_returns_what_it_deployed_with_and_releases_the_lock(self) -> None:
        self.assertEqual(
            (3, [*self.prepared(), *self.held(), *self.deployed(), "release"], []),
            self.execute(Options(), stdin=self.stdin, _deploy=3),
        )

    def test_without_stdin_the_deployment_reads_the_process_stdin(self) -> None:
        self.assertEqual(
            (0, [*self.prepared(), *self.held(), *self.deployed(stdin="sys.stdin"), "release"], []),
            self.execute(Options()),
        )

    def test_taking_over_the_source_is_passed_to_the_checkout_check(self) -> None:
        self.assertEqual(
            (0, [*self.prepared(), *self.held(take_over=True), *self.deployed(), "release"], []),
            self.execute(Options(take_over_source=True), stdin=self.stdin),
        )

    def test_a_canary_home_is_claimed_and_its_values_set_after_every_check(self) -> None:
        code, calls, lines = self.execute(Options(canary_home="throwaway"), stdin=self.stdin)
        self.assertEqual((0, []), (code, lines))
        self.assertEqual(
            [
                "ensure_supported()",
                "load_source_id(paths(home))",
                f"parse_arguments(namespace, '{SOURCE_ID}')",
                "canary_home('throwaway')",
                "validate_managed_roots(paths(canary))",
                f"discover(paths(canary), '{SOURCE_ID}')",
                "_print_source(src, canary)",
                "claim_canary_home(canary)",
                f"canary('{SOURCE_ID}', canary, source)",
                "acquire(paths(canary), probe)",
                f"_reject_other_checkout(paths(canary), '{SOURCE_ID}', False)",
                "recover_incomplete(paths(canary))",
                f"_deploy(paths(canary), options, src, {{'VALUE': 'canary'}}, stdin, '{RUN_ID}')",
                "release",
            ],
            calls,
        )

    def test_preparation_failures_end_the_run_before_anything_else(self) -> None:
        unreadable = OSError(2, "No such file or directory", "C:/x/source.json")
        cases: list[tuple[str, BaseException, int, list[str]]] = [
            ("ensure_supported", DeployError("ERROR: unsupported"), 1, ["", "ERROR: unsupported", ""]),
            (
                "load_source_id",
                DeployError("ERROR: no source", "Fix it.", exit_code=2),
                2,
                ["", "ERROR: no source", "Fix it.", ""],
            ),
            ("parse_arguments", KeyboardInterrupt(), 130, ["", "Cancelled; nothing was changed.", ""]),
            ("canary_home", DeployError("ERROR: not a canary"), 1, ["", "ERROR: not a canary", ""]),
            ("validate_managed_roots", DeployError("ERROR: roots"), 1, ["", "ERROR: roots", ""]),
            ("load", DeployError("ERROR: configuration"), 1, ["", "ERROR: configuration", ""]),
            (
                "discover",
                unreadable,
                1,
                [
                    "",
                    "ERROR: Could not prepare the deployment: C:/x/source.json: No such file or directory",
                    CHECK,
                    HINT,
                    "",
                ],
            ),
        ]
        for name, failure, code, lines in cases:
            self.calls = []
            options = Options(canary_home="throwaway") if name == "canary_home" else Options()
            ran = self.prepared("canary" if name == "canary_home" else "home")
            expected_calls = ran[: next(index for index, call in enumerate(ran) if call.startswith(f"{name}(")) + 1]
            with self.subTest(name=name):
                self.assertEqual(
                    (code, expected_calls, lines),
                    self.execute(options, stdin=self.stdin, **self.failing(name, failure)),
                )

    def test_a_dry_run_deploys_without_a_run_id_or_the_lock(self) -> None:
        dry = Options(dry_run=True)
        self.assertEqual(
            (
                4,
                [
                    *self.prepared(),
                    "_stop_for_pending_recovery(paths(home))",
                    "_deploy(paths(home), options, src, {'VALUE': 'configured'}, stdin, None)",
                ],
                [],
            ),
            self.execute(dry, stdin=self.stdin, _deploy=4),
        )

    def test_a_dry_run_stops_for_a_pending_recovery(self) -> None:
        self.assertEqual(
            (0, [*self.prepared(), "_stop_for_pending_recovery(paths(home))"], []),
            self.execute(Options(dry_run=True), stdin=self.stdin, _stop_for_pending_recovery=True),
        )

    def test_a_dry_run_that_fails_names_the_dry_run(self) -> None:
        stopped = [*self.prepared(), "_stop_for_pending_recovery(paths(home))"]
        deploy = "_deploy(paths(home), options, src, {'VALUE': 'configured'}, stdin, None)"
        cases: list[tuple[dict[str, Any], int, list[str], list[str]]] = [
            (
                {"_deploy": DeployError("ERROR: refused", exit_code=3)},
                3,
                [*stopped, deploy],
                ["", "ERROR: refused", ""],
            ),
            (
                {"_deploy": OSError(13, "Access is denied", "C:/x/y")},
                1,
                [*stopped, deploy],
                ["", "ERROR: Could not finish the dry run: C:/x/y: Access is denied", CHECK, HINT, ""],
            ),
            (
                {"_stop_for_pending_recovery": KeyboardInterrupt()},
                130,
                stopped,
                ["", "Cancelled; nothing was changed.", ""],
            ),
        ]
        for outcomes, code, calls, lines in cases:
            self.calls = []
            with self.subTest(outcomes=outcomes):
                self.assertEqual(
                    (code, calls, lines), self.execute(Options(dry_run=True), stdin=self.stdin, **outcomes)
                )

    def test_a_failure_claiming_the_run_ends_it_before_the_lock_is_held(self) -> None:
        cases: list[tuple[Options, str, BaseException, int, list[str], list[str]]] = [
            (
                Options(),
                "reject_linked_worktree",
                DeployError("ERROR: linked worktree"),
                1,
                ["reject_linked_worktree(paths(home))"],
                ["", "ERROR: linked worktree", ""],
            ),
            (
                Options(),
                "acquire",
                KeyboardInterrupt(),
                130,
                ["reject_linked_worktree(paths(home))", "acquire(paths(home), probe)"],
                ["", "Cancelled; nothing was changed.", ""],
            ),
            (
                Options(canary_home="throwaway"),
                "claim_canary_home",
                OSError(5, "Access is denied", "C:/x/canary"),
                1,
                ["claim_canary_home(canary)"],
                ["", "ERROR: Could not prepare the deployment: C:/x/canary: Access is denied", CHECK, HINT, ""],
            ),
            (
                Options(canary_home="throwaway"),
                "canary",
                DeployError("ERROR: canary values"),
                1,
                ["claim_canary_home(canary)", f"canary('{SOURCE_ID}', canary, source)"],
                ["", "ERROR: canary values", ""],
            ),
        ]
        for options, name, failure, code, claimed, lines in cases:
            self.calls = []
            home = "canary" if options.canary_home else "home"
            with self.subTest(name=name):
                self.assertEqual(
                    (code, [*self.prepared(home), *claimed], lines),
                    self.execute(options, stdin=self.stdin, **self.failing(name, failure)),
                )

    def test_another_checkout_releases_the_lock_with_its_own_exit_code(self) -> None:
        refused = DeployError("ERROR: another checkout", "Take it over.", exit_code=4)
        self.assertEqual(
            (4, [*self.prepared(), *self.held(), "release"], ["", "ERROR: another checkout", "Take it over.", ""]),
            self.execute(Options(), stdin=self.stdin, _reject_other_checkout=refused),
        )

    def test_a_failed_recovery_releases_the_lock_and_deploys_nothing(self) -> None:
        self.assertEqual(
            (1, [*self.prepared(), *self.held(), "recover_incomplete(paths(home))", "release"], RECOVERY_FAILED),
            self.execute(Options(), stdin=self.stdin, recover_incomplete=False),
        )

    def test_a_cancelled_selection_releases_the_lock_without_recovery(self) -> None:
        self.assertEqual(
            (
                130,
                [*self.prepared(), *self.held(), *self.deployed(), "release"],
                ["", "Cancelled; nothing was changed.", ""],
            ),
            self.execute(Options(), stdin=self.stdin, _deploy=Cancelled("Cancelled; nothing was changed.")),
        )
        self.calls = []
        self.assertEqual(
            (
                130,
                [
                    *self.prepared(),
                    *self.held(),
                    *self.deployed(),
                    "print_traceback(Cancelled('Cancelled.'))",
                    "release",
                ],
                ["", "Cancelled.", ""],
            ),
            self.execute(Options(), debug=True, stdin=self.stdin, _deploy=Cancelled("Cancelled.")),
        )

    def test_a_failed_deployment_is_recovered_before_the_lock_is_released(self) -> None:
        recovered = ["recover_incomplete(paths(home))", "release"]
        failure = DeployError("ERROR: apply failed", exit_code=3)
        boom = RuntimeError("boom")
        cases: list[tuple[BaseException, bool, int, list[str], list[str]]] = [
            (failure, False, 3, recovered, ["", "ERROR: apply failed", "", RECONCILING, ""]),
            (
                failure,
                True,
                3,
                ["print_traceback(DeployError('ERROR: apply failed'))", *recovered],
                ["", "ERROR: apply failed", "", RECONCILING, ""],
            ),
            (boom, False, 1, recovered, ["ERROR: Unexpected RuntimeError: boom", RECONCILING, ""]),
            (
                boom,
                True,
                1,
                ["print_traceback(RuntimeError('boom'))", *recovered],
                ["ERROR: Unexpected RuntimeError: boom", RECONCILING, ""],
            ),
            (KeyboardInterrupt(), False, 130, recovered, ["ERROR: Unexpected KeyboardInterrupt: ", RECONCILING, ""]),
            (
                OSError(5, "Access is denied"),
                False,
                1,
                recovered,
                ["ERROR: Unexpected OSError: [Errno 5] Access is denied", RECONCILING, ""],
            ),
        ]
        for failure_raised, debug, code, after, lines in cases:
            self.calls = []
            with self.subTest(failure=failure_raised, debug=debug):
                self.assertEqual(
                    (code, [*self.prepared(), *self.held(), *self.deployed(), *after], lines),
                    self.execute(Options(), debug=debug, stdin=self.stdin, _deploy=failure_raised),
                )

    def test_a_failed_immediate_recovery_keeps_the_lock(self) -> None:
        self.assertEqual(
            (
                3,
                [*self.prepared(), *self.held(), *self.deployed(), "recover_incomplete(paths(home))"],
                ["", "ERROR: apply failed", "", RECONCILING, *RETAINED, ""],
            ),
            self.execute(
                Options(),
                stdin=self.stdin,
                _deploy=DeployError("ERROR: apply failed", exit_code=3),
                recover_incomplete=[True, False],
            ),
        )


if __name__ == "__main__":
    unittest.main()
