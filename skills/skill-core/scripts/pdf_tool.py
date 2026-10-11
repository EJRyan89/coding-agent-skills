"""The shared maintained-release support policy for the native PDF text extractor, not a sandbox."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from bounded_process import streaming

# Vendor evidence and the support-policy rationale are recorded in docs/design.md.
MINIMUMS = {"Xpdf": (4, 6), "Poppler": (26, 10, 0)}
VERSION_ARGUMENTS = ("-v",)
PROBE_SECONDS = 5.0
MAX_PROBE_BYTES = 16 * 1024
BANNER = re.compile(r"pdftotext version ([0-9]{1,4}\.[0-9]{1,4}(?:\.[0-9]{1,4})?)( \[www\.xpdfreader\.com\])?")
COPYRIGHT = re.compile(r"Copyright [0-9, -]+ (Glyph & Cog, LLC|The Poppler Developers)(?: - https?://\S+)?")


def version_text(version: tuple[int, ...]) -> str:
    return ".".join(map(str, version))


def requirements() -> str:
    return " or ".join(f"{provider} {version_text(floor)} or newer" for provider, floor in MINIMUMS.items())


@dataclass(frozen=True)
class PdfIdentity:
    provider: str
    version: tuple[int, ...]

    @property
    def minimum(self) -> tuple[int, ...]:
        return MINIMUMS[self.provider]

    @property
    def outdated(self) -> bool:
        return self.version < self.minimum


class PdfToolRefused(ValueError):
    """A content-free refusal, optionally carrying the identified unsupported provider for reporting."""

    def __init__(self, reason: str, identity: PdfIdentity | None = None) -> None:
        super().__init__(reason)
        self.identity = identity


def parse_banner(text: str, status: int) -> PdfIdentity:
    """Identify only an unambiguous vendor version banner, then enforce that vendor's own floor and status."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    banner = BANNER.fullmatch(lines[0]) if lines else None
    copyrights = [COPYRIGHT.fullmatch(line) for line in lines[1:]]
    if banner is None or not copyrights or any(match is None for match in copyrights):
        raise PdfToolRefused(f"pdftotext provider/version is unidentifiable; install {requirements()}")
    vendors = {match.group(1) for match in copyrights if match is not None}
    poppler = "The Poppler Developers" in vendors
    if poppler and banner.group(2):
        raise PdfToolRefused(f"pdftotext provider/version is ambiguous; install {requirements()}")
    provider = "Poppler" if poppler else "Xpdf"
    version = tuple(map(int, banner.group(1).split(".")))
    if provider == "Poppler" and (len(version) != 3 or (version[0] >= 20 and not 1 <= version[1] <= 12)):
        raise PdfToolRefused(f"pdftotext provider/version is malformed; install {requirements()}")
    identity = PdfIdentity(provider, version)
    statuses = {0} if poppler else {0, 99}
    if status not in statuses:
        raise PdfToolRefused(f"pdftotext version probe failed (exit status {status}); install {requirements()}")
    if identity.outdated:
        raise PdfToolRefused(
            f"pdftotext {provider} {version_text(version)} is unsupported; "
            f"upgrade to {provider} {version_text(identity.minimum)} or newer",
            identity,
        )
    return identity


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
                raise PdfToolRefused(f"pdftotext version probe output exceeds its limit; install {requirements()}")
            status = running.wait()
    except subprocess.TimeoutExpired as exc:
        raise PdfToolRefused(
            f"pdftotext version probe did not finish within {PROBE_SECONDS:g} seconds; install {requirements()}"
        ) from exc
    except OSError as exc:
        raise PdfToolRefused(f"pdftotext version probe could not be started; install {requirements()}") from exc
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PdfToolRefused(f"pdftotext version probe is not UTF-8; install {requirements()}") from exc
    return parse_banner(text, status)
