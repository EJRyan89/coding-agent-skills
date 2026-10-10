"""Fixture tests for tests/validation/workflows.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from workflows import WorkflowSyntaxError, leg_problems, read_workflow, tool_cache_problems, workflow_guard_problems


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
            (clean + "      - run: |\n          echo '${{ github.event.pull_request.body }}'\n", "event data"),
            (clean + "        env:\n          TITLE: ${{ github['event']['pull_request']['title'] }}\n", "event data"),
            (clean + '      - run: echo "${{ github.head_ref }}"\n', "event data"),
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

    def test_workflow_guard_pins_dispatch_and_the_weekly_cron(self) -> None:
        clean = (
            "on:\n  workflow_dispatch:\n    inputs:\n      version:\n        type: string\n"
            "  schedule:\n    - cron: '23 6 * * 2'\n\npermissions:\n  contents: read\n\njobs:\n  run:\n"
            "    steps:\n      - run: echo done\n"
        )
        expected = ["workflow_dispatch", "schedule"]
        self.assertEqual([], workflow_guard_problems(clean, expected, set(), ("23 6 * * 2",)))
        for drifted, problem in (
            (clean.replace("  schedule:\n", "  push:\n  schedule:\n"), "triggers"),
            (clean.replace("  schedule:\n", "  pull_request:\n  schedule:\n"), "triggers"),
            (clean.replace("  schedule:\n    - cron: '23 6 * * 2'\n", ""), "triggers"),
            (clean.replace("* * 2'", "* * 1'"), "crons"),
            (clean.replace("* * 2'\n", "* * 2'\n    - cron: '23 6 * * 5'\n"), "crons"),
        ):
            with self.subTest(problem=problem, drifted=drifted):
                problems = workflow_guard_problems(drifted, expected, set(), ("23 6 * * 2",))
                self.assertTrue(any(problem in found for found in problems), problems)
        # A schedule the caller did not expect is drift too.
        self.assertTrue(
            any("crons" in found for found in workflow_guard_problems(clean, expected, set())),
        )

    def test_a_read_only_job_and_text_that_only_mentions_secrets_pass(self) -> None:
        workflow = (
            "on:\n  workflow_dispatch:\n\npermissions:\n  contents: read\n\njobs:\n  run:\n"
            "    permissions:\n      contents: read\n      checks: none\n    steps:\n"
            "      - run: |\n          # This step needs no secret and no token.\n          echo ${{ github.ref }}\n"
            "  other:\n    permissions: read-all\n    steps:\n      - run: echo done\n"
        )
        self.assertEqual([], workflow_guard_problems(workflow, ["workflow_dispatch"], set()))


LEGS = (
    "env:\n  TOOL_VERSION: '1.2.3'\n\njobs:\n  suite:\n    runs-on: windows-latest\n    strategy:\n      matrix:\n"
    "        shard: ['1/2', '2/2']\n    steps:\n"
    "      - uses: actions/setup-python@SHA # v1\n        with:\n          cache: pip\n"
    "          cache-dependency-path: requirements-dev.txt\n"
    "      - uses: actions/cache@SHA # v1\n        with:\n          key: tool-${{ env.TOOL_VERSION }}\n"
    "      - run: install tool --version $env:TOOL_VERSION --quiet\n"
    "      - name: Validate\n        env:\n          SHARD: ${{ matrix.shard }}\n"
    "        run: python tests/run_validation.py --shard $env:SHARD\n"
    "  validate:\n    needs: suite\n"
)


class LegFixtures(unittest.TestCase):
    def test_each_leg_runs_its_own_shard_and_the_aggregate_needs_every_leg(self) -> None:
        self.assertEqual([], leg_problems(LEGS, 2))
        for drifted, problem in (
            (LEGS, "expected ['1/3', '2/3', '3/3']"),
            (LEGS.replace("['1/2', '2/2']", "['1/2', '1/2']"), "shards"),
            (LEGS.replace("runs-on: windows-latest", "runs-on: ubuntu-latest"), "not windows-latest"),
            (LEGS.replace(" --shard $env:SHARD", ""), "does not run its leg's --shard"),
            (LEGS.replace("SHARD: ${{ matrix.shard }}", "SHARD: 1/2"), "does not run its leg's --shard"),
            (LEGS.replace("tests/run_validation.py", "tests/other.py"), "no step runs"),
            (LEGS.replace("    needs: suite\n", "    needs: other\n"), "does not need the suite"),
        ):
            with self.subTest(problem=problem):
                count = 3 if problem.startswith("expected") else 2
                problems = leg_problems(drifted, count)
                self.assertTrue(any(problem in found for found in problems), problems)

    def test_each_tool_cache_is_keyed_on_a_workflow_pin_its_install_reads(self) -> None:
        caches = {"tool-${{ env.TOOL_VERSION }}": "install tool --version $env:TOOL_VERSION "}
        self.assertEqual([], tool_cache_problems(LEGS, caches))
        for drifted, problem in (
            (LEGS.replace("key: tool-${{ env.TOOL_VERSION }}", "key: tool-1.2.3"), "the cache keys are"),
            (LEGS.replace("env:\n  TOOL_VERSION: '1.2.3'\n", "env:\n  OTHER: '1'\n"), "does not pin"),
            (LEGS.replace("--version $env:TOOL_VERSION", "--version 1.2.3"), "no step runs"),
            (LEGS.replace("          cache: pip\n", ""), "setup-python does not cache pip"),
            (LEGS.replace("requirements-dev.txt", "requirements.txt"), "setup-python does not cache pip"),
        ):
            with self.subTest(problem=problem):
                problems = tool_cache_problems(drifted, caches)
                self.assertTrue(any(problem in found for found in problems), problems)


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
