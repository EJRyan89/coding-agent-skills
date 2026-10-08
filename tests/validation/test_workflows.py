"""Fixture tests for tests/validation/workflows.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from workflows import WorkflowSyntaxError, read_workflow, workflow_guard_problems


class WorkflowsFixtures(unittest.TestCase):
    def test_workflow_guard_reports_each_drift(self) -> None:
        clean = (
            "name: Example\n\non:\n  pull_request:\n  # A comment is not a trigger.\n  push:\n    branches: [main]\n\n"
            "permissions:\n  contents: read\n\njobs:\n  run:\n    runs-on: windows-latest\n    steps:\n"
            f"      - uses: actions/checkout@{'a' * 40} # v7.0.1\n"
        )
        expected = ["pull_request", "push"]
        self.assertEqual([], workflow_guard_problems(clean, expected, {"actions/checkout"}))
        for drifted, problem in (
            (clean.replace("  push:\n", "  pull_request_target:\n  push:\n"), "triggers"),
            (clean.replace("  contents: read\n", "  contents: write\n"), "permissions"),
            (clean.replace("  contents: read\n", "  contents: read\n  pull-requests: write\n"), "permissions"),
            (clean.replace("    steps:\n", "    permissions: write-all\n    steps:\n"), "permissions"),
            (clean + "        env:\n          TOKEN: ${{ secrets.TOKEN }}\n", "secret"),
            (clean + "        env:\n          TOKEN: ${{ github.token }}\n          OTHER: $GITHUB_TOKEN\n", "token"),
            (clean.replace(f"@{'a' * 40} # v7.0.1", "@v7"), "not pinned"),
            (clean.replace(" # v7.0.1", ""), "not pinned"),
            (clean + f"      - uses: actions/cache@{'b' * 40} # v5.0.0\n", "actions"),
            # What the line-based policy missed: a job's own permissions, other spellings of a secret or the token,
            # and an expression in a run block.
            (clean.replace("    steps:\n", "    permissions:\n      contents: write\n    steps:\n"), "permissions"),
            (clean.replace("    steps:\n", "    permissions:\n      id-token: write\n    steps:\n"), "permissions"),
            (clean + "        env:\n          TOKEN: ${{ secrets['TOKEN'] }}\n", "secret"),
            (clean + "        with:\n          token: ${{ github['token'] }}\n", "token"),
            (clean + "        with:\n          token: ${{github.token}}\n", "token"),
            (clean + "      - run: |\n          echo '${{ secrets.TOKEN }}'\n", "secret"),
            (
                clean.replace(
                    "  run:\n", "  call:\n    uses: o/r/.github/workflows/x.yml@main\n    secrets: inherit\n  run:\n"
                ),
                "secrets",
            ),
            (
                clean.replace(
                    "on:\n  pull_request:\n  # A comment is not a trigger.\n  push:\n    branches: [main]\n",
                    "on: [pull_request_target]\n",
                ),
                "triggers",
            ),
            (clean + "      - uses: 'actions/checkout@v7'\n", "pin can be read"),
            (clean.replace("jobs:\n", "jobs: &shared\n"), "outside the YAML"),
        ):
            with self.subTest(problem=problem, drifted=drifted):
                problems = workflow_guard_problems(drifted, expected, {"actions/checkout"})
                self.assertTrue(problems)
                self.assertTrue(any(problem in found for found in problems), problems)

    def test_a_read_only_job_and_text_that_only_mentions_secrets_pass(self) -> None:
        workflow = (
            "on:\n  workflow_dispatch:\n\npermissions:\n  contents: read\n\njobs:\n  run:\n"
            "    permissions:\n      contents: read\n      checks: none\n    steps:\n"
            "      - run: |\n          # This step needs no secret and no token.\n          echo ${{ github.ref }}\n"
            "  other:\n    permissions: read-all\n    steps:\n      - run: echo done\n"
        )
        self.assertEqual([], workflow_guard_problems(workflow, ["workflow_dispatch"], set()))


class WorkflowReaderFixtures(unittest.TestCase):
    def test_the_subset_reads_as_dictionaries_lists_and_strings(self) -> None:
        text = (
            "# A comment.\n"
            "name: Example  # trailing\n"
            "on:\n"
            "  push:\n"
            "    branches: [main, 'release/*']\n"
            "  schedule:\n"
            "    - cron: '23 6 * * 1'\n"
            "jobs:\n"
            "  run:\n"
            "    if: ${{ !cancelled() }}\n"
            "    steps:\n"
            '      - name: "Say \\"hi\\""\n'
            "        # A comment inside a mapping.\n"
            "        with:\n"
            "          python-version: '3.11'\n"
            "          empty: ''\n"
            "        run: |\n"
            "          echo one  # kept: a run block has no comments\n"
            "\n"
            "          echo 'two'\n"
            "      - plain\n"
            "      -\n"
            "        nested: value\n"
            "    needs: []\n"
            "    env:\n"
        )
        self.assertEqual(
            {
                "name": "Example",
                "on": {"push": {"branches": ["main", "release/*"]}, "schedule": [{"cron": "23 6 * * 1"}]},
                "jobs": {
                    "run": {
                        "if": "${{ !cancelled() }}",
                        "steps": [
                            {
                                "name": 'Say "hi"',
                                "with": {"python-version": "3.11", "empty": ""},
                                "run": "echo one  # kept: a run block has no comments\n\necho 'two'\n",
                            },
                            "plain",
                            {"nested": "value"},
                        ],
                        "needs": [],
                        "env": None,
                    }
                },
            },
            read_workflow(text),
        )

    def test_yaml_outside_the_subset_is_refused(self) -> None:
        for text, reason in (
            ("a:\n\tb: c\n", "tab"),
            ("a: 1\na: 2\n", "repeated"),
            ("a: {b: c}\n", "outside the subset"),
            ("a: &anchor b\n", "outside the subset"),
            ("a: *anchor\n", "outside the subset"),
            ("a: !!str b\n", "outside the subset"),
            ("a: b: c\n", "outside the subset"),
            ("a: [b, [c]]\n", "flow sequence"),
            ("a: 'b\n", "unclosed"),
            ("a: 'b' c\n", "text after"),
            ("a:\n  b: c\n d: e\n", "indentation"),
            ("a:\n  b: c\n    d: e\n", "indentation"),
            ("just text\n", "expected `key: value`"),
            ("  a: b\n", "column 0"),
        ):
            with self.subTest(text=text), self.assertRaisesRegex(WorkflowSyntaxError, reason):
                read_workflow(text)


if __name__ == "__main__":
    unittest.main()
