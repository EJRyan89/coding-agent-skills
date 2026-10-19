"""What finalize and prepare report: reviewer summaries, canary stats, and ready runs."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from review_records import Review, source_parts
from review_run import ReadCount
from review_runtime import NATIVE_EFFORT_RUNTIMES


def reviewer_summaries(
    roles: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    attempts: dict[str, int],
    *,
    single: bool,
    seconds: dict[str, int] | None = None,
    models: dict[str, str] | None = None,
    reads: dict[str, ReadCount] | None = None,
) -> list[dict[str, Any]]:
    """Which reviewers ran, the files each covered, the findings it raised, how often it was rerun, its time, and the
    snapshot files it read.

    A finding merged from several specialists counts for each of them. With `reads`, a reviewer it does not count, as
    no guard held it, reads null for both counts: unknown, not zero.
    """
    summaries = []
    for role in roles:
        raised = (
            len(findings) if single else sum(1 for finding in findings if role["id"] in source_parts(finding["source"]))
        )
        summaries.append(
            {
                "id": role["id"],
                "category": role["category"],
                "files": len(role["files"]),
                "findings": raised,
                "retries": attempts.get(role["id"], 0),
                "dispositions_only": role["dispositions_only"],
            }
        )
        if seconds and role["id"] in seconds:
            summaries[-1]["seconds"] = seconds[role["id"]]
        if models and role["id"] in models:
            summaries[-1]["model"] = models[role["id"]]
        if reads is not None:
            counted = reads.get(role["id"])
            summaries[-1]["files_read"] = None if counted is None else counted["files"]
            summaries[-1]["bytes_read"] = None if counted is None else counted["bytes"]
    return summaries


def _count_or_unknown(value: int | None) -> str:
    return "unknown" if value is None else str(value)


def stats_lines(review: Review) -> list[str]:
    """A canary's snapshot and read counts, as finalize prints them after `STATS <selector> `: the numbers its record
    holds, and none for a field the record leaves out."""
    lines = []
    snapshot = review.get("snapshot")
    if snapshot is not None:
        seconds = " ".join(f"{phase}={value:.1f}s" for phase, value in snapshot["seconds"].items())
        # Absent from records written before exclusions were counted.
        excluded = (
            ""
            if "excluded" not in snapshot
            else " excluded="
            + (",".join(f"{reason}:{count}" for reason, count in snapshot["excluded"].items()) or "none")
        )
        lines.append(
            f"snapshot source={snapshot['source']} files={snapshot['files']} bytes={snapshot['bytes']}{excluded} "
            f"{seconds}"
        )
    for reviewer in review.get("reviewers", []):
        if "files_read" in reviewer:
            lines.append(
                f"reviewer {reviewer['id']} files_read={_count_or_unknown(reviewer['files_read'])} "
                f"bytes_read={_count_or_unknown(reviewer['bytes_read'])}"
            )
    return lines


def _print_ready(result: dict[str, Any]) -> None:
    print(f"RUN {result['selector']} {result['run']}")
    for note in result["notes"]:
        print(f"NOTE {result['selector']} {note}")
    if result["dispatch"] == "copilot-host":
        print(f"HOST copilot-cli {result['run']}")
        return
    if result["dispatch"] == "inline":
        print(f"INLINE {result['run']}")
        return
    for role in result["roles"]:
        print(f"ROLE {role['id']} {role['prompt_file']}")
        _print_settings(result["selector"], role, result["runtime"])


def _print_settings(selector: str, role: dict[str, Any], runtime: str) -> None:
    """The model a role's trusted profile asks for, and on a runtime whose native delegation takes one, the effort the
    run recorded for it; the orchestrator starts that role's subagent with them."""
    if role.get("model"):
        print(f"MODEL {selector} {role['id']} {role['model']}")
    if role.get("effort") and runtime in NATIVE_EFFORT_RUNTIMES:
        print(f"EFFORT {selector} {role['id']} {role['effort']}")
