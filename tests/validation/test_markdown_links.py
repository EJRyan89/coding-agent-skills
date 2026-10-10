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

from markdown_links import THREAT_MODEL_DOC, THREAT_MODEL_SUITES, markdown_link_problems, threat_model_test_problems
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


class ThreatModelPolicy(unittest.TestCase):
    TABLE = (
        "# Contract\n\n## Threat model\n\n"
        "| The author controls | The suite guarantees | Held by |\n| --- | --- | --- |\n"
        "| Diff text | Data only. | `test_adversarial_inputs.py::test_held`, `test_a.py::test_in_a_class` |\n"
        "| Paths | Excluded. | `test_adversarial_inputs.py::test_gone`, `test_missing.py::test_held` |\n"
        "| Links | Excluded. | the snapshot tests |\n"
        "| Blobs | Exact bytes. | `test_a.py::test_in_a_class` |\n\n## Formats\n\n| `not_cited.py::test_x` |\n"
    )
    ADVERSARIAL = (
        "import unittest\n\n\ndef test_held() -> None:\n    pass\n\n\n"
        "class Tests(unittest.TestCase):\n    def test_in_a_class(self) -> None:\n        pass\n\n\n"
        "# test_gone is only a comment\n"
    )

    def problems(self, files: Mapping[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, files)
            return threat_model_test_problems(root)

    def test_a_missing_test_a_missing_suite_and_a_row_without_an_adversarial_test_fail(self) -> None:
        self.assertEqual(
            [
                f"{THREAT_MODEL_DOC} names test_adversarial_inputs.py::test_gone, which test_adversarial_inputs.py "
                "does not define",
                f"{THREAT_MODEL_DOC} names test_missing.py::test_held, but {THREAT_MODEL_SUITES}/test_missing.py does "
                "not exist",
                f"{THREAT_MODEL_DOC}: the threat-model row 'Links' names no test in test_adversarial_inputs.py",
                f"{THREAT_MODEL_DOC}: the threat-model row 'Blobs' names no test in test_adversarial_inputs.py",
            ],
            self.problems(
                {
                    THREAT_MODEL_DOC: self.TABLE,
                    f"{THREAT_MODEL_SUITES}/test_adversarial_inputs.py": self.ADVERSARIAL,
                    f"{THREAT_MODEL_SUITES}/test_a.py": self.ADVERSARIAL,
                }
            ),
        )

    def test_rows_that_each_name_an_adversarial_test_that_exists_pass(self) -> None:
        table = (
            "# Contract\n\n## Threat model\n\n"
            "| The author controls | The suite guarantees | Held by |\n| --- | --- | --- |\n"
            "| Diff text | Data only. | `test_adversarial_inputs.py::test_held`, `test_a.py::test_in_a_class` |\n"
            "| Blobs | Exact bytes. | `test_a.py::test_held`, `test_adversarial_inputs.py::test_in_a_class` |\n"
        )
        self.assertEqual(
            [],
            self.problems(
                {
                    THREAT_MODEL_DOC: table,
                    f"{THREAT_MODEL_SUITES}/test_adversarial_inputs.py": self.ADVERSARIAL,
                    f"{THREAT_MODEL_SUITES}/test_a.py": self.ADVERSARIAL,
                }
            ),
        )

    def test_a_contract_without_the_table_fails(self) -> None:
        self.assertEqual(
            [f"{THREAT_MODEL_DOC} has no table under '## Threat model'"],
            self.problems({THREAT_MODEL_DOC: "# Contract\n\n## Formats\n"}),
        )


if __name__ == "__main__":
    unittest.main()
