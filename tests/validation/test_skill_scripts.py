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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

import skill_scripts
from git_test_base import GitTestCase
from skill_scripts import (
    CLIENT_CLASSES,
    COMMAND_RUNNERS,
    UNREAD_PROGRAM,
    CommandReader,
    client_command_problems,
    console_entry_points,
    console_setup_problems,
    gh_filter_problems,
    main_blocks,
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


def unreadable(path: str, line: int, description: str) -> str:
    """The finding the command policies report for a command whose program they cannot read."""
    return (
        f"{path}:{line} cannot read {description}; name the program with a literal or a lookup, or state why in "
        f"COMMANDS_ALLOWED; see {COMMANDS_DOC}"
    )


class SkillScriptsFixtures(GitTestCase):
    def test_process_detection_uses_every_command_runner_and_client_with_dynamic_arguments(self) -> None:
        for callable_name in (*COMMAND_RUNNERS, *CLIENT_CLASSES):
            with self.subTest(callable_name=callable_name):
                module, function = callable_name.split(".")
                # The command goes where the runner takes it: os's spawn calls take a mode first.
                position = COMMAND_RUNNERS[callable_name][0] if callable_name in COMMAND_RUNNERS else 0
                arguments = ", ".join([*(["mode"] * position), "command"])
                tree = ast.parse(f"import {module} as process\nstart = process.{function}\nstart({arguments})\n")
                reader = CommandReader(tree)
                self.assertTrue(reader.starts_process())
                # A runner given a command the module never binds reports it; a client's argument is not a command.
                expected = (
                    [unreadable("start.py", 3, "the program in `command`, which the module binds to nothing it reads")]
                    if callable_name in COMMAND_RUNNERS
                    else []
                )
                self.assertEqual(expected, reader.problems("start.py"))
                self.assertFalse(CommandReader(ast.parse(f"from {module} import {function}\n")).starts_process())

    def test_command_reader_keeps_only_the_f_strings_initial_literal_arguments(self) -> None:
        for expression, prefix in (
            ('f"gh api {endpoint} --template {template}"', "gh api "),
            ('f"gh " "api " f"{endpoint} copilot"', "gh api "),
            ('f"gh.exe api {endpoint}"', "gh.exe api "),
            ('f"gh"', "gh"),
        ):
            with self.subTest(expression=expression):
                tree = ast.parse(expression)
                statement = tree.body[0]
                if not isinstance(statement, ast.Expr):
                    self.fail("Expected an f-string expression")
                literals = CommandReader(tree).program_literals(statement.value)
                constants = [node for node in literals if isinstance(node, ast.Constant)]
                self.assertEqual(literals, constants)
                self.assertEqual([prefix], [node.value for node in constants])
                self.assertEqual([1], [node.lineno for node in constants])

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
            git = "run it through skill-core's git_client.py's GitClient, which bounds it and turns prompts off"
            gh = "run it through skill-core's github_client.py's GitHubClient, which bounds it and turns prompts off"
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

    def test_command_policies_report_f_strings_without_a_readable_literal_prefix(self) -> None:
        for expression in (
            'f"{program} api gh"',
            'f"   {program} api"',
            'f"{program=}"',
            '[f"{program}", "api"]',
            '(f"{program}", "api")',
            'f"{program}" + " gh api"',
        ):
            with self.subTest(expression=expression), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                write_fixture_tree(
                    root,
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
                expected = [
                    unreadable("skills/alpha/scripts/tool.py", 3, "f-string command without a literal program prefix")
                ]
                self.assertEqual(expected, client_command_problems(root))
                self.assertEqual(expected, skill_command_problems(root, {"gh"}, {"git", "python"}))

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
            git = "run it through skill-core's git_client.py's GitClient, which bounds it and turns prompts off"
            gh = "run it through skill-core's github_client.py's GitHubClient, which bounds it and turns prompts off"
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
            git = "run it through skill-core's git_client.py's GitClient, which bounds it and turns prompts off"
            gh = "run it through skill-core's github_client.py's GitHubClient, which bounds it and turns prompts off"
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
            unreadable(path, 9, "the program `getattr(subprocess, 'run')` runs, which getattr takes"),
            unreadable(path, 11, "the program `getattr(os, name)` runs, which getattr takes"),
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
        self.assertFalse(
            CommandReader(ast.parse("import subprocess\ndef f(p: subprocess.Popen[str]): ...\n")).starts_process()
        )

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
            git = "run it through skill-core's git_client.py's GitClient, which bounds it and turns prompts off"
            gh = "run it through skill-core's github_client.py's GitHubClient, which bounds it and turns prompts off"
            self.assertEqual(
                [
                    f"skills/alpha/scripts/tool.py:6 runs git itself; {git}; see {doc}",
                    f"skills/alpha/scripts/tool.py:9 runs gh itself; {gh}; see {doc}",
                    f"skills/alpha/scripts/tool.py:13 runs git itself; {git}; see {doc}",
                ],
                client_command_problems(root),
            )

    def test_command_reader_follows_runners_bound_by_assignment_expressions_unpacking_and_defaults(self) -> None:
        for source in (
            'import subprocess\nif (runner := subprocess.run):\n    runner(["git", "status"])\n',
            'import subprocess\nrunner, other = subprocess.run, print\nrunner(["git", "status"])\n',
            'import subprocess\ndef status(runner=subprocess.run):\n    runner(["git", "status"])\n',
            'import subprocess\nstatus = lambda runner=subprocess.run: runner(["git", "status"])\n',
        ):
            with self.subTest(source=source):
                reader = CommandReader(ast.parse(source))
                self.assertTrue(reader.starts_process())
                self.assertEqual({"git"}, reader.commands(set()))
                self.assertEqual([], reader.problems("tool.py"))
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
            (scripts / "tool.sh").write_text(
                'set -e\n[ -n "$1" ] || exit 3\nexit 0  # exit 6 in a comment\n', encoding="utf-8"
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
                    "skills/alpha/scripts/vague.py: EXIT_CONTRACT_EXEMPT must be a non-empty string saying why",
                ],
                script_contract_problems(mark_skills(root)),
            )

    def test_script_contract_policy_reads_each_report_through_the_module_s_names(self) -> None:
        doc = '"Script results" in docs/adding-a-skill.md'
        stderr = f"prints FAILED on stderr; print it on stdout; see {doc}"
        json_output = f"prints JSON; print one fact per line; see {doc}"
        parser = f"reports a failure through parser.error; print FAILED <reason> and exit 1; see {doc}"
        for source, breach in (
            ("import sys\ndef main():\n    sys.stderr.write('FAILED x')\n", stderr),
            ("from sys import stderr\ndef main():\n    print('FAILED x', file=stderr)\n", stderr),
            ("import sys\ndef main(reason):\n    print('FAILED ' + reason, file=sys.stderr)\n", stderr),
            ("import sys\ndef main(reason):\n    print('FAILED {}'.format(reason), file=sys.stderr)\n", stderr),
            ("from sys import exit\ndef main():\n    exit(3)\n", f"exits 3; scripts exit only 0, 1, or 2; see {doc}"),
            ("import os\ndef main():\n    os._exit(3)\n", f"exits 3; scripts exit only 0, 1, or 2; see {doc}"),
            ("def main():\n    exit(4)\n", f"exits 4; scripts exit only 0, 1, or 2; see {doc}"),
            ("from json import dumps\ndef main():\n    print(dumps({}))\n", json_output),
            ("import json, sys\ndef main():\n    json.dump({}, sys.stdout)\n", json_output),
            (
                "import argparse\ndef main():\n    ap = argparse.ArgumentParser()\n    try:\n        pass\n"
                "    except OSError as e:\n        ap.error(str(e))\n",
                parser,
            ),
        ):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                write_fixture_tree(root, {"skills/alpha/scripts/run.py": source})
                problems = script_contract_problems(mark_skills(root))
                self.assertEqual([breach], [problem.split(" ", 1)[1] for problem in problems])
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

    def test_shell_command_extraction_reads_the_program_every_wrapper_and_substitution_runs(self) -> None:
        for script in (
            'echo "$(rg foo)"',
            "y=`rg foo`",
            "xargs rg foo",
            "xargs -I {} rg {}",
            "find . -exec rg foo {} \\;",
            "env A=1 rg foo",
            "command rg foo",
            "exec rg foo",
            "nohup rg foo",
            "timeout -s KILL 5 rg foo",
            'eval "rg foo"',
            'bash -c "rg foo"',
        ):
            with self.subTest(script=script):
                self.assertIn("rg", shell_commands(script))
        # A lookup, a substitution in single quotes or a comment, and arithmetic run nothing.
        for script in ("command -v rg", "echo '$(rg foo)'", "# run `rg foo` here", "echo $((1 + 2))"):
            with self.subTest(script=script):
                self.assertNotIn("rg", shell_commands(script))
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
                    "skills/alpha/scripts/run.py:2 runs git itself; run it through skill-core's git_client.py's "
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
            core = root / "skills" / "skill-core" / "scripts"
            core.mkdir(parents=True)
            (core / "git_client.py").write_text('command = ["git", *arguments]\n', encoding="utf-8")
            doc = '"Script results" in docs/adding-a-skill.md'
            git = "run it through skill-core's git_client.py's GitClient, which bounds it and turns prompts off"
            gh = "run it through skill-core's github_client.py's GitHubClient, which bounds it and turns prompts off"
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
            git = "run it through skill-core's git_client.py's GitClient, which bounds it and turns prompts off"
            gh = "run it through skill-core's github_client.py's GitHubClient, which bounds it and turns prompts off"
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
                # skill-core's bounded runner, given a command held in a name or built from one.
                "skills/alpha/scripts/bounded.py": 'from bounded_process import run_bounded\n\nCOMMAND = ["jq", "."]\n'
                "run_bounded(COMMAND, 5)\n",
                "skills/alpha/scripts/built.py": 'import bounded_process\n\nBASE = ["dotnet-format"]\n'
                'bounded_process.run_bounded([*BASE, "--version"], timeout=5)\n',
                # skill-core's clients run their command, and a program looked up is one the script runs.
                "skills/alpha/scripts/client.py": "from github_client import GitHubClient\n\n"
                'GitHubClient().run(["api"])\n',
                "skills/alpha/scripts/git.py": 'import git_client\n\ngit_client.GitClient().run(["status"])\n',
                "skills/alpha/scripts/lookup.py": 'import shutil as sh\n\nPROGRAM = sh.which("copilot")\n',
                # skill-core's locator looks a program up as shutil.which does, and so do its lists of every match.
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
        # code-review-core depends on skill-core, so a skill that reaches code-review-core reaches skill-core too.
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
                # A function of the same name that is not skill-core's is a second runner.
                "skills/alpha/scripts/local.py": f"def run_main(main):\n    main()\n\n\n{main}    run_main(main)\n",
                "skills/alpha/scripts/copied.py": 'def main():\n    sys.stdout.reconfigure(encoding="utf-8")\n\n\n'
                f"{imported}{main}    run_main(main)\n",
                "skills/alpha/scripts/library.py": "def helper():\n    return 1\n",
                # Suites are not entry points.
                "skills/alpha/scripts/test_good.py": f"{main}    unittest.main()\n",
                "skills/skill-core/scripts/console.py": 'sys.stdout.reconfigure(encoding="utf-8")\n',
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
            late = f"does not call skill-core's run_main() first in its __main__ block; see {doc}"
            copied = f"reconfigures a stream itself; call run_main() from skill-core instead; see {doc}"
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
            late = f"does not call skill-core's run_main() first in its __main__ block; see {doc}"
            self.assertEqual(
                [
                    f"skills/demo/scripts/control.py:5 {late}",
                    f"skills/demo/scripts/in_try.py:6 {late}",
                    f"skills/demo/scripts/reversed.py:5 {late}",
                    'skills/demo/scripts/unguarded.py:8 runs main outside an `if __name__ == "__main__":` block, so '
                    f"importing it runs it; run skill-core's run_main(main) first in that block instead; see {doc}",
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
            copied = f"reconfigures a stream itself; call run_main() from skill-core instead; see {doc}"
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


class EntryPointRunnerTests(GitTestCase):
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


if __name__ == "__main__":
    unittest.main()
