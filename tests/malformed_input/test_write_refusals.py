"""Every place that catches json_writer's refusal, driven into it: each ends as its contract says, never a traceback.

json_writer refuses a value no JSON text can hold, such as one holding NaN or a lone surrogate, as JsonWriteRefused.
refusal_scan.py finds each place in the repository's code that catches it: an except clause, a suppress, or a tuple of
the errors an entry point ends as a FAILED line. Each row of REFUSALS names one such place, the step inside its handler
that the row makes refuse, and how the row drives the place; the test patches that step to raise the refusal, drives
the place, and holds what it ended with to the row's literal outcome: the exit code and the lines an entry point
prints, the error a converting function raises, by its type and message, or what a place that goes on past the
refusal, as a suppress does, returns or leaves behind. A place that writes again past the refusal, as a fallback does,
has its step refuse the first call alone. The step is json_writer.dumps itself where
the place calls it directly, and otherwise the call in the handler's try that would reach it, since a handler such as
skill_evals' that quotes its message through json_writer must still write its own line. A place the scan finds that no
row drives, or a row whose place the scan no longer finds, fails, so a new catch takes its row in the pull request
that adds it. The test also fails a row whose step the place never called, so a row cannot pass by never refusing.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import pkgutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
import readers_repository
import refusal_scan
from reader_table import REPOSITORY

sys.path.insert(0, str(REPOSITORY / "skills" / "ejryan89-skill-core" / "scripts"))
sys.path.insert(0, str(REPOSITORY / "tests" / "deployer"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))
sys.path.insert(0, str(sibling_scripts("audit-ai-config")))
sys.path.insert(0, str(sibling_scripts("curate-agent-memory")))
sys.path.insert(0, str(sibling_scripts("flag-review-finding")))
sys.path.insert(0, str(sibling_scripts("review-document")))
sys.path.insert(0, str(sibling_scripts("review-insights")))
sys.path.insert(0, str(sibling_scripts("update-pr-dashboard")))

import audit_ai_config
import flag_review_finding
import hook_decision
import json_writer
import mcp_handshake
import memory_audit
import review_config
import review_document
import review_guard
import review_insights
import review_io
import review_pipeline
import review_runtime
import review_source
from dashboard_pipeline import main as dashboard_main
from harness import Deployment, deployment
from json_writer import JsonWriteRefused

from deployer import journal, manifest, platform_support
from deployer.errors import DeployError
from tools import (
    checkout_trust,
    deployable_report,
    hub_guard,
    new_skill,
    plugin_build,
    plugin_canary,
    plugin_release,
    skill_evals,
    skill_reference,
)

REASON = "the value holds a lone surrogate U+D800, which UTF-8 cannot encode"
DUMPS = "json_writer.dumps"
DEBUG_HINT = "Rerun with --debug to see the traceback."
audit_repository = readers_repository.audit_repository


@dataclass(frozen=True)
class Row:
    """One place that catches the refusal: its name as refusal_scan.py gives it, the step patched to refuse, as a
    mock.patch target, how the row drives the place in a folder of its own, and the outcome it must end with."""

    place: str
    step: str
    drive: Callable[[Path], object]
    outcome: object
    # Which of the place's handlers the row drives, where it has more than one.
    label: str = ""
    # Whether the step refuses its first call alone, for a place that writes again past the refusal, as a fallback
    # does; otherwise it refuses every call.
    once: bool = False


def raised(expected: type[Exception], call: Callable[[], object]) -> str:
    """The error of the type the place's contract names that a call raised, by its type and message, or what it
    returned when it raised none; any other error escapes and fails the row with its traceback."""
    try:
        returned = call()
    except expected as error:
        return f"{type(error).__name__}: {error}"
    return f"returned {returned!r}"


def ended(call: Callable[[], int]) -> tuple[int, str, str]:
    """An entry point's exit code and what it printed to stdout and to stderr."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = call()
    return code, out.getvalue(), err.getvalue()


def forward(text: str, folder: Path) -> str:
    """Text with the row's folder, as a path is printed with either slash, written <tmp>."""
    return text.replace(str(folder), "<tmp>").replace(folder.as_posix(), "<tmp>").replace("\\", "/")


@contextlib.contextmanager
def deployed(deploy_first: bool) -> Iterator[Deployment]:
    """A fixture source of one skill and a configured home in the harness's own folder, which checks that a run left
    the source as it was, deployed first when the place reads what it deployed."""
    with deployment() as deployer:
        deployer.make_source_json()
        deployer.make_skill("alpha", "Alpha")
        deployer.make_config()
        if deploy_first:
            deployer.deploy_ok("--all")
        yield deployer


# The lines a deployment prints of the fixture source before anything it selects.
FOUND = ("Source: Test Skills (test/skills)", "Home: <tmp>/home", "Found 1 skill.")


def printed(*blocks: tuple[str, ...]) -> str:
    """What the deployer prints as these blocks of lines, each after a blank line, and a blank line at the end."""
    return "".join("\n" + "\n".join(block) + "\n" for block in blocks) + "\n"


def deploy_run(*arguments: str, deploy_first: bool = False) -> Callable[[Path], object]:
    """A deployer command's exit code and its whole output, with the harness's folder written <tmp>."""

    def drive(_: Path) -> object:
        with deployed(deploy_first) as deployer:
            result = deployer.run_cli(*arguments)
            return result.code, forward(result.output, deployer.root)

    return drive


def _deployer_error(drive: Callable[[Path], None]) -> Callable[[Path], object]:
    def run(folder: Path) -> object:
        return forward(raised(DeployError, lambda: drive(folder)), folder)

    return run


def _journal(folder: Path) -> None:
    journal.Journal(folder / "journal.jsonl", "run").write(cast(journal.JournalEntry, {}))


def _manifest(folder: Path) -> None:
    manifest.Manifest(folder / "manifest.json", cast(manifest.ManifestData, {})).save()


def _hub_guard(folder: Path) -> object:
    reading = hub_guard.Reading({}, text="Get-Item x")
    with (
        mock.patch.object(platform_support, "find_pwsh", return_value="pwsh"),
        mock.patch.object(hub_guard, "run_bounded") as bounded,
    ):
        events = hub_guard.powershell_events("Get-Item x", reading)
    return events, reading.unread, bounded.called


def _new_skill(*extra: str) -> Callable[[Path], object]:
    def drive(folder: Path) -> object:
        return ended(lambda: new_skill.main(["alpha", "--description", "Alpha.", "--root", str(REPOSITORY), *extra]))

    return drive


def _audit_ai_config(folder: Path) -> object:
    (folder / ".git").mkdir()
    with mock.patch.object(sys, "argv", ["audit_ai_config.py", "--json", "--root", str(folder)]):
        return ended(audit_ai_config.main)


def _plugin_canary(folder: Path) -> object:
    home = folder / "home"
    home.mkdir()
    with (
        mock.patch.object(checkout_trust, "refusal", return_value=None),
        mock.patch.object(plugin_canary, "create_home", return_value=home),
    ):
        code, out, err = ended(lambda: plugin_canary.main([]))
    return code, forward(out, folder), err


def _plugin_release_build(folder: Path) -> object:
    entries = [{"name": "alpha", "repository": "https://example.com/alpha"}]
    with (
        mock.patch.object(plugin_release, "archives", return_value={"alpha": b""}),
        mock.patch.object(plugin_release, "read_marketplace", return_value=({"plugins": entries}, entries)),
    ):
        outcome = raised(plugin_release.ReleaseError, lambda: plugin_release.build(folder, "v1.0.0", folder / "out"))
    return outcome, sorted(path.name for path in folder.iterdir())


def _skill_evals(folder: Path) -> object:
    results = folder / "results.md"
    results.write_text("results\n", encoding="utf-8")
    outcome = ended(lambda: skill_evals.record_reading("alpha", "agrees", results))
    return outcome, results.read_text(encoding="utf-8")


def _handshake_entry() -> dict[str, Any]:
    return {"command": "server"}


@contextlib.contextmanager
def _running_server() -> Iterator[mock.Mock]:
    """A started server that has not exited, so the session's failure is its own."""
    running = mock.Mock()
    running.wait.side_effect = subprocess.TimeoutExpired("server", 0)
    yield running


def _handshake(stream: Callable[..., Any] | None) -> Callable[[Path], object]:
    """The handshake with a server command that resolves, started by `stream`, or by the row's own patch of
    streaming when None."""

    def drive(folder: Path) -> object:
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(mcp_handshake, "resolve_command", return_value="server"))
            if stream is not None:
                stack.enter_context(mock.patch.object(mcp_handshake, "streaming", stream))
            return raised(RuntimeError, lambda: mcp_handshake.handshake(folder, _handshake_entry(), 5.0))

    return drive


def _memory_audit(folder: Path) -> object:
    arguments = argparse.Namespace(user_dir=folder, memory_dir=folder, repo=folder, instructions=[], output=None)
    with mock.patch.object(memory_audit, "audit", return_value={}):
        return memory_audit.audit_lines(arguments)


def _load_config(folder: Path) -> object:
    path = folder / "config.json"
    path.write_text("{}\n", encoding="utf-8")
    return raised(review_config.ConfigurationError, lambda: review_config.load_config(path))


def _log_read(folder: Path) -> object:
    read = folder / review_guard.SOURCE / "read.txt"
    read.parent.mkdir()
    read.write_text("read\n", encoding="utf-8")
    log = review_guard.read_log(folder, "reviewer")
    log.parent.mkdir()
    log.write_text("", encoding="utf-8")
    # The refused line is left uncounted and the read goes on: the call returns, and the log stays empty.
    review_guard._log_read("Read", read, (folder, {"id": "reviewer"}))
    return log.read_text(encoding="utf-8")


def _atomic_write_json(folder: Path) -> object:
    path = folder / "record.json"
    return forward(
        raised(review_io.PersistenceError, lambda: review_io.atomic_write_json(path, {})), folder
    ), path.exists()


def _expected_errors_of_the_pipeline(folder: Path) -> object:
    def handler(*_: object) -> int:
        json_writer.dumps(None)
        return 0

    return ended(lambda: review_pipeline._translate_errors(handler, argparse.Namespace(), None))


def _tarball(folder: Path) -> object:
    return raised(
        review_runtime.RuntimeContractError,
        lambda: review_runtime.verify_github_tarball(
            folder / "archive.tar.gz", {}, repository="owner/name", commit="0123456789ab" + "0" * 28
        ),
    )


def _count(folder: Path) -> object:
    log = folder / "reads.log"
    log.write_text("", encoding="utf-8")
    # The refused entry is left uncounted: the call returns, and the log stays empty.
    review_source._count(log, "read.txt")
    return log.read_text(encoding="utf-8")


def _source_main(folder: Path) -> object:
    return ended(lambda: review_source.main(["source-file", "--run", str(folder), "--role", "r", "--path", "a"]))


def _write_fixture(folder: Path) -> object:
    document = review_document.Document("notes.md", b"notes\n", None, review_document.Base(None, None, "new"))
    fixture = folder / "fixture"
    outcome = raised(
        review_document.DocumentError, lambda: review_document.write_fixture(fixture, document, {"number": 1}, None)
    )
    return outcome, fixture.exists()


def _resolve_recorded(folder: Path) -> object:
    entries: list[dict[str, Any]] = [{"resolved_flags": ["F1"]}]
    services = mock.Mock()
    services.flags_path.return_value = folder / "flags.json"
    with mock.patch.object(review_insights, "write_report") as written:
        outcome = forward(
            raised(
                review_insights.InsightError,
                lambda: review_insights._resolve_recorded(folder / "insights.json", {}, entries, "text", services),
            ),
            folder,
        )
    return outcome, entries, written.call_count


def _write_briefs(folder: Path) -> object:
    briefs = folder / "briefs"
    briefs.mkdir()
    with mock.patch.object(audit_repository.tempfile, "mkdtemp", return_value=str(briefs)):
        return forward(
            raised(audit_repository.AuditError, lambda: audit_repository.write_briefs([("alpha", "brief")])), folder
        )


def _write_json(folder: Path) -> object:
    path = folder / "record.json"
    return raised(OSError, lambda: audit_repository.write_json(path, {})), path.exists()


REFUSALS: tuple[Row, ...] = (
    Row(
        "tools.deployable_report._write_results",
        DUMPS,
        lambda folder: forward(
            raised(DeployError, lambda: deployable_report._write_results(folder / "results.json", [])), folder
        ),
        f"DeployError: ERROR: Cannot write the results file: <tmp>/results.json: {REASON}",
    ),
    Row(
        "tools.hub_guard.powershell_events",
        DUMPS,
        _hub_guard,
        (None, [f"could not hand pwsh the command ({REASON})"], False),
    ),
    Row("tools.new_skill.main", "tools.new_skill.scaffold", _new_skill(), (2, "", f"FAILED {REASON}\n")),
    Row(
        "tools.new_skill._main_repository",
        "tools.new_skill.scaffold_repository",
        _new_skill("--repository"),
        (2, "", f"FAILED {REASON}\n"),
    ),
    Row(
        "tools.plugin_build._json_file",
        DUMPS,
        lambda _: raised(plugin_build.BuildError, lambda: plugin_build._json_file("alpha", "plugin.json", {})),
        f"BuildError: plugin alpha: plugin.json cannot be written: {REASON}",
    ),
    Row(
        "tools.plugin_build.main",
        "tools.plugin_build.load_source",
        lambda _: ended(lambda: plugin_build.main([])),
        (1, f"FAILED {REASON}\n", ""),
    ),
    Row(
        "tools.plugin_canary.main",
        "tools.plugin_canary.canary",
        _plugin_canary,
        (
            1,
            f'FAILED "{REASON}"\nHOME_KEPT "<tmp>/home" "the run failed, so its transcripts stay for reading; '
            'remove it with --remove-home"\n',
            "",
        ),
    ),
    Row(
        "tools.plugin_release.build",
        DUMPS,
        _plugin_release_build,
        (f"ReleaseError: .claude-plugin/marketplace.json cannot be written: {REASON}", []),
    ),
    Row(
        "tools.plugin_release.main",
        "tools.plugin_release.build",
        lambda _: ended(lambda: plugin_release.main(["build", "v1.0.0"])),
        (1, f"FAILED {REASON}\n", ""),
    ),
    Row(
        "tools.skill_evals.record_reading",
        "tools.skill_evals.read_sample",
        _skill_evals,
        ((1, f'FAILED "{REASON}"\n', ""), "results\n"),
    ),
    Row(
        "tools.skill_reference.main",
        "tools.skill_reference.problems",
        lambda _: ended(lambda: skill_reference.main(["--root", str(REPOSITORY)])),
        (2, "", f"FAILED {REASON}\n"),
    ),
    Row(
        "deployer.check.execute",
        "deployer.tools.probe",
        deploy_run("check"),
        (1, printed((f"ERROR: Could not finish the check: {REASON}", DEBUG_HINT))),
    ),
    Row(
        "deployer.journal.Journal.write",
        DUMPS,
        _deployer_error(_journal),
        f"DeployError: ERROR: Cannot write the journal <tmp>/journal.jsonl: {REASON}",
    ),
    Row(
        "deployer.manifest.Manifest.save",
        DUMPS,
        _deployer_error(_manifest),
        f"DeployError: ERROR: Cannot write the manifest <tmp>/manifest.json: {REASON}",
    ),
    Row(
        "deployer.pipeline._execute",
        "deployer.pipeline._prepare",
        deploy_run("--all"),
        (1, printed((f"ERROR: Could not prepare the deployment: {REASON}", DEBUG_HINT))),
        "preparing",
    ),
    Row(
        "deployer.pipeline._execute",
        "deployer.pipeline._claim",
        deploy_run("--all"),
        (1, printed(FOUND, (f"ERROR: Could not prepare the deployment: {REASON}", DEBUG_HINT))),
        "claiming",
    ),
    Row(
        "deployer.pipeline._dry_run",
        "deployer.plan.build",
        deploy_run("--all", "--dry-run"),
        (
            1,
            printed(
                FOUND,
                ("Selected 1 item (1 skill):", "  alpha"),
                ("Rendered and validated in memory (dry run).",),
                (f"ERROR: Could not finish the dry run: {REASON}", DEBUG_HINT),
            ),
        ),
    ),
    Row(
        "deployer.source.discover",
        "deployer.source._discover_directories",
        deploy_run("--all", "--dry-run"),
        (
            1,
            printed(
                (
                    f"ERROR: Could not read the skill source: {REASON}",
                    "Check that this checkout is complete and that you can read it, then retry.",
                    DEBUG_HINT,
                )
            ),
        ),
    ),
    Row(
        "deployer.verify.execute",
        "deployer.verify._verify",
        deploy_run("verify", deploy_first=True),
        (1, printed((f"ERROR: Could not finish verifying the adapters: {REASON}", DEBUG_HINT))),
    ),
    Row(
        "audit_ai_config.main",
        "audit_ai_config.format_json",
        _audit_ai_config,
        (1, f"FAILED the JSON report cannot hold a value the audit found: {REASON}\n", ""),
    ),
    Row(
        "mcp_handshake.Session.send",
        DUMPS,
        lambda _: raised(RuntimeError, lambda: mcp_handshake.Session(mock.Mock()).send({"method": "initialize"})),
        f"RuntimeError: cannot send the server's value back: {REASON}",
    ),
    Row(
        "mcp_handshake.handshake",
        "mcp_handshake._initialize_and_list",
        _handshake(lambda *_, **__: _running_server()),
        f"RuntimeError: could not talk to the server: {REASON}",
        "talking",
    ),
    Row(
        "mcp_handshake.handshake",
        "mcp_handshake.streaming",
        _handshake(None),
        f"RuntimeError: could not start: {REASON}",
        "starting",
    ),
    Row(
        "memory_audit.audit_lines",
        DUMPS,
        _memory_audit,
        (1, [f"FAILED the audit report cannot be written: {REASON}"]),
    ),
    Row("review_config.load_config", "review_config.validate_config", _load_config, f"ConfigurationError: {REASON}"),
    Row(
        "hook_decision.denial",
        "hook_decision._denial_text",
        lambda _: hook_decision.denial("refused", "fixed"),
        '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", '
        '"permissionDecisionReason": "fixed"}}',
        once=True,
    ),
    Row("review_guard._log_read", DUMPS, _log_read, ""),
    Row(
        "review_io.atomic_write_json",
        DUMPS,
        _atomic_write_json,
        (f"PersistenceError: Cannot write <tmp>/record.json: {REASON}", False),
    ),
    Row("review_run.EXPECTED_ERRORS", DUMPS, _expected_errors_of_the_pipeline, (1, f"FAILED {REASON}\n", "")),
    Row(
        "review_runtime.verify_github_tarball",
        "review_runtime._listed_blobs",
        _tarball,
        f"RuntimeContractError: GitHub's tarball of owner/name@0123456789ab cannot be checked: {REASON}; set "
        "checkout_path for this repository to snapshot it from git objects",
    ),
    Row("review_source._count", DUMPS, _count, ""),
    Row("review_source.main", "review_source.source_file", _source_main, (1, f"FAILED {REASON}\n", "")),
    Row(
        "flag_review_finding.main",
        "flag_review_finding.run",
        lambda _: ended(lambda: flag_review_finding.main(["list"])),
        (1, f"FAILED {REASON}\n", ""),
    ),
    Row(
        "review_document.write_fixture",
        DUMPS,
        _write_fixture,
        (f"DocumentError: notes.md cannot be held in a fixture: {REASON}", False),
    ),
    Row(
        "review_insights.EXPECTED_ERRORS",
        "review_insights.working_scope",
        lambda _: ended(lambda: review_insights.main(["scope"], services=mock.Mock())),
        (1, f"FAILED {REASON}\n", ""),
    ),
    Row(
        "review_insights._resolve_recorded",
        "review_insights.resolve_flag",
        _resolve_recorded,
        (
            f"InsightError: The decision is recorded in <tmp>/insights.json, but flags F1 could not be resolved: "
            f"{REASON}; decide again to resolve them",
            [{"resolved_flags": []}],
            1,
        ),
    ),
    Row(
        "dashboard_pipeline.EXPECTED_ERRORS",
        "dashboard_pipeline.update",
        lambda folder: ended(lambda: dashboard_main(["update", "--input", str(folder / "input.json")], mock.Mock())),
        (1, f"FAILED {REASON}\n", ""),
    ),
    Row("audit_repository.write_json", DUMPS, _write_json, (f"OSError: {REASON}", False)),
    Row(
        "audit_repository.write_briefs",
        DUMPS,
        _write_briefs,
        f"AuditError: cannot write the briefs marker <tmp>/briefs/{audit_repository.BRIEFS_MARKER}: {REASON}",
    ),
)


def _id(row: Row) -> str:
    return f"{row.place} {row.label}".strip()


@pytest.mark.parametrize("row", REFUSALS, ids=_id)
def test_each_place_ends_a_refused_write_as_its_contract_says(row: Row, tmp_path: Path) -> None:
    refusal = JsonWriteRefused(REASON)
    effect: object = [refusal, mock.DEFAULT] if row.once else refusal
    with mock.patch(row.step, side_effect=effect, wraps=pkgutil.resolve_name(row.step)) as step:
        outcome = row.drive(tmp_path)
    assert step.called, f"{row.place} never reached {row.step}"
    assert outcome == row.outcome


def test_every_place_that_catches_the_refusal_has_a_row_and_every_row_a_place() -> None:
    places = set(refusal_scan.repository_places())
    rows = {row.place for row in REFUSALS}
    assert sorted(places - rows) == [], "add a row to REFUSALS for each place that catches the refusal"
    assert sorted(rows - places) == [], "remove each row whose place no longer catches the refusal"


def test_the_scan_finds_each_spelling_of_the_refusal_and_nothing_else() -> None:
    source = (
        "import json_writer\n"
        "from json_writer import JsonWriteRefused as Refused\n"
        "ERRORS = (OSError, json_writer.JsonWriteRefused)\n"
        "OTHER = (OSError,)\n\n\n"
        "def caught():\n    try:\n        pass\n    except Refused:\n        pass\n\n\n"
        "def family():\n    try:\n        pass\n    except json_writer.CONTENT_ERRORS:\n        pass\n\n\n"
        "def written():\n    return json_writer.dumps({})\n\n\n"
        "class Holder:\n    def method(self):\n"
        "        with contextlib.suppress(json_writer.JsonWriteRefused):\n            pass\n"
    )
    assert refusal_scan.catching_places("module", source) == [
        "module.ERRORS",
        "module.caught",
        "module.family",
        "module.Holder.method",
    ]
    assert refusal_scan.catching_places("module", "def unrelated():\n    raise JsonWriteRefused\n") == []
