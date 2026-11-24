"""The one mark of a line a passing suite writes for the repository's validation runner to keep.

The runner drops a passing suite's output, so a report that is not the suite's verdict, such as a test volume a
teardown could not detach, would reach nobody. A suite prints it as `note_line` gives it instead: the runner keeps what
follows NOTE_MARKER on each line of a passing suite's output, prints it under the suite's name, and lists it in the CI
step summary, while the suite's verdict stays its own checks'. The marker need not start its line, since pytest writes a
test's progress before what the test prints, on the same line.
"""

from __future__ import annotations

from flat_text import flat_text

NOTE_MARKER = "VALIDATION NOTE: "


def note_line(text: str) -> str:
    """The line that carries `text` to the validation runner as a note: NOTE_MARKER, then `text` on one line, its
    control characters, line breaks, and runs of whitespace each made one space."""
    return f"{NOTE_MARKER}{flat_text(text)}"
