"""The characters of a file path a document writes in prose or in a code span, rather than as a link's destination,
which link_destinations reads as CommonMark does: the one class every reader of such a path builds its pattern from.

A path is read as written. Prose and code are not percent-encoded, so `%` is part of a name, and `\\` is kept for the
reader to read as a link's destination is read, a separator on Windows and part of the name elsewhere. A path's first
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
