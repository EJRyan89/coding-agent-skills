"""Regression tests for the reader and writer of the guidance record."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import guidance_record
from guidance_record import Changelog, GuidanceRecord, GuidanceRecordError, Page


def page(url: str, subject: str = "alpha") -> dict[str, object]:
    return {"url": url, "subject": subject, "governs": f"what {url} governs", "read": ["v0.1.0"]}


def changelog(subject: str, **fields: str) -> dict[str, str]:
    url = f"https://example.com/{subject}.md"
    return {"subject": subject, "url": url, "format": "markdown", "read_through": "1.2.3", **fields}


def write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "guidance.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_a_record_reads_its_pages_and_changelogs_and_may_hold_no_changelog(tmp_path: Path) -> None:
    path = write(tmp_path, {"pages": [page("https://example.com/a")], "changelogs": [changelog("alpha")]})
    assert guidance_record.read(path, ["alpha"]) == GuidanceRecord(
        [Page("https://example.com/a", "alpha", "what https://example.com/a governs", ("v0.1.0",))],
        [Changelog("alpha", "https://example.com/alpha.md", "markdown", "1.2.3")],
    )
    path = write(tmp_path, {"pages": [page("https://example.com/a")]})
    assert guidance_record.read(path, ["alpha"]).changelogs == []


def test_the_document_is_what_the_reader_reads_and_leaves_out_an_empty_changelog_list(tmp_path: Path) -> None:
    written = {"pages": [page("https://example.com/a")], "changelogs": [changelog("alpha", format="github-releases")]}
    record = guidance_record.read(write(tmp_path, written), ["alpha"])
    assert guidance_record.document(record) == written
    assert guidance_record.document(GuidanceRecord(record.pages, [])) == {"pages": written["pages"]}


@pytest.mark.parametrize(
    ("changelogs", "reason"),
    [
        pytest.param({}, "$.changelogs must be an array", id="not-an-array"),
        pytest.param([{**changelog("alpha"), "extra": "1"}], "unknown field", id="extra-field"),
        pytest.param([changelog("gamma")], "$.changelogs[0].subject must be one of", id="subject"),
        pytest.param([changelog("alpha", url="http://example.com")], "must be a URL starting https://", id="http"),
        pytest.param([changelog("alpha", url="https://example.com/a b")], "without whitespace", id="space"),
        pytest.param([changelog("alpha", format="rss")], "$.changelogs[0].format must be one of", id="format"),
        pytest.param([changelog("alpha", read_through="v1.2.3")], "a release of numbers joined by dots", id="prefix"),
        pytest.param([changelog("alpha", read_through="1")], "a release of numbers joined by dots", id="one-number"),
        pytest.param(
            [changelog("alpha"), changelog("alpha")], "names a changelog for alpha more than once", id="subject-twice"
        ),
    ],
)
def test_a_malformed_changelog_fails_with_the_reason(tmp_path: Path, changelogs: object, reason: str) -> None:
    path = write(tmp_path, {"pages": [page("https://example.com/a")], "changelogs": changelogs})
    with pytest.raises(GuidanceRecordError) as caught:
        guidance_record.read(path, ["alpha", "python"])
    assert reason in str(caught.value)


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        pytest.param({"pages": []}, "$.pages must be a non-empty array", id="no-pages"),
        pytest.param({"pages": [page("https://example.com/a")], "extra": 1}, "unknown field", id="extra-field"),
        pytest.param({"pages": [{**page("https://example.com/a"), "read": ["v1"]}]}, "a release tag", id="tag"),
        pytest.param(
            {"pages": [page("https://example.com/a"), page("https://example.com/a", "python")]},
            "lists https://example.com/a more than once",
            id="page-twice",
        ),
    ],
)
def test_a_malformed_page_list_fails_with_the_reason(tmp_path: Path, document: object, reason: str) -> None:
    with pytest.raises(GuidanceRecordError) as caught:
        guidance_record.read(write(tmp_path, document), ["alpha", "python"])
    assert reason in str(caught.value)


def test_a_missing_or_unreadable_record_fails_with_the_reason(tmp_path: Path) -> None:
    with pytest.raises(GuidanceRecordError, match="no guidance record at"):
        guidance_record.read(tmp_path / "missing.json", ["alpha"])
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    with pytest.raises(GuidanceRecordError, match="cannot read the guidance record"):
        guidance_record.read(tmp_path / "broken.json", ["alpha"])
