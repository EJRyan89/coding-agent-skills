"""Fixture tests for the console-setup policy in tests/validation/skill_scripts.py: each standard-stream reach,
console device, and lookup by name a wrapper or a reconfigure may take, refused over a standard stream and passed over
any other file. They sit apart from test_skill_scripts.py so that suite's recorded tests keep a shard of their own.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_scripts import console_setup_problems
from validation_support import write_fixture_tree

# The operating-system names this module states on purpose, with the reason, which the platform policy reads.
PLATFORM_ALLOWED = {
    "/proc": "names Linux's per-process descriptor paths as fixture values the console-setup policy refuses or "
    "passes; the fixture writes them into a module it parses and opens nothing",
}


def mark_skills(root: Path) -> Path:
    """Give each skills/<name> folder with a scripts/ directory the SKILL.md that makes it a skill, and return root."""
    for scripts in (root / "skills").glob("*/scripts"):
        (scripts.parent / "SKILL.md").write_text(f"# {scripts.parent.name}\n", encoding="utf-8")
    return root


# Every opener call_tables names builds a text stream over a standard stream or its descriptor when given one, so each
# is refused there as the built-in open is; the names are written out, never read from the table under test.
@pytest.mark.parametrize(
    "opener",
    [
        "open",
        "builtins.open",
        "io.open",
        "codecs.open",
        "os.fdopen",
        "io.TextIOWrapper",
        "tempfile.TemporaryFile",
        "tempfile.NamedTemporaryFile",
        "tempfile.SpooledTemporaryFile",
        "gzip.open",
        "bz2.open",
        "lzma.open",
    ],
)
def test_console_setup_policy_refuses_every_text_opener_over_a_standard_stream(tmp_path: Path, opener: str) -> None:
    module = opener.rpartition(".")[0]
    imports = f"import {module}\n" if module else ""
    write_fixture_tree(
        tmp_path, {"tools/reopened.py": f"{imports}import sys\n\nOUT = {opener}(sys.stdout.fileno(), 'w')\n"}
    )
    mark_skills(tmp_path)
    [problem] = console_setup_problems(tmp_path)
    assert problem.startswith("tools/reopened.py:")
    assert "reconfigures a stream itself" in problem


# Every way a script reaches a standard stream to build a text stream over it: by its descriptor, written out, taken
# from fileno(), copied, or opened as a file first; by a name bound to the stream, a layer under it, its descriptor, or
# a codec class; through a codec's own stream classes; by the console's device path; and partially applied.
STREAM_REACHES = [
    *(f'open({fd}, "w", encoding="utf-8")' for fd in (0, 1, 2)),
    'os.fdopen(1, "w", encoding="utf-8")',
    'io.open(2, "w", encoding="utf-8")',
    'codecs.open(1, "w", "utf-8")',
    'open(file=1, mode="w", encoding="utf-8")',
    'io.TextIOWrapper(io.FileIO(1, "w"), encoding="utf-8")',
    'io.TextIOWrapper(io.BufferedWriter(io.FileIO(fd=2, mode="w")), encoding="utf-8")',
    'os.fdopen(os.dup(1), "w", encoding="utf-8")',
    *(
        f'fd = sys.{stream}.fileno()\nopen(fd, "w", encoding="utf-8")'
        for stream in ("stdin", "stdout", "stderr", "__stdout__")
    ),
    'fd = 1\nos.fdopen(fd, "w", encoding="utf-8")',
    'B = sys.stdout.buffer\nio.TextIOWrapper(B, encoding="utf-8")',
    'S = sys.stderr\nB = S.buffer\nio.TextIOWrapper(B, encoding="utf-8")',
    'io.TextIOWrapper(B, encoding="utf-8")\nB = sys.stdout.buffer',
    'if (B := sys.stdout.buffer):\n    io.TextIOWrapper(B, encoding="utf-8")',
    'B: object = sys.stdout.buffer\nio.TextIOWrapper(B, encoding="utf-8")',
    'B = sys.stdout.detach()\nio.TextIOWrapper(B, encoding="utf-8")',
    'R = sys.stdout.buffer.raw\nio.TextIOWrapper(R, encoding="utf-8")',
    'B = io.BufferedWriter(io.FileIO(1, "w"))\nio.TextIOWrapper(B, encoding="utf-8")',
    'fd = os.dup(sys.stderr.fileno())\nos.fdopen(fd, "w", encoding="utf-8")',
    'W = codecs.getwriter("utf-8")\nW(sys.stdout.buffer)',
    'R = codecs.getreader("utf-8")\nB = sys.stdin.buffer\nR(B)',
    'codecs.EncodedFile(sys.stdout.buffer, "utf-8")',
    'B = sys.stdout.buffer\ncodecs.EncodedFile(B, "utf-8")',
    'codecs.StreamReaderWriter(sys.stdout.buffer, codecs.getreader("utf-8"), codecs.getwriter("utf-8"))',
    "codecs.StreamRecoder(sys.stdout.buffer, None, None, None, None)",
    'codecs.lookup("utf-8").streamwriter(sys.stdout.buffer)',
    'W = codecs.lookup("utf-8").streamwriter\nW(sys.stdout.buffer)',
    "encodings.utf_8.StreamWriter(sys.stdout.buffer)",
    # A codec factory or stream class reached by a lookup by name.
    'getattr(codecs, "getwriter")("utf-8")(sys.stdout.buffer)',
    'W = getattr(codecs, "getreader")("utf-8")\nW(sys.stdin.buffer)',
    'getattr(codecs.lookup("utf-8"), "streamwriter")(sys.stdout.buffer)',
    'getattr(encodings.utf_8, "StreamWriter")(sys.stdout.buffer)',
    *(f'open("{device}", "w", encoding="utf-8")' for device in ("/dev/stdout", "/dev/fd/2", "CONOUT$", "CON")),
    'functools.partial(io.TextIOWrapper, sys.stdout.buffer)(encoding="utf-8")',
    'functools.partial(open, 1, "w")(encoding="utf-8")',
]


@pytest.mark.parametrize("reach", STREAM_REACHES)
def test_console_setup_policy_refuses_a_wrapper_over_every_reach_to_a_standard_stream(
    tmp_path: Path, reach: str
) -> None:
    imports = "import codecs\nimport encodings.utf_8\nimport functools\nimport io\nimport os\nimport sys\n\n"
    write_fixture_tree(tmp_path, {"tools/reopened.py": imports + reach + "\n"})
    mark_skills(tmp_path)
    [problem] = console_setup_problems(tmp_path)
    assert problem.startswith("tools/reopened.py:")
    assert "reconfigures a stream itself" in problem


# The same calls over a file that is no standard stream, a descriptor in another argument, or a stream only written to.
@pytest.mark.parametrize(
    "use",
    [
        'open(path, "w", 1, encoding="utf-8")',
        'open(path, "w", buffering=1, encoding="utf-8")',
        'open(3, "w", encoding="utf-8")',
        'fd = 3\nos.fdopen(fd, "w", encoding="utf-8")',
        'B = raw.buffer\nio.TextIOWrapper(B, encoding="utf-8")',
        'W = codecs.getwriter("utf-8")\nW(raw)',
        'getattr(codecs, "getwriter")("utf-8")(raw)',
        'getattr(codecs, "lookup")("utf-8")(sys.stdout.buffer)',
        'codecs.EncodedFile(raw, "utf-8")',
        'open("/dev/null", "w", encoding="utf-8")',
        'out = sys.stdout\nout.write("text")',
        'tempfile.TemporaryFile("w+", 1, "utf-8")',
        # What a standard stream reads is data, not the stream, and neither is what is built from it.
        'data = sys.stdin.read()\nio.TextIOWrapper(io.BytesIO(data.encode()), encoding="utf-8")',
        'fd = os.open(path, os.O_WRONLY)\nos.fdopen(fd, "w", encoding="utf-8")',
    ],
)
def test_console_setup_policy_passes_a_wrapper_over_any_other_file(tmp_path: Path, use: str) -> None:
    imports = "import codecs\nimport io\nimport os\nimport sys\nimport tempfile\n\n"
    write_fixture_tree(tmp_path, {"tools/plain.py": imports + use + "\n"})
    mark_skills(tmp_path)
    assert console_setup_problems(tmp_path) == []


# A console device in each form Windows opens it by: any case, before an extension or a colon, in any folder, after a
# drive, or through the device namespace; a Linux stream path however its separators fall; and either as bytes.
@pytest.mark.parametrize(
    "device",
    [
        "conout$",
        "Conin$",
        "con",
        "CON:",
        "con.txt",
        "CONOUT$.log",
        "con :",
        "C:\\temp\\CONOUT$",
        "C:CON",
        "\\\\.\\CON",
        "//./conout$",
        "logs/con.txt",
        "/dev//stdout",
        "/dev/./stderr",
        "/dev/fd//1",
        "/dev/tty",
        "/dev/tty1",
        "/dev/pts/3",
        "/dev/console",
        "/proc/self/fd/1",
        "/proc/thread-self/fd/2",
        "/proc/4242/fd//0",
        "//dev/stdout",
        "///dev/tty",
        "/var/../dev/stdout",
        "/../dev/console",
        "/proc/self/fd/../fd/1",
        b"CONOUT$",
    ],
)
def test_console_setup_policy_refuses_a_wrapper_over_every_spelling_of_a_console_device(
    tmp_path: Path, device: str | bytes
) -> None:
    write_fixture_tree(tmp_path, {"tools/reopened.py": f'OUT = open({device!r}, "w", encoding="utf-8")\n'})
    mark_skills(tmp_path)
    assert console_setup_problems(tmp_path) == [
        "tools/reopened.py:1 reconfigures a stream itself; call run_main() from ejryan89-skill-core instead; see "
        '"Script results" in docs/adding-a-skill.md'
    ]


# A reserved name that opens no console, a name a device name only starts, and a path that only ends like one.
@pytest.mark.parametrize(
    "path",
    [
        "NUL",
        "prn.txt",
        "COM1:",
        "console.txt",
        "conifer",
        "con$",
        "dev/stdout",
        "/dev/null",
        "/dev/stdout2",
        "/dev/ttyS",
        "/dev/fd/3",
        "/proc/self/fd/3",
        "/proc/01/fd/1",
        "/proc/self/status",
        "/dev/stdout/..",
        "/dev/tty/../null",
        "",
        b"\xff",
    ],
)
def test_console_setup_policy_passes_a_wrapper_over_a_file_no_console_device_names(
    tmp_path: Path, path: str | bytes
) -> None:
    write_fixture_tree(tmp_path, {"tools/plain.py": f'OUT = open({path!r}, "w", encoding="utf-8")\n'})
    mark_skills(tmp_path)
    assert console_setup_problems(tmp_path) == []


# A stream's reconfigure reached by its name: through builtins' getattr, an alias of it, a name computed at run time,
# or operator.methodcaller, called at once or bound first.
@pytest.mark.parametrize(
    "reach",
    [
        'builtins.getattr(sys.stdout, "reconfigure")(encoding="utf-8")',
        'from builtins import getattr as lookup\nlookup(sys.stderr, "reconfigure")(encoding="utf-8")',
        'getattr(sys.stdout, name)(encoding="utf-8")',
        'fix = builtins.getattr(sys.stdout, name)\nfix(encoding="utf-8")',
        'operator.methodcaller("reconfigure", encoding="utf-8")(sys.stdout)',
        "operator.methodcaller(name, **OPTIONS)(sys.stdout)",
        'functools.partial(sys.stdout.reconfigure, encoding="utf-8")()',
        'fix = functools.partial(sys.stderr.reconfigure)\nfix(encoding="utf-8")',
        'operator.attrgetter("reconfigure")(sys.stdout)(encoding="utf-8")',
        'fix = operator.attrgetter("reconfigure")(sys.stdout)\nfix(encoding="utf-8")',
        'sys.stdout.reconfigure(**{"encoding": "utf-8"})',
        "getattr(sys.stdout, name)(**OPTIONS)",
        'fix: object = sys.stdout.reconfigure\nfix(encoding="utf-8")',
        'fix: object = getattr(sys.stdout, "reconfigure")\nfix(encoding="utf-8")',
        '(fix := builtins.getattr(sys.stderr, "reconfigure"))\nfix(encoding="utf-8")',
        # Every other form of the one catalogue of lookups by name, by the name and by a computed one.
        'inspect.getattr_static(sys.stdout, "reconfigure")(sys.stdout, encoding="utf-8")',
        'object.__getattribute__(sys.stdout, "reconfigure")(encoding="utf-8")',
        'sys.stderr.__getattribute__("reconfigure")(encoding="utf-8")',
        'vars(sys.stdout)["reconfigure"](encoding="utf-8")',
        'sys.stdout.__dict__["reconfigure"](encoding="utf-8")',
        'fix = vars(sys.stdout)["reconfigure"]\nfix(encoding="utf-8")',
        'inspect.getattr_static(sys.stdout, name)(sys.stdout, encoding="utf-8")',
        'object.__getattribute__(sys.stdout, name)(encoding="utf-8")',
        'operator.attrgetter(name)(sys.stdout)(encoding="utf-8")',
        'sys.stdout.__dict__[name](encoding="utf-8")',
        'fix = vars(sys.stderr)[name]\nfix(encoding="utf-8")',
    ],
)
def test_console_setup_policy_refuses_a_reconfigure_looked_up_by_name(tmp_path: Path, reach: str) -> None:
    imports = "import builtins\nimport functools\nimport inspect\nimport operator\nimport sys\n\n"
    write_fixture_tree(tmp_path, {"tools/reconfigured.py": imports + reach + "\n"})
    mark_skills(tmp_path)
    [problem] = console_setup_problems(tmp_path)
    assert problem.startswith("tools/reconfigured.py:")
    assert "reconfigures a stream itself" in problem


# Another method by its name, and a reconfigure by name given no encoding.
@pytest.mark.parametrize(
    "use",
    [
        'builtins.getattr(sys.stdout, "write")("text")',
        'getattr(sys.stdout, name)("text")',
        "builtins.getattr(sys.stderr, name)(line_buffering=True)",
        'operator.methodcaller("write", "text")(sys.stdout)',
        'operator.methodcaller("reconfigure", line_buffering=True)(sys.stdout)',
        'builtins.getattr(sys.stdout, "write")(encoding="utf-8")',
        'operator.methodcaller("write", encoding="utf-8")(sys.stdout)',
        'inspect.getattr_static(sys.stdout, "write")(sys.stdout, "text")',
        'vars(sys.stdout)["write"](encoding="utf-8")',
        'sys.stdout.__dict__["write"]("text")',
        "vars(sys.stdout)[name](line_buffering=True)",
    ],
)
def test_console_setup_policy_passes_another_method_looked_up_by_name(tmp_path: Path, use: str) -> None:
    imports = "import builtins\nimport inspect\nimport operator\nimport sys\n\n"
    write_fixture_tree(tmp_path, {"tools/plain.py": imports + use + "\n"})
    mark_skills(tmp_path)
    assert console_setup_problems(tmp_path) == []
