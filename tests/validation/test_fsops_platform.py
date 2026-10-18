"""Fixture tests for tests/validation/fsops_platform.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

from fsops_platform import _platform_names, filesystem_write_problems, platform_code_problems
from validation_support import write_fixture_tree

# Runs no test of its own; the test functions below check through its assertions, as the class does.
CHECK = unittest.TestCase()


class FsopsPlatformFixtures(unittest.TestCase):
    def test_filesystem_write_policy_resolves_assigned_callables_in_their_own_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deployer/assigned.py": "import shutil as sh\n"
                    "import os as o\n"
                    "from io import open as io_open\n"
                    "writer = open\n"
                    "copy = sh.copyfile\n"
                    "alias = copy\n"
                    'writer("x", "w").close()\n'
                    'alias("x", "y")\n'
                    "def write(path):\n"
                    "    writer = io_open\n"
                    '    writer(path, mode="a")\n'
                    "    descriptor = o.open\n"
                    "    descriptor(path, o.O_WRONLY)\n"
                    "def read(path):\n"
                    "    writer = io_open\n"
                    '    writer(path, "rb")\n'
                    "    descriptor = o.open\n"
                    "    descriptor(path, o.O_RDONLY | o.O_BINARY)\n"
                    "def methods(path, text):\n"
                    "    writer = path.open\n"
                    '    writer("w")\n'
                    "    rename = path.replace\n"
                    "    rename(target=path)\n"
                    "    replace = text.replace\n"
                    '    replace("x", "y")\n'
                    "descriptor = o.open\n"
                    "def local_flags(path):\n"
                    "    from os import O_RDONLY as read_flag\n"
                    "    descriptor(path, read_flag)\n",
                },
            )
            route = "; route it through deployer/fsops.py"
            self.assertEqual(
                [
                    f"deployer/assigned.py:7 writes with open{route}",
                    f"deployer/assigned.py:8 writes with shutil.copyfile{route}",
                    f"deployer/assigned.py:11 writes with open{route}",
                    f"deployer/assigned.py:13 writes with os.open{route}",
                    f"deployer/assigned.py:21 writes with open{route}",
                    f"deployer/assigned.py:23 writes with replace{route}",
                ],
                filesystem_write_problems(root),
            )

    def test_filesystem_write_policy_reads_writes_through_assignment_expressions_unpacking_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deployer/bound.py": "import os, shutil\n"  # 1
                    "if (writer := open):\n"  # 2
                    '    writer("x", "w")\n'  # 3
                    "remove, other = shutil.rmtree, print\n"  # 4
                    'remove("x")\n'  # 5
                    "def copy(source, copier=shutil.copyfile):\n"  # 6
                    '    copier(source, "y")\n'  # 7
                    "for unlink in (os.unlink,):\n"  # 8
                    '    unlink("x")\n'  # 9
                    "for either in (open, print):\n"  # 10
                    '    either("x", "w")\n',  # 11
                },
            )
            route = "; route it through deployer/fsops.py"
            self.assertEqual(
                [
                    f"deployer/bound.py:3 writes with open{route}",
                    f"deployer/bound.py:5 writes with shutil.rmtree{route}",
                    f"deployer/bound.py:7 writes with shutil.copyfile{route}",
                    f"deployer/bound.py:9 writes with os.unlink{route}",
                    "deployer/bound.py:11 cannot resolve callable alias either; "
                    "use a single binding to a known callable",
                ],
                filesystem_write_problems(root),
            )

    def test_assigned_path_open_preserves_read_only_modes_on_constructed_and_loop_receivers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deployer/paths.py": "from pathlib import Path\n"
                    'path = Path("x")\n'
                    "reader = path.open\n"
                    'reader("r")\n'
                    'reader("w")\n'
                    "def read(paths):\n"
                    "    for path in paths:\n"
                    "        reader = path.open\n"
                    '        reader("rb")\n'
                    '        reader("ab")\n',
                },
            )
            route = "; route it through deployer/fsops.py"
            self.assertEqual(
                [
                    f"deployer/paths.py:5 writes with open{route}",
                    f"deployer/paths.py:10 writes with open{route}",
                ],
                filesystem_write_problems(root),
            )

    def test_assigned_temporary_write_allowances_check_the_original_callable_and_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = (
                'FSOPS_ALLOWED = {"tempfile.mkdtemp": "a temporary directory", "open": "temporary output", '
                '"shutil.copyfile": "temporary copies"}\n'
                "import tempfile\n"
                "import shutil\n"
                "scratch = tempfile.mkdtemp\n"
                "writer = open\n"
                "copy = shutil.copyfile\n"
                "work = scratch()\n"
            )
            write_fixture_tree(
                root,
                {
                    "deployer/kept.py": prefix + 'writer(work, "w")\ncopy(work, work)\n',
                    "deployer/strayed.py": prefix
                    + 'writer("elsewhere", "w")\ncopy(work, "elsewhere")\nscratch(dir="elsewhere")\n',
                },
            )
            message = (
                "FSOPS_ALLOWED allows {} only under the system temporary directory, and this call may write elsewhere"
            )
            self.assertEqual(
                [
                    "deployer/strayed.py:8 " + message.format("open"),
                    "deployer/strayed.py:9 " + message.format("shutil.copyfile"),
                    "deployer/strayed.py:10 " + message.format("tempfile.mkdtemp"),
                ],
                filesystem_write_problems(root),
            )

    def test_an_allowed_write_must_land_under_the_system_temporary_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowance = (
                "FSOPS_ALLOWED = {\n"
                '    "tempfile.mkdtemp": "a working directory under the system temporary directory",\n'
                '    "tempfile.TemporaryFile": "captured output under the system temporary directory",\n'
                '    "shutil.rmtree": "removes only the directory mkdtemp made",\n'
                "}\n"
                "import shutil\n"
                "import tempfile\n"
                "from pathlib import Path\n"
                "\n"
                "\n"
            )
            files = {
                "deployer/kept.py": allowance + "def run():\n"
                '    work = Path(tempfile.mkdtemp(prefix="run-"))\n'
                "    with tempfile.TemporaryFile() as captured:\n"
                "        captured.write(b'x')\n"
                "    shutil.rmtree(work, ignore_errors=True)\n",
                "deployer/strayed.py": allowance + "def run(target):\n"
                '    work = tempfile.mkdtemp(dir="C:/managed")\n'  # 12: dir names another directory
                '    tempfile.TemporaryFile("w+b")\n'  # 13: a positional argument may reach dir
                "    shutil.rmtree(target)\n"  # 14: a parameter, not a directory mkdtemp made
                "    shutil.rmtree(work)\n"  # 15: bound to a call whose dir is elsewhere
                '    other = Path("C:/managed")\n'
                "    other = Path(tempfile.mkdtemp())\n"
                "    shutil.rmtree(other)\n",  # 18: bound to something else as well
            }
            write_fixture_tree(root, files)
            message = (
                "FSOPS_ALLOWED allows {} only under the system temporary directory, and this call may write elsewhere"
            )
            self.assertEqual(
                [
                    "deployer/strayed.py:12 " + message.format("tempfile.mkdtemp"),
                    "deployer/strayed.py:13 " + message.format("tempfile.TemporaryFile"),
                    "deployer/strayed.py:14 " + message.format("shutil.rmtree"),
                    "deployer/strayed.py:15 " + message.format("shutil.rmtree"),
                    "deployer/strayed.py:18 " + message.format("shutil.rmtree"),
                ],
                filesystem_write_problems(root),
            )

    def test_every_destination_of_an_allowed_write_must_be_under_the_system_temporary_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowance = (
                "FSOPS_ALLOWED = {\n"
                '    "tempfile.mkdtemp": "a working directory under the system temporary directory",\n'
                '    "shutil.copy": "copies only within the directory mkdtemp made",\n'
                '    "os.chmod": "changes only the directory mkdtemp made",\n'
                '    "open": "writes only inside the directory mkdtemp made",\n'
                '    "write_text": "writes only inside the directory mkdtemp made",\n'
                '    "rename": "renames only within the directory mkdtemp made",\n'
                "}\n"
                "import os\n"
                "import shutil\n"
                "import tempfile\n"
                "from pathlib import Path\n"
                "\n"
                "\n"
            )
            files = {
                # Flags, modes, and the text written name no destination.
                "deployer/kept.py": allowance + "def run():\n"
                "    work = Path(tempfile.mkdtemp())\n"
                "    copy = Path(tempfile.mkdtemp())\n"
                "    shutil.copy(work, copy, follow_symlinks=False)\n"
                "    os.chmod(work, 0o700)\n"
                '    with open(work, "w", encoding="utf-8") as handle:\n'
                '        handle.write("x")\n'
                '    work.write_text("x", encoding="utf-8")\n'
                "    work.rename(copy)\n",
                "deployer/strayed.py": allowance + "def run(elsewhere):\n"
                "    work = Path(tempfile.mkdtemp())\n"
                '    shutil.copy(work / "a", elsewhere)\n'  # 17: the source is not a name mkdtemp bound
                "    shutil.copy(work, elsewhere)\n"  # 18: the destination is second
                "    shutil.copy(work, dst=elsewhere)\n"  # 19: the destination is a keyword
                "    shutil.copy(work, *elsewhere)\n"  # 20: an unpacked argument may be anywhere
                '    with open(elsewhere, "w") as handle:\n'  # 21: the file is a parameter
                '        handle.write("x")\n'
                '    elsewhere.write_text("x")\n'  # 23: the receiver is the destination
                "    work.rename(elsewhere)\n"  # 24: the argument is the destination
                "    os.chmod(elsewhere, 0o700)\n",  # 25: the mode is no destination, but the path is
            }
            write_fixture_tree(root, files)
            message = (
                "FSOPS_ALLOWED allows {} only under the system temporary directory, and this call may write elsewhere"
            )
            self.assertEqual(
                [
                    "deployer/strayed.py:17 " + message.format("shutil.copy"),
                    "deployer/strayed.py:18 " + message.format("shutil.copy"),
                    "deployer/strayed.py:19 " + message.format("shutil.copy"),
                    "deployer/strayed.py:20 " + message.format("shutil.copy"),
                    "deployer/strayed.py:21 " + message.format("open"),
                    "deployer/strayed.py:23 " + message.format("write_text"),
                    "deployer/strayed.py:24 " + message.format("rename"),
                    "deployer/strayed.py:25 " + message.format("os.chmod"),
                ],
                filesystem_write_problems(root),
            )

    def test_filesystem_write_policy_detects_each_write_and_a_stale_allowance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def write(relative_path: str, text: str) -> None:
                (root / relative_path).parent.mkdir(parents=True, exist_ok=True)
                (root / relative_path).write_text(text, encoding="utf-8")

            write("deploy.py", "from pathlib import Path\nPath('x').write_text('y')\n")
            write("deployer/fsops.py", "import os\nos.mkdir('x')\n")
            write(
                "deployer/writes.py",
                '"""Docstrings may say os.remove or shutil.rmtree."""\n'
                "import os\n"
                "import shutil\n"
                "import tempfile\n"
                "from os import replace\n"
                "\n"
                "\n"
                "def write(path, text):\n"
                "    # Comments may say path.unlink().\n"
                "    path.touch()\n"
                "    os.makedirs(path)\n"
                "    shutil.rmtree(path)\n"
                "    tempfile.mkstemp()\n"
                "    replace(path, path)\n"
                '    open(path, "w").close()\n'
                '    open(path, mode="ab").close()\n'
                '    path.open("x").close()\n'
                "    open(path).read()\n"
                '    open(path, "rb").read()\n'
                '    path.open(encoding="utf-8").read()\n'
                '    text.replace("a", "b")\n'
                "    os.path.exists(path)\n"
                "    path.replace(path)\n"
                "    path.chmod(0o600)\n"
                "    os.chmod(path, 0o600)\n"
                '    text.replace("a", "b", 1)\n'
                "    moment.replace(year=1)\n"
                "    return path.mkdir(parents=True)\n",
            )
            write(
                "deployer/allowed.py",
                'FSOPS_ALLOWED = {"tempfile.mkdtemp": "a throwaway working directory outside every managed root"}\n'
                "import tempfile\n"
                "WORK = tempfile.mkdtemp()\n",
            )
            write("deployer/gone.py", 'FSOPS_ALLOWED = {"shutil.rmtree": "the module no longer calls it"}\n')
            write("deployer/unexplained.py", 'FSOPS_ALLOWED = {"touch": ""}\nPATH.touch()\n')
            write(
                "deployer/tested.py",
                "import unittest\n\n\nclass Fixtures(unittest.TestCase):\n    def test_x(self):\n"
                '        PATH.write_bytes(b"")\n',
            )
            write("tools/elsewhere.py", "from pathlib import Path\nPath('x').write_text('y')\n")
            route = "; route it through deployer/fsops.py"
            self.assertEqual(
                [
                    f"deploy.py:2 writes with write_text{route}",
                    f"deployer/unexplained.py:2 writes with touch{route}",
                    f"deployer/writes.py:5 writes with os.replace{route}",
                    f"deployer/writes.py:10 writes with touch{route}",
                    f"deployer/writes.py:11 writes with os.makedirs{route}",
                    f"deployer/writes.py:12 writes with shutil.rmtree{route}",
                    f"deployer/writes.py:13 writes with tempfile.mkstemp{route}",
                    f"deployer/writes.py:14 writes with os.replace{route}",
                    f"deployer/writes.py:15 writes with open{route}",
                    f"deployer/writes.py:16 writes with open{route}",
                    f"deployer/writes.py:17 writes with open{route}",
                    f"deployer/writes.py:23 writes with replace{route}",
                    f"deployer/writes.py:24 writes with chmod{route}",
                    f"deployer/writes.py:25 writes with os.chmod{route}",
                    f"deployer/writes.py:28 writes with mkdir{route}",
                    "deployer/gone.py: FSOPS_ALLOWED allows shutil.rmtree, which it no longer names",
                    "deployer/unexplained.py: FSOPS_ALLOWED must map each token to the reason it is allowed",
                ],
                filesystem_write_problems(root),
            )

    def test_filesystem_write_policy_resolves_imports_temporary_files_os_open_flags_and_held_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deployer/aliased.py": "import shutil as sh\n"  # 1
                    "import os as o\n"  # 2
                    "import tempfile\n"  # 3
                    "from tempfile import TemporaryFile as Scratch\n"  # 4
                    "from os import unlink as remove_file\n"  # 5
                    "\n"  # 6
                    "\n"  # 7
                    "def clean(path, kind, flags):\n"  # 8
                    "    sh.rmtree(path)\n"  # 9
                    "    o.replace(path, path)\n"  # 10
                    "    remove_file(path)\n"  # 11
                    "    tempfile.TemporaryFile()\n"  # 12
                    "    tempfile.SpooledTemporaryFile()\n"  # 13
                    "    Scratch()\n"  # 14
                    "    o.utime(path)\n",  # 15
                    "deployer/opened.py": "import io\n"  # 1
                    "import os\n"  # 2
                    "from os import O_CREAT, O_RDONLY\n"  # 3
                    'APPEND = "a"\n'  # 4
                    'READ = "rb"\n'  # 5
                    'EITHER = "r"\n'  # 6
                    'EITHER = "w"\n'  # 7
                    "\n"  # 8
                    "\n"  # 9
                    "def opened(path, mode, flags):\n"  # 10
                    "    os.open(path, os.O_WRONLY | os.O_TRUNC)\n"  # 11
                    "    os.open(path, flags)\n"  # 12
                    "    os.open(path, O_RDONLY | O_CREAT)\n"  # 13
                    "    os.open(path, 0o1)\n"  # 14
                    "    os.open(path, flags=os.O_RDWR)\n"  # 15
                    "    open(path, mode)\n"  # 16
                    "    open(path, APPEND)\n"  # 17
                    "    io.open(path, mode=EITHER)\n"  # 18
                    "    path.open(mode)\n"  # 19
                    '    path.open(f"{mode}b")\n'  # 20
                    "    os.open(path, os.O_RDONLY | os.O_BINARY)\n"  # 21
                    "    os.open(path, O_RDONLY)\n"  # 22
                    "    os.open(path, 0)\n"  # 23
                    "    open(path, READ)\n"  # 24
                    "    open(path)\n"  # 25
                    '    path.open("r", encoding="utf-8")\n'  # 26
                    "    shutil.which(path)\n",  # 27
                    "deployer/allowed.py": 'FSOPS_ALLOWED = {"tempfile.TemporaryFile": "a throwaway capture"}\n'
                    "from tempfile import TemporaryFile\n"
                    "with TemporaryFile() as captured:\n"
                    "    pass\n",
                },
            )
            route = "; route it through deployer/fsops.py"
            self.assertEqual(
                [
                    f"deployer/aliased.py:4 writes with tempfile.TemporaryFile{route}",
                    f"deployer/aliased.py:5 writes with os.unlink{route}",
                    f"deployer/aliased.py:9 writes with shutil.rmtree{route}",
                    f"deployer/aliased.py:10 writes with os.replace{route}",
                    f"deployer/aliased.py:11 writes with os.unlink{route}",
                    f"deployer/aliased.py:12 writes with tempfile.TemporaryFile{route}",
                    f"deployer/aliased.py:13 writes with tempfile.SpooledTemporaryFile{route}",
                    f"deployer/aliased.py:14 writes with tempfile.TemporaryFile{route}",
                    f"deployer/aliased.py:15 writes with os.utime{route}",
                    f"deployer/opened.py:11 writes with os.open{route}",
                    f"deployer/opened.py:12 writes with os.open{route}",
                    f"deployer/opened.py:13 writes with os.open{route}",
                    f"deployer/opened.py:14 writes with os.open{route}",
                    f"deployer/opened.py:15 writes with os.open{route}",
                    f"deployer/opened.py:16 writes with open{route}",
                    f"deployer/opened.py:17 writes with open{route}",
                    f"deployer/opened.py:18 writes with open{route}",
                    f"deployer/opened.py:19 writes with open{route}",
                    f"deployer/opened.py:20 writes with open{route}",
                ],
                filesystem_write_problems(root),
            )

    def test_filesystem_write_policy_reads_a_keyword_target_and_counts_an_unpacked_mode(self) -> None:
        # A keyword spelling of a write is the same write, and a mode the policy cannot read may be a writing one.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deployer/spelled.py": "import io\n"  # 1
                    "\n"  # 2
                    "\n"  # 3
                    "def spelled(path, other, options, arguments):\n"  # 4
                    "    path.replace(target=other)\n"  # 5
                    "    path.replace(**options)\n"  # 6
                    '    open(path, **{"mode": "w"})\n'  # 7
                    "    open(path, **options)\n"  # 8
                    "    io.open(*arguments)\n"  # 9
                    "    path.open(**options)\n"  # 10
                    '    open(path, "r", **options)\n'  # 11
                    '    open(path, mode="r", **options)\n'  # 12
                    '    text.replace("a", "b", **options)\n'  # 13
                    '    text.replace("a", "b", count=1)\n'  # 14
                    "    moment.replace(year=1)\n"  # 15
                    '    path.open("rb", **options)\n',  # 16
                },
            )
            route = "; route it through deployer/fsops.py"
            self.assertEqual(
                [
                    f"deployer/spelled.py:5 writes with replace{route}",
                    f"deployer/spelled.py:6 writes with replace{route}",
                    f"deployer/spelled.py:7 writes with open{route}",
                    f"deployer/spelled.py:8 writes with open{route}",
                    f"deployer/spelled.py:9 writes with open{route}",
                    f"deployer/spelled.py:10 writes with open{route}",
                ],
                filesystem_write_problems(root),
            )

    def test_an_allowed_rename_with_a_keyword_or_unpacked_target_must_stay_in_the_temporary_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowance = (
                "FSOPS_ALLOWED = {\n"
                '    "tempfile.mkdtemp": "a working directory under the system temporary directory",\n'
                '    "replace": "replaces only within the directory mkdtemp made",\n'
                '    "rename": "renames only within the directory mkdtemp made",\n'
                "}\n"
                "import tempfile\n"
                "from pathlib import Path\n"
                "\n"
                "\n"
            )
            write_fixture_tree(
                root,
                {
                    "deployer/kept.py": allowance + "def run():\n"
                    "    work = Path(tempfile.mkdtemp())\n"
                    "    copy = Path(tempfile.mkdtemp())\n"
                    "    work.replace(target=copy)\n"
                    "    work.rename(target=copy)\n",
                    "deployer/strayed.py": allowance + "def run(elsewhere, options):\n"
                    "    work = Path(tempfile.mkdtemp())\n"
                    "    work.replace(target=elsewhere)\n"  # 12: the keyword names the destination
                    "    work.rename(target=elsewhere)\n"  # 13
                    "    work.rename(**options)\n",  # 14: an unpacking may hold any destination
                },
            )
            message = (
                "FSOPS_ALLOWED allows {} only under the system temporary directory, and this call may write elsewhere"
            )
            self.assertEqual(
                [
                    "deployer/strayed.py:12 " + message.format("replace"),
                    "deployer/strayed.py:13 " + message.format("rename"),
                    "deployer/strayed.py:14 " + message.format("rename"),
                ],
                filesystem_write_problems(root),
            )

    def test_platform_policy_resolves_aliased_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deployer/aliased.py": "import sys as system\nimport os as o\n\n"
                    "WINDOWS = system.platform == 'win32'\nNAME = o.name\nSEPARATOR = o.sep\n",
                    "deployer/plain.py": "import sys\n\nVERSION = sys.version_info\n",
                },
            )
            move = "; move it behind deployer/platform_support.py"
            self.assertEqual(
                [f"deployer/aliased.py:4 names sys.platform{move}", f"deployer/aliased.py:5 names os.name{move}"],
                platform_code_problems(root),
            )

    def test_platform_names_lists_each_token_a_module_names_in_visit_order(self) -> None:
        """_platform_names on its own, before platform_code_problems drops duplicates and sorts: every node kind, the
        skipped tables, docstrings, and test cases."""
        source = (
            '"""Module docstring: sys.platform, LOCALAPPDATA, cygpath."""\n'  # 1
            "import os, ctypes.wintypes\n"  # 2
            "import msvcrt as m\n"  # 3
            "from winreg import HKEY\n"  # 4
            "from os import name, chmod, path\n"  # 5
            "from sys import platform\n"  # 6
            "from . import helper\n"  # 7
            "from ctypes import windll, chmod\n"  # 8
            "\n"  # 9
            'PLATFORM_ALLOWED = {"cygpath": "a reason"}\n'  # 10
            'TABLE: dict = {"USERPROFILE": "cygpath"}\n'  # 11
            'holder.TABLE = "USERPROFILE"\n'  # 12
            'OTHER, TABLE2 = "APPDATA", "cygpath"\n'  # 13
            "\n"  # 14
            "\n"  # 15
            "class Plain(Base):\n"  # 16
            '    """Class docstring: os.name, USERPROFILE."""\n'  # 17
            "\n"  # 18
            "    def method(self):\n"  # 19
            '        """USERPROFILE"""\n'  # 20
            "        return sys.platform, os.name, os.startfile, os.sep\n"  # 21
            "\n"  # 22
            "\n"  # 23
            "async def run():\n"  # 24
            '    """cygpath"""\n'  # 25
            '    subprocess.run(["cygpath", "-w"], creationflags=1, text=True)\n'  # 26
            "    p.chmod(1), st.st_file_attributes, x.y.fchmod, os.lchmod, os.environ\n"  # 27
            '    "cygpath -u and cygpath -w, not cygpaths"\n'  # 28
            '    ("LOCALAPPDATA", "ProgramFiles", "localappdata", "%USERPROFILE%", "cygpathx")\n'  # 29
            '    "APPDATA"\n'  # 30
            "\n"  # 31
            "\n"  # 32
            "class Tests(unittest.TestCase):\n"  # 33
            "    def test(self):\n"  # 34
            '        return sys.platform, "USERPROFILE"\n'  # 35
            "\n"  # 36
            "\n"  # 37
            "class MoreTests(TestCase):\n"  # 38
            "    value = os.name\n"  # 39
            "class GitTests(unittest.TestCase): value = os.name\n"  # 40
            "\n"  # 41
            "class Generic(Base[int]):\n"  # 42
            '    value = "cygpath"\n'  # 43
        )
        common_head = [
            (2, "ctypes"),
            (3, "msvcrt"),
            (4, "winreg"),
            (5, "os.name"),
            (5, "chmod"),
            (6, "sys.platform"),
            (8, "ctypes"),
        ]
        common_tail = [
            (12, "USERPROFILE"),
            (13, "APPDATA"),
            (13, "cygpath"),
            (21, "sys.platform"),
            (21, "os.name"),
            (21, "os.startfile"),
            (26, "cygpath"),
            (26, "creationflags"),
            (27, "chmod"),
            (27, "st_file_attributes"),
            (27, "fchmod"),
            (27, "lchmod"),
            (28, "cygpath"),
            (29, "LOCALAPPDATA"),
            (29, "ProgramFiles"),
            (29, "localappdata"),
            (30, "APPDATA"),
            (43, "cygpath"),
        ]
        self.assertEqual(
            [*common_head, *common_tail],
            _platform_names(ast.parse(source), {"PLATFORM_ALLOWED", "TABLE"}),
        )
        self.assertEqual(
            [*common_head, (10, "cygpath"), (11, "USERPROFILE"), (11, "cygpath"), *common_tail],
            _platform_names(ast.parse(source), set()),
        )

    def test_platform_policy_matches_environment_names_in_any_case(self) -> None:
        # Windows compares environment names ignoring case, so each spelling reads the same variable.
        reads = (
            "import os\n"
            "\n"
            "\n"
            "def locations():\n"
            '    return os.environ.get("PROGRAMFILES"), os.environ["git_bash"], os.getenv("UserProfile")\n'
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, {"deployer/strayed.py": reads, "deployer/platform_support.py": reads})
            move = "; move it behind deployer/platform_support.py"
            self.assertEqual(
                [
                    f"deployer/strayed.py:5 names PROGRAMFILES{move}",
                    f"deployer/strayed.py:5 names UserProfile{move}",
                    f"deployer/strayed.py:5 names git_bash{move}",
                ],
                platform_code_problems(root),
            )

    def test_platform_code_policy_detects_each_token_and_a_stale_allowance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def write(relative_path: str, text: str) -> None:
                (root / relative_path).parent.mkdir(parents=True, exist_ok=True)
                (root / relative_path).write_text(text, encoding="utf-8")

            write("deploy.py", "import sys\n\nif sys.platform == 'win32':\n    pass\n")
            write(
                "deployer/files.py",
                '"""Docstrings may say os.chmod, USERPROFILE, or cygpath."""\n'
                "import os\n"
                "from os import name\n"
                "import ctypes.wintypes\n"
                "from msvcrt import getch\n"
                "\n"
                "\n"
                "def private(path, environment):\n"
                '    """So may a function\'s: LOCALAPPDATA."""\n'
                "    # And comments: sys.platform, os.chmod, cygpath.\n"
                "    path.chmod(0o600)\n"
                '    return environment.get("LOCALAPPDATA"), {"ProgramFiles": os.name}\n',
            )
            write("deployer/platform_support.py", "import sys\nPLATFORM = sys.platform\n")
            write(
                "tools/shell.py",
                "import subprocess\n"
                'SNIPPET = "path=$(cygpath -m \\"$0\\")"\n'
                'OTHER = "cygpathology is not a command"\n'
                'subprocess.run(["x"], creationflags=0, check=False)\n',
            )
            write(
                "tools/allowed.py",
                'PLATFORM_ALLOWED = {"cygpath": "it falls back when cygpath is absent"}\n'
                'SNIPPET = "command -v cygpath && cygpath -m x"\n'
                'HOME = "USERPROFILE"\n',
            )
            write("tools/gone.py", 'PLATFORM_ALLOWED = {"cygpath": "the module no longer names it"}\n')
            write("tools/unexplained.py", 'PLATFORM_ALLOWED = {"cygpath": " "}\nSNIPPET = "cygpath -m x"\n')
            # The runner and every module of the validation package are scanned; the policy module's own table is not.
            write("tests/run_validation.py", 'HOME = "APPDATA"\n')
            write("tests/validation/job_pool.py", "import sys\nPLATFORM = sys.platform\n")
            write(
                "tests/validation/fsops_platform.py",
                'PLATFORM_TOKENS = {"variable": ("USERPROFILE",), "command": ("cygpath",)}\n'
                "import unittest\n"
                "\n"
                "\n"
                "class Fixtures(unittest.TestCase):\n"
                "    def test_names_tokens(self):\n"
                '        self.assertEqual("USERPROFILE", "USERPROFILE")\n'
                "\n"
                "\n"
                'HOME = "USERPROFILE"\n',
            )
            write("tests/deployer/test_platform.py", "import sys\nPLATFORM = sys.platform\n")
            write("skills/alpha/scripts/tool.py", "import sys\nPLATFORM = sys.platform\n")
            move = "; move it behind deployer/platform_support.py"
            self.assertEqual(
                [
                    f"deploy.py:3 names sys.platform{move}",
                    f"deployer/files.py:3 names os.name{move}",
                    f"deployer/files.py:4 names ctypes{move}",
                    f"deployer/files.py:5 names msvcrt{move}",
                    f"deployer/files.py:11 names chmod{move}",
                    f"deployer/files.py:12 names LOCALAPPDATA{move}",
                    f"deployer/files.py:12 names ProgramFiles{move}",
                    f"deployer/files.py:12 names os.name{move}",
                    f"tests/run_validation.py:1 names APPDATA{move}",
                    f"tests/validation/fsops_platform.py:10 names USERPROFILE{move}",
                    f"tests/validation/job_pool.py:2 names sys.platform{move}",
                    f"tools/allowed.py:3 names USERPROFILE{move}",
                    f"tools/shell.py:2 names cygpath{move}",
                    f"tools/shell.py:4 names creationflags{move}",
                    f"tools/unexplained.py:2 names cygpath{move}",
                    "tools/gone.py: PLATFORM_ALLOWED allows cygpath, which it no longer names",
                    "tools/unexplained.py: PLATFORM_ALLOWED must map each token to the reason it is allowed",
                ],
                platform_code_problems(root),
            )

    def test_platform_policy_finds_unix_modules_os_queries_signals_and_process_keywords_through_aliases(self) -> None:
        source = (
            "import fcntl\n"  # 1
            "import pwd as passwords\n"  # 2
            "import grp, termios, tty, pty, posix, nt\n"  # 3
            "from resource import getrlimit\n"  # 4
            "from platform import system as os_name\n"  # 5
            "from os import uname\n"  # 6
            "import platform as p\n"  # 7
            "import os as o\n"  # 8
            "import sys\n"  # 9
            "import signal\n"  # 10
            "import subprocess\n"  # 11
            "from .resource import local\n"  # 12: a module of this repository, not the platform's
            "from . import pwd\n"  # 13
            "\n"  # 14
            "\n"  # 15
            "def describe(command, pid):\n"  # 16
            "    p.system(), p.machine(), p.release(), p.python_version()\n"  # 17
            "    o.uname(), sys.getwindowsversion(), o.getuid()\n"  # 18
            "    o.kill(pid, signal.SIGKILL), o.kill(pid, signal.SIGTERM)\n"  # 19
            "    subprocess.run(command, start_new_session=True, check=False)\n"  # 20
            "    subprocess.run(command, preexec_fn=None, process_group=0, startupinfo=None)\n"  # 21
            "    return o.open(command, o.O_RDONLY | o.O_BINARY)\n"  # 22
        )
        sanctioned = (
            'PLATFORM_ALLOWED = {"platform.system": "reports the platform a run was made on"}\n'
            "import platform\n"
            "SYSTEM = platform.system()\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deployer/strayed.py": source,
                    "deployer/platform_support.py": source,
                    "tools/sanctioned.py": sanctioned,
                },
            )
            move = "; move it behind deployer/platform_support.py"
            expected = [
                (1, "fcntl"),
                (2, "pwd"),
                (3, "grp"),
                (3, "nt"),
                (3, "posix"),
                (3, "pty"),
                (3, "termios"),
                (3, "tty"),
                (4, "resource"),
                (5, "platform.system"),
                (6, "os.uname"),
                (17, "platform.machine"),
                (17, "platform.release"),
                (17, "platform.system"),
                (18, "os.getuid"),
                (18, "os.uname"),
                (18, "sys.getwindowsversion"),
                (19, "signal.SIGKILL"),
                (20, "start_new_session"),
                (21, "preexec_fn"),
                (21, "process_group"),
                (21, "startupinfo"),
                (22, "os.O_BINARY"),
            ]
            self.assertEqual(
                [f"deployer/strayed.py:{line} names {token}{move}" for line, token in expected],
                platform_code_problems(root),
            )

    def test_platform_policy_finds_executable_extensions_stripped_appended_or_compared(self) -> None:
        assumed = (
            "import os\n"  # 1
            "from pathlib import Path\n"  # 2
            "\n"  # 3
            "\n"  # 4
            "def program(name, path):\n"  # 5
            '    name.removesuffix(".exe")\n'  # 6
            '    name.lower().endswith((".cmd", ".BAT"))\n'  # 7
            '    Path(name).with_suffix(".exe")\n'  # 8
            '    path.suffix == ".exe"\n'  # 9
            '    os.path.splitext(name)[1].casefold() in frozenset({".bat", ".py"})\n'  # 10
            '    name + ".exe"\n'  # 11
            '    f"{name}.cmd --version"\n'  # 12
            '    name.replace(".exe", "")\n'  # 13
            '    name.rstrip(".exe")\n'  # 14
            '    path.name == "bash.exe"\n'  # 15
        )
        # Strings that only hold an extension, and scripts compared by type, assume no platform.
        innocent = (
            "from pathlib import Path\n"
            'READER = Path(__file__).with_name("worktrees-powershell.ps1")\n'
            'PATTERN = "*.exe"\n'
            'HINT = "set GIT_BASH to its bash.exe"\n'
            'CASES = "bash | bash.exe | sh | sh.exe"\n'
            "\n"
            "\n"
            "def kind(path, name):\n"
            '    path.suffix == ".ps1"\n'
            '    name.endswith(".py")\n'
            '    name + ".executable"\n'
            '    f"{name}.exec"\n'
            '    f"{name} is not bash.exe"\n'
            '    "bash.exe" + name\n'
        )
        sanctioned = (
            'PLATFORM_ALLOWED = {".exe": "names the program the Windows installer lays down"}\n'
            'PROGRAM = "git" + ".exe"\n'
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "tools/assumed.py": assumed,
                    "deployer/platform_support.py": assumed,
                    "tools/innocent.py": innocent,
                    "tools/sanctioned.py": sanctioned,
                },
            )
            move = "; move it behind deployer/platform_support.py"
            expected = [
                (6, ".exe"),
                (7, ".bat"),
                (7, ".cmd"),
                (8, ".exe"),
                (9, ".exe"),
                (10, ".bat"),
                (11, ".exe"),
                (12, ".cmd"),
                (13, ".exe"),
                (14, ".exe"),
                (15, ".exe"),
            ]
            self.assertEqual(
                [f"tools/assumed.py:{line} names {token}{move}" for line, token in expected],
                platform_code_problems(root),
            )

    def test_platform_policy_finds_windows_process_names_variables_ntpath_taskkill_proc_and_dict_keywords(
        self,
    ) -> None:
        strayed = (
            "import importlib\n"  # 1
            "import ntpath\n"  # 2
            "import os\n"  # 3
            "import subprocess as sp\n"  # 4
            "from ntpath import join\n"  # 5
            "from subprocess import CREATE_NO_WINDOW, STARTUPINFO, run\n"  # 6
            "\n"  # 7
            "\n"  # 8
            "def start(command, pid):\n"  # 9
            "    sp.CREATE_NEW_PROCESS_GROUP | sp.DETACHED_PROCESS | sp.HIGH_PRIORITY_CLASS\n"  # 10
            "    sp.STARTF_USESHOWWINDOW, sp.SW_HIDE, sp.STD_OUTPUT_HANDLE\n"  # 11
            '    os.environ["PATHEXT"], os.environ.get("SystemRoot"), os.getenv("COMSPEC")\n'  # 12
            '    run(["taskkill", "/F", "/T", "/PID", str(pid)], check=False)\n'  # 13
            '    open("/proc/self/stat"), f"/proc/{pid}/stat", "cat /proc/1/cmdline", "/proc"\n'  # 14
            '    sp.run(command, **{"creationflags": 0, "start_new_session": True})\n'  # 15
            "    options = {}\n"  # 16
            '    options["startupinfo"] = None\n'  # 17
            '    options.setdefault("preexec_fn", None)\n'  # 18
            '    getattr(sp, "CREATE_NO_WINDOW", 0), hasattr(os, "fork"), getattr(os, "getuid")()\n'  # 19
            '    importlib.import_module("ntpath"), __import__("winreg")\n'  # 20
            '    importlib.import_module(name="msvcrt")\n'  # 21
        )
        # Portable subprocess names, other strings, and a keyword dict platform_support builds assume no platform.
        innocent = (
            "import importlib\n"
            "import os\n"
            "import subprocess\n"
            "\n"
            "from deployer import platform_support\n"
            "\n"
            "\n"
            "def start(command, holder):\n"
            "    subprocess.run(command, stdout=subprocess.STDOUT, stderr=subprocess.DEVNULL, check=False)\n"
            "    subprocess.run(command, **platform_support.process_options())\n"
            '    subprocess.run(command, **{"check": False, "text": True})\n'
            '    os.environ["PATH"], os.environ.get("SYSTEMROOTS"), "PATHEXT is a Windows variable"\n'
            '    "taskkills", "ntpath", "/process", "lib/proc/x", "~proc", "/procfs"\n'
            '    "creationflag", "the creationflags keyword"\n'
            '    getattr(holder, "CREATE_NO_WINDOW"), getattr(subprocess, "run"), hasattr(os, "sep")\n'
            '    importlib.import_module("json"), getattr(subprocess, command)\n'
        )
        sanctioned = (
            "PLATFORM_ALLOWED = {\n"
            '    "/proc": "reads the Linux process table the canary checks",\n'
            '    "subprocess.CREATE_NO_WINDOW": "starts the Windows runner without a console",\n'
            '    "COMSPEC": "names the shell the runner reports",\n'
            "}\n"
            "import subprocess\n"
            'STAT = "/proc/self/stat"\n'
            "FLAGS = subprocess.CREATE_NO_WINDOW\n"
            'SHELL = "COMSPEC"\n'
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "tools/strayed.py": strayed,
                    "deployer/platform_support.py": strayed,
                    "tools/innocent.py": innocent,
                    "tools/sanctioned.py": sanctioned,
                },
            )
            move = "; move it behind deployer/platform_support.py"
            expected = [
                (2, "ntpath"),
                (5, "ntpath"),
                (6, "subprocess.CREATE_NO_WINDOW"),
                (6, "subprocess.STARTUPINFO"),
                (10, "subprocess.CREATE_NEW_PROCESS_GROUP"),
                (10, "subprocess.DETACHED_PROCESS"),
                (10, "subprocess.HIGH_PRIORITY_CLASS"),
                (11, "subprocess.STARTF_USESHOWWINDOW"),
                (11, "subprocess.STD_OUTPUT_HANDLE"),
                (11, "subprocess.SW_HIDE"),
                (12, "COMSPEC"),
                (12, "PATHEXT"),
                (12, "SystemRoot"),
                (13, "taskkill"),
                (14, "/proc"),
                (15, "creationflags"),
                (15, "start_new_session"),
                (17, "startupinfo"),
                (18, "preexec_fn"),
                (19, "os.fork"),
                (19, "os.getuid"),
                (19, "subprocess.CREATE_NO_WINDOW"),
                (20, "ntpath"),
                (20, "winreg"),
                (21, "msvcrt"),
            ]
            self.assertEqual(
                [f"tools/strayed.py:{line} names {token}{move}" for line, token in expected],
                platform_code_problems(root),
            )

    def test_filesystem_write_policy_counts_archive_extraction_creation_and_writable_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deployer/archives.py": "import bz2\n"  # 1
                    "import dbm\n"  # 2
                    "import gzip\n"  # 3
                    "import lzma\n"  # 4
                    "import shelve\n"  # 5
                    "import shutil\n"  # 6
                    "import sqlite3\n"  # 7
                    "import tarfile\n"  # 8
                    "import zipfile as zf\n"  # 9
                    "from shutil import unpack_archive as unpack\n"  # 10
                    "from zipfile import ZipFile\n"  # 11
                    "from . import fsops\n"  # 12
                    "\n"  # 13
                    "\n"  # 14
                    "def run(archive, target, mode, options):\n"  # 15
                    "    with tarfile.open(archive) as handle:\n"  # 16: reads
                    '        handle.extractall(target, filter="data")\n'  # 17
                    '        handle.extract("member", target)\n'  # 18
                    '        handle.extractfile("member")\n'  # 19: reads
                    "    shutil.unpack_archive(archive, target)\n"  # 20
                    "    unpack(archive, extract_dir=target)\n"  # 21
                    '    shutil.make_archive(target, "zip", archive)\n'  # 22
                    '    ZipFile(archive, "w")\n'  # 23
                    '    zf.ZipFile(archive, mode="a")\n'  # 24
                    '    ZipFile(archive, "x")\n'  # 25
                    "    ZipFile(archive, mode)\n"  # 26: a mode the policy cannot read
                    "    ZipFile(archive, **options)\n"  # 27
                    "    ZipFile(archive)\n"  # 28: reads
                    '    zf.ZipFile(archive, mode="r").namelist()\n'  # 29: reads
                    '    tarfile.open(archive, "w:gz")\n'  # 30
                    '    tarfile.open(archive, "x|xz")\n'  # 31
                    '    tarfile.open(archive, "r:xz")\n'  # 32: reads, though its compression says x
                    '    tarfile.open(name=archive, mode="r|gz")\n'  # 33: reads
                    '    gzip.open(archive, "wb")\n'  # 34
                    "    gzip.open(archive)\n"  # 35: reads
                    '    bz2.open(archive, "rt")\n'  # 36: reads
                    '    lzma.LZMAFile(archive, mode="x")\n'  # 37
                    "    shelve.open(archive)\n"  # 38: creates the database by default
                    '    shelve.open(archive, "r")\n'  # 39: reads
                    "    dbm.open(archive)\n"  # 40: reads by default
                    '    dbm.open(archive, flag="c")\n'  # 41
                    "    sqlite3.connect(archive)\n"  # 42
                    "    fsops.extract_archive(archive, target)\n"  # 43: the sanctioned extraction
                    "\n"  # 44
                    "\n"  # 45
                    "def aliased(handle, target):\n"  # 46
                    "    extract = handle.extractall\n"  # 47
                    "    extract(target)\n",  # 48
                },
            )
            route = "; route it through deployer/fsops.py"
            expected = [
                (10, "shutil.unpack_archive"),
                (17, "extractall"),
                (18, "extract"),
                (20, "shutil.unpack_archive"),
                (21, "shutil.unpack_archive"),
                (22, "shutil.make_archive"),
                (23, "zipfile.ZipFile"),
                (24, "zipfile.ZipFile"),
                (25, "zipfile.ZipFile"),
                (26, "zipfile.ZipFile"),
                (27, "zipfile.ZipFile"),
                (30, "tarfile.open"),
                (31, "tarfile.open"),
                (34, "gzip.open"),
                (37, "lzma.LZMAFile"),
                (38, "shelve.open"),
                (41, "dbm.open"),
                (42, "sqlite3.connect"),
                (48, "extractall"),
            ]
            self.assertEqual(
                [f"deployer/archives.py:{line} writes with {token}{route}" for line, token in expected],
                filesystem_write_problems(root),
            )

    def test_an_allowed_archive_write_must_land_under_the_system_temporary_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowance = (
                "FSOPS_ALLOWED = {\n"
                '    "tempfile.mkdtemp": "a working directory under the system temporary directory",\n'
                '    "extractall": "extracts only into the directory mkdtemp made",\n'
                '    "extract": "extracts only into the directory mkdtemp made",\n'
                '    "shutil.unpack_archive": "unpacks only into the directory mkdtemp made",\n'
                '    "shutil.make_archive": "archives only into the directory mkdtemp made",\n'
                '    "zipfile.ZipFile": "writes only an archive in the directory mkdtemp made",\n'
                "}\n"
                "import shutil\n"
                "import tempfile\n"
                "import zipfile\n"
                "from pathlib import Path\n"
                "\n"
                "\n"
            )
            write_fixture_tree(
                root,
                {
                    # The archive read, the format, and the directory archived name no destination.
                    "deployer/kept.py": allowance + "def run(handle, archive):\n"
                    "    work = Path(tempfile.mkdtemp())\n"
                    '    handle.extractall(work, filter="data")\n'
                    "    handle.extractall(path=work)\n"
                    '    handle.extract("member", work)\n'
                    "    shutil.unpack_archive(archive, work)\n"
                    '    shutil.make_archive(work, "zip", archive)\n'
                    '    zipfile.ZipFile(work, "w")\n',
                    "deployer/strayed.py": allowance + "def run(handle, archive, elsewhere, options):\n"
                    "    work = Path(tempfile.mkdtemp())\n"
                    "    handle.extractall()\n"  # 17: the working directory
                    "    handle.extractall(elsewhere)\n"  # 18
                    "    handle.extractall(**options)\n"  # 19: an unpacking may hold any path
                    '    handle.extract("member")\n'  # 20: the working directory
                    '    handle.extract("member", path=elsewhere)\n'  # 21
                    "    shutil.unpack_archive(work)\n"  # 22: the working directory
                    "    shutil.unpack_archive(work, elsewhere)\n"  # 23
                    '    shutil.make_archive(elsewhere, "zip", work)\n'  # 24
                    '    zipfile.ZipFile(elsewhere, "w")\n',  # 25
                },
            )
            message = (
                "FSOPS_ALLOWED allows {} only under the system temporary directory, and this call may write elsewhere"
            )
            self.assertEqual(
                [
                    "deployer/strayed.py:17 " + message.format("extractall"),
                    "deployer/strayed.py:18 " + message.format("extractall"),
                    "deployer/strayed.py:19 " + message.format("extractall"),
                    "deployer/strayed.py:20 " + message.format("extract"),
                    "deployer/strayed.py:21 " + message.format("extract"),
                    "deployer/strayed.py:22 " + message.format("shutil.unpack_archive"),
                    "deployer/strayed.py:23 " + message.format("shutil.unpack_archive"),
                    "deployer/strayed.py:24 " + message.format("shutil.make_archive"),
                    "deployer/strayed.py:25 " + message.format("zipfile.ZipFile"),
                ],
                filesystem_write_problems(root),
            )

    def test_a_temporary_directory_is_kept_only_through_calls_that_keep_its_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowance = (
                "FSOPS_ALLOWED = {\n"
                '    "tempfile.mkdtemp": "a working directory under the system temporary directory",\n'
                '    "shutil.rmtree": "removes only the directory mkdtemp made",\n'
                "}\n"
                "import os\n"
                "import shutil\n"
                "import tempfile\n"
                "from pathlib import Path as P\n"
                "\n"
                "\n"
            )
            files = {
                "deployer/kept.py": allowance + "def run():\n"
                "    work = P(tempfile.mkdtemp())\n"
                "    named = os.fspath(P(tempfile.mkdtemp()))\n"
                "    text = str(tempfile.mkdtemp())\n"
                "    shutil.rmtree(work)\n"
                "    shutil.rmtree(named)\n"
                "    shutil.rmtree(text)\n",
                "deployer/strayed.py": allowance + "def unrelated_path(_):\n"
                '    return "/some/non-temporary/directory"\n'
                "def run():\n"
                "    path = unrelated_path(tempfile.mkdtemp())\n"
                "    shutil.rmtree(path)\n"  # 15: a function of the module's own may return any path
                "    joined = os.path.join(tempfile.mkdtemp())\n"
                "    shutil.rmtree(joined)\n"  # 17: a call that is not one that keeps the path
                "    wrapped = P(tempfile.mkdtemp(), 'x')\n"
                "    shutil.rmtree(wrapped)\n",  # 19: a second argument changes the path
                # A local function named Path is not pathlib's.
                "deployer/shadowed.py": allowance.replace("from pathlib import Path as P\n", "") + "def P(_):\n"
                '    return "/some/non-temporary/directory"\n'
                "def run():\n"
                "    work = P(tempfile.mkdtemp())\n"
                "    shutil.rmtree(work)\n",  # 14
            }
            write_fixture_tree(root, files)
            message = (
                "FSOPS_ALLOWED allows {} only under the system temporary directory, and this call may write elsewhere"
            )
            self.assertEqual(
                [
                    "deployer/shadowed.py:14 " + message.format("shutil.rmtree"),
                    "deployer/strayed.py:15 " + message.format("shutil.rmtree"),
                    "deployer/strayed.py:17 " + message.format("shutil.rmtree"),
                    "deployer/strayed.py:19 " + message.format("shutil.rmtree"),
                ],
                filesystem_write_problems(root),
            )

    def test_filesystem_write_policy_counts_file_io_logging_files_downloads_in_place_input_and_bytecode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deployer/writes.py": "import compileall\n"
                    "import fileinput\n"
                    "import io\n"
                    "import logging\n"
                    "import py_compile\n"
                    "import urllib.request\n"
                    "from io import FileIO\n"
                    "from logging import handlers\n"
                    "\n"
                    "\n"
                    "def write(path, url, level, mode):\n"
                    '    io.FileIO(path, "w")\n'  # 12
                    '    FileIO(path, mode="ab")\n'  # 13
                    "    io.FileIO(path, mode)\n"  # 14: a mode the policy cannot read
                    "    logging.FileHandler(path)\n"  # 15
                    "    handlers.RotatingFileHandler(path)\n"  # 16
                    "    handlers.TimedRotatingFileHandler(path)\n"  # 17
                    "    handlers.WatchedFileHandler(path)\n"  # 18
                    "    logging.basicConfig(filename=path)\n"  # 19
                    "    urllib.request.urlretrieve(url, path)\n"  # 20
                    "    fileinput.input(path, True)\n"  # 21
                    "    fileinput.FileInput(path, inplace=level)\n"  # 22
                    "    py_compile.compile(path)\n"  # 23
                    "    compileall.compile_dir(path)\n"  # 24
                    "\n"
                    "\n"
                    "def read(path, level):\n"
                    "    io.FileIO(path)\n"
                    '    FileIO(path, "rb")\n'
                    "    logging.basicConfig(level=level)\n"
                    "    logging.getLogger(path)\n"
                    "    fileinput.input(path)\n"
                    "    fileinput.input(path, inplace=False)\n"
                    "    urllib.request.urlopen(path)\n",
                },
            )
            route = "; route it through deployer/fsops.py"
            self.assertEqual(
                [
                    f"deployer/writes.py:12 writes with io.FileIO{route}",
                    f"deployer/writes.py:13 writes with io.FileIO{route}",
                    f"deployer/writes.py:14 writes with io.FileIO{route}",
                    f"deployer/writes.py:15 writes with logging.FileHandler{route}",
                    f"deployer/writes.py:16 writes with logging.handlers.RotatingFileHandler{route}",
                    f"deployer/writes.py:17 writes with logging.handlers.TimedRotatingFileHandler{route}",
                    f"deployer/writes.py:18 writes with logging.handlers.WatchedFileHandler{route}",
                    f"deployer/writes.py:19 writes with logging.basicConfig{route}",
                    f"deployer/writes.py:20 writes with urllib.request.urlretrieve{route}",
                    f"deployer/writes.py:21 writes with fileinput.input{route}",
                    f"deployer/writes.py:22 writes with fileinput.FileInput{route}",
                    f"deployer/writes.py:23 writes with py_compile.compile{route}",
                    f"deployer/writes.py:24 writes with compileall.compile_dir{route}",
                ],
                filesystem_write_problems(root),
            )

    def test_an_allowed_logging_file_download_or_in_place_input_must_land_in_the_temporary_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowance = (
                "FSOPS_ALLOWED = {\n"
                '    "tempfile.mkdtemp": "a working directory under the system temporary directory",\n'
                '    "logging.FileHandler": "logs only inside the directory mkdtemp made",\n'
                '    "logging.basicConfig": "logs only inside the directory mkdtemp made",\n'
                '    "urllib.request.urlretrieve": "downloads only into the directory mkdtemp made",\n'
                '    "fileinput.input": "rewrites only inside the directory mkdtemp made",\n'
                "}\n"
                "import fileinput\n"
                "import logging\n"
                "import tempfile\n"
                "import urllib.request\n"
                "\n"
                "\n"
            )
            files = {
                # The mode, the level, and the address name no destination.
                "deployer/kept.py": allowance + "def run(url):\n"
                "    work = tempfile.mkdtemp()\n"
                '    logging.FileHandler(work, "w")\n'
                "    logging.basicConfig(filename=work, level=logging.INFO)\n"
                "    urllib.request.urlretrieve(url, work)\n"
                "    fileinput.input(work, inplace=True)\n",
                "deployer/strayed.py": allowance + "def run(url, elsewhere):\n"
                "    work = tempfile.mkdtemp()\n"
                "    logging.FileHandler(elsewhere)\n"  # 16
                "    logging.basicConfig(filename=elsewhere)\n"  # 17
                "    urllib.request.urlretrieve(url, filename=elsewhere)\n"  # 18
                "    urllib.request.urlretrieve(work)\n"  # 19: with no file it picks one itself
                "    fileinput.input(elsewhere, inplace=True)\n",  # 20
            }
            write_fixture_tree(root, files)
            message = (
                "FSOPS_ALLOWED allows {} only under the system temporary directory, and this call may write elsewhere"
            )
            self.assertEqual(
                [
                    "deployer/strayed.py:16 " + message.format("logging.FileHandler"),
                    "deployer/strayed.py:17 " + message.format("logging.basicConfig"),
                    "deployer/strayed.py:18 " + message.format("urllib.request.urlretrieve"),
                    "deployer/strayed.py:19 " + message.format("urllib.request.urlretrieve"),
                    "deployer/strayed.py:20 " + message.format("fileinput.input"),
                ],
                filesystem_write_problems(root),
            )

    def test_filesystem_write_policy_counts_a_write_callable_passed_as_a_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deployer/passed.py": "import functools\n"
                    "import os\n"
                    "import shutil\n"
                    "from pathlib import Path\n"
                    "\n"
                    "\n"
                    "def run(paths, texts, path, work):\n"
                    "    list(map(Path.write_text, paths, texts))\n"  # 8
                    "    sorted(paths, key=os.remove)\n"  # 9
                    "    work(callback=shutil.rmtree)\n"  # 10
                    '    handlers = {"remove": os.remove}\n'  # 11
                    "    remove = os.unlink\n"
                    "    list(map(remove, paths))\n"  # 13: an alias passed on
                    "    functools.partial(path.mkdir, parents=True)\n"  # 14
                    "    list(map(open, paths))\n"  # 15: the caller may pass a writing mode
                    "    work(opener=path.open)\n"  # 16
                    "    return handlers\n"
                    "\n"
                    "\n"
                    "def read(paths, path, work):\n"
                    "    list(map(Path.read_text, paths))\n"
                    "    sorted(paths, key=os.path.getsize)\n"
                    "    work(callback=shutil.which)\n"
                    '    readers = {"read": path.read_bytes}\n'
                    "    writer = open\n"
                    '    writer(path, "rb")\n'
                    "    return readers, path.name, os.sep\n",
                },
            )
            route = "; route it through deployer/fsops.py"
            self.assertEqual(
                [
                    f"deployer/passed.py:8 writes with write_text{route}",
                    f"deployer/passed.py:9 writes with os.remove{route}",
                    f"deployer/passed.py:10 writes with shutil.rmtree{route}",
                    f"deployer/passed.py:11 writes with os.remove{route}",
                    f"deployer/passed.py:13 writes with os.unlink{route}",
                    f"deployer/passed.py:14 writes with mkdir{route}",
                    f"deployer/passed.py:15 writes with open{route}",
                    f"deployer/passed.py:16 writes with open{route}",
                ],
                filesystem_write_problems(root),
            )

    def test_an_allowed_write_callable_passed_as_a_value_may_write_anywhere(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deployer/kept.py": 'FSOPS_ALLOWED = {"shutil.rmtree": "removes only the directory mkdtemp made",'
                    ' "tempfile.mkdtemp": "a working directory under the system temporary directory"}\n'
                    "import shutil\n"
                    "import tempfile\n"
                    "work = tempfile.mkdtemp()\n"
                    "shutil.rmtree(work)\n",
                    "deployer/passed.py": 'FSOPS_ALLOWED = {"shutil.rmtree": "removes only a temporary directory"}\n'
                    "import shutil\n"
                    "def run(paths):\n"
                    "    list(map(shutil.rmtree, paths))\n",
                },
            )
            self.assertEqual(
                [
                    "deployer/passed.py:4 FSOPS_ALLOWED allows shutil.rmtree only under the system temporary "
                    "directory, and a callable passed as a value may write anywhere"
                ],
                filesystem_write_problems(root),
            )

    def test_an_allowed_tempfile_call_must_name_its_prefix_and_suffix_by_literals_free_of_separators(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowance = (
                "FSOPS_ALLOWED = {\n"
                '    "tempfile.mkdtemp": "a working directory under the system temporary directory",\n'
                '    "tempfile.NamedTemporaryFile": "captured output under the system temporary directory",\n'
                "}\n"
                "import tempfile\n"
                "\n"
                "\n"
            )
            files = {
                "deployer/kept.py": allowance + "def run():\n"
                '    tempfile.mkdtemp(prefix="run-", suffix=None)\n'
                '    tempfile.mkdtemp(prefix=b"run-", suffix="..tmp")\n'
                '    tempfile.NamedTemporaryFile(suffix=".log")\n',
                "deployer/strayed.py": allowance + "def run(name):\n"
                '    tempfile.mkdtemp(prefix="../escape-")\n'  # 9: a separator leaves the temporary directory
                '    tempfile.mkdtemp(suffix="\\\\..\\\\escape")\n'  # 10: so does a Windows separator
                '    tempfile.mkdtemp(prefix=b"../escape-")\n'  # 11
                "    tempfile.mkdtemp(prefix=name)\n"  # 12: a prefix the policy cannot read
                '    tempfile.NamedTemporaryFile(suffix="/escape.log")\n',  # 13
            }
            write_fixture_tree(root, files)
            message = (
                "FSOPS_ALLOWED allows {} only under the system temporary directory, and this call may write elsewhere"
            )
            self.assertEqual(
                [
                    "deployer/strayed.py:9 " + message.format("tempfile.mkdtemp"),
                    "deployer/strayed.py:10 " + message.format("tempfile.mkdtemp"),
                    "deployer/strayed.py:11 " + message.format("tempfile.mkdtemp"),
                    "deployer/strayed.py:12 " + message.format("tempfile.mkdtemp"),
                    "deployer/strayed.py:13 " + message.format("tempfile.NamedTemporaryFile"),
                ],
                filesystem_write_problems(root),
            )

    def test_a_temporary_directory_name_rebound_by_any_binding_is_not_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowance = (
                "FSOPS_ALLOWED = {\n"
                '    "tempfile.mkdtemp": "a working directory under the system temporary directory",\n'
                '    "shutil.rmtree": "removes only the directory mkdtemp made",\n'
                "}\n"
                "import shutil\n"
                "import tempfile\n"
                "\n"
                "\n"
                "def run(subject):\n"
                "    work = tempfile.mkdtemp()\n"
            )
            removal = "    shutil.rmtree(work)\n"  # 11 in every module
            rebindings = {
                "caught": "try:\n    pass\nexcept OSError as work:\n    pass\n",
                "imported": "import os.path as work\n",
                "defined": "def work():\n    pass\n",
                "classed": "class work:\n    pass\n",
                "captured": "def capture(subject):\n    match subject:\n        case [work]:\n            pass\n",
                "starred": "def capture(subject):\n    match subject:\n        case [*work]:\n            pass\n",
                "rest": "def capture(subject):\n    match subject:\n        case {**work}:\n            pass\n",
            }
            files = {"deployer/kept.py": allowance + removal}
            files |= {f"deployer/{name}.py": allowance + removal + rebound for name, rebound in rebindings.items()}
            write_fixture_tree(root, files)
            message = (
                "FSOPS_ALLOWED allows shutil.rmtree only under the system temporary directory, and this call may write "
                "elsewhere"
            )
            self.assertEqual(
                [f"deployer/{name}.py:11 {message}" for name in sorted(rebindings)],
                filesystem_write_problems(root),
            )

    def test_filesystem_write_policy_reads_a_callable_named_by_a_literal_and_a_module_passed_as_a_value(self) -> None:
        dynamic = (
            "import importlib\n"  # 1
            "import operator\n"  # 2
            "import shutil\n"  # 3
            "from operator import methodcaller\n"  # 4
            "from pathlib import Path\n"  # 5
            "\n"  # 6
            "\n"  # 7
            "class Holder:\n"  # 8
            "    def __init__(self, fs=shutil):\n"  # 9
            "        self.fs = fs\n"  # 10: the module is called later through an attribute
            "        self.other = shutil\n"  # 11
            "\n"  # 12
            "    def go(self, path, text, name, factory, paths):\n"  # 13
            "        self.fs.rmtree(path)\n"  # 14
            '        getattr(shutil, "rmtree")(path)\n'  # 15
            '        importlib.import_module("shutil").rmtree(path)\n'  # 16
            '        __import__("os").remove(path)\n'  # 17
            '        operator.methodcaller("write_text", text)(path)\n'  # 18: counted, and its call unresolved
            "        list(map(methodcaller(name), paths))\n"  # 19: a method name the policy cannot read
            '        list(map(operator.attrgetter("unlink"), paths))\n'  # 20
            "        factory()(path)\n"  # 21: unresolved
            '        remove = getattr(shutil, "rmtree")\n'  # 22
            '        module = importlib.import_module("shutil")\n'  # 23
            '        getattr(path, "write_text")(text)\n'  # 24
            '        list(map(getattr(shutil, "rmtree"), paths))\n'  # 25
            "        return remove, module, Path\n"  # 26
        )
        # Reads named by literals, a copy that is not a Path's, and a module's own move function write nothing.
        reads = (
            "import copy\n"
            "import importlib\n"
            "import operator\n"
            "import os\n"
            "import shutil\n"
            "from . import fsops\n"
            "\n"
            "\n"
            "class Holder:\n"
            "    def __init__(self, root):\n"
            "        self.root = root\n"
            "\n"
            "    def go(self, path, values, paths):\n"
            '        getattr(path, "read_text")()\n'
            '        importlib.import_module("json").dumps(values)\n'
            '        list(map(operator.methodcaller("read_text"), paths))\n'
            '        list(map(operator.attrgetter("name"), paths))\n'
            '        list(map(operator.methodcaller("copy"), paths))\n'
            '        hasattr(os, "remove"), getattr(shutil, "which")("git")\n'
            "        values.copy(), copy.copy(values), fsops.move(path, path)\n"
            "        return self.root\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, {"deployer/dynamic.py": dynamic, "deployer/reads.py": reads})
            route = "; route it through deployer/fsops.py"
            written = [
                (10, "shutil"),
                (11, "shutil"),
                (15, "shutil.rmtree"),
                (16, "shutil.rmtree"),
                (17, "os.remove"),
                (18, "write_text"),
                (19, "operator.methodcaller"),
                (20, "unlink"),
                (22, "shutil.rmtree"),
                (23, "shutil"),
                (24, "write_text"),
                (25, "shutil.rmtree"),
            ]
            self.assertEqual(
                [f"deployer/dynamic.py:{line} writes with {token}{route}" for line, token in written]
                + [
                    "deployer/dynamic.py:18 cannot resolve the callable operator.methodcaller('write_text', text) "
                    "returns; call a known callable instead",
                    "deployer/dynamic.py:21 cannot resolve the callable factory() returns; "
                    "call a known callable instead",
                ],
                filesystem_write_problems(root),
            )

    def test_filesystem_write_policy_reads_a_module_lookup_and_a_chained_literal_lookup_or_reports_it(self) -> None:
        lookups = (
            "import importlib\n"  # 1
            "import shutil\n"  # 2
            "import sys\n"  # 3
            "\n"  # 4
            "\n"  # 5
            "def go(path, name, handlers, kind):\n"  # 6
            '    sys.modules["shutil"].rmtree(path)\n'  # 7
            '    getattr(importlib, "import_module")("shutil").rmtree(path)\n'  # 8
            '    shutil.__dict__["rmtree"](path)\n'  # 9
            '    module = sys.modules["shutil"]\n'  # 10: the module is called later through the name
            "    handlers[kind](path)\n"  # 11: unresolved
            "    shutil.__dict__[name](path)\n"  # 12: unresolved
            "    importlib.import_module(name).rmtree(path)\n"  # 13: unresolved
            "    sys.modules[name].rmtree(path)\n"  # 14: unresolved
            '    __import__("os").__dict__["remove"](path)\n'  # 15
            "    return module\n"  # 16
        )
        # Reads reached through the same lookups, and a call through an item's attribute, write nothing.
        reads = (
            "import importlib\n"
            "import sys\n"
            "\n"
            "\n"
            "def go(path, values, handlers):\n"
            '    sys.modules["json"].dumps(values)\n'
            '    getattr(importlib, "import_module")("json").dumps(values)\n'
            '    sys.modules["shutil"].which("git"), sys.modules["os"].__dict__["getcwd"]()\n'
            '    handlers["x"].run(path), values[0].strip()\n'
            '    return sys.modules["json"]\n'
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, {"deployer/lookups.py": lookups, "deployer/reads.py": reads})
            route = "; route it through deployer/fsops.py"
            written = [
                (7, "shutil.rmtree"),
                (8, "shutil.rmtree"),
                (9, "shutil.rmtree"),
                (10, "shutil"),
                (15, "os.remove"),
            ]
            unresolved = [
                (11, "the callable handlers[kind] looks up"),
                (12, "the callable shutil.__dict__[name] looks up"),
                (13, "the module importlib.import_module(name) names"),
                (14, "the module sys.modules[name] names"),
            ]
            self.assertEqual(
                [f"deployer/lookups.py:{line} writes with {token}{route}" for line, token in written]
                + [
                    f"deployer/lookups.py:{line} cannot resolve {lookup}; call a known callable instead"
                    for line, lookup in unresolved
                ],
                filesystem_write_problems(root),
            )

    def test_an_allowed_write_named_by_a_literal_must_land_in_the_temporary_directory(self) -> None:
        allowance = (
            "FSOPS_ALLOWED = {\n"
            '    "tempfile.mkdtemp": "a working directory under the system temporary directory",\n'
            '    "shutil.rmtree": "removes only the directory mkdtemp made",\n'
            "}\n"
            "import importlib\n"
            "import shutil\n"
            "import tempfile\n"
            "\n"
            "\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deployer/kept.py": allowance + "def run():\n"
                    "    work = tempfile.mkdtemp()\n"
                    '    getattr(shutil, "rmtree")(work)\n'
                    '    importlib.import_module("shutil").rmtree(work)\n',
                    "deployer/strayed.py": allowance + "def run(elsewhere):\n"
                    "    tempfile.mkdtemp()\n"
                    '    getattr(shutil, "rmtree")(elsewhere)\n'  # 12
                    '    importlib.import_module("shutil").rmtree(elsewhere)\n',  # 13
                },
            )
            message = (
                "FSOPS_ALLOWED allows shutil.rmtree only under the system temporary directory, and this call may write "
                "elsewhere"
            )
            self.assertEqual(
                [f"deployer/strayed.py:12 {message}", f"deployer/strayed.py:13 {message}"],
                filesystem_write_problems(root),
            )

    def test_filesystem_write_policy_counts_logging_configurations_app_archives_environments_and_path_copies(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deployer/writes.py": "import logging.config\n"  # 1
                    "import venv\n"  # 2
                    "import zipapp\n"  # 3
                    "from logging.config import dictConfig\n"  # 4
                    "from pathlib import Path\n"  # 5
                    "\n"  # 6
                    "\n"  # 7
                    "def write(config, path, target, paths):\n"  # 8
                    "    logging.config.dictConfig(config)\n"  # 9
                    "    logging.config.fileConfig(path)\n"  # 10
                    "    zipapp.create_archive(path)\n"  # 11
                    "    venv.create(path)\n"  # 12
                    "    venv.EnvBuilder(with_pip=False)\n"  # 13
                    "    Path(path).copy(target)\n"  # 14
                    "    path.move(target=target)\n"  # 15
                    "    list(map(Path.copy, paths))\n"  # 16
                    "    return dictConfig\n",
                    "deployer/reads.py": "import copy\n"
                    "import logging\n"
                    "from . import fsops\n"
                    "\n"
                    "\n"
                    "def read(values, path):\n"
                    "    logging.getLogger(path)\n"
                    "    values.copy(), copy.copy(values), fsops.move(path, path)\n",
                },
            )
            route = "; route it through deployer/fsops.py"
            written = [
                (4, "logging.config.dictConfig"),
                (9, "logging.config.dictConfig"),
                (10, "logging.config.fileConfig"),
                (11, "zipapp.create_archive"),
                (12, "venv.create"),
                (13, "venv.EnvBuilder"),
                (14, "copy"),
                (15, "move"),
                (16, "copy"),
                (17, "logging.config.dictConfig"),
            ]
            self.assertEqual(
                [f"deployer/writes.py:{line} writes with {token}{route}" for line, token in written],
                filesystem_write_problems(root),
            )

    def test_platform_policy_finds_posix_signals_and_process_calls_windows_only_names_tasklist_and_windows_paths(
        self,
    ) -> None:
        strayed = (
            "import os\n"  # 1
            "import signal\n"  # 2
            "import subprocess\n"  # 3
            "import sys\n"  # 4
            "import winsound\n"  # 5
            "import syslog, _overlapped\n"  # 6
            "from pathlib import PureWindowsPath, WindowsPath\n"  # 7
            "import pathlib\n"  # 8
            "\n"  # 9
            "\n"  # 10
            "def stop(pid, path):\n"  # 11
            "    signal.SIGHUP, signal.SIGQUIT, signal.SIGALRM, signal.SIGUSR1, signal.SIGCHLD, signal.SIGPIPE\n"  # 12
            "    signal.alarm(1), signal.setitimer(signal.ITIMER_REAL, 1)\n"  # 13
            "    os.wait(), os.waitpid(pid, os.WNOHANG), os.getpgid(pid), os.setpgrp()\n"  # 14
            "    os.open(path, os.O_RDONLY | os.O_CLOEXEC)\n"  # 15
            "    os.add_dll_directory(path), sys.winver\n"  # 16
            '    subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], check=False)\n'  # 17
            '    os.environ.get("WINDIR"), os.environ["ProgramFiles(x86)"], os.getenv("programdata")\n'  # 18
            "    pathlib.PosixPath(path), pathlib.PureWindowsPath(path).anchor\n"  # 19
        )
        # Signals both platforms define, portable process calls, and POSIX path rules assume no platform.
        innocent = (
            "import os\n"
            "import signal\n"
            "from pathlib import PurePosixPath\n"
            "\n"
            "\n"
            "def stop(pid, path):\n"
            "    signal.SIGINT, signal.SIGTERM, signal.SIGABRT\n"
            "    os.waitpid(pid, 0), os.kill(pid, signal.SIGTERM)\n"
            "    os.open(path, os.O_RDONLY)\n"
            '    PurePosixPath(path).suffix, "tasklists", os.environ.get("WINDIRS")\n'
        )
        sanctioned = (
            'PLATFORM_ALLOWED = {"pathlib.PureWindowsPath": "refuses a drive on every platform"}\n'
            "from pathlib import PureWindowsPath\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "tools/strayed.py": strayed,
                    "deployer/platform_support.py": strayed,
                    "tools/innocent.py": innocent,
                    "tools/sanctioned.py": sanctioned,
                },
            )
            move = "; move it behind deployer/platform_support.py"
            expected = [
                (5, "winsound"),
                (6, "_overlapped"),
                (6, "syslog"),
                (7, "pathlib.PureWindowsPath"),
                (7, "pathlib.WindowsPath"),
                (12, "signal.SIGALRM"),
                (12, "signal.SIGCHLD"),
                (12, "signal.SIGHUP"),
                (12, "signal.SIGPIPE"),
                (12, "signal.SIGQUIT"),
                (12, "signal.SIGUSR1"),
                (13, "signal.alarm"),
                (13, "signal.setitimer"),
                (14, "os.WNOHANG"),
                (14, "os.getpgid"),
                (14, "os.setpgrp"),
                (14, "os.wait"),
                (15, "os.O_CLOEXEC"),
                (16, "os.add_dll_directory"),
                (16, "sys.winver"),
                (17, "tasklist"),
                (18, "ProgramFiles(x86)"),
                (18, "WINDIR"),
                (18, "programdata"),
                (19, "pathlib.PosixPath"),
                (19, "pathlib.PureWindowsPath"),
            ]
            self.assertEqual(
                [f"tools/strayed.py:{line} names {token}{move}" for line, token in expected],
                platform_code_problems(root),
            )


@pytest.mark.parametrize(
    "prefix",
    [
        pytest.param("writer = open\nwriter = open\n", id="bound twice"),
        pytest.param("writer = open\nwriter = None\n", id="rebound to a value"),
        pytest.param("writer = missing\n", id="bound to an unknown name"),
        pytest.param("writer = other\nother = writer\n", id="cyclic"),
        pytest.param("writer = open\nfor writer in callbacks:\n    pass\n", id="rebound by a loop"),
        pytest.param("writer = open\nwith callback as writer:\n    pass\n", id="rebound by a with"),
        pytest.param("import builtins as writer\nwriter = None\n", id="rebound module"),
    ],
)
def test_filesystem_write_policy_reports_ambiguous_assigned_callables(tmp_path: Path, prefix: str) -> None:
    function = "writer.open" if prefix.startswith("import ") else "writer"
    source = prefix + f'{function}("x", "w")\n'
    write_fixture_tree(tmp_path, {"deployer/ambiguous.py": source})
    CHECK.assertEqual(
        [
            f"deployer/ambiguous.py:{len(source.splitlines())} cannot resolve callable alias "
            f"{function}; use a single binding to a known callable"
        ],
        filesystem_write_problems(tmp_path),
    )
