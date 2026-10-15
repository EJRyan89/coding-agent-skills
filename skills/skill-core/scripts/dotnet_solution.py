"""The one reader of a .NET solution's project list, shared by the skills that work on a solution.

A `.sln` lists its projects on `Project("{type}") = "name", "path", "{id}"` lines, and a `.slnx` as the `Path` of
each `Project` element; both write the path with Windows separators, relative to the solution. The readers here take
either, read the file whatever its encoding (Visual Studio writes a `.sln` as UTF-8 with a byte order mark or as
UTF-16), and give the project paths as written, or normalized and absolute, so each skill lists a solution the same
way. A `.slnx` goes through skill-core's `xml_reader`, which refuses a DOCTYPE or an entity before parsing.

The files are a repository's the user did not write, so every read here, and each skill's own read of such a file,
opens only a regular file and refuses one holding more than `READ_LIMIT_BYTES` (`read_bounded`), raising `FileRefused`,
an `OSError` naming the file and the reason.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from xml_reader import read_xml

SOLUTION_PROJECT = re.compile(r'^\s*Project\("[^"]*"\)\s*=\s*"[^"]*"\s*,\s*"([^"]+)"')
SOLUTION_SUFFIXES = (".sln", ".slnx")
# The most bytes a reader reads of one file; a larger file is refused unread, so no file can exhaust the memory a run
# has. A large solution's restore writes the largest file the readers take, its project.assets.json, which stays well
# below it.
READ_LIMIT_BYTES = 64 * 1024 * 1024


class FileRefused(OSError):
    """A file `read_bounded` did not read: `path` and `reason`, which the message joins as `<path> cannot be read:
    <reason>`. It is an `OSError`, so a caller that reports a file it cannot read reports this one the same way."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path} cannot be read: {reason}")
        self.path = path
        self.reason = reason


def read_bounded(path: Path) -> bytes:
    """The bytes of the regular file at `path`, or `FileRefused` when it cannot be read, is not a regular file, such
    as a device, a pipe, or a folder, which this never opens, or holds more than `READ_LIMIT_BYTES`."""
    too_large = f"the file is larger than the reader's bound of {READ_LIMIT_BYTES} bytes, so it is not read"
    try:
        status = path.stat()
        if not stat.S_ISREG(status.st_mode):
            raise FileRefused(path, "not a regular file, so the reader does not open it")
        if status.st_size > READ_LIMIT_BYTES:
            raise FileRefused(path, too_large)
        with path.open("rb") as stream:
            data = stream.read(READ_LIMIT_BYTES + 1)
    except FileRefused:
        raise
    except OSError as error:
        raise FileRefused(path, error.strerror or str(error)) from None
    if len(data) > READ_LIMIT_BYTES:
        raise FileRefused(path, too_large)
    return data


def read_text(path: Path) -> str:
    """Read an MSBuild or solution file whatever its encoding, within `read_bounded`'s bound; only ASCII syntax
    matters to the readers."""
    return decode_text(read_bounded(path))


def decode_text(data: bytes) -> str:
    """The text of an MSBuild or solution file's bytes, UTF-16 by its byte order mark, else UTF-8, so a caller that
    reads the bytes itself, such as within a bound, decodes them as `read_text` does."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def same_path(path: Path) -> str:
    """`path` as the file system compares it: normalized, and case-folded where the platform ignores case."""
    return os.path.normcase(os.path.normpath(str(path)))


def is_solution(path: Path) -> bool:
    return path.name.casefold().endswith(SOLUTION_SUFFIXES)


def read_listed_projects(solution: Path) -> list[str]:
    """The project paths a solution lists, as written: `Project(...) = "name", "path"` lines in a .sln, and the Path
    of each Project element in a .slnx. A .slnx that is not well-formed XML, or declares a DOCTYPE or an entity,
    raises `XmlRefused`, so a caller can tell an unreadable solution from one that lists no project, and a file
    `read_bounded` refuses raises `FileRefused`."""
    return listed_projects(read_text(solution), solution)


def listed_projects(text: str, solution: Path) -> list[str]:
    """The project paths the text of `solution` lists, as written, read as `read_listed_projects` reads the file, for
    a caller that read the text itself."""
    if solution.name.casefold().endswith(".slnx"):
        document = read_xml(text)
        return [path for element in document.iter("Project") if (path := element.get("Path"))]
    return [match.group(1) for line in text.splitlines() if (match := SOLUTION_PROJECT.match(line))]


def solution_projects(solution: Path) -> set[str]:
    """Normalized absolute paths of the projects a solution lists; a .slnx that cannot be read raises `XmlRefused`,
    and a file `read_bounded` refuses `FileRefused`, as `read_listed_projects` does, so it is never taken for a
    solution that lists no project."""
    return {same_path(solution.parent / path.replace("\\", "/")) for path in read_listed_projects(solution)}
