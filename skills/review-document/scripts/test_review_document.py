"""review_document.py: the one-file fixture it builds for a document in a checkout and for a loose file, the base it
chooses, the reviewer it routes to, the output path it refuses, and the report it copies."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "code-review-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import review_document
from git_client import subprocess_runner
from review_canary import fixture_change, validate_fixture_pull
from review_runtime import validate_adapter_manifest

SCRIPT = Path(review_document.__file__).resolve()
SKILLS_ROOT = SCRIPT.parents[2]
IDENTITY = ("-c", "user.name=Document Test", "-c", "user.email=document@example.invalid")
COMMITTED = b"# Retention\n\nExports are kept 30 days.\n\nOwners: storage.\n"


def git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(directory), *IDENTITY, *arguments], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def run_main(*arguments: str) -> tuple[int, list[str]]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = review_document.main(list(arguments))
    return code, output.getvalue().splitlines()


def facts(lines: list[str]) -> dict[str, str]:
    return {line.split(" ", 1)[0]: line.split(" ", 1)[1] for line in lines}


class DocumentTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="review-document-test-", ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / "out put"

    def checkout(self) -> Path:
        """A checkout with docs/design/retention.md committed, its folder name holding a space."""
        checkout = self.root / "design notes"
        (checkout / "docs" / "design").mkdir(parents=True)
        git(checkout, "init", "--quiet")
        git(checkout, "config", "core.autocrlf", "false")
        (checkout / "docs" / "design" / "retention.md").write_bytes(COMMITTED)
        git(checkout, "add", ".")
        git(checkout, "commit", "--quiet", "--no-gpg-sign", "-m", "design")
        return checkout

    def build(self, path: Path, *options: str) -> dict[str, str]:
        code, lines = run_main("build", str(path), "--output", str(self.output), *options)
        self.assertEqual(0, code, lines)
        return facts(lines)

    def fixture_diff(self, fixture: Path) -> str:
        with fixture_change(fixture, subprocess_runner) as change:
            return change.diff


class CheckoutTests(DocumentTestCase):
    def test_an_uncommitted_edit_is_reviewed_against_the_committed_version(self) -> None:
        checkout = self.checkout()
        commit = git(checkout, "rev-parse", "HEAD")
        document = checkout / "docs" / "design" / "retention.md"
        # Written with CRLF, as a checkout that converts line endings leaves it: only the edited line is a change.
        document.write_bytes(COMMITTED.replace(b"30 days", b"90 days").replace(b"\n", b"\r\n"))
        result = self.build(document)
        self.assertEqual(
            {
                "DOCUMENT": "docs/design/retention.md",
                "BASE": f"committed {commit}",
                "ROUTE": "design-review",
                "FIXTURE": str(self.output / "fixture"),
                "OUTPUT": str(self.output),
            },
            result,
        )
        fixture = self.output / "fixture"
        self.assertEqual(COMMITTED, (fixture / "base" / "docs" / "design" / "retention.md").read_bytes())
        pull = validate_fixture_pull(json.loads((fixture / "pull.json").read_text(encoding="utf-8")))
        self.assertEqual("review-document.local/design-notes", pull["repository"])
        self.assertEqual(f"committed {commit}", pull["base_ref"])
        self.assertIn("the uncommitted change", pull["title"])
        diff = self.fixture_diff(fixture)
        self.assertIn("-Exports are kept 30 days.\n+Exports are kept 90 days.\n", diff)
        self.assertEqual(1, diff.count("\n+") - diff.count("\n+++"), diff)
        self.assertNotIn("specialists.json", diff)
        # Nothing is written to the checkout.
        self.assertEqual("M docs/design/retention.md", git(checkout, "status", "--porcelain"))

    def test_base_none_reviews_the_whole_document_in_a_checkout(self) -> None:
        checkout = self.checkout()
        result = self.build(checkout / "docs" / "design" / "retention.md", "--base", "none")
        self.assertEqual("none the whole document was asked for", result["BASE"])
        fixture = self.output / "fixture"
        self.assertEqual([".review-document"], [path.name for path in (fixture / "base").iterdir()])
        self.assertIn("+Exports are kept 30 days.", self.fixture_diff(fixture))

    def test_an_uncommitted_file_is_reviewed_whole(self) -> None:
        checkout = self.checkout()
        draft = checkout / "docs" / "design" / "draft.md"
        draft.write_bytes(b"# Draft\n\nA new idea.\n")
        result = self.build(draft)
        self.assertEqual("none the file is not committed", result["BASE"])
        self.assertEqual("docs/design/draft.md", result["DOCUMENT"])

    def test_base_committed_is_refused_for_a_file_never_committed(self) -> None:
        checkout = self.checkout()
        draft = checkout / "draft.md"
        draft.write_bytes(b"# Draft\n")
        code, lines = run_main("build", str(draft), "--output", str(self.output), "--base", "committed")
        self.assertEqual(
            (1, ["FAILED --base committed has nothing to compare with: the file is not committed"]), (code, lines)
        )
        self.assertFalse(self.output.exists())

    def test_an_unchanged_document_is_refused_with_the_way_to_review_it_whole(self) -> None:
        checkout = self.checkout()
        code, lines = run_main("build", str(checkout / "docs" / "design" / "retention.md"))
        self.assertEqual(1, code)
        reason = "has no uncommitted change; pass --base none to review the whole document"
        self.assertEqual([f"FAILED docs/design/retention.md {reason}"], lines)


class LooseFileTests(DocumentTestCase):
    def test_a_file_outside_any_checkout_is_reviewed_whole_by_the_design_reviewer(self) -> None:
        document = self.root / "loose" / "Plan.MD"
        document.parent.mkdir()
        document.write_bytes(b"# Plan\r\n\r\nShip it.\r\n")
        result = self.build(document)
        self.assertEqual("none the file is outside any git checkout", result["BASE"])
        self.assertEqual(("Plan.MD", "design-review"), (result["DOCUMENT"], result["ROUTE"]))
        fixture = self.output / "fixture"
        pull = validate_fixture_pull(json.loads((fixture / "pull.json").read_text(encoding="utf-8")))
        self.assertEqual(
            {"repository": "review-document.local/document", "base_ref": "none", "head_ref": "working tree"},
            {key: pull[key] for key in ("repository", "base_ref", "head_ref")},
        )
        self.assertEqual(".review-document/specialists.json", pull["manifest_path"])
        manifest = validate_adapter_manifest(
            json.loads((fixture / "base" / ".review-document" / "specialists.json").read_text(encoding="utf-8"))
        )
        specialist = manifest["specialists"][0]
        self.assertEqual(("suite:design-review", ["^Plan\\.MD$"]), (specialist["profile"], specialist["include"]))
        # Without agent-delegation, a runtime that cannot start subagents runs the specialist inline.
        self.assertNotIn("agent-delegation", manifest["required_capabilities"])
        self.assertEqual(
            (fixture / "base" / ".review-document" / "specialists.json").read_bytes(),
            (fixture / "head" / ".review-document" / "specialists.json").read_bytes(),
        )
        self.assertEqual(b"# Plan\n\nShip it.\n", (fixture / "head" / "Plan.MD").read_bytes())
        diff = self.fixture_diff(fixture)
        self.assertIn("+++ b/Plan.MD", diff)
        self.assertNotIn("specialists.json", diff)

    def test_a_file_that_is_not_a_design_document_goes_to_the_generic_reviewer(self) -> None:
        document = self.root / "tool.py"
        document.write_bytes(b"print('hello')\n")
        result = self.build(document)
        self.assertEqual("generic", result["ROUTE"])
        fixture = self.output / "fixture"
        self.assertNotIn("manifest_path", json.loads((fixture / "pull.json").read_text(encoding="utf-8")))
        self.assertEqual([], list((fixture / "base").iterdir()))
        self.assertIn("+print('hello')", self.fixture_diff(fixture))

    def test_base_committed_is_refused_outside_a_checkout(self) -> None:
        document = self.root / "plan.md"
        document.write_bytes(b"# Plan\n")
        code, lines = run_main("build", str(document), "--base", "committed", "--output", str(self.output))
        self.assertEqual((1, ["FAILED --base committed needs a file inside a git checkout"]), (code, lines))

    def test_binary_empty_and_missing_files_are_refused(self) -> None:
        binary, empty = self.root / "design.docx", self.root / "empty.md"
        binary.write_bytes(b"PK\x03\x04\0\0rest")
        empty.write_bytes(b" \r\n")
        for path, reason in (
            (binary, "is not a text file; a .docx or PDF needs its text extracted first"),
            (empty, "is empty"),
            (self.root / "missing.md", "is not a file"),
        ):
            with self.subTest(path.name):
                code, lines = run_main("build", str(path), "--output", str(self.output))
                self.assertEqual(1, code)
                self.assertEqual(1, len(lines), lines)
                self.assertTrue(lines[0].startswith(f"FAILED {path} {reason}"), lines)
        self.assertFalse(self.output.exists())

    def test_the_default_output_is_a_new_temporary_directory(self) -> None:
        document = self.root / "plan.md"
        document.write_bytes(b"# Plan\n")
        # The system temporary directory, moved under this test's own so nothing is left behind.
        with mock.patch.object(tempfile, "tempdir", str(self.root)):
            code, lines = run_main("build", str(document))
        self.assertEqual(0, code, lines)
        output = Path(facts(lines)["OUTPUT"])
        self.assertTrue(output.name.startswith("review-document-"), output)
        self.assertEqual(self.root, output.parent)
        self.assertEqual(output / "fixture", Path(facts(lines)["FIXTURE"]))


class OutputTests(DocumentTestCase):
    def test_an_output_inside_a_skills_directory_is_refused_before_anything_is_read(self) -> None:
        home = self.root / "home"
        targets = [
            SKILLS_ROOT / "review-document" / "out",  # beside SKILL.md
            SKILLS_ROOT / "code-review-core" / "out",
            home / ".claude" / "skills" / "review-document" / "out",
            home / ".agents" / "skills" / "review-document" / "out",
        ]
        missing = self.root / "missing.md"  # never read: the output is refused first
        with mock.patch.dict(os.environ, {"USERPROFILE": str(home), "HOME": str(home)}):
            for target in targets:
                with self.subTest(target=target):
                    code, lines = run_main("build", str(missing), "--output", str(target))
                    self.assertEqual(1, code)
                    self.assertEqual(1, len(lines), lines)
                    self.assertTrue(lines[0].startswith(f"FAILED {target} is inside the skills directory "), lines)
                    self.assertFalse(target.exists())

    def test_an_output_that_already_holds_a_fixture_is_refused(self) -> None:
        document = self.root / "plan.md"
        document.write_bytes(b"# Plan\n")
        self.build(document)
        code, lines = run_main("build", str(document), "--output", str(self.output))
        self.assertEqual(
            (1, [f"FAILED {self.output} already holds a fixture; name a new or empty directory"]), (code, lines)
        )

    def test_report_copies_the_recorded_report_beside_the_fixture(self) -> None:
        document = self.root / "plan.md"
        document.write_bytes(b"# Plan\n")
        self.build(document)
        canary = self.root / "canary" / "review-document.local" / "document" / "pulls" / "1"
        canary.mkdir(parents=True)
        (canary / "review.md").write_text("# Code Review\n", encoding="utf-8")
        (canary / "review.json").write_text("{}", encoding="utf-8")
        code, lines = run_main(
            "report", "--fixture", str(self.output / "fixture"), "--report", str(canary / "review.md")
        )
        self.assertEqual(0, code, lines)
        self.assertEqual([f"REPORT {self.output / 'report.md'}", f"RECORD {canary / 'review.json'}"], lines)
        self.assertEqual("# Code Review\n", (self.output / "report.md").read_text(encoding="utf-8"))

    def test_report_refuses_a_file_that_is_not_a_recorded_report(self) -> None:
        document = self.root / "plan.md"
        document.write_bytes(b"# Plan\n")
        self.build(document)
        code, lines = run_main("report", "--fixture", str(self.output / "fixture"), "--report", str(document))
        self.assertEqual((1, [f"FAILED {document} is not a report finalize recorded"]), (code, lines))
        self.assertFalse((self.output / "report.md").exists())
        code, lines = run_main("report", "--fixture", str(self.root), "--report", str(document))
        self.assertEqual((1, [f"FAILED {self.root} is not a fixture review-document built"]), (code, lines))


class ConsoleTests(DocumentTestCase):
    def test_output_survives_a_console_that_cannot_encode_it(self) -> None:
        # Windows pipes default to a legacy code page; a document's name is printed exactly as it is on disk.
        document = self.root / "設計 ✓.md"
        document.write_bytes(b"# Design\n")
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "build", str(document), "--output", str(self.output)],
            capture_output=True,
            env={**os.environ, "PYTHONIOENCODING": "cp1252"},
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(
            "DOCUMENT 設計 ✓.md\nBASE none the file is outside any git checkout\nROUTE design-review\n"
            f"FIXTURE {self.output / 'fixture'}\nOUTPUT {self.output}\n",
            result.stdout.decode("utf-8").replace("\r\n", "\n"),
        )


if __name__ == "__main__":
    unittest.main()
