"""The deployment pipeline's module layout, as CLAUDE.md describes it, and the kind table every module shares."""

from __future__ import annotations

import unittest

from harness import REPOSITORY_ROOT

from deployer import kinds

PIPELINE_BULLET = "- Any other `deploy.py` invocation runs `deployer/pipeline.py`"
PIPELINE_MODULES = (
    "deployer/kinds.py",
    "deployer/context.py",
    "deployer/report.py",
    "deployer/selection.py",
    "deployer/plan.py",
    "deployer/migrate.py",
)
# The issue's ceiling for the orchestrator once the report, selection, migration, and plan code moved out.
PIPELINE_MAX_LINES = 600


class PipelineLayoutTests(unittest.TestCase):
    def test_claude_md_names_every_pipeline_module(self) -> None:
        lines = (REPOSITORY_ROOT / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
        bullets = [line for line in lines if line.startswith(PIPELINE_BULLET)]
        self.assertEqual(1, len(bullets), "CLAUDE.md has one architecture bullet for the pipeline")
        for module in PIPELINE_MODULES:
            with self.subTest(module=module):
                self.assertIn(f"`{module}`", bullets[0])
                self.assertTrue((REPOSITORY_ROOT / module).is_file())

    def test_the_pipeline_module_stays_an_orchestrator(self) -> None:
        text = (REPOSITORY_ROOT / "deployer" / "pipeline.py").read_text(encoding="utf-8")
        self.assertLessEqual(len(text.splitlines()), PIPELINE_MAX_LINES)

    def test_the_kind_table_names_each_kind_once_in_manifest_order(self) -> None:
        # The manifest keys and journal roots are durable state in every home, so they are pinned against literals.
        self.assertEqual(
            [
                ("skills", "", "skill", "Skill", "claude", True),
                ("shared", "shared asset", "shared asset", "Shared asset", "claude", False),
                ("wrappers", "runtime adapter", "runtime adapter", "Runtime adapter", "agents", True),
                ("agents", "agent", "agent", "Agent", "claude-agents", False),
            ],
            [(kind.key, kind.label, kind.noun, kind.title, kind.root, kind.directory) for kind in kinds.KINDS],
        )
        self.assertEqual({kind.label: kind for kind in kinds.KINDS}, kinds.BY_LABEL)


if __name__ == "__main__":
    unittest.main()
