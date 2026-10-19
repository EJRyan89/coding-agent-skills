from __future__ import annotations

import ast
import contextlib
import io
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))

import change_skill

SCRIPT = Path(__file__).resolve().parent / "change_skill.py"
REPOSITORY = Path(__file__).resolve().parents[4]
GIT_CONFIG: Mapping[str, str] = MappingProxyType({"init.defaultBranch": "main"})


def skill_text(description: str, body: str = "Run it.\n", grants: str = '["Read"]') -> str:
    return f"---\nname: x\ndescription: {description}\nallowed-tools: {grants}\n---\n\n# X\n\n{body}"


# A source tree with a shipped skill, a repository skill, a skill declaring an agent, and a skill with scenarios
# whose skill_deps reach the pipeline it runs.
BASE_FILES = {
    "skills/alpha/SKILL.md": skill_text("Alpha does a thing. Use it when asked."),
    "skills/alpha/scripts/alpha.py": "print('alpha')\n",
    "deploy-meta/alpha.json": json.dumps({"tools": [], "agent_deps": []}) + "\n",
    ".claude/skills/beta/SKILL.md": skill_text("Beta does a thing. Use it when asked."),
    "agents/helper.md": "---\nname: helper\n---\n\nHelp.\n",
    "skills/gamma/SKILL.md": skill_text("Gamma does a thing. Use it when asked."),
    "deploy-meta/gamma.json": json.dumps({"agent_deps": ["helper"]}) + "\n",
    "skills/ejryan89-code-review-core/SKILL.md": skill_text("The pipeline. Use it when asked."),
    "skills/ejryan89-code-review-core/scripts/review_pipeline.py": "print('pipeline')\n",
    "deploy-meta/ejryan89-code-review-core.json": json.dumps({"skill_deps": []}) + "\n",
    "skills/review-prs/SKILL.md": skill_text("Reviews. Use it when asked."),
    "deploy-meta/review-prs.json": json.dumps({"skill_deps": ["ejryan89-code-review-core"]}) + "\n",
    "tests/fixtures/skill-evals/review-prs/clean-change/scenario.json": "{}\n",
    "skills/runtime-compatibility.md": "Shared guidance.\n",
    "docs/guide.md": "# Guide\n",
}


def remove_tree(path: Path) -> None:
    def writable(function: Callable[..., object], target: str, *_: object) -> None:
        Path(target).chmod(stat.S_IWRITE)
        function(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable)
    else:
        shutil.rmtree(path, onerror=writable)


def git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(directory), *arguments], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} failed: {result.stderr}")
    return result.stdout.strip()


def write(root: Path, files: Mapping[str, str]) -> None:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


def run(argv: list[str], root: Path) -> tuple[int, list[str]]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = change_skill.main(argv, root=root)
    return code, output.getvalue().splitlines()


class SectionTests(unittest.TestCase):
    DOCUMENT = (
        "# Title\n\n## Files\n\nThe paragraph before the first subsection.\n\n"
        "### Granting tools\n\nGrant both shells.\n\n```markdown\n## Not a heading\n```\n\nStill granting.\n\n"
        "### `audit-skill`\n\nAudit it.\n\n"
        "## Validation\n\nRun the checks.\n"
    )

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "check out"
        write(self.root, {"docs/adding-a-skill.md": self.DOCUMENT, "docs/notes.txt": "## Files\n"})
        write(Path(temporary.name), {"outside.md": "## Files\n\nOutside.\n"})

    def test_a_subsection_runs_to_the_next_heading_past_a_fenced_heading_shaped_line(self) -> None:
        self.assertEqual(
            (
                0,
                [
                    'SECTION "docs/adding-a-skill.md" 7-15 ### Granting tools',
                    "",
                    "Grant both shells.",
                    "",
                    "```markdown",
                    "## Not a heading",
                    "```",
                    "",
                    "Still granting.",
                ],
            ),
            run(["section", "Granting tools"], self.root),
        )

    def test_a_section_stops_at_its_first_subsection(self) -> None:
        self.assertEqual(
            (0, ['SECTION "docs/adding-a-skill.md" 3-5 ## Files', "", "The paragraph before the first subsection."]),
            run(["section", "Files"], self.root),
        )

    def test_a_title_matches_a_heading_without_its_backticks_and_the_last_section_runs_to_the_end(self) -> None:
        self.assertEqual(
            (0, ['SECTION "docs/adding-a-skill.md" 17-19 ### `audit-skill`', "", "Audit it."]),
            run(["section", "audit-skill"], self.root),
        )
        self.assertEqual(
            (0, ['SECTION "docs/adding-a-skill.md" 21-23 ## Validation', "", "Run the checks."]),
            run(["section", "Validation", "--document", "docs/adding-a-skill.md"], self.root),
        )

    def test_a_title_no_heading_reads_lists_the_headings_and_fails(self) -> None:
        code, lines = run(["section", "Not a heading"], self.root)
        self.assertEqual(1, code)
        self.assertEqual(
            [
                "HEADING 1 # Title",
                "HEADING 3 ## Files",
                "HEADING 7 ### Granting tools",
                "HEADING 17 ### `audit-skill`",
                "HEADING 21 ## Validation",
                'FAILED no heading of the document reads "Not a heading"',
            ],
            lines,
        )

    def test_a_title_two_headings_read_fails_rather_than_choosing(self) -> None:
        write(self.root, {"docs/twice.md": "## Files\n\none\n\n### Files\n\ntwo\n"})
        code, lines = run(["section", "Files", "--document", "docs/twice.md"], self.root)
        self.assertEqual((1, 'FAILED 2 headings of the document reads "Files"'), (code, lines[-1]))

    def test_a_document_outside_the_checkout_or_not_markdown_is_refused(self) -> None:
        for document in ("../outside.md", "docs/notes.txt", "docs/missing.md", str(self.root.parent / "outside.md")):
            with self.subTest(document=document):
                code, lines = run(["section", "Files", "--document", document], self.root)
                self.assertEqual(1, code)
                self.assertEqual([f"FAILED {document} is not a Markdown file in this checkout"], lines)

    def test_the_script_prints_a_section_of_this_checkout_as_a_program(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "section", "Granting tools"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertRegex(completed.stdout.splitlines()[0], r'^SECTION "docs/adding-a-skill\.md" \d+-\d+ ### Granting')


class GateTests(unittest.TestCase):
    git_config = GIT_CONFIG

    def setUp(self) -> None:
        temporary = Path(tempfile.mkdtemp(prefix="change-skill-gates-"))
        self.addCleanup(remove_tree, temporary)
        self.root = temporary / "source tree"
        write(self.root, BASE_FILES)
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        git(self.root, "switch", "-q", "-c", "work")

    def gates(
        self, committed: Mapping[str, str] | None = None, uncommitted: Mapping[str, str] | None = None
    ) -> list[str]:
        """The lines after BASE and CHANGED for a change committed on the branch, then left in the working tree."""
        if committed:
            write(self.root, committed)
            git(self.root, "add", "-A")
            git(self.root, "commit", "-q", "-m", "change")
        write(self.root, uncommitted or {})
        code, lines = run(["gates", "--base", "main"], self.root)
        self.assertEqual(0, code, lines)
        self.assertRegex(lines[0], r"^BASE main [0-9a-f]{40}$")
        self.assertTrue(lines[1].startswith("CHANGED "))
        self.assertEqual(f"OWED {len(lines) - 3}", lines[-1])
        return lines[2:-1]

    def test_nothing_changed_owes_nothing(self) -> None:
        self.assertEqual([], self.gates())

    def test_a_body_change_owes_the_cost_audit_alone(self) -> None:
        body = skill_text("Alpha does a thing. Use it when asked.", body="Run it twice.\n")
        self.assertEqual(["GATE audit-skill alpha"], self.gates(committed={"skills/alpha/SKILL.md": body}))

    def test_a_suite_or_script_change_owes_the_cost_audit_alone(self) -> None:
        self.assertEqual(
            ["GATE audit-skill alpha"], self.gates(uncommitted={"skills/alpha/scripts/test_alpha.py": "pass\n"})
        )

    def test_a_description_change_owes_the_reference_the_canary_and_the_trigger_run(self) -> None:
        changed = skill_text("Alpha does another thing. Use it when asked.")
        self.assertEqual(
            [
                "REGENERATE python tools/skill_reference.py --write",
                "GATE audit-skill alpha",
                "GATE runtime-canary alpha",
                "GATE evaluate-skill alpha trigger",
            ],
            self.gates(uncommitted={"skills/alpha/SKILL.md": changed}),
        )

    def test_a_skill_with_only_a_trigger_scenario_owes_its_run_for_a_description_change_alone(self) -> None:
        scenario = {"tests/fixtures/skill-evals/alpha/description/scenario.json": '{"mode": "trigger"}\n'}
        body = skill_text("Alpha does a thing. Use it when asked.", body="Run it twice.\n")
        self.assertEqual(["GATE audit-skill alpha"], self.gates(committed={**scenario, "skills/alpha/SKILL.md": body}))
        changed = skill_text("Alpha does another thing. Use it when asked.", body="Run it twice.\n")
        self.assertEqual(
            "GATE evaluate-skill alpha trigger", self.gates(uncommitted={"skills/alpha/SKILL.md": changed})[-1]
        )

    def test_an_unreadable_scenario_counts_as_a_review_scenario(self) -> None:
        scenario = {"tests/fixtures/skill-evals/alpha/broken/scenario.json": "{not json\n"}
        self.assertIn(
            "GATE evaluate-skill alpha if-prompt",
            self.gates(committed=scenario, uncommitted={"skills/alpha/scripts/alpha.py": "print('more')\n"}),
        )

    def test_a_user_only_skills_description_owes_no_trigger_run(self) -> None:
        text = "---\nname: zeta\ndescription: Zeta deploys.\ndisable-model-invocation: true\n---\n\n# Zeta\n"
        new = {"skills/zeta/SKILL.md": text, "deploy-meta/zeta.json": "{}\n"}
        self.assertEqual(
            [
                "REGENERATE python tools/skill_reference.py --write",
                "GATE audit-skill zeta",
                "GATE runtime-canary zeta",
            ],
            self.gates(uncommitted=new),
        )

    def test_a_grant_change_owes_the_canary(self) -> None:
        changed = skill_text("Alpha does a thing. Use it when asked.", grants='["Read", "Grep"]')
        self.assertIn("GATE runtime-canary alpha", self.gates(committed={"skills/alpha/SKILL.md": changed}))

    def test_a_new_path_to_the_skills_own_files_owes_the_canary_but_not_the_reference(self) -> None:
        body = 'Run `python -B "${CLAUDE_SKILL_DIR}/scripts/alpha.py"`.\n'
        changed = skill_text("Alpha does a thing. Use it when asked.", body=body)
        self.assertEqual(
            ["GATE audit-skill alpha", "GATE runtime-canary alpha"],
            self.gates(uncommitted={"skills/alpha/SKILL.md": changed}),
        )

    def test_metadata_owes_the_reference_and_the_canary_only_for_what_a_runtime_finds(self) -> None:
        tools = json.dumps({"tools": ["gh"], "agent_deps": []}) + "\n"
        self.assertEqual(
            ["REGENERATE python tools/skill_reference.py --write", "GATE audit-skill alpha"],
            self.gates(committed={"deploy-meta/alpha.json": tools}),
        )
        agents = json.dumps({"tools": ["gh"], "agent_deps": ["helper"]}) + "\n"
        self.assertIn("GATE runtime-canary alpha", self.gates(uncommitted={"deploy-meta/alpha.json": agents}))

    def test_a_flip_of_selectable_owes_the_canary_because_it_adds_or_removes_the_adapter(self) -> None:
        hidden = json.dumps({"tools": [], "agent_deps": [], "selectable": False}) + "\n"
        self.assertEqual(
            [
                "REGENERATE python tools/skill_reference.py --write",
                "GATE audit-skill alpha",
                "GATE runtime-canary alpha",
            ],
            self.gates(uncommitted={"deploy-meta/alpha.json": hidden}),
        )

    def test_metadata_that_cannot_be_read_owes_the_canary(self) -> None:
        self.assertIn("GATE runtime-canary alpha", self.gates(uncommitted={"deploy-meta/alpha.json": "{not json\n"}))

    def test_a_repository_skills_frontmatter_owes_its_shim_and_never_the_canary(self) -> None:
        changed = skill_text("Beta does another thing. Use it when asked.")
        self.assertEqual(
            ["REGENERATE python tools/skill_shims.py --write", "GATE audit-skill beta"],
            self.gates(committed={".claude/skills/beta/SKILL.md": changed}),
        )

    def test_a_changed_agent_owes_the_canary_and_the_audit_of_each_skill_declaring_it(self) -> None:
        self.assertEqual(
            ["GATE audit-skill gamma", "GATE runtime-canary gamma"],
            self.gates(uncommitted={"agents/helper.md": "---\nname: helper\n---\n\nHelp more.\n"}),
        )

    def test_a_runtime_prompt_source_owes_the_samples_and_the_evaluation_of_each_skill_reaching_it(self) -> None:
        self.assertEqual(
            [
                "REGENERATE python tools/runtime_prompts.py --write",
                "GATE audit-skill ejryan89-code-review-core",
                "GATE evaluate-skill review-prs if-prompt",
            ],
            self.gates(
                uncommitted={"skills/ejryan89-code-review-core/scripts/review_prompt.py": "PROMPT = 'review'\n"}
            ),
        )

    def test_a_tool_the_samples_render_through_owes_them_and_no_skill_gate(self) -> None:
        self.assertEqual(
            ["REGENERATE python tools/runtime_prompts.py --write"],
            self.gates(uncommitted={"tools/runtime_prompts.py": "pass\n"}),
        )

    def test_a_new_skill_owes_the_reference_the_audit_and_the_canary(self) -> None:
        new = {"skills/delta/SKILL.md": skill_text("Delta. Use it when asked."), "deploy-meta/delta.json": "{}\n"}
        self.assertEqual(
            [
                "REGENERATE python tools/skill_reference.py --write",
                "GATE audit-skill delta",
                "GATE runtime-canary delta",
                "GATE evaluate-skill delta trigger",
            ],
            self.gates(uncommitted=new),
        )

    def test_a_removed_skill_owes_the_reference_and_no_gate(self) -> None:
        git(self.root, "rm", "-q", "-r", "skills/alpha", "deploy-meta/alpha.json")
        self.assertEqual(["REGENERATE python tools/skill_reference.py --write"], self.gates())

    def test_a_shared_asset_or_another_file_owes_nothing(self) -> None:
        changed = {"skills/runtime-compatibility.md": "Changed.\n", "docs/guide.md": "# Guide\n\nMore.\n"}
        self.assertEqual([], self.gates(committed=changed))

    def test_a_base_git_cannot_find_is_a_failed_line(self) -> None:
        code, lines = run(["gates", "--base", "no-such-branch"], self.root)
        self.assertEqual(1, code)
        self.assertTrue(lines[-1].startswith("FAILED "), lines)

    def test_a_base_that_reads_as_an_option_is_a_usage_error(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            change_skill.main(["gates", "--base=--output=x"], root=self.root)
        self.assertEqual(2, raised.exception.code)
        self.assertIn("is not a ref", stderr.getvalue())


class RuntimePromptSourceTests(unittest.TestCase):
    def test_the_sources_are_the_skills_and_fixtures_the_sample_tool_renders_from(self) -> None:
        tool = REPOSITORY / "tools" / "runtime_prompts.py"
        tree = ast.parse(tool.read_text(encoding="utf-8"))
        siblings = {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "sibling_scripts"
            and isinstance(node.args[0], ast.Constant)
        }
        self.assertEqual({"ejryan89-code-review-core", "review-document", "review-insights"}, siblings)
        skills = {
            source.split("/")[1] for source in change_skill.RUNTIME_PROMPT_SOURCES if source.startswith("skills/")
        }
        self.assertEqual(siblings, skills)
        source = tool.read_text(encoding="utf-8")
        self.assertIn('"tests" / "fixtures" / "skill-evals" / "review-prs"', source)
        self.assertIn("from tools import skill_evals", source)
        self.assertIn("tests/fixtures/skill-evals/review-prs/", change_skill.RUNTIME_PROMPT_SOURCES)
        self.assertIn("tools/skill_evals.py", change_skill.RUNTIME_PROMPT_SOURCES)


@pytest.mark.parametrize(
    "title", ["Files", "Description", "Granting tools", "Paths to a skill's own files", "Validation"]
)
def test_every_section_the_skill_quotes_from_the_contract_is_one_heading_there(title: str) -> None:
    contract = (REPOSITORY / "docs" / "adding-a-skill.md").read_text(encoding="utf-8").splitlines()
    heading, body = change_skill.section(contract, title)
    assert title == heading.text
    assert any(line.strip() for line in body)
