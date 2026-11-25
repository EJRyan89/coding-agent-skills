"""The real output of other programs captured under tests/fixtures/captured-output, which the suites hold this
repository's readers of that output to, and which of them a newer release of their program has left behind.

tests/fixtures/captured-output/provenance.json names each sample by its path under that folder, with `versions`, the
version of each runtime whose output it holds, by its name in a skill's runtime support declaration, or of each of
PROGRAMS, by the name it is started with, `captured`, the date, and `how`, the command or page it came from and what
was cut or replaced, such as a path written `<temp>`.
The weekly watches report a sample whose runtime has moved past its recorded version, as a SAMPLE_BEHIND line, so the
sample is captured again rather than left to age: tools/runtime_changelogs.py against each changelog's latest release,
and tools/runtime_command_lines.py against the version the installed runtime prints. No watch reads a program's
release, so a sample of gh's or git's output is captured again in the pull request that changes its reader, or that
finds the program's answer has changed its shape.
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
# The programs other than the runtimes whose output a sample may hold, by the name they are started with: a set, since
# validation reads a tuple or list that begins with "gh" or "git" as a command started outside ejryan89-skill-core.
PROGRAMS = frozenset({"gh", "git"})


class SampleError(Exception):
    """The provenance record cannot be read, or names a sample in a shape this reader does not know."""


@dataclass(frozen=True)
class Sample:
    path: str
    versions: Mapping[str, tuple[int, ...]]
    captured: str
    how: str


def read(top: Path, runtimes: tuple[str, ...]) -> list[Sample]:
    """Every sample the record at `top` names, each with a version of one or more of `runtimes` and PROGRAMS, or
    SampleError."""
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
        raise SampleError(f"{path}: versions names no runtime or program")
    known = (*runtimes, *sorted(PROGRAMS))
    read: dict[str, versions.Version] = {}
    for name, version in recorded.items():
        number = versions.read(version, least=3, most=3) if isinstance(version, str) else None
        if name not in known or number is None:
            raise SampleError(f"{path}: versions maps one of {', '.join(known)} to a version, not {name}")
        read[name] = number
    if not isinstance(captured, str) or calendar_date.parse(captured) is None:
        raise SampleError(f"{path}: captured is a date, YYYY-MM-DD")
    if not isinstance(how, str) or not how.strip():
        raise SampleError(f"{path}: how says how the sample was captured")
    return Sample(path, read, captured, how)


@dataclass(frozen=True)
class Behind:
    """A sample whose runtime has moved past its recorded version, with each value a summary reads carried here, never
    read back from the line, since a sample's path may hold a space."""

    label: str
    path: str
    recorded: str
    latest: str

    @property
    def line(self) -> str:
        """SAMPLE_BEHIND <label> <sample> <recorded> <latest>, as the watches print it."""
        return f"SAMPLE_BEHIND {self.label} {self.path} {self.recorded} {self.latest}"


def behind(samples: list[Sample], runtime: str, version: tuple[int, ...], label: str | None = None) -> list[Behind]:
    """Each sample of `runtime` older than `version`, the runtime named by `label` where the caller names it
    otherwise."""
    found = []
    for item in samples:
        recorded = item.versions.get(runtime)
        if recorded is not None and recorded < version:
            found.append(Behind(label or runtime, item.path, versions.text(recorded), versions.text(version)))
    return found
