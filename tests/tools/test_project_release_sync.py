"""Regression suite for tools/project_release_sync.py, with `gh api graphql` replaced by a fake board.

The expected status lines and problem lines are written here as literals, not read back from the module, so a change
to what the tool prints fails this suite until CONTRIBUTING.md and docs/releasing.md are revisited too.
"""

from __future__ import annotations

import contextlib
import io
import json
import runpy
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

from github_client import CommandResult, GitHubClient

from tools import project_release_sync

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OPTIONS = ["v0.1.0", "v0.2.0", "v0.9.0", "v0.10.0", "v1.0.0"]


def node(number: int | None, milestone: str | None, release: str | None) -> dict[str, Any]:
    """An item node as the board query returns it; a None number is a draft issue."""
    content: dict[str, Any] = (
        {"title": "a draft"} if number is None else {"number": number, "milestone": milestone and {"title": milestone}}
    )
    return {"id": f"ITEM_{number}", "content": content, "release": release and {"name": release}}


class FakeBoard:
    """Answers the board query page by page and records each mutation's variables; `failures` fails mutations."""

    def __init__(
        self,
        pages: list[list[dict[str, Any]]],
        options: Sequence[str] = OPTIONS,
        *,
        field: bool = True,
        project: bool = True,
        failures: Sequence[CommandResult] = (),
    ) -> None:
        self.pages = pages
        self.options = options
        self.field = field
        self.project = project
        self.failures = list(failures)
        self.reads: list[dict[str, str]] = []
        self.mutations: list[dict[str, str]] = []

    def __call__(self, command: Sequence[str], **_: object) -> CommandResult:
        # The default runner takes the time limit as a keyword, which the entry-point test passes through.
        assert list(command[:3]) == ["gh", "api", "graphql"]
        fields = dict(value.split("=", 1) for value in command[4::2])
        if fields["query"].startswith("mutation"):
            if self.failures:
                return self.failures.pop(0)
            self.mutations.append(fields)
            return CommandResult(0, json.dumps({"data": {}}), "")
        self.reads.append(fields)
        index = len(self.reads) - 1
        field = {"id": "FIELD", "options": [{"id": f"OPT_{name}", "name": name} for name in self.options]}
        board = {
            "id": "PROJECT",
            "field": field if self.field else {},
            "items": {
                "pageInfo": {"hasNextPage": index + 1 < len(self.pages), "endCursor": f"CURSOR_{index}"},
                "nodes": self.pages[index],
            },
        }
        owner = {"projectV2": board if self.project else None}
        return CommandResult(0, json.dumps({"data": {"repositoryOwner": owner}}), "")


def run(board: FakeBoard, *arguments: str) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    client = GitHubClient(runner=board, sleeper=lambda _: None)
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = project_release_sync.main(list(arguments), client=client)
    return code, stdout.getvalue(), stderr.getvalue()


def changes(board: FakeBoard) -> dict[str, str | None]:
    """Each item the mutations touched, mapped to the option it was set to, or None when it was cleared."""
    touched: dict[str, str | None] = {}
    for fields in board.mutations:
        for key, value in fields.items():
            if key.startswith("item"):
                touched[value] = fields.get("option" + key.removeprefix("item"))
    return touched


def test_each_item_takes_its_milestone_and_one_without_a_milestone_is_cleared() -> None:
    board = FakeBoard(
        [
            [node(1, "v0.10.0", None), node(2, "v0.9.0", "v0.9.0"), node(3, None, "v0.2.0")],
            [node(4, "v1.0.0", "v0.1.0"), node(5, None, None), node(None, None, "v0.1.0")],
        ]
    )
    assert run(board) == (0, "SYNCED\nupdated 2, cleared 2\n", "")
    assert changes(board) == {"ITEM_1": "OPT_v0.10.0", "ITEM_4": "OPT_v1.0.0", "ITEM_3": None, "ITEM_None": None}


def test_every_page_is_read_from_the_cursor_before_it() -> None:
    board = FakeBoard([[node(1, "v0.1.0", "v0.1.0")], [node(2, "v0.1.0", "v0.1.0")], []])
    assert run(board, "--check") == (0, "IN_SYNC\n", "")
    assert [read.get("after") for read in board.reads] == [None, "CURSOR_0", "CURSOR_1"]
    assert {"{owner}"} == {read["owner"] for read in board.reads}
    assert {("1", "Release")} == {(read["number"], read["field"]) for read in board.reads}


def test_check_changes_nothing_and_names_each_item_that_does_not_match() -> None:
    board = FakeBoard([[node(7, "v0.10.0", "v0.1.0"), node(8, None, "v0.9.0"), node(9, "v0.2.0", "v0.2.0")]])
    expected = "DRIFTED\n- #7: Release is v0.1.0, milestone is v0.10.0\n- #8: Release is v0.9.0, milestone is none\n"
    assert (1, expected, "") == run(board, "--check")
    assert board.mutations == []


def test_a_milestone_with_no_option_is_left_unset_and_reported() -> None:
    board = FakeBoard([[node(1, "v0.11.0", None), node(2, "v0.11.0", None), node(3, "v0.12.0", None)]])
    expected = (
        "DRIFTED\nupdated 0, cleared 0\n"
        "- milestone v0.11.0 has no Release option (2 items)\n"
        "- milestone v0.12.0 has no Release option (1 item)\n"
    )
    assert (1, expected, "") == run(board)
    assert board.mutations == []


@pytest.mark.parametrize(
    "options",
    [
        pytest.param(["v0.1.0", "v0.10.0", "v0.2.0"], id="title order"),
        pytest.param(["v1.0.0", "v0.9.0"], id="major before minor"),
    ],
)
def test_options_out_of_version_order_are_drift(options: list[str]) -> None:
    board = FakeBoard([[node(1, options[0], options[0])]], options)
    ordered = ", ".join(sorted(options, key=lambda name: [int(part) for part in name[1:].split(".")]))
    expected = f"DRIFTED\n- the Release options are not in version order: {', '.join(options)} (expected {ordered})\n"
    assert (1, expected, "") == run(board, "--check")


def test_options_in_version_order_with_a_name_that_is_no_version_hold() -> None:
    board = FakeBoard([[node(1, "v0.9.0", "v0.9.0")]], ["Backlog", "v0.9.0", "v0.10.0", "v1.0.0"])
    assert run(board, "--check") == (0, "IN_SYNC\n", "")


def test_an_item_this_reader_cannot_see_is_left_alone() -> None:
    hidden = {"id": "HIDDEN", "content": None, "release": {"name": "v0.1.0"}}
    board = FakeBoard([[hidden, node(1, "v0.2.0", "v0.2.0")]])
    assert run(board) == (0, "SYNCED\nupdated 0, cleared 0\n", "")
    assert board.mutations == []


def test_changes_go_in_batches_with_their_values_as_variables() -> None:
    board = FakeBoard([[node(number, "v0.10.0", None) for number in range(25)]])
    assert run(board) == (0, "SYNCED\nupdated 25, cleared 0\n", "")
    assert [sum(key.startswith("item") for key in fields) for fields in board.mutations] == [20, 5]
    first = board.mutations[0]
    assert (first["project"], first["field"]) == ("PROJECT", "FIELD")
    assert "singleSelectOptionId: $option0" in first["query"]
    assert "ITEM_" not in first["query"]
    assert {f"ITEM_{number}" for number in range(25)} == set(changes(board))


def test_a_clear_uses_the_clear_mutation() -> None:
    board = FakeBoard([[node(1, None, "v0.1.0")]])
    run(board)
    assert "clearProjectV2ItemFieldValue" in board.mutations[0]["query"]
    assert "updateProjectV2ItemFieldValue" not in board.mutations[0]["query"]


def test_a_batch_whose_answer_is_lost_is_sent_again() -> None:
    lost = CommandResult(1, "", "gh: HTTP 502")
    board = FakeBoard([[node(1, "v0.10.0", None)]], failures=[lost, lost])
    assert run(board) == (0, "SYNCED\nupdated 1, cleared 0\n", "")
    assert changes(board) == {"ITEM_1": "OPT_v0.10.0"}


def test_a_batch_lost_every_time_fails_without_a_traceback() -> None:
    lost = CommandResult(1, "", "gh: HTTP 502")
    board = FakeBoard([[node(1, "v0.10.0", None)]], failures=[lost, lost, lost])
    code, stdout, stderr = run(board)
    assert (code, stdout) == (2, "")
    assert stderr.startswith("ERROR: gh: HTTP 502"), stderr
    assert "outcome of this POST request is unknown" in stderr


def test_a_refused_mutation_is_not_sent_again() -> None:
    refused = CommandResult(1, "", "gh: Resource not accessible by integration (HTTP 403)")
    board = FakeBoard([[node(1, "v0.10.0", None)]], failures=[refused, refused])
    assert run(board)[0] == 2
    assert len(board.failures) == 1


@pytest.mark.parametrize(
    ("board", "message"),
    [
        pytest.param(
            FakeBoard([[]], project=False),
            "ERROR: the repository owner has no project 1 this token can read\n",
            id="no project",
        ),
        pytest.param(
            FakeBoard([[]], field=False),
            "ERROR: project 1 has no single-select field named 'Release'\n",
            id="no field",
        ),
    ],
)
def test_a_board_without_the_project_or_field_is_an_error(board: FakeBoard, message: str) -> None:
    assert (2, "", message) == run(board, "--check")


def test_a_read_without_the_project_scope_is_an_error() -> None:
    def runner(command: Sequence[str]) -> CommandResult:
        return CommandResult(1, "", "error: your authentication token is missing required scopes [read:project]")

    stderr = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
        code = project_release_sync.main(["--check"], client=GitHubClient(runner=runner, sleeper=lambda _: None))
    assert code == 2
    assert stderr.getvalue() == "ERROR: error: your authentication token is missing required scopes [read:project]\n"


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        pytest.param(["--check"], (0, "IN_SYNC\n"), id="check"),
        pytest.param([], (0, "SYNCED\nupdated 0, cleared 0\n"), id="sync"),
    ],
)
def test_cli_entry_point_has_the_exact_status_output_and_process_exit_code(
    arguments: list[str], expected: tuple[int, str]
) -> None:
    board = FakeBoard([[node(1, "v0.9.0", "v0.9.0")]])
    stdout = io.StringIO()
    with (
        mock.patch("github_client.subprocess_runner", side_effect=board),
        mock.patch.object(sys, "argv", ["project_release_sync.py", *arguments]),
        contextlib.redirect_stdout(stdout),
        pytest.raises(SystemExit) as raised,
    ):
        runpy.run_path(str(REPOSITORY_ROOT / "tools" / "project_release_sync.py"), run_name="__main__")
    assert expected == (raised.value.code, stdout.getvalue())


def test_contributing_runs_the_sync_after_filing_an_issue() -> None:
    text = (REPOSITORY_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    section = text.split("\n## Issues and pull requests\n", 1)[1].split("\n## ", 1)[0]
    for phrase in ("Release", "python tools/project_release_sync.py", "version order"):
        assert phrase in section
    assert "grouped by milestone" not in section


def test_the_release_procedure_adds_the_option_and_checks_the_board() -> None:
    text = (REPOSITORY_ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
    milestones = text.split("\n## Milestones\n", 1)[1].split("\n## ", 1)[0]
    before_tagging = text.split("\n## Before tagging\n", 1)[1].split("\n## ", 1)[0]
    assert "Release" in milestones
    assert "python tools/project_release_sync.py --check" in before_tagging


def test_after_tagging_closes_the_milestone_and_syncs_the_next_one() -> None:
    text = (REPOSITORY_ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
    after_tagging = text.split("\n## After tagging\n", 1)[1].split("\n## ", 1)[0]
    step = next(line for line in after_tagging.splitlines() if "**Milestones.**" in line)
    for phrase in ("Close the released milestone", "tag date", "python tools/project_release_sync.py", "`SYNCED`"):
        assert phrase in step
    assert "--check" not in step
    # The step comes before the rotation audit, which files its findings into the next milestone.
    assert after_tagging.index("**Milestones.**") < after_tagging.index("**Rotation audit.**")
