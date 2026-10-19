"""Reading a skill evaluation run's record: its review, findings, and ledger, and the checks that make a record
count."""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))

from review_records import Finding, LedgerEntry, Review

# The reviewer subagent models a run is judged on, each by its name and the model ID a run sets and must find in its
# messages. The one place they are named. A run sets the ID, never Claude Code's alias of the same name, since an alias
# can resolve to another generation of the family, which would then be recorded under this name.
MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
# The dated suffix a model's messages may carry after its ID.
DATED = re.compile(r"-[0-9]{8}")
# What a failure is, decided where the failure is made: a judgment the record shows, a run that recorded no review of
# its own, or a record that counts for nothing, since it is not the judged model's or no guard held its reviewers.
JUDGMENT, RUN, RECORD = "judgment", "run", "record"
# A record a run wrote, which its source has checked: a review record, which ejryan89-code-review-core's record contract
# holds, read through review, findings, and ledger_entries as that record's shapes, or a trigger record.
Record = Mapping[str, Any]


class RecordError(Exception):
    """A record that is missing, unreadable, refused, or lacks what an expectation reads. A record source's error
    says which kind of failure it is, RUN or RECORD; one an expectation's check raises is a JUDGMENT."""

    def __init__(self, message: str, kind: str = RUN) -> None:
        super().__init__(message)
        self.kind = kind


# Which of a scenario's runs on a model: None when each runs once, or its number from 1 when it runs several times.
Index = int | None


def review(record: Record) -> Review:
    found: Review = record["review"]
    return found


def findings(record: Record) -> list[Finding]:
    found: list[Finding] = record["findings"]
    return found


def ledger_entries(record: Record) -> list[LedgerEntry]:
    ledger: list[LedgerEntry] | None = record.get("ledger")
    if ledger is None:
        raise RecordError("the record has no ledger")
    return ledger


def entry_severity(record: Record, key: tuple[int, str]) -> str:
    """The severity of the ledger entry with this version and ID."""
    entry = next((item for item in ledger_entries(record) if (item["version"], item["id"]) == key), None)
    if entry is None:
        raise RecordError(f"the record's ledger has no v{key[0]}:{key[1]}")
    return str(entry["severity"])


def judgments(record: Record) -> dict[tuple[int, str], str]:
    """This review's disposition of each ledger entry it judged, keyed by the entry's version and ID."""
    version = review(record)["version"]
    judged: dict[tuple[int, str], str] = {}
    for entry in ledger_entries(record):
        for item in entry["dispositions"]:
            if item.get("version") == version:
                judged[(entry["version"], entry["id"])] = str(item.get("disposition"))
    return judged


def record_name(model: str, index: Index) -> str:
    """A record's file name under --records: the model's, and with several runs, the run's number after it."""
    return f"{model}.json" if index is None else f"{model}-{index}.json"


def names_model(name: str, model_id: str) -> bool:
    """Whether a message's model is that model ID, with or without a dated suffix."""
    return name == model_id or (name.startswith(model_id) and DATED.fullmatch(name[len(model_id) :]) is not None)


def unguarded(record: Record) -> str | None:
    """Why the record shows a reviewer the reviewer guard did not hold, or None. The guard's claim starts the role's
    read log, so a held reviewer's files_read is a count, if only 0; null means no guard ran."""
    reviewers = review(record).get("reviewers")
    if not reviewers:
        return "the record names no reviewer"
    loose = [str(reviewer["id"]) for reviewer in reviewers if reviewer.get("files_read") is None]
    return f"no reviewer guard held {', '.join(loose)}: its files_read is null" if loose else None


def unreported_model(record: Record, model: str) -> str | None:
    """Why the record does not show every reviewer on the model's ID, by the model each reviewer's result reports, or
    None. A reviewer reports its own model, so this is weaker than a run's check of the transcript."""
    others = [
        f"{reviewer['id']} on {reviewer.get('model', 'no model it reports')}"
        for reviewer in review(record).get("reviewers", [])
        if not names_model(str(reviewer.get("model", "")), MODELS[model])
    ]
    return f"its reviewers did not all run on {MODELS[model]}: {', '.join(others)}" if others else None
