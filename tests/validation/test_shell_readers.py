"""Fixture tests for the shell readers in tests/validation/skill_scripts.py: the programs a Bash script runs, through
its substitutions, wrappers, eval, and a shell's -c, and the script a pwsh or Windows PowerShell start-up runs, read
from Bash, a Python runner's list, and a PowerShell source. They stand apart from test_skill_scripts.py, which holds
the policies that read these, so that suite fits its shards.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import skill_scripts
import toolchain
from skill_scripts import UNREAD_PROGRAM, CommandReader, shell_commands


@pytest.mark.parametrize(
    "script",
    [
        'echo "$(rg foo)"',
        "y=`rg foo`",
        # A substitution's end is its own ), not one in a string, an escape, or a nested substitution.
        "x=\"$(printf ')' | rg foo)\"",
        'x="$(printf "(" ; rg foo)"',
        "x=$(printf \\( ; rg foo)",
        'x="$(echo "$(true)" ; rg foo)"',
        "x=$(echo `true` ; rg foo)",
        "xargs rg foo",
        "xargs -I {} rg {}",
        "find . -exec rg foo {} \\;",
        "find . -exec rg foo {} + -exec jq . {} +",
        "env A=1 rg foo",
        "command rg foo",
        "exec rg foo",
        "nohup rg foo",
        "timeout -s KILL 5 rg foo",
        'eval "rg foo"',
        'bash -c "rg foo"',
        # A shell's -c in a bundle, before or after its other options, as the shell reads it.
        "bash -lc 'rg foo'",
        "bash -e -c 'rg foo'",
        "bash -c -e 'rg foo'",
        "bash -o pipefail -ec 'rg foo'",
        "sh --norc -c -- 'rg foo'",
        # A wrapper option in every spelling getopt reads it.
        "timeout --signal TERM 5 rg foo",
        "timeout --signal=TERM -k1 5 rg foo",
        "xargs -0n 1 rg foo",
        "xargs --max-procs 2 --max-chars=9 rg foo",
        "env --chdir d -u X rg foo",
        "env -vS'rg foo'",
        "env --split-string='-i rg' foo",
        "nice -5 rg foo",
    ],
)
def test_shell_command_extraction_reads_the_program_every_wrapper_and_substitution_runs(script: str) -> None:
    assert "rg" in shell_commands(script)


# find runs every action's program whichever terminator, quoted, escaped, or +, ends the action before it.
@pytest.mark.parametrize(
    "script",
    [
        "find . -exec jq . {} + -exec rg foo {} +",
        "find . -exec jq . {} \\; -exec rg foo {} \\;",
        "find . -exec jq . {} ';' -exec rg foo {} ';'",
        'find . -exec jq . {} ";" -exec rg foo {} ";"',
    ],
)
def test_shell_command_extraction_reads_each_find_action_past_its_terminator(script: str) -> None:
    assert shell_commands(script) == {"find", "jq", "rg"}


# A command separator quoted or escaped is a word of the command, so the word after it is not a program.
@pytest.mark.parametrize(
    "separator",
    [
        *(f"\\{op}" for op in (";", "&", "|", "(", "{")),
        "\\&\\&",
        "\\|\\|",
        "\\|\\&",
        *(f"{quote}{op}{quote}" for quote in "'\"" for op in (";", "&", "&&", "|", "||", "|&", "(", "\n", "{")),
        "a';'b",
    ],
)
def test_shell_command_extraction_reads_a_quoted_or_escaped_separator_as_a_word(separator: str) -> None:
    assert shell_commands(f"echo a {separator} rg foo") == {"echo"}


# A quoted or escaped reserved word is a plain word: at a command's start it is the program Bash runs, and it opens,
# closes, or ends no case, loop, or function.
@pytest.mark.parametrize(
    ("script", "programs"),
    [
        ("'if' rg foo", {"if"}),
        ('"do" rg foo', {"do"}),
        ("\\fi rg foo", {"fi"}),
        ("\\! rg foo", set()),
        ("'for' x in rg", {"for"}),
        ("'case' x", {"case"}),
        ("'function' rg", {"function"}),
        ("f '()'; f", {"f"}),
        ("case x in 'esac') rg foo;; esac", {"rg"}),
        ("case x in \\)|a) rg foo;; esac", {"rg"}),
    ],
)
def test_shell_command_extraction_reads_a_quoted_or_escaped_reserved_word_as_a_word(
    script: str, programs: set[str]
) -> None:
    assert shell_commands(script) == programs


# The unquoted reserved words and functions, read as Bash reads them: a function runs no program of its name, a case
# word runs nothing until its `in`, each case item and the item that ends at `esac` runs its commands.
@pytest.mark.parametrize(
    ("script", "programs"),
    [
        ("echo a; function f { jq .; }; f", {"echo", "jq"}),
        ("echo a; f () { jq .; }; f", {"echo", "jq"}),
        ("case $(echo a) in a) jq .;; esac", {"echo", "jq"}),
        ("case x in a) jq .;; b) rg foo;; esac", {"jq", "rg"}),
        ("case x in a) jq .\nesac", {"jq"}),
    ],
)
def test_shell_command_extraction_reads_unquoted_reserved_words_and_functions(script: str, programs: set[str]) -> None:
    assert shell_commands(script) == programs


def test_the_shell_lexer_gives_each_word_beside_its_syntax() -> None:
    words = list(skill_scripts.ShellLexer("echo aX'|'b \\; c && d").words())
    assert words == [("echo", "echo"), ("aX|b", None), (";", None), ("c", "c"), ("&&", "&&"), ("d", "d")]


# A line continuation joins its lines, so the word after it is the git command's subcommand.
def test_the_git_subcommand_reader_reads_past_a_line_continuation() -> None:
    assert skill_scripts.shell_git_subcommands("git \\\n  push") == {"push"}


# An unquoted separator still ends the command, so the word after it is the next command's program.
@pytest.mark.parametrize("separator", [";", "&", "&&", "|", "||", "|&", "\n"])
def test_shell_command_extraction_reads_the_program_after_an_unquoted_separator(separator: str) -> None:
    assert shell_commands(f"echo a {separator} rg foo") == {"echo", "rg"}


# A lookup, a substitution in single quotes or a comment, and arithmetic run nothing.
@pytest.mark.parametrize("script", ["command -v rg", "echo '$(rg foo)'", "# run `rg foo` here", "echo $((1 + 2))"])
def test_shell_command_extraction_finds_no_program_where_nothing_runs(script: str) -> None:
    assert "rg" not in shell_commands(script)


@pytest.mark.parametrize(
    ("script", "spans"),
    [
        pytest.param("a $(b) c", [(4, "b")], id="unquoted"),
        pytest.param("a \"$(b ')' c)\" d", [(5, "b ')' c")], id="a quoted ) inside"),
        pytest.param("a $(b $(c) d) e", [(4, "b $(c) d")], id="nested, outermost only"),
        pytest.param("a `b \\` c` d", [(3, "b \\` c")], id="an escaped backtick inside"),
        pytest.param("x=$'\\''; $(b)", [(11, "b")], id="after an ANSI-C string"),
        pytest.param("a '$(b)' # $(c)\n$(d)", [(18, "d")], id="single quotes and a comment"),
        pytest.param("a $((1 + 2))", [], id="arithmetic"),
        pytest.param("a $(b ')", [(4, "b ')")], id="left open"),
    ],
)
def test_substitution_spans_name_each_substitution_where_its_text_begins(
    script: str, spans: list[tuple[int, str]]
) -> None:
    assert skill_scripts.substitution_spans(script) == spans


@pytest.mark.parametrize(
    ("words", "program"),
    [
        pytest.param(["env", "-i", "A=1", "-u", "B", "head", "-n", "1"], ["head", "-n", "1"], id="env"),
        pytest.param(["timeout", "-k", "1", "5", "head"], ["head"], id="timeout"),
        pytest.param(["exec", "-a", "name", "head"], ["head"], id="exec"),
        pytest.param(["command", "-p", "head"], ["head"], id="command"),
        pytest.param(["command", "-v", "head"], [], id="a lookup"),
        pytest.param(["env"], [], id="no program"),
        pytest.param(["head", "-n", "1"], [], id="no wrapper"),
        pytest.param(["timeout", "--signal", "TERM", "5", "gh", "pr"], ["gh", "pr"], id="a long option in full"),
        pytest.param(["xargs", "-0n", "1", "gh", "pr"], ["gh", "pr"], id="a bundle ending in a valued letter"),
        pytest.param(["xargs", "--max-procs", "2", "gh"], ["gh"], id="every valued long option"),
        pytest.param(["env", "-S", "gh api", "x"], ["gh", "api", "x"], id="env's split line"),
        pytest.param(["env", "-iS", "A=1 gh", "x"], ["gh", "x"], id="env's split line in a bundle"),
        pytest.param(["env", "--split-string=-u B gh api"], ["gh", "api"], id="env's split line read again"),
        pytest.param(
            ["env", "-S", "gh 'api", "x"], ["gh 'api", "x"], id="a split line shlex cannot split stays one word"
        ),
    ],
)
def test_unwrapped_reads_past_a_wrapper_to_the_command_it_runs(words: list[str], program: list[str]) -> None:
    assert skill_scripts.unwrapped(words) == program


# An option the wrapper's table does not settle, an abbreviation or one it does not list, never gives the program.
@pytest.mark.parametrize(
    ("words", "unread"),
    [
        (["timeout", "--sig", "TERM", "5", "gh", "pr", "list"], "timeout --sig"),
        (["xargs", "--max-a", "1", "gh", "pr", "view"], "xargs --max-a"),
        (["env", "--unse", "X", "gh", "api"], "env --unse"),
        (["env", "-W", "gh"], "env -W"),
        (["nice", "--adj=5", "gh"], "nice --adj=5"),
    ],
)
def test_unwrapped_leaves_the_program_unread_behind_an_option_it_cannot_place(words: list[str], unread: str) -> None:
    assert skill_scripts.unwrapped(words) == [f"{UNREAD_PROGRAM}{unread}"]
    assert f"{UNREAD_PROGRAM}{unread}" in shell_commands(" ".join(words))
    assert "gh" not in shell_commands(" ".join(words))


# A shell started without -c runs a file or its standard input, and -o's value is no option.
@pytest.mark.parametrize("words", [["-o", "c", "script.sh"], ["script.sh", "-c", "x"], ["-e"], ["--rcfile", "-c"]])
def test_shell_script_index_finds_no_script_where_the_shell_runs_none(words: list[str]) -> None:
    assert skill_scripts.shell_script_index(words) is None


def test_the_command_policy_reads_a_python_list_s_shell_script_among_its_options() -> None:
    for command in ('["bash", "-e", "-c", "jq ."]', '("sh", "-lc", "jq .")', '["bash", "-c", "-e", "jq .", "x"]'):
        source = f"import subprocess\nsubprocess.run({command})\n"
        assert "jq" in CommandReader(ast.parse(source)).commands(set()), command


# A start-up of pwsh or Windows PowerShell runs the script its -Command, -CommandWithArgs, or Windows PowerShell's
# first positional word holds; the policy reads that script's commands as PowerShell's parser reads them.
@pytest.mark.parametrize(
    "script",
    [
        "pwsh -Command 'jq .'",
        "pwsh -NoProfile -c 'jq .'",
        "powershell 'jq .'",
        "powershell.exe -ExecutionPolicy Bypass jq .",
        "pwsh -cwa 'jq $args' .",
        "env pwsh -c 'jq .'",
        "pwsh -c 'Invoke-Expression \"jq .\"'",
    ],
)
def test_the_command_policy_reads_the_script_a_powershell_start_up_runs(script: str) -> None:
    assert "jq" in shell_commands(script)


@pytest.mark.parametrize(
    ("script", "unread"),
    [
        ("pwsh -EncodedCommand agBxACAALgA=", "pwsh -EncodedCommand agBxACAALgA="),
        ("pwsh -NoProfile -Command -", "pwsh -NoProfile -Command -"),
        ("powershell -NoProfile -Command", "powershell -NoProfile -Command"),
    ],
)
def test_the_command_policy_refuses_a_powershell_start_up_whose_script_it_cannot_read(script: str, unread: str) -> None:
    assert shell_commands(script) == {script.split()[0], f"{UNREAD_PROGRAM}{unread}"}


# pwsh runs its first positional word as -File's, so a -Command after it is the script's argument.
@pytest.mark.parametrize(
    "script",
    ["pwsh -File run.ps1 -Command 'jq .'", "pwsh run.ps1 'jq .'", "pwsh run.ps1 -c 'jq .'", "pwsh -NoProfile"],
)
def test_a_powershell_start_up_that_runs_a_file_or_nothing_runs_only_powershell(script: str) -> None:
    assert shell_commands(script) == {"pwsh"}


def test_a_command_s_script_is_every_word_after_it_and_one_with_arguments_is_its_first() -> None:
    assert "rg" in shell_commands("pwsh -c 'Get-Date;' rg")
    assert shell_commands("pwsh -cwa 'Get-Date;' rg") == {"pwsh"}


def test_the_command_policy_reads_the_script_a_python_list_starts_powershell_to_run() -> None:
    for command in (
        '["pwsh", "-NoProfile", "-Command", "jq ."]',
        '("powershell.exe", "jq .")',
        '["C:/Program Files/PowerShell/7/pwsh.exe", "-c", "jq", "."]',
    ):
        source = f"import subprocess\nsubprocess.run({command})\n"
        assert "jq" in CommandReader(ast.parse(source)).commands(set()), command
    # A word it cannot read where the start-up's parameters or script stand leaves the script unread.
    for command, unread in (
        ('["pwsh", flag, "jq ."]', "pwsh <value> jq ."),
        ('["pwsh", "-c", script]', "pwsh -c <value>"),
        ('["pwsh", "-c", 5]', "pwsh -c <value>"),
    ):
        source = f"import subprocess\nsubprocess.run({command})\n"
        assert f"{UNREAD_PROGRAM}{unread}" in CommandReader(ast.parse(source)).commands(set()), command
    source = 'import subprocess\nsubprocess.run(["pwsh", "-File", path, "-c", "jq ."])\n'
    assert "jq" not in CommandReader(ast.parse(source)).commands(set())


def test_the_command_policy_reads_the_script_a_powershell_source_starts_powershell_to_run() -> None:
    sources = [
        "pwsh -NoProfile -Command 'jq .'",
        "powershell.exe jq .",
        "pwsh -c $script",
        "pwsh -File ./run.ps1",
        # Its twin: a Bash shell a PowerShell source starts with -c.
        "bash -e -c 'jq .'",
        "bash -c $script",
        "bash ./run.sh $argument",
        # Options alone run no script; one the source does not spell among them may be the script.
        "bash -e",
        "bash +e $script",
    ]
    assert skill_scripts.powershell_commands(sources) == [
        {"pwsh", "jq"},
        {"powershell", "jq"},
        {"pwsh", f"{UNREAD_PROGRAM}pwsh -c <value>"},
        {"pwsh"},
        {"bash", "jq"},
        {"bash", f"{UNREAD_PROGRAM}bash -c <value>"},
        {"bash"},
        {"bash"},
        {"bash", f"{UNREAD_PROGRAM}bash +e <value>"},
    ]


# A git command inside a script a command starts is read for its subcommand too: eval's, a shell's -c, the script a
# pwsh or Windows PowerShell start-up runs, and what that script starts in turn.
@pytest.mark.parametrize(
    "script",
    [
        "bash -c 'git push'",
        "sh -ec 'git -C dir push'",
        "eval 'git push'",
        "eval git push",
        "env bash -c 'git push'",
        "find . -exec bash -c 'git push' {} ';'",
        "x=$(bash -c 'git push')",
        "pwsh -c 'git push'",
        "powershell -NoProfile -Command 'git -C $directory push'",
        "pwsh -c 'bash -c \"git push\"'",
        "pwsh -c 'Start-Process git -ArgumentList push'",
        "pwsh -c \"pwsh -c 'git push'\"",
    ],
)
def test_the_git_subcommand_reader_reads_the_git_command_a_started_script_runs(script: str) -> None:
    assert skill_scripts.shell_git_subcommands(script) == {"push"}


# A git command a wrapper of COMMAND_WRAPPERS or find's -exec family runs directly is read past the wrapper's options,
# and a wrapper of a wrapper, to the git command's own words; find's action ends at its `;` or `{} +`. A bare `time` is
# Bash's keyword, not the time program, so its row reaches the program through env.
@pytest.mark.parametrize(
    "script",
    [
        "xargs git push",
        "env git push",
        "timeout 5 git push",
        "find . -exec git push {} +",
        "find . -exec git push \\;",
        "command git push",
        "builtin git push",
        "exec git push",
        "nohup git push",
        "env time -p git push",
        "nice -n 5 git push",
        "env -i A=1 git push",
        "env -S 'git push'",
        "stdbuf -o L git push",
        "xargs -n 1 git push",
        "timeout --signal TERM 5 git push",
        "find . -execdir git push ';'",
        "find . -ok git push {} \\;",
        "find . -okdir git push {} +",
        "nohup env timeout 5 git push",
        "find . -exec env git push {} +",
        "x=$(xargs git push)",
        "bash -c 'xargs git push'",
    ],
)
def test_the_git_subcommand_reader_reads_the_git_command_a_wrapper_runs(script: str) -> None:
    assert skill_scripts.shell_git_subcommands(script) == {"push"}


# A wrapper that runs no git command, or only looks git up, gives no subcommand; find's second action is read apart
# from its first.
@pytest.mark.parametrize(
    ("script", "subcommands"),
    [
        ("command -v git push", set()),
        ("xargs echo git push", set()),
        ("find . -exec git status {} \\; -exec echo push \\;", {"status"}),
        ("find . -exec git \\; -exec echo push \\;", set()),
        ("find . -exec git -C {} + -exec echo push {} +", set()),
        ("find . -exec echo {} + -exec git push {} +", {"push"}),
    ],
)
def test_the_git_subcommand_reader_reads_only_the_command_a_wrapper_runs(script: str, subcommands: set[str]) -> None:
    assert skill_scripts.shell_git_subcommands(script) == subcommands


# A started script that runs no git command gives no subcommand, though a word of it is one.
@pytest.mark.parametrize(
    "script",
    [
        "bash -c 'echo push'",
        "bash ./push.sh",
        "eval 'echo git push'",
        "pwsh -c 'Write-Output push'",
        "pwsh -File push.ps1 git push",
    ],
)
def test_the_git_subcommand_reader_finds_none_where_a_started_script_runs_no_git(script: str) -> None:
    assert skill_scripts.shell_git_subcommands(script) == set()


# A quoted or escaped ; is a word of the command before it, which runs no git; find's second action, past one, does.
@pytest.mark.parametrize(
    ("script", "subcommands"),
    [
        ("echo a \\; git push", set()),
        ("echo a ';' git push", set()),
        ("find . -exec true {} \\; -exec bash -c 'git push' \\;", {"push"}),
        ("find . -exec true {} ';' -exec bash -c 'git push' ';'", {"push"}),
    ],
)
def test_the_git_subcommand_reader_reads_past_a_quoted_or_escaped_semicolon(script: str, subcommands: set[str]) -> None:
    assert skill_scripts.shell_git_subcommands(script) == subcommands


# A subcommand no literal names is recorded as its word: the Bash text, or UNREAD_WORD for PowerShell's.
@pytest.mark.parametrize(
    ("script", "subcommand"),
    [
        ("bash -c 'git \"$sub\"'", "$sub"),
        ("pwsh -c 'git $sub'", skill_scripts.UNREAD_WORD),
        ("pwsh -c 'Start-Process git -ArgumentList $arguments'", skill_scripts.UNREAD_WORD),
    ],
)
def test_the_git_subcommand_reader_records_a_subcommand_no_literal_names_as_its_word(
    script: str, subcommand: str
) -> None:
    assert skill_scripts.shell_git_subcommands(script) == {subcommand}


# Start-Process starts pwsh or Windows PowerShell with the script its -ArgumentList holds, in any spelling of the
# parameters PowerShell binds, as a list, an array, or one string; and its twin, a Bash shell started with -c.
@pytest.mark.parametrize(
    ("source", "programs"),
    [
        ("Start-Process pwsh -ArgumentList '-Command','jq .'", {"pwsh", "jq"}),
        ("Start-Process pwsh -ArgumentList '-NoProfile -Command jq .'", {"pwsh", "jq"}),
        ("Start-Process -FilePath powershell -ArgumentList @('-NoProfile', '-Command', 'jq .')", {"powershell", "jq"}),
        ("Start-Process pwsh '-c', 'jq .'", {"pwsh", "jq"}),
        ("Start-Process -FilePath pwsh '-c', 'jq .'", {"pwsh", "jq"}),
        ("Start-Process -Fi pwsh -Args ('-c jq .')", {"pwsh", "jq"}),
        ("Start-Process pwsh -Wait -Arg '-c jq .'", {"pwsh", "jq"}),
        ("Start-Process pwsh -ArgumentList:'-c jq .'", {"pwsh", "jq"}),
        ("Start-Process -Verb RunAs pwsh '-c jq .'", {"pwsh", "jq"}),
        ("Start-Process -Verbose pwsh '-c jq .'", {"pwsh", "jq"}),
        ("saps pwsh -ArgumentList '-Command \"jq .\"'", {"pwsh", "jq"}),
        # A switch by its alias takes no value, so the program after it is still the program.
        ("Start-Process -vb pwsh '-c jq .'", {"pwsh", "jq"}),
        ("Start-Process -db pwsh '-c jq .'", {"pwsh", "jq"}),
        ("Start-Process -wi pwsh '-c jq .'", {"pwsh", "jq"}),
        ("Start-Process -cf pwsh '-c jq .'", {"pwsh", "jq"}),
        ("Start-Process -nnw -Lup pwsh '-c jq .'", {"pwsh", "jq"}),
        ("Start-Process -ea Stop pwsh '-c jq .'", {"pwsh", "jq"}),
        ("Start-Process bash -ArgumentList '-c', '\"jq .\"'", {"bash", "jq"}),
    ],
)
def test_the_command_policy_reads_the_script_start_process_starts_powershell_to_run(
    source: str, programs: set[str]
) -> None:
    assert skill_scripts.powershell_commands([source]) == [programs]


# An argument list that is no literal, or holds an item that is not one where the script stands, is refused, as an
# -EncodedCommand is.
@pytest.mark.parametrize(
    ("source", "program", "unread"),
    [
        ("Start-Process pwsh -ArgumentList $arguments", "pwsh", "pwsh <value>"),
        ("Start-Process pwsh -ArgumentList '-c', $script", "pwsh", "pwsh -c <value>"),
        ("Start-Process -FilePath powershell -ArgumentList @($arguments)", "powershell", "powershell <value>"),
        ("Start-Process pwsh @options", "pwsh", "pwsh <value>"),
        ("Start-Process pwsh -ArgumentList '-c \"jq .'", "pwsh", "pwsh <value>"),
    ],
)
def test_the_command_policy_refuses_a_start_process_argument_list_it_cannot_read(
    source: str, program: str, unread: str
) -> None:
    assert skill_scripts.powershell_commands([source]) == [{program, f"{UNREAD_PROGRAM}{unread}"}]


# A parameter Start-Process does not have, or a prefix of more than one, leaves its program unread rather than guessed.
@pytest.mark.parametrize(
    "source", ["Start-Process -Bogus x pwsh '-c jq .'", "Start-Process -W pwsh '-c jq .'", "start -Re x rg foo"]
)
def test_the_command_policy_refuses_a_start_process_parameter_it_cannot_place(source: str) -> None:
    assert skill_scripts.powershell_commands([source]) == [{f"{UNREAD_PROGRAM}{source}"}]


START_PROCESS_LISTING = (
    "$ErrorActionPreference = 'Stop'; "
    "$rows = @(foreach ($parameter in (Get-Command Start-Process).Parameters.Values) { "
    "[ordered]@{ name = $parameter.Name; switch = [bool]$parameter.SwitchParameter; "
    "aliases = @($parameter.Aliases) } }); "
    "[Console]::Out.WriteLine((ConvertTo-Json -InputObject $rows -Compress -Depth 3))"
)


class StartProcessTable(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "the table is Start-Process's parameters on Windows")
    def test_the_start_process_table_is_what_powershell_lists(self) -> None:
        listed = toolchain.powershell_json(
            START_PROCESS_LISTING, [], "UNUSED", "PowerShell could not list Start-Process"
        )
        self.assertEqual(
            skill_scripts.START_PROCESS_PARAMETERS,
            {row["name"]: (row["switch"], tuple(row["aliases"])) for row in listed},
        )


# Start-Process with no argument list, or one that runs a file, starts PowerShell running no script the source holds.
# The policy reads a -File's script in its own file, as it does for `pwsh -File`, never as a script here.
@pytest.mark.parametrize(
    "source", ["Start-Process pwsh", "Start-Process pwsh -ArgumentList '-File', './run.ps1', '-c', 'jq .'"]
)
def test_start_process_starting_powershell_on_a_file_or_nothing_runs_only_powershell(source: str) -> None:
    assert skill_scripts.powershell_commands([source]) == [{"pwsh"}]


@pytest.mark.parametrize(
    ("arguments", "words"),
    [
        pytest.param(["-c", "jq ."], ["-c", "jq", "."], id="items joined, then split at spaces"),
        pytest.param(['-c "jq ."'], ["-c", "jq ."], id="double quotes hold a space"),
        pytest.param(["-c 'jq .'"], ["-c", "'jq", ".'"], id="single quotes hold none"),
        pytest.param(["-c", "# noXte"], ["-c", "#", "noXte"], id="no other character quotes, escapes, or comments"),
        pytest.param(["C:\\a\\b.ps1"], ["C:\\a\\b.ps1"], id="a backslash is no escape"),
        pytest.param(["-c", None, "x"], ["-c", None], id="no literal"),
        pytest.param(['-c "jq'], [None], id="an unclosed quote"),
        pytest.param([], [], id="no items"),
    ],
)
def test_start_process_words_are_its_argument_list_joined_and_split_as_windows_does(
    arguments: list[str | None], words: list[str | None]
) -> None:
    assert skill_scripts.started_process_words(arguments) == words
