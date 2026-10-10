"""Regression tests for the text the snapshot extracts from a changed Word document, and the diff of two extractions.

Every document here is built by the test itself with `zipfile`, so no binary fixture is committed.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any, ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parent))

import review_documents as documents
import review_pipeline as rp
import review_runtime
from review_documents import ExtractionRefused, document_diff, document_format, extract, locations
from review_runtime import (
    RuntimeContractError,
    fetch_source_file,
    materialize_source_snapshot,
    materialize_source_snapshot_from_github,
    verify_source_snapshot,
)
from review_specialists import parse_unified_diff, patch_fingerprints

NAMESPACES = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
)
STYLES = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {NAMESPACES}>'
    '<w:style w:type="paragraph" w:styleId="Titre1"><w:name w:val="heading 1"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Custom"><w:name w:val="My section"/><w:basedOn w:val="Outlined"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Outlined"><w:name w:val="Outlined"/>'
    '<w:pPr><w:outlineLvl w:val="2"/></w:pPr></w:style>'
    "</w:styles>"
)


def paragraph(text: str = "", *, style: str | None = None, level: int | None = None, inner: str = "") -> str:
    """One `w:p`: its style, its list level, a run holding `text`, and any other markup inside it."""
    properties = ""
    if style is not None:
        properties += f'<w:pStyle w:val="{style}"/>'
    if level is not None:
        properties += f'<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="1"/></w:numPr>'
    run = f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>' if text else ""
    return f"<w:p>{f'<w:pPr>{properties}</w:pPr>' if properties else ''}{run}{inner}</w:p>"


def table(*rows: tuple[str, ...]) -> str:
    cells = "".join("<w:tr>" + "".join(f"<w:tc>{paragraph(cell)}</w:tc>" for cell in row) + "</w:tr>" for row in rows)
    return f"<w:tbl><w:tblPr/>{cells}</w:tbl>"


def body_xml(*blocks: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {NAMESPACES}><w:body>'
        + "".join(blocks)
        + "<w:sectPr/></w:body></w:document>"
    ).encode("utf-8")


def word_document(*blocks: str, document: bytes | None = None, extra: dict[str, bytes] | None = None) -> bytes:
    """A `.docx` package holding these body blocks (or `document` as its main part), the styles above, and any
    `extra` members, by name."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        package.writestr("word/document.xml", document if document is not None else body_xml(*blocks))
        package.writestr("word/styles.xml", STYLES)
        for name, content in (extra or {}).items():
            package.writestr(name, content)
    return buffer.getvalue()


def lines(content: bytes) -> list[str]:
    return extract("docx", content).decode("utf-8").splitlines()


class ExtractionTests(unittest.TestCase):
    def test_paragraphs_headings_lists_and_tables_are_lines_labelled_with_their_place(self) -> None:
        content = word_document(
            paragraph("Design", style="Title"),
            paragraph("Scope", style="Titre1"),
            paragraph(),
            paragraph("The service keeps one queue."),
            paragraph("first", level=0),
            paragraph("nested", level=1),
            table(("Name", "Limit"), ("", ""), ("queue", "10")),
            paragraph("Detail", style="Custom"),
            paragraph("Under it", style="Heading2"),
        )

        self.assertEqual(
            lines(content),
            [
                "[P1] # Design",
                "[P2] # Scope",
                "[P4] The service keeps one queue.",
                "[P5] - first",
                "[P6]   - nested",
                "[P7 R1] | Name | Limit |",
                "[P7 R3] | queue | 10 |",
                "[P8] ### Detail",
                "[P9] ## Under it",
            ],
        )

    def test_tracked_changes_stay_in_their_line_as_markers(self) -> None:
        inserted = '<w:ins w:id="1" w:author="a"><w:r><w:t>twelve</w:t></w:r></w:ins>'
        deleted = '<w:del w:id="2" w:author="a"><w:r><w:delText>ten</w:delText></w:r></w:del>'
        formatting = '<w:r><w:rPr><w:ins w:id="3" w:author="a"/></w:rPr><w:t> hours.</w:t></w:r>'
        content = word_document(paragraph("Retain for ", inner=deleted + inserted + formatting))

        self.assertEqual(lines(content), ["[P1] Retain for [-ten-]{+twelve+} hours."])

    def test_cells_breaks_and_fallbacks_never_split_or_repeat_a_line(self) -> None:
        cell = f"<w:tc>{paragraph('one')}{paragraph()}{paragraph('two')}</w:tc>"
        broken = "<w:r><w:t>a</w:t><w:br/><w:t>b</w:t><w:tab/><w:t>c\u2028d</w:t></w:r>"
        fallback = (
            '<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:t>box</w:t></mc:Choice>'
            "<mc:Fallback><w:t>box</w:t></mc:Fallback></mc:AlternateContent></w:r>"
        )
        content = word_document(
            f"<w:tbl><w:tr>{cell}</w:tr></w:tbl>", paragraph(inner=broken), paragraph(inner=fallback)
        )

        self.assertEqual(lines(content), ["[P1 R1] | one / two |", "[P2] a b c d", "[P3] box"])

    def test_a_document_without_text_extracts_to_nothing(self) -> None:
        self.assertEqual(extract("docx", word_document(paragraph())), b"")

    def test_the_format_is_named_by_suffix_ignoring_case(self) -> None:
        self.assertEqual(document_format("docs/Design.DOCX"), "docx")
        self.assertIsNone(document_format("docs/design.pdf"))
        self.assertIsNone(document_format("docs.docx/readme"))
        self.assertIsNone(document_format("docx"))

    def test_locations_name_the_paragraph_the_row_and_the_heading_above(self) -> None:
        text = "[P1] # Scope\n[P3] Body\n[P4 R2] | a |\nunlabelled\n"

        self.assertEqual(
            locations(text),
            [
                "paragraph 1",
                'paragraph 3, under the heading "Scope"',
                'paragraph 4, table row 2, under the heading "Scope"',
                "",
            ],
        )


class RefusalTests(unittest.TestCase):
    def assertRefused(self, content: bytes, reason: str) -> None:
        with self.assertRaises(ExtractionRefused) as caught:
            extract("docx", content)
        self.assertIn(reason, str(caught.exception))

    def test_a_document_type_or_entity_is_refused_before_parsing(self) -> None:
        external = b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///c:/windows/win.ini">]><d>&x;</d>'
        laughs = b'<?xml version="1.0"?><!doctype d [<!entity a "aaaa">]><d>&a;</d>'
        with mock_parser_never_created(self):
            self.assertRefused(word_document(document=external), "declares a document type or an entity")
            self.assertRefused(word_document(document=laughs), "declares a document type or an entity")

    def test_a_member_path_that_leaves_the_package_is_refused(self) -> None:
        for name in ("../evil.xml", "/abs.xml", "c:/abs.xml", "word\\..\\x.xml", "word//x.xml", "word/./x.xml"):
            with self.subTest(name=name):
                self.assertRefused(word_document(paragraph("x"), extra={name: b"x"}), "leaves the package")

    def test_a_part_over_the_limit_is_refused_as_it_is_read(self) -> None:
        huge = b"<d>" + b" " * (documents.MAX_DOCUMENT_PART_BYTES + 1) + b"</d>"
        self.assertRefused(word_document(document=huge), "over 16 MiB")

    def test_damaged_unusual_or_foreign_packages_are_refused(self) -> None:
        self.assertRefused(b"PK\x03\x04 not really", "not a readable zip package")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as package:
            package.writestr("other.xml", "<x/>")
        self.assertRefused(buffer.getvalue(), "has no word/document.xml")
        self.assertRefused(word_document(document=b"<w:document><unclosed>"), "not well-formed XML")
        utf16 = '<?xml version="1.0" encoding="UTF-16"?><d/>'.encode("utf-16")
        self.assertRefused(word_document(document=utf16), "not UTF-8")
        declared = b'<?xml version="1.0" encoding="ISO-8859-1"?><d/>'
        self.assertRefused(word_document(document=declared), "other than UTF-8")
        many = {f"part{index}.xml": b"x" for index in range(documents.MAX_DOCUMENT_MEMBERS)}
        self.assertRefused(word_document(paragraph("x"), extra=many), "more than 2000 members")
        self.assertRefused(word_document(paragraph("x"), extra={"WORD/document.xml": b"x"}), "share a name")

    def test_extracted_text_over_the_limit_is_refused(self) -> None:
        text = "x" * 1000
        content = word_document(*(paragraph(text) for _ in range(documents.MAX_EXTRACTED_BYTES // 1000 + 1)))
        self.assertRefused(content, "text is over 1 MiB")


class mock_parser_never_created:
    """Fails the test if expat is asked for a parser inside the block: a refusal before parsing creates none."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.test = test

    def __enter__(self) -> None:
        self.original = documents.expat.ParserCreate

        def refuse(*_: object, **__: object) -> object:
            self.test.fail("a parser was created")

        documents.expat.ParserCreate = refuse  # type: ignore[assignment]  # restored on exit

    def __exit__(self, *_: object) -> None:
        documents.expat.ParserCreate = self.original  # type: ignore[assignment]  # the original, put back


class DiffTests(unittest.TestCase):
    HEADER = ("diff --git a/docs/design.docx b/docs/design.docx", "index 1111111..2222222 100644")

    def test_an_inserted_paragraph_is_one_added_line_and_the_new_side_numbers_the_head_file(self) -> None:
        base = "[P1] # Scope\n[P2] Keep one queue.\n[P3] Retry twice.\n"
        head = "[P1] # Scope\n[P2] Bound the queue at ten.\n[P3] Keep one queue.\n[P4] Retry twice.\n"

        diff = document_diff(self.HEADER, "docs/design.docx", "docs/design.docx", base, head)
        parsed = parse_unified_diff(diff)

        self.assertEqual(list(parsed), ["docs/design.docx"])
        self.assertEqual(parsed["docs/design.docx"]["added"], {2: "[P2] Bound the queue at ten."})
        self.assertIn(" [P3] Keep one queue.", diff.splitlines())
        self.assertNotIn("-[P2] Keep one queue.", diff.splitlines())
        self.assertTrue(diff.splitlines()[2].startswith("extracted: the text of this Word document"))
        self.assertEqual(patch_fingerprints(parsed)["docs/design.docx"]["lines"], 1)

    def test_an_added_document_is_all_added_lines_and_an_unchanged_text_has_no_hunk(self) -> None:
        added = document_diff(["diff --git a/n.docx b/n.docx", "new file mode 100644"], None, "n.docx", "", "[P1] a\n")
        self.assertIn("--- /dev/null", added.splitlines())
        self.assertEqual(parse_unified_diff(added)["n.docx"]["added"], {1: "[P1] a"})

        same = document_diff(self.HEADER, "docs/design.docx", "docs/design.docx", "[P1] a\n", "[P1] a\n")
        self.assertNotIn("@@", same)
        self.assertIn("no line of its text differs", same)
        self.assertEqual(list(parse_unified_diff(same)), ["docs/design.docx"])

    def test_a_base_that_could_not_be_extracted_shows_the_head_as_added_and_says_why(self) -> None:
        diff = document_diff(self.HEADER, "d.docx", "d.docx", "[P1] old\n", "[P1] new\n", base_refused="it is broken")

        self.assertIn("the base version could not be extracted (it is broken)", diff)
        self.assertEqual(parse_unified_diff(diff)["d.docx"]["added"], {1: "[P1] new"})

    def test_a_path_git_quotes_round_trips_through_the_diff_parser(self) -> None:
        for path in ("docs/r\u00e9sum\u00e9.docx", 'docs/say "hi".docx', "docs/plain name.docx"):
            with self.subTest(path=path):
                header = [f"diff --git {documents.quoted_path('a/', path)} {documents.quoted_path('b/', path)}"]
                diff = document_diff(header, path, path, "", "[P1] x\n")
                self.assertEqual(list(parse_unified_diff(diff)), [path])


REPOSITORY = "example/one"
BASE_DOCUMENT = word_document(paragraph("Scope", style="Titre1"), paragraph("Keep one queue."))
HEAD_DOCUMENT = word_document(
    paragraph("Scope", style="Titre1"), paragraph("Bound the queue at ten."), paragraph("Keep one queue.")
)
HOSTILE_DOCUMENT = word_document(
    document=b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///c:/windows/win.ini">]><d>&x;</d>'
)


def git(checkout: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", *arguments], cwd=checkout, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(checkout: Path, files: dict[str, bytes]) -> str:
    """Commit `files` (and whatever earlier commits left) in the checkout; return the commit."""
    for relative, content in files.items():
        target = checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    git(checkout, "add", "-A")
    git(checkout, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "c")
    return git(checkout, "rev-parse", "HEAD")


class SnapshotRouteTests(unittest.TestCase):
    """Every route holds a changed document as its extracted text and leaves an unchanged one `binary`."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        git(self.checkout, "init", "-q", "--template=", "-b", "main")
        git(self.checkout, "remote", "add", "origin", f"https://github.com/{REPOSITORY}.git")
        self.base = commit(self.checkout, {"docs/design.docx": BASE_DOCUMENT, "docs/old.docx": BASE_DOCUMENT})
        self.head = commit(self.checkout, {"docs/design.docx": HEAD_DOCUMENT, "docs/hostile.docx": HOSTILE_DOCUMENT})
        self.changed = ("docs/design.docx", "docs/hostile.docx")

    def assert_extracted(self, destination: Path, metadata: dict[str, Any], refused: dict[str, str]) -> None:
        self.assertEqual({"docs/design.docx": "docx"}, metadata[review_runtime.SNAPSHOT_EXTRACTED])
        text = (destination / "docs" / "design.docx").read_bytes()
        self.assertEqual(extract("docx", HEAD_DOCUMENT), text)
        self.assertEqual(hashlib.sha256(text).hexdigest(), metadata["source_hashes"]["docs/design.docx"])
        self.assertEqual("unextractable", metadata["excluded_paths"]["docs/hostile.docx"])
        self.assertEqual({"docs/hostile.docx": "word/document.xml declares a document type or an entity"}, refused)
        verify_source_snapshot(destination, expected_repository=REPOSITORY, expected_commit=self.head)

    def test_a_whole_checkout_snapshot_extracts_a_changed_document_and_leaves_an_unchanged_one_binary(self) -> None:
        refused: dict[str, str] = {}
        destination = self.root / "whole"
        metadata = materialize_source_snapshot(
            self.checkout, REPOSITORY, self.head, destination, changed_paths=self.changed, refused=refused
        )

        self.assert_extracted(destination, metadata, refused)
        self.assertEqual("binary", metadata["excluded_paths"]["docs/old.docx"])

    def test_a_lazy_snapshot_holds_the_changed_document_and_source_file_returns_its_text(self) -> None:
        refused: dict[str, str] = {}
        destination = self.root / "lazy"
        metadata = materialize_source_snapshot(
            self.checkout,
            REPOSITORY,
            self.head,
            destination,
            changed_paths=self.changed,
            upfront=lambda _path: False,
            refused=refused,
        )

        self.assert_extracted(destination, metadata, refused)
        staging = self.root / "staging"
        staging.mkdir()

        def fetch(relative: str) -> Path | str:
            return fetch_source_file(
                self.checkout, destination, relative, repository=REPOSITORY, commit=self.head, staging=staging
            )

        self.assertEqual(destination / "docs" / "design.docx", fetch("docs/design.docx"))
        self.assertEqual("binary", fetch("docs/old.docx"))
        self.assertEqual("unextractable", fetch("docs/hostile.docx"))

    def test_a_tarball_snapshot_extracts_as_the_checkout_does(self) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, data in {
                "docs/design.docx": HEAD_DOCUMENT,
                "docs/old.docx": BASE_DOCUMENT,
                "docs/hostile.docx": HOSTILE_DOCUMENT,
            }.items():
                info = tarfile.TarInfo(f"example-one-abc/{name}")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        refused: dict[str, str] = {}
        destination = self.root / "tarball"

        def fetcher(_repository: str, _commit: str, target: Path) -> None:
            target.write_bytes(buffer.getvalue())

        metadata = materialize_source_snapshot_from_github(
            REPOSITORY,
            self.head,
            destination,
            fetcher=fetcher,
            changed_paths=self.changed,
            refused=refused,
        )

        self.assert_extracted(destination, metadata, refused)
        self.assertEqual("binary", metadata["excluded_paths"]["docs/old.docx"])

    def test_a_manifest_that_names_a_document_it_does_not_hold_fails_verification(self) -> None:
        destination = self.root / "whole"
        materialize_source_snapshot(self.checkout, REPOSITORY, self.head, destination, changed_paths=self.changed)
        manifest = destination / review_runtime.SOURCE_SNAPSHOT_MANIFEST
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        for extracted in ({"docs/old.docx": "docx"}, {"docs/design.docx": "pdf"}, ["docs/design.docx"]):
            with self.subTest(extracted=extracted):
                manifest.write_text(json.dumps({**metadata, "extracted": extracted}), encoding="utf-8")
                with self.assertRaises(RuntimeContractError):
                    verify_source_snapshot(destination, expected_repository=REPOSITORY, expected_commit=self.head)

    def test_the_base_version_is_read_by_the_blob_the_diff_names(self) -> None:
        blob = git(self.checkout, "rev-parse", f"{self.base}:docs/design.docx")
        read = rp._base_reader(self.checkout, REPOSITORY, {}, rp.Services())

        self.assertEqual(BASE_DOCUMENT, read("docs/design.docx", blob[:7]))
        with self.assertRaises(RuntimeContractError):
            read("docs/design.docx", "not-an-id")


class StubGitHub:
    """The two reads a review makes for a document's base version on the tarball route."""

    def __init__(self, blob: str, content: bytes | None) -> None:
        self.blob, self.content = blob, content
        self.calls: list[tuple[str, ...]] = []

    def get_merge_base(self, repository: str, base: str, head: str) -> str:
        self.calls.append(("merge-base", base, head))
        return "c" * 40

    def get_file_blob(self, repository: str, commit: str, path: str, *, maximum: int) -> tuple[str, bytes | None]:
        self.calls.append(("blob", commit, path))
        return self.blob, self.content


class GitHubBaseTests(unittest.TestCase):
    PULL: ClassVar[dict[str, str]] = {"baseRefOid": "a" * 40, "headRefOid": "b" * 40}

    def reader(self, github: StubGitHub) -> rp.BaseReader:
        return rp._base_reader(None, REPOSITORY, self.PULL, rp.Services(github=github))  # type: ignore[arg-type]  # a stub of the two reads

    def test_the_base_is_read_at_the_merge_base_and_checked_against_the_diffs_blob(self) -> None:
        blob = review_runtime._git_blob_id("sha1", len(BASE_DOCUMENT), [BASE_DOCUMENT])
        github = StubGitHub(blob, BASE_DOCUMENT)
        read = self.reader(github)

        self.assertEqual(BASE_DOCUMENT, read("docs/design.docx", blob[:7]))
        self.assertEqual(BASE_DOCUMENT, read("docs/other.docx", blob[:9]))
        self.assertEqual(
            [
                ("merge-base", "a" * 40, "b" * 40),
                ("blob", "c" * 40, "docs/design.docx"),
                ("blob", "c" * 40, "docs/other.docx"),
            ],
            github.calls,
            "the merge base is asked for once",
        )
        with self.assertRaises(RuntimeContractError):
            read("docs/design.docx", "1234567")
        with self.assertRaises(RuntimeContractError):
            self.reader(StubGitHub(blob, BASE_DOCUMENT + b"x"))("docs/design.docx", blob[:7])
        self.assertIsNone(self.reader(StubGitHub(blob, None))("docs/design.docx", blob[:7]))


if __name__ == "__main__":
    unittest.main()
