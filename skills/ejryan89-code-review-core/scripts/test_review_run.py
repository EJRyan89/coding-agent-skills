"""A prepared run's run.json, read field by field: a run as prepare writes it loads unchanged, one prepared before an
optional field was recorded loads without it, and the first field missing, unknown, or of the wrong kind or shape is
refused by name, by load_run and in the FAILED line of every step that reads the run."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import review_pipeline
import review_run


def prepared_state(**fields: Any) -> dict[str, Any]:
    """A run.json as prepare writes it for an inline re-review by specialists, every optional field there, with
    `fields` in place of its own."""
    state: dict[str, Any] = {
        "schema_version": 1,
        "selector": "example/one#12",
        "mode": "re-review",
        "canary": False,
        "fixture": None,
        "config_path": "/config/code-review.json",
        "host": "copilot-cli",
        "runtime": "copilot-cli",
        "dispatch": "inline",
        "kind": "specialists",
        "request_path": "/run/request.json",
        "reviewer_root": "/run/reviewer",
        "result_path": "/run/result.json",
        "adapter": {
            "name": "example-review",
            "scope": "repository",
            "source": "base",
            "source_commit": "f" * 40,
            "source_hashes": {"review/SKILL.md": "a" * 64},
        },
        "roles": [
            {"id": "generic-review", "prompt_file": "/run/work/generic.md", "result_file": "/run/work/generic.json"},
            {
                "id": "python-reviewer",
                "prompt_file": "/run/work/python.md",
                "result_file": "/run/work/python.json",
                "model": None,
                "effort": "high",
            },
        ],
        "attempts": {"generic-review": 1, "python-reviewer": 0},
        "dispatched_at": {"generic-review": 10_000.5},
        "snapshot": {"source": "checkout", "files": 3},
        "source_repository": None,
        "snapshot_stamp": None,
        "reads": {"generic-review": {"files": 2, "bytes": 310}},
        "notes": ["Scope full, could not compare with v1 (requested auto: first re-review)."],
        "patches": {"app.py": {"sha256": "c" * 64, "lines": [1, 2]}},
        "scope": {
            "requested": "auto",
            "used": "full",
            "reason": "first re-review",
            "since_version": 1,
            "files_changed": None,
            "files_total": 3,
            "lines_changed": None,
            "lines_total": 40,
        },
        "uncovered_files": ["assets/logo.png"],
        "archive_base": {"version": 1, "ledger_sha256": "d" * 64},
        "documents": {"docs/spec.pdf": ["page 1, line 1", ""]},
        "seal": {
            "files": {"request.json": "a" * 64, "work/plan.json": "b" * 64},
            "results": {"generic-review": "e" * 64},
        },
    }
    state.update(fields)
    return state


def write_run(run: Path, state: dict[str, Any]) -> None:
    (run / review_run.RUN_FILE).write_text(json.dumps(state), encoding="utf-8")


def changed(change: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
    """A prepared run.json with `change` applied to it."""
    state = prepared_state()
    change(state)
    return state


def test_a_run_as_prepare_writes_it_loads_unchanged(tmp_path: Path) -> None:
    state = prepared_state()
    write_run(tmp_path, state)
    loaded = review_run.load_run(tmp_path)
    assert state == loaded
    assert json.dumps(state, sort_keys=True) == json.dumps(loaded, sort_keys=True)


# Every field a run prepared before it was recorded may lack, named here apart from the shape the reader takes them
# from, and a role's model and effort, which an entrypoint's role never has.
OPTIONAL_FIELDS = (
    "dispatch",
    "dispatched_at",
    "snapshot",
    "source_repository",
    "snapshot_stamp",
    "reads",
    "archive_base",
    "documents",
    "seal",
)


@pytest.mark.parametrize("name", OPTIONAL_FIELDS)
def test_a_run_prepared_before_an_optional_field_loads_without_it(tmp_path: Path, name: str) -> None:
    state = prepared_state()
    del state[name]
    write_run(tmp_path, state)
    assert state == review_run.load_run(tmp_path)


@pytest.mark.parametrize("path", [" ", "\u00a0", "folder/ "])
def test_an_uncovered_file_git_names_by_whitespace_alone_loads(tmp_path: Path, path: str) -> None:
    state = prepared_state(uncovered_files=[path])
    write_run(tmp_path, state)
    assert review_run.load_run(tmp_path)["uncovered_files"] == [path]


def test_a_run_prepared_before_every_optional_field_loads_without_them(tmp_path: Path) -> None:
    state = {name: value for name, value in prepared_state().items() if name not in OPTIONAL_FIELDS}
    state["roles"] = [{key: role[key] for key in ("id", "prompt_file", "result_file")} for role in state["roles"]]
    state["adapter"] = {"name": "example-review", "scope": "repository"}
    write_run(tmp_path, state)
    assert state == review_run.load_run(tmp_path)
    assert set(OPTIONAL_FIELDS) == review_run.RUN_OPTIONAL_FIELDS
    assert {"model", "effort"} == review_run.ROLE_OPTIONAL_FIELDS
    assert {"source", "source_commit", "source_hashes"} == review_run.ADAPTER_OPTIONAL_FIELDS
    assert {"prior_record"} == review_run.FIXTURE_OPTIONAL_FIELDS


# A fixture canary's paths as prepare writes them for a re-review, and as a run prepared before the copy of the prior
# record was kept has them.
FIXTURE = {"directory": "/fixtures/one", "prior": "/records/v1.json", "prior_record": "/run/prior-record.json"}


@pytest.mark.parametrize(
    "fixture",
    [FIXTURE, {**FIXTURE, "prior": None, "prior_record": None}, {"directory": "/fixtures/one", "prior": None}],
    ids=["re-review", "initial", "before-the-copy"],
)
def test_a_fixture_canarys_run_loads_unchanged(tmp_path: Path, fixture: dict[str, Any]) -> None:
    state = prepared_state(canary=True, fixture=fixture)
    write_run(tmp_path, state)
    assert state == review_run.load_run(tmp_path)


def _role(index: int, **fields: Any) -> Callable[[dict[str, Any]], object]:
    return lambda state: state["roles"][index].update(fields)


def _drop(*path: str | int) -> Callable[[dict[str, Any]], object]:
    def change(state: dict[str, Any]) -> None:
        *parents, last = path
        target: Any = state
        for key in parents:
            target = target[key]
        del target[last]

    return change


def _set(*path: str | int, value: object) -> Callable[[dict[str, Any]], object]:
    def change(state: dict[str, Any]) -> None:
        *parents, last = path
        target: Any = state
        for key in parents:
            target = target[key]
        target[last] = value

    return change


DIGEST = "a lowercase hexadecimal digest of 64 characters"
AT_LEAST_ZERO = "an integer of at least 0"

# Each damaged run: what is done to a prepared run.json, and the refusal, which names the field.
DAMAGED = [
    pytest.param(
        _drop("selector"), "run must be an object with field selector, found an object without it", id="missing"
    ),
    pytest.param(
        _set("added", value=1), 'run must be an object of known fields, found unknown field "added"', id="unknown-field"
    ),
    pytest.param(
        _set("selector", value=None), "run.selector must be a non-empty single-line string, found null", id="selector"
    ),
    pytest.param(
        _set("mode", value="later"), 'run.mode must be one of "initial", "re-review", found string "later"', id="mode"
    ),
    pytest.param(_set("canary", value="yes"), 'run.canary must be a boolean, found string "yes"', id="canary"),
    pytest.param(_set("fixture", value=[]), "run.fixture must be an object or null, found an array", id="fixture"),
    pytest.param(
        _set("config_path", value=None), "run.config_path must be a non-empty string, found null", id="config-path"
    ),
    pytest.param(_set("host", value=5), "run.host must be a non-empty string or null, found integer 5", id="host"),
    pytest.param(
        _set("dispatch", value="remote"),
        'run.dispatch must be one of "subagents", "copilot-host", "inline", found string "remote"',
        id="dispatch",
    ),
    pytest.param(
        _set("kind", value="plugin"),
        'run.kind must be one of "entrypoint", "generic", "specialists", found string "plugin"',
        id="kind",
    ),
    pytest.param(
        _set("result_path", value=""), "run.result_path must be a non-empty string, found an empty string", id="result"
    ),
    pytest.param(_set("adapter", value=None), "run.adapter must be an object, found null", id="adapter"),
    pytest.param(
        _drop("adapter", "name"),
        "run.adapter must be an object with field name, found an object without it",
        id="adapter-missing-name",
    ),
    pytest.param(
        _set("adapter", "scope", value="team"),
        'run.adapter.scope must be one of "generic", "repository", found string "team"',
        id="adapter-scope",
    ),
    pytest.param(
        _set("adapter", "source", value="head"),
        'run.adapter.source must be one of "generic", "trusted-ref", "base", "default-branch", "generic-fallback", '
        'found string "head"',
        id="adapter-source",
    ),
    pytest.param(
        _set("adapter", "source_commit", value="F" * 40),
        "run.adapter.source_commit must be a lowercase hexadecimal digest of 40 or 64 characters or null, found a "
        "string with a character that is not a lowercase hexadecimal digit",
        id="adapter-commit",
    ),
    pytest.param(
        _set("adapter", "source_hashes", "review/SKILL.md", value="abc"),
        f"run.adapter.source_hashes.review/SKILL.md must be {DIGEST}, found a string of 3 characters",
        id="adapter-hash",
    ),
    pytest.param(
        _set("adapter", "id", value="example-review"),
        'run.adapter must be an object of known fields, found unknown field "id"',
        id="adapter-unknown-field",
    ),
    pytest.param(
        _set("fixture", value={"prior": None}),
        "run.fixture must be an object with field directory, found an object without it",
        id="fixture-missing-directory",
    ),
    pytest.param(
        _set("fixture", value={**FIXTURE, "prior_record": 5}),
        "run.fixture.prior_record must be a non-empty string or null, found integer 5",
        id="fixture-prior-record",
    ),
    pytest.param(
        _set("fixture", value={**FIXTURE, "prior": ""}),
        "run.fixture.prior must be a non-empty string or null, found an empty string",
        id="fixture-prior",
    ),
    pytest.param(_set("roles", value={}), "run.roles must be an array, found an object", id="roles"),
    pytest.param(
        _drop("roles", 1, "result_file"),
        "run.roles[1] must be an object with field result_file, found an object without it",
        id="role-missing-field",
    ),
    pytest.param(
        _role(1, id="generic-review"),
        'run.roles[1].id must be a role id no earlier role has, found string "generic-review"',
        id="role-repeated",
    ),
    pytest.param(
        _role(1, model=5), "run.roles[1].model must be a non-empty string or null, found integer 5", id="role-model"
    ),
    pytest.param(
        _role(0, state="running"),
        'run.roles[0] must be an object of known fields, found unknown field "state"',
        id="role-unknown-field",
    ),
    pytest.param(
        _set("attempts", "ghost", value=0),
        'run.attempts must be an object of known fields, found unknown field "ghost"',
        id="attempts-unknown-role",
    ),
    pytest.param(
        _drop("attempts", "python-reviewer"),
        "run.attempts must be an object with field python-reviewer, found an object without it",
        id="attempts-missing-role",
    ),
    pytest.param(
        _set("attempts", "generic-review", value=-1),
        f"run.attempts.generic-review must be {AT_LEAST_ZERO}, found integer -1",
        id="attempts-below-zero",
    ),
    pytest.param(
        _set("dispatched_at", "ghost", value=1.0),
        'run.dispatched_at must be an object of known fields, found unknown field "ghost"',
        id="dispatched-unknown-role",
    ),
    pytest.param(
        _set("dispatched_at", "generic-review", value="soon"),
        'run.dispatched_at.generic-review must be a finite number, found string "soon"',
        id="dispatched-at",
    ),
    pytest.param(
        _set("reads", "generic-review", "files", value=-1),
        f"run.reads.generic-review.files must be {AT_LEAST_ZERO}, found integer -1",
        id="read-count-below-zero",
    ),
    pytest.param(
        _set("reads", "generic-review", "searches", value=-1),
        f"run.reads.generic-review.searches must be {AT_LEAST_ZERO}, found integer -1",
        id="search-count-below-zero",
    ),
    pytest.param(
        _drop("reads", "generic-review", "bytes"),
        "run.reads.generic-review must be an object with field bytes, found an object without it",
        id="read-count-missing",
    ),
    pytest.param(
        _set("reads", "ghost", value={"files": 0, "bytes": 0}),
        'run.reads must be an object of known fields, found unknown field "ghost"',
        id="reads-unknown-role",
    ),
    pytest.param(_set("seal", value=[]), "run.seal must be an object, found an array", id="seal"),
    pytest.param(
        _drop("seal", "results"),
        "run.seal must be an object with field results, found an object without it",
        id="seal-missing-results",
    ),
    pytest.param(
        _set("seal", "files", "request.json", value="abc"),
        f"run.seal.files.request.json must be {DIGEST}, found a string of 3 characters",
        id="seal-digest",
    ),
    pytest.param(
        _set("seal", "results", "ghost", value="e" * 64),
        'run.seal.results must be an object of known fields, found unknown field "ghost"',
        id="seal-unknown-role",
    ),
    pytest.param(
        _set("notes", 0, value=""), "run.notes[0] must be a non-empty string, found an empty string", id="note"
    ),
    pytest.param(
        _set("patches", "app.py", value=[]), "run.patches.app.py must be an object, found an array", id="patch"
    ),
    pytest.param(
        _set("scope", "used", value="partial"),
        'run.scope.used must be one of "full", "incremental", found string "partial"',
        id="scope-used",
    ),
    pytest.param(
        _set("scope", "files_changed", value=-1),
        f"run.scope.files_changed must be {AT_LEAST_ZERO} or null, found integer -1",
        id="scope-count",
    ),
    pytest.param(
        _drop("scope", "reason"),
        "run.scope must be an object with field reason, found an object without it",
        id="scope-missing-field",
    ),
    pytest.param(
        _set("uncovered_files", value="assets/logo.png"),
        'run.uncovered_files must be an array, found string "assets/logo.png"',
        id="uncovered-files",
    ),
    pytest.param(
        _set("uncovered_files", 0, value=""),
        "run.uncovered_files[0] must be a non-empty string, found an empty string",
        id="uncovered-file-empty",
    ),
    pytest.param(_set("archive_base", value=1), "run.archive_base must be an object, found integer 1", id="archive"),
    pytest.param(
        _set("documents", "docs/spec.pdf", 0, value=3),
        "run.documents.docs/spec.pdf[0] must be a string, found integer 3",
        id="document-place",
    ),
]


@pytest.mark.parametrize(("change", "message"), DAMAGED)
def test_a_damaged_field_is_refused_by_name(
    tmp_path: Path, change: Callable[[dict[str, Any]], object], message: str
) -> None:
    write_run(tmp_path, changed(change))
    with pytest.raises(review_run.PipelineFieldError) as raised:
        review_run.load_run(tmp_path)
    assert isinstance(raised.value, review_run.PipelineError)
    assert message == str(raised.value)


def test_the_first_damaged_field_is_the_one_named(tmp_path: Path) -> None:
    state = prepared_state(canary="yes", notes=[3])
    write_run(tmp_path, state)
    with pytest.raises(review_run.PipelineFieldError, match=r"^run\.canary must be a boolean"):
        review_run.load_run(tmp_path)


def _pipeline(*arguments: str) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = review_pipeline.main(list(arguments))
    return code, stdout.getvalue(), stderr.getvalue()


# Every step after prepare that reads a run, with the arguments it needs beside --run.
STEPS = [
    ["check"],
    ["finalize"],
    ["unfinalized"],
    ["next-role"],
    ["dispatch"],
    ["wait", "--timeout", "1"],
    ["workflow"],
    ["wait-reviewers", "--timeout", "1"],
    ["validate-result", "--role", "generic-review"],
]

# A damaged field of a role, of the adapter, and of a fixture canary's paths, each with its refusal.
STEP_DAMAGE = [
    pytest.param(
        _role(1, model=5), "run.roles[1].model must be a non-empty string or null, found integer 5", id="role-model"
    ),
    pytest.param(
        _drop("adapter", "name"),
        "run.adapter must be an object with field name, found an object without it",
        id="adapter-name",
    ),
    pytest.param(
        _set("fixture", value={**FIXTURE, "prior_record": 5}),
        "run.fixture.prior_record must be a non-empty string or null, found integer 5",
        id="fixture-prior-record",
    ),
]


@pytest.mark.parametrize("arguments", STEPS, ids=lambda a: a[0])
@pytest.mark.parametrize(("change", "message"), STEP_DAMAGE)
def test_every_step_that_reads_a_damaged_run_names_the_field_in_its_failed_line(
    tmp_path: Path, arguments: list[str], change: Callable[[dict[str, Any]], object], message: str
) -> None:
    run = tmp_path.resolve()
    command, *rest = arguments
    state = prepared_state(canary=True, dispatch="copilot-host" if command in {"dispatch", "wait"} else "inline")
    change(state)
    write_run(run, state)
    before = (run / review_run.RUN_FILE).read_bytes()
    # A step that can take several runs names the run each line is for; a step given one run names only the reason.
    several = {"check", "finalize", "unfinalized", "wait-reviewers"}
    expected = f"FAILED {run} {message}\n" if command in several else f"FAILED {message}\n"
    assert (1, expected, "") == _pipeline(command, "--run", str(run), *rest)
    assert before == (run / review_run.RUN_FILE).read_bytes()
