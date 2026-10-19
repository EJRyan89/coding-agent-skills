"""Loading a skill's evaluation scenarios: their fixtures, documents, reviewer files, trigger requests, and the
expectations each run is judged against."""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("code-review-core")))
sys.path.insert(0, str(sibling_scripts("review-document")))

from git_client import GitClient
from review_document import specialists_manifest
from review_records import DISPOSITIONS as RECORD_DISPOSITIONS
from review_records import LEDGER_ID, SEVERITY_RANK
from review_runtime import DOCUMENT_STAGES, RuntimeContractError, declared_reviewer_files, validate_adapter_manifest
from suite_names import is_suite_name

from deployer import fsops
from tools import runtime_canary
from tools.skill_evals_records import Record, RecordError, entry_severity, findings, judgments, review

PLATFORM_ALLOWED = {
    "pathlib.PureWindowsPath": "refuses a path a scenario names that Windows reads as absolute, a drive's included, "
    "on every platform, since a scenario runs on each"
}


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "skill-evals"
# The record's own vocabulary, from code-review-core's record module, in a stable order for messages: severities from
# the lowest rank, dispositions alphabetically.
SEVERITIES = tuple(sorted(SEVERITY_RANK, key=SEVERITY_RANK.__getitem__))
VERDICTS = ("APPROVED", "CHANGES_REQUESTED", "INCOMPLETE")
DISPOSITIONS = tuple(sorted(RECORD_DISPOSITIONS))
TRIGGER = "trigger"
MODES = ("initial", "re-review", TRIGGER)
SCENARIO_FIELDS = frozenset({"skill", "mode", "expectations"})
# A trigger scenario's fields, where its shipped skills are, the record each of its runs writes in its folder, and the
# turns a request's session may take: enough to read a file before choosing, few enough that a session which starts a
# skill stops soon after, since the start is all that is judged.
TRIGGER_FIELDS = frozenset({"skill", "mode", "requests", "near_misses"})
SHIPPED_SKILLS = "skills"
# The folder in a run's own folder where a document scenario's document is written for the skill to review.
DOCUMENT_FOLDER = "document"
# A repository reviewer's scenario: the skill that runs a reviewer manifest on a fixture, where this repository's own
# skills are, and the folders of a reviewer whose files are placed, besides its SKILL.md and what its manifest declares.
REVIEWER_SKILL = "review-prs"
REPOSITORY_SKILLS = PurePosixPath(".claude", "skills")
REVIEWER_FOLDERS = ("references", "scripts")
# The ending a repository reviewer's fixture holds a file under, which the run's copy drops.
HELD = ".fixture"
# The identity the throwaway checkout's one commit is made under, so no identity of the user's is needed or recorded,
# and signing is disabled.
COMMIT = ["-c", "user.name=skill-evals", "-c", "user.email=skill-evals@example.invalid", "-c", "commit.gpgsign=false"]


class ScenarioError(Exception):
    """A scenario that cannot be loaded."""


@dataclass(frozen=True)
class Expectation:
    label: str
    # The reason the record fails the expectation, or None when it passes.
    check: Callable[[Record], str | None]


@dataclass(frozen=True, kw_only=True)
class Scenario:
    skill: str
    name: str
    directory: Path
    mode: str
    prior: Path | None
    expectations: tuple[Expectation, ...]
    # A document scenario's document, and the version committed before it, if any; None for a fixture scenario.
    document: Path | None = None
    base: Path | None = None
    # A repository reviewer's scenario: the reviewer, the checkout its files come from, and those files, each by its
    # path in the checkout, which the run places at the same path in both trees of a throwaway copy of the fixture.
    reviewer: str | None = None
    checkout: Path | None = None
    placed: tuple[str, ...] = ()
    # The document stage a document scenario starts the skill at, or None for the skill's default.
    stage: str | None = None
    # A repository reviewer's fixture's held files, each by its path in the fixture, which the copy restores.
    held: tuple[str, ...] = ()
    # A trigger scenario's requests that should start its skill, and its near misses, which should not.
    requests: tuple[str, ...] = ()
    near_misses: tuple[str, ...] = ()


def quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _fields(spec: Mapping[str, object], required: set[str], optional: frozenset[str] = frozenset()) -> None:
    missing = sorted(required - set(spec))
    unknown = sorted(set(spec) - required - optional)
    if missing:
        raise ScenarioError(f"{spec.get('kind')} expectation lacks {', '.join(missing)}")
    if unknown:
        raise ScenarioError(f"{spec.get('kind')} expectation does not take {', '.join(unknown)}")


def _choice(value: object, choices: Sequence[str], what: str) -> str:
    if value not in choices:
        raise ScenarioError(f"{what} must be one of {', '.join(choices)}, not {quote(str(value))}")
    return str(value)


def _path(value: object) -> str:
    """A plain relative path. One either platform reads as absolute, a drive's included, is refused on both, since a
    scenario runs on each: on Windows, a path joined to a drive leaves the scenario's folder."""
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).anchor
        or ".." in PurePosixPath(value).parts
        or str(PurePosixPath(value)) != value
    ):
        raise ScenarioError(f"path must be a plain relative path, not {quote(str(value))}")
    return value


def _count(value: object, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ScenarioError(f"{what} must be a count, not {quote(str(value))}")
    return value


def _lines(value: object) -> tuple[int, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(line, int) or isinstance(line, bool) or line < 1 for line in value)
        or len(set(value)) != len(value)
    ):
        raise ScenarioError(f"lines must be distinct positive line numbers, not {quote(str(value))}")
    return tuple(sorted(value))


def _entry(value: object) -> tuple[int, str]:
    match = LEDGER_ID.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ScenarioError(f"entry must look like v1:F001, not {quote(str(value))}")
    return int(match[1]), match[2]


def _where(path: str, lines: tuple[int, ...]) -> str:
    return f"{path}:{','.join(str(line) for line in lines)}"


def _rank(severity: str) -> int:
    return SEVERITIES.index(severity)


def finding_expectation(spec: Mapping[str, object]) -> Expectation:
    _fields(spec, {"kind", "path", "lines", "severity"}, frozenset({"category"}))
    path, lines = _path(spec["path"]), _lines(spec["lines"])
    severity = _choice(spec["severity"], SEVERITIES, "severity")
    category: str | None = None
    if "category" in spec:
        if not isinstance(spec["category"], str) or not spec["category"].strip():
            raise ScenarioError("category must be a category name; leave it out for any category")
        category = spec["category"].strip()

    def matches(finding: Mapping[str, Any]) -> bool:
        return category is None or str(finding.get("category", "")).casefold() == category.casefold()

    def check(record: Record) -> str | None:
        there = [finding for finding in findings(record) if finding["path"] == path]
        if any(
            finding["line"] in lines and _rank(finding["severity"]) >= _rank(severity) and matches(finding)
            for finding in there
        ):
            return None
        seen = ", ".join(describe(finding) for finding in there) or "none"
        return f"no finding at least {wanted} on those lines; findings on {path}: {seen}"

    def describe(finding: Mapping[str, Any]) -> str:
        kind = f" {finding.get('category')}" if category is not None else ""
        return f"{finding['severity']}{kind} at line {finding['line']}"

    wanted = severity if category is None else f"{severity} {category}"
    return Expectation(f"finding {_where(path, lines)} {wanted}", check)


def no_finding_above_expectation(spec: Mapping[str, object]) -> Expectation:
    _fields(spec, {"kind", "severity"}, frozenset({"paths", "lines"}))
    severity = _choice(spec["severity"], SEVERITIES, "severity")
    if severity == SEVERITIES[-1]:
        raise ScenarioError(f"no_finding_above {severity} holds for every record: no severity ranks above it")
    paths: tuple[str, ...] | None = None
    if "paths" in spec:
        if not isinstance(spec["paths"], list) or not spec["paths"]:
            raise ScenarioError("paths must be a list of paths; leave it out for every path")
        paths = tuple(_path(path) for path in spec["paths"])
    lines: tuple[int, ...] | None = None
    if "lines" in spec:
        if paths is None or len(paths) != 1:
            raise ScenarioError("lines are lines of one path: give exactly one path in paths")
        lines = _lines(spec["lines"])

    def check(record: Record) -> str | None:
        above = [
            f"{finding['severity']} at {finding['path']}:{finding['line']}"
            for finding in findings(record)
            if _rank(finding["severity"]) > _rank(severity)
            and (paths is None or finding["path"] in paths)
            and (lines is None or finding["line"] in lines)
        ]
        return f"found {', '.join(above)}" if above else None

    where = _where(paths[0], lines) if paths and lines else ",".join(paths) if paths else "*"
    return Expectation(f"no_finding_above {severity} {where}", check)


def only_at_expectation(spec: Mapping[str, object]) -> Expectation:
    _fields(spec, {"kind", "path", "lines"})
    path, lines = _path(spec["path"]), _lines(spec["lines"])

    def check(record: Record) -> str | None:
        elsewhere = [
            f"{finding['severity']} at {finding['path']}:{finding['line']}"
            for finding in findings(record)
            if finding["path"] != path or finding["line"] not in lines
        ]
        return f"found {', '.join(elsewhere)}" if elsewhere else None

    return Expectation(f"only_at {_where(path, lines)}", check)


def verdict_expectation(spec: Mapping[str, object]) -> Expectation:
    _fields(spec, {"kind", "verdict"})
    verdict = _choice(spec["verdict"], VERDICTS, "verdict")

    def check(record: Record) -> str | None:
        actual = review(record)["verdict"]
        return None if actual == verdict else f"verdict is {actual}"

    return Expectation(f"verdict {verdict}", check)


def ledger_expectation(spec: Mapping[str, object]) -> Expectation:
    _fields(spec, {"kind", "addressed", "still_present"})
    expected = {name: _count(spec[name], name) for name in ("addressed", "still_present")}

    def check(record: Record) -> str | None:
        judged = judgments(record)
        actual = {name: sum(1 for disposition in judged.values() if disposition == name) for name in expected}
        return None if actual == expected else " ".join(f"{name}={count}" for name, count in actual.items())

    return Expectation(" ".join(["ledger", *(f"{name}={count}" for name, count in expected.items())]), check)


def disposition_expectation(spec: Mapping[str, object]) -> Expectation:
    _fields(spec, {"kind", "entry", "disposition"})
    entry = _entry(spec["entry"])
    disposition = _choice(spec["disposition"], DISPOSITIONS, "disposition")

    def check(record: Record) -> str | None:
        judged = judgments(record)
        if entry not in judged:
            return f"this review did not judge {spec['entry']}"
        return None if judged[entry] == disposition else f"{spec['entry']} is {judged[entry]}"

    return Expectation(f"disposition {spec['entry']} {disposition}", check)


def repeats_expectation(spec: Mapping[str, object]) -> Expectation:
    _fields(spec, {"kind", "path", "lines", "entry"})
    path, lines = _path(spec["path"]), _lines(spec["lines"])
    version, identifier = _entry(spec["entry"])

    def check(record: Record) -> str | None:
        # Only a finding at the entry's severity must restate it. A less severe one there is another defect, and the
        # record contract refuses a link from a more severe one, which a review must record as a new finding.
        severity = _rank(entry_severity(record, (version, identifier)))
        unlinked = [
            f"{finding.get('id', 'a finding')} at line {finding['line']}"
            for finding in findings(record)
            if finding["path"] == path
            and finding["line"] in lines
            and _rank(finding["severity"]) == severity
            and finding.get("repeats") != {"version": version, "id": identifier}
        ]
        return f"{', '.join(unlinked)} not linked to {spec['entry']}" if unlinked else None

    return Expectation(f"repeats {_where(path, lines)} {spec['entry']}", check)


KINDS: dict[str, Callable[[Mapping[str, Any]], Expectation]] = {
    "finding": finding_expectation,
    "no_finding_above": no_finding_above_expectation,
    "only_at": only_at_expectation,
    "verdict": verdict_expectation,
    "ledger": ledger_expectation,
    "disposition": disposition_expectation,
    "repeats": repeats_expectation,
}


RE_REVIEW_KINDS = frozenset({"ledger", "disposition", "repeats"})


def expectation(spec: object, mode: str) -> Expectation:
    if not isinstance(spec, dict):
        raise ScenarioError("each expectation must be an object")
    kind = spec.get("kind")
    if kind not in KINDS:
        raise ScenarioError(f"unknown expectation kind {quote(str(kind))}")
    if kind in RE_REVIEW_KINDS and mode != "re-review":
        raise ScenarioError(f"a {kind} expectation needs a re-review")
    return KINDS[kind](spec)


def _document(directory: Path, value: object, what: str) -> Path:
    """A file a document scenario names by its path from the scenario's folder. The path may lead to another
    scenario's file under the same scenarios root, so one document serves several skills' scenarios uncopied. A path
    either platform reads as absolute, a drive's included, is refused on both, since a scenario runs on each."""
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or PurePosixPath(value).anchor
        or PureWindowsPath(value).anchor
    ):
        raise ScenarioError(f"{what} must be a relative path, not {quote(str(value))}")
    root = directory.parents[1].resolve()
    path = (directory / value).resolve()
    if not path.is_relative_to(root):
        raise ScenarioError(f"{what} {value} is outside the scenarios under {root}")
    if not path.is_file():
        raise ScenarioError(f"{what} {value} is missing")
    return path


def _fixture(directory: Path) -> None:
    for tree in ("base", "head"):
        if not (directory / tree).is_dir():
            raise ScenarioError(f"has no {tree}/ tree")
    if not (directory / "pull.json").is_file():
        raise ScenarioError("has no pull.json")


def _fixture_in_place(directory: Path) -> None:
    """Check a fixture reviewed in place, which holds no held file, since only a reviewer's copy restores one."""
    _fixture(directory)
    for tree, relative in held_files(directory):
        raise ScenarioError(f"its {tree}/ tree holds {relative}, which only a repository reviewer's copy restores")


def _reviewer_folder(checkout: Path, folder: PurePosixPath) -> list[str]:
    """Every file a run places from the reviewer's folders, by its path in the checkout: all of references/, and all
    of scripts/ but its regression suites and Python's caches. The suites test the reviewer, which no review runs,
    and they name the scenarios' own files, which would show a condition script that looks for a change's tests one
    that is not there."""
    files = []
    for name in REVIEWER_FOLDERS:
        for path in sorted((checkout / folder / name).rglob("*")):
            relative = PurePosixPath(path.relative_to(checkout).as_posix())
            if path.is_file() and "__pycache__" not in relative.parts and not is_suite_name(relative.name):
                files.append(str(relative))
    return files


def _manifest_path(directory: Path) -> str | None:
    """The reviewer manifest a fixture's pull.json names, or None for the generic reviewer."""
    try:
        manifest_path = json.loads((directory / "pull.json").read_text(encoding="utf-8")).get("manifest_path")
    except (OSError, ValueError, AttributeError) as exc:
        raise ScenarioError(f"cannot read pull.json's manifest_path: {exc}") from exc
    return None if manifest_path is None else _path(manifest_path)


def _manifest(tree: Path, manifest_path: str, what: str) -> dict[str, Any]:
    try:
        return validate_adapter_manifest(json.loads((tree / manifest_path).read_text(encoding="utf-8")))
    except (OSError, ValueError, RuntimeContractError) as exc:
        raise ScenarioError(f"{what} {manifest_path} cannot be read: {exc}") from exc


def finding_categories(spec: Mapping[str, object], directory: Path, checkout: Path) -> list[str] | None:
    """The finding categories of the manifest the scenario's reviewers follow, or None when it lists none: a document
    scenario's is review-document's design manifest, a repository reviewer's the checkout's manifest, and any other
    fixture's the one its pull.json names, which prepare reads from the base tree."""
    if "document" in spec:
        return list(specialists_manifest(PurePosixPath(str(spec["document"])).name)["finding_categories"])
    manifest_path = _manifest_path(directory)
    if manifest_path is None:
        return None
    tree = checkout if "reviewer" in spec else directory / "base"
    categories = _manifest(tree, manifest_path, "the manifest").get("finding_categories")
    return None if categories is None else list(categories)


def _listed_categories(expectations: Sequence[Mapping[str, Any]], categories: Sequence[str] | None) -> None:
    """Refuse a finding expectation's category its manifest does not list, ignoring case, as the judge matches it."""
    if categories is None:
        return
    listed = {category.casefold() for category in categories}
    for item in expectations:
        if item["kind"] == "finding" and "category" in item and item["category"].strip().casefold() not in listed:
            raise ScenarioError(
                f"category {quote(item['category'])} is not one of the manifest's finding_categories: "
                f"{', '.join(categories)}"
            )


def reviewer_files(reviewer: str, directory: Path, checkout: Path) -> tuple[str, ...]:
    """The files a run of a repository reviewer's scenario places from the checkout: the reviewer's SKILL.md, its
    folders, its manifest, at the fixture's pull.json manifest_path, and every file that manifest declares (its
    resources, its specialists' profiles and resources, and its condition scripts). A condition's `reads` are not
    among them: they name what the script reads of the reviewed tree, the fixture's own. Raise ScenarioError when the
    reviewer or one of its files is missing from the checkout, or the fixture already holds a file the run places."""
    folder = REPOSITORY_SKILLS / reviewer
    if not (checkout / folder / "SKILL.md").is_file():
        raise ScenarioError(f"no repository skill {reviewer}: the checkout has no {folder}/SKILL.md")
    manifest_path = _manifest_path(directory)
    if manifest_path is None or not PurePosixPath(manifest_path).is_relative_to(folder):
        raise ScenarioError(f"pull.json's manifest_path must name {reviewer}'s manifest under {folder}/")
    manifest = _manifest(checkout, manifest_path, f"{reviewer}'s manifest")
    own = [str(folder / "SKILL.md"), *_reviewer_folder(checkout, folder), manifest_path]
    files = tuple(dict.fromkeys([*own, *declared_reviewer_files(manifest)]))
    for relative in files:
        if not (checkout / relative).is_file():
            raise ScenarioError(f"{reviewer} declares {relative}, which the checkout lacks")
        for tree in ("base", "head"):
            if (directory / tree / relative).exists():
                raise ScenarioError(f"its {tree}/ tree holds {relative}, which the run places from the checkout")
    return files


def held_files(directory: Path) -> list[tuple[str, str]]:
    """Each file of a fixture's trees held under its name with HELD appended, as its tree and its path there."""
    return [
        (tree, path.relative_to(directory / tree).as_posix())
        for tree in ("base", "head")
        for path in sorted((directory / tree).rglob(f"*{HELD}"))
        if path.is_file()
    ]


def _held(directory: Path, placed: Sequence[str]) -> tuple[str, ...]:
    """A repository reviewer's fixture's held files, each by its path in the fixture. Raise ScenarioError when one is
    named HELD alone, sits beside the file it is restored to, or is restored to a file the run places."""
    found = []
    for tree, relative in held_files(directory):
        restored = relative.removesuffix(HELD)
        if PurePosixPath(relative).name == HELD:
            raise ScenarioError(f"its {tree}/ tree holds {relative}, which names no file to restore")
        if (directory / tree / restored).exists():
            raise ScenarioError(f"its {tree}/ tree holds both {restored} and {relative}")
        if restored in placed:
            raise ScenarioError(f"its {tree}/ tree holds {relative}, which the run places from the checkout")
        found.append(f"{tree}/{relative}")
    return tuple(found)


def _reviewer(
    spec: Mapping[str, object], skill: str, directory: Path, checkout: Path
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """A repository reviewer's scenario's reviewer, the files a run places, and the fixture's held files: the scenario
    sits under the reviewer's name, and the skill it names is the one that runs the reviewer on a fixture."""
    if spec["reviewer"] != skill:
        raise ScenarioError(f"scenario.json names reviewer {quote(str(spec['reviewer']))}, not {skill}")
    if spec["skill"] != REVIEWER_SKILL:
        raise ScenarioError(f"a reviewer's scenario is started by {REVIEWER_SKILL}, not {quote(str(spec['skill']))}")
    if "document" in spec:
        raise ScenarioError("a reviewer's scenario is a fixture scenario, with no document")
    _fixture(directory)
    placed = reviewer_files(skill, directory, checkout)
    return skill, placed, _held(directory, placed)


def _requests(value: object, what: str) -> tuple[str, ...]:
    """A trigger scenario's list of requests, each one line of text a user might type, given once."""
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        raise ScenarioError(f"{what} must be a non-empty list of requests")
    for item in value:
        if not item.strip() or item != item.strip() or not item.isprintable():
            raise ScenarioError(f"each of {what} must be one line of text, not {quote(item)}")
        if item.startswith(("-", "/")):
            raise ScenarioError(
                f"{quote(item)} starts with {item[0]}, which Claude Code reads as an option or a command"
            )
    if len(set(value)) != len(value):
        raise ScenarioError(f"{what} name a request twice")
    return tuple(value)


def skills_started(record: Record, request: str) -> list[str]:
    """The skills a trigger record shows the session of this request starting."""
    entry = next((item for item in record["requests"] if item["request"] == request), None)
    if entry is None:
        raise RecordError(f"the record has no session of {quote(request)}")
    return list(entry["started"])


def trigger_expectation(skill: str, request: str, starts: bool) -> Expectation:
    """That the session given only this request starts the skill, or, for a near miss, does not."""

    def check(record: Record) -> str | None:
        found = skills_started(record, request)
        return None if (skill in found) == starts else f"the session started {', '.join(found) or 'no skill'}"

    return Expectation(f"{'starts' if starts else 'does_not_start'} {quote(request)}", check)


def load_trigger(skill: str, directory: Path, spec: Mapping[str, object], checkout: Path) -> Scenario:
    """A trigger scenario: requests that should start a shipped, model-invocable skill and near misses that should
    not, one expectation each."""
    if set(spec) != TRIGGER_FIELDS:
        raise ScenarioError(f"a trigger scenario.json must have exactly {', '.join(sorted(TRIGGER_FIELDS))}")
    if spec["skill"] != skill:
        raise ScenarioError(f"scenario.json names skill {quote(str(spec['skill']))}, not {skill}")
    skill_file = checkout / SHIPPED_SKILLS / skill / "SKILL.md"
    if not skill_file.is_file():
        raise ScenarioError(
            f"a trigger scenario's skill is a shipped one, and the checkout has no {SHIPPED_SKILLS}/{skill}"
        )
    if runtime_canary.starts_only_by_user(skill_file):
        raise ScenarioError(f"{skill} sets disable-model-invocation, so no request can start it")
    requests, near_misses = _requests(spec["requests"], "requests"), _requests(spec["near_misses"], "near_misses")
    both = [item for item in requests if item in near_misses]
    if both:
        raise ScenarioError(f"{quote(both[0])} is both a request and a near miss")
    expectations = [trigger_expectation(skill, item, True) for item in requests]
    expectations += [trigger_expectation(skill, item, False) for item in near_misses]
    return Scenario(
        skill=skill,
        name=directory.name,
        directory=directory,
        mode=TRIGGER,
        prior=None,
        expectations=tuple(expectations),
        requests=requests,
        near_misses=near_misses,
    )


def load_scenario(skill: str, directory: Path, checkout: Path = REPOSITORY_ROOT) -> Scenario:
    try:
        spec = json.loads((directory / "scenario.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScenarioError(f"{directory.name}: cannot read scenario.json: {exc}") from exc
    try:
        if not isinstance(spec, dict):
            raise ScenarioError("scenario.json must hold an object")
        mode = _choice(spec.get("mode"), MODES, "mode")
        if mode == TRIGGER:
            return load_trigger(skill, directory, spec, checkout)
        return review_scenario(skill, directory, spec, mode, checkout)
    except ScenarioError as exc:
        raise ScenarioError(f"{directory.name}: {exc}") from exc


def review_scenario(skill: str, directory: Path, spec: Mapping[str, object], mode: str, checkout: Path) -> Scenario:
    """A scenario of a review: a fixture, a document, or a repository reviewer's fixture, and its expectations."""
    allowed = SCENARIO_FIELDS | ({"prior"} if mode == "re-review" else set())
    if "document" in spec:
        allowed |= {"document"} | ({"base"} if "base" in spec else set()) | ({"stage"} if "stage" in spec else set())
    if "reviewer" in spec:
        allowed |= {"reviewer"}
    if set(spec) != allowed:
        raise ScenarioError(f"scenario.json must have exactly {', '.join(sorted(allowed))}")
    reviewer: str | None = None
    placed: tuple[str, ...] = ()
    held: tuple[str, ...] = ()
    if "reviewer" in spec:
        reviewer, placed, held = _reviewer(spec, skill, directory, checkout)
    elif spec["skill"] != skill:
        raise ScenarioError(f"scenario.json names skill {quote(str(spec['skill']))}, not {skill}")
    document: Path | None = None
    base: Path | None = None
    stage: str | None = None
    if "document" in spec:
        if mode != "initial":
            raise ScenarioError("a document scenario is an initial review: its mode is initial")
        document = _document(directory, spec["document"], "document")
        base = _document(directory, spec["base"], "base") if "base" in spec else None
        stage = _choice(spec["stage"], tuple(DOCUMENT_STAGES), "stage") if "stage" in spec else None
    elif reviewer is None:
        _fixture_in_place(directory)  # a reviewer's scenario had its fixture checked in _reviewer
    prior = None
    if mode == "re-review":
        prior = directory / _path(spec["prior"])
        if not prior.is_file():
            raise ScenarioError(f"prior record {spec['prior']} is missing")
    if not isinstance(spec["expectations"], list) or not spec["expectations"]:
        raise ScenarioError("expectations must be a non-empty list")
    expectations = tuple(expectation(item, mode) for item in spec["expectations"])
    _listed_categories(spec["expectations"], finding_categories(spec, directory, checkout))
    return Scenario(
        skill=str(spec["skill"]),
        name=directory.name,
        directory=directory,
        mode=mode,
        prior=prior,
        expectations=expectations,
        document=document,
        base=base,
        reviewer=reviewer,
        checkout=checkout if reviewer else None,
        placed=placed,
        stage=stage,
        held=held,
    )


def prepare_arguments(scenario: Scenario, fixture: Path | None = None) -> list[str]:
    """The arguments that prepare a run of a fixture scenario, which `review_pipeline.py prepare` and review-prs both
    take: a fixture canary of the scenario, or of the copy of it given, and for a re-review its prior record."""
    arguments = ["--canary", "--fixture", str(scenario.directory if fixture is None else fixture)]
    if scenario.prior is not None:
        arguments += ["--re-review", "--prior", str(scenario.prior)]
    return arguments


def stage_document(scenario: Scenario, folder: Path, git: GitClient) -> Path:
    """Write a document scenario's document into a new folder under its own file name, and return its path there.
    Without a base it stands alone, so the skill reviews it whole; with one, the folder is a new git checkout whose
    one commit holds the base under that name, and the document is written over it as an uncommitted edit."""
    if scenario.document is None:
        raise ScenarioError(f"{scenario.name} is a fixture scenario, with no document")
    folder.mkdir(parents=True)
    staged = folder / scenario.document.name
    if scenario.base is not None:
        with tempfile.TemporaryDirectory(prefix="skill-evals-hooks.") as hooks:
            isolated = ["-c", f"core.hooksPath={hooks}"]
            git.output([*isolated, "init", "--quiet"], directory=folder)
            staged.write_bytes(scenario.base.read_bytes())
            git.output([*isolated, "add", "--", staged.name], directory=folder)
            git.output(
                [*isolated, *COMMIT, "commit", "--quiet", "--no-verify", "--message", "The committed base"],
                directory=folder,
            )
    staged.write_bytes(scenario.document.read_bytes())
    return staged


@contextlib.contextmanager
def reviewed_fixture(scenario: Scenario) -> Iterator[Path]:
    """The fixture a run reviews. A repository reviewer's scenario's is a throwaway copy of it, outside the run's home
    so the session never finds the copied skill as one of its own, with each held file renamed without HELD and each
    placed file written from the checkout at its path in both trees; the copy is removed when the block ends, however
    it ends. Any other scenario's is its own folder, untouched."""
    if scenario.reviewer is None or scenario.checkout is None:
        yield scenario.directory
        return
    copy = Path(tempfile.mkdtemp(prefix="skill-evals-fixture-")).resolve()
    try:
        fixture = copy / scenario.name
        shutil.copytree(scenario.directory, fixture)
        for relative in scenario.held:
            held = fixture / relative
            held.rename(held.with_name(held.name.removesuffix(HELD)))
        for relative in scenario.placed:
            content = (scenario.checkout / relative).read_bytes()
            for tree in ("base", "head"):
                target = fixture / tree / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        yield fixture
    finally:
        fsops.remove(copy)


def skill_arguments(scenario: Scenario, run: Path, git: GitClient, fixture: Path | None = None) -> list[str]:
    """What the skill is started with: a fixture scenario's prepare arguments, on the fixture given when it is a
    copy, or the path of a document scenario's document, staged in the run's folder, and its stage, if it names one."""
    if scenario.document is None:
        return prepare_arguments(scenario, fixture)
    staged = str(stage_document(scenario, run / DOCUMENT_FOLDER, git))
    return [staged] if scenario.stage is None else [staged, "--stage", scenario.stage]


def load_scenarios(
    skill: str, names: Sequence[str] = (), root: Path = SCENARIO_ROOT, checkout: Path = REPOSITORY_ROOT
) -> list[Scenario]:
    """Each named scenario once in first-occurrence order, or every scenario in name order; a repository reviewer's
    files come from the checkout."""
    folder = root / skill
    found = sorted(path.parent.name for path in folder.glob("*/scenario.json")) if folder.is_dir() else []
    if not found:
        raise ScenarioError(f"no scenarios for {skill}: each is a <scenario>/scenario.json under {folder}")
    unknown = [name for name in names if name not in found]
    if unknown:
        raise ScenarioError(f"no scenario {', '.join(unknown)} for {skill}")
    return [load_scenario(skill, folder / name, checkout) for name in dict.fromkeys(names or found)]
