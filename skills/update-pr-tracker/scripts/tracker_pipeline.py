"""Deterministic update-pr-tracker steps, so the orchestrating agent only asks the user about reviews.

    collect  read every open pull request and its reviewed head for the selected repositories into a tracker input file
             (by default in a new temporary directory)
    update   render the owned dashboard section from that file, with every other argument taken from the configuration

Every command prints machine-readable lines and exits 0 on success. Expected failures, including a collection in
which any repository failed, print `FAILED <reason>` as the last line and exit 1; only a usage error exits 2.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "code-review-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pr_change import FATAL_ERROR_KINDS, ChangeDetector, at_or_before
from review_archive import ArchiveError, pull_records
from review_config import ConfigurationError, default_config_path, load_config, resolve_repositories
from review_flags import FlagError, default_flags_path, load_store
from review_github import GitHubClient, GitHubError
from review_io import PersistenceError, atomic_write_json, map_in_order, working_path
from review_operation import reviewed_head
from review_records import RecordError, flagged_entries, ledger_id
from update_pr_tracker import Row, TrackerError, review_candidates, update_dashboard_rows, validate_items

# One query per page of 50 open pull requests. Nested connections are not paginated: a pull request with more
# than 100 review requests or participants fails its repository rather than being tracked from partial data.
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
GHOST_LOGIN = "ghost"
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


def _complete_logins(connection: dict[str, Any], key: str | None, what: str) -> list[str]:
    if connection["pageInfo"]["hasNextPage"]:
        raise CollectionError(f"has more than 100 {what}")
    logins = []
    for node in connection["nodes"]:
        actor = node[key] if key else node
        # A team review request has no login and can never name the configured user.
        if isinstance(actor, dict) and isinstance(actor.get("login"), str) and actor["login"]:
            logins.append(actor["login"])
    return logins


def tracker_item(
    repository: str,
    node: dict[str, Any],
    archive_root: Path,
    *,
    ancestry: Ancestry = lambda repository, earlier, later: None,
    flags: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """One tracker input item from a GraphQL pull-request node and the archive's reviewed head. For a review with a
    finding ledger, it also counts the open findings `flags` name and what moved since the user's last review, using
    `ancestry` to place that review's commit among the review versions' heads."""
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
            "author_name": name.strip() if isinstance(name, str) and name.strip() else None,
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
    reviewed = reviewed_head(archive_root, repository, number)
    item["reviewed_head_sha"] = reviewed["head_sha"] if reviewed else None
    item["reviewed_incomplete"] = bool(reviewed and reviewed["incomplete"])
    item["ai_review"] = {key: reviewed[key] for key in ("verdict", "counts", "ledger", "report")} if reviewed else None
    if reviewed and reviewed["ledger"]:
        records = [
            record
            for record in pull_records(archive_root, repository, number)
            if record["review"]["version"] <= reviewed["version"]
        ]
        ledger = records[-1]["ledger"]
        opened = {ledger_id(entry["version"], entry["id"]) for entry in ledger if entry["state"] == "open"}
        item["ai_review"]["flagged"] = len(opened & set(flagged_entries(ledger, flags, repository, number)))
        baseline = _baseline(records, item["user_review_sha"], repository, ancestry)
        item["ai_review"]["since_review"] = None if baseline is None else _since_review(ledger, baseline)
    return item


def _baseline(
    records: list[dict[str, Any]], user_review_sha: str | None, repository: str, ancestry: Ancestry
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


def _since_review(ledger: list[dict[str, Any]], baseline: int) -> dict[str, int]:
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


def collect(
    output: Path,
    *,
    repositories: list[str] | None = None,
    repository_set: str | None = None,
    config_path: Path | None = None,
    services: Services | None = None,
) -> dict[str, int | str]:
    """Each selected repository's open-pull count, or its error.

    The input file is written only when every repository succeeded. Rendering from a partial collection would
    silently drop the failed repositories' rows, so a failure leaves the dashboard to keep its previous rows.
    Missing GitHub CLI, authentication, and rate-limit failures stop the collection at once.
    """
    services = services or Services()
    config = load_config(config_path)
    selected = resolve_repositories(
        config, explicit=repositories, repository_set=repository_set, operation="update-pr-tracker"
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
        return validate_items(
            [tracker_item(repository, node, archive_root, ancestry=ancestry, flags=flags) for node in nodes]
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
        if error is not None:
            results[repository] = str(error)
            continue
        results[repository] = len(collected)
        items.extend(collected)
    if all(isinstance(value, int) for value in results.values()):
        atomic_write_json(output, validate_items(items))
    return results


def update(
    input_path: Path,
    *,
    removals: list[str] | None = None,
    config_path: Path | None = None,
    services: Services | None = None,
) -> tuple[Path, list[Row]]:
    """Render the owned section with the configured dashboard, login, markers, overrides, author names, and home repositories."""
    services = services or Services()
    config_path = (config_path or default_config_path()).resolve()
    config = load_config(config_path)
    dashboard = Path(config["dashboard_file"])
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
    )
    return dashboard, rows


def main(arguments: list[str] | None = None, services: Services | None = None) -> int:
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
    args = parser.parse_args(arguments)
    try:
        if args.command == "collect":
            output = working_path(args.output, "update-pr-tracker-input-", "input.json")
            results = collect(
                output,
                repositories=args.repositories,
                repository_set=args.repository_set,
                config_path=args.config,
                services=services,
            )
            for repository, result in results.items():
                print(
                    f"REPOSITORY {repository} pulls={result}"
                    if isinstance(result, int)
                    else f"REPOSITORY_FAILED {repository} {result}"
                )
            failed = sum(1 for result in results.values() if not isinstance(result, int))
            if failed:
                print(
                    f"FAILED {failed} of {len(results)} repositories could not be collected; no input was written "
                    "and the dashboard keeps its previous rows"
                )
                return 1
            print(f"INPUT {output}")
            return 0
        dashboard, rows = update(args.input, removals=args.remove, config_path=args.config, services=services)
        print(f"UPDATED {dashboard} rows={len(rows)}")
        if args.candidates:
            for candidate in review_candidates(rows):
                print(f"CANDIDATE {candidate['status']} {candidate['repository']}#{candidate['number']}")
        return 0
    except EXPECTED_ERRORS as exc:
        print(f"FAILED {exc}")
        return 1


if __name__ == "__main__":
    # Output names configured paths; a Windows pipe's legacy code page cannot encode every character they hold.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
