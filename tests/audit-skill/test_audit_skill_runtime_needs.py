"""audit-skill's runtime needs, held to repository validation's for every shipped skill.

Both derive a skill's needs through ejryan89-skill-core's runtime_support, each reading the skill's own grants,
metadata, agents, and scripts and the skills it invokes. The RUNTIME_NEEDS lines audit-skill prints for a skill in this
source tree name exactly the capabilities the runtime-support policy holds its declaration to, so neither reader can
drift from the other unseen.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("audit-skill")))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "validation"))

import skill_inventory
from skill_layout import derived_needs

REPOSITORY = Path(__file__).resolve().parents[2]
SKILLS = sorted(path.parent.name for path in (REPOSITORY / "skills").glob("*/SKILL.md"))


def printed_needs(name: str) -> set[str]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = skill_inventory.main(["tools", str(REPOSITORY / "skills" / name / "SKILL.md")])
    assert code == 0, output.getvalue()
    return {line.split()[1] for line in output.getvalue().splitlines() if line.startswith("RUNTIME_NEEDS ")}


@pytest.mark.parametrize("name", SKILLS)
def test_audit_skill_derives_the_needs_validation_holds_each_skill_to(name: str) -> None:
    assert printed_needs(name) == derived_needs(REPOSITORY, name)


def test_a_skill_that_invokes_another_needs_what_the_invoked_skill_does() -> None:
    # update-pr-dashboard declares only gh; the reviews it starts through review-prs need the rest.
    assert {"agent-delegation", "workflow", "fail-closed-agent-hooks"} <= printed_needs("update-pr-dashboard")
