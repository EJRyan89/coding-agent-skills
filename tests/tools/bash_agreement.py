"""Holds tools/bash_syntax.py to Bash's own syntax check: each text a suite hands the reader also goes through
`bash -n`, Git Bash on Windows and the system Bash elsewhere, and the two must agree on whether Bash reads it.

tests/tools/test_bash_syntax.py and tests/tools/test_hub_guard.py import it from beside them, and
tests/deployer/test_rendering_locking.py from here; it holds no test.
"""

from __future__ import annotations

import itertools
import sys
import tempfile
import threading
from collections.abc import Iterable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from markdown_sections import find_fences

from deployer import platform_support, render
from tools import bash_syntax

# Bash's verdict on each text this process has checked, whether it reads it and what it said, since suites hand the
# same command more than once.
VERDICTS: dict[str, tuple[bool, str]] = {}
VERDICTS_LOCK = threading.Lock()
WORKSPACE = tempfile.TemporaryDirectory(prefix="bash-agreement.")
NAMES = itertools.count()

# One Bash reads every unchecked text, since a start costs far more than a reading: each text in a subshell whose
# function turns on `set -n`, read but do not run, before `eval` parses the text, so its status is the one `bash -n`
# gives that text as a file. tests/tools/test_bash_syntax.py holds the two to the same verdicts. The subshell's own
# `$?` is never the verdict, since an MSYS Bash reads a subshell Windows ends as exit 0: `local -` restores the options
# when the function returns, so the subshell itself prints its reading's status after the reading, and a text with no
# such line was not read in the batch.
READ_EACH = r"""read_text() {
  local -
  eval $'set -n\n'"$1"
}
for ((index = 0; index < $2; index++)); do
  file="$1/$index.sh"
  IFS= read -r -d '' text < "$file"
  ( read_text "$text"; printf 'read %s %s\n' "$index" "$?" ) 2> "$file.said"
done"""


def find_bash() -> str:
    bash = platform_support.find_bash()
    if bash is None:
        raise AssertionError(f"{platform_support.BASH_MISSING} Bash checks the Bash reader's verdicts")
    return bash


def bash_n_verdict(text: str) -> tuple[bool, str]:
    """Whether `bash -n` reads text as a file, with what it printed: one Bash start for one text."""
    with VERDICTS_LOCK:
        path = Path(WORKSPACE.name) / f"{next(NAMES)}.sh"
    path.write_bytes(text.encode("utf-8"))
    result = platform_support.run_tool([find_bash(), "-n", platform_support.normalize(path)])
    return result.returncode == 0, result.output.strip()


def _read_each(texts: list[str]) -> list[tuple[bool, str]]:
    with VERDICTS_LOCK:
        folder = Path(WORKSPACE.name) / f"batch-{next(NAMES)}"
    folder.mkdir()
    for index, text in enumerate(texts):
        (folder / f"{index}.sh").write_bytes(text.encode("utf-8"))
    result = platform_support.run_tool(
        [find_bash(), "-c", READ_EACH, "read-each", platform_support.normalize(folder), str(len(texts))]
    )
    if result.returncode != 0:
        raise AssertionError(f"Bash did not read each of {len(texts)} texts: {result.output.strip()}")
    # The index and status each subshell printed after its reading. A subshell that ended first printed none: Windows
    # ended it, or a syntax error such as an open array assignment left it from inside `eval`, so that text goes to a
    # `bash -n` of its own, whose status this process reads.
    statuses = {
        words[1]: words[2] for words in (line.split() for line in result.output.splitlines()) if words[:1] == ["read"]
    }
    return [
        (
            (
                statuses[str(index)] == "0",
                (folder / f"{index}.sh.said").read_text(encoding="utf-8", errors="replace").strip(),
            )
            if str(index) in statuses
            else bash_n_verdict(text)
        )
        for index, text in enumerate(texts)
    ]


def bash_verdicts(texts: Iterable[str]) -> dict[str, tuple[bool, str]]:
    """Whether Bash reads each text, with what it printed, from one Bash start for the texts not yet checked; Bash
    missing fails, as it is a prerequisite."""
    wanted = list(dict.fromkeys(texts))
    with VERDICTS_LOCK:
        unchecked = [text for text in wanted if text not in VERDICTS]
    if unchecked:
        # A subshell for each text costs more than the start it saves when there is only one.
        found = [bash_n_verdict(unchecked[0])] if len(unchecked) == 1 else _read_each(unchecked)
        with VERDICTS_LOCK:
            VERDICTS.update(zip(unchecked, found, strict=True))
    with VERDICTS_LOCK:
        return {text: VERDICTS[text] for text in wanted}


def reader_reads(text: str) -> bool:
    try:
        bash_syntax.parse(text)
    except bash_syntax.BashSyntaxError:
        return False
    return True


def assert_reader_agrees(texts: Iterable[str]) -> None:
    """Fails naming each text Bash and the reader disagree on: one reads it and the other refuses it."""
    disagreements = []
    for text, (bash_reads, said) in bash_verdicts(texts).items():
        if reader_reads(text) != bash_reads:
            verdict = (
                "reads it and the reader refuses it" if bash_reads else f"refuses it ({said}) and the reader reads it"
            )
            disagreements.append(f"Bash {verdict}: {text!r}")
    if disagreements:
        raise AssertionError("\n".join(disagreements))


def children(item: object) -> list[object]:
    """The nodes a tree node holds, in the order the text holds them."""
    if isinstance(item, bash_syntax.CommandList):
        return [and_or for and_or, _ in item.items]
    if isinstance(item, bash_syntax.AndOr):
        return [item.first, *(pipeline for _, pipeline in item.rest)]
    if isinstance(item, bash_syntax.Pipeline):
        return list(item.commands)
    if isinstance(item, (bash_syntax.Subshell, bash_syntax.Group, bash_syntax.FunctionDefinition)):
        return [item.body]
    if isinstance(item, bash_syntax.If):
        held: list[object] = [part for clause in item.clauses for part in clause]
        return held if item.otherwise is None else [*held, item.otherwise]
    if isinstance(item, bash_syntax.Loop):
        return [item.body] if item.condition is None else [item.condition, item.body]
    if isinstance(item, bash_syntax.Case):
        return list(item.branches)
    return []


def simple_commands(node: object) -> list[bash_syntax.Simple]:
    """Every simple command in a tree, in the order the text holds them."""
    if isinstance(node, bash_syntax.Simple):
        return [node]
    return [command for child in children(node) for command in simple_commands(child)]


def rendered_fragments(directory: Path) -> list[str]:
    """The Bash a rendered skill holds, as the deployer checks it: each .sh or .bash file, and the body of each
    Markdown fence it substitutes as shell."""
    fragments: list[str] = []
    for path in sorted(directory.rglob("*")):
        suffix = path.suffix.casefold()
        if suffix in (".sh", ".bash"):
            fragments.append(path.read_text(encoding="utf-8"))
        elif suffix == ".md":
            lines = path.read_text(encoding="utf-8").split("\n")
            fragments.extend(
                "".join(f"{fence.text(lines[index])}\n" for index in fence.body())
                for fence in find_fences(lines)
                if render.fence_context(fence) == "shell"
            )
    return fragments


def assert_value_quoted(fragments: Iterable[str], value: str) -> None:
    """Fails unless Bash and the reader agree on every fragment, and each fragment that holds value is one they both
    read, in which the reader finds value whole inside one quoted part of a word, as the token-in-quotes rule renders
    it."""
    fragments = list(fragments)
    assert_reader_agrees(fragments)
    held = [fragment for fragment in fragments if value in fragment]
    if not held:
        raise AssertionError(f"no rendered fragment holds {value!r}")
    for fragment in held:
        try:
            tree = bash_syntax.parse(fragment)
        except bash_syntax.BashSyntaxError as error:
            raise AssertionError(f"Bash and the reader refuse the rendered {fragment!r}: {error}") from error
        parts = [part for command in simple_commands(tree) for word in command.words for part in word.parts]
        if not any(part.quoted and part.kind == "text" and value in part.text for part in parts):
            raise AssertionError(f"the reader does not find {value!r} inside one quoted word of {fragment!r}")
