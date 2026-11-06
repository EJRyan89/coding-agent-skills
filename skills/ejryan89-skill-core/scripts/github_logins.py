"""The one reader of a GitHub login, a user's or an organization's, shared by every skill that takes one.

It holds GitHub's own rule: letters and digits, with single hyphens between them, so no hyphen leads, trails, or
follows another, and at most thirty-nine characters. An Enterprise Managed Users login is the user's handle, an
underscore, and the enterprise's short code, `name_short`, within the same thirty-nine; the underscore appears
nowhere else. A login so read holds no space, colon, quote, or other character a search query or a command line
reads apart, so a script may place one in either.
"""

from __future__ import annotations

import re

MAXIMUM_LENGTH = 39
# The rule above as a pattern another may embed, such as a repository identity's owner before its `/`: the lookahead
# holds the length, since the run of a login's characters it measures ends where the login does.
LOGIN = (
    rf"(?=[A-Za-z0-9_-]{{1,{MAXIMUM_LENGTH}}}(?![A-Za-z0-9_-]))"
    r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:_[A-Za-z0-9]+)?(?![A-Za-z0-9_-])"
)
_LOGIN = re.compile(LOGIN)


def is_login(text: str) -> bool:
    """Whether `text` is a GitHub login by the rule above."""
    return _LOGIN.fullmatch(text) is not None
