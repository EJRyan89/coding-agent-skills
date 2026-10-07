"""Fixture tests for tests/validation/workflows.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from workflows import workflow_guard_problems


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
        ):
            with self.subTest(problem=problem, drifted=drifted):
                problems = workflow_guard_problems(drifted, expected, {"actions/checkout"})
                self.assertTrue(problems)
                self.assertTrue(any(problem in found for found in problems), problems)


if __name__ == "__main__":
    unittest.main()
