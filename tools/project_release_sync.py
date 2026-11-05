"""Set the project board's Release field from each item's milestone, so the board groups by release in version order.

Usage:
  python tools/project_release_sync.py [--check]

The board groups its Releases view by a single-select `Release` field rather than by milestone, because GitHub sorts
milestones by title, which puts v0.10.0 beside v0.1.0. Milestones stay the record of scope, and this tool copies each
item's milestone into the field: it sets the option named like the milestone, and clears the field on an item with no
milestone. It reads the board the current checkout's repository owner holds as project 1, through `gh api graphql`,
which needs the `project` scope (`gh auth refresh -s project`).

It prints SYNCED (exit 0) once every item's Release matches its milestone, then one line saying how many items it
updated and cleared. It prints DRIFTED (exit 1), then that line, then one line per problem left, when a milestone has
no option of its name, which is added in the field's settings, when the options that name versions are not in version
order, or when the token in use cannot read an item, as one in a repository it has no access to: that line names each
such item by its node id, since nothing here can say whether its Release matches. With `--check` it changes nothing
and prints IN_SYNC (exit 0), or DRIFTED (exit 1) with one line per item whose Release does not match its milestone as
well. It exits 2 when `gh` cannot read or change the board, the board has no Release field, or its answer lacks a
field the query asks for or holds one of another kind; setting a value twice changes nothing, so a run cut short is
rerun. CONTRIBUTING.md runs it after an issue is filed or moved between milestones, and the release procedure in
docs/releasing.md runs it with `--check`.

Exit status:
  0  SYNCED, or IN_SYNC with --check
  1  DRIFTED, or a last line FAILED <reason>
  2  a usage error, or `gh` cannot read or change the board, or its answer is not the one the query asks for
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

from console import run_main
from github_client import GitHubClient, GitHubError
from release_tags import release_order
from typed_fields import FieldReader

PROJECT_NUMBER = 1
FIELD_NAME = "Release"
PAGE_SIZE = 50
# Mutations sent in one request; GitHub answers a large batch slowly enough to fail it at the gateway.
BATCH_SIZE = 20
# A batch whose answer was lost is sent again, since each mutation sets a value and setting it twice changes nothing.
BATCH_ATTEMPTS = 3
# Reads the board's answer; a field of another kind raises FieldError, a ValueError main ends with exit 2.
READ = FieldReader()

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
    unseen: list[str]  # the node id of each item whose content the token cannot read


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


def _item(node: object, path: str) -> Item | None:
    """The item a node describes, or None for one whose content this reader cannot see; a node of any other shape
    than the query asks for raises FieldError. Only a node it can see needs its Release, which a write is decided by."""
    node = READ.json_object(node, path, required=("id", "content"), optional=("release",))
    identifier = READ.string(node["id"], f"{path}.id")
    if node["content"] is None:
        return None
    node = READ.json_object(node, path, required=("id", "content", "release"))
    content = READ.json_object(node["content"], f"{path}.content")
    if "number" in content:
        content = READ.json_object(content, f"{path}.content", required=("number", "milestone"))
        label = f"#{READ.integer(content['number'], f'{path}.content.number')}"
        milestone = READ.optional(
            READ.json_object, content["milestone"], f"{path}.content.milestone", required=("title",)
        )
        title = None if milestone is None else READ.string(milestone["title"], f"{path}.content.milestone.title")
    else:
        content = READ.json_object(content, f"{path}.content", required=("title",))
        label = f"draft {READ.string(content['title'], f'{path}.content.title', empty=True)!r}"
        title = None
    release = READ.optional(READ.json_object, node["release"], f"{path}.release", required=("name",))
    name = None if release is None else READ.string(release["name"], f"{path}.release.name")
    return Item(identifier, label, title, name)


def _options(field: dict[str, Any], path: str) -> dict[str, str]:
    """The field's options, option name to option id, in the field's order."""
    options: dict[str, str] = {}
    for index, option in enumerate(READ.array(field["options"], f"{path}.options")):
        option = READ.json_object(option, f"{path}.options[{index}]", required=("id", "name"))
        name = READ.string(option["name"], f"{path}.options[{index}].name")
        options[name] = READ.string(option["id"], f"{path}.options[{index}].id")
    return options


def read_board(client: GitHubClient) -> Board:
    """Every item of the board with its milestone and Release, and the field's options, through every page; an answer
    of another shape than the query asks for raises FieldError, a ValueError, so it ends as a board not read."""
    project = field = None
    options: dict[str, str] = {}
    items: list[Item] = []
    unseen: list[str] = []
    after: str | None = None
    path = "data.repositoryOwner.projectV2"
    while True:
        arguments = ["api", "graphql", "-f", f"query={ITEMS_QUERY}", "-F", "owner={owner}"]
        arguments += ["-F", f"number={PROJECT_NUMBER}", "-f", f"field={FIELD_NAME}", "-F", f"first={PAGE_SIZE}"]
        arguments += ["-f", f"after={after}"] if after else []
        board = _get(client.json(arguments, graphql=True), "data", "repositoryOwner", "projectV2")
        if not isinstance(board, Mapping):
            raise ValueError(f"the repository owner has no project {PROJECT_NUMBER} this token can read")
        board = READ.json_object(board, path, required=("id", "field", "items"))
        project = READ.string(board["id"], f"{path}.id")
        if not isinstance(_get(board, "field", "id"), str):
            raise ValueError(f"project {PROJECT_NUMBER} has no single-select field named {FIELD_NAME!r}")
        single_select = READ.json_object(board["field"], f"{path}.field", required=("id", "options"))
        field = READ.string(single_select["id"], f"{path}.field.id")
        options = _options(single_select, f"{path}.field")
        page = READ.json_object(board["items"], f"{path}.items", required=("pageInfo", "nodes"))
        for index, node in enumerate(READ.array(page["nodes"], f"{path}.items.nodes")):
            item = _item(node, f"{path}.items.nodes[{index}]")
            if item is None:
                unseen.append(node["id"])
            else:
                items.append(item)
        info = READ.json_object(page["pageInfo"], f"{path}.items.pageInfo", required=("hasNextPage", "endCursor"))
        if not READ.boolean(info["hasNextPage"], f"{path}.items.pageInfo.hasNextPage"):
            return Board(project, field, options, items, unseen)
        after = READ.string(info["endCursor"], f"{path}.items.pageInfo.endCursor")


def order_problem(options: Sequence[str]) -> str | None:
    """A problem line when the options named like a version are not in version order, else None."""
    keys = {name: order for name in options if (order := release_order(name)) is not None}
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
    if board.unseen:
        problems.append(
            f"{len(board.unseen)} item{'s' if len(board.unseen) != 1 else ''} this token cannot read, so "
            f"{'their' if len(board.unseen) != 1 else 'its'} {FIELD_NAME} is not checked: {', '.join(board.unseen)}"
        )
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
