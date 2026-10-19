"""The shared maintained-release support policy for the native PDF text extractor, not a sandbox.

`deploy.py check` and every extraction choose the parser alike: of every `pdftotext` skill-core's locator finds, in
its search order, the first whose provider and version the probe accepts. Each one passed over is named, with what
the probe found and where, so a parser that comes first only because of where a shell puts it, such as the old Xpdf
Git for Windows bundles in Git Bash's `/mingw64/bin`, is reported as skipped rather than blocking a supported one
further down. The floors bend for no platform: on Linux a distribution's `pdftotext` is refused like any other below its
floor, and the refusal points to the Xpdf command-line tools.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, assert_never

from bounded_process import streaming

# The vendors whose pdftotext the probe accepts.
PdfProvider = Literal["Xpdf", "Poppler"]
# Vendor evidence and the support-policy rationale are recorded in docs/design.md.
MINIMUMS: dict[PdfProvider, tuple[int, ...]] = {"Xpdf": (4, 6), "Poppler": (26, 10, 0)}
VERSION_ARGUMENTS = ("-v",)
PROBE_SECONDS = 5.0
MAX_PROBE_BYTES = 16 * 1024
BANNER = re.compile(r"pdftotext version ([0-9]{1,4}\.[0-9]{1,4}(?:\.[0-9]{1,4})?)( \[www\.xpdfreader\.com\])?")
COPYRIGHT = re.compile(r"Copyright [0-9, -]+ (Glyph & Cog, LLC|The Poppler Developers)(?: - https?://\S+)?")


def version_text(provider: PdfProvider, version: tuple[int, ...]) -> str:
    """A version as its vendor numbers it: Xpdf's minor and Poppler's month, since its year-numbered releases, in two
    digits, as in Xpdf 4.06 and Poppler 26.09.0; the comparison stays numeric."""
    match provider:
        case "Xpdf":
            two_digit_minor = True
        case "Poppler":
            two_digit_minor = version[0] >= 20
        case _:
            assert_never(provider)
    parts = [str(part) for part in version]
    if two_digit_minor and len(version) > 1:
        parts[1] = f"{version[1]:02d}"
    return ".".join(parts)


def requirements() -> str:
    return " or ".join(f"{provider} {version_text(provider, floor)} or newer" for provider, floor in MINIMUMS.items())


# What a refusal and `deploy.py check` add to the floors on this platform: nothing on Windows, and on Linux, where a
# distribution's Poppler keeps its upstream version while it backports fixes, so its pdftotext stays below Poppler's
# floor, the Xpdf command-line tools Xpdf publishes for Linux, which meet Xpdf's.
INSTALL_HINT = (
    None
    if sys.platform == "win32"
    else (
        "a distribution's Poppler keeps a version below its floor as it backports fixes, so install the Xpdf "
        "command-line tools from https://www.xpdfreader.com/download.html"
    )
)


def install_text() -> str:
    """What a refusal tells the user to install: each provider's floor, then this platform's hint, if any."""
    floors = f"install {requirements()}"
    return floors if INSTALL_HINT is None else f"{floors}; {INSTALL_HINT}"


@dataclass(frozen=True)
class PdfIdentity:
    provider: PdfProvider
    version: tuple[int, ...]

    @property
    def minimum(self) -> tuple[int, ...]:
        return MINIMUMS[self.provider]

    @property
    def outdated(self) -> bool:
        return self.version < self.minimum

    @property
    def version_text(self) -> str:
        return version_text(self.provider, self.version)


class PdfToolRefused(ValueError):
    """A content-free refusal, optionally carrying the identified unsupported provider for reporting.

    The probe of one parser raises it with what it found, such as `Xpdf 4.00, older than Xpdf 4.06`, and the choice
    among them with the whole reason, which names each parser passed over and what to install.
    """

    def __init__(self, reason: str, identity: PdfIdentity | None = None) -> None:
        super().__init__(reason)
        self.identity = identity


def parse_banner(text: str, status: int) -> PdfIdentity:
    """Identify only an unambiguous vendor version banner, then enforce that vendor's own floor and status."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    banner = BANNER.fullmatch(lines[0]) if lines else None
    copyrights = [COPYRIGHT.fullmatch(line) for line in lines[1:]]
    if banner is None or not copyrights or any(match is None for match in copyrights):
        raise PdfToolRefused("provider/version is unidentifiable")
    vendors = {match.group(1) for match in copyrights if match is not None}
    poppler = "The Poppler Developers" in vendors
    if poppler and banner.group(2):
        raise PdfToolRefused("provider/version is ambiguous")
    provider: PdfProvider = "Poppler" if poppler else "Xpdf"
    version = tuple(map(int, banner.group(1).split(".")))
    if provider == "Poppler" and (len(version) != 3 or (version[0] >= 20 and not 1 <= version[1] <= 12)):
        raise PdfToolRefused("provider/version is malformed")
    identity = PdfIdentity(provider, version)
    statuses = {0} if poppler else {0, 99}
    if status not in statuses:
        raise PdfToolRefused(f"version probe failed with exit status {status}")
    if identity.outdated:
        raise PdfToolRefused(
            f"{provider} {identity.version_text}, older than {provider} {version_text(provider, identity.minimum)}",
            identity,
        )
    return identity


# The functions that start a command whose program tests/run_validation.py cannot read here, with the reason.
COMMANDS_ALLOWED = {
    "probe_pdftotext": "probes a pdftotext path its caller found; deployer/tools.py and code-review-core's "
    "review_documents.py look each up by name through executable_locator, and the policy reads the program there",
}


def probe_pdftotext(path: str) -> PdfIdentity:
    """Check this exact resolved path with no document or stdin, bounded combined output, and a deadline."""
    try:
        with streaming(
            [path, *VERSION_ARGUMENTS],
            idle_timeout=PROBE_SECONDS,
            timeout=PROBE_SECONDS,
            exit_wait=1.0,
            merge_stderr=True,
        ) as running:
            running.stdin.close()
            output = running.read(MAX_PROBE_BYTES + 1)
            if len(output) > MAX_PROBE_BYTES:
                raise PdfToolRefused("version probe output exceeds its limit")
            status = running.wait()
    except subprocess.TimeoutExpired as exc:
        raise PdfToolRefused(f"version probe did not finish within {PROBE_SECONDS:g} seconds") from exc
    except OSError as exc:
        raise PdfToolRefused("version probe could not be started") from exc
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PdfToolRefused("version probe output is not UTF-8") from exc
    return parse_banner(text, status)


@dataclass(frozen=True)
class Skipped:
    """A pdftotext passed over: where the locator found it, what its probe found, and its identity, when known."""

    path: str
    finding: str
    identity: PdfIdentity | None = None

    def __str__(self) -> str:
        return f"{self.path} ({self.finding})"


def skipped_text(skipped: Iterable[Skipped]) -> str:
    """The parsers passed over, as `deploy.py check` and a refusal name them."""
    return "skipped " + ", ".join(str(item) for item in skipped)


@dataclass(frozen=True)
class PdfSelection:
    """The parser chosen: its resolved path, which extraction runs, its identity, and every one passed over first."""

    path: str
    identity: PdfIdentity
    skipped: tuple[Skipped, ...] = ()


def select_pdftotext(found: Iterable[str], probe: Callable[[str], PdfIdentity] = probe_pdftotext) -> PdfSelection:
    """The first of `found`, in order, whose resolved path `probe` accepts. Raises PdfToolRefused naming every one
    passed over, with the first identified one's identity, when none is accepted; `found` is never empty, since a
    caller reports a parser that is not installed itself."""
    skipped: list[Skipped] = []
    for candidate in found:
        try:
            resolved = str(Path(candidate).resolve())
        except OSError:
            skipped.append(Skipped(candidate, "could not be accessed"))
            continue
        try:
            return PdfSelection(resolved, probe(resolved), tuple(skipped))
        except PdfToolRefused as exc:
            skipped.append(Skipped(candidate, str(exc), exc.identity))
    if not skipped:
        raise ValueError("select_pdftotext needs at least one pdftotext to choose from")
    identity = next((item.identity for item in skipped if item.identity is not None), None)
    raise PdfToolRefused(f"no supported pdftotext: {skipped_text(skipped)}; {install_text()}", identity)
