"""Deterministic update-pr-dashboard steps, so the orchestrating agent only asks the user about reviews.

    collect  read every open pull request and its reviewed head for the selected repositories into a tracker input file
             (by default in a new temporary directory), and, for a repository set, name each repository outside it in
             which the configured user authors, is asked to review, or has reviewed an open pull request
    update   render the owned dashboard section from that file, with every other argument taken from the configuration,
             name each review that went stale since the last update, and record the pull requests the tracker holds
             and their reviews beside the configuration
    override list, set, or clear the configuration's `dashboard.status_overrides`, validating the whole configuration
             before it is written; a set must name a pull request that record holds, and the listing marks each
             override it does not, but it reads nothing from GitHub, so a closed pull request's override clears like
             any other
    repository
             add a repository to the configuration and to a repository set, by default the one `collect` reads,
             validating the whole configuration before it is written; a repository with no entry yet gets the
             generic reviewer
    review-arguments
             print the `review-prs` arguments for the candidates the user confirmed, read from that record and the
             configuration's overrides, or name the confirmed stale ones when the re-review scope is still to be asked

Every command prints machine-readable lines and exits 0 on success. Expected failures, including a collection in
which any repository failed and an update in which any pull request's comparison failed, print `FAILED <reason>` as the
last line and exit 1; only a usage error exits 2.
"""

from __future__ import annotations

import argparse
import copy
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from console import run_main
from flat_text import flat_text
from github_client import GitHubError
from pr_change import FATAL_ERROR_KINDS, ChangeDetector, at_or_before
from review_archive import ArchiveError, pull_records
from review_config import (
    DASHBOARD_AI_REVIEW_MISSING,
    ConfigurationError,
    default_config_path,
    load_config,
    resolve_repositories,
    selected_repository_set,
    validate_config,
    validate_repository_identity,
)
from review_flags import FlagError, default_flags_path, load_store
from review_github import READ, GitHubClient
from review_io import PersistenceError, atomic_write_json, map_in_order, read_json, working_path
from review_operation import reviewed_head
from review_records import (
    RE_REVIEW_SCOPES,
    LedgerEntry,
    RecordError,
    ReviewRecord,
    flagged_entries,
    ledger_history,
    ledger_id,
)
from typed_fields import FieldError
from update_pr_dashboard import (
    CANDIDATE_STATES,
    ComparisonFailures,
    Row,
    TrackerError,
    normalize_overrides,
    normalize_pull_keys,
    replacement,
    review_candidates,
    stale_since,
    update_dashboard_rows,
    validate_items,
    validate_tracker_record,
)

# One query per page of 50 open pull requests. A pull request with more than 100 review requests or participants has
# that connection read again, whole, by `CONNECTION_QUERIES`, so no item is tracked from partial data.
PULLS_QUERY = """
query($owner: String!, $name: String!, $login: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 50, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number url title isDraft baseRefName headRefOid updatedAt reviewDecision
        author { login ... on User { name } }
        reviewRequests(first: 100) {
          pageInfo { hasNextPage }
          nodes { requestedReviewer { ... on User { login } ... on Bot { login } ... on Mannequin { login } } }
        }
        participants(first: 100) { pageInfo { hasNextPage } nodes { login } }
        reviews(last: 1, author: $login, states: [APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED]) {
          nodes { state commit { oid } }
        }
      }
    }
  }
}
""".strip()
# Every page of one pull request's review requests or participants, by connection name.
CONNECTION_QUERIES = {
    connection: f"""
query($owner: String!, $name: String!, $number: Int!, $after: String) {{
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{
      {connection}(first: 100, after: $after) {{ pageInfo {{ hasNextPage endCursor }} nodes {{ {fields} }} }}
    }}
  }}
}}
""".strip()
    for connection, fields in (
        (
            "reviewRequests",
            "requestedReviewer { ... on User { login } ... on Bot { login } ... on Mannequin { login } }",
        ),
        ("participants", "login"),
    )
}
# Every open pull request one search matches, each read only for its repository's name.
SEARCH_QUERY = """
query($q: String!, $after: String) {
  search(type: ISSUE, query: $q, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { repository { nameWithOwner } } }
  }
}
""".strip()
# The searches whose repositories a tracked set should hold: open pull requests the user authored, is asked to review,
# or has reviewed, outside archived repositories, which take no new pull requests.
UNTRACKED_SEARCHES = ("author", "review-requested", "reviewed-by")
# The entry a repository added without one gets: the generic reviewer, the one reviewer that needs no checkout or
# review skill of the repository's own, so it is valid for any repository.
GENERIC_REPOSITORY_ENTRY: dict[str, Any] = {
    "reviewer": {
        "id": "generic",
        "protocol_version": 1,
        "trusted_ref": None,
        "scope": "generic",
        "manifest_path": None,
    },
    "checkout_path": None,
}
GHOST_LOGIN = "ghost"
TRACKER_RECORD = "tracker.json"
# The file name review_config gives the default configuration, whose record keeps the plain name `TRACKER_RECORD`.
DEFAULT_CONFIG_NAME = "config.json"
# Whether a repository's first commit is its second commit or an ancestor of it; None when that cannot be told.
Ancestry = Callable[[str, str, str], "bool | None"]


class CollectionError(ValueError):
    pass


EXPECTED_ERRORS = (
    CollectionError,
    TrackerError,
    ConfigurationError,
    GitHubError,
    ArchiveError,
    PersistenceError,
    RecordError,
    FlagError,
    OSError,
)


@dataclass
class Services:
    """External effects, replaceable in tests."""

    github: GitHubClient = field(default_factory=GitHubClient)
    flags_path: Callable[[], Path] = default_flags_path


def configured_login(config: dict[str, Any], github: GitHubClient) -> str:
    """The configured `github_login`, else the account GitHub CLI is authenticated as."""
    return config["github_login"] or github.authenticated_login()


def complete_connections(github: GitHubClient, repository: str, node: Any) -> Any:
    """The pull-request node with each review-request or participant connection that has another page replaced by
    every node of it, read through `github`; a node of an unexpected shape is returned as it is, for `tracker_item`
    to refuse."""
    if not isinstance(node, dict) or not isinstance(node.get("number"), int):
        return node
    owner, name = repository.split("/", 1)
    completed = dict(node)
    for connection, query in CONNECTION_QUERIES.items():
        value = node.get(connection)
        if not (isinstance(value, dict) and isinstance(value.get("pageInfo"), dict)):
            continue
        if value["pageInfo"].get("hasNextPage") is True:
            nodes = github.graphql_nodes(
                query,
                {"owner": owner, "name": name, "number": node["number"]},
                ("repository", "pullRequest", connection),
            )
            completed[connection] = {"pageInfo": {"hasNextPage": False}, "nodes": nodes}
    return completed


def _complete_logins(connection: dict[str, Any], key: str | None, what: str) -> list[str]:
    if connection["pageInfo"]["hasNextPage"]:
        raise CollectionError(f"has more than 100 {what}")
    logins = []
    for node in connection["nodes"]:
        actor = node[key] if key else node
        # A team review request has no login and can never name the configured user.
        try:
            logins.append(READ.string(READ.json_object(actor, "actor").get("login"), "actor.login"))
        except FieldError:
            continue
    return logins


def _profile_name(value: Any) -> str | None:
    """An author's profile name, trimmed, or None when the author has none."""
    try:
        return READ.string(value, "author.name").strip()
    except FieldError:
        return None


def tracker_item(
    repository: str,
    node: dict[str, Any],
    archive_root: Path,
    *,
    ancestry: Ancestry = lambda repository, earlier, later: None,
    flags: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """One tracker input item from a GraphQL pull-request node and the archive's reviewed head. For a review record,
    it also counts the open findings `flags` name and what moved since the user's last review, read from the ledger
    the record stores or, for one written before ledgers, the ledger the earlier records compute, using `ancestry` to
    place that review's commit among the review versions' heads."""
    flags = list(flags)
    try:
        number = node["number"]
        author = node["author"] or {"login": GHOST_LOGIN}
        name = author.get("name")
        latest = node["reviews"]["nodes"][-1] if node["reviews"]["nodes"] else None
        item = {
            "repository": repository,
            "number": number,
            "url": node["url"],
            "title": node["title"],
            "author": author["login"],
            "author_name": _profile_name(name),
            "requested_reviewers": _complete_logins(node["reviewRequests"], "requestedReviewer", "review requests"),
            "participants": _complete_logins(node["participants"], None, "participants"),
            "draft": node["isDraft"],
            "base_ref": node["baseRefName"],
            "head_sha": node["headRefOid"],
            "updated_at": node["updatedAt"],
            "review_decision": node["reviewDecision"],
            "user_review_state": latest["state"] if latest else None,
            "user_review_sha": (latest["commit"] or {}).get("oid") if latest else None,
        }
    except CollectionError as exc:
        raise CollectionError(f"{repository}#{node.get('number')} {exc}") from exc
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        raise CollectionError(f"{repository} pull-request data has an unexpected shape") from exc
    # Each record is read and validated once, here; the reviewed head and the ledger both come from this list.
    records = pull_records(archive_root, repository, number)
    reviewed = reviewed_head(archive_root, repository, number, records=records)
    item["reviewed_head_sha"] = reviewed["head_sha"] if reviewed else None
    item["reviewed_incomplete"] = bool(reviewed and reviewed["incomplete"])
    item["ai_review"] = {key: reviewed[key] for key in ("verdict", "counts", "ledger", "report")} if reviewed else None
    # A legacy review has no records to read a ledger from, so its row shows only its counts.
    if reviewed and reviewed["source"] == "record":
        ledger = ledger_history(records)[reviewed["version"]]
        opened = {ledger_id(entry["version"], entry["id"]) for entry in ledger if entry["state"] == "open"}
        item["ai_review"]["flagged"] = len(opened & set(flagged_entries(ledger, flags, repository, number)))
        baseline = _baseline(records, item["user_review_sha"], repository, ancestry)
        item["ai_review"]["since_review"] = None if baseline is None else _since_review(ledger, baseline)
    return item


def _baseline(
    records: list[ReviewRecord], user_review_sha: str | None, repository: str, ancestry: Ancestry
) -> int | None:
    """The latest review version whose head is at or before the user's last review commit, 0 when every one is after
    it, or None when the user has not reviewed or GitHub cannot compare the commits. An exact head match needs no
    comparison."""
    if user_review_sha is None:
        return None
    heads = {record["review"]["version"]: record["pull_request"]["head_sha"] for record in records}
    exact = [version for version, head in heads.items() if head == user_review_sha]
    if exact:
        return max(exact)
    for version in sorted(heads, reverse=True):
        before = ancestry(repository, heads[version], user_review_sha)
        if before is None:
            return None
        if before:
            return version
    return 0


def _since_review(ledger: list[LedgerEntry], baseline: int) -> dict[str, int]:
    """What moved after the baseline version: entries raised since and still open, and entries raised by then that a
    later review addressed. The two never share an entry."""
    new = sum(entry["state"] == "open" and entry["version"] > baseline for entry in ledger)
    addressed = sum(
        entry["version"] <= baseline
        and entry["state"] == "closed"
        and entry["dispositions"][-1]["disposition"] == "addressed"
        and entry["dispositions"][-1]["version"] > baseline
        for entry in ledger
    )
    return {"version": baseline, "new": new, "addressed": addressed}


@dataclass
class Collection:
    """Each selected repository's open-pull count or its error, and, for a collection of a repository set, the
    repositories outside it in which the user has an open pull request to track, or why they could not be found.
    `untracked` is None for named repositories, which are not a set the user keeps."""

    results: dict[str, int | str]
    untracked: list[str] | None = None
    untracked_error: str | None = None


def untracked_repositories(github: GitHubClient, login: str, tracked: Iterable[str]) -> list[str]:
    """The repositories, lowercase and sorted, outside `tracked` that hold an open pull request `login` authored, is
    asked to review, or has reviewed."""
    found: set[str] = set()
    for qualifier in UNTRACKED_SEARCHES:
        query = f"is:pr is:open archived:false {qualifier}:{login}"
        for node in github.graphql_nodes(SEARCH_QUERY, {"q": query}, ("search",)):
            name = READ.json_object(node.get("repository", {}), "search node.repository").get("nameWithOwner")
            if name is not None:
                found.add(validate_repository_identity(name, "search node.repository.nameWithOwner"))
    return sorted(found - set(tracked))


def collect(
    output: Path,
    *,
    repositories: list[str] | None = None,
    repository_set: str | None = None,
    config_path: Path | None = None,
    services: Services | None = None,
) -> Collection:
    """Each selected repository's open-pull count, or its error, and, when no repositories are named, the
    repositories outside the selected set that `untracked_repositories` finds.

    The input file is written only when every repository succeeded. Rendering from a partial collection would
    silently drop the failed repositories' rows, so a failure leaves the dashboard to keep its previous rows.
    Missing GitHub CLI, authentication, and rate-limit failures stop the collection at once. A failed search for
    untracked repositories is reported rather than failing the collection, since it changes no row.
    """
    services = services or Services()
    config = load_config(config_path)
    selected = resolve_repositories(
        config, explicit=repositories, repository_set=repository_set, operation="update-pr-dashboard"
    )
    login = configured_login(config, services.github)
    archive_root = Path(config["archive_root"])
    flags = load_store(services.flags_path())["flags"]
    results: dict[str, int | str] = {}
    items: list[dict[str, Any]] = []

    def ancestry(repository: str, earlier: str, later: str) -> bool | None:
        return at_or_before(services.github, repository, earlier, later)

    def pulls(repository: str) -> list[dict[str, Any]]:
        owner, name = repository.split("/", 1)
        nodes = services.github.graphql_nodes(
            PULLS_QUERY, {"owner": owner, "name": name, "login": login}, ("repository", "pullRequests")
        )
        complete = [complete_connections(services.github, repository, node) for node in nodes]
        return validate_items(
            [tracker_item(repository, node, archive_root, ancestry=ancestry, flags=flags) for node in complete]
        )

    outcomes = map_in_order(
        pulls,
        selected,
        catch=(
            GitHubError,
            CollectionError,
            TrackerError,
            ConfigurationError,
            ArchiveError,
            PersistenceError,
            RecordError,
        ),
        fatal=lambda error: isinstance(error, GitHubError) and error.kind in FATAL_ERROR_KINDS,
    )
    for repository, (collected, error) in zip(selected, outcomes, strict=True):
        if error is not None or collected is None:  # pulls returns items whenever it raises nothing
            results[repository] = str(error)
            continue
        results[repository] = len(collected)
        items.extend(collected)
    if all(isinstance(value, int) for value in results.values()):
        atomic_write_json(output, validate_items(items))
    collection = Collection(results)
    if repositories is None:
        try:
            collection.untracked = untracked_repositories(services.github, login, selected)
        except (GitHubError, ConfigurationError) as exc:
            collection.untracked_error = str(exc)
    return collection


def update(
    input_path: Path,
    *,
    removals: list[str] | None = None,
    config_path: Path | None = None,
    services: Services | None = None,
) -> tuple[Path, list[Row], list[str]]:
    """Render the owned section with the configured dashboard, login, markers, overrides, author names, and
    home repositories, and return the dashboard, its rows, and the pull requests whose review went stale since the
    update that wrote the tracker record (see `stale_since`)."""
    services = services or Services()
    config_path = (config_path or default_config_path()).resolve()
    config = load_config(config_path)
    dashboard = Path(config["dashboard_file"])
    record = tracker_record_path(config_path)
    previous = read_tracker_record(record)
    rows = update_dashboard_rows(
        input_path,
        dashboard,
        configured_login(config, services.github),
        detector=ChangeDetector(services.github),
        start_marker=config["dashboard"]["start_marker"],
        end_marker=config["dashboard"]["end_marker"],
        overrides=config["dashboard"]["status_overrides"],
        author_names=config["dashboard"]["author_names"],
        removals=removals or [],
        home_repositories=config["repository_sets"][config["default_repository_set"]],
        config_path=str(config_path),
        record=record,
    )
    return dashboard, rows, stale_since(previous, rows)


def tracker_record_path(config_path: Path) -> Path:
    """Where `update` records the pull requests the tracker holds, the one derivation every command reads it by: beside
    the file the configuration path leads to, through any link, so a configuration reached through a link in another
    folder has the record `update` wrote. It is named after that file, `tracker.json` for the default name and
    `<name>.tracker.json` for any other, so two configurations in one folder never share one."""
    resolved = config_path.resolve()
    name = TRACKER_RECORD if resolved.name == DEFAULT_CONFIG_NAME else f"{resolved.stem}.{TRACKER_RECORD}"
    return resolved.parent / name


def read_tracker_record(record: Path) -> dict[str, Any] | None:
    """The validated tracker record at `record`, or None before any update has written it."""
    if not record.exists():
        return None
    return validate_tracker_record(read_json(record))


def held_pulls(config_path: Path) -> set[str] | None:
    """The pull requests the last `update` with this configuration held, or None before any has recorded them."""
    record = read_tracker_record(tracker_record_path(config_path))
    return None if record is None else set(record["pulls"])


def list_overrides(config_path: Path | None = None) -> list[tuple[str, str, bool]]:
    """Each configured status override, keyed and trimmed as the tracker matches it, in key order, with whether the
    tracker still holds its pull request."""
    path = config_path or default_config_path()
    overrides = normalize_overrides(load_config(path)["dashboard"]["status_overrides"])
    held = held_pulls(path) or set()
    return [(key, status, key in held) for key, status in sorted(overrides.items())]


def _pull_key(value: str) -> str:
    return next(iter(normalize_pull_keys([value], "override")))


def override_changes(sets: list[str], clears: list[str]) -> dict[str, str | None]:
    """Each named pull request's new status, or None to clear its override, keyed as the tracker matches it."""
    changes: dict[str, str | None] = {}
    for value in sets:
        key, separator, status = value.partition("=")
        if not separator:
            raise TrackerError(f"Status override {value!r} must be owner/repo#number=status")
        _add_change(changes, _pull_key(key), status.strip())
    for value in clears:
        _add_change(changes, _pull_key(value), None)
    return changes


def _add_change(changes: dict[str, str | None], key: str, status: str | None) -> None:
    if key in changes:
        raise TrackerError(f"Status override {key} is named more than once")
    changes[key] = status


def rewrite_config(edit: Callable[[dict[str, Any], dict[str, Any]], None], config_path: Path | None = None) -> Path:
    """Change the configuration file in one validated write, through `edit`, which changes the value as the user
    wrote it in place, given beside it in the normalized form, or raises to write nothing.

    Every other value is kept as the user wrote it rather than in `write_config`'s normalized form, which would fill
    in every default, and it is validated exactly as `review_config.py write` validates it, before `edit` and, before
    and after it is serialized, after it. The layout is normalized, keys sorted and indented two spaces as that command
    writes them, so a file it wrote changes only in what `edit` changed; keeping an arbitrary hand layout would need an
    indentation guess in the shared writer, and JSON holds no comments to lose. A configuration `replacement` refuses
    is not written either. The write replaces the file the configuration path leads to, through any symbolic link, and
    keeps that file's mode."""
    path = config_path or default_config_path()
    target, mode = replacement(path)
    raw = read_json(path)
    edit(raw, validate_config(raw))
    atomic_write_json(target, raw, validator=validate_config, mode=mode)
    return path


def change_overrides(
    sets: list[str], clears: list[str], *, config_path: Path | None = None
) -> tuple[Path, dict[str, str | None]]:
    """Set and clear status overrides in one `rewrite_config` write, changing no other value in the configuration
    file. A change that fails, such as a computed state, a malformed key, clearing an override that is not set, or
    setting one for a pull request the tracker does not hold, writes nothing."""
    path = config_path or default_config_path()
    changes = override_changes(sets, clears)

    def edit(raw: dict[str, Any], _: dict[str, Any]) -> None:
        dashboard = raw.setdefault("dashboard", {})
        current = dashboard.get("status_overrides", {})
        present = {_pull_key(key) for key in current}
        missing = [key for key, status in changes.items() if status is None and key not in present]
        if missing:
            raise TrackerError(f"No status override for {missing[0]}")
        kept = {key: status for key, status in current.items() if _pull_key(key) not in changes}
        dashboard["status_overrides"] = {
            **kept,
            **{key: status for key, status in changes.items() if status is not None},
        }
        validate_config(raw)
        _refuse_untracked([key for key, status in changes.items() if status is not None], path)

    return rewrite_config(edit, path), changes


def add_repositories(
    repositories: list[str], *, repository_set: str | None = None, config_path: Path | None = None
) -> tuple[Path, str, list[tuple[str, bool]]]:
    """Add each repository to a repository set in one `rewrite_config` write, giving one with no entry under
    `repositories` the generic reviewer's entry and keeping the entry of one that has its own. The set is the named
    one, else the one `collect` reads without a scope. Return the configuration, the set, and each repository,
    lowercase as the configuration matches it, with whether its entry was added. A malformed identity, a repository
    named twice or already in the set, and a set the configuration does not name are refused, writing nothing."""
    identities = [validate_repository_identity(repository) for repository in repositories]
    for index, identity in enumerate(identities):
        if identity in identities[:index]:
            raise ConfigurationError(f"Repository {identity} is named more than once")
    added: list[tuple[str, bool]] = []
    chosen = ""

    def edit(raw: dict[str, Any], config: dict[str, Any]) -> None:
        nonlocal chosen
        chosen = selected_repository_set(config, repository_set=repository_set, operation="update-pr-dashboard")
        if chosen not in config["repository_sets"]:
            raise ConfigurationError(f"Unknown repository set: {chosen}")
        for identity in identities:
            if identity in config["repository_sets"][chosen]:
                raise ConfigurationError(f"Repository {identity} is already in repository set {chosen}")
            if identity not in config["repositories"]:
                raw.setdefault("repositories", {})[identity] = copy.deepcopy(GENERIC_REPOSITORY_ENTRY)
            raw["repository_sets"][chosen].append(identity)
            added.append((identity, identity not in config["repositories"]))

    return rewrite_config(edit, config_path), chosen, added


def _refuse_untracked(keys: list[str], config_path: Path) -> None:
    """Refuse the first key naming no pull request the tracker holds, so a mistyped one never sits unmatched."""
    if not keys:
        return
    held = held_pulls(config_path)
    record = tracker_record_path(config_path)
    for key in keys:
        if held is None:
            raise TrackerError(f"Status override {key} names no tracked pull request: no update has written {record}")
        if key not in held:
            raise TrackerError(f"Status override {key} names no pull request the tracker holds in {record}")


def recorded_candidates(config_path: Path) -> dict[str, str] | None:
    """Each pull request the last update offered for review, with its AI review state: a row it showed whose review is
    missing or stale and that no status override in the configuration pins. None before any update has recorded its
    rows."""
    record = read_tracker_record(tracker_record_path(config_path))
    if record is None:
        return None
    pinned = normalize_overrides(load_config(config_path)["dashboard"]["status_overrides"])
    return {
        key: review["state"]
        for key, review in record["reviews"].items()
        if review["state"] in CANDIDATE_STATES and key not in pinned
    }


def review_arguments(
    confirmed: list[str], scope: str | None, *, config_path: Path | None = None
) -> tuple[list[str], list[str]]:
    """The `review-prs` arguments for the confirmed candidates, in key order: `--pull` for each missing review,
    `--re-review` for each stale one, and `--scope` once when any is stale. Without a scope, a confirmed stale one
    needs the user's choice first, so the arguments are empty and the stale ones are returned instead. A key the last
    update did not offer for review, and a scope no confirmed pull request needs, are refused."""
    path = config_path or default_config_path()
    keys = sorted(normalize_pull_keys(confirmed, "confirmed"))
    if not keys:
        raise TrackerError("Name at least one confirmed pull request")
    candidates = recorded_candidates(path)
    record = tracker_record_path(path)
    if candidates is None:
        raise TrackerError(f"Confirmed pull request {keys[0]} names no candidate: no update has written {record}")
    for key in keys:
        if key not in candidates:
            raise TrackerError(
                f"Confirmed pull request {key} names no pull request the last update offered for review, one whose AI "
                f"review is missing or stale and that no status override pins, in {record}"
            )
    missing = [key for key in keys if candidates[key] == DASHBOARD_AI_REVIEW_MISSING]
    stale = [key for key in keys if candidates[key] != DASHBOARD_AI_REVIEW_MISSING]
    if stale and scope is None:
        return [], stale
    if scope is not None and not stale:
        raise TrackerError("--scope is taken only when a confirmed pull request is stale; leave it out")
    arguments = [argument for key in missing for argument in ("--pull", key)]
    arguments += [argument for key in stale for argument in ("--re-review", key)]
    return arguments + (["--scope", scope] if scope is not None else []), []


def main(arguments: list[str] | None = None, services: Services | None = None) -> int:
    services = services or Services()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, help="defaults to CODE_REVIEW_CONFIG or the standard config path")
    commands = parser.add_subparsers(dest="command", required=True)
    collect_parser = commands.add_parser("collect")
    scope = collect_parser.add_mutually_exclusive_group()
    scope.add_argument("--repository", action="append", dest="repositories")
    scope.add_argument("--repository-set")
    collect_parser.add_argument("--output", type=Path, help="input file; defaults to a new temporary directory")
    update_parser = commands.add_parser("update")
    update_parser.add_argument("--input", required=True, type=Path)
    update_parser.add_argument("--remove", action="append", default=[], help="owner/repo#number; repeatable")
    update_parser.add_argument("--candidates", action="store_true", help="list missing or stale AI reviews")
    override_parser = commands.add_parser("override")
    override_parser.add_argument(
        "--set", action="append", default=[], dest="sets", metavar="OWNER/REPO#N=STATUS", help="repeatable"
    )
    override_parser.add_argument(
        "--clear", action="append", default=[], dest="clears", metavar="OWNER/REPO#N", help="repeatable"
    )
    repository_parser = commands.add_parser("repository")
    repository_parser.add_argument(
        "--add", action="append", required=True, dest="additions", metavar="OWNER/REPO", help="repeatable"
    )
    repository_parser.add_argument("--repository-set", help="defaults to the set collect reads without a scope")
    arguments_parser = commands.add_parser("review-arguments")
    arguments_parser.add_argument(
        "--confirm",
        action="append",
        required=True,
        dest="confirmed",
        metavar="OWNER/REPO#N",
        help="a candidate the user confirmed; repeatable",
    )
    arguments_parser.add_argument(
        "--scope", choices=RE_REVIEW_SCOPES, help="how review-prs re-reviews the stale ones; taken only when one is"
    )
    args = parser.parse_args(arguments)
    try:
        if args.command == "override":
            return _override(args.sets, args.clears, args.config)
        if args.command == "review-arguments":
            return _review_arguments(args.confirmed, args.scope, args.config)
        if args.command == "repository":
            return _repository(args.additions, args.repository_set, args.config)
        if args.command == "collect":
            return _collect(args, services)
        calls = services.github.call_count
        dashboard, rows, stale = update(args.input, removals=args.remove, config_path=args.config, services=services)
        print(f"UPDATED {dashboard} rows={len(rows)}")
        print(f"GITHUB_CALLS {services.github.call_count - calls}")
        for key in stale:
            print(f"STALE_SINCE_LAST_UPDATE {key}")
        if args.candidates:
            for candidate in review_candidates(rows):
                print(f"CANDIDATE {candidate['status']} {candidate['repository']}#{candidate['number']}")
        return 0
    except ComparisonFailures as exc:
        for failure in exc.failures:
            print(f"PULL_FAILED {failure.pull} {flat_text(failure.reason)}")
        print(f"FAILED {flat_text(str(exc))}")
        return 1
    except EXPECTED_ERRORS as exc:
        print(f"FAILED {flat_text(str(exc))}")
        return 1


def _collect(args: argparse.Namespace, services: Services) -> int:
    output = working_path(args.output, "update-pr-dashboard-input-", "input.json")
    collection = collect(
        output,
        repositories=args.repositories,
        repository_set=args.repository_set,
        config_path=args.config,
        services=services,
    )
    results = collection.results
    for repository, result in results.items():
        print(
            f"REPOSITORY {repository} pulls={result}"
            if isinstance(result, int)
            else f"REPOSITORY_FAILED {repository} {flat_text(result)}"
        )
    for repository in collection.untracked or []:
        print(f"UNTRACKED_REPOSITORY {repository}")
    if collection.untracked_error is not None:
        print(f"UNTRACKED_CHECK_FAILED {flat_text(collection.untracked_error)}")
    failed = sum(1 for result in results.values() if not isinstance(result, int))
    if failed:
        print(
            f"FAILED {failed} of {len(results)} repositories could not be collected; no input was written "
            "and the dashboard keeps its previous rows"
        )
        return 1
    print(f"INPUT {output}")
    return 0


def _review_arguments(confirmed: list[str], scope: str | None, config_path: Path | None) -> int:
    review_prs, needs_scope = review_arguments(confirmed, scope, config_path=config_path)
    for key in needs_scope:
        print(f"SCOPE_NEEDED {key}")
    if review_prs:
        print(f"REVIEW_ARGUMENTS {' '.join(review_prs)}")
    return 0


def _override(sets: list[str], clears: list[str], config_path: Path | None) -> int:
    if not sets and not clears:
        overrides = list_overrides(config_path)
        for key, status, held in overrides:
            print(f"{'OVERRIDE' if held else 'OVERRIDE_UNTRACKED'} {key} {status}")
        print(f"OVERRIDES {len(overrides)}")
        return 0
    path, changes = change_overrides(sets, clears, config_path=config_path)
    for key, change in changes.items():
        print(f"CLEARED {key}" if change is None else f"SET {key} {change}")
    print(f"WROTE {path}")
    return 0


def _repository(additions: list[str], repository_set: str | None, config_path: Path | None) -> int:
    path, chosen, added = add_repositories(additions, repository_set=repository_set, config_path=config_path)
    for repository, entry in added:
        if entry:
            print(f"ADDED_REPOSITORY {repository} reviewer=generic")
        print(f"ADDED_TO_SET {chosen} {repository}")
    print(f"WROTE {path}")
    return 0


if __name__ == "__main__":
    run_main(main)
