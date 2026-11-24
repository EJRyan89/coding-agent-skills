"""The workflow-token route's condition: find the lines a pull request adds that speak of the workflow run's token, so
the reviewer judges each against the token's row in docs/design.md, in whatever words it is written.

    python -B token_mentions.py --source-root <snapshot>

A review runs it as a specialists manifest's condition. Its environment names the pull request's unified diff in
CODE_REVIEW_DIFF_FILE and the file to write in CODE_REVIEW_CONDITION_OUTPUT. It reads the diff alone, in any file, and
nothing of the snapshot, which `--source-root` names only because every condition is given it.

Each run of a file's consecutive added lines is read as one text, so a sentence wrapped across comment lines is read
whole, each character in Unicode's compatibility form, NFKC, and each format character, such as a soft hyphen or a
zero-width space, dropped, so an invisible character inside a word does not hide it. A line mentions the token when
it names a credential by one of NAMED_CREDENTIALS, in any ASCII case, or the word "token" stands within MENTION_REACH
characters of a word in WORKFLOW_WORDS, such as "job", "step", "workflow", or this repository's "gate" and "check", in
that text. A line a removal or the context holds never counts: the reviewer anchors a finding on an added line. Limits:
a confusable letter, such as a Cyrillic o (U+043E) in "token", or a hyphen inside the word hides a mention; and a
diff line of many megabytes is read through a map of each character's origin, so it may take memory many times its
size and end at the review's condition timeout, which fails the review rather than closing the route.

The diff is the pull request author's, so everything it writes from the diff is quoted data. It writes each mentioning
line as `MENTION <file>:<line> <what> (added lines <first>-<last>)`, the file a JSON string and `<what>` either
`names <credential>`, a spelling of one of NAMED_CREDENTIALS, or `token near <word>`, one of WORKFLOW_WORDS, then
`TEXT <line>`, the added line as the author wrote it, cut to MAX_LINE_CHARACTERS around where it mentions the token,
and written as one JSON string with every character outside ASCII escaped, so no line of the author's starts a line of
its own or forges a `MENTION` line, and a control, format, or bidirectional character the author wrote, such as a
right-to-left override, shows escaped for the reviewer to see. It writes at most MAX_MENTIONS of them, in diff
order, while the file stays within MAX_FILE_BYTES, with an `OMITTED <n>` line for the rest.

A hunk header whose line number is not a whole number whole_number reads ends it with `FAILED ValueError: ...`.

It reads the diff through conditions.read_files, so a changed file whose path could name a file outside the snapshot is
not read, and gets a `REFUSED <path>: ...` line on stdout instead.

Prints `MENTIONS <found> given=<n>`.

Exit status:
  0  it wrote at least one mention, which opens the route
  1  no added line mentions the token, which closes the route
  2  a usage error or a last line FAILED <reason>, which fails the review
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))

from conditions import (
    DIFF_VARIABLE,
    FAILURE_STATUS,
    OUTPUT_VARIABLE,
    FileDiff,
    read_files,
    refusal_line,
    run_condition,
    unset_variables_line,
)
from console import run_main
from diff_paths import HUNK_HEADER
from json_writer import dumps
from whole_number import read

DUPLICATION_ALLOWED = {
    "EXIT_CONTRACT_EXEMPT": (
        ".claude/skills/review-repository/scripts/doc_passages.py and "
        ".claude/skills/review-repository/scripts/untested_functions.py speak the same condition protocol and state "
        "the same exemption; validation reads an exemption from the script it excuses, so none imports it"
    ),
}

EXIT_CONTRACT_EXEMPT = (
    "a code-review condition: exit 0 opens its route and 1 closes it, so a failure exits 2, which fails the review "
    "rather than silently closing the route"
)
# The names a credential is written by that mention the run's token wherever they stand: the variable gh reads, the
# secret's name, and the expression that reads the run's own token.
NAMED_CREDENTIALS = re.compile(r"(?<!\w)(?:gh_token|github_token|github\.token)(?!\w)", re.IGNORECASE | re.ASCII)
# The word "token", singular or plural, alone: not the end of a longer name such as `EXPRESSION_TOKEN`.
TOKEN_WORD = re.compile(r"(?<![\w-])tokens?(?![\w-])", re.IGNORECASE | re.ASCII)
# The words that place a token in a workflow run: what holds it, what runs it, and what grants it, this repository's
# own names for its CI jobs among them, such as "the mutation gate", "the deployable check", "validation", "a shard",
# and "a suite". ASCII
# alone, so a dotless or dotted i matches no word and every word the condition file names is ASCII.
WORKFLOW_WORDS = re.compile(
    r"(?<![\w-])(?:workflows?|jobs?|steps?|legs?|runs?|runners?|actions?|ci|permissions?|checkouts?|checks?|gates?"
    r"|validation|matrix|shards?|suites?)(?![\w-])",
    re.IGNORECASE | re.ASCII,
)
# How far from "token" a workflow word may stand, in characters of the joined text: about a sentence's length.
MENTION_REACH = 120
MAX_MENTIONS = 60
MAX_LINE_CHARACTERS = 300
# The condition file's bound, under the CONDITION_FILE_BYTES a review gives a reviewer, so the OMITTED line survives.
MAX_FILE_BYTES = 30 * 1024


@dataclass(frozen=True)
class Mention:
    """One added line that mentions the token, the run of added lines it stands in, and what it names."""

    path: str
    line: int
    first: int
    last: int
    what: str
    text: str  # the line as the author wrote it, stripped of surrounding whitespace
    at: int  # where in `text` it mentions the token


def added_runs(file: FileDiff) -> list[list[tuple[int, str]]]:
    """Each run of consecutive added lines in a file's hunks, as (head line number, text) pairs."""
    runs: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    number = 0
    for line in file.lines:
        if match := HUNK_HEADER.match(line):
            number = read(match.group(1), ValueError(f"the hunk header {line[:80]!r} gives no line number"))
            current = []
            runs.append(current)
        elif line.startswith("+"):
            current.append((number, line[1:]))
            number += 1
        elif line.startswith("\\"):
            continue
        else:
            if current:
                current = []
                runs.append(current)
            if not line.startswith("-"):
                number += 1
    return [run for run in runs if run]


def folded(text: str) -> tuple[str, list[int]]:
    """A line as the condition reads it, each character in NFKC and every format character (Unicode category Cf)
    dropped, and for each character of it the index in `text` of the character it came from."""
    read: list[str] = []
    origins: list[int] = []
    for index, character in enumerate(text):
        if unicodedata.category(character) == "Cf":
            continue
        normal = unicodedata.normalize("NFKC", character)
        read.append(normal)
        origins.extend([index] * len(normal))
    return "".join(read), origins


def _what(text: str, start: int, end: int) -> tuple[str, int] | None:
    """What the joined text names at or near [start, end), "names <credential>" or "token near <word>", and where it
    mentions the token, or None."""
    if named := NAMED_CREDENTIALS.search(text, start, end):
        return f"names {named.group(0)}", named.start()
    for token in TOKEN_WORD.finditer(text, start, end):
        window = text[max(0, token.start() - MENTION_REACH) : token.end() + MENTION_REACH]
        if word := WORKFLOW_WORDS.search(window):
            return f"token near {word.group(0).casefold()}", token.start()
    return None


def mentions(files: list[FileDiff]) -> list[Mention]:
    """Each added line of `files` that mentions the token, in diff order."""
    found: list[Mention] = []
    for file in files:
        for run in added_runs(file):
            joined = ""
            spans: list[tuple[int, int, list[int]]] = []
            for _, text in run:
                seen, origins = folded(text)
                spans.append((len(joined), len(joined) + len(seen), origins))
                joined += seen + " "
            for (number, raw), (start, end, origins) in zip(run, spans, strict=True):
                if (named := _what(joined, start, end)) is not None:
                    what, at = named
                    lead = len(raw) - len(raw.lstrip())
                    position = origins[at - start] - lead
                    mention = Mention(file.path, number, run[0][0], run[-1][0], what, raw.strip(), position)
                    found.append(mention)
    return found


def excerpt(mention: Mention) -> str:
    """The mention's line, or, past MAX_LINE_CHARACTERS, that many characters around where it mentions the token."""
    text = mention.text
    if len(text) <= MAX_LINE_CHARACTERS:
        return text
    start = max(0, min(mention.at - MAX_LINE_CHARACTERS // 2, len(text) - MAX_LINE_CHARACTERS))
    end = start + MAX_LINE_CHARACTERS
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def render(found: list[Mention]) -> tuple[str, int]:
    """The condition file and how many mentions it gives: each mention's line and its quoted text, in turn, while
    MAX_MENTIONS and MAX_FILE_BYTES allow, then an OMITTED line for the rest."""
    blocks: list[str] = []
    size = 0
    for mention in found[:MAX_MENTIONS]:
        run = f"(added lines {mention.first}-{mention.last})"
        block = f"MENTION {dumps(mention.path)}:{mention.line} {mention.what} {run}\nTEXT {dumps(excerpt(mention))}\n"
        if size + len(block.encode("utf-8")) > MAX_FILE_BYTES:
            break
        blocks.append(block)
        size += len(block.encode("utf-8"))
    if len(found) > len(blocks):
        blocks.append(f"OMITTED {len(found) - len(blocks)}\n")
    return "".join(blocks), sum(block.startswith("MENTION ") for block in blocks)


def _run() -> int:
    parser = argparse.ArgumentParser(description="Find the added lines that speak of the workflow run's token.")
    parser.add_argument("--source-root", required=True, type=Path)
    arguments = parser.parse_args()
    unset = unset_variables_line()
    if unset:
        print(unset)
        return FAILURE_STATUS
    diff = Path(os.environ[DIFF_VARIABLE]).read_bytes().decode("utf-8", errors="replace")
    files, refused = read_files(diff, arguments.source_root)
    for path in refused:
        print(refusal_line(path))
    found = mentions(files)
    if not found:
        print("MENTIONS 0 given=0")
        return 1
    written, given = render(found)
    print(f"MENTIONS {len(found)} given={given}")
    Path(os.environ[OUTPUT_VARIABLE]).write_bytes(written.encode("utf-8"))
    return 0


def main() -> int:
    return run_condition(_run)


if __name__ == "__main__":
    run_main(main, newline="\n")
