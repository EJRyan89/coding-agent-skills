"""Fixture tests for tests/validation/upgrade_notes.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from upgrade_notes import upgrade_notes_problems
from validation_support import write_fixture_tree


class UpgradeNotesPolicy(unittest.TestCase):
    RELEASING = (
        "# Releasing\n\n## Versioning\n\n**Contracts**:\n\n1. **Durable user data.** Records.\n\n"
        "**Levels**, from `1.0.0` on:\n\n- **Patch.** Nothing to do.\n- **Minor.** Something optional.\n"
        "- **Major.** The user must act.\n\n**Before `1.0.0`**, the minor level carries more.\n\n## Before tagging\n"
    )

    RELEASED: ClassVar[dict[str, str]] = {
        "deployer/manifest.py": "MANIFEST_VERSION = 7\nOLDEST_READABLE_VERSION = 6\n",
        "deployer/tools.py": 'MINIMUM_PYTHON = (3, 11)\nGH = Tool("gh", "gh", find, minimum=(2, 48, 0))\n',
        "deploy-meta/alpha.json": '{"required_vars": ["ALPHA"], "tools": []}\n',
        "skills/alpha/SKILL.md": "alpha\n",
        "skills/code-review-core/references/record.schema.json": '{"type": "object"}\n',
        "docs/code-review-operations-contract.md": "# Contract\n\n## Behavior\n\n| Skill | Output |\n| --- | --- |\n"
        "| a | b |\n\n## Formats\n\n### Record\n\n| Field | Type |\n| --- | --- |\n| `id` | string |\n",
        "docs/releasing.md": RELEASING,
        "docs/upgrade-notes.md": "# Upgrade notes\n\n## Unreleased\n",
    }

    CONTRACT_CHANGES: ClassVar[dict[str, dict[str, str]]] = {
        "deployer/manifest.py": {"deployer/manifest.py": "MANIFEST_VERSION = 8\nOLDEST_READABLE_VERSION = 6\n"},
        "deploy-meta/alpha.json": {"deploy-meta/alpha.json": '{"required_vars": ["ALPHA", "BETA"], "tools": []}\n'},
        "skills/code-review-core/references/record.schema.json": {
            "skills/code-review-core/references/record.schema.json": '{"type": "array"}\n'
        },
        "docs/code-review-operations-contract.md": {
            "docs/code-review-operations-contract.md": RELEASED["docs/code-review-operations-contract.md"]
            + "| `name` | string |\n"
        },
        "skills/beta": {"skills/beta/SKILL.md": "beta\n"},
        "deployer/tools.py": {
            "deployer/tools.py": 'MINIMUM_PYTHON = (3, 12)\nGH = Tool("gh", "gh", find, minimum=(2, 48, 0))\n'
        },
    }

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / "repository with spaces"
        self.root.mkdir()
        (self.home / "empty").write_text("", encoding="utf-8")
        environment = {
            "GIT_CONFIG_GLOBAL": str(self.home / "empty"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *arguments: str, cwd: Path | None = None) -> None:
        subprocess.run(["git", "-C", str(cwd or self.root), *arguments], check=True, capture_output=True)

    def release(self, files: Mapping[str, str] | None = None, tag: str | None = "v0.1.0") -> None:
        """Commit the released tree, tag it, and point origin/main at it, as a fetched clone has it."""
        write_fixture_tree(self.root, files if files is not None else self.RELEASED)
        self.git("init", "-q", "-b", "main")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "release")
        if tag:
            self.git("tag", tag)
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")

    def restore(self) -> None:
        self.git("checkout", "--", ".")
        self.git("clean", "-q", "-f", "-d")

    def entry(self, contract: str, level: str = "minor", action: str = "none", pull_request: str = "#7") -> str:
        return (
            f"\n### A contract changed\n\n- Level: {level}.\n- Contract: {contract}\n"
            f"- User action: {action}\n- Pull request: {pull_request}\n"
        )

    def notes(self, *entries: str) -> dict[str, str]:
        return {"docs/upgrade-notes.md": self.RELEASED["docs/upgrade-notes.md"] + "".join(entries)}

    def problems(self, files: Mapping[str, str]) -> list[str]:
        write_fixture_tree(self.root, files)
        return upgrade_notes_problems(self.root)

    def test_a_contract_change_without_an_entry_fails_and_names_the_item(self) -> None:
        self.release()
        for item, change in self.CONTRACT_CHANGES.items():
            with self.subTest(item=item):
                problems = self.problems(change)
                self.assertEqual(1, len(problems), problems)
                self.assertTrue(problems[0].startswith(f"{item}: "), problems[0])
                self.assertIn("changed since v0.1.0", problems[0])
                self.assertIn(f"naming `{item}`", problems[0])
                self.assertIn("patch, minor, or major", problems[0])
                self.assertIn("the user action or none, and the pull request", problems[0])
                self.restore()

    def test_a_contract_change_with_a_new_entry_passes(self) -> None:
        self.release()
        for item, change in self.CONTRACT_CHANGES.items():
            with self.subTest(item=item):
                self.assertEqual([], self.problems({**change, **self.notes(self.entry(f"`{item}`"))}))
                self.restore()

    def test_one_entry_may_name_several_items(self) -> None:
        self.release()
        changes = {**self.CONTRACT_CHANGES["skills/beta"], **self.CONTRACT_CHANGES["deployer/tools.py"]}
        self.assertEqual([], self.problems({**changes, **self.notes(self.entry("`skills/beta`, `deployer/tools.py`"))}))

    def test_removing_a_skill_names_its_directory_and_its_required_variables(self) -> None:
        self.release()
        shutil.rmtree(self.root / "skills" / "alpha")
        (self.root / "deploy-meta" / "alpha.json").unlink()
        self.assertEqual(
            ["deploy-meta/alpha.json", "skills/alpha"],
            sorted(problem.split(":", 1)[0] for problem in upgrade_notes_problems(self.root)),
        )

    def test_a_change_outside_the_contract_values_needs_no_entry(self) -> None:
        self.release()
        contract = self.RELEASED["docs/code-review-operations-contract.md"]
        self.assertEqual(
            [],
            self.problems(
                {
                    "deployer/manifest.py": self.RELEASED["deployer/manifest.py"]
                    + "\n\ndef read() -> None:\n    pass\n",
                    "deployer/tools.py": "# The tools.\n" + self.RELEASED["deployer/tools.py"],
                    "deploy-meta/alpha.json": '{"required_vars": ["ALPHA"], "tools": ["gh"]}\n',
                    "deploy-meta/gamma.json": '{"required_vars": [], "tools": []}\n',
                    "skills/alpha/scripts/run.py": "pass\n",
                    "skills/code-review-core/references/record.schema.json": '{\n  "type": "object"\n}\n',
                    "skills/code-review-core/references/template.md": "a template\n",
                    "docs/code-review-operations-contract.md": contract.replace("| a | b |", "| a | c |").replace(
                        "| `id` | string |", "| `id`   |   string |"
                    ),
                    "docs/guide.md": "a guide\n",
                }
            ),
        )

    def test_an_entry_released_with_the_tag_does_not_count(self) -> None:
        self.release({**self.RELEASED, **self.notes(self.entry("`deployer/manifest.py`"))})
        problems = self.problems(self.CONTRACT_CHANGES["deployer/manifest.py"])
        self.assertEqual(["deployer/manifest.py"], [problem.split(":", 1)[0] for problem in problems])

    def test_a_malformed_entry_fails_by_field(self) -> None:
        self.release()
        change = self.CONTRACT_CHANGES["deployer/manifest.py"]
        cases = {
            "breaking": (
                self.entry("`deployer/manifest.py`", level="breaking"),
                "starts its Level with 'breaking', not one of the levels in the Versioning section of "
                "docs/releasing.md: patch, minor, or major",
            ),
            "action": (
                self.entry("`deployer/manifest.py`", action=""),
                "is missing '- User action:' or leaves it empty",
            ),
            "pull request": (
                self.entry("`deployer/manifest.py`", pull_request="soon"),
                "names no pull request as #N in '- Pull request:'",
            ),
            "contract": (self.entry("the manifest"), "names no contract item in backticks, nor none, in '- Contract:'"),
        }
        for name, (entry, expected) in cases.items():
            with self.subTest(case=name):
                problems = self.problems({**change, **self.notes(entry)})
                self.assertIn(f"docs/upgrade-notes.md entry 'A contract changed' {expected}", problems)

    def test_an_entry_tied_to_no_file_names_none(self) -> None:
        self.release()
        self.assertEqual([], self.problems(self.notes(self.entry("none", action="pass --cross-major once"))))

    def test_no_tag_reachable_from_origin_main_passes(self) -> None:
        self.release(tag=None)
        self.assertEqual([], self.problems(self.CONTRACT_CHANGES["deployer/manifest.py"]))

    def test_a_missing_origin_main_fails_with_the_fetch_command(self) -> None:
        self.release()
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        self.assertEqual(
            [
                "origin/main is missing, so the upgrade-notes check cannot find the last tag; "
                "fetch it with `git fetch origin main --tags`"
            ],
            upgrade_notes_problems(self.root),
        )

    def test_a_shallow_clone_fails_with_the_fetch_command(self) -> None:
        self.release()
        self.git("commit", "-q", "--allow-empty", "-m", "after")
        clone = self.home / "shallow clone"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", self.root.as_uri(), str(clone)], check=True, capture_output=True
        )
        self.assertEqual(
            [
                "this clone is shallow, so the upgrade-notes check cannot see the last tag; "
                "fetch the history with `git fetch --unshallow --tags`"
            ],
            upgrade_notes_problems(clone),
        )


if __name__ == "__main__":
    unittest.main()
