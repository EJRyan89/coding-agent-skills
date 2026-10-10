"""The deployer invariants every deployer suite holds through the harness, and the ones no other suite reaches.

The harness compares the source tree around every deployment a test runs, and the home around every refusal; the
tests here show each comparison failing on a deployment that breaks its invariant and passing on one that keeps it.
"""

from __future__ import annotations

import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from harness import DeployerTestCase, tree_state

from deployer import pipeline, render
from deployer.errors import DeployError

REFUSAL = "ERROR: synthetic refusal"


class TreeStateTests(DeployerTestCase):
    def tree(self, name: str) -> Path:
        tree = self.root / name
        self.write(tree / "a" / "file.txt", "one\n")
        self.write(tree / "b.txt", "two\n")
        return tree

    def test_a_changed_byte_an_added_directory_and_a_removed_file_each_change_the_state(self) -> None:
        unchanged = tree_state(self.tree("unchanged"))
        self.assertEqual({"a", "a/file.txt", "b.txt"}, set(unchanged))
        self.assertEqual(unchanged, tree_state(self.tree("identical")))
        changes = {
            "a byte": lambda tree: self.write(tree / "a" / "file.txt", "One\n"),
            "a directory": lambda tree: (tree / "a" / "empty").mkdir(),
            "a removed file": lambda tree: (tree / "b.txt").unlink(),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                tree = self.tree(name)
                change(tree)
                self.assertNotEqual(unchanged, tree_state(tree))


def failing_after_applying() -> mock._patch:
    """Make the deployment fail with REFUSAL once it has applied its plan: a refusal moved after the mutation."""
    apply = pipeline._apply

    def apply_then_refuse(*arguments: Any) -> None:
        apply(*arguments)
        raise DeployError(REFUSAL)

    return mock.patch("deployer.pipeline._apply", side_effect=apply_then_refuse)


def rendering_then(action: Callable[[], object]) -> mock._patch:
    """Run action as the deployment renders, as a renderer that wrote into its source would."""
    real_render = render.render

    def render_then_act(*arguments: Any) -> render.Staged:
        staged = real_render(*arguments)
        action()
        return staged

    return mock.patch("deployer.render.render", side_effect=render_then_act)


class RefusalBeforeMutationGuardTests(DeployerTestCase):
    """deploy_fails holds a refusal to the home it found, whatever the refusal's message."""

    def setUp(self) -> None:
        super().setUp()
        self.make_source_json()
        self.make_skill("alpha", "Alpha at {{HOME}}", ["HOME"])
        self.make_config()

    def test_a_refusal_raised_before_the_deployment_changes_anything_passes(self) -> None:
        with mock.patch("deployer.pipeline._apply", side_effect=DeployError(REFUSAL)):
            self.deploy_fails("--all", pattern=REFUSAL)
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_a_refusal_raised_after_the_deployment_changed_the_home_fails_the_test(self) -> None:
        with failing_after_applying(), self.assertRaises(AssertionError) as caught:
            self.deploy_fails("--all", pattern=REFUSAL)
        self.assertIn("the refusal changed the home", str(caught.exception))
        self.assertTrue((self.skills_dir / "alpha" / "SKILL.md").is_file())

    def test_a_failure_that_states_why_it_changes_the_home_passes(self) -> None:
        with failing_after_applying():
            self.deploy_fails("--all", pattern=REFUSAL, changes_home="the run fails after it installed alpha")
        self.assertTrue((self.skills_dir / "alpha" / "SKILL.md").is_file())

    def test_a_refusal_into_another_home_is_held_to_that_home(self) -> None:
        other = self.root / "canary home"
        other.mkdir()
        with failing_after_applying(), self.assertRaises(AssertionError) as caught:
            self.deploy_fails("--all", "--canary-home", str(other), pattern=REFUSAL, home=other)
        self.assertIn("the refusal changed the home", str(caught.exception))


class SourceImmutabilityGuardTests(DeployerTestCase):
    """Every deployment a test runs through the harness leaves its source byte for byte as it found it."""

    def setUp(self) -> None:
        super().setUp()
        self.make_source_json(shared_assets={"shared-doc.md": "owner"})
        self.make_skill("alpha", "Alpha at {{HOME}}", ["HOME"], ["shared-doc.md"])
        self.make_shared_asset("shared-doc.md", "Shared at {{HOME}}")
        self.make_config()

    def test_a_deployment_that_renders_without_touching_its_source_passes(self) -> None:
        before = tree_state(self.source)
        self.deploy_ok("--all")
        self.deploy_ok("--all", "--dry-run")
        self.assertEqual(before, tree_state(self.source))
        self.assertIn("{{HOME}}", (self.source / "skills" / "alpha" / "SKILL.md").read_text(encoding="utf-8"))

    def test_a_deployment_that_rewrites_a_template_fails_the_test(self) -> None:
        template = self.source / "skills" / "shared-doc.md"
        for arguments in (("--all",), ("--all", "--dry-run")):
            with self.subTest(arguments=arguments):
                self.make_shared_asset("shared-doc.md", "Shared at {{HOME}}")
                rewrite = rendering_then(lambda: template.write_bytes(b"Shared at rendered\n"))
                with rewrite, self.assertRaises(AssertionError) as caught:
                    self.deploy(*arguments)
                self.assertIn("the deployment changed its source", str(caught.exception))

    def test_a_deployment_that_leaves_a_file_in_its_source_fails_the_test(self) -> None:
        cache = self.source / "skills" / "alpha" / "__pycache__"

        def leave_bytecode() -> None:
            cache.mkdir()
            (cache / "tool.cpython-311.pyc").write_bytes(b"\x00")

        with rendering_then(leave_bytecode), self.assertRaises(AssertionError) as caught:
            self.deploy("--all")
        self.assertIn("the deployment changed its source", str(caught.exception))


class PermanentBackupTests(DeployerTestCase):
    """A permanent backup, .backups/<run-id>/<name>, survives every later deployment and the uninstall."""

    def setUp(self) -> None:
        super().setUp()
        self.make_source_json()
        self.make_agent("reviewer", "Review the change.")
        self.make_skill("alpha", "Alpha at {{HOME}}", ["HOME"], agent_deps=["reviewer"])
        self.make_config()

    def backups(self) -> dict[Path, dict[str, str]]:
        return {
            root: tree_state(root / ".backups")
            for root in (self.skills_dir, self.claude_agents_dir)
            if (root / ".backups").is_dir()
        }

    def force_over_an_edit(self, edit: str) -> str:
        self.append(self.skills_dir / "alpha" / "SKILL.md", f"{edit}\n")
        self.append(self.claude_agents_dir / "reviewer.md", f"{edit}\n")
        self.deploy_ok("--all", "--force-item", "alpha", "--force-item", "reviewer")
        return str(self.manifest()["last_run_id"])

    def test_permanent_backups_survive_later_deployments_and_the_uninstall(self) -> None:
        self.deploy_ok("--all")
        first = self.force_over_an_edit("# first edit")
        kept = self.backups()
        self.assertEqual({self.skills_dir, self.claude_agents_dir}, set(kept))
        self.assertIn(f"{first}/alpha/SKILL.md", kept[self.skills_dir])
        self.assertIn(f"{first}/reviewer.md", kept[self.claude_agents_dir])
        second = self.force_over_an_edit("# second edit")
        self.assertNotEqual(first, second)
        after_second = self.backups()
        for root, state in kept.items():
            self.assertLessEqual(state.items(), after_second[root].items(), root)
        self.make_skill("alpha", "Updated alpha at {{HOME}}", ["HOME"], agent_deps=["reviewer"])
        self.deploy_ok("--all")
        self.assertEqual(after_second, self.backups())
        self.deploy_ok(stdin="none\n")
        self.assertFalse((self.skills_dir / "alpha").exists())
        self.assertFalse((self.claude_agents_dir / "reviewer.md").exists())
        self.assertEqual(after_second, self.backups())
        self.assertIn(
            "# first edit",
            (self.skills_dir / ".backups" / first / "alpha" / "SKILL.md").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
