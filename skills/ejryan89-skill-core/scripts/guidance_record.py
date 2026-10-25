"""The guidance record: the published guidance pages this repository's guidance audit reads, and each runtime's
changelog. The one reader and writer of `.claude/skills/audit-repository/references/guidance.json`.

The audit-repository skill's guidance mode writes its briefs from the pages and appends a tag to each page's reads,
and tools/runtime_changelogs.py lists the changelog entries since each `read_through` and the pages that moved. The
file is one JSON object:

    {"pages": [{"url": "<https URL>", "subject": "<subject>", "governs": "<what it governs>", "read": ["vX.Y.Z"]}],
     "changelogs": [{"subject": "<subject>", "url": "<https URL>", "format": "markdown" | "github-releases",
                     "read_through": "<release>"}]}

A subject is a runtime the deployer supports or Python, a read is a release tag as release_tags.py defines one, and a
`read_through` is the last release of that runtime whose changelog entries the guidance audit read, which the
maintainer moves forward by hand in the pull request that records the audit. `changelogs` may be left out; each
subject names one changelog at most, and each page is listed once. One document has one address in the whole record:
a changelog that is also read as a page is written the same way in both lists, and two addresses canonical_url gives
one form, such as a raw GitHub file named by its branch and by `refs/heads/` and its branch, are refused.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import json_reader
import versions
from release_tags import is_release_tag
from typed_fields import FieldError, FieldReader

# The guidance record, relative to the top of the repository it describes.
GUIDANCE_PATH = ".claude/skills/audit-repository/references/guidance.json"
# The subject of Python's pages, beside the runtimes the deployer supports.
PYTHON_SUBJECT = "python"
PAGE_SCHEME = "https://"
# How a runtime publishes its changelog: one Markdown file whose headings name each release, or GitHub releases, one
# per tag, read through the REST API.
CHANGELOG_FORMATS = ("markdown", "github-releases")


class GuidanceRecordError(ValueError):
    """A guidance record that is missing, cannot be read, or is not in the shape the module docstring gives."""


@dataclass(frozen=True)
class Page:
    """One page of a runtime's, or Python's, published guidance, and the release tags at which it was read."""

    url: str
    subject: str
    governs: str
    read: tuple[str, ...]


@dataclass(frozen=True)
class Changelog:
    """Where a runtime publishes the changes each release makes, in one of CHANGELOG_FORMATS, and the last release
    whose entries the guidance audit read."""

    subject: str
    url: str
    format: str
    read_through: str


@dataclass(frozen=True)
class GuidanceRecord:
    pages: list[Page]
    changelogs: list[Changelog]


READ = FieldReader()


def read(path: Path, subjects: Sequence[str]) -> GuidanceRecord:
    """The record's pages and changelogs, each about one of `subjects`, or GuidanceRecordError naming what is wrong
    with it."""
    name = path.as_posix()
    try:
        document = json_reader.read_file(path)
    except FileNotFoundError as exc:
        raise GuidanceRecordError(f"no guidance record at {name}") from exc
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise GuidanceRecordError(f"cannot read the guidance record {name}: {exc}") from exc
    try:
        fields = READ.json_object(document, "$", required={"pages"}, optional={"changelogs"})
        pages = READ.array(fields["pages"], "$.pages", partial(_page, subjects=subjects), nonempty=True)
        changelogs = READ.array(fields.get("changelogs", []), "$.changelogs", partial(_changelog, subjects=subjects))
    except FieldError as exc:
        raise GuidanceRecordError(f"{name}: {exc}") from exc
    for what, values in (
        ("lists", [page.url for page in pages]),
        ("names a changelog for", [changelog.subject for changelog in changelogs]),
    ):
        repeated = sorted({value for value in values if values.count(value) > 1})
        if repeated:
            raise GuidanceRecordError(f"{name} {what} {', '.join(repeated)} more than once")
    spellings: dict[str, set[str]] = {}
    for url in (*(page.url for page in pages), *(changelog.url for changelog in changelogs)):
        spellings.setdefault(canonical_url(url), set()).add(url)
    differing = sorted(sorted(written) for written in spellings.values() if len(written) > 1)
    if differing:
        raise GuidanceRecordError(
            f"{name} names one document as {' and '.join(differing[0])}; write each document's address one way"
        )
    return GuidanceRecord(pages, changelogs)


def canonical_url(url: str) -> str:
    """One form for the addresses that name the same document: the host in lower case, as urlsplit already gives the
    scheme, and a raw GitHub file's `refs/heads/<branch>` written as its branch, as raw.githubusercontent.com serves
    both."""
    parts = urlsplit(url)
    host = parts.netloc.lower()
    path = parts.path
    # "", the owner, the repository, then "refs" and "heads" where the branch is spelled out in full.
    segments = path.split("/")
    if host == "raw.githubusercontent.com" and segments[3:5] == ["refs", "heads"]:
        path = "/".join([*segments[:3], *segments[5:]])
    return parts._replace(netloc=host, path=path).geturl()


def document(record: GuidanceRecord) -> dict[str, object]:
    """The record as the JSON object `read` reads, leaving `changelogs` out when there is none."""
    pages = [{"url": p.url, "subject": p.subject, "governs": p.governs, "read": list(p.read)} for p in record.pages]
    changelogs = [
        {"subject": c.subject, "url": c.url, "format": c.format, "read_through": c.read_through}
        for c in record.changelogs
    ]
    return {"pages": pages, **({"changelogs": changelogs} if changelogs else {})}


def _url(value: object, path: str) -> str:
    url = READ.string(value, path, single_line=True, trimmed=True)
    if not url.startswith(PAGE_SCHEME) or any(character.isspace() for character in url):
        raise READ.fail(path, f"a URL starting {PAGE_SCHEME} without whitespace", json.dumps(url))
    return url


def _page(value: object, path: str, subjects: Sequence[str]) -> Page:
    fields = READ.json_object(value, path, required={"url", "subject", "governs", "read"})
    url = _url(fields["url"], f"{path}.url")
    subject = READ.one_of(fields["subject"], f"{path}.subject", subjects)
    governs = READ.string(fields["governs"], f"{path}.governs", single_line=True, trimmed=True)
    reads = READ.array(fields["read"], f"{path}.read", _tag)
    if len(set(reads)) != len(reads):
        raise READ.fail(f"{path}.read", "tags each named once", "a tag named twice")
    return Page(url, subject, governs, tuple(reads))


def _changelog(value: object, path: str, subjects: Sequence[str]) -> Changelog:
    fields = READ.json_object(value, path, required={"subject", "url", "format", "read_through"})
    subject = READ.one_of(fields["subject"], f"{path}.subject", subjects)
    url = _url(fields["url"], f"{path}.url")
    form = READ.one_of(fields["format"], f"{path}.format", CHANGELOG_FORMATS)
    release = READ.string(fields["read_through"], f"{path}.read_through", single_line=True, trimmed=True)
    if versions.read(release) is None:
        raise READ.fail(f"{path}.read_through", "a release of numbers joined by dots", json.dumps(release))
    return Changelog(subject, url, form, release)


def _tag(value: object, path: str) -> str:
    tag = READ.string(value, path, single_line=True, trimmed=True)
    if not is_release_tag(tag):
        raise READ.fail(path, "a release tag vX.Y.Z", json.dumps(tag))
    return tag
