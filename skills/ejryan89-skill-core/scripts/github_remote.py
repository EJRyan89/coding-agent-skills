"""The one reader of a GitHub repository from a git remote's URL.

A checkout's origin names its GitHub repository in one of the forms git writes for github.com: HTTPS
(`https://github.com/<owner>/<repo>`, with a user before the host or not), scp-like SSH
(`git@github.com:<owner>/<repo>`), or an SSH URL (`ssh://git@github.com/<owner>/<repo>`, with a port or not), each
with or without `.git` and a trailing `/`. The whole URL must be one of them, so a URL on another host that holds
`github.com/<owner>/<repo>` in its path is not a GitHub remote.
"""

from __future__ import annotations

import re

GITHUB_REMOTE = re.compile(
    r"(?:https?://(?:[^@/]+@)?github\.com/|ssh://git@github\.com(?::\d+)?/|git@github\.com:)"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?",
    re.IGNORECASE,
)


def github_repository(url: str) -> str | None:
    """`owner/name` as the remote's URL writes it, or None when the URL, its surrounding whitespace aside, is not one
    of the github.com forms."""
    match = GITHUB_REMOTE.fullmatch(url.strip())
    return match.group(1) if match else None
