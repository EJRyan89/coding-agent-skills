"""The one list of the names Windows opens as a device rather than a file, for every reader that refuses one.

Windows reserves these names in every folder, in any case, alone or before any extension or colon, and with spaces
before either: `con`, `NUL.txt`, `Com1.tar.gz`, `aux .txt`, `NUL:`, and `CON:stream` each open a device, the colon
being where Windows ends a device name as it ends a drive's. The ports run from 0 to 9 and take the superscript
digits one to three as well, and the console's input and output buffers are `CONIN$` and `CONOUT$`. The deployer
refuses such a name on every platform, so a name deployed on one deploys on each, and a review snapshot refuses a
path with one as a segment, so it never writes to a device.
"""

from __future__ import annotations

RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"{port}{digit}" for port in ("com", "lpt") for digit in "0123456789¹²³"}
)


def is_reserved_device_name(name: str) -> bool:
    """Whether a single name, in any case, is a Windows device name, alone, before an extension or a colon, or before
    spaces and either: its stem ends at the first dot or colon."""
    return name.split(":", 1)[0].split(".", 1)[0].rstrip(" ").casefold() in RESERVED_DEVICE_NAMES
