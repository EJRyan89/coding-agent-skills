"""Fixture tests for tests/validation/markdown_links.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from markdown_links import (
    SECURITY_DOC,
    THREAT_MODEL_DOC,
    THREAT_MODEL_SUITES,
    TRUST_MODEL_DOC,
    cited_test_problems,
    lessons_ledger_problems,
    markdown_link_problems,
    threat_model_test_problems,
    trust_model_area_problems,
)
from validation_support import write_fixture_tree


class MarkdownLinkPolicy(unittest.TestCase):
    TARGET = (
        "# Guide\n\n## The `run_validation` step_two\n\n## Notes: (draft) & more!\n\n## Notes: (draft) & more!\n\n"
        "```markdown\n## Not a heading\n```\n"
    )

    def problems(self, files: Mapping[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            write_fixture_tree(root, files)
            return markdown_link_problems(root)

    def test_fragments_follow_githubs_heading_slugs(self) -> None:
        # Backticks and punctuation go, underscores and hyphens stay, and a repeated heading is numbered.
        index = (
            "# Index\n\n## Local heading\n\n"
            "See [the step](docs/target.md#the-run_validation-step_two), [notes](docs/target.md#notes-draft--more)"
            " and [again](<docs/target.md#notes-draft--more-1>).\n"
            "Also [here](#local-heading), [the folder](docs/), ![a picture](docs/target.md 'title'),"
            " [away](https://example.com/missing.md#nowhere) and [mail](mailto:someone@example.com).\n"
            "Code is not a link: `[x](missing.md)`.\n\n"
            "```text\n[x](missing.md)\n```\n\n"
            "[reference]: docs/target.md#guide\n"
        )
        self.assertEqual([], self.problems({"index.md": index, "docs/target.md": self.TARGET}))

    def test_a_link_to_a_missing_file_fails(self) -> None:
        self.assertEqual(
            ["docs/guide.md:3 links to ../missing.md, which does not exist"],
            self.problems({"docs/guide.md": "# Guide\n\nSee [gone](../missing.md).\n"}),
        )

    def test_a_fragment_that_names_no_heading_fails(self) -> None:
        self.assertEqual(
            [
                "index.md:1 links to docs/target.md#the-run-validation-step-two, which has no heading with that slug",
                "index.md:2 links to docs/target.md#not-a-heading, which has no heading with that slug",
                "index.md:3 links to #missing, which has no heading with that slug",
            ],
            self.problems(
                {
                    "index.md": "[a](docs/target.md#the-run-validation-step-two)\n"
                    "[b](docs/target.md#not-a-heading)\n"
                    "[c](#missing)\n",
                    "docs/target.md": self.TARGET,
                }
            ),
        )

    def test_ignored_and_worktree_markdown_is_not_read(self) -> None:
        broken = "[gone](missing.md)\n"
        self.assertEqual(
            [],
            self.problems(
                {".gitignore": "ignored.md\n", "ignored.md": broken, ".claude/worktrees/feat-x/README.md": broken}
            ),
        )


class CitedTestPolicy(unittest.TestCase):
    # A helper beside the tests, a class defined under an `if`, as a platform's tests are, and a class whose tests are
    # its base's.
    SUITE = (
        "import unittest\n\n\ndef helper() -> None:\n    pass\n\n\ndef test_alone() -> None:\n    pass\n\n\n"
        "class Base(unittest.TestCase):\n    def test_inherited(self) -> None:\n        pass\n\n\n"
        "class Derived(Base):\n    pass\n\n\n"
        "if True:\n\n    class GuardTests(unittest.TestCase):\n        def test_held(self) -> None:\n"
        "            pass\n\n        def check(self) -> None:\n            pass\n"
    )

    def problems(self, files: Mapping[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            write_fixture_tree(root, {"tests/test_a.py": self.SUITE, **files})
            return cited_test_problems(root)

    def test_cited_tests_and_test_classes_that_exist_pass(self) -> None:
        guide = (
            "# Guide\n\n"
            "Held by `tests/test_a.py::test_alone`, `tests/test_a.py::GuardTests::test_held`, and "
            "`tests/test_a.py::Derived::test_inherited`.\n"
            "The `GuardTests` cases and `test_held` in `tests/test_a.py`, and `test_alone` in `test_a.py`.\n"
            "| `GuardTests` | `test_a.py::Derived` |\n"
            "The policy `test_policy` in `tests/policy.py`, with its fixtures in `tests/test_a.py`.\n"
            "Named alone, `Derived` and `test_inherited` are defined somewhere; `subTest` is no citation.\n\n"
            "```text\n`tests/test_a.py::test_gone`\n```\n"
        )
        policy = (
            "import unittest\n\n\nclass Policies(unittest.TestCase):\n    def test_policy(self) -> None:\n"
            "        pass\n"
        )
        self.assertEqual(
            [],
            self.problems(
                {
                    "docs/guide.md": guide,
                    "tests/policy.py": policy,
                    "tests/fixtures/x/README.md": "`tests/test_a.py::test_gone`\n",
                    "docs/upgrade-notes.md": "`tests/test_a.py::test_gone`\n",
                }
            ),
        )

    def test_a_missing_test_or_test_class_fails(self) -> None:
        guide = (
            "`tests/test_a.py::test_gone` and `tests/test_a.py::GoneTests`.\n"
            "`tests/test_a.py::GuardTests::test_gone` and `tests/test_a.py::Derived::test_held`.\n"
            "The `GoneTests` cases in `tests/test_a.py`.\n"
            "`test_gone` alone.\n"
        )
        self.assertEqual(
            [
                "guide.md:1 cites tests/test_a.py::test_gone, which tests/test_a.py does not define as a test or a "
                "test class",
                "guide.md:1 cites tests/test_a.py::GoneTests, which tests/test_a.py does not define as a test or a "
                "test class",
                "guide.md:2 cites tests/test_a.py::GuardTests::test_gone, which tests/test_a.py does not define as a "
                "test of its class GuardTests",
                "guide.md:2 cites tests/test_a.py::Derived::test_held, which tests/test_a.py does not define as a "
                "test of its class Derived",
                "guide.md:3 cites GoneTests in tests/test_a.py, where no test or test class has that name",
                "guide.md:4 cites test_gone, which no Python file defines as a test or a test class",
            ],
            self.problems({"guide.md": guide}),
        )

    def test_a_cited_helper_is_not_a_test(self) -> None:
        guide = (
            "`tests/test_a.py::helper`, `tests/test_a.py::GuardTests::check`, and `tests/test_a.py::Base::helper`.\n"
        )
        self.assertEqual(
            [
                "guide.md:1 cites tests/test_a.py::helper, which tests/test_a.py does not define as a test or a test "
                "class",
                "guide.md:1 cites tests/test_a.py::GuardTests::check, which tests/test_a.py does not define as a test "
                "of its class GuardTests",
                "guide.md:1 cites tests/test_a.py::Base::helper, which tests/test_a.py does not define as a test of "
                "its class Base",
            ],
            self.problems({"guide.md": guide}),
        )

    def test_a_missing_suite_fails_whether_named_with_its_path_or_alone(self) -> None:
        guide = (
            "`tests/test_gone.py::test_alone`, and the `GuardTests` cases in `tests/test_gone.py`.\n"
            "`test_gone.py::test_alone`.\n"
        )
        self.assertEqual(
            [
                "guide.md:1 cites tests/test_gone.py::test_alone, but tests/test_gone.py does not exist",
                "guide.md:1 cites GuardTests in tests/test_gone.py, which does not exist",
                "guide.md:2 cites test_gone.py::test_alone, but any file named test_gone.py does not exist",
            ],
            self.problems({"guide.md": guide}),
        )

    def test_the_threat_model_names_its_suites_under_the_code_review_scripts(self) -> None:
        # The contract's suites named alone are under THREAT_MODEL_SUITES, so tests/test_a.py does not hold its
        # citation.
        contract = "`test_a.py::test_alone` and `test_b.py::test_alone`.\n"
        self.assertEqual(
            [f"{THREAT_MODEL_DOC}:1 cites test_a.py::test_alone, but {THREAT_MODEL_SUITES}/test_a.py does not exist"],
            self.problems({THREAT_MODEL_DOC: contract, f"{THREAT_MODEL_SUITES}/test_b.py": self.SUITE}),
        )


class LessonsLedgerPolicy(unittest.TestCase):
    SUITE = (
        "import unittest\n\n\ndef helper() -> None:\n    pass\n\n\n"
        "class GuardTests(unittest.TestCase):\n    def test_held(self) -> None:\n        pass\n"
    )
    SKILL = "---\nname: a\n---\n\n# A\n\n## 3. Bug class\n\nSearch.\n"
    ENTRY = "- **Found in:** #12\n- **Lesson:** Text.\n"

    def problems(self, ledger: str | None, files: Mapping[str, str] | None = None) -> list[str]:
        tree = {
            "tests/test_a.py": self.SUITE,
            ".claude/skills/a/SKILL.md": self.SKILL,
            ".github/workflows/watch.yml": "on: workflow_dispatch\n",
            **(files or {}),
        }
        if ledger is not None:
            tree["docs/lessons.md"] = ledger
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            write_fixture_tree(root, tree)
            return lessons_ledger_problems(root)

    def test_entries_whose_guards_resolve_or_that_give_a_reason_pass(self) -> None:
        ledger = (
            "# Lessons\n\nPreamble with `tests/test_a.py::helper`, which no entry cites.\n\n## Lessons\n\n"
            f"### Every reference resolves\n\n{self.ENTRY}"
            "- **Guard:** `tests/test_a.py::GuardTests::test_held`, `tests/test_a.py::GuardTests`,"
            " `tests/test_a.py::test_held`, `.claude/skills/a/SKILL.md#3-bug-class`,\n"
            "  and `.github/workflows/watch.yml`.\n\n"
            f"### No check can tell\n\n{self.ENTRY}- **No guard:** A reader cannot tell it from the code.\n\n"
            "```markdown\n### A fenced heading is no entry\n```\n"
        )
        self.assertEqual([], self.problems(ledger))

    def test_an_entry_with_neither_a_guard_nor_a_reason_fails(self) -> None:
        ledger = (
            f"# Lessons\n\n## Lessons\n\n### Nothing holds it\n\n{self.ENTRY}\n"
            f"### An empty reason\n\n{self.ENTRY}- **No guard:**\n\n"
            f"### No reference\n\n{self.ENTRY}- **Guard:** the review\n\n"
            f"### Both\n\n{self.ENTRY}- **Guard:** `.github/workflows/watch.yml`\n- **No guard:** None is possible.\n"
        )
        self.assertEqual(
            [
                "docs/lessons.md:5 the lesson 'Nothing holds it' names neither a guard nor the reason no guard is "
                "possible",
                "docs/lessons.md:10 the lesson 'An empty reason' names neither a guard nor the reason no guard is "
                "possible",
                "docs/lessons.md:20 the lesson 'No reference' names no guard in a code span",
                "docs/lessons.md:22 the lesson 'Both' names both a guard and the reason no guard is possible",
            ],
            self.problems(ledger),
        )

    def test_a_guard_that_does_not_resolve_fails(self) -> None:
        ledger = (
            f"# Lessons\n\n## Lessons\n\n### Gone\n\n{self.ENTRY}- **Guard:**"
            " `tests/test_a.py::test_gone`, `tests/test_a.py::helper`, `tests/test_gone.py::test_held`,"
            " `test_a.py::test_held`, `.claude/skills/a/SKILL.md#4-gone`, `.claude/skills/b/SKILL.md#a`,"
            " `docs/guide.md#a`, `.github/workflows/gone.yml`, and `GuardTests`,\n"
            "  and on its continuation line `tests/test_a.py::GoneTests`.\n"
            "- **Lesson:** Not a guard: `tests/test_a.py::GoneTests`.\n"
        )
        prefix = "docs/lessons.md:9 the lesson 'Gone'"
        self.assertEqual(
            [
                f"{prefix} cites tests/test_a.py::test_gone, which tests/test_a.py does not define as a test or a "
                "test class",
                f"{prefix} cites tests/test_a.py::helper, which tests/test_a.py does not define as a test or a test "
                "class",
                f"{prefix} cites tests/test_gone.py::test_held, but tests/test_gone.py does not exist",
                f"{prefix} names test_a.py::test_held, which is not a test ID, a skill's step, or a workflow by its "
                "path from the repository root",
                f"{prefix} cites .claude/skills/a/SKILL.md#4-gone, which names no heading of .claude/skills/a/SKILL.md",
                f"{prefix} cites .claude/skills/b/SKILL.md#a, but .claude/skills/b/SKILL.md does not exist",
                f"{prefix} names docs/guide.md#a, which is not a test ID, a skill's step, or a workflow by its path "
                "from the repository root",
                f"{prefix} cites .github/workflows/gone.yml, but .github/workflows/gone.yml does not exist",
                f"{prefix} names GuardTests, which is not a test ID, a skill's step, or a workflow by its path from "
                "the repository root",
                "docs/lessons.md:10 the lesson 'Gone' cites tests/test_a.py::GoneTests, which tests/test_a.py does not "
                "define as a test or a test class",
            ],
            self.problems(ledger, {"docs/guide.md": "# A\n"}),
        )

    def test_an_entry_that_names_no_issue_fails(self) -> None:
        ledger = (
            "# Lessons\n\n## Lessons\n\n### Unsourced\n\n- **Found in:** the audit\n- **Guard:**"
            " `.github/workflows/watch.yml`\n\n### Unfielded\n\n- **Guard:** `.github/workflows/watch.yml`\n"
        )
        self.assertEqual(
            [
                "docs/lessons.md:5 the lesson 'Unsourced' names no issue it was found in",
                "docs/lessons.md:10 the lesson 'Unfielded' names no issue it was found in",
            ],
            self.problems(ledger),
        )

    def test_a_missing_ledger_section_or_an_empty_one_fails(self) -> None:
        self.assertEqual(["docs/lessons.md does not exist"], self.problems(None))
        self.assertEqual(["docs/lessons.md has no '## Lessons' section"], self.problems("# Lessons\n\nProse.\n"))
        self.assertEqual(
            ["docs/lessons.md has no lesson under '## Lessons'"],
            self.problems("# Lessons\n\n## Lessons\n\nProse.\n\n## After\n\n### Not a lesson\n"),
        )


class ThreatModelPolicy(unittest.TestCase):
    TABLE = (
        "# Contract\n\n## Threat model\n\n"
        "| The author controls | The suite guarantees | Held by |\n| --- | --- | --- |\n"
        "| Diff text | Data only. | `test_adversarial_inputs.py::test_held`, `test_a.py::test_in_a_class` |\n"
        "| Paths | Excluded. | `test_a.py::test_held`, the `Tests` cases in `test_adversarial_inputs.py` |\n"
        "| Links | Excluded. | the snapshot tests |\n"
        "| Blobs | Exact bytes. | `test_adversarial_inputs.py::Tests::test_in_a_class` |\n\n"
        "## Formats\n\n| `not_cited.py::test_x` |\n"
    )

    def problems(self, files: Mapping[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, files)
            return threat_model_test_problems(root)

    def test_a_row_that_names_no_adversarial_test_by_its_id_fails(self) -> None:
        self.assertEqual(
            [
                f"{THREAT_MODEL_DOC}: the threat-model row 'Paths' names no test in test_adversarial_inputs.py",
                f"{THREAT_MODEL_DOC}: the threat-model row 'Links' names no test in test_adversarial_inputs.py",
            ],
            self.problems({THREAT_MODEL_DOC: self.TABLE}),
        )

    def test_rows_that_each_name_an_adversarial_test_pass(self) -> None:
        table = (
            "# Contract\n\n## Threat model\n\n"
            "| The author controls | The suite guarantees | Held by |\n| --- | --- | --- |\n"
            "| Diff text | Data only. | `test_adversarial_inputs.py::test_held`, `test_a.py::test_in_a_class` |\n"
            "| Blobs | Exact bytes. | `test_a.py::test_held`, `test_adversarial_inputs.py::Tests::test_in_a_class` |\n"
        )
        self.assertEqual([], self.problems({THREAT_MODEL_DOC: table}))

    def test_a_contract_without_the_table_fails(self) -> None:
        self.assertEqual(
            [f"{THREAT_MODEL_DOC} has no table under '## Threat model'"],
            self.problems({THREAT_MODEL_DOC: "# Contract\n\n## Formats\n"}),
        )


class TrustModelAreaPolicy(unittest.TestCase):
    DESIGN = (
        "# Design\n\n## Trust model\n\n"
        "| Boundary | What it holds | Why | Where |\n| --- | --- | --- | --- |\n"
        "| The deployer | Writes skills. | Runtimes load them. | Recovery. |\n"
        "| A grant | Restricts nothing. | Approval only. | Granting tools. |\n\n"
        "## Invariants\n\n| Not a boundary | x | y | z |\n"
    )

    def problems(self, files: Mapping[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, files)
            return trust_model_area_problems(root)

    def security(self, *items: str) -> str:
        listed = "".join(f"{item}\n" for item in items)
        return f"# Security policy\n\n## Security-sensitive areas\n\n{listed}\n## Other\n\n- **Not a boundary**: x.\n"

    def test_every_boundary_with_an_area_and_unnamed_items_pass(self) -> None:
        security = self.security(
            "- **The deployer**: writes outside a root.", "- **A grant**: too broad.", "- secrets."
        )
        self.assertEqual([], self.problems({TRUST_MODEL_DOC: self.DESIGN, SECURITY_DOC: security}))

    def test_a_boundary_with_no_area_and_an_area_with_no_boundary_fail(self) -> None:
        security = self.security("- **The deployer**: writes outside a root.", "- **A grants**: too broad.")
        self.assertEqual(
            [
                f"{SECURITY_DOC}: no security-sensitive area names the trust-model boundary 'A grant' of "
                f"{TRUST_MODEL_DOC}",
                f"{SECURITY_DOC}: the security-sensitive area 'A grants' names no trust-model boundary of "
                f"{TRUST_MODEL_DOC}",
            ],
            self.problems({TRUST_MODEL_DOC: self.DESIGN, SECURITY_DOC: security}),
        )

    def test_a_design_without_the_table_or_a_policy_without_the_section_fails(self) -> None:
        self.assertEqual(
            [f"{TRUST_MODEL_DOC} has no table under '## Trust model'"],
            self.problems({TRUST_MODEL_DOC: "# Design\n\n## Trust model\n\nProse.\n", SECURITY_DOC: "# S\n"}),
        )
        self.assertEqual(
            [f"{SECURITY_DOC} has no '## Security-sensitive areas' section"],
            self.problems({TRUST_MODEL_DOC: self.DESIGN, SECURITY_DOC: "# Security policy\n"}),
        )
