"""Set the project board's Release field from each item's milestone, so the board groups by release in version order.

Usage:
  python tools/project_release_sync.py [--check]

The board groups its Releases view by a single-select `Release` field rather than by milestone, because GitHub sorts
milestones by title, which puts v0.10.0 beside v0.1.0. Milestones stay the record of scope, and this tool copies each
item's milestone into the field: it sets the option named like the milestone, and clears the field on an item with no
milestone. It reads the board the current checkout's repository owner holds as project 1, through `gh api graphql`,
which needs the `project` scope (`gh auth refresh -s project`).

It prints SYNCED (exit 0) once every item's Release matches its milestone, after one line saying how many items it
updated and cleared. It prints DRIFTED (exit 1), then one line per problem left, when a milestone has no option of its
name, which is added in the field's settings, or when the options that name versions are not in version order. With
`--check` it changes nothing and prints IN_SYNC (exit 0), or DRIFTED (exit 1) with one line per item whose Release
does not match its milestone as well. It exits 2 when `gh` cannot read or change the board, or the board has no
Release field; setting a value twice changes nothing, so a run cut short is rerun. CONTRIBUTING.md runs it after an
issue is filed or moved between milestones, and the release procedure in docs/releasing.md runs it with `--check`.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

from console import run_main
from github_client import GitHubClient, GitHubError

PROJECT_NUMBER = 1
FIELD_NAME = "Release"
PAGE_SIZE = 50
# Mutations sent in one request; GitHub answers a large batch slowly enough to fail it at the gateway.
BATCH_SIZE = 20
# A batch whose answer was lost is sent again, since each mutation sets a value and setting it twice changes nothing.
BATCH_ATTEMPTS = 3
VERSION = re.compile(r"v(\d+)\.(\d+)\.(\d+)")

ITEMS_QUERY = """
query($owner: String!, $number: Int!, $field: String!, $first: Int!, $after: String) {
  repositoryOwner(login: $owner) {
    ... on ProjectV2Owner {
      projectV2(number: $number) {
        id
        field(name: $field) { ... on ProjectV2SingleSelectField { id options { id name } } }
        items(first: $first, after: $after) {
          pageInfo { hasNextPage endCursor }
          nodes {
            id
            content {
              ... on Issue { number milestone { title } }
              ... on PullRequest { number milestone { title } }
              ... on DraftIssue { title }
            }
            release: fieldValueByName(name: $field) { ... on ProjectV2ItemFieldSingleSelectValue { name } }
          }
        }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class Item:
    """A board item: its node id, a label for the reader, its milestone, and its Release, each None when unset."""

    id: str
    label: str
    milestone: str | None
    release: str | None


@dataclass(frozen=True)
class Board:
    project: str
    field: str
    options: dict[str, str]  # option name to option id, in the field's order
    items: list[Item]


@dataclass(frozen=True)
class Plan:
    updates: list[tuple[Item, str]]  # each item and the option id it is set to
    clears: list[Item]
    problems: list[str]


def _get(document: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(document, Mapping):
            return None
        document = document.get(key)
    return document


def _item(node: Mapping[str, Any]) -> Item | None:
    """The item a node describes, or None for one whose content this reader cannot see."""
    content = node.get("content")
    if not isinstance(content, Mapping):
        return None
    number = content.get("number")
    label = f"#{number}" if isinstance(number, int) else f"draft {content.get('title')!r}"
    milestone = _get(content, "milestone", "title")
    release = _get(node, "release", "name")
    return Item(
        str(node["id"]),
        label,
        milestone if isinstance(milestone, str) else None,
        release if isinstance(release, str) else None,
    )


def read_board(client: GitHubClient) -> Board:
    """Every item of the board with its milestone and Release, and the field's options, through every page."""
    project = field = None
    options: dict[str, str] = {}
    items: list[Item] = []
    after: str | None = None
    while True:
        arguments = ["api", "graphql", "-f", f"query={ITEMS_QUERY}", "-F", "owner={owner}"]
        arguments += ["-F", f"number={PROJECT_NUMBER}", "-f", f"field={FIELD_NAME}", "-F", f"first={PAGE_SIZE}"]
        arguments += ["-f", f"after={after}"] if after else []
        board = _get(client.json(arguments, graphql=True), "data", "repositoryOwner", "projectV2")
        if not isinstance(board, Mapping):
            raise ValueError(f"the repository owner has no project {PROJECT_NUMBER} this token can read")
        project = str(board["id"])
        if not isinstance(_get(board, "field", "id"), str):
            raise ValueError(f"project {PROJECT_NUMBER} has no single-select field named {FIELD_NAME!r}")
        field = board["field"]["id"]
        options = {option["name"]: option["id"] for option in board["field"]["options"]}
        page = board["items"]
        items += [item for node in page["nodes"] if (item := _item(node)) is not None]
        if not page["pageInfo"]["hasNextPage"]:
            return Board(project, field, options, items)
        after = page["pageInfo"]["endCursor"]


def order_problem(options: Sequence[str]) -> str | None:
    """A problem line when the options named like a version are not in version order, else None."""
    keys = {name: tuple(int(part) for part in match.groups()) for name in options if (match := VERSION.fullmatch(name))}
    versions = list(keys)
    ordered = sorted(versions, key=keys.__getitem__)
    if versions == ordered:
        return None
    return f"the {FIELD_NAME} options are not in version order: {', '.join(versions)} (expected {', '.join(ordered)})"


def plan(board: Board) -> Plan:
    """What makes every item's Release match its milestone, and the problems no change here can fix."""
    updates: list[tuple[Item, str]] = []
    clears: list[Item] = []
    missing: dict[str, int] = {}
    for item in board.items:
        if item.milestone == item.release:
            continue
        if item.milestone is None:
            clears.append(item)
        elif item.milestone in board.options:
            updates.append((item, board.options[item.milestone]))
        else:
            missing[item.milestone] = missing.get(item.milestone, 0) + 1
    problems = [
        f"milestone {milestone} has no {FIELD_NAME} option ({count} item{'s' if count != 1 else ''})"
        for milestone, count in sorted(missing.items())
    ]
    order = order_problem(list(board.options))
    return Plan(updates, clears, problems + ([order] if order else []))


def _mutation(index: int, item: Item, option: str | None) -> tuple[str, str, list[str]]:
    """One aliased mutation: its variable declarations, its text, and the gh arguments that carry its values."""
    target = f"projectId: $project, itemId: $item{index}, fieldId: $field"
    if option is None:
        return (
            f"$item{index}: ID!",
            f"m{index}: clearProjectV2ItemFieldValue(input: {{{target}}}) {{ clientMutationId }}",
            ["-f", f"item{index}={item.id}"],
        )
    return (
        f"$item{index}: ID!, $option{index}: String!",
        f"m{index}: updateProjectV2ItemFieldValue(input: {{{target}, value: {{singleSelectOptionId: $option{index}}}}})"
        " { clientMutationId }",
        ["-f", f"item{index}={item.id}", "-f", f"option{index}={option}"],
    )


def apply(client: GitHubClient, board: Board, changes: Sequence[tuple[Item, str | None]]) -> None:
    """Send the changes in batches, each sent again when GitHub's answer is lost, up to BATCH_ATTEMPTS times."""
    for start in range(0, len(changes), BATCH_SIZE):
        parts = [
            _mutation(index, item, option) for index, (item, option) in enumerate(changes[start : start + BATCH_SIZE])
        ]
        declarations = ", ".join(["$project: ID!", "$field: ID!", *(part[0] for part in parts)])
        body = "\n".join(part[1] for part in parts)
        arguments = ["api", "graphql", "-f", f"query=mutation({declarations}) {{\n{body}\n}}"]
        arguments += ["-f", f"project={board.project}", "-f", f"field={board.field}"]
        arguments += [value for part in parts for value in part[2]]
        for attempt in range(BATCH_ATTEMPTS):
            try:
                client.json(arguments, retry=False, graphql=True)
                break
            except GitHubError as error:
                if error.kind != "unknown_outcome" or attempt == BATCH_ATTEMPTS - 1:
                    raise
                client.sleeper(client.backoff.transient_wait(attempt))


def main(arguments: list[str] | None = None, client: GitHubClient | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="change nothing; report what does not match")
    options = parser.parse_args(arguments)
    client = client or GitHubClient()
    try:
        board = read_board(client)
        changes = plan(board)
        if not options.check:
            apply(client, board, [*changes.updates, *((item, None) for item in changes.clears)])
    except (GitHubError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if options.check:
        drift = [
            f"{item.label}: {FIELD_NAME} is {item.release or 'unset'}, milestone is {item.milestone or 'none'}"
            for item in [*(item for item, _ in changes.updates), *changes.clears]
        ]
        problems = drift + changes.problems
        print("DRIFTED" if problems else "IN_SYNC")
    else:
        problems = changes.problems
        print("DRIFTED" if problems else "SYNCED")
        print(f"updated {len(changes.updates)}, cleared {len(changes.clears)}")
    for problem in problems:
        print(f"- {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    run_main(lambda: main(sys.argv[1:]), errors="backslashreplace")
