"""Grant and description policies: allowed-tools grants, descriptions a runtime adapter accepts, and the
.agents/skills shims of repository skills.
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

from skill_layout import embedded_program_problems, output_placeholder_problems
from validation_support import REPOSITORY_ROOT, REPOSITORY_SKILLS, SKILLS_ROOT, skills_by_name

from deployer import render

GRANTS_DOC = '"Granting tools" in docs/adding-a-skill.md'
# A fence command that runs the skill's own scripts, which its grants must cover: a shipped skill names them through
# its directory, and a repository skill runs the repository's tools and tests from the working tree.
OWN_SCRIPT_COMMAND = re.compile(r"\$\{CLAUDE_SKILL_DIR\}")
REPOSITORY_TOOL_COMMAND = re.compile(r"^python (?:-B )?(?:tools|tests)/")
# The modules through which a skill script starts a process: directly, or through skill-core's bounded runner.
PROCESS_MODULES = frozenset({"subprocess", "bounded_process"})


def shipped_skill_files(root: Path) -> list[Path]:
    """Each shipped skill's SKILL.md, a skill in a category included: a skill directory with deploy-meta."""
    skills = skills_by_name(root)
    found = (skills[metadata.stem] for metadata in (root / "deploy-meta").glob("*.json") if metadata.stem in skills)
    return sorted((directory / "SKILL.md" for directory in found), key=lambda path: path.parent.name)


def repository_skill_files(root: Path) -> list[Path]:
    """Each repository skill's SKILL.md under .claude/skills, which the shipped-skill policies hold too."""
    return sorted((root / REPOSITORY_SKILLS).glob("*/SKILL.md"), key=lambda path: path.parent.name)


def skill_grant_problems(
    root: Path, skill_files: list[Path] | None = None, own: re.Pattern[str] = OWN_SCRIPT_COMMAND
) -> list[str]:
    """Report shell grants that cover every command or one shell only, grants no step uses, own-script commands
    left ungranted, and grants that pre-approve a script that starts code from the target repository.

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
            elif kind == "GRANTED_REPOSITORY_CODE":
                tool, number, command = detail.split(" ", 2)
                problems.append(
                    f"{name}:{number} a {tool} grant pre-approves {command}, which runs code from the target "
                    f"repository; see {GRANTS_DOC}"
                )
            elif kind == "EXPANDS":
                number, command = detail.split(" ", 1)
                problems.append(
                    f"{name}:{number} expands a shell variable, so it always prompts: {command}; see {GRANTS_DOC}"
                )
    return problems


def repository_code_declaration_problems(root: Path) -> list[str]:
    """Report each RUNS_REPOSITORY_CODE declaration the inventory would not read, and each in a script that starts
    no process.

    The inventory leaves a command ungranted only for a script whose module-level declaration is a non-empty string,
    so a declaration of another shape would fail silently, and one in a script that starts nothing is stale.
    """
    sys.path.insert(0, str(SKILLS_ROOT / "analyze-skill-cost" / "scripts"))
    import skill_inventory

    declaration = skill_inventory.REPOSITORY_CODE_DECLARATION
    problems: list[str] = []
    for script in sorted([*(root / "skills").rglob("*.py"), *(root / REPOSITORY_SKILLS).rglob("*.py")]):
        tree = ast.parse(script.read_text(encoding="utf-8"))
        assigned = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign | ast.AnnAssign)
            and any(
                isinstance(target, ast.Name) and target.id == declaration
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            )
        ]
        if not assigned:
            continue
        name = script.relative_to(root).as_posix()
        if len(assigned) != 1 or assigned[0] not in tree.body or not skill_inventory.runs_repository_code(script):
            problems.append(f"{name}: {declaration} must be one module-level assignment of a non-empty reason")
        imports = {
            alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
        } | {(node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        if not imports & PROCESS_MODULES:
            problems.append(f"{name}: declares {declaration} but starts no process; remove the declaration")
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

    def test_runtime_prompt_samples_match_what_the_skills_render(self) -> None:
        # analyze-skill-cost measures the prompt a skill's script writes for a subagent from its rendered sample, so a
        # prompt change that does not rerun tools/runtime_prompts.py --write has the audit measure a prompt no run sees.
        from tools import runtime_prompts

        self.assertEqual([], runtime_prompts.problems(REPOSITORY_ROOT))

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

    def test_repository_code_declarations_are_read_and_current(self) -> None:
        # A script that starts code the target repository configures declares it, so the grant policy above can
        # hold its command ungranted; a declaration the inventory cannot read would let a grant cover it unnoticed.
        self.assertEqual([], repository_code_declaration_problems(REPOSITORY_ROOT))
