"""Fixture tests for tests/validation/upgrade_notes.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import contextlib
import io
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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

import upgrade_notes
from upgrade_notes import Snapshot, changed_contract_items, contract_list_problems, upgrade_notes_problems
from validation_support import write_fixture_tree

from tests import run_validation
from tools import release_notes

# Runs no test of its own; the test functions below check through its assertions, as the classes do.
CHECK = unittest.TestCase()


def snapshot(files: Mapping[str, str]) -> Snapshot:
    return Snapshot(frozenset(files), lambda name: files[name])


def changed(module: str, before: str, after: str) -> list[str]:
    return [item for item, _ in changed_contract_items(snapshot({module: before}), snapshot({module: after}))]


class ContractValueReaders(unittest.TestCase):
    """Each contract value is read however it is written, so a raised floor or a new manifest version is named."""

    def test_a_floor_written_another_way_with_the_same_value_is_no_change(self) -> None:
        self.assertEqual(
            [],
            changed(
                "deployer/tools.py",
                'T = Tool("gh", "gh", locate, minimum=(2, 48, 0))\n',
                '# The tools.\nGH = (2, 48, 0)\nT = Tool(name="gh", label="gh", locate=locate, minimum=GH)\n',
            ),
        )


TOOL = "@dataclass\nclass Tool:\n    name: str\n    label: str\n    locate: object\n    version_arguments: tuple\n"


@pytest.mark.parametrize(
    ("module", "before", "after"),
    [
        pytest.param(
            "deployer/tools.py",
            f'{TOOL}    minimum: tuple = ()\nT = Tool("gh", "gh", locate, ("--version",), (2, 48, 0))\n',
            f'{TOOL}    minimum: tuple = ()\nT = Tool("gh", "gh", locate, ("--version",), (2, 49, 0))\n',
            id="a floor by position",
        ),
        pytest.param(
            "deployer/tools.py",
            'T = Tool(name="gh", label="gh", locate=locate, minimum=(2, 48, 0))\n',
            'T = Tool(name="gh", label="gh", locate=locate, minimum=(2, 49, 0))\n',
            id="every argument by keyword",
        ),
        pytest.param(
            "deployer/tools.py",
            'GH = (2, 48, 0)\nT = Tool("gh", "gh", locate, minimum=GH)\n',
            'GH = (2, 49, 0)\nT = Tool("gh", "gh", locate, minimum=GH)\n',
            id="a floor in a constant",
        ),
        pytest.param(
            "deployer/tools.py",
            "MINIMUM_PYTHON: tuple[int, ...] = (3, 11, 4)\n",
            "MINIMUM_PYTHON: tuple[int, ...] = (3, 12, 0)\n",
            id="an annotated floor",
        ),
        # The control: a floor by keyword, as the check always read one.
        pytest.param(
            "deployer/tools.py",
            'T = Tool("gh", "gh", locate, minimum=(2, 48, 0))\n',
            'T = Tool("gh", "gh", locate, minimum=(2, 49, 0))\n',
            id="a floor by keyword",
        ),
        pytest.param(
            "deployer/manifest.py",
            "MANIFEST_VERSION: int = 8\n",
            "MANIFEST_VERSION: int = 9\n",
            id="an annotated manifest version",
        ),
        pytest.param(
            "deployer/manifest.py",
            "if True:\n    MANIFEST_VERSION = 8\n",
            "if True:\n    MANIFEST_VERSION = 9\n",
            id="a nested manifest version",
        ),
        pytest.param(
            "deployer/manifest.py", "MANIFEST_VERSION = 8\n", "MANIFEST_VERSION = 9\n", id="a manifest version"
        ),
    ],
)
def test_a_floor_by_position_by_keyword_or_in_a_constant_and_an_annotated_or_nested_version_are_read(
    module: str, before: str, after: str
) -> None:
    CHECK.assertEqual([module], changed(module, before, after))


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
        "skills/skill-core/scripts/pdf_tool.py": 'MINIMUMS = {"Xpdf": (4, 6), "Poppler": (26, 10, 0)}\n',
        "deploy-meta/alpha.json": '{"required_vars": ["ALPHA"], "tools": []}\n',
        "skills/alpha/SKILL.md": "alpha\n",
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
        "deployer/tools.py": {
            "deployer/tools.py": 'MINIMUM_PYTHON = (3, 12)\nGH = Tool("gh", "gh", find, minimum=(2, 48, 0))\n'
        },
        "skills/skill-core/scripts/pdf_tool.py": {
            "skills/skill-core/scripts/pdf_tool.py": 'MINIMUMS = {"Xpdf": (4, 7), "Poppler": (26, 10, 0)}\n'
        },
    }
    MANIFEST_CHANGE = CONTRACT_CHANGES["deployer/manifest.py"]

    def test_each_shared_pdf_provider_floor_needs_its_own_upgrade_note(self) -> None:
        self.release()
        item = "skills/skill-core/scripts/pdf_tool.py"
        changed = {item: 'MINIMUMS = {"Xpdf": (4, 6), "Poppler": (26, 11, 0)}\n'}
        self.assertEqual([item], [problem.split(":", 1)[0] for problem in self.problems(changed)])
        self.assertEqual([], self.problems(changed, self.body(self.entry(f"`{item}`"))))

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / "repository with spaces"
        self.root.mkdir()
        # The base's global configuration stays in force, so the fixture's commits start no automatic maintenance.
        environment = {
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

    def run_with_body(self, body: str | None) -> tuple[int, str]:
        body_file = self.home / "pull request body.md"
        if body is not None:
            body_file.write_text(body, encoding="utf-8")
        output = io.StringIO()
        with (
            mock.patch.object(upgrade_notes, "REPOSITORY_ROOT", self.root),
            mock.patch.object(run_validation, "tool_versions", return_value={}),
            mock.patch.object(run_validation, "report_prerequisite_problems", return_value=[]),
            mock.patch.object(run_validation, "all_jobs", return_value=[]),
            mock.patch.object(run_validation, "append_step_summary"),
            contextlib.redirect_stdout(output),
        ):
            arguments = ["-k", "*UpgradeNotesPolicies.test_the_repository_names_every_contract_change*"]
            if body is not None:
                arguments += ["--pr-body", str(body_file)]
            status = run_validation.main(arguments)
        return status, output.getvalue()

    def test_runner_reads_the_body_file_instead_of_the_branch_commit_note(self) -> None:
        self.release()
        self.commit(f"Change the manifest\n\n{self.body(self.entry('`deployer/manifest.py`'))}", self.MANIFEST_CHANGE)
        status, output = self.run_with_body(None)
        self.assertEqual(0, status, output)
        self.assertIn("1 policy checks and 0 suite jobs", output)

        status, output = self.run_with_body(self.body())
        self.assertEqual(1, status, output)
        self.assertIn("deployer/manifest.py: MANIFEST_VERSION or OLDEST_READABLE_VERSION changed", output)
        self.assertIn("no upgrade note since then names it", output)

        status, output = self.run_with_body(self.body(self.entry("`deployer/manifest.py`")))
        self.assertEqual(0, status, output)
        self.assertIn("1 policy checks and 0 suite jobs", output)

    def test_runner_reads_a_note_found_only_in_the_supplied_body_file(self) -> None:
        self.release()
        self.commit("Change the manifest without a commit note", self.MANIFEST_CHANGE)
        status, output = self.run_with_body(None)
        self.assertEqual(1, status, output)
        self.assertIn("no upgrade note since then names it", output)

        status, output = self.run_with_body(self.body(self.entry("`deployer/manifest.py`")))
        self.assertEqual(0, status, output)
        self.assertIn("1 policy checks and 0 suite jobs", output)

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

    def test_a_hard_wrapped_entry_in_a_merged_commit_message_is_read_whole(self) -> None:
        # A squash commit's message may be wrapped, which moves a field's contract item off its label's line; a
        # wrapped entry that truly names no item still fails.
        wrapped = (
            "\n### A contract changed in a commit whose message is\nwrapped\n\n- Level: minor: an additive\nchange.\n"
            "- Contract: the manifest in\n{contract}\nand nothing else.\n- User action: none.\n"
        )
        self.release()
        named = self.body(wrapped.format(contract="`deployer/manifest.py`,"))
        self.commit(f"Change the manifest (#8)\n\n{named}", self.MANIFEST_CHANGE)
        self.merged()
        self.assertEqual([], upgrade_notes_problems(self.root, self.body()))
        self.commit(f"Change it again (#9)\n\n{self.body(wrapped.format(contract='the module,'))}")
        self.merged()
        self.assertRegex(
            "\n".join(upgrade_notes_problems(self.root, self.body())),
            r"commit [0-9a-f]{7}'s ## Upgrade note entry 'A contract changed in a commit whose message is wrapped' "
            r"names no contract item in backticks, nor none, in '- Contract:'",
        )

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
            # A Level that starts with no word at all is held to the same rule, not skipped.
            "numeric": (
                self.entry("`deployer/manifest.py`", level="0.5.0"),
                "starts its Level with '0.5.0.', not one of the levels in the Versioning section of "
                "docs/releasing.md: patch, minor, or major",
            ),
            "punctuation first": (
                self.entry("`deployer/manifest.py`", level="`minor`"),
                "starts its Level with '`minor`.', not one of the levels in the Versioning section of "
                "docs/releasing.md: patch, minor, or major",
            ),
            "word run into a digit": (
                self.entry("`deployer/manifest.py`", level="minor2"),
                "starts its Level with 'minor2.', not one of the levels in the Versioning section of "
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
            "none with prose": (
                self.entry("none, because nothing changed"),
                "names no contract item in backticks, nor none, in '- Contract:'",
            ),
            "none with repeated periods": (
                self.entry("none.."),
                "names no contract item in backticks, nor none, in '- Contract:'",
            ),
        }
        for name, (entry, expected) in cases.items():
            with self.subTest(case=name):
                self.assertIn(f"{where} {expected}", self.problems(self.MANIFEST_CHANGE, self.body(entry)))

    def test_a_level_that_is_a_sentence_starting_with_a_level_word_passes(self) -> None:
        self.release()
        for level in (
            "minor",
            "Patch. A fix",
            "minor: an addition; before `1.0.0` the minor level carries what would be major",
            "minor, because before 1.0.0 the minor level carries it",
        ):
            with self.subTest(level=level):
                entry = self.entry("`deployer/manifest.py`", level=level)
                self.assertEqual([], self.problems(self.MANIFEST_CHANGE, self.body(entry)))
                self.restore()

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
        for contract in ("none", "none.", "None", "None.", "NONE", "NONE.", " none. "):
            with self.subTest(contract=contract):
                self.assertEqual(
                    [], self.problems({}, self.body(self.entry(contract, action="pass --cross-major once")))
                )

    def test_a_merged_entry_tied_to_no_file_may_name_none_with_a_period(self) -> None:
        self.release()
        self.commit(f"Clarify guidance (#8)\n\n{self.body(self.entry('none.'))}")
        self.merged()
        for body in (None, self.body()):
            with self.subTest(body=body is not None):
                self.assertEqual([], upgrade_notes_problems(self.root, body))

    def test_a_none_contract_does_not_cover_a_changed_contract_item(self) -> None:
        self.release()
        for contract in ("none", "none."):
            with self.subTest(contract=contract):
                problems = self.problems(self.MANIFEST_CHANGE, self.body(self.entry(contract)))
                self.assertEqual(1, len(problems), problems)
                self.assertTrue(problems[0].startswith("deployer/manifest.py: "), problems[0])
                self.assertIn("changed since v0.1.0, and no upgrade note since then names it", problems[0])

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

    def test_generated_summaries_do_not_become_entries_and_the_policy_accepts_them(self) -> None:
        self.release()
        for value in ("patch", "minor", "major", None):
            with self.subTest(value=value):
                entries = (
                    [] if value is None else release_notes.body_entries(self.body(self.entry("none", level=value)))
                )
                sourced = [release_notes.SourcedEntry(entry, "fixture", "#7", from_notes=False) for entry in entries]
                section = release_notes.version_section(
                    "v0.2.0", sourced, upgrade_notes.versioning_levels(self.RELEASING)
                )
                self.assertEqual(
                    release_notes.notes_entries("\n\n".join(item.rendered() for item in sourced)),
                    release_notes.notes_entries(section),
                )
                self.assertEqual(
                    [],
                    self.problems({"docs/upgrade-notes.md": f"# Upgrade notes\n\n{section}\n{self.NOTES}"}),
                )

    def test_the_policy_and_generator_import_one_version_and_level_reader(self) -> None:
        self.assertIs(upgrade_notes.VERSION, release_notes.VERSION)
        self.assertIs(upgrade_notes.versioning_levels, release_notes.versioning_levels)

    def test_contract_and_level_sections_accept_trailing_whitespace_and_crlf(self) -> None:
        self.release()
        changed = self.RELEASING.replace("## Versioning\n", "## Versioning \t\n").replace("\n", "\r\n")
        self.assertEqual(["patch", "minor", "major"], upgrade_notes.versioning_levels(changed))
        self.assertEqual(
            [],
            self.problems({"docs/releasing.md": changed}, self.body(self.entry("none"))),
        )

    def test_the_tag_and_its_tree_are_the_checkouts_whatever_repository_git_dir_names(self) -> None:
        # The commits since the tag are read through tools/release_notes.py, so the tag must be read through the same
        # git, or the two halves of one check read different repositories.
        self.assertIs(release_notes.git, upgrade_notes.git)
        self.release()
        other = self.home / "another repository"
        self.git("init", "-q", "-b", "main", str(other))
        self.git("commit", "-q", "--allow-empty", "-m", "another", cwd=other)
        self.git("tag", "v9.9.9", cwd=other)
        self.git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=other)
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}):
            tag, problem = upgrade_notes.last_release_tag(self.root)
            names = upgrade_notes.tag_snapshot(self.root, "v0.1.0").names
        self.assertEqual(("v0.1.0", None), (tag, problem))
        self.assertEqual(frozenset(self.RELEASED), names)

    def test_no_tag_reachable_from_origin_main_passes(self) -> None:
        self.release(tag=None)
        self.assertEqual([], self.problems(self.MANIFEST_CHANGE))

    def test_the_last_tag_is_the_highest_release_not_a_nearer_tag_of_another_name(self) -> None:
        # A pre-release or any other tag is not a release, so the contract files compare with the release before it.
        self.release()
        self.commit("change the manifest", self.MANIFEST_CHANGE)
        self.git("tag", "v0.2.0-rc.1")
        self.commit("later")
        self.git("tag", "nightly")
        self.merged()
        self.assertEqual(("v0.1.0", None), upgrade_notes.last_release_tag(self.root))
        problems = upgrade_notes_problems(self.root)
        self.assertEqual(["deployer/manifest.py"], [problem.split(":", 1)[0] for problem in problems])

    def test_a_tag_that_is_not_a_release_is_no_release(self) -> None:
        self.release(tag="v0.1.0-rc.1")
        self.assertEqual((None, None), upgrade_notes.last_release_tag(self.root))
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
        "`skills/skill-core/scripts/pdf_tool.py`",
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
