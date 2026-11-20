"""The one resolver of a Markdown link's destination to the repository path it leads to.

A link's destination, as ejryan89-skill-core's `link_destinations` reads it into `Destination.posix_path`, is resolved
as GitHub resolves a link in a file it renders and as Claude Code reads one in a skill: against the folder of the file
that holds the link, and, when it starts with `/`, from the repository root. So `references/rules.md` and
`./references/rules.md` in `.claude/skills/review/SKILL.md` both lead to `.claude/skills/review/references/rules.md`,
`../shared.md` to `.claude/skills/shared.md`, and `/docs/rules.md` to `docs/rules.md`. A destination that is only a
fragment leads to the linking file itself.

Only a link's destination has a folder to resolve from. A path written bare in prose or in a code span is a reader's own
reading, such as a review skill's file named from the repository root its reviewer works in, and is not resolved here.
"""

from __future__ import annotations

import posixpath


def link_target(linking_file: str, destination: str) -> str | None:
    """The repository path a link's `destination` leads to from `linking_file`, both `/`-separated repository paths, or
    None when it climbs out of the repository with `..` or names the repository root itself.

    `destination` is a path, not a URL: a caller passes no destination `Destination.is_url` reports, so a leading `//`,
    a network path, never reaches here, and any leading `/` is read as the repository root.
    """
    if not destination:
        return linking_file or None
    base = "" if destination.startswith("/") else posixpath.dirname(linking_file)
    path = posixpath.normpath(posixpath.join(base, destination.lstrip("/")))
    if path in (".", "..") or path.startswith("../"):
        return None
    return path
