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
items, numbered, and counts them all. Drift is strongest with the most severities, then the most findings; a flip
between approval and requested changes, then the one with the most findings that differ; a disagreement with the
widest spread of severities, then the most findings; and an overlap a conflict, then the most similar. Unsupplied
references are kept in the order found, by reviewer, file, and line.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from itertools import pairwise
from typing import Any

from flat_text import flat_text
from review_records import SEVERITIES, SEVERITY_RANK, finding_reference, source_parts
from review_synthesis import finding_ref, group_key, headline
from typed_fields import FieldReader

ANCHOR_WINDOW = 3
SIGNALS_KEPT = 20
EXAMPLES_KEPT = 3
DIFFERENCES_KEPT = 10
REPORTS_KEPT = 5
# A flip between these two verdicts is a contradiction; one to or from an incomplete review may be a review that did
# not finish.
DECISIVE_VERDICTS = frozenset({"APPROVED", "CHANGES_REQUESTED"})
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
Read = Callable[[Any, str], Any]
READ = FieldReader()


def _text(value: Any, path: str) -> str:
    """Text a record or a guidance file gave, which may be blank."""
    return READ.string(value, path, empty=True)


def _name(value: Any, path: str) -> str:
    return READ.string(value, path)


def _number(value: Any, path: str) -> int:
    return READ.integer(value, path, minimum=1)


def _line(value: Any, path: str) -> int:
    return READ.integer(value, path)


def _texts(value: Any, path: str) -> list[str]:
    return READ.array(value, path, _text)


def _pair(item: Read) -> Read:
    def read(value: Any, path: str) -> list[Any]:
        found = READ.array(value, path, item, maximum=2)
        if len(found) != 2:
            raise READ.fail(path, "an array of 2 items", f"an array of {len(found)} items")
        return found

    return read


def _one_of(*allowed: str) -> Read:
    return lambda value, path: READ.one_of(value, path, allowed)


def _severity_counts(value: Any, path: str) -> dict[str, int]:
    counts = READ.json_object(value, path, optional=SEVERITIES)
    return {severity: _number(count, f"{path}.{severity}") for severity, count in counts.items()}


_PULL: dict[str, Read] = {"id": _name, "repository": _name, "pull_number": _number, "head_sha": _name}
# Each signal's fields, as the behavior contract's signals tables state them, each with how it is read.
FIELDS: dict[str, dict[str, Read]] = {
    "severity_drift": {
        "id": _name,
        "basis": _one_of("group", "ledger"),
        "repository": _name,
        "category": _text,
        "headline": _text,
        "severity": _severity_counts,
        "examples": _texts,
    },
    "verdict_flips": {
        **_PULL,
        "versions": _pair(_number),
        "verdicts": _pair(_name),
        "differences": _texts,
    },
    "reviewer_disagreements": {**_PULL, "path": _text, "line": _line, "reports": _texts},
    "guidance_overlaps": {
        "id": _name,
        "repository": _name,
        "relation": _one_of("conflict", "duplicate"),
        "similarity": READ.number,
        "differs": _texts,
        "paths": _pair(_text),
        "lines": _pair(_line),
        "texts": _pair(_text),
        "reviewers": _texts,
    },
    "unsupplied_references": {
        "id": _name,
        "repository": _name,
        "reviewer": _text,
        "path": _text,
        "line": _line,
        "reference": _text,
    },
}


def read(value: Any, path: str = "signals") -> dict[str, Any]:
    """A report's signals, every field read as FIELDS gives it, each kind's count at least the items it kept; a
    `FieldError` names the first field that is not."""
    signals = READ.json_object(value, path, required=[*SIGNALS, "counts"])
    counts = READ.json_object(signals["counts"], f"{path}.counts", required=SIGNALS)
    for name, fields in FIELDS.items():
        items = READ.array(signals[name], f"{path}.{name}")
        for index, item in enumerate(items):
            where = f"{path}.{name}[{index}]"
            READ.json_object(item, where, required=fields)
            for field, reader in fields.items():
                reader(item[field], f"{where}.{field}")
        READ.integer(counts[name], f"{path}.counts.{name}", minimum=len(items))
    return signals


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
            entry=(entry["version"], entry["id"]),
            ref=finding_reference(repository, number, latest["version"], latest["id"]),
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


def _flip(records: list[dict[str, Any]], before: dict[str, Any], after: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """The flip two reviews make, and how many findings differ between them before DIFFERENCES_KEPT cuts them."""
    by_version = {record["review"]["version"]: record for record in records}
    pairs, gone, added = _matched(_held(before, by_version), _held(after, by_version))
    differences = [_difference(a, b) for a, b in pairs if a["severity"] != b["severity"]]
    differences += [_difference(item, None) for item in gone] + [_difference(None, item) for item in added]
    repository, number, _ = _review_key(before)
    flip = {
        "repository": repository,
        "pull_number": number,
        "head_sha": before["pull_request"]["head_sha"],
        "versions": [before["review"]["version"], after["review"]["version"]],
        "verdicts": [before["review"]["verdict"], after["review"]["verdict"]],
        "differences": differences[:DIFFERENCES_KEPT],
    }
    return flip, len(differences)


def flip_rank(flip: dict[str, Any], differences: int) -> tuple[Any, ...]:
    """Strongest first: a flip between approval and requested changes, then the most findings that differ, then by
    repository, pull request, and versions."""
    decisive = set(flip["verdicts"]) == DECISIVE_VERDICTS
    return not decisive, -differences, flip["repository"], flip["pull_number"], flip["versions"]


def verdict_flips(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each pair of consecutive reviews of a pull request at the same head commit whose verdicts differ, strongest
    first (see flip_rank)."""
    pulls: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for record in records:
        pulls.setdefault(_review_key(record)[:2], []).append(record)
    found: list[tuple[dict[str, Any], int]] = []
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
    return [flip for flip, _ in sorted(found, key=lambda item: flip_rank(*item))]


def _reviewers(finding: dict[str, Any]) -> frozenset[str]:
    return frozenset(source_parts(finding["source"]))


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


def _reported(members: list[tuple[dict[str, Any], dict[str, Any]]], cluster: list[int]) -> list[int]:
    """The REPORTS_KEPT findings of a cluster it reports, in place order: the first pair that disagrees, so the cut
    never hides what makes it a disagreement, then one of each other severity, then the rest from the first."""
    chosen = next(
        [first, second]
        for position, first in enumerate(cluster)
        for second in cluster[position + 1 :]
        if _disagree(members[first][1], members[second][1])
    )
    for index in cluster:
        if members[index][1]["severity"] not in {members[item][1]["severity"] for item in chosen}:
            chosen.append(index)
    chosen += [index for index in cluster if index not in chosen]
    return sorted(chosen[:REPORTS_KEPT])


def _spread(members: list[tuple[dict[str, Any], dict[str, Any]]], cluster: list[int]) -> int:
    ranks = [SEVERITY_RANK[members[index][1]["severity"]] for index in cluster]
    return max(ranks) - min(ranks)


def reviewer_disagreements(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each place at one head commit of a pull request where reviewers that share no source gave findings within
    ANCHOR_WINDOW lines of each other different severities: the findings joined by such pairs, from the first. The
    widest spread of severities comes first, then the place with the most findings, then by place."""
    heads: dict[tuple[str, int, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for record in records:
        key = (*_review_key(record)[:2], record["pull_request"]["head_sha"])
        heads.setdefault(key, []).extend((record, finding) for finding in record["findings"])
    found: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for (repository, number, head), members in sorted(heads.items()):
        members.sort(key=lambda member: (member[1]["path"], member[1]["line"], *_order(member)))
        for cluster in _clusters(members):
            anchor = members[cluster[0]][1]
            item = {
                "repository": repository,
                "pull_number": number,
                "head_sha": head,
                "path": anchor["path"],
                "line": anchor["line"],
                "reports": [
                    flat_text(f"{finding['severity']} {finding_ref(record, finding)} {finding['source']}")
                    for record, finding in (members[index] for index in _reported(members, cluster))
                ],
            }
            rank = (-_spread(members, cluster), -len(cluster), repository, number, head, anchor["path"], anchor["line"])
            found.append((rank, item))
    return [item for _, item in sorted(found, key=lambda entry: entry[0])]


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
