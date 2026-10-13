"""The one reader of a .NET solution's project list, shared by the skills that work on a solution.

A `.sln` lists its projects on `Project("{type}") = "name", "path", "{id}"` lines, and a `.slnx` as the `Path` of
each `Project` element; both write the path with Windows separators, relative to the solution. The readers here take
either, read the file whatever its encoding (Visual Studio writes a `.sln` as UTF-8 with a byte order mark or as
UTF-16), and give the project paths as written, or normalized and absolute, so each skill lists a solution the same
way. A `.slnx` goes through skill-core's `xml_reader`, which refuses a DOCTYPE or an entity before parsing.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from xml_reader import read_xml

SOLUTION_PROJECT = re.compile(r'^\s*Project\("[^"]*"\)\s*=\s*"[^"]*"\s*,\s*"([^"]+)"')
SOLUTION_SUFFIXES = (".sln", ".slnx")


def read_text(path: Path) -> str:
    """Read an MSBuild or solution file whatever its encoding; only ASCII syntax matters to the readers."""
    data = path.read_bytes()
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
    raises `XmlRefused`, so a caller can tell an unreadable solution from one that lists no project."""
    text = read_text(solution)
    if solution.name.casefold().endswith(".slnx"):
        document = read_xml(text)
        return [path for element in document.iter("Project") if (path := element.get("Path"))]
    return [match.group(1) for line in text.splitlines() if (match := SOLUTION_PROJECT.match(line))]


def solution_projects(solution: Path) -> set[str]:
    """Normalized absolute paths of the projects a solution lists; a .slnx that cannot be read raises `XmlRefused`,
    as `read_listed_projects` does, so it is never taken for a solution that lists no project."""
    return {same_path(solution.parent / path.replace("\\", "/")) for path in read_listed_projects(solution)}
