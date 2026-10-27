"""Tests for tools/captured_samples.py: every captured sample has its provenance, holds no machine's paths, and is
reported once a newer release of its runtime leaves it behind."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from tools import captured_samples
from tools.captured_samples import Sample, SampleError

REPOSITORY = Path(__file__).resolve().parents[2]
CAPTURED = REPOSITORY / "tests" / "fixtures" / "captured-output"
PROVENANCE = "provenance.json"
RUNTIMES = ("claude-code", "codex", "copilot-cli")
# A path into a real profile or home, as a capture names it before its folders are written <temp> and <home>.
MACHINE_PATHS = re.compile(r"[A-Za-z]:[\\/]+(?:Users|Documents and Settings)\b|(?<![\w.<>])/(?:home|Users)/\w")


def recorded() -> dict[str, dict[str, object]]:
    samples: dict[str, dict[str, object]] = json.loads((CAPTURED / PROVENANCE).read_text(encoding="utf-8"))["samples"]
    return samples


def test_every_sample_has_its_provenance_and_every_provenance_its_sample() -> None:
    files = {path.relative_to(CAPTURED).as_posix() for path in CAPTURED.rglob("*") if path.is_file()}
    assert files - {PROVENANCE} == set(recorded())


def test_the_record_is_read_whole() -> None:
    samples = captured_samples.read(REPOSITORY, RUNTIMES)
    assert {sample.path for sample in samples} == set(recorded())
    copilot = next(sample for sample in samples if sample.path == "copilot/skill-list.json")
    assert copilot.versions == {"copilot-cli": (1, 0, 103)}
    entry = recorded()["copilot/skill-list.json"]
    assert (copilot.captured, copilot.how) == (entry["captured"], entry["how"])


@pytest.mark.parametrize("sample", sorted(recorded()))
def test_no_sample_names_a_machines_profile_or_home(sample: str) -> None:
    text = (CAPTURED / sample).read_text(encoding="utf-8")
    assert MACHINE_PATHS.findall(text) == []


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("C:\\Users\\someone\\x", id="windows-profile"),
        pytest.param('"C:\\\\Users\\\\someone"', id="windows-profile-in-json"),
        pytest.param("/home/someone/x", id="linux-home"),
        pytest.param("/Users/someone/x", id="mac-home"),
    ],
)
def test_a_machines_profile_or_home_is_found(text: str) -> None:
    assert MACHINE_PATHS.findall(text)


@pytest.mark.parametrize(
    "text", [pytest.param("<home>\\.agents\\skills", id="placeholder"), pytest.param("~/.agents", id="tilde")]
)
def test_a_placeholder_is_no_machines_path(text: str) -> None:
    assert MACHINE_PATHS.findall(text) == []


def write_record(top: Path, record: object) -> Path:
    path = top / "tests" / "fixtures" / "captured-output" / PROVENANCE
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return top


GOOD = {"versions": {"codex": "0.163.0"}, "captured": "2026-10-24", "how": "codex --version"}


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        pytest.param([], "names no sample", id="not-an-object"),
        pytest.param({"samples": {}}, "names no sample", id="no-sample"),
        pytest.param({"samples": {"a": {**GOOD, "extra": 1}}}, "exactly versions, captured, and how", id="extra-key"),
        pytest.param({"samples": {"a": {**GOOD, "versions": {}}}}, "names no runtime", id="no-runtime"),
        pytest.param(
            {"samples": {"a": {**GOOD, "versions": {"gh": "2.0.0"}}}},
            "a: versions maps a runtime of claude-code, codex, copilot-cli to a version, not gh",
            id="unknown-runtime",
        ),
        pytest.param({"samples": {"a": {**GOOD, "versions": {"codex": "0.163"}}}}, "not codex", id="short-version"),
        pytest.param({"samples": {"a": {**GOOD, "captured": "October"}}}, "captured is a date", id="not-a-date"),
        pytest.param({"samples": {"a": {**GOOD, "captured": "2026-02-30"}}}, "captured is a date", id="no-such-day"),
        pytest.param({"samples": {"a": {**GOOD, "captured": "20261024"}}}, "captured is a date", id="basic-format"),
        pytest.param({"samples": {"a": {**GOOD, "how": " "}}}, "how says how", id="no-how"),
    ],
)
def test_a_record_of_another_shape_is_refused(tmp_path: Path, record: object, reason: str) -> None:
    with pytest.raises(SampleError, match=re.escape(reason)):
        captured_samples.read(write_record(tmp_path, record), RUNTIMES)


def test_a_record_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    top = write_record(tmp_path, {})
    (top / "tests" / "fixtures" / "captured-output" / PROVENANCE).write_bytes(b'{"samples": "\xe9"}')
    with pytest.raises(SampleError, match=re.escape("cannot read tests/fixtures/captured-output/provenance.json")):
        captured_samples.read(top, RUNTIMES)


def test_a_record_that_is_not_json_is_refused(tmp_path: Path) -> None:
    top = write_record(tmp_path, {})
    (top / "tests" / "fixtures" / "captured-output" / PROVENANCE).write_text("{", encoding="utf-8")
    with pytest.raises(SampleError, match=re.escape("cannot read tests/fixtures/captured-output/provenance.json")):
        captured_samples.read(top, RUNTIMES)


SAMPLES = [
    Sample("codex/a.txt", {"codex": (0, 163, 0)}, "2026-10-24", "a"),
    Sample("both.json", {"codex": (0, 160, 0), "copilot-cli": (1, 0, 103)}, "2026-10-24", "b"),
    Sample("copilot/c.txt", {"copilot-cli": (1, 0, 100)}, "2026-10-24", "c"),
]


def test_a_sample_older_than_its_runtimes_version_is_behind_and_one_at_it_is_not() -> None:
    assert captured_samples.behind(SAMPLES, "codex", (0, 163, 0)) == ["SAMPLE_BEHIND codex both.json 0.160.0 0.163.0"]
    assert captured_samples.behind(SAMPLES, "copilot-cli", (1, 0, 103), "copilot") == [
        "SAMPLE_BEHIND copilot copilot/c.txt 1.0.100 1.0.103"
    ]
    assert captured_samples.behind(SAMPLES, "claude-code", (9, 0, 0)) == []
    assert captured_samples.behind(SAMPLES, "codex", (0, 159, 0)) == []


def test_the_module_imports_on_its_own_from_any_folder(tmp_path: Path) -> None:
    # Isolated, so nothing but what the module puts on sys.path finds the checkout: the repository root, then
    # ejryan89-skill-core's scripts above it, as the deployer package puts them again when it is imported.
    module = REPOSITORY / "tools" / "captured_samples.py"
    program = f"import json, runpy, sys; runpy.run_path({str(module)!r}); print(json.dumps(sys.path[:3]))"
    done = subprocess.run(
        [sys.executable, "-I", "-c", program],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert done.returncode == 0, done.stderr
    core = REPOSITORY / "skills" / "ejryan89-skill-core" / "scripts"
    found = [os.path.normcase(entry) for entry in json.loads(done.stdout)]
    assert found == [os.path.normcase(str(path)) for path in (core, core, REPOSITORY)]
