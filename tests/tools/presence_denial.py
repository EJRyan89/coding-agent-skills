"""A path the file system will not let a tool examine or list, for the suites under tests/tools that show a tool
refusing such a path rather than reading it as absent or empty.

The tools' suites import it from beside them; it holds no test.
"""

from __future__ import annotations

import contextlib
import errno
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

Wrap = Callable[[Callable[..., Any]], Callable[..., Any]]


@contextlib.contextmanager
def denied(target: Path, *, stat: bool = True) -> Iterator[None]:
    """`target` cannot be listed, as a file system refuses a folder: os.scandir and os.listdir, and so Path.iterdir
    and os.walk, raise PermissionError, and Path.glob and Path.rglob match nothing in it, as Python 3.15's do where
    iterdir raises. With `stat`, its stat is refused too: Path.stat, os.stat, and os.lstat raise, and Path.exists,
    is_file, and is_dir and the os.path predicates answer False, as from Python 3.14 on."""

    def names(argument: object) -> bool:
        return isinstance(argument, (str, os.PathLike)) and Path(argument) == target

    def refusing(real: Callable[..., Any]) -> Callable[..., Any]:
        def call(path: Any, *args: Any, **kwargs: Any) -> Any:
            if names(path):
                raise PermissionError(errno.EACCES, "Access is denied", str(target))
            return real(path, *args, **kwargs)

        return call

    def answering(answer: Any) -> Wrap:
        def wrap(real: Callable[..., Any]) -> Callable[..., Any]:
            def call(path: Any, *args: Any, **kwargs: Any) -> Any:
                return answer() if names(path) else real(path, *args, **kwargs)

            return call

        return wrap

    patches: list[tuple[object, str, Wrap]] = [(os, "scandir", refusing), (os, "listdir", refusing)]
    patches += [(Path, name, answering(lambda: iter(()))) for name in ("glob", "rglob")]
    if stat:
        patches += [(Path, "stat", refusing), (os, "stat", refusing), (os, "lstat", refusing)]
        patches += [(Path, name, answering(lambda: False)) for name in ("exists", "is_file", "is_dir")]
        patches += [(os.path, name, answering(lambda: False)) for name in ("exists", "lexists", "isfile", "isdir")]
    with contextlib.ExitStack() as stack:
        for owner, name, wrap in patches:
            stack.enter_context(mock.patch.object(owner, name, wrap(getattr(owner, name))))
        yield
