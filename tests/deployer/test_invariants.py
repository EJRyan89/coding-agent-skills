"""The deployer invariants every deployer suite holds through the harness, and the ones no other suite reaches.

The harness compares the source tree around every deployment a test runs, and the home around every refusal; the
tests here show each comparison failing on a deployment that breaks its invariant and passing on one that keeps it.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from harness import FAILS_PARTWAY, DeployerTestCase, tree_state

from deployer import cli, pipeline, platform_support, render
from deployer.errors import DeployError
from deployer.paths import Paths

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


class CLIImmutabilityGuardTests(DeployerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()

    def test_a_cli_refusal_before_mutation_passes(self) -> None:
        result = self.run_cli("--include", "alpha")
        self.assertEqual(2, result.code, result.output)
        self.assertIn("--include can only be used with --all", result.output)

    def test_a_cli_refusal_after_mutating_the_home_fails_the_test(self) -> None:
        with failing_after_applying(), self.assertRaisesRegex(AssertionError, "the refusal changed the home"):
            self.run_cli("--all")
        self.assertTrue((self.skills_dir / "alpha" / "SKILL.md").is_file())

    def test_a_cli_failure_that_states_why_it_changes_the_home_passes(self) -> None:
        with failing_after_applying():
            result = self.run_cli("--all", changes_home="the run fails after it installed alpha")
        self.assertEqual(1, result.code, result.output)
        self.assertIn(REFUSAL, result.output)

    def test_a_cli_refusal_cannot_change_an_existing_byte_in_either_tree(self) -> None:
        for tree, message in (
            (self.source, "the deployment changed its source"),
            (self.home, "the refusal changed the home"),
        ):
            with self.subTest(tree=tree.name):
                path = tree / "existing.txt"
                path.write_bytes(b"before\n")

                def refuse(*_arguments: Any, target: Path = path) -> int:
                    target.write_bytes(b"Before\n")
                    return 2

                with (
                    mock.patch.object(cli, "main", side_effect=refuse),
                    self.assertRaisesRegex(AssertionError, message),
                ):
                    self.run_cli("--bogus")

    def test_changes_home_never_allows_a_cli_refusal_to_mutate_its_source(self) -> None:
        with (
            rendering_then(lambda: self.write(self.source / "unexpected.txt", "mutation\n")),
            failing_after_applying(),
            self.assertRaisesRegex(AssertionError, "the deployment changed its source"),
        ):
            self.run_cli("--all", changes_home="the run fails after it installed alpha")

    def test_a_cli_refusal_checks_the_alternate_source_and_canary_home(self) -> None:
        alternate = self.snapshot_source("alternate")
        canary = self.root / "canary"
        canary.mkdir()
        for tree, message in (
            (alternate, "the deployment changed its source"),
            (canary, "the refusal changed the home"),
        ):
            with self.subTest(tree=tree.name):

                def refuse(_arguments: list[str], paths: Paths, _stdin: object, target: Path = tree) -> int:
                    self.assertEqual(self.home, paths.home)
                    self.assertEqual(alternate, paths.source_dir)
                    self.write(target / "unexpected.txt", "mutation\n")
                    return 1

                with (
                    mock.patch.object(cli, "main", side_effect=refuse),
                    self.assertRaisesRegex(AssertionError, message),
                ):
                    self.run_cli("--all", "--canary-home", str(canary), source=alternate, refusal_home=canary)

    def test_cli_success_and_cancellation_keep_the_input_stream_semantics(self) -> None:
        for stdin, code in (("1\n", 0), ("", 130)):
            with self.subTest(code=code):
                result = self.run_cli("--dry-run", stdin=stdin)
                self.assertEqual(code, result.code, result.output)

    def test_configure_refusals_use_the_same_guard(self) -> None:
        def refuse(*_arguments: Any, **_options: Any) -> int:
            self.write(self.home / "unexpected.txt", "mutation\n")
            return 1

        with (
            mock.patch("deployer.configure.run", side_effect=refuse),
            self.assertRaisesRegex(AssertionError, "the refusal changed the home"),
        ):
            self.configure()


def killed_after_applying() -> mock._patch:
    """Kill the deployment once it has applied its plan, as SystemExit raised past the pipeline ends it."""
    return mock.patch("deployer.pipeline._finalize", side_effect=SystemExit(1))


class RaisingRunGuardTests(DeployerTestCase):
    """A run that raises, as a killed deployment does, is held to its source and home before the exception reaches the
    test."""

    def setUp(self) -> None:
        super().setUp()
        self.make_source_json()
        self.make_skill("alpha", "Alpha at {{HOME}}", ["HOME"])
        self.make_config()

    def test_a_killed_run_that_rewrites_its_source_fails_the_test(self) -> None:
        with (
            rendering_then(lambda: self.write(self.source / "unexpected.txt", "mutation\n")),
            killed_after_applying(),
            self.assertRaisesRegex(AssertionError, "the deployment changed its source") as caught,
        ):
            self.deploy("--all", changes_home=FAILS_PARTWAY)
        self.assertIsInstance(caught.exception.__context__, SystemExit)

    def test_a_killed_run_that_changes_the_home_without_saying_why_fails_the_test(self) -> None:
        with killed_after_applying(), self.assertRaisesRegex(AssertionError, "the refusal changed the home"):
            self.deploy("--all")
        self.assertTrue((self.skills_dir / "alpha" / "SKILL.md").is_file())

    def test_a_killed_run_that_says_why_it_changes_the_home_raises_its_own_exception(self) -> None:
        with killed_after_applying(), self.assertRaises(SystemExit):
            self.deploy("--all", changes_home=FAILS_PARTWAY)
        self.assertTrue((self.skills_dir / "alpha" / "SKILL.md").is_file())

    def test_a_raising_refusal_that_changes_the_home_fails_the_test(self) -> None:
        def refuse(*_arguments: Any) -> int:
            self.write(self.home / "unexpected.txt", "mutation\n")
            raise RuntimeError("synthetic escape")

        with (
            mock.patch.object(cli, "main", side_effect=refuse),
            self.assertRaisesRegex(AssertionError, "the refusal changed the home"),
        ):
            self.run_cli("--all")


class CheckoutProgramGuardTests(DeployerTestCase):
    """A program run against a copy of this checkout is held to the same comparisons as a deployment."""

    def environment(self) -> dict[str, str]:
        return {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home)}

    def test_a_program_that_leaves_its_checkout_alone_passes_with_its_streams_apart(self) -> None:
        program = "import sys\nprint('out')\nprint('err', file=sys.stderr)\nsys.exit(3)\n"
        result = self.run_in_checkout(program, environment=self.environment())
        self.assertEqual((3, "out\n", "err\n"), (result.code, result.stdout, result.stderr))

    def test_a_program_that_writes_into_its_checkout_fails_the_test(self) -> None:
        program = "import pathlib, sys\n(pathlib.Path(sys.argv[1]) / 'unexpected.txt').write_text('mutation')\n"
        with self.assertRaisesRegex(AssertionError, "the deployment changed its source"):
            self.run_in_checkout(program, environment=self.environment())

    def test_a_failing_program_that_changes_the_home_without_saying_why_fails_the_test(self) -> None:
        program = "import pathlib, sys\n(pathlib.Path(sys.argv[2]) / 'unexpected.txt').write_text('x')\nsys.exit(1)\n"
        with self.assertRaisesRegex(AssertionError, "the refusal changed the home"):
            self.run_in_checkout(program, str(self.home), environment=self.environment())


class FreshHomeGuardTests(DeployerTestCase):
    """The fixture home starts empty, as a fresh profile folder does, so a refusal that makes a root is seen."""

    def setUp(self) -> None:
        super().setUp()
        self.make_source_json()
        self.make_skill("alpha", "Alpha")

    def test_the_fixture_home_starts_empty(self) -> None:
        self.assertEqual({}, tree_state(self.home))

    def test_a_refusal_that_makes_a_managed_root_fails_the_test(self) -> None:
        def refuse(*_arguments: Any) -> int:
            (self.home / ".claude" / "skills").mkdir(parents=True)
            return 1

        with (
            mock.patch.object(cli, "main", side_effect=refuse),
            self.assertRaisesRegex(AssertionError, "the refusal changed the home"),
        ):
            self.run_cli("--all")

    def test_a_cancelled_configure_leaves_a_fresh_home_empty(self) -> None:
        result = self.configure(stdin="")
        self.assertEqual(130, result.code, result.output)
        self.assertEqual({}, tree_state(self.home))


class EntryPointGuardTests(DeployerTestCase):
    """deploy.py run as a program runs from a copy of this checkout, which the harness compares around the run."""

    def test_an_entry_point_run_that_changes_its_checkout_fails_the_test(self) -> None:
        def write_beside(arguments: list[str], _environment: object = None) -> platform_support.ToolResult:
            Path(arguments[2]).with_name("unexpected.txt").write_bytes(b"mutation\n")
            return platform_support.ToolResult(0, "")

        with (
            mock.patch("deployer.platform_support.run_tool", side_effect=write_beside),
            self.assertRaisesRegex(AssertionError, "the deployment changed its source"),
        ):
            self.run_entry_point("--help", environment={})

    def test_an_entry_point_run_names_the_copy_and_not_the_checkout(self) -> None:
        seen: list[str] = []

        def record(arguments: list[str], _environment: object = None) -> platform_support.ToolResult:
            seen.append(arguments[2])
            return platform_support.ToolResult(0, "printed\n")

        with mock.patch("deployer.platform_support.run_tool", side_effect=record):
            result = self.run_entry_point("--help", environment={})
        self.assertEqual((0, "printed\n"), (result.code, result.output))
        self.assertEqual([str(self.root / "repository" / "deploy.py")], seen)


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
