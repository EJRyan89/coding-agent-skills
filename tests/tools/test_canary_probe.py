"""Regression suite for the runtime canary's fixture probe, tests/fixtures/runtime-canary's canary_probe.py.

Each case runs the real probe where a canary deploys it, in the runtime canary's home or in the plugin canary's
installed copy, and reads what it wrote through the canary's own reader.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from tools import plugin_canary, runtime_canary
from tools.runtime_canary import FIXTURE_SKILL, FIXTURE_SOURCE, RUN_ID, STATE, Completed


def run_probe(script: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    """The probe run as a runtime's shell runs it, with the arguments the canary's prompt asks for."""
    return subprocess.run(
        [sys.executable, "-B", str(script), "--help"],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def deployed_probe(home: Path) -> Path:
    """The probe the runtime canary deploys into its home, with the home's state folder made as the canary makes it."""
    assert runtime_canary.deploy(home, FIXTURE_SOURCE)[0] == 0
    (home / STATE).mkdir()
    return home / ".claude" / "skills" / FIXTURE_SKILL / "scripts" / "canary_probe.py"


def test_in_the_runtime_canarys_home_it_records_its_copy_and_the_run(tmp_path: Path) -> None:
    home = tmp_path / "Canary Home"
    home.mkdir()
    script = deployed_probe(home)
    (home / STATE / RUN_ID).write_text("run one\n", encoding="utf-8")
    elsewhere = tmp_path / "Somewhere Else"
    elsewhere.mkdir()
    process = run_probe(script, elsewhere)
    assert (process.returncode, process.stdout) == (0, f"RUNTIME_CANARY_PROBE RAN {script.as_posix()}\n")
    records = runtime_canary.read_records(home / STATE / "probe.jsonl")
    # The record holds what a canary reads, and nothing else.
    assert records == [{"script": script.as_posix(), "cwd": elsewhere.as_posix(), "run": "run one"}]
    assert runtime_canary.marked(records, "run one") == records
    assert runtime_canary.marked(records, "another run") == []


def test_in_the_plugin_canarys_installed_copy_it_records_beside_the_copy(tmp_path: Path) -> None:
    built = tmp_path / "built"
    built.mkdir()
    plugin = plugin_canary.build(built, [FIXTURE_SOURCE])[0]
    installed = tmp_path / "cache" / plugin.name / "0.0.0-dev"
    shutil.copytree(built / plugin.name, installed)
    (tmp_path / STATE).mkdir()
    script = installed / "skills" / FIXTURE_SKILL / "scripts" / "canary_probe.py"

    def shell(arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Completed:
        process = run_probe(script, cwd)
        return Completed(process.returncode, json.dumps({"stdout": process.stdout}) + "\n", process.stderr)

    session = plugin_canary.Session(tmp_path, shell, {}, 30)
    lines = plugin_canary.probe(session, "claude", {plugin.name: str(installed)}, plugin.name)
    assert lines[0] == (
        f'RUNTIME claude {plugin.name}:{FIXTURE_SKILL} RAN "{script.resolve().as_posix()}" "{tmp_path.as_posix()}"'
    )


def test_with_no_run_identifier_it_writes_nothing_and_exits_1(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    script = deployed_probe(home)
    identifier = (home / STATE / RUN_ID).as_posix()
    for written, reason in ((None, "cannot read the run identifier in"), ("\n", "is empty")):
        if written is not None:
            (home / STATE / RUN_ID).write_text(written, encoding="utf-8")
        process = run_probe(script, home)
        assert process.returncode == 1, process
        assert process.stdout == ""
        assert process.stderr.startswith("FAILED "), process.stderr
        assert reason in process.stderr
        assert identifier in process.stderr
        assert not (home / STATE / "probe.jsonl").exists()


def test_a_canary_run_whose_probe_found_no_identifier_is_never_a_denied_write(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    script = deployed_probe(home)
    log = home / STATE / "scripts" / "codex-probe.jsonl"
    log.parent.mkdir()
    (home / STATE / "recorder").mkdir()
    (home / STATE / "recorder" / "sitecustomize.py").write_text(runtime_canary.RECORDER, encoding="utf-8")
    environment = runtime_canary.environment(dict(os.environ), log, home)
    process = subprocess.run(
        [sys.executable, "-B", str(script), "--help"], cwd=home, env=environment, capture_output=True, check=False
    )
    assert process.returncode == 1
    reason = runtime_canary.verdict(
        FIXTURE_SKILL,
        home,
        probes=[],
        started=runtime_canary.read_records(log),
        completed=Completed(0, "", ""),
        denials=[],
        timeout=60,
    )
    # The recorder sees a Python script end from 3.12; before it, the run reads as one that wrote no marker.
    if sys.version_info >= (3, 12):
        assert reason == f'FAILED "ran {script.as_posix()} but it exited with code 1"'
    else:
        assert reason == f'FAILED "ran {script.as_posix()} but it wrote no marker; was the write denied?"'
