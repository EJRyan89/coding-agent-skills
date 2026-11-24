"""The one reader of a GitHub repository from a git remote's URL, and the one spelling of the characters a
repository's owner and name hold.

A checkout's origin names its GitHub repository in one of the forms git writes for github.com: HTTPS
(`https://github.com/<owner>/<repo>`, with a user before the host or not), scp-like SSH
(`git@github.com:<owner>/<repo>`), or an SSH URL (`ssh://git@github.com/<owner>/<repo>`, with a port or not), each
with or without `.git` and a trailing `/`. The whole URL must be one of them, so a URL on another host that holds
`github.com/<owner>/<repo>` in its path is not a GitHub remote. A user before the host holds no `/`, `?`, or `#`,
since each ends a URL's authority as git and `urllib.parse.urlsplit` read it: in `https://example.com#@github.com/o/r`
the host is `example.com`, and what follows the `#` is its fragment, never a user and a host.
"""

from __future__ import annotations

import re

# The characters a repository's owner and name are made of: letters, digits, `_`, `.`, and `-`, as a character class's
# body. Every reader of an `owner/name` here builds on it, so the readers accept one set of characters; a reader with a
# narrower rule, as the review core's repository identity reads the owner by GitHub's login rule and refuses a name
# made only of dots, builds that rule on this class.
REPOSITORY_CHARACTERS = "A-Za-z0-9_.-"
# An owner and a name, each a run of REPOSITORY_CHARACTERS, as a pattern another may embed.
OWNER_AND_NAME = rf"[{REPOSITORY_CHARACTERS}]+/[{REPOSITORY_CHARACTERS}]+"
GITHUB_REMOTE = re.compile(
    r"(?:https?://(?:[^@/?#]+@)?github\.com/|ssh://git@github\.com(?::\d+)?/|git@github\.com:)"
    # The name's run is lazy, so a `.git` the URL ends with is left to the suffix after it.
    rf"({OWNER_AND_NAME}?)(?:\.git)?/?",
    re.IGNORECASE,
)


def github_repository(url: str) -> str | None:
    """`owner/name` as the remote's URL writes it, or None when the URL, its surrounding whitespace aside, is not one
    of the github.com forms."""
    match = GITHUB_REMOTE.fullmatch(url.strip())
    return match.group(1) if match else None
