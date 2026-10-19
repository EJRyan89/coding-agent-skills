"""The closed vocabularies a review writes into its records, reports, and run state.

Each is defined once, here: its type, a `Literal` that mypy holds every value written to it against, and its members,
a tuple in the order an error naming them lists them. The records, the configuration, the pipeline, and the readers of
records import them from here. A value stays the string it names, so a record, a report, or a status line written
with one is the same text it always was.
"""

from __future__ import annotations

from typing import Literal, get_args

# How much a finding matters, most severe first.
Severity = Literal["MUST_FIX", "SHOULD_FIX", "SUGGESTION"]
SEVERITIES: tuple[Severity, ...] = get_args(Severity)
# Each severity's rank: a report orders findings by it, and a repeat needs a target at least as severe.
SEVERITY_RANK: dict[Severity, int] = {"SUGGESTION": 1, "SHOULD_FIX": 2, "MUST_FIX": 3}
# Each severity and the title of its section in a report, most severe first, which a legacy report counts it under.
SEVERITY_SECTIONS: tuple[tuple[Severity, str], ...] = (
    ("MUST_FIX", "MUST FIX"),
    ("SHOULD_FIX", "SHOULD FIX"),
    ("SUGGESTION", "SUGGESTIONS"),
)

# What a review concludes: changes requested by its open findings, else incomplete when changed source could not be
# reviewed in full, else approved.
Verdict = Literal["APPROVED", "CHANGES_REQUESTED", "INCOMPLETE"]
VERDICTS: tuple[Verdict, ...] = get_args(Verdict)

# How a review judged a prior finding or a review comment.
Disposition = Literal["addressed", "partially_addressed", "still_present", "superseded", "unable_to_verify"]
DISPOSITIONS: tuple[Disposition, ...] = get_args(Disposition)
# The dispositions that leave a finding open.
OPEN_DISPOSITIONS: frozenset[Disposition] = frozenset({"still_present", "partially_addressed"})
# Where a finding ledger entry stands, and the state each disposition leaves it in.
LedgerState = Literal["open", "closed", "unverified"]
LEDGER_STATE_NAMES: tuple[LedgerState, ...] = get_args(LedgerState)
LEDGER_STATES: dict[Disposition, LedgerState] = {
    "still_present": "open",
    "partially_addressed": "open",
    "addressed": "closed",
    "superseded": "closed",
    "unable_to_verify": "unverified",
}

# How a review's reviewer roles were worked, as its record's `review.dispatch` says: as native subagents (or by a Claude
# Code Workflow), by the bounded Copilot CLI host, or inline by the orchestrating session itself, one role at a time.
DispatchMode = Literal["subagents", "copilot-host", "inline"]
DISPATCH_MODES: tuple[DispatchMode, ...] = get_args(DispatchMode)
