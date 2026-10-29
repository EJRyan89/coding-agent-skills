"""Fixture tests for tests/validation/call_tables.py.

Each table entry is held to the standard library call it names, on the platforms where that call exists, so a row
whose argument names or positions drift from the library fails here rather than in every policy that reads it.
"""

from __future__ import annotations

import asyncio
import codecs
import importlib
import inspect
import io
import os
import subprocess
import sys
import warnings
from collections.abc import Callable
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import bounded_process
from call_tables import EVENT_LOOP, EVENT_LOOP_STARTS, LAST, PROCESS_STARTS, TEXT_OPENERS, ProcessStart

KEYWORD_KINDS = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)

ENCODING_ALLOWED = {
    "test_codecs_open_with_no_encoding_opens_text": (
        "reads no text: it opens the null device with no encoding to show that codecs.open then opens text, the "
        "reason the encoding policy reads it"
    ),
}


def _callable(name: str) -> Callable[..., object] | None:
    """The function a table name stands for on this platform, or None where the platform has none."""
    found: object
    module, _, attribute = name.rpartition(".")
    if module == EVENT_LOOP:
        found = getattr(asyncio.AbstractEventLoop, attribute)
    else:
        try:
            found = getattr(importlib.import_module(module or "builtins"), attribute, None)
        except ImportError:
            return None
    return found if callable(found) else None


def _parameters(function: Callable[..., object]) -> list[inspect.Parameter] | None:
    try:
        return list(inspect.signature(function).parameters.values())
    except (TypeError, ValueError):
        return None


def _present() -> list[tuple[str, ProcessStart, list[inspect.Parameter]]]:
    found = []
    for name, start in PROCESS_STARTS.items():
        function = _callable(name)
        parameters = None if function is None else _parameters(function)
        if parameters is not None:
            # A method's self is no argument a call gives it.
            found.append((name, start, [p for p in parameters if p.name != "self"]))
    return found


def test_each_process_start_names_its_command_where_the_library_takes_it() -> None:
    present = _present()
    # The library's signatures are read on every platform, so the check is never empty.
    assert any(name == "subprocess.run" for name, _, _ in present)
    for name, start, parameters in present:
        position, keyword = start.command
        # A positional-only parameter's name differs between platforms, as Windows' os.spawnv calls its program path,
        # and no call can give it by keyword, so only a name a keyword reaches is held.
        if parameters[position].kind in KEYWORD_KINDS:
            assert parameters[position].name == keyword, name


def test_each_process_start_takes_its_environment_where_the_table_says() -> None:
    for name, start, parameters in _present():
        names = [parameter.name for parameter in parameters]
        variadic = any(parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters)
        if start.environment is None:
            # A start that takes none inherits this process's environment: no parameter could give it one.
            assert "env" not in names, name
            continue
        position, keyword = start.environment
        if position == LAST:
            assert variadic, name
        elif position is not None:
            assert len(parameters) > position, name
            if parameters[position].kind in KEYWORD_KINDS:
                assert names[position] == keyword, name
        else:
            # A keyword the call takes by name, or hands on through its own keywords.
            assert keyword in names or parameters[-1].kind is inspect.Parameter.VAR_KEYWORD, name


def test_the_starts_that_decode_text_always_and_the_one_that_takes_no_encoding() -> None:
    always = {name for name, start in PROCESS_STARTS.items() if start.text == "always"}
    assert always == {"os.popen", "subprocess.getoutput", "subprocess.getstatusoutput"}
    # subprocess.getoutput takes an encoding; os.popen takes none, so every os.popen decodes with the locale's.
    assert "encoding" in inspect.signature(subprocess.getoutput).parameters
    popen = _callable("os.popen")
    assert popen is not None
    assert "encoding" not in inspect.signature(popen).parameters


def test_the_event_loop_starts_and_ejryan89_skill_cores_runners() -> None:
    assert {"subprocess_exec", "subprocess_shell"} == EVENT_LOOP_STARTS
    cleared = {name for name, start in PROCESS_STARTS.items() if start.clears}
    assert cleared == {"bounded_process.run_bounded", "bounded_process.streaming"}
    for name in cleared:
        assert callable(getattr(bounded_process, name.rpartition(".")[2]))


def test_codecs_open_with_no_encoding_opens_text() -> None:
    # The reason codecs.open is a text opener: given no encoding it returns the built-in open's text wrapper. Python
    # 3.14 deprecated it, which says nothing about what it opens.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        with codecs.open(os.devnull) as opened:  # type: ignore[deprecated]  # the deprecated open is the subject
            assert isinstance(opened, io.TextIOWrapper)


@pytest.mark.parametrize("name", sorted(TEXT_OPENERS))
def test_each_text_opener_takes_its_mode_and_encoding_where_the_table_says(name: str) -> None:
    function = _callable(name)
    assert function is not None, name
    parameters = _parameters(function)
    assert parameters is not None, name
    names = [parameter.name for parameter in parameters]
    opener = TEXT_OPENERS[name]
    if opener.mode is not None:
        assert names[opener.mode] == "mode", name
    if opener.encoding is not None:
        assert names[opener.encoding] == "encoding", name
    if opener.plain_file:
        assert names[0] in {"file", "filename"}, name
