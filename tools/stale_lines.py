"""The one report of a generated file out of step with its source, for the tools that regenerate one.

tools/skill_reference.py, tools/skill_shims.py, and tools/runtime_prompts.py each check that what they write is
current, and print each problem they find on a `STALE <problem>` line through `report`, so a contributor and the
change-skill procedure read one form whichever of them ran. Each tool's own text says what the problem names: a part
of docs/skills.md, a shim file, or a prompt sample.
"""

from __future__ import annotations

from collections.abc import Sequence

# The word that opens each line, which change-skill's SKILL.md names.
STALE = "STALE"


def report(found: Sequence[str]) -> int:
    """Print each problem on a `STALE <problem>` line, in order, and return the tool's exit status: 1 when it found
    any, 0 when everything it writes is current."""
    for problem in found:
        print(f"{STALE} {problem}")
    return 1 if found else 0
