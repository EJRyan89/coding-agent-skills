"""The one test of whether a file is an AppleDouble sidecar macOS wrote, for every walk of a folder a user names.

On a volume with no room for a file's extended attributes, such as FAT, exFAT, or an SMB share, macOS keeps them in a
sidecar named `._<name>` beside the file or folder `<name>`, and it writes one whenever it sets an attribute, as it does
on every file a process writes there. A sidecar opens with the AppleDouble header, eight bytes, big-endian: the magic
number 0x00051607 (`00 05 16 07`), then the version 0x00020000 (`00 02 00 00`).

`._<name>` is a legal name on every platform, so a name alone never decides it. `is_sidecar` takes a file for a sidecar
only when its folder's listing also holds `<name>` and the file opens with the whole header. One that cannot be opened
or read, or is shorter than the header, is not taken for one: it stays the caller's file, whose own reader reports what
it cannot read.
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

PREFIX = "._"
HEADER = b"\x00\x05\x16\x07\x00\x02\x00\x00"

# The presence reads here that read a file they cannot open or read as no sidecar, each with why that is the answer
# it should give.
PRESENCE_ALLOWED = {
    "is_sidecar": "a file whose header cannot be read is kept as the caller's file, never dropped, so the caller's own "
    "read of it reports the failure",
}


def is_sidecar(path: Path, names: Collection[str]) -> bool:
    """Whether the regular file `path` is an AppleDouble sidecar: its name is `._<name>`, `names`, its folder's
    listing, holds `<name>`, and it opens with `HEADER`. Only a file whose name passes is opened."""
    if not path.name.startswith(PREFIX) or path.name.removeprefix(PREFIX) not in names:
        return False
    try:
        with path.open("rb") as handle:
            return handle.read(len(HEADER)) == HEADER
    except OSError:
        return False
