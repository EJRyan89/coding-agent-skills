"""Regression tests for the UTF-8 console setup every skill entry point calls."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import unittest
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


if __name__ == "__main__":
    unittest.main()
