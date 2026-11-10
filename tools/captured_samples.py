"""The real output of other programs captured under tests/fixtures/captured-output, which the suites hold this
repository's readers of that output to, and which of them a newer release of their program has left behind.

tests/fixtures/captured-output/provenance.json names each sample by its path under that folder, with `versions`, the
version of each runtime whose output it holds, by its name in a skill's runtime support declaration, `captured`, the
date, and `how`, the command or page it came from and what was cut or replaced, such as a path written `<temp>`.
The weekly watches report a sample whose runtime has moved past its recorded version, as a SAMPLE_BEHIND line, so the
sample is captured again rather than left to age: tools/runtime_changelogs.py against each changelog's latest release,
and tools/runtime_command_lines.py against the version the installed runtime prints.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

import calendar_date
import json_reader
import versions

CAPTURED = Path("tests") / "fixtures" / "captured-output"
PROVENANCE = CAPTURED / "provenance.json"


class SampleError(Exception):
    """The provenance record cannot be read, or names a sample in a shape this reader does not know."""


@dataclass(frozen=True)
class Sample:
    path: str
    versions: Mapping[str, tuple[int, ...]]
    captured: str
    how: str


def read(top: Path, runtimes: tuple[str, ...]) -> list[Sample]:
    """Every sample the record at `top` names, each with a version of one or more of `runtimes`, or SampleError."""
    try:
        record = json_reader.read_file(top / PROVENANCE)
    # ValueError covers json_reader's whole family: its refusals, nesting too deep included, and the decode errors
    except (OSError, ValueError) as exc:
        raise SampleError(f"cannot read {PROVENANCE.as_posix()}: {exc}") from exc
    samples = record.get("samples") if isinstance(record, dict) else None
    if not isinstance(samples, dict) or not samples:
        raise SampleError(f"{PROVENANCE.as_posix()} names no sample")
    return [sample(path, entry, runtimes) for path, entry in samples.items()]


def sample(path: str, entry: object, runtimes: tuple[str, ...]) -> Sample:
    if not isinstance(entry, dict) or set(entry) != {"versions", "captured", "how"}:
        raise SampleError(f"{path}: an entry holds exactly versions, captured, and how")
    recorded, captured, how = entry["versions"], entry["captured"], entry["how"]
    if not isinstance(recorded, dict) or not recorded:
        raise SampleError(f"{path}: versions names no runtime")
    read: dict[str, versions.Version] = {}
    for runtime, version in recorded.items():
        number = versions.read(version, least=3, most=3) if isinstance(version, str) else None
        if runtime not in runtimes or number is None:
            raise SampleError(f"{path}: versions maps a runtime of {', '.join(runtimes)} to a version, not {runtime}")
        read[runtime] = number
    if not isinstance(captured, str) or calendar_date.parse(captured) is None:
        raise SampleError(f"{path}: captured is a date, YYYY-MM-DD")
    if not isinstance(how, str) or not how.strip():
        raise SampleError(f"{path}: how says how the sample was captured")
    return Sample(path, read, captured, how)


def behind(samples: list[Sample], runtime: str, version: tuple[int, ...], label: str | None = None) -> list[str]:
    """SAMPLE_BEHIND <label> <sample> <recorded> <version> for each sample of `runtime` older than `version`, the
    runtime named by `label` where the caller names it otherwise."""
    lines = []
    for item in samples:
        recorded = item.versions.get(runtime)
        if recorded is not None and recorded < version:
            lines.append(
                f"SAMPLE_BEHIND {label or runtime} {item.path} {versions.text(recorded)} {versions.text(version)}"
            )
    return lines
