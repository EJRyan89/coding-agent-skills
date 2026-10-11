"""review_document.py: the one-file fixture it builds for a document in a checkout and for a loose file, a Word
document, and a PDF, the base it chooses, the reviewer it routes to, the output path it refuses, and the report it
copies."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "code-review-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import review_document
import review_pipeline as rp
from git_client import subprocess_runner
from github_client import CommandResult
from review_canary import fixture_change, validate_fixture_pull
from review_config import write_config
from review_github import GitHubClient
from review_runtime import validate_adapter_manifest
from test_review_documents import StubPdftotext, paragraph, pdf_document, table, without_pdftotext, word_document

SCRIPT = Path(review_document.__file__).resolve()
SKILLS_ROOT = SCRIPT.parents[2]
IDENTITY = ("-c", "user.name=Document Test", "-c", "user.email=document@example.invalid")
COMMITTED = b"# Retention\n\nExports are kept 30 days.\n\nOwners: storage.\n"
UNCHANGED_TEXT = (
    "has no uncommitted change to its text, only to formatting, layout, images, or parts whose text is not extracted; "
    "pass --base none to review the whole document"
)


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


def added(diff: str) -> list[str]:
    """The added lines of a diff a reviewer reads, each as its line number and text."""
    return [line[1:].lstrip() for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]


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
        binary, empty = self.root / "design.bin", self.root / "empty.md"
        binary.write_bytes(b"%PDF-1.7\n\0\0rest")
        empty.write_bytes(b" \r\n")
        for path, reason in (
            (binary, "is not a text file, a Word document, or a PDF"),
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


class ExtractedDocumentTests(DocumentTestCase):
    """A Word document or a PDF is written to the fixture as its exact bytes, and the fixture canary's snapshot
    extracts it."""

    def setUp(self) -> None:
        super().setUp()
        environment = mock.patch.dict(
            os.environ,
            {
                "CODE_REVIEW_STATE": str(self.root / "state" / "state.json"),
                "CODE_REVIEW_FLAGS": str(self.root / "flags" / "flags.json"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        # Each run and canary root goes under this test's own folder.
        self.temporary = self.root / "tmp"
        self.temporary.mkdir()
        temporary = mock.patch.object(tempfile, "tempdir", str(self.temporary))
        temporary.start()
        self.addCleanup(temporary.stop)
        self.config = self.root / "config.json"
        write_config(
            {
                "schema_version": 1,
                "default_repository_set": "primary",
                "repository_sets": {"primary": ["example/one"]},
                "repositories": {
                    "example/one": {
                        "reviewer": {
                            "id": "generic",
                            "protocol_version": 1,
                            "trusted_ref": None,
                            "scope": "generic",
                            "manifest_path": None,
                        },
                        "checkout_path": None,
                    }
                },
                "archive_root": str(self.root / "archive"),
                "local_mirror_root": None,
                "summary_root": str(self.root / "summaries"),
                "dashboard_file": str(self.root / "dashboard.md"),
                "github_login": "reviewer",
                "runtime": "auto",
                "verdict_policy": {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
                "dashboard": {},
            },
            self.config,
        )

    def pipeline(self, *arguments: str) -> list[str]:
        """Run the review pipeline as the skill does, with no GitHub; returns its lines once it succeeds."""

        def no_github(command: object) -> CommandResult:
            raise AssertionError(f"a document review called gh: {command}")

        services = rp.Services(
            github=GitHubClient(runner=no_github), resolve_runtime=lambda configured, host: "claude-code"
        )
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = rp.main(["--config", str(self.config), *arguments], services=services)
        self.assertEqual((0, ""), (code, errors.getvalue()), output.getvalue())
        return output.getvalue().splitlines()

    def review(self, document: str, line: int) -> tuple[str, dict[str, Any]]:
        """Prepare the fixture `build` wrote, have the design reviewer report one finding on `line` of `document`'s
        extracted text, and finalize. Returns the diff the reviewer read and the record."""
        fixture = self.output / "fixture"
        prepared = self.pipeline("prepare", "--host", "claude-code", "--canary", "--fixture", str(fixture))
        run = Path(next(fact for fact in prepared if fact.startswith("RUN ")).split(" ", 2)[2])
        roles = rp.load_run(run)["roles"]
        self.assertEqual(["design-review"], [role["id"] for role in roles])
        diff = (run / "work" / "design-review.diff").read_text(encoding="utf-8")
        finding = {
            "path": document,
            "line": line,
            "severity": "SHOULD_FIX",
            "title": "Retries have no bound",
            "category": "requirement-gap",
            "body": "Retrying forever never gives up on a poisoned message.",
        }
        result = {"model": "fixture-model", "summary": "One gap.", "findings": [finding], "prior_dispositions": []}
        Path(roles[0]["result_file"]).write_text(json.dumps(result), encoding="utf-8")
        recorded = next(fact for fact in self.pipeline("finalize", "--run", str(run)) if fact.startswith("RECORDED "))
        code, lines = run_main("report", "--fixture", str(fixture), "--report", recorded.split(" ", 4)[4])
        self.assertEqual(0, code, lines)
        record = json.loads(Path(facts(lines)["RECORD"]).read_text(encoding="utf-8"))
        return diff, record

    def test_a_word_draft_is_reviewed_by_the_design_reviewer_and_its_finding_says_where_it_is(self) -> None:
        draft = self.root / "drafts" / "Queue Design.DOCX"
        draft.parent.mkdir()
        content = word_document(
            paragraph("Scope", style="Titre1"), paragraph(), paragraph("Keep one queue."), paragraph("Retry forever.")
        )
        draft.write_bytes(content)
        result = self.build(draft)
        self.assertEqual(
            ("Queue Design.DOCX", "none the file is outside any git checkout", "design-review"),
            (result["DOCUMENT"], result["BASE"], result["ROUTE"]),
        )
        fixture = self.output / "fixture"
        # The exact bytes, never a line-ending conversion, so the snapshot extracts the document as it is.
        self.assertEqual(content, (fixture / "head" / "Queue Design.DOCX").read_bytes())
        manifest = json.loads((fixture / "base" / ".review-document" / "specialists.json").read_text("utf-8"))
        self.assertEqual(["^Queue\\ Design\\.DOCX$"], manifest["specialists"][0]["include"])

        diff, record = self.review("Queue Design.DOCX", 3)
        self.assertEqual(["1 | [P1] # Scope", "2 | [P3] Keep one queue.", "3 | [P4] Retry forever."], added(diff))
        [finding] = record["findings"]
        self.assertEqual('paragraph 4, under the heading "Scope"', finding["location"])
        report = (self.output / "report.md").read_text(encoding="utf-8")
        self.assertIn("**Line 3 (paragraph 4&#44; under the heading &#34;Scope&#34;):** `[P4] Retry forever.`", report)

    def test_a_committed_word_document_is_reviewed_by_the_paragraphs_its_edit_changes(self) -> None:
        checkout = self.checkout()
        document = checkout / "docs" / "design" / "queue.docx"
        committed = word_document(paragraph("Scope", style="Titre1"), paragraph("Keep one queue."))
        document.write_bytes(committed)
        git(checkout, "add", ".")
        git(checkout, "commit", "--quiet", "--no-gpg-sign", "-m", "queue")
        edited = word_document(
            paragraph("Scope", style="Titre1"), paragraph("Retry forever."), paragraph("Keep one queue.")
        )
        document.write_bytes(edited)
        result = self.build(document)
        self.assertEqual(f"committed {git(checkout, 'rev-parse', 'HEAD')}", result["BASE"])
        self.assertEqual("design-review", result["ROUTE"])
        fixture = self.output / "fixture"
        self.assertEqual(committed, (fixture / "base" / "docs" / "design" / "queue.docx").read_bytes())
        self.assertEqual(edited, (fixture / "head" / "docs" / "design" / "queue.docx").read_bytes())

        diff, record = self.review("docs/design/queue.docx", 2)
        # Only the inserted paragraph is added: the one after it holds the same text, though its label moves.
        self.assertEqual(["2 | [P2] Retry forever."], added(diff))
        self.assertEqual('paragraph 2, under the heading "Scope"', record["findings"][0]["location"])

    def test_a_word_document_whose_text_is_not_extracted_or_unchanged_is_refused_before_any_fixture(self) -> None:
        hostile = self.root / "hostile.docx"
        hostile.write_bytes(word_document(paragraph("x"), extra={"../../evil.xml": b"x"}))
        blank = self.root / "blank.docx"
        blank.write_bytes(word_document(paragraph()))
        for path, reason in (
            (hostile, "is a document whose text could not be extracted: a member's path leaves the package"),
            (blank, "holds no text to review"),
        ):
            with self.subTest(path.name):
                code, lines = run_main("build", str(path), "--output", str(self.output))
                self.assertEqual((1, [f"FAILED {path} {reason}"]), (code, lines))
        self.assertFalse(self.output.exists())

        checkout = self.checkout()
        document = checkout / "queue.docx"
        document.write_bytes(word_document(paragraph("Keep one queue.")))
        git(checkout, "add", ".")
        git(checkout, "commit", "--quiet", "--no-gpg-sign", "-m", "queue")
        # A new style changes the package and leaves the text as it was.
        document.write_bytes(word_document(paragraph("Keep one queue."), extra={"word/theme.xml": b"<theme/>"}))
        code, lines = run_main("build", str(document), "--output", str(self.output))
        self.assertEqual((1, [f"FAILED queue.docx {UNCHANGED_TEXT}"]), (code, lines))
        self.assertFalse(self.output.exists())
        self.assertEqual("none the whole document was asked for", self.build(document, "--base", "none")["BASE"])

    def test_an_edit_that_only_renumbers_the_same_lines_of_text_is_refused_as_the_diff_would_show_none(self) -> None:
        # The diff matches lines without their labels, so a change that only moves labels leaves it no hunk.
        checkout = self.checkout()
        # The stub reads the edited PDF as a first page with no text, such as a picture, before the same line.
        StubPdftotext(self, {"marker": b"(Cover)", "output": "\fKeep one queue.\n\f"}, output="Keep one queue.\n\f")
        for name, committed, edited in (
            (
                "empty paragraph.docx",
                word_document(paragraph("Keep one queue.")),
                word_document(paragraph(), paragraph("Keep one queue.")),
            ),
            (
                "moved row.docx",
                word_document(paragraph("Scope"), table(("Owner", "storage"))),
                word_document(paragraph("Scope"), paragraph(), paragraph(), table(("Owner", "storage"))),
            ),
            ("page.pdf", pdf_document(["Keep one queue."]), pdf_document(["Cover"], ["Keep one queue."])),
        ):
            with self.subTest(name):
                document = checkout / name
                document.write_bytes(committed)
                git(checkout, "add", ".")
                git(checkout, "commit", "--quiet", "--no-gpg-sign", "-m", name)
                document.write_bytes(edited)
                code, lines = run_main("build", str(document), "--output", str(self.output))
                self.assertEqual((1, [f"FAILED {name} {UNCHANGED_TEXT}"]), (code, lines))
        self.assertFalse(self.output.exists())
        # One changed line of text is reviewed, though every label after it moves too.
        document = checkout / "empty paragraph.docx"
        document.write_bytes(word_document(paragraph(), paragraph("Keep two queues.")))
        self.assertEqual("design-review", self.build(document)["ROUTE"])

    def test_a_pdf_draft_is_reviewed_by_the_design_reviewer_and_its_finding_gives_its_page_and_line(self) -> None:
        stub = StubPdftotext(self, output="Scope\n\nKeep one queue.\n\fRetry forever.\n\f")
        draft = self.root / "drafts" / "Queue Design.PDF"
        draft.parent.mkdir()
        content = pdf_document(["Scope", "Keep one queue."], ["Retry forever."])
        draft.write_bytes(content)
        result = self.build(draft)
        self.assertEqual(
            ("Queue Design.PDF", "none the file is outside any git checkout", "design-review"),
            (result["DOCUMENT"], result["BASE"], result["ROUTE"]),
        )
        fixture = self.output / "fixture"
        # The exact bytes, so the snapshot extracts the PDF as it is; build ran the tool once, to find text in it.
        self.assertEqual(content, (fixture / "head" / "Queue Design.PDF").read_bytes())
        self.assertEqual([content.hex()], [call["document"] for call in stub.calls()])
        manifest = json.loads((fixture / "base" / ".review-document" / "specialists.json").read_text("utf-8"))
        self.assertEqual(["^Queue\\ Design\\.PDF$"], manifest["specialists"][0]["include"])

        diff, record = self.review("Queue Design.PDF", 3)
        self.assertEqual(
            ["1 | [p1 l1] Scope", "2 | [p1 l2] Keep one queue.", "3 | [p2 l1] Retry forever."], added(diff)
        )
        [finding] = record["findings"]
        self.assertEqual("page 2, line 1", finding["location"])
        report = (self.output / "report.md").read_text(encoding="utf-8")
        self.assertIn("**Line 3 (page 2&#44; line 1):** `[p2 l1] Retry forever.`", report)

    def test_a_pdf_whose_text_is_not_extracted_or_unchanged_is_refused_before_any_fixture(self) -> None:
        draft = self.root / "queue.pdf"
        draft.write_bytes(pdf_document(["Keep one queue."]))
        without_pdftotext(self)
        code, lines = run_main("build", str(draft), "--output", str(self.output))
        reason = (
            "is a document whose text could not be extracted: pdftotext, which extracts a PDF's text, is not installed"
        )
        self.assertEqual((1, [f"FAILED {draft} {reason}"]), (code, lines))
        self.assertFalse(self.output.exists())

        StubPdftotext(
            self,
            {"marker": b"(rejected)", "status": 1},
            {"marker": b"(Keep one queue.)", "output": "Keep one queue.\n\f"},
            output="\f",
        )
        rejected, blank = self.root / "rejected.pdf", self.root / "blank.pdf"
        rejected.write_bytes(pdf_document(["rejected"]))
        blank.write_bytes(pdf_document([]))
        for path, reason in (
            (rejected, "is a document whose text could not be extracted: pdftotext could not read it (exit status 1)"),
            (blank, "holds no text to review"),
        ):
            with self.subTest(path.name):
                code, lines = run_main("build", str(path), "--output", str(self.output))
                self.assertEqual((1, [f"FAILED {path} {reason}"]), (code, lines))
        self.assertFalse(self.output.exists())

        checkout = self.checkout()
        document = checkout / "queue.pdf"
        document.write_bytes(pdf_document(["Keep one queue."]))
        git(checkout, "add", ".")
        git(checkout, "commit", "--quiet", "--no-gpg-sign", "-m", "queue")
        # Other bytes that read as the same text.
        document.write_bytes(pdf_document(["Keep one queue.", ""]))
        code, lines = run_main("build", str(document), "--output", str(self.output))
        self.assertEqual((1, [f"FAILED queue.pdf {UNCHANGED_TEXT}"]), (code, lines))
        self.assertFalse(self.output.exists())


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

    def test_an_output_that_is_not_a_new_or_empty_directory_is_refused_before_anything_is_read(self) -> None:
        document = self.root / "plan.md"
        document.write_bytes(b"# Plan\n")
        self.build(document)
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        (unrelated / "notes.txt").write_bytes(b"mine\n")
        (self.root / "empty folder").mkdir()
        a_file = self.root / "a file"
        a_file.write_bytes(b"mine\n")
        missing = self.root / "missing.md"  # never read: the output is refused first
        for target in (self.output, unrelated, a_file):
            with self.subTest(target=target.name):
                code, lines = run_main("build", str(missing), "--output", str(target))
                reason = "exists and is not an empty directory; name a new or empty directory"
                self.assertEqual((1, [f"FAILED {target} {reason}"]), (code, lines))
        self.assertEqual(["notes.txt"], [entry.name for entry in unrelated.iterdir()])
        self.assertEqual(b"mine\n", a_file.read_bytes())
        code, lines = run_main("build", str(document), "--output", str(self.root / "empty folder"))
        self.assertEqual((0, str(self.root / "empty folder")), (code, facts(lines)["OUTPUT"]))

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

        # A report.md already there is never replaced; the reason names where the recorded report stays.
        (self.output / "report.md").write_text("mine\n", encoding="utf-8")
        code, lines = run_main(
            "report", "--fixture", str(self.output / "fixture"), "--report", str(canary / "review.md")
        )
        reason = f"already exists, and is not replaced; the report is at {canary / 'review.md'}"
        self.assertEqual((1, [f"FAILED {self.output / 'report.md'} {reason}"]), (code, lines))
        self.assertEqual("mine\n", (self.output / "report.md").read_text(encoding="utf-8"))

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
