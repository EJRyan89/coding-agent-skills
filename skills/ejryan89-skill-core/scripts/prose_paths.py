"""The characters of a file path a document writes in prose or in a code span, rather than as a link's destination,
which link_destinations reads as CommonMark does: the one class every reader of such a path builds its pattern from.

A path is read as written. Prose and code are not percent-encoded, so `%` is part of a name, and `\\` is kept for the
reader to read as a link's destination is read, a separator on Windows and part of the name elsewhere. RELATIVE_PATH is
the body every reader builds its pattern around, and PROSE_PATH the one pattern of a path written bare in prose with a
separator and an extension. A path's first
character, or the first after its root, is PATH_START: a word character, a non-ASCII letter or digit among them, `.`,
`%`, or `-`. Every later one is PATH_CHARACTER: those, `/`, `\\`, `+`, `@`, `~`, or `&`, so `docs/c++.md`,
`docs/über.md`, `C:\\PROGRA~1\\a.md`, and `references/Q&A.md` are paths whole. So a path that starts with `~`, `@`, `+`,
`&`, or `$`, as a home folder, an import, or a variable does, is not read, nor is one holding a space or a character
prose ends or joins words with, such as `'`, `=`, `#`, `;`, `,`, `:`, `!`, or a glob's `*`; each reader states where a
path it reads starts and ends, and these limits, beside its pattern.

Limit: a name written in Unicode's decomposed form, NFD, as `u` followed by the combining U+0308 for `ü`, is no path,
since a combining mark is no word character; the composed form, NFC, which editors write, is read.
"""

from __future__ import annotations

# The first character of a path, or the first after its root, as a regular expression's character class.
PATH_START = r"[\w.%-]"
# Every character of a path after its first, as a regular expression's character class.
PATH_CHARACTER = r"[\w%+@~&./\\-]"
# A relative path's text: its first character, then the rest, the one body every reader of a path builds its pattern
# around, each stating where its path starts and ends.
RELATIVE_PATH = rf"{PATH_START}{PATH_CHARACTER}*"
# A path written bare in prose that holds a `/` and ends at a file's extension, read whole: it starts only where no
# PATH_CHARACTER stands before it, so a path is never read from inside another, after its `%` or `\\`, and
# `docs%20x/guide.md` and `a\\b/c.md` are each one path. A `#` ends it, so a fragment is never part of the path.
PROSE_PATH = rf"(?<!{PATH_CHARACTER}){PATH_START}{PATH_CHARACTER}*/{PATH_CHARACTER}*\.\w+"
