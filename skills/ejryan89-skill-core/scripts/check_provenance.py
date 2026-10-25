"""The guidance an audit skill's checks derive from: the reader of a skill's `references/provenance.json`.

`audit-skill` and `audit-ai-config` judge other people's skills and configuration by rules that published
guidance sets, so each names, for every check it can report, the page the check rests on and the passage of it, or,
where no page governs the check, the decision of this source it rests on. The file is one JSON object:

    {"checks": {"<check>": [{"page": "<https URL>", "passage": "<sentence>"}, {"decision": "<sentence>"}]}}

A page source gives the page's URL and, in a sentence, the passage the check rests on; a decision source gives, in a
sentence, the decision and where this source records it. Every value is one line. Every check the skill's scripts can
report has an entry, every entry names one of them, and each entry holds at least one source: `load` refuses a file
that differs, so a check added without its source, or retired with its entry left behind, fails the skill's run and
its suite alike. The skills carry the file as text and never fetch a page; the repository's guidance audit compares
each passage with the live page.

`lines` gives a check's sources as the lines a skill prints when asked why a finding holds:
`PAGE <check> <url> <passage>` and `DECISION <check> <sentence>`.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import json_reader
from typed_fields import FieldError, FieldReader, describe

# Where a skill keeps the file, relative to its folder.
PROVENANCE_FILE = Path("references") / "provenance.json"
PAGE_SCHEME = "https://"


class ProvenanceError(ValueError):
    """A provenance file that cannot be read, is not in the shape the module docstring gives, or does not hold one
    entry for each check."""


class ProvenanceFieldError(FieldError, ProvenanceError):
    """A field of a provenance file that is not of the kind the shape gives."""


READ = FieldReader(ProvenanceFieldError)


@dataclass(frozen=True)
class Source:
    """What a check rests on: a published page and the passage of it, or, with `page` None, a decision of this
    source, stated in `text`."""

    page: str | None
    text: str


def load(path: Path, checks: Collection[str]) -> dict[str, tuple[Source, ...]]:
    """Each check's sources, read from `path`, which must hold an entry for every one of `checks` and no other."""
    try:
        document = json_reader.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProvenanceError(f"cannot read {path.as_posix()}: {exc}") from exc
    entries = READ.json_object(READ.json_object(document, "$", required={"checks"})["checks"], "$.checks")
    missing = sorted(set(checks) - set(entries))
    unknown = sorted(set(entries) - set(checks))
    if missing or unknown:
        problems = [f"no entry for {', '.join(missing)}"] if missing else []
        problems += [f"an entry for no check: {', '.join(unknown)}"] if unknown else []
        raise ProvenanceError(f"{path.as_posix()}: {'; '.join(problems)}")
    return {
        check: tuple(READ.array(entries[check], f"$.checks.{check}", _source, nonempty=True))
        for check in sorted(entries)
    }


def _source(value: Any, path: str) -> Source:
    fields = READ.json_object(value, path)
    if "page" not in fields:
        READ.json_object(value, path, required={"decision"})
        return Source(None, READ.string(fields["decision"], f"{path}.decision", single_line=True, trimmed=True))
    READ.json_object(value, path, required={"page", "passage"})
    page = READ.string(fields["page"], f"{path}.page", single_line=True, trimmed=True)
    if not page.startswith(PAGE_SCHEME) or any(character.isspace() for character in page):
        raise READ.fail(f"{path}.page", f"a URL starting {PAGE_SCHEME} without whitespace", describe(page))
    return Source(page, READ.string(fields["passage"], f"{path}.passage", single_line=True, trimmed=True))


def lines(provenance: Mapping[str, tuple[Source, ...]], check: str) -> list[str]:
    """The lines naming what `check` rests on, one per source, in the file's order."""
    return [
        f"PAGE {check} {source.page} {source.text}" if source.page else f"DECISION {check} {source.text}"
        for source in provenance[check]
    ]
