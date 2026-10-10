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

from upgrade_notes import contract_list_problems, upgrade_notes_problems
from validation_support import write_fixture_tree


class UpgradeNotesPolicy(unittest.TestCase):
    RELEASING = (
        "# Releasing\n\n## Versioning\n\n**Contracts**:\n\n1. **Durable user data.** Records.\n\n"
        "**Levels**, from `1.0.0` on:\n\n- **Patch.** Nothing to do.\n- **Minor.** Something optional.\n"
        "- **Major.** The user must act.\n\n**Before `1.0.0`**, the minor level carries more.\n\n## Before tagging\n"
    )
    NOTES = "# Upgrade notes\n\nWhat each release asks of a user.\n\n## v0.1.0\n"

    RELEASED: ClassVar[dict[str, str]] = {
        "deployer/manifest.py": "MANIFEST_VERSION = 7\nOLDEST_READABLE_VERSION = 6\n",
        "deployer/tools.py": 'MINIMUM_PYTHON = (3, 11)\nGH = Tool("gh", "gh", find, minimum=(2, 48, 0))\n',
        "deploy-meta/alpha.json": '{"required_vars": ["ALPHA"], "tools": []}\n',
        "skills/alpha/SKILL.md": "alpha\n",
        "skills/tools/delta/SKILL.md": "delta\n",
        "skills/code-review-core/references/record.schema.json": '{"type": "object"}\n',
        "docs/code-review-operations-contract.md": "# Contract\n\n## Behavior\n\n| Skill | Output |\n| --- | --- |\n"
        "| a | b |\n\n## Formats\n\n### Record\n\n| Field | Type |\n| --- | --- |\n| `id` | string |\n",
        "docs/releasing.md": RELEASING,
        "docs/upgrade-notes.md": NOTES,
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
        # A skill added to a category the release already has is its own item; the category is not a skill.
        "skills/tools/gamma": {"skills/tools/gamma/SKILL.md": "gamma\n"},
        "deployer/tools.py": {
            "deployer/tools.py": 'MINIMUM_PYTHON = (3, 12)\nGH = Tool("gh", "gh", find, minimum=(2, 48, 0))\n'
        },
    }
    MANIFEST_CHANGE = CONTRACT_CHANGES["deployer/manifest.py"]

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

    def commit(self, message: str, files: Mapping[str, str] | None = None) -> None:
        write_fixture_tree(self.root, files or {})
        self.git("add", ".")
        message_file = self.home / "message.txt"
        message_file.write_bytes(message.encode())
        self.git("commit", "-q", "--allow-empty", "-F", str(message_file))

    def release(
        self, files: Mapping[str, str] | None = None, tag: str | None = "v0.1.0", message: str = "release"
    ) -> None:
        """Commit the released tree, tag it, and point origin/main at it, as a fetched clone has it."""
        self.git("init", "-q", "-b", "main")
        self.commit(message, files if files is not None else self.RELEASED)
        if tag:
            self.git("tag", tag)
        self.merged()

    def merged(self) -> None:
        """Point origin/main at HEAD, as a squash merge of the branch so far would."""
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")

    def restore(self) -> None:
        self.git("checkout", "--", ".")
        self.git("clean", "-q", "-f", "-d")

    def entry(self, contract: str, level: str = "minor", action: str = "none", pull_request: str = "") -> str:
        return f"\n### A contract changed\n\n- Level: {level}.\n- Contract: {contract}\n- User action: {action}\n" + (
            f"- Pull request: {pull_request}\n" if pull_request else ""
        )

    def body(self, *entries: str) -> str:
        """A body from the template: the entries under ## Upgrade note, or None, and the sections around it."""
        note = "".join(entries) or "\nNone\n"
        return (
            "Closes #7\r\n\r\nWhy.\r\n\r\n## Implications\r\n\r\nNone\r\n\r\n## Upgrade note\r\n\r\n"
            "<!-- ### An example in a comment\r\n- Level: minor.\r\n- Contract: `deployer/manifest.py` -->\r\n"
            + note.replace("\n", "\r\n")
            + "\r\n## Validation\r\n\r\n- [x] Validation passes\r\n"
        )

    def problems(self, files: Mapping[str, str], body: str | None = None) -> list[str]:
        write_fixture_tree(self.root, files)
        return upgrade_notes_problems(self.root, body)

    def test_a_contract_change_without_an_upgrade_note_fails_and_names_the_item(self) -> None:
        self.release()
        for body in (None, self.body()):
            for item, change in self.CONTRACT_CHANGES.items():
                with self.subTest(item=item, body=body is not None):
                    problems = self.problems(change, body)
                    self.assertEqual(1, len(problems), problems)
                    self.assertTrue(problems[0].startswith(f"{item}: "), problems[0])
                    self.assertIn("changed since v0.1.0, and no upgrade note since then names it", problems[0])
                    self.assertIn(f"under ## Upgrade note in the pull request body naming `{item}`", problems[0])
                    self.assertIn("patch, minor, or major", problems[0])
                    self.assertIn("--pr-body <file>, and otherwise the branch's commit messages", problems[0])
                    self.restore()

    def test_a_contract_change_with_an_entry_in_the_pull_request_body_passes(self) -> None:
        self.release()
        for item, change in self.CONTRACT_CHANGES.items():
            with self.subTest(item=item):
                self.assertEqual([], self.problems(change, self.body(self.entry(f"`{item}`"))))
                self.restore()

    def test_without_a_body_an_entry_in_the_branchs_commit_message_passes(self) -> None:
        self.release()
        self.commit(f"Change the manifest\n\n{self.body(self.entry('`deployer/manifest.py`'))}", self.MANIFEST_CHANGE)
        self.assertEqual([], upgrade_notes_problems(self.root))

    def test_with_a_body_the_branchs_own_commit_messages_do_not_count(self) -> None:
        # The body, not the branch's commits, becomes the squash commit's message, so the entry must be there.
        self.release()
        self.commit(f"Change the manifest\n\n{self.body(self.entry('`deployer/manifest.py`'))}", self.MANIFEST_CHANGE)
        problems = upgrade_notes_problems(self.root, self.body())
        self.assertEqual(["deployer/manifest.py"], [problem.split(":", 1)[0] for problem in problems])

    def test_an_entry_merged_into_origin_main_since_the_tag_counts_for_a_later_pull_request(self) -> None:
        self.release()
        self.commit(
            f"Change the manifest (#8)\n\n{self.body(self.entry('`deployer/manifest.py`'))}", self.MANIFEST_CHANGE
        )
        self.merged()
        self.assertEqual([], upgrade_notes_problems(self.root, self.body()))

    def test_an_entry_written_by_hand_under_unreleased_before_the_move_still_counts(self) -> None:
        self.release()
        hand = self.entry("`deployer/manifest.py`", pull_request="#8")
        unreleased = self.NOTES.replace("## v0.1.0", f"## Unreleased\n{hand}\n## v0.1.0")
        self.commit("Change the manifest (#8)", {**self.MANIFEST_CHANGE, "docs/upgrade-notes.md": unreleased})
        self.commit("Move the notes into pull request bodies (#9)", {"docs/upgrade-notes.md": self.NOTES})
        self.merged()
        self.assertEqual([], upgrade_notes_problems(self.root, self.body()))

    def test_one_entry_may_name_several_items(self) -> None:
        self.release()
        changes = {**self.CONTRACT_CHANGES["skills/beta"], **self.CONTRACT_CHANGES["deployer/tools.py"]}
        self.assertEqual([], self.problems(changes, self.body(self.entry("`skills/beta`, `deployer/tools.py`"))))

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
        self.release(message=f"release\n\n{self.body(self.entry('`deployer/manifest.py`'))}")
        problems = self.problems(self.MANIFEST_CHANGE, self.body())
        self.assertEqual(["deployer/manifest.py"], [problem.split(":", 1)[0] for problem in problems])

    def test_a_malformed_entry_fails_by_field(self) -> None:
        self.release()
        where = "the pull request body's ## Upgrade note entry 'A contract changed'"
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
                self.assertIn(f"{where} {expected}", self.problems(self.MANIFEST_CHANGE, self.body(entry)))

    def test_an_entry_written_by_hand_must_name_its_pull_request(self) -> None:
        self.release()
        unreleased = self.NOTES.replace("## v0.1.0", f"## Unreleased\n{self.entry('none')}\n## v0.1.0")
        self.commit("Fix a thing", {"docs/upgrade-notes.md": unreleased})
        self.commit("Move the notes", {"docs/upgrade-notes.md": self.NOTES})
        problems = upgrade_notes_problems(self.root)
        self.assertEqual(1, len(problems), problems)
        self.assertRegex(
            problems[0],
            r"^docs/upgrade-notes\.md as commit [0-9a-f]{7} left it entry 'A contract changed' is missing "
            r"'- Pull request:' or leaves it empty$",
        )

    def test_an_entry_tied_to_no_file_names_none(self) -> None:
        self.release()
        self.assertEqual([], self.problems({}, self.body(self.entry("none", action="pass --cross-major once"))))

    def test_a_notes_section_that_is_not_a_version_fails(self) -> None:
        self.release()
        for heading in ("Unreleased", "Pending"):
            with self.subTest(heading=heading):
                notes = self.NOTES.replace(
                    "## v0.1.0", f"## {heading}\n{self.entry('none', pull_request='#7')}\n## v0.1.0"
                )
                self.assertEqual(
                    [
                        f"docs/upgrade-notes.md has a '## {heading}' section; an entry goes under ## Upgrade note in "
                        "the pull request body, and tools/release_notes.py writes each version's section from the "
                        "merged commits when it is released"
                    ],
                    self.problems({"docs/upgrade-notes.md": notes}, self.body()),
                )

    def test_no_tag_reachable_from_origin_main_passes(self) -> None:
        self.release(tag=None)
        self.assertEqual([], self.problems(self.MANIFEST_CHANGE))

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


class ContractListPolicy(unittest.TestCase):
    """Each file the upgrade-notes check reads is named in a contract list, so the list and the check agree."""

    NAMED = (
        "`deployer/manifest.py`",
        "`deploy-meta/`",
        "`skills/code-review-core/references/`",
        "`docs/code-review-operations-contract.md`",
        "`skills/`",
        "`deployer/tools.py`",
    )

    def profile(self, names: tuple[str, ...]) -> str:
        items = "".join(f"- {name};\r\n" for name in names)
        return f"# Profile\r\n\r\n## Contract files\r\n\r\n{items}\r\n## Documentation\r\n\r\n- `deployer/tools.py`\r\n"

    def test_a_list_naming_every_file_the_check_reads_passes(self) -> None:
        self.assertEqual([], contract_list_problems("profile.md", self.profile(self.NAMED), "## Contract files"))

    def test_a_file_the_check_reads_but_the_list_leaves_out_fails_by_name(self) -> None:
        # The tool floors are named in the next section only, which does not count.
        problems = contract_list_problems("profile.md", self.profile(self.NAMED[:-1]), "## Contract files")
        self.assertEqual(
            [
                "profile.md does not name `deployer/tools.py` under ## Contract files, though the upgrade-notes "
                "check reads a tool floor from it"
            ],
            problems,
        )

    def test_a_document_without_the_section_fails(self) -> None:
        self.assertEqual(
            ["profile.md has no ## Contract files section naming the contract files the upgrade-notes check reads"],
            contract_list_problems("profile.md", "# Profile\n", "## Contract files"),
        )


if __name__ == "__main__":
    unittest.main()
