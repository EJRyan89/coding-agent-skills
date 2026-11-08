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


# audit-skill reads the git command a wrapper, find's actions, eval, or a shell's -c runs through
# ejryan89-skill-core's command_wrappers, as validation's reader does, so the two agree on every wrapper row.
@pytest.mark.parametrize("script", WRAPPED_GIT_PUSH_SCRIPTS)
def test_audit_skill_reads_the_git_command_a_wrapper_runs_as_validation_does(script: str) -> None:
    assert skill_inventory.bash_git_subcommands(script) == skill_scripts.shell_git_subcommands(script) == {"push"}


# A wrapper that runs no git command, only looks git up, or takes an option its table does not settle gives no
# subcommand to either reader; find's second action is read apart from its first.
@pytest.mark.parametrize(
    ("script", "subcommands"),
    [
        ("command -v git push", set()),
        # command's -v or -V looks a program up only among command's own options, bundled or not; after the program
        # it is the program's own option.
        ("command -pv git push", set()),
        ("command -Vp git push", set()),
        ("command git push -v", {"push"}),
        ("command -p git fetch -v", {"fetch"}),
        # `||` is no pipe, so `time` after it, or on the line after one that ends in it, is Bash's keyword, whose
        # second -p is the timed command's word; after a pipe it is the time program.
        ("a || time -p -p git push", set()),
        ("a ||\ntime -p -p git push", set()),
        ("a |\ntime -p -p git push", {"push"}),
        ("xargs echo git push", set()),
        ("timeout --sig TERM 5 git push", set()),
        ("find . -exec git status {} \\; -exec echo push \\;", {"status"}),
        ("find . -exec echo {} + -exec git push {} +", {"push"}),
    ],
)
def test_audit_skill_reads_only_the_command_a_wrapper_runs_as_validation_does(
    script: str, subcommands: set[str]
) -> None:
    assert skill_inventory.bash_git_subcommands(script) == skill_scripts.shell_git_subcommands(script) == subcommands


def test_a_skill_that_invokes_another_needs_what_the_invoked_skill_does() -> None:
    # update-pr-dashboard declares only gh; the reviews it starts through review-prs need the rest.
    assert {"agent-delegation", "workflow", "fail-closed-agent-hooks"} <= printed_needs("update-pr-dashboard")
