"""audit-skill's runtime needs, held to repository validation's for every shipped skill and for fixture skills.

Both derive a skill's needs through ejryan89-skill-core's runtime_support, each reading the skill's own grants,
metadata, agents, and scripts and the skills it invokes, and both read a Bash or PowerShell script's git subcommands
through ejryan89-skill-core's one reader, git_subcommands, each listing a PowerShell script's commands with
powershell_commands's parser script. The RUNTIME_NEEDS lines audit-skill prints for a skill in this source tree name
exactly the capabilities the runtime-support policy holds its declaration to, so neither derivation can drift from the
other unseen.
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
import skill_layout
from skill_layout import derived_needs
from test_shell_readers import POWERSHELL_WRAPPED_GIT_PUSH_SOURCES, WRAPPED_GIT_PUSH_SCRIPTS

REPOSITORY = Path(__file__).resolve().parents[2]
SKILLS = sorted(path.parent.name for path in (REPOSITORY / "skills").glob("*/SKILL.md"))


def printed_needs(skill_md: Path) -> set[str]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = skill_inventory.main(["tools", str(skill_md)])
    assert code == 0, output.getvalue()
    return {line.split()[1] for line in output.getvalue().splitlines() if line.startswith("RUNTIME_NEEDS ")}


@pytest.mark.parametrize("name", SKILLS)
def test_audit_skill_derives_the_needs_validation_holds_each_skill_to(name: str) -> None:
    assert printed_needs(REPOSITORY / "skills" / name / "SKILL.md") == derived_needs(REPOSITORY, name)


def fixture_skill(root: Path, scripts: dict[str, str]) -> Path:
    """A source tree under `root` holding one skill, alpha, with these scripts, by file name, and full support on
    every runtime; the SKILL.md it returns."""
    skill = root / "skills" / "alpha"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: alpha\ndescription: Runs.\n---\nBody.\n", encoding="utf-8")
    for name, text in scripts.items():
        (skill / "scripts" / name).write_text(text, encoding="utf-8")
    (root / "deploy-meta").mkdir()
    (root / "deploy-meta" / "alpha.json").write_text(
        '{"runtime_support": {"claude-code": "full", "codex": "full", "copilot-cli": "full"}}\n', encoding="utf-8"
    )
    return skill / "SKILL.md"


def both_needs(root: Path, scripts: dict[str, str]) -> tuple[set[str], set[str]]:
    """The needs audit-skill prints for the fixture skill and the needs validation holds it to."""
    skill_md = fixture_skill(root, scripts)
    return printed_needs(skill_md), derived_needs(root, "alpha")


# The issue's forms: a git push in a Bash or PowerShell script, run directly, by Start-Process, behind a wrapper, or in
# the script a shell or PowerShell start-up runs, needs network access in both derivations; one in a regression suite
# or a comment needs none.
@pytest.mark.parametrize(
    ("name", "text", "needs"),
    [
        ("update.ps1", "git push origin\n", {"sandbox-network"}),
        ("update.ps1", "Start-Process git -ArgumentList 'push', 'origin'\n", {"sandbox-network"}),
        ("update.ps1", "& git.exe -C $root fetch\n", {"sandbox-network"}),
        ("update.ps1", "pwsh -NoProfile -Command 'git pull'\n", {"sandbox-network"}),
        ("update.ps1", "bash -c 'git ls-remote origin'\n", {"sandbox-network"}),
        ("update.ps1", "git status\n# git push\n", set()),
        ("update.PS1", "git clone $url\n", {"sandbox-network"}),
        ("test_update.ps1", "git push\n", set()),
        ("update.sh", "git push origin\n", {"sandbox-network"}),
        ("update.sh", "pwsh -c 'git push'\n", {"sandbox-network"}),
        ("update.sh", "git status\n", set()),
    ],
)
def test_a_git_subcommand_a_script_runs_needs_the_same_capabilities_in_both(
    tmp_path: Path, name: str, text: str, needs: set[str]
) -> None:
    assert both_needs(tmp_path, {name: text}) == (needs, needs)


# A subcommand no literal names may be any, one that reaches a remote included, so both hold the script to network
# access: Bash's word, and PowerShell's word that is no literal.
@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("update.sh", 'git "$sub" origin\n'),
        ("update.sh", "pwsh -c 'git $sub'\n"),
        ("update.ps1", "git $sub origin\n"),
        ("update.ps1", "Start-Process git -ArgumentList $arguments\n"),
    ],
)
def test_a_git_subcommand_no_literal_names_needs_network_access_in_both(tmp_path: Path, name: str, text: str) -> None:
    assert both_needs(tmp_path, {name: text}) == ({"sandbox-network"}, {"sandbox-network"})


# Every wrapper form both readers' suites hold, as a fixture script, gives both derivations the same needs.
@pytest.mark.parametrize(
    ("name", "text"),
    [
        *(("update.sh", script) for script in WRAPPED_GIT_PUSH_SCRIPTS),
        *(("update.ps1", source) for source in POWERSHELL_WRAPPED_GIT_PUSH_SOURCES),
    ],
)
def test_a_wrapped_git_push_needs_network_access_in_both(tmp_path: Path, name: str, text: str) -> None:
    assert both_needs(tmp_path, {name: text + "\n"}) == ({"sandbox-network"}, {"sandbox-network"})


def test_validation_and_audit_skill_read_a_script_through_one_reader(tmp_path: Path) -> None:
    script = fixture_skill(tmp_path, {"update.ps1": "env git push\n"}).parent / "scripts" / "update.ps1"
    assert skill_layout.script_git_subcommands(script) == skill_inventory.script_git_subcommands(script) == {"push"}


def test_a_skill_that_invokes_another_needs_what_the_invoked_skill_does() -> None:
    # update-pr-dashboard declares only gh; the reviews it starts through review-prs need the rest.
    assert {"agent-delegation", "workflow", "fail-closed-agent-hooks"} <= printed_needs(
        REPOSITORY / "skills" / "update-pr-dashboard" / "SKILL.md"
    )
