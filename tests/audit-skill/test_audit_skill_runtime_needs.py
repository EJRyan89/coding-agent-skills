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
import skill_scripts
from skill_layout import derived_needs
from test_shell_readers import WRAPPED_GIT_PUSH_SCRIPTS

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


# The wrapper rows audit-skill's GIT_COMMAND pattern reads as validation's reader does. It finds git only where a
# command starts or after `command`, `exec`, or find's `-exec`, so every other row of WRAPPED_GIT_PUSH_SCRIPTS is a
# limit of audit-skill's reader until it reads through the same wrapper catalogue. Its pattern takes Bash's time
# keyword as a command start but not the keyword's -p or --, so TIME_KEYWORD_LIMITS are among those limits.
AUDIT_SKILL_READS = frozenset(
    {
        "find . -exec git push {} +",
        "find . -exec git push \\;",
        "find . -exec git push",
        "command git push",
        "exec git push",
    }
)
TIME_KEYWORD_LIMITS = frozenset({"time -p git push", "time -p -- git push", "! time -p git push"})


@pytest.mark.parametrize("script", WRAPPED_GIT_PUSH_SCRIPTS)
def test_audit_skill_reads_the_git_command_a_wrapper_runs_as_validation_does_or_names_the_limit(script: str) -> None:
    assert skill_scripts.shell_git_subcommands(script) == {"push"}
    # A limit row that audit-skill comes to read fails here, so the limit leaves AUDIT_SKILL_READS's complement.
    expected = {"push"} if script in AUDIT_SKILL_READS else set()
    assert skill_inventory.bash_git_subcommands(script) == expected


def test_audit_skill_reads_only_wrapper_rows_validation_holds() -> None:
    assert set(WRAPPED_GIT_PUSH_SCRIPTS) >= AUDIT_SKILL_READS


def test_the_time_keyword_rows_are_rows_validation_holds_and_limits_of_audit_skill() -> None:
    assert set(WRAPPED_GIT_PUSH_SCRIPTS) >= TIME_KEYWORD_LIMITS
    assert not TIME_KEYWORD_LIMITS & AUDIT_SKILL_READS


def test_a_skill_that_invokes_another_needs_what_the_invoked_skill_does() -> None:
    # update-pr-dashboard declares only gh; the reviews it starts through review-prs need the rest.
    assert {"agent-delegation", "workflow", "fail-closed-agent-hooks"} <= printed_needs("update-pr-dashboard")
