"""Fixture tests for tests/validation/skill_grants.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skill_grants import (
    GRANTS_DOC,
    REPOSITORY_TOOL_COMMAND,
    description_problems,
    repository_skill_files,
    repository_skill_problems,
    skill_grant_problems,
)
from validation_support import write_fixture_tree


class SkillGrantsFixtures(unittest.TestCase):
    def test_repository_skill_shim_check_detects_each_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def write(relative: str, text: str) -> None:
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_text(text, encoding="utf-8")

            pointer = "Read and follow `../../../.claude/skills/{}/SKILL.md`.\n"
            write(".claude/skills/good/SKILL.md", "---\nname: good\ndescription: Good.\n---\n\nBody.\n")
            write(
                ".agents/skills/good/SKILL.md", "---\nname: good\ndescription: Good.\n---\n\n" + pointer.format("good")
            )
            write(".claude/skills/missing/SKILL.md", "---\nname: missing\ndescription: Missing.\n---\n")
            write(".claude/skills/drifted/SKILL.md", "---\nname: drifted\ndescription: New.\n---\n")
            write(
                ".agents/skills/drifted/SKILL.md",
                "---\nname: drifted\ndescription: Old.\n---\n\n" + pointer.format("drifted"),
            )
            write(".claude/skills/astray/SKILL.md", "---\nname: astray\ndescription: Astray.\n---\n")
            write(
                ".agents/skills/astray/SKILL.md",
                "---\nname: astray\ndescription: Astray.\n---\n\n" + pointer.format("other"),
            )
            write(".agents/skills/stray/SKILL.md", "---\nname: stray\ndescription: Stray.\n---\n")
            self.assertEqual(
                [
                    ".agents/skills/stray/SKILL.md has no .claude/skills/stray/SKILL.md",
                    ".agents/skills/astray/SKILL.md does not point to ../../../.claude/skills/astray/SKILL.md",
                    ".agents/skills/drifted/SKILL.md frontmatter differs from .claude/skills/drifted/SKILL.md",
                    ".claude/skills/missing/SKILL.md has no .agents/skills/missing/SKILL.md shim",
                ],
                repository_skill_problems(root),
            )

    def test_grant_policy_holds_repository_skills_to_the_repository_tools_they_run(self) -> None:
        own = "python -B tools/a.py"
        twin = json.dumps([f"Bash({own}*)", f"PowerShell({own}*)"])
        skills = {
            "bare": ('["Bash"]', own),
            "unpaired": (json.dumps([f"Bash({own}*)"]), own),
            # gh acts outside the conversation, so it may keep prompting; tools/b.py is the repository's own.
            "ungranted": (twin, 'python -B tools/b.py\ngh pr create --body-file "<file>"'),
            "good": (twin, f'{own} "<skill>"'),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    f".claude/skills/{skill}/SKILL.md": (
                        f"---\nname: {skill}\nallowed-tools: {allowed}\n---\n\n```bash\n{command}\n```\n"
                    )
                    for skill, (allowed, command) in skills.items()
                },
            )
            self.assertEqual(
                [
                    f".claude/skills/bare/SKILL.md grants Bash for every command; see {GRANTS_DOC}",
                    f".claude/skills/ungranted/SKILL.md:7 no Bash grant covers python -B tools/b.py; see {GRANTS_DOC}",
                    ".claude/skills/ungranted/SKILL.md:7 no PowerShell grant covers python -B tools/b.py; "
                    f"see {GRANTS_DOC}",
                    f".claude/skills/unpaired/SKILL.md grants Bash({own}*) in one shell only; see {GRANTS_DOC}",
                ],
                skill_grant_problems(root, repository_skill_files(root), REPOSITORY_TOOL_COMMAND),
            )

    def test_description_policy_holds_repository_skills(self) -> None:
        descriptions = {
            "long": "x" * 1020 + " Use it when asked.",
            "silent": "Does things.",
            "user": "Does things.\ndisable-model-invocation: true",
            "good": "Does things. Use it when asked.",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    f".claude/skills/{skill}/SKILL.md": f"---\nname: {skill}\ndescription: {text}\n---\n\nBody.\n"
                    for skill, text in descriptions.items()
                },
            )
            from tools import skill_reference

            self.assertEqual(
                [
                    ".claude/skills/long/SKILL.md: Skill 'long' description is 1039 characters; runtime adapters "
                    "allow at most 1024",
                    ".claude/skills/silent/SKILL.md: the model may start it, so its description must "
                    f"{skill_reference.WHEN_RULE}",
                ],
                description_problems(root, repository_skill_files(root)),
            )

    def test_skill_grant_policy_detects_unscoped_unpaired_unused_and_ungranted_grants(self) -> None:
        own = 'python -B "${CLAUDE_SKILL_DIR}/scripts/'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            skills = {
                "bare": ('["Bash"]', f'{own}x.py"'),
                "unpaired": (json.dumps([f"Bash({own}*)"]), f'{own}x.py"'),
                "ungranted": (
                    json.dumps([f"Bash({own}*)", f"PowerShell({own}*)"]),
                    'python -B "${CLAUDE_SKILL_DIR}/../core/scripts/y.py"\ngit status',
                ),
                "none": ('["Read"]', f'{own}x.py"'),
                "unused": (json.dumps([f"Bash({own}*)", f"PowerShell({own}*)", "Glob"]), f'{own}x.py"'),
                "expands": (json.dumps([f"Bash({own}*)", f"PowerShell({own}*)"]), f'{own}x.py" --cwd "$PWD"'),
                "good": (json.dumps([f"Bash({own}*)", f"PowerShell({own}*)"]), f'{own}x.py" --plan "<plan file>"'),
            }
            for skill, (allowed, command) in skills.items():
                (root / "deploy-meta" / f"{skill}.json").write_text("{}", encoding="utf-8")
                directory = root / "skills" / skill
                directory.mkdir(parents=True)
                (directory / "SKILL.md").write_text(
                    f"---\nname: {skill}\nallowed-tools: {allowed}\n---\n\n```bash\n{command}\n```\n", encoding="utf-8"
                )
            (root / "skills" / "asset").mkdir()
            self.assertEqual(
                [
                    f"skills/bare/SKILL.md grants Bash for every command; see {GRANTS_DOC}",
                    "skills/expands/SKILL.md:7 expands a shell variable, "
                    f'so it always prompts: {own}x.py" --cwd "$PWD"; '
                    f"see {GRANTS_DOC}",
                    f"skills/none/SKILL.md grants Read, which no step uses; see {GRANTS_DOC}",
                    f"skills/none/SKILL.md:6 runs a command without a shell grant; see {GRANTS_DOC}",
                    f"skills/ungranted/SKILL.md:7 no Bash grant covers "
                    f'python -B "${{CLAUDE_SKILL_DIR}}/../core/scripts/y.py"; see {GRANTS_DOC}',
                    f"skills/ungranted/SKILL.md:7 no PowerShell grant covers "
                    f'python -B "${{CLAUDE_SKILL_DIR}}/../core/scripts/y.py"; see {GRANTS_DOC}',
                    f"skills/unpaired/SKILL.md grants Bash({own}*) in one shell only; see {GRANTS_DOC}",
                    f"skills/unused/SKILL.md grants Glob, which no step uses; see {GRANTS_DOC}",
                ],
                skill_grant_problems(root),
            )


if __name__ == "__main__":
    unittest.main()
