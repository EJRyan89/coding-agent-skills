"""Regression tests for the mark of a line a passing suite writes for the validation runner to keep."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from validation_notes import NOTE_MARKER, note_line


class NoteLineTests(unittest.TestCase):
    def test_the_marker_is_pinned(self) -> None:
        # A pinned contract value: tests/validation/job_pool.py keeps what follows it from a passing suite's output.
        self.assertEqual("VALIDATION NOTE: ", NOTE_MARKER)

    def test_a_note_is_one_line_after_the_marker(self) -> None:
        self.assertEqual(f"{NOTE_MARKER}a volume stays attached: busy", note_line("a volume stays attached: busy"))
        self.assertEqual(
            f"{NOTE_MARKER}exited with status 16: couldn't eject Resource busy",
            note_line(" exited with status 16: couldn't eject\n  Resource busy\x1b\r\n"),
        )
