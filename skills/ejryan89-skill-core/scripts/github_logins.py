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
_LOGIN = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:_[A-Za-z0-9]+)?")


def is_login(text: str) -> bool:
    """Whether `text` is a GitHub login by the rule above."""
    return len(text) <= MAXIMUM_LENGTH and _LOGIN.fullmatch(text) is not None
