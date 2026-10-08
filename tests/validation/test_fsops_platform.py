"""Fixture tests for tests/validation/fsops_platform.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fsops_platform import _platform_names, filesystem_write_problems, platform_code_problems


class FsopsPlatformFixtures(unittest.TestCase):
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
            "\n"  # 40
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


if __name__ == "__main__":
    unittest.main()
