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
from concurrent.futures import ThreadPoolExecutor
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


def _bash_verdict(bash: str, text: str) -> tuple[bool, str]:
    with VERDICTS_LOCK:
        path = Path(WORKSPACE.name) / f"{next(NAMES)}.sh"
    path.write_bytes(text.encode("utf-8"))
    result = platform_support.run_tool([bash, "-n", platform_support.normalize(path)])
    return result.returncode == 0, result.output.strip()


def bash_verdicts(texts: Iterable[str]) -> dict[str, tuple[bool, str]]:
    """Whether `bash -n` reads each text, with what it printed; Bash missing fails, as it is a prerequisite."""
    wanted = list(dict.fromkeys(texts))
    with VERDICTS_LOCK:
        unchecked = [text for text in wanted if text not in VERDICTS]
    if unchecked:
        bash = platform_support.find_bash()
        if bash is None:
            raise AssertionError(f"{platform_support.BASH_MISSING} Bash checks the Bash reader's verdicts")
        with ThreadPoolExecutor(max_workers=min(8, len(unchecked))) as pool:
            found = list(pool.map(lambda text: _bash_verdict(bash, text), unchecked))
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
