"""Grant and description policies: allowed-tools grants, descriptions a runtime adapter accepts, and the
.agents/skills shims of repository skills.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

from skill_layout import embedded_program_problems, output_placeholder_problems
from validation_support import REPOSITORY_ROOT, REPOSITORY_SKILLS, SKILLS_ROOT

from deployer import render

GRANTS_DOC = '"Granting tools" in docs/adding-a-skill.md'
# A fence command that runs the skill's own scripts, which its grants must cover: a shipped skill names them through
# its directory, and a repository skill runs the repository's tools and tests from the working tree.
OWN_SCRIPT_COMMAND = re.compile(r"\$\{CLAUDE_SKILL_DIR\}")
REPOSITORY_TOOL_COMMAND = re.compile(r"^python (?:-B )?(?:tools|tests)/")


def shipped_skill_files(root: Path) -> list[Path]:
    """Each shipped skill's SKILL.md: a skills/ folder with deploy-meta, which a shared asset lacks."""
    found = (root / "skills" / metadata.stem / "SKILL.md" for metadata in (root / "deploy-meta").glob("*.json"))
    return sorted((path for path in found if path.is_file()), key=lambda path: path.parent.name)


def repository_skill_files(root: Path) -> list[Path]:
    """Each repository skill's SKILL.md under .claude/skills, which the shipped-skill policies hold too."""
    return sorted((root / REPOSITORY_SKILLS).glob("*/SKILL.md"), key=lambda path: path.parent.name)


def skill_grant_problems(
    root: Path, skill_files: list[Path] | None = None, own: re.Pattern[str] = OWN_SCRIPT_COMMAND
) -> list[str]:
    """Report shell grants that cover every command or one shell only, grants no step uses, and own-script commands
    left ungranted.

    The rules are the analyze-skill-cost inventory's, so the audit and this policy cannot disagree. A shipped skill's
    own scripts run through ${CLAUDE_SKILL_DIR}; a repository skill's are the repository's tools and tests.
    """
    sys.path.insert(0, str(SKILLS_ROOT / "analyze-skill-cost" / "scripts"))
    import skill_inventory

    problems: list[str] = []
    for skill_md in shipped_skill_files(root) if skill_files is None else skill_files:
        name = skill_md.relative_to(root).as_posix()
        for line in skill_inventory.tools(skill_md):
            kind, _, detail = line.partition(" ")
            if kind == "UNSCOPED_ALLOWED":
                problems.append(f"{name} grants {detail} for every command; see {GRANTS_DOC}")
            elif kind == "UNPAIRED_ALLOWED":
                problems.append(f"{name} grants {detail} in one shell only; see {GRANTS_DOC}")
            elif kind == "UNUSED_ALLOWED":
                problems.append(f"{name} grants {detail}, which no step uses; see {GRANTS_DOC}")
            elif kind == "MISSING_ALLOWED" and detail.split(" ")[0] == "Bash":
                problems.append(f"{name}:{detail.split(' ')[1]} runs a command without a shell grant; see {GRANTS_DOC}")
            elif kind == "UNGRANTED" and own.search(detail.split(" ", 2)[2]):
                tool, number, command = detail.split(" ", 2)
                problems.append(f"{name}:{number} no {tool} grant covers {command}; see {GRANTS_DOC}")
            elif kind == "EXPANDS":
                number, command = detail.split(" ", 1)
                problems.append(
                    f"{name}:{number} expands a shell variable, so it always prompts: {command}; see {GRANTS_DOC}"
                )
    return problems


def repository_skill_problems(root: Path) -> list[str]:
    """Report each repository skill under .claude/skills without a matching .agents/skills shim, and each stray shim.

    Codex and Copilot CLI read .agents/skills, so a shim must carry the skill's own frontmatter, which is all they
    see before choosing it, and point to the skill as the authoritative workflow.
    """

    def frontmatter(path: Path) -> str:
        parts = path.read_text(encoding="utf-8").replace("\r\n", "\n").split("---\n", 2)
        return parts[1] if len(parts) == 3 and parts[0] == "" else ""

    skills = {path.parent.name: path for path in (root / ".claude" / "skills").glob("*/SKILL.md")}
    shims = {path.parent.name: path for path in (root / ".agents" / "skills").glob("*/SKILL.md")}
    found = [
        f".agents/skills/{name}/SKILL.md has no .claude/skills/{name}/SKILL.md"
        for name in sorted(set(shims) - set(skills))
    ]
    for name in sorted(skills):
        shim = shims.get(name)
        if shim is None:
            found.append(f".claude/skills/{name}/SKILL.md has no .agents/skills/{name}/SKILL.md shim")
            continue
        if not frontmatter(skills[name]) or frontmatter(shim) != frontmatter(skills[name]):
            found.append(f".agents/skills/{name}/SKILL.md frontmatter differs from .claude/skills/{name}/SKILL.md")
        if f"`../../../.claude/skills/{name}/SKILL.md`" not in shim.read_text(encoding="utf-8"):
            found.append(f".agents/skills/{name}/SKILL.md does not point to ../../../.claude/skills/{name}/SKILL.md")
    return found


def description_problems(root: Path, skill_files: list[Path]) -> list[str]:
    """Report descriptions a runtime adapter would refuse, and model-invocable ones that never say when to start.

    The rules are the renderer's and the skill reference's, which hold every shipped skill as it deploys.
    """
    from deployer.errors import DeployError
    from tools import skill_reference

    problems: list[str] = []
    for skill_md in skill_files:
        name = skill_md.relative_to(root).as_posix()
        try:
            description, user_only = render._adapter_frontmatter(skill_md.parent.name, skill_md.read_bytes())
        except DeployError as exc:
            problems.append(f"{name}: {exc.lines[0].removeprefix('ERROR: ')}")
            continue
        if not user_only and not skill_reference.says_when(description):
            problems.append(f"{name}: the model may start it, so its description must {skill_reference.WHEN_RULE}")
    return problems


class SkillGrantsPolicies(unittest.TestCase):
    def test_skill_reference_matches_the_skills(self) -> None:
        # docs/skills.md is generated from each skill's frontmatter and metadata, so a skill change that does not
        # rerun tools/skill_reference.py --write leaves the reference describing a skill that no longer exists.
        from tools import skill_reference

        self.assertEqual([], skill_reference.problems(REPOSITORY_ROOT))

    def test_repository_skills_have_matching_shims(self) -> None:
        self.assertEqual([], repository_skill_problems(REPOSITORY_ROOT))

    def test_repository_skills_meet_the_shipped_skill_policies(self) -> None:
        # A repository skill loads into every session here as a shipped one does, so the same rules hold it.
        skills = repository_skill_files(REPOSITORY_ROOT)
        self.assertTrue(skills)
        self.assertEqual([], embedded_program_problems(REPOSITORY_ROOT, REPOSITORY_SKILLS))
        self.assertEqual([], output_placeholder_problems(REPOSITORY_ROOT, REPOSITORY_SKILLS))
        self.assertEqual([], skill_grant_problems(REPOSITORY_ROOT, skills, REPOSITORY_TOOL_COMMAND))
        self.assertEqual([], description_problems(REPOSITORY_ROOT, skills))

    def test_skills_grant_only_scoped_twin_shell_patterns_that_cover_their_own_scripts(self) -> None:
        # allowed-tools pre-approves; a bare Bash would pre-approve every command in the turn that starts the skill.
        self.assertEqual([], skill_grant_problems(REPOSITORY_ROOT))
