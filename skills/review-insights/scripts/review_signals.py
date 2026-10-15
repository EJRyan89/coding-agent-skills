"""Consistency signals a report computes from the archive alone, before any model reads it: where reviews of the same
thing disagreed with themselves or with each other, which is the main symptom of ambiguous or contradictory guidance.

- Severity drift: a group of findings with the same repository, category, and headline, or one ledger entry's finding
  and its repeats in the range, that carries more than one severity.
- A verdict flip: two consecutive reviews of one pull request at the same head commit with different verdicts, with
  the findings each held open whose severity or presence differs. Findings are matched by their ledger entry, then by
  file and a line within ANCHOR_WINDOW of each other, so two initial reviews of the same code compare too.
- Reviewer disagreement: findings at the same head commit of one pull request, in the same file within ANCHOR_WINDOW
  lines, raised by reviewers that share none of their sources, at different severities.

The report also carries the guidance candidates `review_guidance.py` finds. Each kind keeps its SIGNALS_KEPT strongest
items, numbered, and counts them all.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from itertools import pairwise
from typing import Any

from flat_text import flat_text
from review_records import SEVERITY_RANK
from review_synthesis import finding_ref, group_key, headline

ANCHOR_WINDOW = 3
SIGNALS_KEPT = 20
EXAMPLES_KEPT = 3
DIFFERENCES_KEPT = 10
REPORTS_KEPT = 5
# Each signal the report keeps: the prefix of its items' IDs and the kind of its synthesis input lines.
SIGNALS = {
    "severity_drift": ("D", "severity-drift"),
    "verdict_flips": ("V", "verdict-flip"),
    "reviewer_disagreements": ("R", "reviewer-disagreement"),
    "guidance_overlaps": ("O", "guidance-overlap"),
    "unsupplied_references": ("U", "guidance-unsupplied"),
}

Pair = tuple[dict[str, Any], dict[str, Any], Any]
FindingKey = tuple[str, int, int, str]


def empty() -> dict[str, Any]:
    return {**{name: [] for name in SIGNALS}, "counts": dict.fromkeys(SIGNALS, 0)}


def kept(found: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Each signal's strongest SIGNALS_KEPT items, numbered, and how many there were."""
    signals: dict[str, Any] = {"counts": {name: len(found[name]) for name in SIGNALS}}
    for name, (prefix, _) in SIGNALS.items():
        signals[name] = [
            {"id": f"{prefix}{number}", **item} for number, item in enumerate(found[name][:SIGNALS_KEPT], start=1)
        ]
    return signals


def input_lines(signals: dict[str, Any]) -> list[dict[str, Any]]:
    """The synthesis input's line for each signal the report keeps."""
    return [{"kind": kind, **item} for name, (_, kind) in SIGNALS.items() for item in signals[name]]


def _review_key(record: dict[str, Any]) -> tuple[str, int, int]:
    return record["repository"].lower(), record["pull_request"]["number"], record["review"]["version"]


def _order(member: tuple[dict[str, Any], dict[str, Any]]) -> tuple[Any, ...]:
    return *_review_key(member[0]), member[1]["id"]


def _drift(basis: str, members: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    members = sorted(members, key=_order)
    record, first = members[0]
    # One example of each severity first, the most severe first, then the rest in review order.
    chosen: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for member in sorted(members, key=lambda item: -SEVERITY_RANK[item[1]["severity"]]):
        if member[1]["severity"] not in {finding["severity"] for _, finding in chosen}:
            chosen.append(member)
    chosen += [member for member in members if not any(member is item for item in chosen)]
    return {
        "basis": basis,
        "repository": record["repository"].lower(),
        "category": first["category"],
        "headline": headline(first),
        "severity": dict(sorted(Counter(finding["severity"] for _, finding in members).items())),
        "examples": [f"{finding['severity']} {finding_ref(item, finding)}" for item, finding in chosen[:EXAMPLES_KEPT]],
    }


def severity_drift(
    pairs: Iterable[Pair], records: Iterable[dict[str, Any]], entries: dict[FindingKey, FindingKey]
) -> list[dict[str, Any]]:
    """Every group of counted findings, and every ledger entry raised more than once in the range, that carries more
    than one severity: those with the most severities first, then the most findings."""
    groups: dict[tuple[str, str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for record, finding, _ in pairs:
        groups.setdefault(group_key(record, finding), []).append((record, finding))
    occurrences: dict[FindingKey, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for record in records:
        for finding in record["findings"]:
            key = (*_review_key(record), finding["id"])
            occurrences.setdefault(entries.get(key, key), []).append((record, finding))
    found = [
        _drift(basis, members)
        for basis, collection in (("group", groups), ("ledger", occurrences))
        for members in collection.values()
        if len({finding["severity"] for _, finding in members}) > 1
    ]
    return sorted(
        found,
        key=lambda item: (
            -len(item["severity"]),
            -sum(item["severity"].values()),
            item["repository"],
            item["headline"],
        ),
    )


def _held(record: dict[str, Any], by_version: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """The findings a review held open: its ledger's open entries, each where it was last raised by then, or for a
    record written before ledgers, its own findings."""
    if "ledger" not in record:
        return [_holding(record, finding, None) for finding in record["findings"]]
    version = record["review"]["version"]
    held = []
    for entry in record["ledger"]:
        if entry["state"] != "open":
            continue
        raised = [{"version": entry["version"], "id": entry["id"]}, *entry["repeats"]]
        latest = max((item for item in raised if item["version"] <= version), key=lambda item: item["version"])
        source = by_version.get(latest["version"], {"findings": []})
        finding = next((item for item in source["findings"] if item["id"] == latest["id"]), None)
        holding = _holding(record, finding or {"severity": entry["severity"], "path": None, "line": None}, None)
        repository, number, _ = _review_key(record)
        holding.update(
            entry=(entry["version"], entry["id"]), ref=f"{repository}#{number} v{latest['version']} {latest['id']}"
        )
        held.append(holding)
    return held


def _holding(record: dict[str, Any], finding: dict[str, Any], entry: Any) -> dict[str, Any]:
    ref = finding_ref(record, finding) if "id" in finding else ""
    return {
        "entry": entry,
        "ref": ref,
        "severity": finding["severity"],
        "path": finding["path"],
        "line": finding["line"],
    }


def _near(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return (
        first["path"] is not None
        and first["path"] == second["path"]
        and abs(first["line"] - second["line"]) <= ANCHOR_WINDOW
    )


def _same(first: dict[str, Any], second: dict[str, Any], *, by_entry: bool) -> bool:
    """Whether two held findings are one: by ledger entry, or else by file and a nearby line."""
    if by_entry:
        return first["entry"] is not None and first["entry"] == second["entry"]
    return _near(first, second)


def _matched(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    """The findings two reviews share, by ledger entry and then by place, and those only one of them held."""
    left, right, pairs = list(before), list(after), []
    for by_entry in (True, False):
        for item in list(left):
            match = next((other for other in right if _same(item, other, by_entry=by_entry)), None)
            if match is not None:
                pairs.append((item, match))
                left.remove(item)
                right.remove(match)
    return pairs, left, right


def _difference(before: dict[str, Any] | None, after: dict[str, Any] | None) -> str:
    place = next((item for item in (before, after) if item is not None and item["path"] is not None), None)
    where = f"{place['path']}:{place['line']}" if place else "(no location)"
    severities = [item["severity"] if item else "absent" for item in (before, after)]
    refs = ", ".join(item["ref"] for item in (before, after) if item is not None)
    return flat_text(f"{where} {severities[0]} -> {severities[1]} {refs}")


def _flip(records: list[dict[str, Any]], before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    by_version = {record["review"]["version"]: record for record in records}
    pairs, gone, added = _matched(_held(before, by_version), _held(after, by_version))
    differences = [_difference(a, b) for a, b in pairs if a["severity"] != b["severity"]]
    differences += [_difference(item, None) for item in gone] + [_difference(None, item) for item in added]
    repository, number, _ = _review_key(before)
    return {
        "repository": repository,
        "pull_number": number,
        "head_sha": before["pull_request"]["head_sha"],
        "versions": [before["review"]["version"], after["review"]["version"]],
        "verdicts": [before["review"]["verdict"], after["review"]["verdict"]],
        "differences": differences[:DIFFERENCES_KEPT],
    }


def verdict_flips(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each pair of consecutive reviews of a pull request at the same head commit whose verdicts differ."""
    pulls: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for record in records:
        pulls.setdefault(_review_key(record)[:2], []).append(record)
    found = []
    for _, history in sorted(pulls.items()):
        history.sort(key=lambda record: record["review"]["version"])
        by_head: dict[str, list[dict[str, Any]]] = {}
        for record in history:
            by_head.setdefault(record["pull_request"]["head_sha"], []).append(record)
        for runs in by_head.values():
            found += [
                _flip(history, before, after)
                for before, after in pairwise(runs)
                if before["review"]["verdict"] != after["review"]["verdict"]
            ]
    return found


def _reviewers(finding: dict[str, Any]) -> frozenset[str]:
    return frozenset(part.strip() for part in finding["source"].split(" + ") if part.strip())


def _disagree(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return first["severity"] != second["severity"] and not _reviewers(first) & _reviewers(second)


def _clusters(members: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[list[int]]:
    """The indexes of `members`, sorted by place, joined wherever two findings disagree: every pair is compared, so a
    finding near one end of a disagreement never hides it, and each cluster is reported once."""
    root = list(range(len(members)))

    def find(index: int) -> int:
        while root[index] != index:
            root[index] = root[root[index]]
            index = root[index]
        return index

    for index, (_, first) in enumerate(members):
        for later in range(index + 1, len(members)):
            second = members[later][1]
            if second["path"] != first["path"] or second["line"] - first["line"] > ANCHOR_WINDOW:
                break
            if _disagree(first, second):
                root[find(later)] = find(index)
    clusters: dict[int, list[int]] = {}
    for index in range(len(members)):
        clusters.setdefault(find(index), []).append(index)
    return [cluster for cluster in clusters.values() if len(cluster) > 1]


def reviewer_disagreements(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each place at one head commit of a pull request where reviewers that share no source gave findings within
    ANCHOR_WINDOW lines of each other different severities: the findings joined by such pairs, from the first."""
    heads: dict[tuple[str, int, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for record in records:
        key = (*_review_key(record)[:2], record["pull_request"]["head_sha"])
        heads.setdefault(key, []).extend((record, finding) for finding in record["findings"])
    found = []
    for (repository, number, head), members in sorted(heads.items()):
        members.sort(key=lambda member: (member[1]["path"], member[1]["line"], *_order(member)))
        for cluster in _clusters(members):
            anchor = members[cluster[0]][1]
            found.append(
                {
                    "repository": repository,
                    "pull_number": number,
                    "head_sha": head,
                    "path": anchor["path"],
                    "line": anchor["line"],
                    "reports": [
                        flat_text(f"{finding['severity']} {finding_ref(record, finding)} {finding['source']}")
                        for record, finding in (members[index] for index in cluster[:REPORTS_KEPT])
                    ],
                }
            )
    return found


def _severities(counts: dict[str, int]) -> str:
    return ",".join(f"{severity}:{count}" for severity, count in counts.items())


def facts(signals: dict[str, Any]) -> list[str]:
    """The signals as the report prints them, one fact per line."""
    lines = []
    for item in signals["severity_drift"]:
        lines.append(
            f"SEVERITY_DRIFT {item['id']} basis={item['basis']} repository={item['repository']} "
            f"severities={_severities(item['severity'])} {flat_text(item['category'])}: {item['headline']}"
        )
        lines += [f"DRIFT_EXAMPLE {item['id']} {example}" for example in item["examples"]]
    for item in signals["verdict_flips"]:
        versions = " ".join(
            f"v{version}={verdict}" for version, verdict in zip(item["versions"], item["verdicts"], strict=True)
        )
        pull = f"{item['repository']}#{item['pull_number']}"
        lines.append(f"VERDICT_FLIP {item['id']} {pull} head={item['head_sha'][:12]} {versions}")
        lines += [f"FLIP_DIFFERENCE {item['id']} {difference}" for difference in item["differences"]]
    for item in signals["reviewer_disagreements"]:
        lines.append(
            f"REVIEWER_DISAGREEMENT {item['id']} {item['repository']}#{item['pull_number']} "
            f"head={item['head_sha'][:12]} {flat_text(item['path'])}:{item['line']}"
        )
        lines += [f"DISAGREEING {item['id']} {report}" for report in item["reports"]]
    for item in signals["guidance_overlaps"]:
        places = " ".join(f"{flat_text(path)}:{line}" for path, line in zip(item["paths"], item["lines"], strict=True))
        lines.append(
            f"GUIDANCE_OVERLAP {item['id']} relation={item['relation']} similarity={item['similarity']} "
            f"repository={item['repository']} {places}"
        )
    for item in signals["unsupplied_references"]:
        lines.append(
            f"GUIDANCE_UNSUPPLIED {item['id']} repository={item['repository']} reviewer={flat_text(item['reviewer'])} "
            f"{flat_text(item['path'])}:{item['line']} {flat_text(item['reference'])}"
        )
    lines += [
        f"SIGNALS_OMITTED {name} {count - SIGNALS_KEPT}"
        for name, count in signals["counts"].items()
        if count > SIGNALS_KEPT
    ]
    return lines


def render(signals: dict[str, Any]) -> list[str]:
    """The report's Markdown section of signals, or nothing when there are none."""
    sections = [
        (
            "Severity drift",
            [
                f"{item['id']} ({item['basis']}) {item['repository']} {item['category']}: {item['headline']} — "
                f"{_severities(item['severity'])}; e.g. {'; '.join(item['examples'])}"
                for item in signals["severity_drift"]
            ],
        ),
        (
            "Verdicts that changed without a code change",
            [
                f"{item['id']} {item['repository']}#{item['pull_number']} at {item['head_sha'][:12]}: "
                + ", ".join(
                    f"v{version} {verdict}" for version, verdict in zip(item["versions"], item["verdicts"], strict=True)
                )
                + (f" — {'; '.join(item['differences'])}" if item["differences"] else "")
                for item in signals["verdict_flips"]
            ],
        ),
        (
            "Reviewer disagreement",
            [
                f"{item['id']} {item['repository']}#{item['pull_number']} {item['path']}:{item['line']} — "
                f"{'; '.join(item['reports'])}"
                for item in signals["reviewer_disagreements"]
            ],
        ),
        (
            "Guidance overlap candidates",
            [
                f"{item['id']} {item['relation']} ({item['similarity']}) {item['repository']}: "
                + " / ".join(
                    f"{path}:{line} “{text}”"
                    for path, line, text in zip(item["paths"], item["lines"], item["texts"], strict=True)
                )
                for item in signals["guidance_overlaps"]
            ],
        ),
        (
            "Guidance references not given to the reviewer",
            [
                f"{item['id']} {item['repository']} {item['reviewer']}: {item['path']}:{item['line']} names "
                f"{item['reference']}"
                for item in signals["unsupplied_references"]
            ],
        ),
    ]
    lines: list[str] = []
    # Each row is one bullet line, and a record's category, path, or reviewer ID may hold a line break.
    for title, rows in sections:
        if rows:
            lines += [f"### {title}", "", *(f"- {flat_text(row)}" for row in rows), ""]
    omitted = [f"{name}: {count - SIGNALS_KEPT}" for name, count in signals["counts"].items() if count > SIGNALS_KEPT]
    if omitted:
        lines += [f"Omitted beyond the first {SIGNALS_KEPT} of each: {', '.join(omitted)}.", ""]
    return ["## Consistency signals", "", *lines] if lines else []
