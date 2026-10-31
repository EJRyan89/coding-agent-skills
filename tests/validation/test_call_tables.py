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
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import bounded_process
from call_tables import (
    EVENT_LOOP,
    EVENT_LOOP_STARTS,
    LAST,
    PATH_METHODS,
    PROCESS_STARTS,
    TEXT_METHODS,
    TEXT_OPENERS,
    ProcessStart,
    TextOpener,
)

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
    _hold_mode_and_keywords(name, opener, parameters)


def _hold_mode_and_keywords(
    name: str, opener: TextOpener, parameters: list[inspect.Parameter], *, forwarded: bool = False
) -> None:
    """The mode a call opens in when given none is the default of its mode parameter, which only a keyword reaches
    where the table gives it no position, and each keyword the table reads an encoding by is one of its parameters, or
    one of the keywords a method that is `forwarded` hands to io.TextIOWrapper."""
    by_name = {parameter.name: parameter for parameter in parameters}
    mode = by_name.get("mode")
    if mode is None:
        assert opener.default_mode == "", name
    else:
        assert mode.default == opener.default_mode, name
        assert opener.mode is not None or mode.kind is inspect.Parameter.KEYWORD_ONLY, name
    for keyword in opener.encoding_keywords:
        assert keyword in by_name or forwarded, (name, keyword)
        if forwarded and keyword not in by_name:
            assert parameters[-1].kind is inspect.Parameter.VAR_KEYWORD, (name, keyword)
            assert keyword in inspect.signature(io.TextIOWrapper).parameters, (name, keyword)


@pytest.mark.parametrize(
    ("path_class", "method"), [(name, method) for name, methods in PATH_METHODS.items() for method in methods]
)
def test_each_path_method_takes_its_mode_and_encoding_where_the_table_says(path_class: str, method: str) -> None:
    found = _callable(path_class)
    assert found is not None, path_class
    parameters = _parameters(getattr(found, method))
    assert parameters is not None, (path_class, method)
    # A bound call gives every argument after self.
    parameters = parameters[1:]
    opener = PATH_METHODS[path_class][method]
    if opener.mode is not None:
        assert parameters[opener.mode].name == "mode", (path_class, method)
    if opener.encoding is not None:
        slot = parameters[min(opener.encoding, len(parameters) - 1)]
        # A parameter of its own, or the first of the positional arguments the method hands to io.TextIOWrapper,
        # whose own first after the stream is the encoding.
        if slot.kind is inspect.Parameter.VAR_POSITIONAL:
            assert parameters.index(slot) == opener.encoding, (path_class, method)
            assert list(inspect.signature(io.TextIOWrapper).parameters)[1] == "encoding"
        else:
            assert slot.name == "encoding", (path_class, method)
    forwarded = path_class == "zipfile.Path"
    _hold_mode_and_keywords(f"{path_class}.{method}", opener, parameters, forwarded=forwarded)


def test_each_path_class_defines_only_the_text_methods_the_table_gives_it() -> None:
    for name, methods in PATH_METHODS.items():
        found = _callable(name)
        assert found is not None, name
        assert {method for method in TEXT_METHODS if hasattr(found, method)} == set(methods), name


def test_zipfile_path_methods_take_the_encoding_first_after_the_mode(tmp_path: Path) -> None:
    # What the table's zipfile.Path slots stand on: the positional arguments open takes after its mode, and read_text
    # takes, are io.TextIOWrapper's, the encoding first.
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr("a.txt", "é".encode("utf-16"))
    member = zipfile.Path(archive, "a.txt")
    with member.open("r", "utf-16") as opened:
        assert opened.read() == "é"
    assert member.read_text("utf-16") == "é"
