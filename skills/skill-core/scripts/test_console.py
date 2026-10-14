"""Regression tests for the UTF-8 console setup and the runner every skill entry point calls."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import subprocess
import sys
import unittest
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import console

SCRIPT_DIRECTORY = Path(__file__).resolve().parent


def child(body: str) -> subprocess.CompletedProcess[bytes]:
    """Run `body` in a Python whose console is a Windows pipe's legacy code page."""
    program = f"import sys\nsys.path.insert(0, {str(SCRIPT_DIRECTORY)!r})\nimport console\n{body}"
    return subprocess.run(
        [sys.executable, "-B", "-c", program],
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        check=False,
    )


class Utf8OutputTests(unittest.TestCase):
    def test_a_legacy_code_page_cannot_print_the_fixture_text_without_it(self) -> None:
        result = child('print("→ ✓")')
        self.assertNotEqual(0, result.returncode)
        self.assertIn(b"UnicodeEncodeError", result.stderr)

    def test_both_streams_print_utf8(self) -> None:
        result = child('console.use_utf8_output()\nprint("→ ✓")\nprint("→ ✓", file=sys.stderr)')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("→ ✓\n".encode(), result.stdout.replace(b"\r\n", b"\n"))
        self.assertEqual("→ ✓\n".encode(), result.stderr.replace(b"\r\n", b"\n"))

    def test_stdout_is_strict_by_default_and_replaces_when_asked(self) -> None:
        strict = child(
            "console.use_utf8_output()\ntry:\n    print('\\udcff')\nexcept UnicodeEncodeError:\n    sys.exit(3)"
        )
        self.assertEqual(3, strict.returncode, strict.stderr)
        replaced = child("console.use_utf8_output(errors='replace')\nsys.stdout.write('\\udcff')")
        self.assertEqual((0, b"?"), (replaced.returncode, replaced.stdout), replaced.stderr)

    def test_stderr_keeps_escaping_what_utf8_cannot_encode(self) -> None:
        result = child("console.use_utf8_output()\nsys.stderr.write('\\udcff')")
        self.assertEqual((0, b"\\udcff"), (result.returncode, result.stderr))

    def test_newline_applies_to_both_streams_only_when_given(self) -> None:
        result = child("console.use_utf8_output(newline='\\n')\nprint('a')\nprint('b', file=sys.stderr)")
        self.assertEqual((0, b"a\n", b"b\n"), (result.returncode, result.stdout, result.stderr))
        native = child("console.use_utf8_output()\nprint('a')")
        self.assertEqual(os.linesep.encode(), native.stdout[1:])

    def test_a_stream_that_is_not_a_text_wrapper_is_left_alone(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            console.use_utf8_output()
            print("→ ✓")
            self.assertIs(output, sys.stdout)
            self.assertIs(errors, sys.stderr)
        self.assertEqual("→ ✓\n", output.getvalue())


def run_in_process(main: Callable[[], int | None]) -> tuple[object, str, str]:
    """Run `main` through the runner with captured streams: the exit code, stdout, and stderr."""
    output, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        try:
            console.run_main(main)
        except SystemExit as exited:
            return exited.code, output.getvalue(), errors.getvalue()
    raise AssertionError("run_main returned instead of exiting")


def raises(error: BaseException) -> Callable[[], int]:
    def main() -> int:
        raise error

    return main


class RunMainTests(unittest.TestCase):
    def test_exits_with_what_main_returns(self) -> None:
        self.assertEqual((0, "done\n", ""), run_in_process(lambda: print("done") or 0))
        self.assertEqual((1, "", ""), run_in_process(lambda: 1))
        self.assertEqual((None, "", ""), run_in_process(lambda: None))

    def test_a_system_exit_passes_through_with_its_code(self) -> None:
        self.assertEqual((2, "", ""), run_in_process(raises(SystemExit(2))))
        self.assertEqual(("stop", "", ""), run_in_process(raises(SystemExit("stop"))))

    def test_an_argparse_usage_error_keeps_exit_2_and_its_usage_on_stderr(self) -> None:
        parser = argparse.ArgumentParser(prog="tool.py")
        parser.add_argument("--count", type=int)

        def main() -> int:
            parser.parse_args(["--count", "x"])
            return 0

        code, output, errors = run_in_process(main)
        self.assertEqual((2, ""), (code, output))
        self.assertIn("tool.py: error: argument --count: invalid int value: 'x'", errors)

    def test_an_unhandled_exception_prints_one_failed_line_and_exits_1(self) -> None:
        self.assertEqual(
            (1, "FAILED LookupError: no such pull request\n", ""),
            run_in_process(raises(LookupError("no such pull request"))),
        )

    def test_the_failed_line_flattens_the_message_onto_one_line(self) -> None:
        self.assertEqual(
            (1, "FAILED ValueError: first line second line\n", ""),
            run_in_process(raises(ValueError("  first line\n\n  second\tline \n"))),
        )

    def test_an_exception_with_no_message_names_its_type(self) -> None:
        self.assertEqual((1, "FAILED RuntimeError\n", ""), run_in_process(raises(RuntimeError())))
        self.assertEqual((1, "FAILED KeyError: 'title'\n", ""), run_in_process(raises(KeyError("title"))))

    def test_output_printed_before_the_failure_stays_ahead_of_the_failed_line(self) -> None:
        def main() -> int:
            print("COLLECTED 3")
            raise OSError("disk full")

        self.assertEqual((1, "COLLECTED 3\nFAILED OSError: disk full\n", ""), run_in_process(main))

    def test_ctrl_c_prints_failed_interrupted_and_exits_130(self) -> None:
        self.assertEqual((130, "FAILED interrupted\n", ""), run_in_process(raises(KeyboardInterrupt())))

    def test_a_program_prints_no_traceback_and_exits_with_the_contract_codes(self) -> None:
        for body, code, line in (
            ("raise LookupError('gone → away')", 1, "FAILED LookupError: gone → away\n"),
            ("raise KeyboardInterrupt", 130, "FAILED interrupted\n"),
            ("return 0", 0, ""),
        ):
            with self.subTest(body=body):
                result = child(f"def main():\n    {body}\nconsole.run_main(main)")
                self.assertEqual(code, result.returncode, result.stderr)
                self.assertEqual(line.encode(), result.stdout.replace(b"\r\n", b"\n"))
                self.assertEqual(b"", result.stderr)

    def test_a_requested_traceback_follows_the_line_on_stderr_with_the_same_exit_code(self) -> None:
        for body, code, line, last in (
            ("raise LookupError('gone')", 1, "FAILED LookupError: gone\n", "LookupError: gone"),
            ("raise KeyboardInterrupt", 130, "FAILED interrupted\n", "KeyboardInterrupt"),
        ):
            with self.subTest(body=body):
                result = child(f"def main():\n    {body}\nconsole.run_main(main, traceback_requested=lambda: True)")
                self.assertEqual(code, result.returncode, result.stderr)
                self.assertEqual(line.encode(), result.stdout.replace(b"\r\n", b"\n"))
                errors = result.stderr.replace(b"\r\n", b"\n").decode()
                self.assertTrue(errors.startswith("Traceback (most recent call last):\n"), errors)
                self.assertIn("in main", errors)
                self.assertTrue(errors.endswith(f"{last}\n"), errors)

    def test_a_traceback_is_shown_only_when_asked_and_its_question_answers(self) -> None:
        def unanswerable() -> bool:
            raise ImportError("no deployer")

        for requested in (None, lambda: False, unanswerable):
            with self.subTest(requested=requested):
                output, errors = io.StringIO(), io.StringIO()
                with (
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(errors),
                    self.assertRaises(SystemExit) as exited,
                ):
                    console.run_main(raises(ValueError("boom")), traceback_requested=requested)
                self.assertEqual(
                    (1, "FAILED ValueError: boom\n", ""), (exited.exception.code, output.getvalue(), errors.getvalue())
                )

    def test_success_and_a_system_exit_never_ask_for_a_traceback(self) -> None:
        asked: list[bool] = []

        def requested() -> bool:
            asked.append(True)
            return True

        for main in (lambda: 0, raises(SystemExit(2))):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                console.run_main(main, traceback_requested=requested)
        self.assertEqual([], asked)

    def test_the_runner_sets_up_utf8_output_first_with_its_options(self) -> None:
        result = child(
            "def main():\n    print('→ ✓')\n    sys.stdout.write('\\udcff')\n"
            "console.run_main(main, errors='replace', newline='\\n')"
        )
        self.assertEqual((0, "→ ✓\n?".encode()), (result.returncode, result.stdout), result.stderr)

    def test_a_message_utf8_cannot_encode_prints_escaped_on_a_strict_stream(self) -> None:
        result = child("def main():\n    raise OSError('bad \\udcff path')\nconsole.run_main(main)")
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertEqual(b"FAILED OSError: bad \\udcff path", result.stdout.rstrip())
        self.assertEqual(b"", result.stderr)


if __name__ == "__main__":
    unittest.main()
