"""Fixture tests for the shell readers in tests/validation/skill_scripts.py: the programs a Bash script runs, through
its substitutions, wrappers, eval, and a shell's -c, and the script a pwsh or Windows PowerShell start-up runs, read
from Bash, a Python runner's list, and a PowerShell source. They stand apart from test_skill_scripts.py, which holds
the policies that read these, so that suite fits its shards.
"""

from __future__ import annotations

import ast
import functools
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bash_agreement
import command_wrappers
import git_subcommands
import powershell_commands
import skill_scripts
import toolchain
from command_wrappers import UNREAD_PROGRAM
from skill_scripts import CommandReader, PowerShellReading, shell_commands
from validation_support import VALIDATION_DOC, write_fixture_tree


def shell_git_subcommands(script: str) -> set[str]:
    """The subcommand of each git command a Bash script runs, as the runtime-support policy reads it:
    ejryan89-skill-core's git_subcommands, the script a PowerShell start-up runs read by powershell_readings."""
    return git_subcommands.bash_git_subcommands(script, skill_scripts.powershell_script_commands)


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
        # A trap's action, which Bash runs as eval runs its script when the condition fires.
        "trap 'rg foo' EXIT",
        "trap -- 'cd x && rg foo' 0 INT",
        'trap "rg $(x)" EXIT',
        "builtin trap 'xargs rg' EXIT",
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
        # ;& and ;;& end a case item as ;; does, so the next item's pattern runs nothing.
        ("case x in a) jq .;& b) rg foo;;& c) echo a;; esac", {"jq", "rg", "echo"}),
    ],
)
def test_shell_command_extraction_reads_unquoted_reserved_words_and_functions(script: str, programs: set[str]) -> None:
    assert shell_commands(script) == programs


# Bash's time keyword, read as Bash 5 reads it: at a pipeline's start, after `!`, a separator, or a reserved word, it
# takes -p and then --, each unquoted and optional, before the pipeline it times. A second -p, a quoted one, or one
# after -- is the timed command's own word. After a pipe, the newline right after one, or an assignment, `time` is the
# time program, which runs the program its words name.
TIMED_SCRIPTS: tuple[tuple[str, set[str]], ...] = (
    ("time jq .", {"jq"}),
    ("time -p jq .", {"jq"}),
    ("time -- jq .", {"jq"}),
    ("time -p -- jq .", {"jq"}),
    ("! time -p jq .", {"jq"}),
    ("time -p ! jq .", {"jq"}),
    ("time -p time -p jq .", {"jq"}),
    ("time -p jq . | rg x", {"jq", "rg"}),
    ("true && time -p jq .", {"true", "jq"}),
    ("if time -p jq .; then rg x; fi", {"jq", "rg"}),
    ("{ time -p jq .; }", {"jq"}),
    ("x=$(time -p jq .)", {"jq"}),
    ("echo a\ntime -p jq .", {"echo", "jq"}),
    ("time -p -p jq .", set()),
    ('time "-p" jq .', set()),
    ("time -- -p jq .", set()),
    ("echo a | time -p jq .", {"echo", "time", "jq"}),
    ("echo a |& time -p jq .", {"echo", "time", "jq"}),
    ("echo a |\n time -p jq .", {"echo", "time", "jq"}),
    ("A=1 time -p jq .", {"time", "jq"}),
)


@pytest.mark.parametrize(("script", "programs"), TIMED_SCRIPTS)
def test_shell_command_extraction_reads_the_program_the_time_keyword_times(script: str, programs: set[str]) -> None:
    assert shell_commands(script) == programs


@pytest.mark.parametrize(
    "script",
    [
        "time git push",
        "time -p git push | cat",
        "echo a | time -p git push",
        "x=$(time -p git push)",
    ],
)
def test_the_git_subcommand_reader_reads_the_git_command_the_time_keyword_times(script: str) -> None:
    assert shell_git_subcommands(script) == {"push"}


# The reader reads Bash through the one grammar, so each construct it once read its own way is read as Bash runs it.
@pytest.mark.parametrize(
    ("script", "programs"),
    [
        # A here-document's body is data, and only the substitutions of an unquoted one run.
        ("cat <<EOF\nrg foo\nEOF", {"cat"}),
        ("cat <<'EOF'\n$(rg foo)\nEOF", {"cat"}),
        ("cat <<EOF\n$(rg foo)\nEOF", {"cat", "rg"}),
        ("cat <<EOF\nit's\nEOF\nrg foo", {"cat", "rg"}),
        # A backslash ending a comment continues nothing, so the next line runs.
        ("# a comment \\\nrg foo", {"rg"}),
        ("echo a # \\\\\nrg foo", {"echo", "rg"}),
        # Arithmetic runs no program its words name.
        ("echo $(((now - modified) / 86400))", {"echo"}),
        # An assignment's value holding a substitution is one word, so the command after it is read.
        ('A=$(dirname -- "$0") exec bash "$COPY"', {"dirname", "exec", "bash"}),
        # A function's arguments run nothing, so a wrapper's name among them runs no program.
        ("f() { :; }; f xargs rg foo", set()),
    ],
)
def test_shell_command_extraction_reads_bash_as_the_grammar_reads_it(script: str, programs: set[str]) -> None:
    bash_agreement.assert_reader_agrees([script])
    assert shell_commands(script) == programs


# Bash runs the lines before a syntax error, so none of a refused script's programs can be trusted as read: the reader
# gives UNREAD_BASH with the grammar's reason, for the script and for one eval or a shell's -c runs, and the policy
# fails it naming the file it stands in.
@pytest.mark.parametrize(
    ("script", "reason"),
    [
        ("if true; then rg foo", "expected `fi`"),
        ("echo 'open", "a single quote is not closed"),
        ("eval 'rg \"'", "a double quote is not closed"),
        ("bash -c 'case x in'", "a case pattern is not closed"),
    ],
)
def test_shell_command_extraction_marks_bash_the_grammar_refuses(script: str, reason: str) -> None:
    assert {f"{skill_scripts.UNREAD_BASH}{reason}"} <= shell_commands(script)


# Windows lists a skill's files by a case-folded key, so run.py comes before SKILL.md there; the policy reports in the
# order of the paths as written on every platform.
@pytest.mark.parametrize("listing", ["as listed", "case-folded"])
def test_the_command_policy_fails_a_file_holding_bash_the_grammar_refuses_naming_it(
    listing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if listing == "case-folded":
        listed = skill_scripts._skill_files
        monkeypatch.setattr(
            skill_scripts,
            "_skill_files",
            lambda directory: sorted(listed(directory), key=lambda path: str(path).casefold()),
        )
    files = {
        "deploy-meta/alpha.json": '{"tools": []}',
        "skills/alpha/SKILL.md": "# alpha\n\n```bash\nif true; then echo a\n```\n",
        "skills/alpha/scripts/run.sh": "echo 'open\n",
        "skills/alpha/scripts/run.py": 'import subprocess\nsubprocess.run(["bash", "-c", "case x in"])\n',
    }
    reasons = {
        "skills/alpha/SKILL.md": "expected `fi`",
        "skills/alpha/scripts/run.py": "a case pattern is not closed",
        "skills/alpha/scripts/run.sh": "a single quote is not closed",
    }
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, files)
        problems = skill_scripts.skill_command_problems(root, set(), {"bash", "echo"})
    # The one pin of the problem's wording; the other tests take it from refused_bash_problem.
    assert problems == [
        f"skill alpha: {path} runs Bash the grammar does not read ({reason}); Bash runs the lines before a syntax "
        f"error, so none of its programs can be trusted as read; see {skill_scripts.COMMANDS_DOC}"
        for path, reason in sorted(reasons.items())
    ]


def test_the_command_policy_fails_a_dependency_script_holding_bash_the_grammar_refuses_for_the_skill_reaching_it() -> (
    None
):
    # A dependency's script is the skill's own when the skill imports it, so Bash the grammar refuses there fails the
    # skill that reaches it, by the script's path, and not one that reaches only a conforming script.
    files = {
        "deploy-meta/alpha.json": json.dumps({"tools": [], "skill_deps": ["core"]}),
        "deploy-meta/beta.json": json.dumps({"tools": [], "skill_deps": ["core"]}),
        "skills/alpha/SKILL.md": "# alpha\n",
        "skills/alpha/scripts/tool.py": "import refused\n",
        "skills/beta/SKILL.md": "# beta\n",
        "skills/beta/scripts/tool.py": "import conforming\n",
        "skills/core/SKILL.md": "# core\n",
        "skills/core/scripts/refused.py": 'import subprocess\nsubprocess.run(["bash", "-c", "case x in"])\n',
        "skills/core/scripts/conforming.py": 'import subprocess\nsubprocess.run(["bash", "-c", "echo a"])\n',
    }
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, files)
        problems = skill_scripts.skill_command_problems(root, set(), {"bash", "echo"})
    assert problems == [
        skill_scripts.refused_bash_problem(
            "skill alpha: skills/core/scripts/refused.py",
            f"{skill_scripts.UNREAD_BASH}a case pattern is not closed",
            skill_scripts.COMMANDS_DOC,
        )
    ]


# Bash runs a refused script's lines before its syntax error, a git call or a checker among them, so the shared-client
# and checker policies fail Bash the grammar refuses where a script hands it a shell, naming the line, and pass the
# same call handed Bash the grammar reads.
@pytest.mark.parametrize(
    ("script", "problem"), [("git push\nif true; then", True), ("echo a\nif true; then :; fi", False)]
)
def test_the_client_policy_fails_bash_the_grammar_refuses_where_a_script_hands_it_a_shell(
    script: str, problem: bool
) -> None:
    source = f"import subprocess\n\nsubprocess.run({script!r}, shell=True)\n"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {"skills/beta/SKILL.md": "# beta\n", "skills/beta/scripts/run.py": source})
        problems = skill_scripts.client_command_problems(root)
    expected = skill_scripts.refused_bash_problem(
        "skills/beta/scripts/run.py:3", f"{skill_scripts.UNREAD_BASH}expected `fi`", skill_scripts.COMMANDS_DOC
    )
    assert problems == ([expected] if problem else [])


# The checker policy reads Bash two ways: the program a runner call starts, a shell=True string's or a literal -c
# script's, and a -c script given as an f-string, which only the second reads; each case below reaches one or both.
@pytest.mark.parametrize(
    ("call", "problem"),
    [
        ('["bash", "-c", "shellcheck a.sh\\nif true; then"]', True),
        ('["bash", "-c", "echo a"]', False),
        ('"shellcheck a.sh\\nif true; then", shell=True', True),
        ('"echo a", shell=True', False),
        ('["bash", "-c", f"shellcheck {x}\\nif true; then"]', True),
        ('["bash", "-c", f"echo {x}"]', False),
    ],
)
def test_the_checker_policy_fails_bash_the_grammar_refuses_where_a_script_hands_it_a_shell(
    call: str, problem: bool
) -> None:
    source = f'import subprocess\n\nx = "a.sh"\nsubprocess.run({call})\n'
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {"tools/x.py": source})
        problems = skill_scripts.checker_command_problems(root, [root / "tools" / "x.py"])
    expected = skill_scripts.refused_bash_problem(
        "tools/x.py:4", f"{skill_scripts.UNREAD_BASH}expected `fi`", VALIDATION_DOC
    )
    assert problems == ([expected] if problem else [])


# A program a wrapper runs that only running settles is named by its own word as the script writes it, wherever the
# wrapper's options put it and whatever other word running settles stands before it, never by the reader's own
# placeholder for the part running settles.
@pytest.mark.parametrize(
    ("script", "unread"),
    [
        ('xargs "$(command -v rg)" foo', '"$(command -v rg)"'),
        ("xargs `which rg`", "`which rg`"),
        ('env "a$(b)" x', '"a$(b)"'),
        ('timeout "$(cat limit)" "$(command -v rg)" foo', '"$(command -v rg)"'),
        ('sudo -u "$(whoami)" "$(command -v rg)"', '"$(command -v rg)"'),
        ('nice -n "$((n + 1))" "$(command -v rg)"', '"$(command -v rg)"'),
        ('xargs -I "$(i)" "$(p)"', '"$(p)"'),
        ('timeout "$(t)" xargs "$(p)"', '"$(p)"'),
    ],
)
def test_a_wrapped_program_only_running_settles_is_named_as_written(script: str, unread: str) -> None:
    programs = {program for program in shell_commands(script) if program.startswith(UNREAD_PROGRAM)}
    assert programs == {f"{UNREAD_PROGRAM}{unread}"}


# Inside a script eval, a shell's -c, env -S, or a trap's action runs, a word is the text its command reads, not as
# written, so a part running settles there is named as the word the reader cannot read, never by the reader's own
# placeholder.
@pytest.mark.parametrize(
    "script",
    [
        'sh -c "xargs $(p)"',
        'eval "xargs $(p) y"',
        'trap "xargs $(p)" EXIT',
        'env -S "xargs $(p)"',
        'env -S "$(p) x"',
        'bash -c "xargs \\"$(p)\\""',
    ],
)
def test_a_wrapped_program_only_running_settles_inside_a_started_script_is_an_unread_word(script: str) -> None:
    programs = {program for program in shell_commands(script) if program.startswith(UNREAD_PROGRAM)}
    assert programs == {f"{UNREAD_PROGRAM}{powershell_commands.UNREAD_WORD}"}


# The temporary-folder policy reads a mktemp command whole, its redirections' substitutions as the commands they run,
# which leave where mktemp makes its file to its template; a script the grammar refuses, or one of whose substitutions
# it refuses, is named as unread, since Bash runs the lines before a syntax error.
@pytest.mark.parametrize("redirection", ["2>/dev/null", "2>`bash -c 'case x in'`", "\\\n  2>/dev/null"])
def test_a_mktemp_command_is_judged_whole_wherever_its_words_and_redirections_stand(redirection: str) -> None:
    text = (
        'JUDGED=$(python private_folder.py temporary-directory "${TMPDIR:-/tmp}")\n'
        'TEMPLATE="${JUDGED#TEMPORARY_DIRECTORY }/x.XXXXXX"\n'
        f'A=$(mktemp "$TEMPLATE" {redirection})\n'
    )
    assert skill_scripts._bash_temporary_problems("make.sh", text) == []


# A mktemp a wrapper, eval, a shell's -c, or a trap's action runs is judged as one the script runs itself.
@pytest.mark.parametrize(
    "script", ["env mktemp", "eval mktemp", "bash -c mktemp", "trap mktemp EXIT", "trap 'rm -f \"$(mktemp)\"' EXIT"]
)
def test_a_mktemp_a_started_script_runs_is_judged(script: str) -> None:
    problems = skill_scripts._bash_temporary_problems("make.sh", f"echo first\n{script}\n")
    assert [problem.partition(" ")[0] for problem in problems] == ["make.sh:2:"]


@pytest.mark.parametrize("script", ["if true; then mktemp", 'A=$(mktemp "$T" 2>$(if))', "V=${A /tmp"])
def test_a_script_the_grammar_refuses_is_named_as_unread_by_the_temporary_folder_policy(script: str) -> None:
    problems = skill_scripts._bash_temporary_problems("make.sh", f"echo first\n{script}\n")
    assert [problem.partition(" (")[0] for problem in problems] == [
        "make.sh:1: validation cannot read the script as Bash"
    ]


# A line continuation joins its lines, so the word after it is the git command's subcommand.
def test_the_git_subcommand_reader_reads_past_a_line_continuation() -> None:
    assert shell_git_subcommands("git \\\n  push") == {"push"}


# An unquoted separator still ends the command, so the word after it is the next command's program.
@pytest.mark.parametrize("separator", [";", "&", "&&", "|", "||", "|&", "\n"])
def test_shell_command_extraction_reads_the_program_after_an_unquoted_separator(separator: str) -> None:
    assert shell_commands(f"echo a {separator} rg foo") == {"echo", "rg"}


# A lookup, a substitution in single quotes or a comment, arithmetic, and a trap that lists or resets run nothing.
@pytest.mark.parametrize(
    "script",
    [
        "command -v rg",
        "echo '$(rg foo)'",
        "# run `rg foo` here",
        "echo $((1 + 2))",
        "trap -p 'rg foo' EXIT",
        "trap 'rg foo'",
        "trap - 'rg foo' EXIT",
    ],
)
def test_shell_command_extraction_finds_no_program_where_nothing_runs(script: str) -> None:
    assert "rg" not in shell_commands(script)


# A wrapper a package manager installs under a g or u prefix, as gtimeout, is a program of its own the command policy
# holds, and is read past, as the wrapper it builds, to the program it runs.
@pytest.mark.parametrize(
    ("script", "programs"),
    [
        ("gtimeout 5 jq .", {"gtimeout", "jq"}),
        ("genv A=1 rg x", {"genv", "rg"}),
        ("genv -S 'jq .'", {"genv", "jq"}),
        ("utimeout --signal TERM 5 jq .", {"utimeout", "jq"}),
        ("gxargs -n 1 rg x", {"gxargs", "rg"}),
        ("gnohup gstdbuf -o L jq .", {"gnohup", "gstdbuf", "jq"}),
        ("ucommand -v rg", {"ucommand"}),
    ],
)
def test_shell_command_extraction_reads_past_a_wrapper_under_a_prefix(script: str, programs: set[str]) -> None:
    assert shell_commands(script) == programs


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
    assert command_wrappers.unwrapped(words) == program


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
    assert command_wrappers.unwrapped(words) == [f"{UNREAD_PROGRAM}{unread}"]
    assert f"{UNREAD_PROGRAM}{unread}" in shell_commands(" ".join(words))
    assert "gh" not in shell_commands(" ".join(words))


# A shell started without -c runs a file or its standard input, and -o's value is no option.
@pytest.mark.parametrize("words", [["-o", "c", "script.sh"], ["script.sh", "-c", "x"], ["-e"], ["--rcfile", "-c"]])
def test_shell_script_index_finds_no_script_where_the_shell_runs_none(words: list[str]) -> None:
    assert command_wrappers.shell_script_index(words) is None


def test_the_command_policy_reads_a_python_list_s_shell_script_among_its_options() -> None:
    for command in ('["bash", "-e", "-c", "jq ."]', '("sh", "-lc", "jq .")', '["bash", "-c", "-e", "jq .", "x"]'):
        source = f"import subprocess\nsubprocess.run({command})\n"
        assert "jq" in CommandReader(ast.parse(source)).commands(set()), command


# A start-up of pwsh or Windows PowerShell runs the script its -Command, -CommandWithArgs, or Windows PowerShell's
# first positional word holds; the policy reads that script's commands as PowerShell's parser reads them.
POWERSHELL_START_UP_SCRIPTS = (
    "pwsh -Command 'jq .'",
    "pwsh -NoProfile -c 'jq .'",
    "powershell 'jq .'",
    "powershell.exe -ExecutionPolicy Bypass jq .",
    "pwsh -cwa 'jq $args' .",
    "env pwsh -c 'jq .'",
    "pwsh -c 'Invoke-Expression \"jq .\"'",
)


@pytest.mark.usefixtures("primed_bash_scripts")
@pytest.mark.parametrize("script", POWERSHELL_START_UP_SCRIPTS)
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
STARTED_GIT_PUSH_SCRIPTS = (
    "bash -c 'git push'",
    "sh -ec 'git -C dir push'",
    "eval 'git push'",
    "eval git push",
    "trap 'git push' EXIT",
    "env bash -c 'git push'",
    "find . -exec bash -c 'git push' {} ';'",
    "x=$(bash -c 'git push')",
    "pwsh -c 'git push'",
    "powershell -NoProfile -Command 'git -C $directory push'",
    "pwsh -c 'bash -c \"git push\"'",
    "pwsh -c 'Start-Process git -ArgumentList push'",
    "pwsh -c \"pwsh -c 'git push'\"",
)


@pytest.mark.usefixtures("primed_bash_scripts")
@pytest.mark.parametrize("script", STARTED_GIT_PUSH_SCRIPTS)
def test_the_git_subcommand_reader_reads_the_git_command_a_started_script_runs(script: str) -> None:
    assert shell_git_subcommands(script) == {"push"}


# A git command a wrapper of COMMAND_WRAPPERS or find's -exec family runs directly is read past the wrapper's options,
# and a wrapper of a wrapper, to the git command's own words; find's action ends at its `;` or `{} +`. A bare `time` is
# Bash's keyword, not the time program, so the program's row reaches it through env, and the keyword's rows are read
# past its -p and --, and after `!`. An action with no terminator runs to the end of the command.
# tests/audit-skill/test_audit_skill_runtime_needs.py holds audit-skill's reader to these rows.
WRAPPED_GIT_PUSH_SCRIPTS = (
    "xargs git push",
    "env git push",
    "timeout 5 git push",
    "find . -exec git push {} +",
    "find . -exec git push \\;",
    "find . -exec git push",
    "command git push",
    "builtin git push",
    "exec git push",
    "nohup git push",
    "env time -p git push",
    "time -p git push",
    "time -p -- git push",
    "! time -p git push",
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
    # A wrapper a package manager installs under a g or u prefix is read as the wrapper it builds.
    "gtimeout 5 git push",
    "genv GIT_DIR=x git push",
    "gnice -n 5 git push",
    "gnohup git push",
    "gstdbuf -o L git push",
    "gxargs git push",
    "utimeout 5 git push",
    "uenv -i git push",
    "nohup genv utimeout 5 git push",
    "find . -exec gtimeout 5 git push {} +",
)


@pytest.mark.parametrize("script", WRAPPED_GIT_PUSH_SCRIPTS)
def test_the_git_subcommand_reader_reads_the_git_command_a_wrapper_runs(script: str) -> None:
    assert shell_git_subcommands(script) == {"push"}


# The PowerShell twin of WRAPPED_GIT_PUSH_SCRIPTS: on Linux pwsh, where each wrapper of COMMAND_WRAPPERS and find are
# programs, the git command one runs is read past the wrapper's options as the Bash reader reads it. PowerShell has no
# time keyword and no escaped `;`, and its `{}` is a script block, which it reads as no literal.
POWERSHELL_WRAPPED_GIT_PUSH_SOURCES = (
    "xargs git push",
    "env git push",
    "timeout 5 git push",
    "find . -exec git push '{}' +",
    "find . -exec git push {} +",
    "find . -exec git push ';'",
    "find . -exec git push",
    "command git push",
    "builtin git push",
    "exec git push",
    "nohup git push",
    "time -p git push",
    "nice -n 5 git push",
    "env -i A=1 git push",
    "env -S 'git push'",
    "stdbuf -o L git push",
    "xargs -n 1 git push",
    "timeout --signal TERM 5 git push",
    "find . -execdir git push ';'",
    "find . -ok git push '{}' ';'",
    "find . -okdir git push '{}' +",
    "nohup env timeout 5 git push",
    "find . -exec env git push '{}' +",
    "Start-Process env -ArgumentList 'git', 'push'",
    "env bash -c 'git push'",
    "env pwsh -c 'git push'",
    "gtimeout 5 git push",
    "genv GIT_DIR=x git push",
    "uenv -i git push",
)


def _powershell_git_subcommands(reading: PowerShellReading) -> set[str]:
    """The subcommand of each git command a PowerShell source runs, as the runtime-support policy reads it:
    ejryan89-skill-core's git_subcommands over the commands powershell_readings lists."""
    return git_subcommands.powershell_git_subcommands(reading.commands, skill_scripts.powershell_script_commands)


@pytest.mark.parametrize("source", POWERSHELL_WRAPPED_GIT_PUSH_SOURCES)
def test_the_powershell_reader_reads_the_git_command_a_wrapper_runs(
    source: str, powershell_reading: dict[str, PowerShellReading]
) -> None:
    assert "git" in powershell_reading[source].programs
    assert _powershell_git_subcommands(powershell_reading[source]) == {"push"}


# A PowerShell source's Bash -c script appends its commands to the reading, each part only running settles as the
# command reader places it, UNREAD_PART and its word's place, and the runtime-support reader reads those words through
# git_subcommands: as a subcommand that may be any, as one eval word, as a -c script and a start-up's script it cannot
# read, just as it reads a Bash script's own UNREAD_PART.
def test_a_placed_part_reaches_the_git_subcommand_reader_as_one_only_running_settles() -> None:
    cases = {
        "bash -c 'git $(x)'": {powershell_commands.UNREAD_WORD},
        "bash -c 'git push $(x)'": {"push"},
        "bash -c 'bash -c \"$(x)\"'": set(),
        "bash -c 'eval \"git push $(x)\"'": {"push"},
        "bash -c 'pwsh -c \"$(x)\"'": set(),
    }
    readings = skill_scripts.prime(lambda: skill_scripts.powershell_readings(list(cases)))
    for (source, expected), reading in zip(cases.items(), readings, strict=True):
        placed = [word for command in reading.commands for word in command if skill_scripts.PLACED_PART.fullmatch(word)]
        assert placed, source
        assert all(word != git_subcommands.UNREAD_PART for word in placed), source
        assert _powershell_git_subcommands(reading) == expected, source


# Git named by a path is no declared tool's name, so the command policy reads no program of it, but it runs git, so
# the runtime-support reader reads its subcommand.
POWERSHELL_GIT_PATH_SOURCES = (
    "& 'C:/Program Files/Git/cmd/git' push",
    "C:\\Git\\cmd\\git.exe push",
    "/usr/bin/git push",
)


@pytest.mark.parametrize("source", POWERSHELL_GIT_PATH_SOURCES)
def test_the_powershell_reader_reads_the_git_command_a_path_names(
    source: str, powershell_reading: dict[str, PowerShellReading]
) -> None:
    assert set(powershell_reading[source].programs) == set()
    assert _powershell_git_subcommands(powershell_reading[source]) == {"push"}


# The issue's forms: the program a wrapper or find's action runs is among a PowerShell source's programs, so the
# declared-tool policy holds it, and a wrapper that runs none, or only looks one up, adds none.
POWERSHELL_WRAPPER_PROGRAMS = (
    ("env gh pr list", {"env", "gh"}),
    ("xargs gh pr list", {"xargs", "gh"}),
    ("find . -exec gh pr list '{}' +", {"find", "gh"}),
    ("xargs -n $count gh pr list", {"xargs", "gh"}),
    ("nohup env timeout 5 gh pr list", {"nohup", "env", "timeout", "gh"}),
    ("gtimeout 5 gh pr list", {"gtimeout", "gh"}),
    ("command -v gh", {"command"}),
    ("xargs echo gh", {"xargs", "echo"}),
    ("env", {"env"}),
)


@pytest.mark.parametrize(("source", "programs"), POWERSHELL_WRAPPER_PROGRAMS)
def test_the_powershell_reader_reads_the_program_a_wrapper_runs(
    source: str, programs: set[str], powershell_reading: dict[str, PowerShellReading]
) -> None:
    assert set(powershell_reading[source].programs) == programs


# A program a wrapper or find's action runs that no literal names, and a shell or start-up a wrapper runs on a script
# that is no literal, is refused as unread rather than passed unseen.
POWERSHELL_UNREAD_WRAPPED_PROGRAMS = (
    ("env $tool", "<value>"),
    ("xargs $tool pr list", "<value> pr list"),
    ("find . -exec $tool '{}' +", "<value> {}"),
    ("env pwsh -c $script", "pwsh -c <value>"),
    ("env bash -c $script", "bash -c <value>"),
    ("env --unknown gh", "env --unknown"),
)


@pytest.mark.parametrize(("source", "unread"), POWERSHELL_UNREAD_WRAPPED_PROGRAMS)
def test_the_powershell_reader_refuses_a_wrapped_program_it_cannot_read(
    source: str, unread: str, powershell_reading: dict[str, PowerShellReading]
) -> None:
    assert f"{UNREAD_PROGRAM}{unread}" in powershell_reading[source].programs


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
    assert shell_git_subcommands(script) == subcommands


# A started script that runs no git command gives no subcommand, though a word of it is one.
STARTED_SCRIPTS_RUNNING_NO_GIT = (
    "bash -c 'echo push'",
    "bash ./push.sh",
    "eval 'echo git push'",
    "pwsh -c 'Write-Output push'",
    "pwsh -File push.ps1 git push",
)


@pytest.mark.usefixtures("primed_bash_scripts")
@pytest.mark.parametrize("script", STARTED_SCRIPTS_RUNNING_NO_GIT)
def test_the_git_subcommand_reader_finds_none_where_a_started_script_runs_no_git(script: str) -> None:
    assert shell_git_subcommands(script) == set()


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
    assert shell_git_subcommands(script) == subcommands


# A subcommand no literal names is recorded as its word: the Bash text, or UNREAD_WORD for PowerShell's.
UNNAMED_SUBCOMMAND_SCRIPTS = (
    ("bash -c 'git \"$sub\"'", "$sub"),
    ("pwsh -c 'git $sub'", powershell_commands.UNREAD_WORD),
    ("pwsh -c 'Start-Process git -ArgumentList $arguments'", powershell_commands.UNREAD_WORD),
)


@pytest.mark.usefixtures("primed_bash_scripts")
@pytest.mark.parametrize(("script", "subcommand"), UNNAMED_SUBCOMMAND_SCRIPTS)
def test_the_git_subcommand_reader_records_a_subcommand_no_literal_names_as_its_word(
    script: str, subcommand: str
) -> None:
    assert shell_git_subcommands(script) == {subcommand}


# Start-Process starts pwsh or Windows PowerShell with the script its -ArgumentList holds, in any spelling of the
# parameters PowerShell binds, as a list, an array, or one string; and its twin, a Bash shell started with -c.
START_PROCESS_STARTED_SCRIPTS = (
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
)


@pytest.mark.parametrize(("source", "programs"), START_PROCESS_STARTED_SCRIPTS)
def test_the_command_policy_reads_the_script_start_process_starts_powershell_to_run(
    source: str, programs: set[str], powershell_reading: dict[str, PowerShellReading]
) -> None:
    assert set(powershell_reading[source].programs) == programs


# An argument list that is no literal, or holds an item that is not one where the script stands, is refused, as an
# -EncodedCommand is.
START_PROCESS_UNREAD_ARGUMENT_LISTS = (
    ("Start-Process pwsh -ArgumentList $arguments", "pwsh", "pwsh <value>"),
    ("Start-Process pwsh -ArgumentList '-c', $script", "pwsh", "pwsh -c <value>"),
    ("Start-Process -FilePath powershell -ArgumentList @($arguments)", "powershell", "powershell <value>"),
    ("Start-Process pwsh @options", "pwsh", "pwsh <value>"),
    ("Start-Process pwsh -ArgumentList '-c \"jq .'", "pwsh", "pwsh <value>"),
)


@pytest.mark.parametrize(("source", "program", "unread"), START_PROCESS_UNREAD_ARGUMENT_LISTS)
def test_the_command_policy_refuses_a_start_process_argument_list_it_cannot_read(
    source: str, program: str, unread: str, powershell_reading: dict[str, PowerShellReading]
) -> None:
    assert set(powershell_reading[source].programs) == {program, f"{UNREAD_PROGRAM}{unread}"}


# A parameter Start-Process does not have, or a prefix of more than one, leaves its program unread rather than guessed.
START_PROCESS_UNPLACED_PARAMETERS = (
    "Start-Process -Bogus x pwsh '-c jq .'",
    "Start-Process -W pwsh '-c jq .'",
    "start -Re x rg foo",
)


@pytest.mark.parametrize("source", START_PROCESS_UNPLACED_PARAMETERS)
def test_the_command_policy_refuses_a_start_process_parameter_it_cannot_place(
    source: str, powershell_reading: dict[str, PowerShellReading]
) -> None:
    assert set(powershell_reading[source].programs) == {f"{UNREAD_PROGRAM}{source}"}


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
            powershell_commands.START_PROCESS_PARAMETERS,
            {row["name"]: (row["switch"], tuple(row["aliases"])) for row in listed},
        )


# Start-Process with no argument list, or one that runs a file, starts PowerShell running no script the source holds.
# The policy reads a -File's script in its own file, as it does for `pwsh -File`, never as a script here.
START_PROCESS_FILE_OR_NOTHING = (
    "Start-Process pwsh",
    "Start-Process pwsh -ArgumentList '-File', './run.ps1', '-c', 'jq .'",
)


@pytest.mark.parametrize("source", START_PROCESS_FILE_OR_NOTHING)
def test_start_process_starting_powershell_on_a_file_or_nothing_runs_only_powershell(
    source: str, powershell_reading: dict[str, PowerShellReading]
) -> None:
    assert set(powershell_reading[source].programs) == {"pwsh"}


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
    assert powershell_commands.started_process_words(arguments) == words


# Every PowerShell source a row above reads, each once.
POWERSHELL_SOURCES = tuple(
    dict.fromkeys(
        [
            *POWERSHELL_WRAPPED_GIT_PUSH_SOURCES,
            *POWERSHELL_GIT_PATH_SOURCES,
            *(source for source, _ in POWERSHELL_WRAPPER_PROGRAMS),
            *(source for source, _ in POWERSHELL_UNREAD_WRAPPED_PROGRAMS),
            *(source for source, _ in START_PROCESS_STARTED_SCRIPTS),
            *(source for source, _, _ in START_PROCESS_UNREAD_ARGUMENT_LISTS),
            *START_PROCESS_UNPLACED_PARAMETERS,
            *START_PROCESS_FILE_OR_NOTHING,
        ]
    )
)


@pytest.fixture(scope="module")
def powershell_reading() -> dict[str, PowerShellReading]:
    """Each PowerShell row's reading, by its source: powershell_readings over every row's source in one call, with each
    script a source starts primed first, so the module starts pwsh once for its rows and once for each level of the
    scripts they start, rather than once for each row."""
    sources = list(POWERSHELL_SOURCES)
    readings = skill_scripts.prime(functools.partial(skill_scripts.powershell_readings, sources))
    return dict(zip(sources, readings, strict=True))


@pytest.fixture(scope="module")
def primed_bash_scripts() -> None:
    """Prime the PowerShell script each Bash row that starts PowerShell runs, as both Bash readers reach it, so those
    rows read it from the one batched read."""
    scripts = [
        *POWERSHELL_START_UP_SCRIPTS,
        *STARTED_GIT_PUSH_SCRIPTS,
        *STARTED_SCRIPTS_RUNNING_NO_GIT,
        *(script for script, _ in UNNAMED_SUBCOMMAND_SCRIPTS),
    ]
    skill_scripts.prime(lambda: [(shell_commands(script), shell_git_subcommands(script)) for script in scripts])


# One source from each table above, at a different place in the batch: its own single-source read is the batched one.
SINGLE_SOURCES = (
    POWERSHELL_WRAPPED_GIT_PUSH_SOURCES[-1],
    POWERSHELL_GIT_PATH_SOURCES[0],
    POWERSHELL_WRAPPER_PROGRAMS[4][0],
    POWERSHELL_UNREAD_WRAPPED_PROGRAMS[3][0],
    START_PROCESS_STARTED_SCRIPTS[0][0],
    START_PROCESS_UNREAD_ARGUMENT_LISTS[1][0],
    START_PROCESS_UNPLACED_PARAMETERS[2],
    START_PROCESS_FILE_OR_NOTHING[1],
)


def test_the_batched_reading_is_each_source_s_single_reading(powershell_reading: dict[str, PowerShellReading]) -> None:
    for source in SINGLE_SOURCES:
        assert skill_scripts.powershell_readings([source]) == [powershell_reading[source]], source


class _CountedReadings:
    """skill_scripts.powershell_readings, recording the sources of each call, each a pwsh start."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.read = skill_scripts.powershell_readings

    def __call__(self, sources: list[str]) -> list[PowerShellReading]:
        self.calls.append(list(sources))
        return self.read(sources)


# A Bash script that starts PowerShell on a script that starts PowerShell in turn, and one that starts it once.
NESTED_STARTS = ("pwsh -c \"pwsh -c 'jq .'\"", "pwsh -c 'rg x'", "pwsh -c 'rg x'")


def _nested_programs() -> list[set[str]]:
    return [shell_commands(script) for script in NESTED_STARTS]


def test_prime_reads_each_level_of_started_scripts_in_one_pwsh_start(monkeypatch: pytest.MonkeyPatch) -> None:
    counted = _CountedReadings()
    monkeypatch.setattr(skill_scripts, "powershell_readings", counted)
    monkeypatch.setattr(skill_scripts, "_SCRIPT_READINGS", {})
    primed = skill_scripts.prime(_nested_programs)
    # The scripts the Bash scripts start, deferred in one list, whose own started script is read before them.
    assert counted.calls == [["pwsh -c 'jq .'", "rg x"], ["jq ."], ["pwsh -c 'jq .'", "rg x"]]
    assert primed == [{"pwsh", "jq"}, {"pwsh", "rg"}, {"pwsh", "rg"}]
    # Primed, a read starts no pwsh, and gives what the prime gave.
    assert _nested_programs() == primed
    assert len(counted.calls) == 3
    # Unprimed, each script is read on its own, once, and gives the same.
    monkeypatch.setattr(skill_scripts, "_SCRIPT_READINGS", {})
    counted.calls.clear()
    assert _nested_programs() == primed
    assert counted.calls == [["pwsh -c 'jq .'"], ["jq ."], ["rg x"]]


# The finding the command policies report, up to its remedy, for a runner the module hands on as a value.
HANDED_ON = "cannot read the program `start` runs, which the module hands on as a value"


def test_command_reader_finds_a_runner_behind_any_binding_of_a_name() -> None:
    # The binding read first is a name that leads to no runner; the runner lies behind the other binding, which the
    # walk still has to read.
    script = (
        "import subprocess\nimport threading\ndef rebound(flag):\n    other = len\n    start = subprocess.run\n"
        "    if flag:\n        start = other\n    return threading.Thread(target=start)\n"
    )
    problems = CommandReader(ast.parse(script)).problems("tool.py")
    assert [problem.partition(";")[0] for problem in problems] == [f"tool.py:8 {HANDED_ON}"]


@pytest.mark.parametrize(("first", "expected"), [("len", []), ("subprocess.run", [f"tool.py:24 {HANDED_ON}"])])
def test_command_reader_follows_each_name_of_a_rebound_loop_variable_once(first: str, expected: list[str]) -> None:
    # A walk that kept a set of the names it had followed for each path followed `start` once per order the branches
    # that rebind it from itself can run in: eight branches made hundreds of thousands of reads. Followed once per
    # question, the reads grow with the bindings, and a runner bound before the loop is still found.
    arms = "".join(f"        elif choice == {arm}:\n            start = start\n" for arm in range(8))
    script = (
        "import subprocess\nimport threading\ndef looped(flag, choice):\n"
        f"    start = {first}\n    while flag:\n        if not choice:\n            start = start\n{arms}"
        "    return threading.Thread(target=start)\n"
    )
    runner = CommandReader._runner
    reads = 0

    def counted(reader: CommandReader, node: ast.expr) -> str:
        nonlocal reads
        reads += 1
        if reads > 500:
            raise AssertionError("the command reader followed a name more than 500 times")
        return runner(reader, node)

    with mock.patch.object(CommandReader, "_runner", counted):
        problems = CommandReader(ast.parse(script)).problems("tool.py")
    assert [problem.partition(";")[0] for problem in problems] == expected


# A chain of names, each bound in two branches to the one before, over a first binding the case gives.
CHAIN_LINKS = 16


def _two_branch_chain(first: str) -> str:
    links = "".join(
        f"    if flag:\n        a{link} = a{link - 1}\n    else:\n        a{link} = a{link - 1}\n"
        for link in range(1, CHAIN_LINKS + 1)
    )
    return (
        f"import subprocess\ndef chained(flag, given):\n    a0 = {first}\n{links}"
        f'    subprocess.run([a{CHAIN_LINKS}, "status"], check=True)\n'
    )


def test_command_reader_reads_each_name_of_a_chain_cut_in_one_branch_once_per_cut() -> None:
    # Each link is the one before, cut at its `%` in one branch and whole in the other, so each name is read under
    # two cuts. A record copied at each cut would double the reads with each link again; one record keyed by the cut
    # reads each name once under each cut.
    links = "".join(
        f"    a{link} = a{link - 1} % given if flag else a{link - 1}\n" for link in range(1, CHAIN_LINKS + 1)
    )
    script = (
        f'import subprocess\ndef _chained(flag, given):\n    a0 = "git"\n{links}'
        f'    subprocess.run([a{CHAIN_LINKS}, "status"], check=True)\n'
    )
    sources = CommandReader._name_sources
    reads = 0

    def counted(reader: CommandReader, node: ast.Name, question: skill_scripts.Question) -> list[skill_scripts.Source]:
        nonlocal reads
        reads += 1
        if reads > 8 * CHAIN_LINKS:
            raise AssertionError(f"the command reader read a name more than {8 * CHAIN_LINKS} times")
        return sources(reader, node, question)

    reader = CommandReader(ast.parse(script))
    with mock.patch.object(CommandReader, "_name_sources", counted):
        problems = reader.problems("tool.py")
    assert problems == []
    assert sorted({found for call in reader.calls for found in reader.started(call)}) == [(3, "git")]


@pytest.mark.parametrize(
    ("script", "commands", "expected"),
    [
        pytest.param(
            'import subprocess\ndef run(flag, x):\n    TEMPLATE = "git status {} && curl https://example.invalid"\n'
            "    command = TEMPLATE.format(x) if flag else TEMPLATE\n"
            "    subprocess.run(command, shell=True, check=True)\n",
            ["curl", "git"],
            [],
            id="a name through .format() and plainly",
        ),
        pytest.param(
            'import subprocess\ndef run(flag, x):\n    TEMPLATE = "echo {} && git push"\n'
            "    subprocess.run(TEMPLATE.format(x) if flag else TEMPLATE, shell=True, check=True)\n",
            ["echo", "git"],
            [],
            id="a name through .format() and plainly in one expression",
        ),
        pytest.param(
            'import subprocess\ndef template():\n    return "git status %s && curl https://example.invalid"\n'
            "def run(flag, x):\n    subprocess.run((template() % x) if flag else template(), shell=True, check=True)\n",
            ["curl", "git"],
            [],
            id="a return through % and plainly",
        ),
        pytest.param(
            "import subprocess\ndef _run(command, flag, x):\n"
            "    subprocess.run(command % x if flag else command, shell=True, check=True)\n"
            'def main(flag, x):\n    _run("git status %s && curl https://example.invalid", flag, x)\n',
            ["curl", "git"],
            [],
            id="a parameter through % and plainly",
        ),
        pytest.param(
            'import subprocess\nPROGRAM = "%s"\ndef run(flag, x):\n    program = PROGRAM if flag else PROGRAM % x\n'
            '    subprocess.run([program, "status"], check=True)\n',
            [],
            [
                "tool.py:2 cannot read %-format command without a literal program prefix",
                "tool.py:2 cannot read the program in `'%s'`, which names none",
            ],
            id="a name plainly and through %",
        ),
        pytest.param(
            'import subprocess\nPROGRAM = "{}"\ndef run(flag, x):\n'
            "    program = PROGRAM if flag else PROGRAM.format(x)\n"
            '    subprocess.run([program, "status"], check=True)\n',
            [],
            [
                "tool.py:2 cannot read .format() command without a literal program prefix",
                "tool.py:2 cannot read the program in `'{}'`, which names none",
            ],
            id="a name plainly and through .format()",
        ),
    ],
)
def test_command_reader_reads_a_key_again_under_each_cut_that_reaches_it(
    script: str, commands: list[str], expected: list[str]
) -> None:
    # A `%` or `.format()` cuts each source's text at its first placeholder, so what a key gives depends on the cut
    # above it: a key read once under one cut must be read again under another, or that route's programs are lost.
    reader = CommandReader(ast.parse(script))
    assert sorted(reader.commands({"curl", "echo", "git"})) == commands
    assert [problem.partition(";")[0] for problem in reader.problems("tool.py")] == expected


@pytest.mark.parametrize(
    ("first", "programs", "expected"),
    [
        ('"git"', [(3, "git")], []),
        ("given", [], ["tool.py:2 cannot read the program in parameter given, which no call in this module passes"]),
    ],
)
def test_command_reader_reads_each_name_of_a_two_branch_chain_once(
    first: str, programs: list[tuple[int, str]], expected: list[str]
) -> None:
    # A walk that kept a set of the names on each path read a name once for every path to it, two to the power of
    # the chain's length: 131,071 reads of a name at sixteen links. Read once per question, each name is reached once
    # through each of its two bindings.
    sources = CommandReader._name_sources
    reads = 0

    def counted(reader: CommandReader, node: ast.Name, question: skill_scripts.Question) -> list[skill_scripts.Source]:
        nonlocal reads
        reads += 1
        if reads > 4 * CHAIN_LINKS:
            raise AssertionError(f"the command reader read a name more than {4 * CHAIN_LINKS} times")
        return sources(reader, node, question)

    reader = CommandReader(ast.parse(_two_branch_chain(first)))
    with mock.patch.object(CommandReader, "_name_sources", counted):
        problems = reader.problems("tool.py")
    assert sorted({found for call in reader.calls for found in reader.started(call)}) == programs
    assert [problem.partition(";")[0] for problem in problems] == expected


@pytest.mark.parametrize(
    ("script", "programs", "expected"),
    [
        (
            # Two names bound to each other, over a literal.
            'import subprocess\ndef looped(flag):\n    a = "git"\n    b = a\n    while flag:\n        a = b\n'
            "    subprocess.run([a])\n",
            [(3, "git")],
            [],
        ),
        (
            # A name read from itself, over a parameter other modules may pass.
            "import subprocess\ndef looped(flag, given):\n    a = given\n    while flag:\n        a = a\n"
            "    subprocess.run([a])\n",
            [],
            ["tool.py:2 cannot read the program in parameter given, which no call in this module passes"],
        ),
        (
            # A function that returns its own call and a literal.
            'import subprocess\ndef program(flag):\n    return program(flag) if flag else "git"\n'
            "def run(flag):\n    subprocess.run([program(flag)])\n",
            [(3, "git")],
            [],
        ),
        (
            # A function that returns nothing, reached from two names: each call is named, though the function is
            # read once.
            "import subprocess\ndef program():\n    pass\ndef run(flag):\n    first = program()\n"
            "    second = program()\n    subprocess.run([first if flag else second])\n",
            [],
            [
                "tool.py:5 cannot read the program in `program()`, whose function returns none",
                "tool.py:6 cannot read the program in `program()`, whose function returns none",
            ],
        ),
        (
            # An attribute assigned to itself over a literal, read twice in one command.
            'import subprocess\nclass Tool:\n    def __init__(self):\n        self.program = "git"\n'
            "    def again(self):\n        self.program = self.program\n"
            "    def run(self, flag):\n        subprocess.run([self.program if flag else self.program])\n",
            [(4, "git")],
            [],
        ),
        (
            # An attribute assigned from another attribute, each read in one command.
            'import subprocess\nclass Tool:\n    def __init__(self):\n        self.first = "git"\n'
            "        self.second = self.first\n"
            "    def run(self, flag):\n        subprocess.run([self.first if flag else self.second])\n",
            [(4, "git")],
            [],
        ),
        (
            # A collection two loops walk, each loop's name read in one command.
            'import subprocess\nPROGRAMS = ["git", "gh"]\ndef run(flag):\n    for first in PROGRAMS:\n'
            "        for second in PROGRAMS:\n            subprocess.run([first if flag else second])\n",
            [(2, "gh"), (2, "git")],
            [],
        ),
        (
            # A parameter two call sites pass, reached through two names in one command.
            "import subprocess\ndef _run(program, flag):\n    chosen = program\n"
            "    subprocess.run([program if flag else chosen])\n"
            'def main(flag):\n    _run("git", flag)\n    _run("gh", flag)\n',
            [(6, "git"), (7, "gh")],
            [],
        ),
    ],
)
def test_command_reader_answers_names_and_functions_that_lead_back_to_themselves(
    script: str, programs: list[tuple[int, str]], expected: list[str]
) -> None:
    reader = CommandReader(ast.parse(script))
    started = [found for call in reader.calls for found in reader.started(call)]
    assert sorted(set(started)) == programs
    assert [problem.partition(";")[0] for problem in reader.problems("tool.py")] == expected
