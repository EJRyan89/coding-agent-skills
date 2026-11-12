"""One pwsh per test process that runs the hub guard's PowerShell reader, for the guard's tests that read in-process.

The guard reads each PowerShell command in a pwsh start of its own, by design, and about half of a start is pwsh
starting. A test that decides a command in its own process hands that start here instead: `resident_reads` routes
the one `run_bounded` call `hub_guard.powershell_events` makes for its reader to a pwsh that stays running, which sets
the same base64 payload as the reader's stdin with `[Console]::SetIn`, runs the same reader text,
`hub_guard.powershell_reader()`, as a script block in a scope of its own, and answers with what the reader wrote.
Everything else in `powershell_events` runs as it is: finding pwsh, the payload, the time limit it asks for, and its
checks on the answer.

The resident holds no state from one read to the next: each read runs in a new scope, `$Error` is cleared after it,
and a read that leaves a global variable or function behind is a malfunction. A malfunction (the resident ended,
answered with something other than an answer, kept state, or did not answer in time) fails the test that met it when
`resident_reads` ends, so no decision passes on a reading the resident broke. The tests that hold the guard's own
pwsh start (`EndToEndTests`, `PowerShellReaderStartTests`, the hook command tests, `NodeCatalogueTests`) read through
real starts, and `test_hub_guard_pwsh.py` holds that the resident reads every node sample as a real start does.

tests/tools/test_hub_guard.py and tests/tools/test_hub_guard_pwsh.py import it from beside them; it holds no test.
"""

from __future__ import annotations

import atexit
import base64
import contextlib
import queue
import subprocess
import sys
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import IO, Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import bounded_process
from bounded_process import CUT_NOTE, Finished

from tools import hub_guard

# The resident's own script. Its first line of input is the reader's text, base64-encoded UTF-8; each later line is one
# read's stdin, the payload the guard hands the reader. It answers each read with one line, "READ <stdout> <stderr>",
# each base64-encoded UTF-8, or "STATE <message>" when the read left a global variable or function behind. A reader
# error stops the reader as it stops pwsh, and is answered as pwsh writes it, on stderr with nothing on stdout.
SERVER = r"""
$in = [Console]::In
$out = [IO.StreamWriter]::new([Console]::OpenStandardOutput(), [Text.UTF8Encoding]::new($false))
$out.AutoFlush = $true
$reader = [ScriptBlock]::Create([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($in.ReadLine())))
function ConvertTo-Base64([string] $Text) { [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Text)) }
function Get-GlobalNames {
    @(Get-Variable -Scope Global | ForEach-Object { "variable $($_.Name)" }) +
        @(Get-ChildItem function: | ForEach-Object { "function $($_.Name)" }) | Sort-Object
}
function Invoke-Read([string] $Request) {
    $before = Get-GlobalNames
    [Console]::SetIn([IO.StringReader]::new($Request))
    $errors = ''
    # Each line is kept as the reader writes it, so what it wrote before an error stops it stays, as on pwsh's stdout.
    $lines = [Collections.Generic.List[string]]::new()
    try { & $reader | ForEach-Object { $lines.Add("$_") } } catch { $errors = "$_" }
    finally { [Console]::SetIn($in); $global:Error.Clear() }
    $kept = @(Compare-Object @($before) @(Get-GlobalNames) | ForEach-Object InputObject)
    if ($kept.Count -gt 0) { return "STATE the read left $($kept -join ', ') behind" }
    $text = if ($lines.Count -gt 0) { ($lines -join "`n") + "`n" } else { '' }
    "READ $(ConvertTo-Base64 $text) $(ConvertTo-Base64 $errors)"
}
while ($null -ne ($request = $in.ReadLine())) { $out.WriteLine((Invoke-Read $request)) }
"""
HERE = Path(__file__).resolve().parent
# The words after the program on the command line powershell_events starts its reader with.
READER_OPTIONS = ("-NoLogo", "-NoProfile", "-NonInteractive", "-Command")


class Malfunction(Exception):
    """The resident did not answer a read as the reader in its own pwsh start would have."""


class ResidentPwsh:
    """One running pwsh that answers each read with what `reader` writes for that stdin."""

    def __init__(self, program: str, reader: str) -> None:
        # Started directly, not through run_bounded, which waits for its command to end; with the environment
        # run_bounded hands a command it is given none for, git's repository-selecting variables removed. It runs in
        # this folder rather than wherever the test that first read through it stood, a test's own temporary folder
        # among them, which Windows would then refuse to remove while the resident runs.
        self.process = subprocess.Popen(
            [program, *READER_OPTIONS, SERVER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=bounded_process.non_interactive_environment(),
            cwd=HERE,
        )
        self.answers: queue.Queue[bytes] = queue.Queue()
        assert self.process.stdout is not None
        threading.Thread(target=self._collect, args=(self.process.stdout,), daemon=True).start()
        self._send(base64.b64encode(reader.encode("utf-8")))

    def _collect(self, stdout: IO[bytes]) -> None:
        """Queues each answer line, and an empty one when the resident's output ends."""
        for line in stdout:
            self.answers.put(line)
        self.answers.put(b"")

    def _send(self, line: bytes) -> None:
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(line + b"\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            # OSError for a resident that ended, ValueError for one this process closed.
            raise Malfunction(f"the resident pwsh took no input ({exc})") from exc

    def read(self, payload: bytes, timeout: float, output_limit: int | None) -> Finished:
        """What the reader writes for `payload` on its stdin, kept to `output_limit` bytes a stream as run_bounded
        keeps them. Raises subprocess.TimeoutExpired, after ending the resident, past `timeout` seconds."""
        self._send(payload)
        try:
            answer = self.answers.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise subprocess.TimeoutExpired("resident pwsh", timeout) from None
        if not answer:
            raise Malfunction("the resident pwsh ended")
        kind, _, rest = answer.rstrip(b"\r\n").partition(b" ")
        if kind == b"STATE":
            raise Malfunction(rest.decode("utf-8", "replace"))
        streams = rest.split(b" ")
        if kind != b"READ" or len(streams) != 2:
            raise Malfunction(f"the resident pwsh answered {answer[:200]!r}")
        stdout, stderr = (kept(base64.b64decode(stream), output_limit) for stream in streams)
        return Finished(0 if not stderr else 1, stdout, stderr)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        # Closed here rather than when it is collected, where a flush into the ended pipe would fail unseen.
        assert self.process.stdin is not None
        with contextlib.suppress(OSError):
            self.process.stdin.close()


def kept(stream: bytes, limit: int | None) -> bytes:
    """A stream as run_bounded reads it back: its first `limit` bytes and CUT_NOTE when it holds more."""
    if limit is None or len(stream) <= limit:
        return stream
    return stream[:limit] + CUT_NOTE.format(cut=len(stream) - limit, limit=limit).encode()


_RESIDENTS: dict[str, ResidentPwsh] = {}
_LOCK = threading.Lock()


def resident(program: str) -> ResidentPwsh:
    """This process's resident for the pwsh at `program`, started at its first read and again after one ends."""
    found = _RESIDENTS.get(program)
    if found is None or found.process.poll() is not None:
        found = _RESIDENTS[program] = ResidentPwsh(program, hub_guard.powershell_reader())
    return found


@atexit.register
def close_all() -> None:
    with _LOCK:
        for each in _RESIDENTS.values():
            each.close()
        _RESIDENTS.clear()


def reads_its_reader(command: Sequence[str]) -> bool:
    """Whether a command is the reader start powershell_events makes: pwsh, its options, and the reader's text."""
    return list(command[1:]) == [*READER_OPTIONS, hub_guard.powershell_reader()]


@contextlib.contextmanager
def resident_reads() -> Iterator[list[str]]:
    """Hands the guard's reader starts to the resident pwsh while it lasts, and yields the malfunctions met.

    Every other command hub_guard.run_bounded is given runs as it would. On leaving, a malfunction met fails the caller
    with an AssertionError, whatever decision the guard made of it.
    """
    malfunctions: list[str] = []

    def bounded(command: Sequence[str], timeout: float, **options: Any) -> Finished:
        if not reads_its_reader(command) or set(options) - {"input_bytes", "env", "output_limit", "tree"}:
            return bounded_process.run_bounded(command, timeout, **options)
        with _LOCK:
            try:
                return resident(command[0]).read(options["input_bytes"], timeout, options.get("output_limit"))
            except Malfunction as exc:
                malfunctions.append(str(exc))
                raise OSError(f"the resident pwsh failed ({exc})") from exc
            except subprocess.TimeoutExpired:
                malfunctions.append(f"the resident pwsh did not answer within {timeout} seconds")
                raise

    with mock.patch.object(hub_guard, "run_bounded", side_effect=bounded):
        yield malfunctions
    if malfunctions:
        raise AssertionError(f"the resident pwsh malfunctioned: {malfunctions}")
