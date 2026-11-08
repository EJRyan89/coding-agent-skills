"""Fixture tests for tests/validation/skill_scripts.py.

Each policy fails on a violating input and passes on a conforming one. The console policy's entry points are also
run as written, each with its main failing, to show the runner each block starts with keeps the FAILED-line promise.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import skill_scripts
from call_tables import EVENT_LOOP
from command_wrappers import UNREAD_PROGRAM
from skill_scripts import (
    CLIENT_CLASSES,
    COMMAND_RUNNERS,
    CommandReader,
    client_command_problems,
    console_entry_points,
    console_setup_problems,
    gh_filter_problems,
    main_blocks,
    optional_tool_placement_problems,
    script_contract_problems,
    secret_named_function_problems,
    shell_commands,
    skill_command_problems,
)
from validation_support import REPOSITORY_ROOT, write_fixture_tree

from deployer import platform_support


def mark_skills(root: Path) -> Path:
    """Give each skills/<name> folder with a scripts/ directory the SKILL.md that makes it a skill, and return root."""
    for scripts in (root / "skills").glob("*/scripts"):
        (scripts.parent / "SKILL.md").write_text(f"# {scripts.parent.name}\n", encoding="utf-8")
    return root


COMMANDS_DOC = '"Commands skills may run" in docs/adding-a-skill.md'
# The operating-system names this module states on purpose, with the reason, which the platform policy reads.
PLATFORM_ALLOWED = {
    "/proc": "names Linux's per-process descriptor paths as fixture values the console-setup policy refuses or "
    "passes; the fixture writes them into a module it parses and opens nothing",
}


def unreadable(path: str, line: int, description: str) -> str:
    """The finding the command policies report for a command whose program they cannot read."""
    return (
        f"{path}:{line} cannot read {description}; name the program with a literal or a lookup, or state why in "
        f"COMMANDS_ALLOWED; see {COMMANDS_DOC}"
    )


class SkillScriptsFixtures(unittest.TestCase):
    def test_client_command_policy_reads_f_string_prefixes_through_runners_and_bound_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "skills/alpha/SKILL.md": "# alpha\n",
                    "skills/alpha/scripts/tool.py": "import subprocess as sp\n"
                    "from subprocess import check_output\n"
                    "from bounded_process import run_bounded\n"
                    "runner = sp.run\n"
                    "alias = runner\n"
                    'alias(f"gh api {endpoint}", check=True, timeout=30)\n'
                    'check_output(args=f"git log {revision}")\n'
                    'COMMAND = f"gh auth {action}"\n'
                    "sp.Popen(COMMAND)\n"
                    'run_bounded([f"git {suffix}", "status"], 5)\n'
                    'sp.call((f"gh {suffix}", "api"))\n'
                    'sp.check_call(f"git status " + revision)\n'
                    'BASE = [f"gh api {endpoint}"]\n'
                    "sp.run([*BASE])\n"
                    'sp.run(f"git status")\n'
                    'sp.run(f"echo {message} gh")\n'
                    'print(f"gh api {endpoint}")\n',
                },
            )
            doc = '"Script results" in docs/adding-a-skill.md'
            git = (
                "run it through ejryan89-skill-core's git_client.py's GitClient, which bounds it and turns prompts off"
            )
            gh = (
                "run it through ejryan89-skill-core's github_client.py's GitHubClient, which bounds it and turns "
                "prompts off"
            )
            self.assertEqual(
                [
                    f"skills/alpha/scripts/tool.py:{line} runs {command} itself; {client}; see {doc}"
                    for line, command, client in (
                        (6, "gh", gh),
                        (7, "git", git),
                        (8, "gh", gh),
                        (10, "git", git),
                        (11, "gh", gh),
                        (12, "git", git),
                        (13, "gh", gh),
                        (15, "git", git),
                    )
                ],
                client_command_problems(root),
            )

    def test_skill_command_policy_reads_f_string_prefixes_and_honors_declared_and_standard_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "skills" / "alpha" / "scripts" / "tool.py"
            write_fixture_tree(
                root,
                {
                    "deploy-meta/alpha.json": "{}",
                    "skills/alpha/SKILL.md": "# alpha\n",
                    "skills/alpha/scripts/tool.py": "import subprocess\n"
                    "runner = subprocess.run\n"
                    'runner(f"gh api {endpoint}")\n'
                    'COMMAND = f"copilot {arguments}"\n'
                    "runner(args=COMMAND)\n"
                    'runner([f"jq {filter}", filename])\n'
                    'runner(f"git status {path}")\n'
                    'runner(f"python {script}")\n'
                    'runner(f"echo {message} yq")\n'
                    'print(f"rg {pattern}")\n',
                },
            )
            doc = '"Commands skills may run" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    "skill alpha runs copilot without declaring it in tools",
                    "skill alpha runs gh without declaring it in tools",
                    f"skill alpha runs jq, which a standard install lacks; see {doc}",
                ],
                skill_command_problems(root, {"copilot", "gh"}, {"git", "python"}),
            )
            (root / "deploy-meta" / "alpha.json").write_text(
                json.dumps({"tools": ["gh"], "optional_tools": ["copilot"]}), encoding="utf-8"
            )
            script.write_text(script.read_text(encoding="utf-8").replace("jq ", "python "), encoding="utf-8")
            self.assertEqual([], skill_command_problems(root, {"copilot", "gh"}, {"git", "python"}))

    def test_command_policies_read_format_join_and_split_commands_and_report_one_without_a_program(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deploy-meta/alpha.json": "{}",
                    "skills/alpha/SKILL.md": "# alpha\n",
                    "skills/alpha/scripts/tool.py": "import shlex\n"
                    "import subprocess\n"
                    'subprocess.run("gh api %s" % endpoint)\n'  # 3
                    'subprocess.run("gh api {}".format(endpoint))\n'  # 4
                    'TEMPLATE = "git log {revision}"\n'  # 5
                    "subprocess.run(TEMPLATE.format(revision=revision), shell=True)\n"
                    'subprocess.run(" ".join(("gh", "auth", "status")), shell=True)\n'  # 7
                    'subprocess.run(shlex.split("git status --short"))\n'  # 8
                    'subprocess.run(shlex.join(("jq", ".")), shell=True)\n'  # 9
                    'subprocess.run("%s api" % program)\n'  # 10
                    'subprocess.run("{} api".format(program))\n'  # 11
                    'subprocess.run(" ".join(parts))\n',  # 12
                },
            )
            unread = [
                unreadable("skills/alpha/scripts/tool.py", line, description)
                for line, description in (
                    (10, "%-format command without a literal program prefix"),
                    (11, ".format() command without a literal program prefix"),
                    (12, "the program in `parts`, which the module binds to nothing it reads"),
                )
            ]
            doc = '"Script results" in docs/adding-a-skill.md'
            git = (
                "run it through ejryan89-skill-core's git_client.py's GitClient, which bounds it and turns prompts off"
            )
            gh = (
                "run it through ejryan89-skill-core's github_client.py's GitHubClient, which bounds it and turns "
                "prompts off"
            )
            self.assertEqual(
                [
                    *unread,
                    *(
                        f"skills/alpha/scripts/tool.py:{line} runs {command} itself; {client}; see {doc}"
                        for line, command, client in (
                            (3, "gh", gh),
                            (4, "gh", gh),
                            (5, "git", git),
                            (7, "gh", gh),
                            (8, "git", git),
                        )
                    ),
                ],
                client_command_problems(root),
            )
            self.assertEqual(
                [
                    *unread,
                    "skill alpha runs gh without declaring it in tools",
                    f"skill alpha runs jq, which a standard install lacks; see {COMMANDS_DOC}",
                ],
                skill_command_problems(root, {"gh"}, {"git", "python"}),
            )

    def test_command_policies_follow_a_parameter_to_the_calls_that_pass_it(self) -> None:
        script = (
            "import subprocess\n"
            "from bounded_process import streaming\n"
            "def _run(command):\n"
            "    return subprocess.run(command, check=False)\n"
            '_run(["yq", "."])\n'
            '_run(command=["rg", "x"])\n'
            'def _probe(path, arguments=("-v",)):\n'
            "    with streaming([path, *arguments]) as running:\n"
            "        return running\n"
            "def probe_tool(path):\n"  # 10: the pdf_tool shape, whose callers are in other modules
            "    return _probe(path)\n"
            "def checked(program):\n"  # 12: a public function another module may call too
            '    return subprocess.run([program, "--version"])\n'
            'checked("perl")\n'
            "def handed(command):\n"  # 15: a function handed on, so not every call is in view
            "    subprocess.run(command)\n"
            "RUNNERS = [handed]\n"
            'handed(["ruby"])\n'
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deploy-meta/alpha.json": "{}",
                    "skills/alpha/SKILL.md": "# alpha\n",
                    "skills/alpha/scripts/tool.py": script,
                },
            )
            unread = [
                unreadable("skills/alpha/scripts/tool.py", line, f"the program in parameter {name}, {reason}")
                for line, name, reason in (
                    (10, "path", "which no call in this module passes"),
                    (12, "program", "which other modules may pass"),
                    (15, "command", "whose function is passed on"),
                )
            ]
            self.assertEqual(unread, client_command_problems(root))
            self.assertEqual(
                [
                    *unread,
                    *(
                        f"skill alpha runs {name}, which a standard install lacks; see {COMMANDS_DOC}"
                        for name in ("perl", "rg", "ruby", "yq")
                    ),
                ],
                skill_command_problems(root, {"gh"}, {"git", "python"}),
            )

    def test_command_policies_hold_commands_allowed_to_its_shape_and_to_what_it_excuses(self) -> None:
        script = (
            "import subprocess\n"
            "def probe_tool(path):\n"
            '    return subprocess.run([path, "-v"])\n'
            "def checked(program):\n"
            "    return subprocess.run(program)\n"
        )
        for allowance, expected in (
            # Each function the module states a reason for passes, and a stated function that starts no unread
            # command is reported, so an allowance cannot outlive the code it excuses.
            (
                '{"probe_tool": "runs the tool its caller found", "stale": "no longer needed"}',
                [
                    unreadable(
                        "skills/alpha/scripts/tool.py",
                        4,
                        "the program in parameter program, which no call in this module passes",
                    ),
                    "skills/alpha/scripts/tool.py: COMMANDS_ALLOWED allows stale, which starts no command whose "
                    "program is unread",
                ],
            ),
            # A reason that says nothing, or an allowance that is not a literal dictionary, sanctions nothing.
            (
                '{"probe_tool": " "}',
                [
                    "skills/alpha/scripts/tool.py: COMMANDS_ALLOWED must map each scope to the reason its program "
                    "is unread",
                    unreadable(
                        "skills/alpha/scripts/tool.py",
                        2,
                        "the program in parameter path, which no call in this module passes",
                    ),
                    unreadable(
                        "skills/alpha/scripts/tool.py",
                        4,
                        "the program in parameter program, which no call in this module passes",
                    ),
                ],
            ),
        ):
            with self.subTest(allowance=allowance), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                write_fixture_tree(
                    root,
                    {
                        "deploy-meta/alpha.json": "{}",
                        "skills/alpha/SKILL.md": "# alpha\n",
                        "skills/alpha/scripts/tool.py": f"{script}COMMANDS_ALLOWED = {allowance}\n",
                    },
                )
                self.assertEqual(expected, client_command_problems(root))
                self.assertEqual(expected, skill_command_problems(root, {"gh"}, {"git", "python"}))

    def test_command_policies_read_a_runner_the_table_names_at_its_callers_instead(self) -> None:
        # bounded_process.run_bounded is a COMMAND_RUNNERS entry, so its command parameter is read where each caller
        # passes it; the same parameter in another module, or another function of the module, is unread.
        runner = "import subprocess\ndef run_bounded(command, timeout):\n    return subprocess.Popen(command)\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "skills/alpha/SKILL.md": "# alpha\n",
                    "skills/alpha/scripts/bounded_process.py": f"{runner}def other(command):\n"
                    "    return subprocess.Popen(command)\n",
                    "skills/alpha/scripts/elsewhere.py": runner,
                },
            )
            self.assertEqual(
                [
                    unreadable(
                        "skills/alpha/scripts/bounded_process.py",
                        4,
                        "the program in parameter command, which no call in this module passes",
                    ),
                    unreadable(
                        "skills/alpha/scripts/elsewhere.py",
                        2,
                        "the program in parameter command, which no call in this module passes",
                    ),
                ],
                client_command_problems(root),
            )

    def test_command_policies_read_os_exec_and_spawn_through_aliases_and_keywords(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deploy-meta/alpha.json": "{}",
                    "skills/alpha/SKILL.md": "# alpha\n",
                    "skills/alpha/scripts/tool.py": "import os\n"
                    "import os as system\n"
                    "from os import execvp as replace, spawnv\n"
                    'os.execv("git", ["status"])\n'  # 4
                    'system.execlp("gh", "gh", "api")\n'  # 5
                    'replace(file="git", args=["log"])\n'  # 6
                    'spawnv(os.P_WAIT, "gh", ["api"])\n'  # 7
                    'system.spawnlp(mode, file="git")\n'  # 8
                    'os.posix_spawn("gh", ["api"], {})\n'  # 9
                    "os.execvp(program, [program])\n"  # 10
                    'system.spawnve(mode, "jq", ["."], {})\n',  # 11
                },
            )
            doc = '"Script results" in docs/adding-a-skill.md'
            git = (
                "run it through ejryan89-skill-core's git_client.py's GitClient, which bounds it and turns prompts off"
            )
            gh = (
                "run it through ejryan89-skill-core's github_client.py's GitHubClient, which bounds it and turns "
                "prompts off"
            )
            unread = unreadable(
                "skills/alpha/scripts/tool.py",
                10,
                "the program in `program`, which the module binds to nothing it reads",
            )
            self.assertEqual(
                [
                    unread,
                    *(
                        f"skills/alpha/scripts/tool.py:{line} runs {command} itself; {client}; see {doc}"
                        for line, command, client in (
                            (4, "git", git),
                            (5, "gh", gh),
                            (6, "git", git),
                            (7, "gh", gh),
                            (8, "git", git),
                            (9, "gh", gh),
                        )
                    ),
                ],
                client_command_problems(root),
            )
            self.assertEqual(
                [
                    unread,
                    "skill alpha runs gh without declaring it in tools",
                    f"skill alpha runs jq, which a standard install lacks; see {COMMANDS_DOC}",
                ],
                skill_command_problems(root, {"gh"}, {"git", "python"}),
            )

    def test_command_policies_read_each_other_way_a_program_reaches_a_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deploy-meta/alpha.json": "{}",
                    "skills/alpha/SKILL.md": "# alpha\n",
                    "skills/alpha/scripts/tool.py": "import subprocess\n"
                    "import sys\n"
                    'subprocess.run([sys.executable, "-B", "x.py"])\n'  # the interpreter is python
                    'subprocess.run(["status"], executable="perl")\n'  # executable= replaces the program
                    'COMMANDS = (["node", "x"], ["ruby", "y"])\n'
                    "for command in COMMANDS:\n"
                    "    subprocess.run(command)\n"
                    'subprocess.run(["rg", "x"] if flag else ["jq", "y"])\n'
                    "class Runner:\n"
                    "    def __init__(self):\n"
                    '        self.program = "yq"\n'
                    "    def go(self):\n"
                    '        subprocess.run([self.program, "api"])\n'
                    "subprocess.run(**options)\n"  # 14
                    "later = []\n"  # 15: filled in by a call the reader does not follow
                    'later.append("git")\n'
                    "subprocess.run(later)\n"
                    "subprocess.run(sys.argv[1:])\n"  # 18
                    'with open("x") as handle:\n'
                    "    subprocess.run(handle)\n",  # 20
                },
            )
            path = "skills/alpha/scripts/tool.py"
            self.assertEqual(
                [
                    unreadable(path, 14, "the program in `subprocess.run(**options)`, which unpacks its arguments"),
                    unreadable(path, 15, "the program in `[]`, which the module fills in elsewhere"),
                    unreadable(path, 18, "the program in `sys.argv[1:]`"),
                    unreadable(path, 20, "the program in `handle`, which the module binds to nothing it reads"),
                    *(
                        f"skill alpha runs {name}, which a standard install lacks; see {COMMANDS_DOC}"
                        # sys.executable is the platform's Python command.
                        for name in (
                            "jq",
                            "node",
                            "perl",
                            "python" if platform_support.WINDOWS else "python3",
                            "rg",
                            "ruby",
                            "yq",
                        )
                    ),
                ],
                skill_command_problems(root, {"gh"}, {"git"}),
            )

    def test_command_policies_read_a_literal_path_by_its_base_name_or_report_one_they_cannot(self) -> None:
        windows = "C:\\Tools\\yq.exe"
        spaced = "C:\\Program Files\\Tool\\tool.exe"
        argued = "/usr/bin/perl -e 1"
        script = (
            "import subprocess\n"
            'subprocess.run(["/usr/bin/ruby", "-e", "1"])\n'  # absolute
            'subprocess.run(["./custom-tool"])\n'  # relative
            f"subprocess.run({windows!r})\n"  # Windows-style, with its .exe suffix
            'subprocess.run(["../bin/jq.EXE", "."])\n'
            "def spaced():\n"
            f"    subprocess.run([{spaced!r}])\n"  # 7: a space may be part of the path or start its arguments
            "def argued():\n"
            f"    subprocess.run({argued!r}, shell=True)\n"  # 9
            "def directory():\n"
            '    subprocess.run(["/usr/local/bin/"])\n'  # 11
            "def empty():\n"
            '    subprocess.run([""])\n'  # 13
        )
        path = "skills/alpha/scripts/tool.py"
        unread = [
            unreadable(path, 7, f"the program in `{spaced!r}`, a path that a space may be part of"),
            unreadable(path, 9, f"the program in `{argued!r}`, a path that a space may be part of"),
            unreadable(path, 11, "the program in `'/usr/local/bin/'`, a path that names none"),
            unreadable(path, 13, "the program in `''`, which names none"),
        ]
        reasons = {
            "spaced": "runs the tool its installer placed",
            "argued": "runs perl by its path",
            "directory": "runs what the folder holds",
            "empty": "runs what its caller fills in",
        }
        sanction = f"COMMANDS_ALLOWED = {reasons!r}\n"
        tools = ["custom-tool", "jq", "ruby", "yq"]
        for allowance, declared, expected in (
            # Unsanctioned, each unread path is reported and each path read by its base name is an undeclared tool.
            (
                "",
                [],
                [
                    *unread,
                    *(f"skill alpha runs {name} without declaring it in tools" for name in tools),
                ],
            ),
            # Declared tools and a stated reason for each unread path pass.
            (sanction, tools, []),
        ):
            with self.subTest(allowance=allowance), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                write_fixture_tree(
                    root,
                    {
                        "deploy-meta/alpha.json": json.dumps({"tools": declared}),
                        "skills/alpha/SKILL.md": "# alpha\n",
                        "skills/alpha/scripts/tool.py": script + allowance,
                    },
                )
                self.assertEqual(expected, skill_command_problems(root, {"gh", *tools}, {"git", "python"}))
                self.assertEqual(expected[: len(unread)] if not allowance else [], client_command_problems(root))

    def test_command_policies_report_a_runner_handed_on_as_a_value_or_taken_by_getattr(self) -> None:
        script = (
            "import functools\n"
            "import os\n"
            "import subprocess\n"
            "import threading\n"
            "from subprocess import run\n"
            "def partial():\n"
            '    return functools.partial(subprocess.run, ["git"])\n'  # 7
            "def named():\n"
            '    return getattr(subprocess, "run")(["git"])\n'  # 9
            "def dynamic(name):\n"
            "    return getattr(os, name)\n"  # 11
            'RUNNERS = {"run": subprocess.run}\n'  # 12
            "def mapped(commands):\n"
            "    return list(map(run, commands))\n"  # 14
            "def threaded():\n"
            '    return threading.Thread(target=subprocess.Popen, args=(["git"],))\n'  # 16
            "def rebound(flag):\n"
            "    start = subprocess.call\n"
            "    if flag:\n"
            "        start = subprocess.check_call\n"
            "    return threading.Thread(target=start)\n"  # 21: a name bound twice still names a runner
            # A class used as a type, a getattr of what starts nothing, and a call through an alias start nothing
            # unread.
            "def typed(process: subprocess.Popen[bytes]) -> subprocess.Popen[bytes]:\n"
            '    return process if isinstance(process, subprocess.Popen) else getattr(subprocess, "DEVNULL")\n'
            "def called():\n"
            "    start = subprocess.run\n"
            '    return start(["git", "status"])\n'
        )
        path = "skills/alpha/scripts/tool.py"
        handed = "which the module hands on as a value"
        expected = [
            unreadable(path, 7, f"the program `subprocess.run` runs, {handed}"),
            unreadable(path, 9, "the program `getattr(subprocess, 'run')` runs, which a lookup by name takes"),
            unreadable(path, 11, "the program `getattr(os, name)` runs, which a lookup by name takes"),
            unreadable(path, 12, f"the program `subprocess.run` runs, {handed}"),
            unreadable(path, 14, f"the program `run` runs, {handed}"),
            unreadable(path, 16, f"the program `subprocess.Popen` runs, {handed}"),
            unreadable(path, 21, f"the program `start` runs, {handed}"),
        ]
        reader = CommandReader(ast.parse(script))
        self.assertEqual(expected, reader.problems(path))
        self.assertEqual({"git"}, reader.commands(set()))
        scopes = ("partial", "named", "dynamic", "RUNNERS", "mapped", "threaded", "rebound")
        sanctioned = f"{script}COMMANDS_ALLOWED = {dict.fromkeys(scopes, 'starts what its caller gives it')!r}\n"
        self.assertEqual([], CommandReader(ast.parse(sanctioned)).problems(path))
        # A module that only hands a runner on starts a process all the same.
        self.assertTrue(CommandReader(ast.parse("import subprocess\nRUNNERS = [subprocess.run]\n")).starts_process())

    def test_command_policies_report_a_runner_taken_by_either_spelling_of_getattr(self) -> None:
        script = (
            "import builtins\n"
            "import os\n"
            "import subprocess\n"
            "from builtins import getattr as lookup\n"
            "def named():\n"
            '    return builtins.getattr(subprocess, "run")(["git"])\n'  # 6
            "def aliased():\n"
            '    return lookup(subprocess, "Popen")\n'  # 8
            "def dynamic(name):\n"
            "    return builtins.getattr(os, name)\n"  # 10
            # A lookup of what starts nothing, and a getattr given no name, are no runner.
            "def constant():\n"
            '    return builtins.getattr(subprocess, "DEVNULL")\n'
            "def unnamed():\n"
            "    return getattr(subprocess)\n"
        )
        path = "skills/alpha/scripts/tool.py"
        self.assertEqual(
            [
                unreadable(
                    path, 6, "the program `builtins.getattr(subprocess, 'run')` runs, which a lookup by name takes"
                ),
                unreadable(path, 8, "the program `lookup(subprocess, 'Popen')` runs, which a lookup by name takes"),
                unreadable(path, 10, "the program `builtins.getattr(os, name)` runs, which a lookup by name takes"),
            ],
            CommandReader(ast.parse(script)).problems(path),
        )
        self.assertFalse(
            CommandReader(ast.parse("import subprocess\ndef f(p: subprocess.Popen[str]): ...\n")).starts_process()
        )

    def test_command_policies_report_a_runner_taken_by_every_lookup_by_name(self) -> None:
        # Each form of the one catalogue of lookups by name, given the runner's name and given a computed one.
        script = (
            "import builtins\n"
            "import inspect\n"
            "import operator\n"
            "import os\n"
            "import subprocess\n"
            "def named(name):\n"
            '    inspect.getattr_static(subprocess, "run")(["git"])\n'  # 7
            '    object.__getattribute__(subprocess, "run")(["git"])\n'  # 8
            '    builtins.object.__getattribute__(subprocess, "Popen")\n'  # 9
            '    subprocess.__getattribute__("call")\n'  # 10
            '    operator.attrgetter("run")(subprocess)(["git"])\n'  # 11
            '    operator.attrgetter("DEVNULL", "run")(subprocess)\n'  # 12
            '    vars(subprocess)["run"](["git"])\n'  # 13
            '    subprocess.__dict__["run"](["git"])\n'  # 14
            '    inspect.getattr_static(subprocess, attr="run")\n'  # 15
            "    inspect.getattr_static(os, name)\n"  # 16
            "    operator.attrgetter(name)(subprocess)\n"  # 17
            "    vars(os)[name]\n"  # 18
            # A lookup of what starts nothing, on a module with no runner, or given no object, takes no runner.
            '    inspect.getattr_static(subprocess, "DEVNULL")\n'
            '    vars(subprocess)["PIPE"]\n'
            '    subprocess.__dict__["STDOUT"]\n'
            '    operator.attrgetter("DEVNULL")(subprocess)\n'
            "    vars(builtins)[name]\n"
            "    operator.attrgetter(name)()\n"
            "    vars()[name]\n"
        )
        path = "skills/alpha/scripts/tool.py"

        def taken(line: int, text: str) -> str:
            return unreadable(path, line, f"the program `{text}` runs, which a lookup by name takes")

        self.assertEqual(
            [
                taken(7, "inspect.getattr_static(subprocess, 'run')"),
                taken(8, "object.__getattribute__(subprocess, 'run')"),
                taken(9, "builtins.object.__getattribute__(subprocess, 'Popen')"),
                taken(10, "subprocess.__getattribute__('call')"),
                taken(11, "operator.attrgetter('run')(subprocess)"),
                taken(12, "operator.attrgetter('DEVNULL', 'run')(subprocess)"),
                taken(13, "vars(subprocess)['run']"),
                taken(14, "subprocess.__dict__['run']"),
                taken(15, "inspect.getattr_static(subprocess, attr='run')"),
                taken(16, "inspect.getattr_static(os, name)"),
                taken(17, "operator.attrgetter(name)(subprocess)"),
                taken(18, "vars(os)[name]"),
            ],
            CommandReader(ast.parse(script)).problems(path),
        )

    def test_a_lookup_by_name_takes_a_runner_only_where_its_module_and_name_may_give_one(self) -> None:
        script = (
            "import builtins\n"
            "import importlib\n"
            "import operator\n"
            "import os\n"
            "import subprocess\n"
            "def taken(name):\n"
            '    builtins.vars(subprocess)["run"](["git"])\n'  # 7
            # A dotted path on a module looked up by a computed name starts at a runner's name.
            '    operator.attrgetter("run.__call__")(importlib.import_module(name))\n'  # 8
            # A runner's attribute name on a module with no runner, a module looked up by a computed name given
            # what is no runner's name, and a receiver that reaches no name take no runner.
            "def quiet(name, make):\n"
            '    vars(os)["run"]\n'
            '    getattr(importlib.import_module(name), "DEVNULL")\n'
            '    operator.attrgetter("DEVNULL.run")(importlib.import_module(name))\n'
            '    getattr(make(), "run")\n'
            "    getattr(make(), name)\n"
        )
        path = "skills/alpha/scripts/tool.py"
        self.assertEqual(
            [
                unreadable(
                    path, 7, "the program `builtins.vars(subprocess)['run']` runs, which a lookup by name takes"
                ),
                unreadable(
                    path,
                    8,
                    "the program `operator.attrgetter('run.__call__')(importlib.import_module(name))` runs, which a "
                    "lookup by name takes",
                ),
            ],
            CommandReader(ast.parse(script)).problems(path),
        )

    def test_command_policies_report_a_runner_in_a_collection_indexed_where_it_is_written(self) -> None:
        script = (
            "import os\n"
            "import subprocess\n"
            "def mapping():\n"
            '    return {"run": subprocess.run}["run"](["ruby"])\n'  # 4
            "def pair():\n"
            '    return (subprocess.run, os.system)[0](["ruby"])\n'  # 6
            # A subscripted generic outside an annotation, an annotation, and an isinstance check start nothing.
            "GENERIC = subprocess.Popen[bytes]\n"
            "def typed(process: subprocess.Popen[bytes]) -> bool:\n"
            "    return isinstance(process, subprocess.Popen)\n"
        )
        path = "skills/alpha/scripts/tool.py"
        handed = "which the module hands on as a value"
        reader = CommandReader(ast.parse(script))
        self.assertEqual(
            [
                unreadable(path, 4, f"the program `subprocess.run` runs, {handed}"),
                unreadable(path, 6, f"the program `os.system` runs, {handed}"),
                unreadable(path, 6, f"the program `subprocess.run` runs, {handed}"),
            ],
            reader.problems(path),
        )
        types_only = "GENERIC = subprocess.Popen[bytes]\ndef typed(process: subprocess.Popen[bytes]) -> bool:\n"
        typed = f"import subprocess\n{types_only}    return isinstance(process, subprocess.Popen)\n"
        self.assertFalse(CommandReader(ast.parse(typed)).starts_process())
        self.assertEqual([], CommandReader(ast.parse(typed)).problems(path))

    def test_command_policies_read_a_runner_from_a_module_looked_up_by_name(self) -> None:
        computed = (
            "import importlib\n"
            "import sys\n"
            "def by_name(name):\n"
            "    importlib.import_module(name).run(['ruby'])\n"  # 4
            "    getattr(sys.modules[name], 'run')(['ruby'])\n"  # 5
            "    getattr(__import__(name), name)\n"  # 6
        )
        reader = CommandReader(ast.parse(computed))
        self.assertTrue(reader.starts_process())
        self.assertEqual(
            [
                unreadable(
                    "tool.py",
                    4,
                    "the program `importlib.import_module(name).run` runs, from a module looked up by a computed name",
                ),
                unreadable(
                    "tool.py", 5, "the program `getattr(sys.modules[name], 'run')` runs, which a lookup by name takes"
                ),
                unreadable(
                    "tool.py", 6, "the program `getattr(__import__(name), name)` runs, which a lookup by name takes"
                ),
            ],
            reader.problems("tool.py"),
        )
        # A module looked up by name whose attribute starts nothing, or one that is no runner's module, is read alike.
        quiet = (
            "import importlib\n"
            "import sys\n"
            "def f(name):\n"
            "    importlib.import_module('subprocess').DEVNULL\n"
            "    sys.modules['json'].run\n"
            "    importlib.import_module(name).loads('{}')\n"
        )
        self.assertFalse(CommandReader(ast.parse(quiet)).starts_process())
        self.assertEqual([], CommandReader(ast.parse(quiet)).problems("tool.py"))

    def test_command_reader_is_built_once_per_content_and_again_when_a_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deploy-meta/core.json": json.dumps({"tools": ["gh"], "selectable": False}),
                    "deploy-meta/alpha.json": json.dumps({"tools": ["gh"], "skill_deps": ["core"]}),
                    "deploy-meta/beta.json": json.dumps({"tools": ["gh"], "skill_deps": ["core"]}),
                    "skills/core/SKILL.md": "# core\n",
                    "skills/core/scripts/github.py": 'import subprocess\nsubprocess.run(["gh", "api"])\n',
                    "skills/alpha/SKILL.md": "# alpha\n",
                    "skills/alpha/scripts/run.py": "import github\n",
                    "skills/beta/SKILL.md": "# beta\n",
                    "skills/beta/scripts/run.py": "import github  # beta\n",
                },
            )
            skill_scripts._read_commands.cache_clear()
            with mock.patch.object(
                skill_scripts.CommandReader, "__init__", autospec=True, side_effect=skill_scripts.CommandReader.__init__
            ) as built:
                # The policies read core's github.py for each skill that reaches it, and the client policy reads every
                # script again, yet each content is read once in the run.
                self.assertEqual([], skill_command_problems(root, {"copilot", "gh"}, {"git", platform_support.PYTHON}))
                client_command_problems(root)
                self.assertEqual(3, built.call_count)
                github = root / "skills" / "core" / "scripts" / "github.py"
                github.write_text('import subprocess\nsubprocess.run(["copilot", "api"])\n', encoding="utf-8")
                self.assertEqual(
                    [
                        "skill alpha runs copilot through skills/core/scripts/github.py without declaring it in tools",
                        "skill alpha declares tool gh but never runs it",
                        "skill beta runs copilot through skills/core/scripts/github.py without declaring it in tools",
                        "skill beta declares tool gh but never runs it",
                        "skill core runs copilot without declaring it in tools",
                        "skill core declares tool gh but never runs it",
                    ],
                    skill_command_problems(root, {"copilot", "gh"}, {"git", "python"}),
                )
                self.assertEqual(4, built.call_count)

    def test_client_command_policy_resolves_assigned_runners_and_lookups_in_each_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "skills/alpha/SKILL.md": "# alpha\n",
                    "skills/alpha/scripts/tool.py": "import subprocess as sp\n"
                    "import shutil\n"
                    "from bounded_process import run_bounded\n"
                    "run = sp.run\n"
                    "alias = run\n"
                    'alias("git status")\n'
                    "def first():\n"
                    "    run = sp.check_output\n"
                    '    run("gh auth status")\n'
                    "def second():\n"
                    "    run = run_bounded\n"
                    "    lookup = shutil.which\n"
                    '    command = lookup("git")\n'
                    "    run(command, 5)\n",
                },
            )
            doc = '"Script results" in docs/adding-a-skill.md'
            git = (
                "run it through ejryan89-skill-core's git_client.py's GitClient, which bounds it and turns prompts off"
            )
            gh = (
                "run it through ejryan89-skill-core's github_client.py's GitHubClient, which bounds it and turns "
                "prompts off"
            )
            self.assertEqual(
                [
                    f"skills/alpha/scripts/tool.py:6 runs git itself; {git}; see {doc}",
                    f"skills/alpha/scripts/tool.py:9 runs gh itself; {gh}; see {doc}",
                    f"skills/alpha/scripts/tool.py:13 runs git itself; {git}; see {doc}",
                ],
                client_command_problems(root),
            )

    def test_command_reader_reports_a_runner_bound_to_several_callables_by_a_loop(self) -> None:
        reader = CommandReader(
            ast.parse('import os, subprocess\nfor runner in (subprocess.run, os.system):\n    runner("git status")\n')
        )
        self.assertEqual(
            ["tool.py:3 cannot resolve callable alias runner; use a single binding to a known callable"],
            reader.problems("tool.py"),
        )

    def test_command_policies_report_ambiguous_assigned_runners(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deploy-meta/alpha.json": "{}",
                    "skills/alpha/SKILL.md": "# alpha\n",
                    "skills/alpha/scripts/tool.py": "import subprocess\n"
                    "runner = subprocess.run\n"
                    "runner = None\n"
                    'runner("git status")\n',
                },
            )
            expected = [
                "skills/alpha/scripts/tool.py:4 cannot resolve callable alias runner; "
                "use a single binding to a known callable"
            ]
            self.assertEqual(expected, client_command_problems(root))
            self.assertEqual(expected, skill_command_problems(root, set(), {"git"}))

    def test_skill_command_policy_resolves_assigned_runners_clients_and_lookups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deploy-meta/alpha.json": "{}",
                    "skills/alpha/SKILL.md": "# alpha\n",
                    "skills/alpha/scripts/tool.py": "import subprocess\n"
                    "from github_client import GitHubClient\n"
                    "import shutil\n"
                    "runner = subprocess.run\n"
                    'runner("jq .")\n'
                    "client = GitHubClient\n"
                    "client()\n"
                    "lookup = shutil.which\n"
                    'lookup("copilot")\n',
                },
            )
            doc = '"Commands skills may run" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    "skill alpha runs copilot without declaring it in tools",
                    "skill alpha runs gh without declaring it in tools",
                    f"skill alpha runs jq, which a standard install lacks; see {doc}",
                ],
                skill_command_problems(root, {"copilot", "gh"}, {"git"}),
            )

    def test_script_contract_policy_detects_each_breach_and_honors_a_stated_exemption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "skills" / "alpha" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "run.py").write_text(
                "import json, sys\n"
                "def main(parser, value):\n"
                "    try:\n"
                "        value()\n"
                "    except OSError as exc:\n"
                "        parser.error(str(exc))\n"
                "    print(f'FAILED {value}', file=sys.stderr)\n"
                "    print(json.dumps(value))\n"
                "    if value:\n"
                "        return 3\n"
                "    parser.error('usage before any work is fine')\n"
                "    print('FAILED on stdout is fine')\n"
                "    return 2\n"
                "if __name__ == '__main__':\n"
                "    sys.exit(4)\n",
                encoding="utf-8",
            )
            (scripts / "hook.py").write_text(
                "import json\nEXIT_CONTRACT_EXEMPT = 'A hook protocol answers in JSON'\nprint(json.dumps({}))\n",
                encoding="utf-8",
            )
            (scripts / "vague.py").write_text("EXIT_CONTRACT_EXEMPT = ' '\n", encoding="utf-8")
            (scripts / "test_run.py").write_text("import sys\nsys.exit(5)\n", encoding="utf-8")
            # An Arabic-Indic one, which int() reads as 1, and a code past the digit limit are outside the contract.
            unreadable = (chr(0x0661), "1" + "0" * 5000)
            (scripts / "tool.sh").write_text(
                'set -e\n[ -n "$1" ] || exit 3\nexit 0  # exit 6 in a comment\n'
                + "".join(f"exit {code}\n" for code in unreadable),
                encoding="utf-8",
            )
            (scripts / "test_tool.sh").write_text("exit 7\n", encoding="utf-8")
            doc = '"Script results" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    f"skills/alpha/scripts/run.py:6 reports a failure through parser.error; print FAILED <reason> and "
                    f"exit 1; see {doc}",
                    f"skills/alpha/scripts/run.py:7 prints FAILED on stderr; print it on stdout; see {doc}",
                    f"skills/alpha/scripts/run.py:8 prints JSON; print one fact per line; see {doc}",
                    f"skills/alpha/scripts/run.py:10 exits 3; scripts exit only 0, 1, or 2; see {doc}",
                    f"skills/alpha/scripts/run.py:15 exits 4; scripts exit only 0, 1, or 2; see {doc}",
                    f"skills/alpha/scripts/tool.sh:2 exits 3; scripts exit only 0, 1, or 2; see {doc}",
                    *(
                        f"skills/alpha/scripts/tool.sh:{line} exits {code}; scripts exit only 0, 1, or 2; see {doc}"
                        for line, code in enumerate(unreadable, 4)
                    ),
                    "skills/alpha/scripts/vague.py: EXIT_CONTRACT_EXEMPT must be a non-empty string saying why",
                ],
                script_contract_problems(mark_skills(root)),
            )

    def test_script_contract_policy_reads_print_under_the_name_builtins_gives_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "skills/alpha/scripts/run.py": "import builtins, json, sys\n"
                    "from builtins import print as emit\n"
                    "def main():\n"
                    "    builtins.print(json.dumps({}))\n"
                    "    emit('FAILED x', file=sys.stderr)\n",
                },
            )
            doc = '"Script results" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    f"skills/alpha/scripts/run.py:4 prints JSON; print one fact per line; see {doc}",
                    f"skills/alpha/scripts/run.py:5 prints FAILED on stderr; print it on stdout; see {doc}",
                ],
                script_contract_problems(mark_skills(root)),
            )

    def test_script_contract_policy_allows_a_report_to_a_file_and_a_failed_line_on_stdout(self) -> None:
        # Writing to a file, a FAILED line on stdout, and JSON written to a file are each allowed.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "skills/alpha/scripts/run.py": "import json, sys\ndef main(handle):\n"
                    "    sys.stdout.write('FAILED x')\n    json.dump({}, handle)\n"
                    "    print(json.dumps({}), file=handle)\n    exit(1)\n"
                },
            )
            self.assertEqual([], script_contract_problems(mark_skills(root)))

    def test_print_choke_point_policy_refuses_a_stdout_line_that_may_hold_a_break(self) -> None:
        # Each print here carries a value that may hold a line break straight to stdout.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "skills/alpha/scripts/run.py": "import sys\n"
                    "from flat_text import flat_text\n"
                    "def main(exc, line, lines, count, separator):\n"
                    "    print(f'FAILED {exc}')\n"
                    "    print(line)\n"
                    "    sys.stdout.write(line)\n"
                    "    print('KEY', line)\n"
                    "    print(f'COUNT {len(lines)} {flat_text(line)}')\n"
                    "    print('\\n'.join(lines))\n"
                    "    print('A\\nB')\n"
                    "    print(flat_text(line), sep=separator)\n"
                    "    print(f'KEY {line}', file=sys.stdout)\n"
                    "    print(f'KEY {flat_text(line)}' if count else line)\n"
                    "    sys.__stdout__.writelines(lines)\n",
                },
            )
            refusal = (
                "prints text that may hold a line break; pass each outside value through flat_text, fact_line, or "
                'single_line, or state why in PRINT_ALLOWED; see "Script results" in docs/adding-a-skill.md'
            )
            self.assertEqual(
                [f"skills/alpha/scripts/run.py:{line} {refusal}" for line in range(4, 15)],
                skill_scripts.print_choke_point_problems(mark_skills(root)),
            )

    def test_print_choke_point_policy_reads_print_by_every_name_its_end_and_a_constant_only_while_bound_once(
        self,
    ) -> None:
        # print reached through builtins, a value given as end, and a constant the module binds again or a parameter
        # or local hides each carry a value that may hold a line break.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "skills/alpha/scripts/run.py": "import builtins\n"
                    "from builtins import print as emit\n"
                    "DONE = 'DONE all'\n"
                    "AGAIN = 'AGAIN once'\n"
                    "AGAIN = 'AGAIN twice'\n"
                    "def main(exc, line, ONCE='x'):\n"
                    "    builtins.print(f'FAILED {exc}')\n"
                    "    emit(f'FAILED {exc}')\n"
                    "    print('KEY', end=line)\n"
                    "    print(AGAIN)\n"
                    "    print(ONCE)\n"
                    "    DONE = line\n"
                    "    print(DONE)\n",
                },
            )
            refusal = (
                "prints text that may hold a line break; pass each outside value through flat_text, fact_line, or "
                'single_line, or state why in PRINT_ALLOWED; see "Script results" in docs/adding-a-skill.md'
            )
            self.assertEqual(
                [f"skills/alpha/scripts/run.py:{line} {refusal}" for line in (7, 8, 9, 10, 11, 13)],
                skill_scripts.print_choke_point_problems(mark_skills(root)),
            )

    def test_print_choke_point_policy_accepts_each_choke_point_and_what_is_no_fact_line(self) -> None:
        # Literals, a module's one-line constant, each choke point under any name its import gives it, an f-string or
        # either-or of them, and a join of flattened lines pass; stderr, a file, a test, a module under another
        # protocol, and a repository skill are not read.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "skills/alpha/scripts/run.py": "import sys\n"
                    "import flat_text\n"
                    "from flat_text import fact_line, single_line as one\n"
                    "from console import failed_line\n"
                    "DONE = 'DONE all'\n"
                    "def main(exc, line, lines, count, handle):\n"
                    "    print()\n"
                    "    print('VALID', 3)\n"
                    "    print(DONE)\n"
                    "    print(f'FAILED {flat_text.flat_text(str(exc))}')\n"
                    "    print(fact_line('KEY', line, count))\n"
                    "    print(one(f'COUNT {len(lines)} {line}'), end='')\n"
                    "    print(failed_line(exc), flush=True)\n"
                    "    print(f'KEY {one(line)}' if count else 'NONE')\n"
                    "    print('\\n'.join(one(item) for item in lines))\n"
                    "    sys.stdout.write(one(line))\n"
                    "    print(line, file=sys.stderr)\n"
                    "    print(line, file=handle)\n"
                    "    sys.stderr.write(line)\n",
                    "skills/alpha/scripts/test_run.py": "def test(line):\n    print(line)\n",
                    "skills/ejryan89-skill-core/scripts/console.py": "from flat_text import flat_text\n"
                    "def failed_line(error):\n    return f'FAILED {flat_text(str(error))}'\n"
                    "def report(error):\n    print(failed_line(error), end='')\n",
                    "skills/alpha/scripts/hook.py": "EXIT_CONTRACT_EXEMPT = 'answers a hook protocol'\n"
                    "def main(line):\n    print(line)\n",
                    ".claude/skills/beta/SKILL.md": "# beta\n",
                    ".claude/skills/beta/scripts/run.py": "def main(line):\n    print(line)\n",
                },
            )
            self.assertEqual([], skill_scripts.print_choke_point_problems(mark_skills(root)))

    def test_print_choke_point_policy_takes_a_reason_beside_the_code_and_reports_one_no_longer_needed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "skills/alpha/scripts/run.py": "PRINT_ALLOWED = {\n"
                    "    'print_report': 'prints the Markdown report as it is',\n"
                    "    'gone': 'printed once',\n"
                    "}\n"
                    "def print_report(report):\n    print(report, end='')\n",
                    "skills/alpha/scripts/vague.py": "PRINT_ALLOWED = {'main': ''}\ndef main(line):\n    print(line)\n",
                },
            )
            self.assertEqual(
                [
                    "skills/alpha/scripts/run.py: PRINT_ALLOWED allows gone, which holds nothing it sanctions; remove "
                    "the entry",
                    "skills/alpha/scripts/vague.py: PRINT_ALLOWED must map each function, class, or assignment to its "
                    "reason",
                    "skills/alpha/scripts/vague.py:3 prints text that may hold a line break; pass each outside value "
                    'through flat_text, fact_line, or single_line, or state why in PRINT_ALLOWED; see "Script '
                    'results" in docs/adding-a-skill.md',
                ],
                skill_scripts.print_choke_point_problems(mark_skills(root)),
            )

    def test_secret_named_function_policy_flags_trusted_but_not_untrusted_or_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "skills" / "example" / "scripts"
            scripts.mkdir(parents=True)
            (root / "deployer").mkdir()
            (scripts / "runtime.py").write_text(
                "def resolve_trusted_commit():\n    pass\n\n\n"
                "class Reader:\n    def _trusted_files(self):\n        pass\n\n\n"
                "def is_trusted():\n    pass\n\n\ndef untrusted_text():\n    pass\n",
                encoding="utf-8",
            )
            (scripts / "test_runtime.py").write_text("def test_trusted_commit():\n    pass\n", encoding="utf-8")
            (root / "deployer" / "check.py").write_text("def trusted_paths():\n    pass\n", encoding="utf-8")
            self.assertEqual(
                [
                    "deployer/check.py:1: function trusted_paths",
                    "skills/example/scripts/runtime.py:1: function resolve_trusted_commit",
                    "skills/example/scripts/runtime.py:6: function _trusted_files",
                ],
                [problem.split(" is named", 1)[0] for problem in secret_named_function_problems(mark_skills(root))],
            )

    def test_shell_command_extraction_marks_a_program_it_cannot_read(self) -> None:
        self.assertIn(f"{UNREAD_PROGRAM}$tool", shell_commands('xargs "$tool" foo'))

    def test_powershell_and_python_shell_readers_read_the_program_each_way_runs(self) -> None:
        sources = [
            "Start-Process rg -ArgumentList foo",
            'Start-Process -Wait -FilePath "rg"',
            'Invoke-Expression "rg foo"',
            "& (Get-Command rg) foo",
            '$p = "rg"; & $p foo',
            '. "$PSScriptRoot/helper.ps1"',
        ]
        self.assertEqual(
            [{"rg"}, {"rg"}, {"rg"}, {"rg"}, {f"{UNREAD_PROGRAM}$p"}, set()], skill_scripts.powershell_commands(sources)
        )
        for source in (
            'import subprocess\nsubprocess.run("cd x && rg foo", shell=True)\n',
            'import os\nos.system("git log | rg foo")\n',
            'import subprocess\nsubprocess.run(["bash", "-c", "rg foo"])\n',
        ):
            with self.subTest(source=source):
                self.assertIn("rg", CommandReader(ast.parse(source)).commands(set()))
        # Without a shell, the command's first word is its program, and the rest are its arguments.
        self.assertEqual(
            {"git"},
            CommandReader(ast.parse('import subprocess\nsubprocess.run("git log rg", shell=False)\n')).commands(set()),
        )

    def test_shell_command_extraction_ignores_keywords_patterns_and_functions(self) -> None:
        script = (
            "set -euo pipefail\n"
            'BASE=$(git rev-parse HEAD | tr -d "\\r") && echo "$BASE" >/dev/null\n'
            'case "$1" in\n'
            '  main|release/*) grep -q "x|jq" file ;;\n'
            '  *) helper "rg" ;;\n'
            "esac\n"
            'for name in alpha beta; do basename "$name"; done\n'
            'helper() { if [ -n "$1" ]; then sed -n 1p "$1"; fi; }\n'
            'while read -r line; do printf "%s\\n" "$line" | jq .; done < list\n'
            '"$SCRIPT" --flag; ./local.sh\n'
        )
        self.assertEqual(
            {"basename", "echo", "git", "grep", "jq", "printf", "read", "sed", "set", "tr"}, shell_commands(script)
        )

    def test_shell_command_extraction_runs_a_program_a_quoted_function_name_never_defined(self) -> None:
        # Bash refuses a quoted or escaped function name as not a valid identifier and defines no function, so the
        # later command of that name runs a program; an unquoted name still defines one, in each header form.
        for script, expected in (
            ("'f' () { jq .; }; f", {"f", "jq"}),
            ('"f" () { jq .; }; f', {"f", "jq"}),
            ("\\f () { jq .; }; f", {"f", "jq"}),
            ("function 'g' { jq .; }; g", {"g", "jq"}),
            ("function 'g' () { jq .; }; g", {"g", "jq"}),
            ('function "g" { jq .; }; g', {"g", "jq"}),
            ("function \\g { jq .; }; g", {"g", "jq"}),
            ("f () { jq .; }; f", {"jq"}),
            ("function g { jq .; }; g", {"jq"}),
            ("function g () { jq .; }; g", {"jq"}),
        ):
            with self.subTest(script=script):
                self.assertEqual(expected, shell_commands(script))

    def test_skill_command_policy_reads_the_commands_of_an_unclosed_fence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "deploy-meta/alpha.json": '{"tools": []}',
                    "skills/alpha/SKILL.md": "# alpha\n\n```bash\nrg foo\n",
                },
            )
            self.assertEqual(
                [f"skill alpha runs rg, which a standard install lacks; see {COMMANDS_DOC}"],
                skill_command_problems(root, set(), set()),
            )

    def test_command_policies_read_a_bytes_command_and_a_subclass_of_a_client(self) -> None:
        reader = CommandReader(
            ast.parse('import subprocess\nsubprocess.run(b"git status", shell=True)\nsubprocess.run([b"gh", b"api"])\n')
        )
        self.assertEqual({"git", "gh"}, reader.commands(set()))
        subclass = CommandReader(
            ast.parse(
                "from github_client import GitHubClient\n\n\nclass Mine(GitHubClient):\n    pass\n\n\n"
                "class Deeper(Mine):\n    pass\n\n\nDeeper().run(['api'])\n"
            )
        )
        self.assertTrue(subclass.starts_process())
        self.assertEqual({"gh"}, subclass.commands(set()))
        # A class that derives from no client starts nothing.
        self.assertFalse(CommandReader(ast.parse("class Mine(object):\n    pass\n\n\nMine()\n")).starts_process())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {"skills/alpha/scripts/run.py": 'import subprocess\nsubprocess.run([b"git", b"status"])\n'},
            )
            self.assertEqual(
                [
                    "skills/alpha/scripts/run.py:2 runs git itself; run it through ejryan89-skill-core's "
                    "git_client.py's "
                    'GitClient, which bounds it and turns prompts off; see "Script results" in docs/adding-a-skill.md'
                ],
                client_command_problems(mark_skills(root)),
            )

    def test_client_command_policy_detects_git_and_gh_a_script_runs_itself(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "skills" / "alpha" / "scripts"
            (scripts / "nested").mkdir(parents=True)
            (scripts / "tool.py").write_text(
                "import subprocess\n"
                'subprocess.run(["git", "fetch", "--all"], capture_output=True)\n'
                'runner(("gh", "api", "user"))\n'
                'subprocess.run("git status", shell=True)\n'
                'subprocess.check_output("gh")\n'
                'client.run(["fetch", "--all"])\n'
                'which("gh")\n'
                'print(["github", "gitlab"])\n',
                encoding="utf-8",
            )
            (scripts / "nested" / "helper.py").write_text('run(["git", "status"])\n', encoding="utf-8")
            (scripts / "test_tool.py").write_text('run(["git", "init"])\n', encoding="utf-8")
            (scripts / "tool.sh").write_text("git fetch --all\n", encoding="utf-8")
            core = root / "skills" / "ejryan89-skill-core" / "scripts"
            core.mkdir(parents=True)
            (core / "git_client.py").write_text('command = ["git", *arguments]\n', encoding="utf-8")
            doc = '"Script results" in docs/adding-a-skill.md'
            git = (
                "run it through ejryan89-skill-core's git_client.py's GitClient, which bounds it and turns prompts off"
            )
            gh = (
                "run it through ejryan89-skill-core's github_client.py's GitHubClient, which bounds it and turns "
                "prompts off"
            )
            self.assertEqual(
                [
                    f"skills/alpha/scripts/nested/helper.py:1 runs git itself; {git}; see {doc}",
                    f"skills/alpha/scripts/tool.py:2 runs git itself; {git}; see {doc}",
                    f"skills/alpha/scripts/tool.py:3 runs gh itself; {gh}; see {doc}",
                    f"skills/alpha/scripts/tool.py:4 runs git itself; {git}; see {doc}",
                    f"skills/alpha/scripts/tool.py:5 runs gh itself; {gh}; see {doc}",
                ],
                client_command_problems(mark_skills(root)),
            )

    def test_client_command_policy_resolves_aliases_imported_runners_and_bound_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "skills" / "alpha" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "tool.py").write_text(
                "import shutil\n"
                "import subprocess as sp\n"
                "from subprocess import Popen\n"
                "\n"
                "from bounded_process import run_bounded\n"
                "\n"
                'SHELL = "gh auth status"\n'  # 7
                'GIT = shutil.which("git")\n'  # 8
                "sp.run(SHELL, shell=True)\n"
                'Popen("git log")\n'  # 10
                'run_bounded([GIT, "status"], 5)\n'
                'sp.run("echo git")\n',
                encoding="utf-8",
            )
            doc = '"Script results" in docs/adding-a-skill.md'
            git = (
                "run it through ejryan89-skill-core's git_client.py's GitClient, which bounds it and turns prompts off"
            )
            gh = (
                "run it through ejryan89-skill-core's github_client.py's GitHubClient, which bounds it and turns "
                "prompts off"
            )
            self.assertEqual(
                [
                    f"skills/alpha/scripts/tool.py:7 runs gh itself; {gh}; see {doc}",
                    f"skills/alpha/scripts/tool.py:8 runs git itself; {git}; see {doc}",
                    f"skills/alpha/scripts/tool.py:10 runs git itself; {git}; see {doc}",
                ],
                client_command_problems(mark_skills(root)),
            )

    def test_gh_filter_policy_detects_jq_template_and_json_query_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "skills" / "alpha" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "tool.py").write_text(
                'run(["gh", "api", "repos/o/r/pulls", "--jq", ".[].number"])\n'
                'run(["pr", "view", "--json", "baseRefName", "-q", ".baseRefName"])\n'
                'run(["gh", "api", "x", "--template", "{{.}}"])\n'
                'run(["git", "fetch", "-q"])\n'
                'run(["gh", "pr", "view", "--json", "baseRefName"])\n',
                encoding="utf-8",
            )
            (scripts / "tool.sh").write_text(
                "gh pr view --json baseRefName -q .baseRefName\n"
                "BASE=$(gh api repos/o/r --jq=.default_branch)\n"
                "git fetch -q origin\n"
                "gh api repos/o/r --paginate --slurp\n",
                encoding="utf-8",
            )
            (scripts / "tool.ps1").write_text("gh api repos/o/r --template '{{.name}}'\n", encoding="utf-8")
            (scripts / "test_tool.py").write_text('self.assertNotIn("--jq", calls)\n', encoding="utf-8")
            doc = '"Commands skills may run" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    f"skills/alpha/scripts/tool.ps1:1 filters gh output with --template; parse its JSON in Python "
                    f"instead; see {doc}",
                    f"skills/alpha/scripts/tool.py:1 filters gh output with --jq; parse its JSON in Python instead; "
                    f"see {doc}",
                    f"skills/alpha/scripts/tool.py:2 filters gh output with -q; parse its JSON in Python instead; "
                    f"see {doc}",
                    f"skills/alpha/scripts/tool.py:3 filters gh output with --template; parse its JSON in Python "
                    f"instead; see {doc}",
                    f"skills/alpha/scripts/tool.sh:1 filters gh output with -q; parse its JSON in Python instead; "
                    f"see {doc}",
                    f"skills/alpha/scripts/tool.sh:2 filters gh output with --jq; parse its JSON in Python instead; "
                    f"see {doc}",
                ],
                gh_filter_problems(root),
            )

    def test_the_python_and_the_shell_readers_refuse_each_filter_form_alike(self) -> None:
        # One corpus of the forms gh's flag parser reads, held to both readers, so neither misses one the other sees.
        forms = {
            "--jq": ["--jq", ".x"],
            "--jq=": ["--jq=.x"],
            "--template": ["--template", "{{.}}"],
            "--template=": ["--template={{.}}"],
            "-q": ["-q", ".x"],
            "-q<filter>": ["-q.x"],
            "-t": ["-t", "{{.}}"],
            "-t<filter>": ["-t{{.}}"],
        }
        doc = '"Commands skills may run" in docs/adding-a-skill.md'
        for name, words in forms.items():
            flag = name.rstrip("=").replace("<filter>", "")
            arguments = ["gh", "pr", "view", "--json", "title", *words]
            for script, text in (
                ("tool.py", f"run({arguments!r})\n"),
                ("tool.sh", " ".join(arguments) + "\n"),
                ("tool.ps1", f"$title = {' '.join(arguments)}\n"),
            ):
                with self.subTest(form=name, script=script), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    write_fixture_tree(root, {f"skills/alpha/scripts/{script}": text})
                    self.assertEqual(
                        [
                            f"skills/alpha/scripts/{script}:1 filters gh output with {flag}; parse its JSON in Python "
                            f"instead; see {doc}"
                        ],
                        gh_filter_problems(root),
                    )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # git's -q, with no --json beside it or no gh on its line, and a word that only starts like a flag.
            write_fixture_tree(
                root,
                {
                    "skills/alpha/scripts/tool.py": 'run(["git", "fetch", "-q"])\n'
                    'run(["gh", "pr", "view", "--jquery"])\n'
                    # gh's -t is a title where the command writes no JSON.
                    'run(["pr", "create", "-t", "Title"])\n'
                    'run(["gh", "issue", "create", "-tTitle"])\n',
                    "skills/alpha/scripts/tool.sh": "git fetch -q origin\ngh pr view --jquery x\n"
                    'gh pr create -t "Title" --body x\n',
                    "skills/alpha/scripts/tool.ps1": "gh issue create -t Title `\n  --body x\n",
                },
            )
            self.assertEqual([], gh_filter_problems(root))

    def test_a_gh_filter_shorthand_is_read_after_shorthands_that_take_no_value(self) -> None:
        # gh's flag parser reads a cluster of shorthands letter by letter until one that takes a value.
        doc = '"Commands skills may run" in docs/adding-a-skill.md'
        refused = {
            "-iq": ["gh", "api", "repos/o/r", "-iq", ".name"],
            "-iq<filter>": ["gh", "api", "repos/o/r", "-iq.name"],
            "-it": ["gh", "api", "repos/o/r", "-it", "{{.name}}"],
            "-cq": ["gh", "pr", "view", "1", "--json", "comments", "-cq", ".comments"],
            "-wcq": ["gh", "pr", "view", "1", "--json", "comments", "-wcq.comments"],
            "-ct": ["gh", "issue", "view", "1", "--json", "comments", "-ct{{.}}"],
            # A letter no filter-taking command lists, as one a later gh may add, is read as taking no value.
            "-xq": ["gh", "run", "view", "1", "--json", "status", "-xq", ".status"],
            "-hxq": ["gh", "run", "view", "1", "--json", "status", "-hxq.status"],
        }
        for name, arguments in refused.items():
            flag = "-" + name.replace("<filter>", "")[-1]
            for script, text in (
                ("tool.py", f"run({arguments!r})\n"),
                ("tool.sh", " ".join(arguments) + "\n"),
                ("tool.ps1", f"$out = {' '.join(arguments)}\n"),
            ):
                with self.subTest(form=name, script=script), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    write_fixture_tree(root, {f"skills/alpha/scripts/{script}": text})
                    self.assertEqual(
                        [
                            f"skills/alpha/scripts/{script}:1 filters gh output with {flag}; parse its JSON in Python "
                            f"instead; see {doc}"
                        ],
                        gh_filter_problems(root),
                    )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # A shorthand that takes a value takes the rest of the word, a q or a t in it included, and a word of
            # letters that holds no filter shorthand is none.
            write_fixture_tree(
                root,
                {
                    "skills/alpha/scripts/tool.py": 'run(["gh", "api", "repos/o/r", "-ftitle=q"])\n'
                    'run(["gh", "api", "repos/o/r", "-Xquery"])\n'
                    'run(["gh", "pr", "list", "--json", "title", "-Lq"])\n'
                    'run(["gh", "pr", "list", "--json", "title", "-lq"])\n'
                    'run(["gh", "pr", "list", "--json", "title", "-R", "o/r", "-Rqo/r"])\n'
                    'run(["gh", "pr", "view", "--json", "title", "-cw"])\n'
                    # A long flag is no cluster, whatever letters follow its dashes.
                    'run(["gh", "pr", "view", "--json", "title", "--quiet", "--iq"])\n',
                    "skills/alpha/scripts/tool.sh": "gh api repos/o/r -ftitle=q -Hquery:x -i\n"
                    "gh pr view --json title --quiet --iq\n",
                },
            )
            self.assertEqual([], gh_filter_problems(root))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # A long filter flag's attached filter may run onto further lines.
            write_fixture_tree(
                root, {"skills/alpha/scripts/tool.py": 'run(["gh", "pr", "view", "--json", "title", "--jq=.a\\n.b"])\n'}
            )
            self.assertEqual(
                [
                    "skills/alpha/scripts/tool.py:1 filters gh output with --jq; parse its JSON in Python instead; "
                    f"see {doc}"
                ],
                gh_filter_problems(root),
            )

    def test_the_gh_filter_shorthands_are_read_on_gh_api_and_on_a_continued_command(self) -> None:
        doc = '"Commands skills may run" in docs/adding-a-skill.md'

        def refusal(script: str, line: int, flag: str) -> str:
            return f"{script}:{line} filters gh output with {flag}; parse its JSON in Python instead; see {doc}"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    # gh api writes JSON with no --json, so its shorthands filter it, with or without gh first.
                    "skills/alpha/scripts/tool.py": 'run(["api", "repos/o/r", "-q", ".name"])\n'
                    'run(["gh", "api", "repos/o/r", "-t{{.name}}"])\n'
                    # gh with words it unpacks, which may ask for JSON, and a command joined from lists by `+`; a
                    # joined title is no filter.
                    'run(["gh", *base, "-t", "{{.}}"])\n'  # 3
                    'run(["gh", "pr", "list", "--json", "title"] + ["-t", "{{.}}"])\n'  # 4
                    'run(base + ["--json", "title", "-q", ".x"])\n'  # 5
                    'run(["gh", "pr", "create"] + ["-t", "Title"])\n'
                    # A `+` whose left side joins a list to words it does not write, and a gh list given more words.
                    'run(base + ["--json", "title"] + ["-t", "{{.}}"])\n'  # 7
                    'run(["gh", "pr", "view", "1"] + extra + ["-t", "{{.}}"])\n'  # 8
                    'run(["gh", "pr", "list", "-t", "{{.}}"] + extra)\n'  # 9
                    # --json with its fields after `=`, and a filter that runs onto a second line.
                    'run(["gh", "pr", "view", "--json=title", "-q", ".title"])\n'  # 10
                    'run(["gh", "pr", "view", "--json", "title", "-q.title\\n.x"])\n'  # 11
                    # A word a name holds, a list a name starts, another program's list that unpacks words, and an
                    # empty list are no gh filter.
                    'run(["gh", "pr", "create", "-t", title])\n'
                    'run([program, "pr", "list", "-t", "x"])\n'
                    'run(["git", *options, "-q"])\n'
                    "run([])\n",
                    "skills/alpha/scripts/tool.sh": "name=$(gh api repos/o/r -t '{{.name}}')\n"
                    # A command continued by a backslash is read whole, by its first line.
                    "gh pr view --json title \\\n  --jq .title\n"
                    "gh pr list --json title \\\n  -t '{{.}}'\n"
                    # A backtick that ends a Bash line continues nothing.
                    "gh pr view --json title # the `title`\n"
                    "echo -t done\n",
                    "skills/alpha/scripts/tool.ps1": "gh pr view --json title `\n  -q .title\n",
                    "skills/Zeta/scripts/tool.bash": "gh pr view --json title -q .title\n",
                    # A repository skill's script is a skill script too.
                    ".claude/skills/beta/scripts/tool.sh": "gh api repos/o/r --jq .name\n",
                },
            )
            # In the order the paths' text sorts in on every platform, where Windows would sort Zeta after alpha.
            self.assertEqual(
                [
                    refusal(".claude/skills/beta/scripts/tool.sh", 1, "--jq"),
                    refusal("skills/Zeta/scripts/tool.bash", 1, "-q"),
                    refusal("skills/alpha/scripts/tool.ps1", 1, "-q"),
                    refusal("skills/alpha/scripts/tool.py", 1, "-q"),
                    refusal("skills/alpha/scripts/tool.py", 2, "-t"),
                    refusal("skills/alpha/scripts/tool.py", 3, "-t"),
                    refusal("skills/alpha/scripts/tool.py", 4, "-t"),
                    refusal("skills/alpha/scripts/tool.py", 5, "-q"),
                    refusal("skills/alpha/scripts/tool.py", 7, "-t"),
                    refusal("skills/alpha/scripts/tool.py", 8, "-t"),
                    refusal("skills/alpha/scripts/tool.py", 9, "-t"),
                    refusal("skills/alpha/scripts/tool.py", 10, "-q"),
                    refusal("skills/alpha/scripts/tool.py", 11, "-q"),
                    refusal("skills/alpha/scripts/tool.sh", 1, "-t"),
                    refusal("skills/alpha/scripts/tool.sh", 2, "--jq"),
                    refusal("skills/alpha/scripts/tool.sh", 4, "-t"),
                ],
                gh_filter_problems(root),
            )

    def test_skill_command_policy_detects_undeclared_unused_and_nonstandard_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            for skill, metadata in {
                "alpha": {"tools": ["copilot"]},
                "beta": {},
                "gamma": {"tools": ["gh"]},
            }.items():
                (root / "deploy-meta" / f"{skill}.json").write_text(json.dumps(metadata), encoding="utf-8")
                (root / "skills" / skill / "scripts").mkdir(parents=True)
            (root / "skills" / "alpha" / "SKILL.md").write_text("Prose that mentions `gh` only.\n", encoding="utf-8")
            (root / "skills" / "alpha" / "scripts" / "run.py").write_text(
                'run(["gh", "pr", "list"])\n', encoding="utf-8"
            )
            (root / "skills" / "alpha" / "scripts" / "test_run.py").write_text('which("copilot")\n', encoding="utf-8")
            (root / "skills" / "beta" / "SKILL.md").write_text(
                "```bash\ngit status && dotnet-format --version\n```\n", encoding="utf-8"
            )
            (root / "skills" / "gamma" / "SKILL.md").write_text("Gamma\n", encoding="utf-8")
            (root / "skills" / "gamma" / "scripts" / "run.sh").write_text(
                "  gh pr view | jq .title\nrg -n TODO .\n", encoding="utf-8"
            )
            (root / "skills" / "gamma" / "scripts" / "fetch.py").write_text(
                'subprocess.run(["yq", "."])\nshutil.which("curl")\n', encoding="utf-8"
            )
            doc = '"Commands skills may run" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    "skill alpha runs gh without declaring it in tools",
                    "skill alpha declares tool copilot but never runs it",
                    "skill beta runs dotnet-format without declaring it in tools",
                    f"skill gamma runs jq, which a standard install lacks; see {doc}",
                    f"skill gamma runs rg, which a standard install lacks; see {doc}",
                    f"skill gamma runs yq, which a standard install lacks; see {doc}",
                ],
                skill_command_problems(root, {"copilot", "dotnet-format", "gh"}, {"curl", "git"}),
            )

    def test_skill_command_policy_reads_python_commands_through_imports_names_and_skill_core(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "deploy-meta/alpha.json": "{}",
                "skills/alpha/SKILL.md": "# alpha\n",
                # A runner reached through an alias or a from-import, or a keyword argument.
                "skills/alpha/scripts/aliased.py": 'import subprocess as sp\n\nsp.run(["yq", "."])\n',
                "skills/alpha/scripts/imported.py": "from subprocess import check_output\n\n"
                'check_output(["rg", "x"])\n',
                "skills/alpha/scripts/keyword.py": 'import subprocess\n\nsubprocess.run(args=["perl", "-e", "1"])\n',
                "skills/alpha/scripts/shell.py": 'import os\n\nos.system("node --version")\n',
                # ejryan89-skill-core's bounded runner, given a command held in a name or built from one.
                "skills/alpha/scripts/bounded.py": 'from bounded_process import run_bounded\n\nCOMMAND = ["jq", "."]\n'
                "run_bounded(COMMAND, 5)\n",
                "skills/alpha/scripts/built.py": 'import bounded_process\n\nBASE = ["dotnet-format"]\n'
                'bounded_process.run_bounded([*BASE, "--version"], timeout=5)\n',
                # ejryan89-skill-core's clients run their command, and a program looked up is one the script runs.
                "skills/alpha/scripts/client.py": "from github_client import GitHubClient\n\n"
                'GitHubClient().run(["api"])\n',
                "skills/alpha/scripts/git.py": 'import git_client\n\ngit_client.GitClient().run(["status"])\n',
                "skills/alpha/scripts/lookup.py": 'import shutil as sh\n\nPROGRAM = sh.which("copilot")\n',
                # ejryan89-skill-core's locator looks a program up as shutil.which does, and so do its lists of every
                # match.
                "skills/alpha/scripts/walked.py": "from executable_locator import path_programs\n\n"
                'PROGRAMS = path_programs("ruby")\n',
                "skills/alpha/scripts/candidates.py": "import executable_locator\n\n"
                'FOUND = executable_locator.find_executables("lua")\n',
                # A command whose program the locator names by its path runs the program the command names.
                "skills/alpha/scripts/located.py": "from bounded_process import run_bounded\n"
                "from executable_locator import located_command\n\n"
                'run_bounded(located_command(["swift", "--version"]), 5)\n',
                # Neither a string that reads like a call nor a list of other words is a command.
                "skills/alpha/scripts/quiet.py": 'TEXT = "subprocess.run([\\"zsh\\"])"\nLABELS = ["zip", "unzip"]\n'
                'print(["python", "-B"])\n',
            }
            write_fixture_tree(root, files)
            doc = '"Commands skills may run" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    "skill alpha runs copilot without declaring it in tools",
                    "skill alpha runs dotnet-format without declaring it in tools",
                    "skill alpha runs gh without declaring it in tools",
                    *(
                        f"skill alpha runs {name}, which a standard install lacks; see {doc}"
                        for name in ("jq", "lua", "node", "perl", "rg", "ruby", "swift", "yq")
                    ),
                ],
                skill_command_problems(root, {"copilot", "dotnet-format", "gh"}, {"git", "python"}),
            )

    def test_skill_command_policy_reads_powershell_scripts_and_fences_through_powershell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "deploy-meta/beta.json": json.dumps({"tools": ["gh"]}),
                "skills/beta/SKILL.md": "# beta\n\n```powershell\njq . data.json\n```\n\n"
                "```pwsh\ncopilot --version\n```\n",
                # A function the script defines, a cmdlet or alias PowerShell ships, and a path are not programs it
                # names; a cmdlet of a module PowerShell does not ship is one, and a command held in a variable is one
                # the policy cannot read.
                "skills/beta/scripts/run.ps1": "function Invoke-Helper { param($Value) $Value }\n"
                "Get-ChildItem | ForEach-Object { $_.Name }\n"
                "git status\n"
                "& 'gh' api user\n"
                "dotnet-format.exe --version\n"
                "Invoke-Helper 1\n"
                "gci\n"
                ".\\local.ps1\n"
                "& $env:TOOL\n"
                "nonesuch --flag\n"
                "Invoke-ScriptAnalyzer -Path run.ps1\n",
            }
            write_fixture_tree(root, files)
            doc = '"Commands skills may run" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    "skill beta runs a program it names by `$env:TOOL`, which the policy cannot read; name the "
                    f"program with a literal; see {doc}",
                    "skill beta runs copilot without declaring it in tools",
                    "skill beta runs dotnet-format without declaring it in tools",
                    f"skill beta runs Invoke-ScriptAnalyzer, which a standard install lacks; see {doc}",
                    f"skill beta runs jq, which a standard install lacks; see {doc}",
                    f"skill beta runs nonesuch, which a standard install lacks; see {doc}",
                ],
                skill_command_problems(root, {"copilot", "dotnet-format", "gh"}, {"git", "python"}),
            )

    def test_skill_command_policy_reaches_a_dependency_by_its_imports_not_by_a_string(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "deploy-meta/core.json": json.dumps({"tools": ["gh"], "selectable": False}),
                "deploy-meta/aliased.json": json.dumps({"skill_deps": ["core"]}),
                "deploy-meta/quoted.json": json.dumps({"skill_deps": ["core"]}),
                "skills/core/SKILL.md": "# core\n",
                "skills/core/scripts/github.py": 'run(["gh", "api"])\n',
                "skills/aliased/SKILL.md": "# aliased\n",
                "skills/aliased/scripts/run.py": "import json, github as api\n",
                # Text that reads like an import is not one.
                "skills/quoted/SKILL.md": "# quoted\n",
                "skills/quoted/scripts/run.py": 'HELP = """\nimport github\nfrom github import Client\n"""\n',
                # A sanction's reason names a file that holds a copy, which the module does not run.
                "deploy-meta/sanctioned.json": json.dumps({"skill_deps": ["core"]}),
                "skills/core/scripts/store.py": (
                    'DUPLICATION_ALLOWED = {\n    "LIMIT": "skills/core/scripts/github.py holds a copy",\n}\n'
                ),
                "skills/sanctioned/SKILL.md": "# sanctioned\n",
                "skills/sanctioned/scripts/run.py": "import store\n",
            }
            write_fixture_tree(root, files)
            self.assertEqual(
                ["skill aliased runs gh through skills/core/scripts/github.py without declaring it in tools"],
                skill_command_problems(root, {"gh"}, {"git", "python"}),
            )

    def test_skill_command_policy_counts_the_dependency_scripts_a_skill_reaches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            for skill, metadata in {
                "core": {"tools": ["copilot", "gh"], "selectable": False},
                "named": {"tools": ["copilot", "gh"], "skill_deps": ["core"]},
                "imports": {"optional_tools": ["gh"], "skill_deps": ["core"]},
                "unused": {"optional_tools": ["dotnet-format"], "skill_deps": ["core"]},
                "inherits": {"skill_deps": ["core"]},
                "silent": {"skill_deps": ["core"]},
            }.items():
                (root / "deploy-meta" / f"{skill}.json").write_text(json.dumps(metadata), encoding="utf-8")
                (root / "skills" / skill / "scripts").mkdir(parents=True)
                (root / "skills" / skill / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
            core = root / "skills" / "core" / "scripts"
            (core / "pipeline.py").write_text("from github import Client\nimport store\n", encoding="utf-8")
            (core / "github.py").write_text('run(["gh", "api"])\n', encoding="utf-8")
            (core / "store.py").write_text("import json\n", encoding="utf-8")
            (core / "hosts.py").write_text('shutil.which("copilot")\n', encoding="utf-8")
            (core / "test_hosts.py").write_text("import pipeline\n", encoding="utf-8")
            # A skill reaches pipeline.py by naming its path, and github.py through pipeline.py's import, but never
            # hosts.py, so copilot is declared without being run.
            (root / "skills" / "named" / "SKILL.md").write_text(
                '```bash\npython -B "${CLAUDE_SKILL_DIR}/../core/scripts/pipeline.py" run\n```\n', encoding="utf-8"
            )
            (root / "skills" / "imports" / "scripts" / "run.py").write_text(
                "sys.path.insert(0, str(CORE))\nfrom store import load\nimport github\n", encoding="utf-8"
            )
            (root / "skills" / "unused" / "scripts" / "run.py").write_text("import store\n", encoding="utf-8")
            (root / "skills" / "inherits" / "scripts" / "run.py").write_text("import store\n", encoding="utf-8")
            # silent reaches github.py, so gh is its tool too: a skill that reaches the shared client declares gh,
            # and one that reaches only store.py, like inherits, declares nothing.
            (root / "skills" / "silent" / "scripts" / "run.py").write_text("import github\n", encoding="utf-8")
            self.assertEqual(
                [
                    "skill named declares tool copilot but never runs it",
                    "skill silent runs gh through skills/core/scripts/github.py without declaring it in tools",
                    "skill unused declares tool dotnet-format but never runs it",
                ],
                skill_command_problems(root, {"copilot", "dotnet-format", "gh"}, {"git", "python"}),
            )

    def test_skill_command_policy_follows_dependencies_of_dependencies(self) -> None:
        # ejryan89-code-review-core depends on ejryan89-skill-core, so a skill that reaches ejryan89-code-review-core
        # reaches ejryan89-skill-core too.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            for skill, metadata in {
                "base": {"tools": ["gh"], "selectable": False},
                "middle": {"tools": ["gh"], "skill_deps": ["base"], "selectable": False},
                "top": {"tools": ["gh"], "skill_deps": ["middle"]},
                "idle": {"tools": ["gh"], "skill_deps": ["middle"]},
            }.items():
                (root / "deploy-meta" / f"{skill}.json").write_text(json.dumps(metadata), encoding="utf-8")
                (root / "skills" / skill / "scripts").mkdir(parents=True)
                (root / "skills" / skill / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
            (root / "skills" / "base" / "scripts" / "client.py").write_text('run(["gh", "api"])\n', encoding="utf-8")
            middle = root / "skills" / "middle" / "scripts"
            (middle / "pipeline.py").write_text("import client\n", encoding="utf-8")
            (middle / "store.py").write_text("import json\n", encoding="utf-8")
            # top reaches base's client.py through middle's pipeline.py; idle reaches only middle's store.py.
            (root / "skills" / "top" / "scripts" / "run.py").write_text("import pipeline\n", encoding="utf-8")
            (root / "skills" / "idle" / "scripts" / "run.py").write_text("import store\n", encoding="utf-8")
            self.assertEqual(
                ["skill idle declares tool gh but never runs it"],
                skill_command_problems(root, {"gh"}, {"git", "python"}),
            )

    def test_console_setup_policy_detects_a_missing_late_unresolved_or_bypassed_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            main = 'if __name__ == "__main__":\n'
            imported = "from console import run_main\n\n"
            files = {
                "skills/alpha/scripts/good.py": f"{imported}{main}    run_main(main)\n",
                "skills/alpha/scripts/aliased.py": "import console as c\n\n"
                f"{main}    c.run_main(main, errors='replace')\n",
                "skills/alpha/scripts/arguments.py": f"{imported}{main}    run_main(lambda: main(sys.argv[1:]))\n",
                "skills/alpha/scripts/late.py": f"{imported}{main}    parse()\n    run_main(main)\n",
                "skills/alpha/scripts/missing.py": f"{main}    raise SystemExit(main())\n",
                # The console setup alone leaves an unhandled exception to print a traceback.
                "skills/alpha/scripts/setup_only.py": "from console import use_utf8_output\n\n"
                f"{main}    use_utf8_output()\n    raise SystemExit(main())\n",
                # A function of the same name that is not ejryan89-skill-core's is a second runner.
                "skills/alpha/scripts/local.py": f"def run_main(main):\n    main()\n\n\n{main}    run_main(main)\n",
                "skills/alpha/scripts/copied.py": 'def main():\n    sys.stdout.reconfigure(encoding="utf-8")\n\n\n'
                f"{imported}{main}    run_main(main)\n",
                "skills/alpha/scripts/library.py": "def helper():\n    return 1\n",
                # Suites are not entry points.
                "skills/alpha/scripts/test_good.py": f"{main}    unittest.main()\n",
                "skills/ejryan89-skill-core/scripts/console.py": 'sys.stdout.reconfigure(encoding="utf-8")\n',
                # deploy.py, the deployer, and tools/ are held as skill scripts are.
                "deploy.py": f"{imported}{main}    raise SystemExit(main())\n",
                "deployer/platform_support.py": "def setup():\n    reconfigure = sys.stdout.reconfigure\n"
                '    reconfigure(encoding="utf-8")\n',
                "deployer/cli.py": "def main():\n    return 0\n",
                "tools/good_tool.py": f"{imported}{main}    run_main(main, errors='backslashreplace')\n",
                "tools/late_tool.py": f"{imported}{main}    options = parse()\n    run_main(main)\n",
                "tools/test_tool.py": f"{main}    unittest.main()\n",
            }
            write_fixture_tree(root, files)
            mark_skills(root)
            doc = '"Script results" in docs/adding-a-skill.md'
            late = f"does not call ejryan89-skill-core's run_main() first in its __main__ block; see {doc}"
            copied = f"reconfigures a stream itself; call run_main() from ejryan89-skill-core instead; see {doc}"
            self.assertEqual(
                [
                    f"deploy.py:3 {late}",
                    f"deployer/platform_support.py:3 {copied}",
                    f"skills/alpha/scripts/copied.py:2 {copied}",
                    f"skills/alpha/scripts/late.py:3 {late}",
                    f"skills/alpha/scripts/local.py:5 {late}",
                    f"skills/alpha/scripts/missing.py:1 {late}",
                    f"skills/alpha/scripts/setup_only.py:3 {late}",
                    f"tools/late_tool.py:3 {late}",
                ],
                console_setup_problems(root),
            )

    def test_console_setup_policy_reads_every_main_guard_and_a_main_run_without_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            body = "def main():\n    return 0\n\n\n"
            files = {
                "skills/demo/scripts/reversed.py": f'{body}if "__main__" == __name__:\n    main()\n',
                "skills/demo/scripts/in_try.py": f"{body}try:\n"
                '    if __name__ == "__main__":\n        main()\nexcept ImportError:\n    pass\n',
                "skills/demo/scripts/unguarded.py": f"import sys\n\n\n{body}sys.exit(main())\n",
                "skills/demo/scripts/control.py": f'{body}if __name__ == "__main__":\n    main()\n',
                "skills/demo/scripts/good.py": f"from console import run_main\n\n\n{body}"
                'if "__main__" == __name__:\n    run_main(main)\n',
            }
            write_fixture_tree(root, files)
            mark_skills(root)
            doc = '"Script results" in docs/adding-a-skill.md'
            late = f"does not call ejryan89-skill-core's run_main() first in its __main__ block; see {doc}"
            self.assertEqual(
                [
                    f"skills/demo/scripts/control.py:5 {late}",
                    f"skills/demo/scripts/in_try.py:6 {late}",
                    f"skills/demo/scripts/reversed.py:5 {late}",
                    'skills/demo/scripts/unguarded.py:8 runs main outside an `if __name__ == "__main__":` block, so '
                    "importing it runs it; run ejryan89-skill-core's run_main(main) first in that block instead; "
                    f"see {doc}",
                ],
                sorted(console_setup_problems(root)),
            )
            self.assertEqual(
                [8], [block.lineno for block in main_blocks(ast.parse(files["skills/demo/scripts/good.py"]))]
            )

    def test_console_setup_policy_detects_a_stream_replaced_wrapped_or_reconfigured_through_an_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "deployer/replaced.py": "import io\nimport sys\n\n\ndef setup():\n"
                '    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")\n',  # 6
                "deployer/renamed.py": "import codecs\nimport sys as system\n\n"
                'system.stderr = codecs.getwriter("utf-8")(system.stderr.buffer)\n',  # 4
                "deployer/set.py": 'import sys\n\nsetattr(sys, "stdout", open(1, "w", encoding="utf-8"))\n',  # 3
                "tools/wrapped.py": "import io\nimport sys\n\n"
                'OUT = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")\n',  # 4
                "tools/bound.py": 'import sys\n\nfix = sys.stderr.reconfigure\nfix(encoding="utf-8")\n',  # 4
                "tools/fetched.py": 'import sys\n\ngetattr(sys.stdout, "reconfigure")(encoding="utf-8")\n'  # 3
                "sys.stdout.reconfigure(**OPTIONS)\n",  # 4: the options may hold an encoding
                # Wrapping a file that is not a standard stream, writing to a stream, or naming one changes no stream.
                "tools/plain.py": "import io\nimport sys\n\n\ndef read(raw):\n"
                '    text = io.TextIOWrapper(raw, encoding="utf-8")\n'
                "    out = sys.stdout\n"
                "    out.write(text.read())\n"
                "    sys.stderr.reconfigure(line_buffering=True)\n",
            }
            write_fixture_tree(root, files)
            mark_skills(root)
            doc = '"Script results" in docs/adding-a-skill.md'
            copied = f"reconfigures a stream itself; call run_main() from ejryan89-skill-core instead; see {doc}"
            self.assertEqual(
                [
                    f"deployer/renamed.py:4 {copied}",
                    f"deployer/replaced.py:6 {copied}",
                    f"deployer/set.py:3 {copied}",
                    f"tools/bound.py:4 {copied}",
                    f"tools/fetched.py:3 {copied}",
                    f"tools/fetched.py:4 {copied}",
                    f"tools/wrapped.py:4 {copied}",
                ],
                console_setup_problems(root),
            )


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


@pytest.mark.parametrize("callable_name", [*COMMAND_RUNNERS, *CLIENT_CLASSES])
def test_process_detection_uses_every_command_runner_and_client_with_dynamic_arguments(callable_name: str) -> None:
    module, _, function = callable_name.rpartition(".")
    # An event loop's runner is a method of the loop a call returns.
    loop = module == EVENT_LOOP
    header = "import asyncio\nprocess = asyncio.new_event_loop()\n" if loop else f"import {module} as process\n"
    # The command goes where the runner takes it: os's spawn calls take a mode first, and an event loop's runners a
    # protocol factory.
    position = COMMAND_RUNNERS[callable_name][0] if callable_name in COMMAND_RUNNERS else 0
    arguments = ", ".join([*(["mode"] * position), "command"])
    tree = ast.parse(f"{header}start = process.{function}\nstart({arguments})\n")
    reader = CommandReader(tree)
    assert reader.starts_process()
    # A runner given a command the module never binds reports it; a client's argument is not a command.
    line = header.count("\n") + 2
    expected = (
        [unreadable("start.py", line, "the program in `command`, which the module binds to nothing it reads")]
        if callable_name in COMMAND_RUNNERS
        else []
    )
    assert expected == reader.problems("start.py")
    if not loop:
        assert not CommandReader(ast.parse(f"from {module} import {function}\n")).starts_process()


@pytest.mark.parametrize(
    ("expression", "prefix"),
    [
        ('f"gh api {endpoint} --template {template}"', "gh api "),
        ('f"gh " "api " f"{endpoint} copilot"', "gh api "),
        ('f"gh.exe api {endpoint}"', "gh.exe api "),
        ('f"gh"', "gh"),
    ],
)
def test_command_reader_keeps_only_the_f_strings_initial_literal_arguments(expression: str, prefix: str) -> None:
    tree = ast.parse(expression)
    statement = tree.body[0]
    if not isinstance(statement, ast.Expr):
        raise AssertionError("Expected an f-string expression")
    literals = CommandReader(tree).program_literals(statement.value)
    constants = [node for node in literals if isinstance(node, ast.Constant)]
    assert literals == constants
    assert [prefix] == [node.value for node in constants]
    assert [node.lineno for node in constants] == [1]


def test_optional_tool_placement_policy_reports_a_tool_optional_for_every_skill_listed_as_required(
    tmp_path: Path,
) -> None:
    (tmp_path / "deploy-meta").mkdir()
    metadata = tmp_path / "deploy-meta" / "review.json"
    metadata.write_text(json.dumps({"tools": ["copilot", "gh"], "optional_tools": ["pdftotext"]}), encoding="utf-8")
    assert optional_tool_placement_problems(tmp_path, {"copilot"}) == [
        "skill review lists copilot under tools, which the catalogue makes optional for every skill; declare it "
        'in optional_tools; see "Commands skills may run" in docs/adding-a-skill.md'
    ]
    metadata.write_text(json.dumps({"tools": ["gh"], "optional_tools": ["copilot", "pdftotext"]}), encoding="utf-8")
    assert optional_tool_placement_problems(tmp_path, {"copilot"}) == []


@pytest.mark.parametrize(
    "expression",
    [
        'f"{program} api gh"',
        'f"   {program} api"',
        'f"{program=}"',
        '[f"{program}", "api"]',
        '(f"{program}", "api")',
        'f"{program}" + " gh api"',
    ],
)
def test_command_policies_report_f_strings_without_a_readable_literal_prefix(tmp_path: Path, expression: str) -> None:
    write_fixture_tree(
        tmp_path,
        {
            "deploy-meta/alpha.json": "{}",
            "skills/alpha/SKILL.md": "# alpha\n",
            "skills/alpha/scripts/tool.py": "import subprocess\n"
            "runner = subprocess.run\n"
            f"COMMAND = {expression}\n"
            "runner(args=COMMAND)\n"
            "runner(COMMAND)\n"
            'print(f"{label}")\n',
        },
    )
    expected = [unreadable("skills/alpha/scripts/tool.py", 3, "f-string command without a literal program prefix")]
    assert expected == client_command_problems(tmp_path)
    assert expected == skill_command_problems(tmp_path, {"gh"}, {"git", "python"})


@pytest.mark.parametrize(
    ("label", "source"),
    [
        pytest.param("pty", "import pty\npty.spawn(['ruby', '-e'])\n", id="pty"),
        pytest.param("startfile", "import os\nos.startfile('ruby')\n", id="startfile"),
        pytest.param(
            "loop",
            "import asyncio\n"
            "async def start():\n"
            "    loop = asyncio.get_running_loop()\n"
            "    await loop.subprocess_exec(asyncio.SubprocessProtocol, 'ruby', '-e')\n"
            "    run = loop.subprocess_shell\n"
            "    await run(asyncio.SubprocessProtocol, 'jq . | ruby -e 1')\n",
            id="loop",
        ),
        pytest.param(
            "asyncio.subprocess",
            "import asyncio.subprocess\n"
            "asyncio.subprocess.create_subprocess_exec('ruby', '-e')\n"
            "asyncio.subprocess.create_subprocess_shell('ruby -e 1')\n",
            id="asyncio.subprocess",
        ),
    ],
)
def test_command_policies_read_pty_startfile_an_event_loop_and_asyncio_subprocess_runners(
    label: str, source: str
) -> None:
    reader = CommandReader(ast.parse(source))
    assert reader.starts_process()
    assert ({"ruby", "jq"} if label == "loop" else {"ruby"}) == reader.commands(set())
    assert reader.problems("tool.py") == []


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "import importlib\nimportlib.import_module('subprocess').run(['ruby', '.'])\n", id="import_module"
        ),
        pytest.param("__import__('subprocess').run('ruby .', shell=True)\n", id="__import__"),
        pytest.param("import sys\nsys.modules['subprocess'].run(['ruby', '.'])\n", id="sys.modules"),
        pytest.param(
            "import importlib\nmodule = importlib.import_module('subprocess')\nmodule.run(['ruby'])\n",
            id="bound module",
        ),
        pytest.param(
            "import importlib\nrun = importlib.import_module('subprocess').run\nrun(['ruby'])\n", id="bound runner"
        ),
        pytest.param(
            "import importlib\nimportlib.import_module('asyncio.subprocess').create_subprocess_exec('ruby')\n",
            id="dotted",
        ),
    ],
)
def test_command_policies_read_a_runner_from_a_module_looked_up_by_a_literal_name(source: str) -> None:
    reader = CommandReader(ast.parse(source))
    assert reader.starts_process()
    assert {"ruby"} == reader.commands(set())
    assert reader.problems("tool.py") == []


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            'import subprocess\nif (runner := subprocess.run):\n    runner(["git", "status"])\n',
            id="an assignment expression",
        ),
        pytest.param(
            'import subprocess\nrunner, other = subprocess.run, print\nrunner(["git", "status"])\n', id="unpacking"
        ),
        pytest.param(
            'import subprocess\ndef status(runner=subprocess.run):\n    runner(["git", "status"])\n', id="a default"
        ),
        pytest.param(
            'import subprocess\nstatus = lambda runner=subprocess.run: runner(["git", "status"])\n',
            id="a lambda default",
        ),
    ],
)
def test_command_reader_follows_runners_bound_by_assignment_expressions_unpacking_and_defaults(source: str) -> None:
    reader = CommandReader(ast.parse(source))
    assert reader.starts_process()
    assert {"git"} == reader.commands(set())
    assert reader.problems("tool.py") == []


SCRIPT_RESULTS_DOC = '"Script results" in docs/adding-a-skill.md'
FAILED_ON_STDERR = f"prints FAILED on stderr; print it on stdout; see {SCRIPT_RESULTS_DOC}"
PRINTS_JSON = f"prints JSON; print one fact per line; see {SCRIPT_RESULTS_DOC}"


@pytest.mark.parametrize(
    ("source", "breach"),
    [
        pytest.param(
            "import sys\ndef main():\n    sys.stderr.write('FAILED x')\n", FAILED_ON_STDERR, id="stderr.write"
        ),
        pytest.param(
            "from sys import stderr\ndef main():\n    print('FAILED x', file=stderr)\n",
            FAILED_ON_STDERR,
            id="print to an imported stderr",
        ),
        pytest.param(
            "import sys\ndef main(reason):\n    print('FAILED ' + reason, file=sys.stderr)\n",
            FAILED_ON_STDERR,
            id="a joined line",
        ),
        pytest.param(
            "import sys\ndef main(reason):\n    print('FAILED {}'.format(reason), file=sys.stderr)\n",
            FAILED_ON_STDERR,
            id="a formatted line",
        ),
        pytest.param(
            "from sys import exit\ndef main():\n    exit(3)\n",
            f"exits 3; scripts exit only 0, 1, or 2; see {SCRIPT_RESULTS_DOC}",
            id="an imported exit",
        ),
        pytest.param(
            "import os\ndef main():\n    os._exit(3)\n",
            f"exits 3; scripts exit only 0, 1, or 2; see {SCRIPT_RESULTS_DOC}",
            id="os._exit",
        ),
        pytest.param(
            "def main():\n    exit(4)\n",
            f"exits 4; scripts exit only 0, 1, or 2; see {SCRIPT_RESULTS_DOC}",
            id="the builtin exit",
        ),
        pytest.param(
            "from json import dumps\ndef main():\n    print(dumps({}))\n", PRINTS_JSON, id="an imported dumps"
        ),
        pytest.param(
            "import json, sys\ndef main():\n    json.dump({}, sys.stdout)\n", PRINTS_JSON, id="json.dump to stdout"
        ),
        pytest.param(
            "import argparse\ndef main():\n    ap = argparse.ArgumentParser()\n    try:\n        pass\n"
            "    except OSError as e:\n        ap.error(str(e))\n",
            f"reports a failure through parser.error; print FAILED <reason> and exit 1; see {SCRIPT_RESULTS_DOC}",
            id="parser.error",
        ),
    ],
)
def test_script_contract_policy_reads_each_report_through_the_module_s_names(
    tmp_path: Path, source: str, breach: str
) -> None:
    write_fixture_tree(tmp_path, {"skills/alpha/scripts/run.py": source})
    problems = script_contract_problems(mark_skills(tmp_path))
    assert [breach] == [problem.split(" ", 1)[1] for problem in problems]


@pytest.mark.parametrize("label", ["zsh", "console", "dash", "fish", "cmd", "bat"])
def test_skill_command_policy_reads_every_command_shell_fence(tmp_path: Path, label: str) -> None:
    write_fixture_tree(
        tmp_path,
        {
            "deploy-meta/alpha.json": '{"tools": []}',
            "skills/alpha/SKILL.md": f"# alpha\n\n```{label}\nrg foo\n```\n",
        },
    )
    assert [f"skill alpha runs rg, which a standard install lacks; see {COMMANDS_DOC}"] == skill_command_problems(
        tmp_path, set(), set()
    )


# Run in a child Python for one entry point: import it under another name, so its __main__ block does not run, replace
# its main with a stub that raises each failure in turn, then execute the block as written, and print each outcome.
ENTRY_POINT_PROBE = r"""
import ast, contextlib, importlib.util, io, json, sys
from pathlib import Path

path = Path(sys.argv[1])
sys.path.insert(0, str(path.parent))
specification = importlib.util.spec_from_file_location("entry_point_under_test", path)
module = importlib.util.module_from_spec(specification)
sys.modules[specification.name] = module
specification.loader.exec_module(module)
tree = ast.parse(path.read_text(encoding="utf-8"))
line = int(sys.argv[2])
block = next(node for node in ast.walk(tree) if isinstance(node, ast.If) and node.lineno == line)
code = compile(ast.Module(body=block.body, type_ignores=[]), str(path), "exec")
outcomes = []
for failure in (LookupError("injected" + chr(10) + "failure"), KeyboardInterrupt()):
    def stub(*arguments, failure=failure, **keywords):
        raise failure
    for name in ("main", "_main"):
        if hasattr(module, name):
            setattr(module, name, stub)
    output, errors = io.StringIO(), io.StringIO()
    status = "returned"
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        try:
            exec(code, vars(module))
        except SystemExit as exited:
            status = exited.code
    outcomes.append([status, output.getvalue(), errors.getvalue()])
sys.__stdout__.write(json.dumps(outcomes))
"""


def entry_points_with_a_main_block() -> list[tuple[Path, int]]:
    """The entry points the console policy covers that run as programs, each with the line of each __main__ block the
    policy reads in it."""
    return [
        (path, block.lineno)
        for path in console_entry_points(REPOSITORY_ROOT)
        for block in main_blocks(ast.parse(path.read_text(encoding="utf-8")))
    ]


class EntryPointRunnerTests(unittest.TestCase):
    def test_every_entry_point_turns_a_failure_into_a_failed_line_and_ctrl_c_into_exit_130(self) -> None:
        # The policy proves each block starts with the runner; this runs each block as written with its main failing.
        entry_points = entry_points_with_a_main_block()
        self.assertIn(REPOSITORY_ROOT / "deploy.py", [path for path, _ in entry_points])
        for path, line in entry_points:
            name = path.relative_to(REPOSITORY_ROOT).as_posix()
            with self.subTest(entry_point=name):
                result = subprocess.run(
                    [sys.executable, "-B", "-c", ENTRY_POINT_PROBE, str(path), str(line)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    stdin=subprocess.DEVNULL,
                    cwd=REPOSITORY_ROOT,
                    timeout=120,
                    check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(
                    [[1, "FAILED LookupError: injected failure\n", ""], [130, "FAILED interrupted\n", ""]],
                    json.loads(result.stdout),
                )


STARTS_OUTSIDE = "outside its runner"
UNREAD = "cannot read the program"

# Each module that starts a checker some way the policy must see, the problem it must report, and the forms it passes.
CHECKER_ROWS: dict[str, tuple[str, str | None]] = {
    "tools/shell_string.py": (
        "import subprocess\n\ndef run(p):\n    subprocess.run(f'shellcheck {p}', shell=True)\n",
        f"starts shellcheck {STARTS_OUTSIDE}",
    ),
    "tools/split_string.py": (
        "import shlex\nimport subprocess\n\ndef run(p):\n    subprocess.run(shlex.split('shellcheck -x') + [p])\n",
        f"starts shellcheck {STARTS_OUTSIDE}",
    ),
    "tools/bash_c_fstring.py": (
        "import subprocess\n\ndef run(p):\n    subprocess.run(['bash', '-c', f'shellcheck {p}'])\n",
        f"starts shellcheck {STARTS_OUTSIDE}",
    ),
    "tools/neutral_program.py": (
        "import subprocess\n\ndef run(program, p):\n    subprocess.run([program, '--format=gcc', p])\n",
        UNREAD,
    ),
    "tools/attribute_program.py": (
        "import subprocess\n\ndef run(lint, p):\n    subprocess.run([lint.binary, p])\n",
        UNREAD,
    ),
    "tools/environment_unpassed.py": (
        "from deployer import platform_support, render\n\n"
        "def run(p):\n    environment = render.SHELLCHECK_ENVIRONMENT\n    platform_support.run_tool(['git', p])\n",
        "names SHELLCHECK_ENVIRONMENT outside the shellcheck runner",
    ),
    "tools/module_level_builder.py": (
        "from subprocess import run\n\nfrom deployer import render\n\n"
        "run(render.shellcheck_command('shellcheck', 'x.sh'))\n",
        UNREAD,
    ),
    "tools/run_tool_literal.py": (
        "from deployer import platform_support\n\ndef run(p):\n    platform_support.run_tool(['shellcheck', p])\n",
        f"starts shellcheck {STARTS_OUTSIDE}",
    ),
    "tools/located_shellcheck.py": (
        "import shutil\nimport subprocess\n\ndef run(p):\n    subprocess.run([shutil.which('shellcheck'), p])\n",
        f"starts shellcheck {STARTS_OUTSIDE}",
    ),
    "tests/validation/ruff_job.py": (
        "from job_pool import run_process\n\ndef run():\n    run_process(['ruff', 'check', '.'])\n",
        f"starts ruff {STARTS_OUTSIDE}",
    ),
    "tests/validation/ruff_module.py": (
        "import subprocess\nimport sys\n\ndef run():\n    subprocess.run([sys.executable, '-m', 'ruff', 'check'])\n",
        f"starts ruff {STARTS_OUTSIDE}",
    ),
    "tests/validation/ruff_options.py": (
        "from toolchain import RUFF_OPTIONS\n\ndef options():\n    return [*RUFF_OPTIONS]\n",
        "names RUFF_OPTIONS outside the ruff runner",
    ),
    "deployer/mypy_program.py": (
        "import subprocess\n\ndef run():\n    subprocess.run(['mypy', '.'])\n",
        f"starts mypy {STARTS_OUTSIDE}",
    ),
    "deployer/mypy_module.py": (
        "import subprocess\nimport sys\n\ndef run():\n    subprocess.run([sys.executable, '-m', 'mypy', '.'])\n",
        f"starts mypy {STARTS_OUTSIDE}",
    ),
    "tools/mypy_environment.py": (
        "import os\n\nfrom toolchain import MYPY_ENVIRONMENT\n\n"
        "def run():\n    return {**os.environ, **MYPY_ENVIRONMENT}\n",
        "names MYPY_ENVIRONMENT outside the mypy runner",
    ),
    "tools/mypy_api.py": (
        "from mypy import api\n\ndef run():\n    api.run(['.'])\n",
        "imports mypy, which would run it in this process",
    ),
    "tools/mypy_module.py": (
        "import mypy.api\n\ndef run():\n    mypy.api.run(['.'])\n",
        "imports mypy, which would run it in this process",
    ),
    "tools/mypy_importlib.py": (
        "import importlib\n\ndef run():\n    importlib.import_module('mypy.api').run(['.'])\n",
        "imports mypy, which would run it in this process",
    ),
    "tools/ruff_dunder.py": (
        "def run():\n    return __import__('ruff')\n",
        "imports ruff, which would run it in this process",
    ),
    "tools/ruff_from_main.py": (
        "from ruff.__main__ import find_ruff_bin\n\nPROGRAM = find_ruff_bin()\n",
        "imports ruff, which would run it in this process",
    ),
    "tools/bash_syntax.py": (
        "import subprocess\n\ndef run(p):\n    subprocess.run(['bash', '-n', p])\n",
        f"starts bash -n {STARTS_OUTSIDE}",
    ),
    "tools/script_analyzer.py": (
        "SCRIPT = 'Invoke-ScriptAnalyzer -Path $env:TARGET -Severity Warning'\n",
        "runs PSScriptAnalyzer outside tests/validation/shell_targets.py:SCRIPT_ANALYZER_RUN",
    ),
    "skills/alpha/scripts/lint.py": (
        "import subprocess\n\ndef run(p):\n    subprocess.run(['shellcheck', p])\n",
        f"starts shellcheck {STARTS_OUTSIDE}",
    ),
    # The forms the policy passes: each checker's own runner, a command it reads that runs no checker, a command it
    # cannot read whose module says why, the command's name alone, and a regression suite's expected command lines.
    "deployer/render.py": (
        "from . import platform_support\n\n"
        "SHELLCHECK_ENVIRONMENT = {'SHELLCHECK_OPTS': ''}\n"
        "COMMANDS_ALLOWED = {\n"
        "    'shellcheck_command': 'builds the line for the runner',\n"
        "    'run_shellcheck': 'starts the ShellCheck its caller located',\n"
        "    'run_bash_syntax_check': 'starts the Bash its caller located',\n"
        "}\n\n"
        "def shellcheck_command(shellcheck, *arguments):\n    return [shellcheck, '--norc', *arguments]\n\n"
        "def run_shellcheck(shellcheck, *arguments):\n"
        "    return platform_support.run_tool(shellcheck_command(shellcheck, *arguments), SHELLCHECK_ENVIRONMENT)\n\n"
        "def run_bash_syntax_check(bash, path):\n    return platform_support.run_tool([bash, '-n', path])\n",
        None,
    ),
    "tests/validation/toolchain.py": (
        "from job_pool import run_process\n\nfrom deployer import render\n\n"
        "RUFF_OPTIONS = ('--no-respect-gitignore',)\nMYPY_ENVIRONMENT = {'MYPYPATH': ''}\n"
        "COMMANDS_ALLOWED = {\n"
        "    'shellcheck_job': 'starts the ShellCheck its caller located',\n"
        "    'bash_syntax_job': 'starts the Bash its caller located',\n"
        "    'ruff_job': 'starts the ruff its caller located',\n"
        "    'mypy_job': 'starts the mypy its caller located',\n"
        "}\n\n"
        "def shellcheck_job(shellcheck, *arguments):\n"
        "    run_process(render.shellcheck_command(shellcheck, *arguments), render.SHELLCHECK_ENVIRONMENT)\n\n"
        "def bash_syntax_job(bash, path):\n    run_process([bash, '-n', path])\n\n"
        "def ruff_job(ruff, subcommand, targets, cwd):\n"
        "    run_process([ruff, *subcommand, *RUFF_OPTIONS, *targets], cwd=cwd)\n\n"
        "def find_ruff():\n    from ruff.__main__ import find_ruff_bin\n\n    return find_ruff_bin()\n\n"
        "def mypy_job(mypy, configuration, arguments, cwd):\n"
        "    run_process([mypy, '--config-file', str(configuration), *arguments], MYPY_ENVIRONMENT, cwd=cwd)\n",
        None,
    ),
    "tests/validation/shell_targets.py": (
        "SCRIPT_ANALYZER_RUN = (\n    'foreach ($t in $targets) { '\n"
        "    'Invoke-ScriptAnalyzer -Path $t -Settings @{} -Severity Warning, Error }'\n)\n",
        None,
    ),
    "tools/readable.py": (
        "import subprocess\n\nCOMMANDS_ALLOWED = {'run_located': 'starts the program its caller located'}\n\n"
        "def run(p):\n    subprocess.run(['git', 'status', p])\n\n"
        "def run_located(program):\n    subprocess.run([program, '--version'])\n\n"
        "NAME = 'Invoke-ScriptAnalyzer'\n",
        None,
    ),
    "tests/validation/test_expected.py": (
        "import subprocess\n\ndef test_it():\n    subprocess.run(['shellcheck', 'x.sh'])\n",
        None,
    ),
}


class CheckerCommandFixtures(unittest.TestCase):
    def problems(self, files: dict[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_fixture_tree(root, files)
            return skill_scripts.checker_command_problems(root, [root / name for name in files])

    def test_each_way_of_starting_a_checker_outside_its_runner_is_refused_and_each_runner_passes(self) -> None:
        for name, (source, expected) in CHECKER_ROWS.items():
            with self.subTest(module=name):
                problems = self.problems({name: source})
                if expected is None:
                    self.assertEqual([], problems)
                else:
                    self.assertTrue(problems, "the policy reported nothing")
                    self.assertTrue(all(problem.startswith(f"{name}:") for problem in problems), problems)
                    self.assertIn(expected, " ".join(problems))

    def test_a_script_analyzer_run_without_its_settings_is_refused(self) -> None:
        runner = "SCRIPT_ANALYZER_RUN = 'Invoke-ScriptAnalyzer -Path $t -Severity Warning, Error'\n"
        self.assertEqual(
            [
                "tests/validation/shell_targets.py: SCRIPT_ANALYZER_RUN runs PSScriptAnalyzer without -Settings @{}, "
                "so a settings file beside a target or in the working folder is read"
            ],
            self.problems({"tests/validation/shell_targets.py": runner}),
        )

    def test_a_runner_reached_through_an_alias_the_reader_cannot_resolve_is_refused(self) -> None:
        source = "import subprocess\n\nrunner = subprocess.run if True else None\n\ndef go():\n    runner(['git'])\n"
        problems = self.problems({"tools/alias.py": source})
        self.assertTrue(problems)
        self.assertTrue(all(problem.startswith("tools/alias.py:") for problem in problems), problems)


# The declared-tool policy reads the program Bash's time keyword times past the keyword's options, so a tool a skill
# runs under `time -p` is held to its declaration as one it runs directly is.
@pytest.mark.parametrize("script", ["time -p jq .", "time -p -- jq .", "! time -p jq ."])
def test_the_command_policy_holds_the_program_the_time_keyword_times_to_its_declaration(script: str) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(
            root,
            {
                "deploy-meta/alpha.json": "{}",
                "skills/alpha/SKILL.md": "# alpha\n",
                "skills/alpha/scripts/tool.sh": f"{script}\n",
            },
        )
        doc = '"Commands skills may run" in docs/adding-a-skill.md'
        assert skill_command_problems(root, {"gh"}, {"git"}) == [
            f"skill alpha runs jq, which a standard install lacks; see {doc}"
        ]
