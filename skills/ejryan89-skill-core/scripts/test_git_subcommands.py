"""The one reader of the git subcommands a skill's Bash and PowerShell scripts run (git_subcommands.py), and the
reading of a PowerShell source's commands it runs on (powershell_commands.py)."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bash_syntax
import git_subcommands
import powershell_commands
from powershell_commands import UNREAD_WORD


def no_powershell(script: str) -> Sequence[Sequence[str]]:
    raise AssertionError(f"read PowerShell it should not start: {script!r}")


def listed(scripts: dict[str, list[list[str]]], read: list[str]) -> git_subcommands.PowerShellReader:
    """A PowerShellReader that lists each script's commands from `scripts` and notes each script it is asked for."""

    def reader(script: str) -> Sequence[Sequence[str]]:
        read.append(script)
        return scripts.get(script, [])

    return reader


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('git -C "$CLONE" fetch --tags origin', {"fetch"}),
        ("OUT=$(git ls-remote --tags origin) && git push", {"ls-remote", "push"}),
        ("if git rev-parse --git-dir; then :; fi", {"rev-parse"}),
        ('echo "run git fetch first"', set()),
        ("  # git pull later", set()),
        ("legit fetch", set()),
        # Bash finds a program by its name as written, so Git is no git.
        ("Git push", set()),
        # A wrapper's options are read by its getopt table to the command it runs, and a lookup runs nothing.
        ("nice -n 5 git push", {"push"}),
        ("env -i A=1 timeout --signal TERM 5 git fetch", {"fetch"}),
        ("xargs -n 1 git push", {"push"}),
        ("env -S 'git push'", {"push"}),
        ("command -v git", set()),
        ("xargs echo git push", set()),
        ("timeout --sig TERM 5 git push", set()),
        # find's actions, eval, and a shell's -c script are read as commands of their own.
        ("find . -exec git status {} \\; -exec echo push \\;", {"status"}),
        ("eval git fetch", {"fetch"}),
        ("bash -lc 'cd x && git pull'", {"pull"}),
        # A quoted operator is a word, a substitution in a string is a command, and so is a group's or a continuation's.
        ("echo 'a; git push'", set()),
        ('echo "now $(git rev-parse HEAD)"', {"rev-parse"}),
        ("{ git push; }", {"push"}),
        ("git \\\n  fetch", {"fetch"}),
        # A subcommand no literal names is read as its word.
        ('git "$sub"', {"$sub"}),
        # Bash's time keyword takes -p and then --, each optional, before the pipeline it times; a second -p, or one
        # after --, is the timed command's own word. After a pipe, the newline right after one, an assignment, or in
        # find's action, `time` is the time program, read by its options.
        ("time -p git push", {"push"}),
        ("time -p -- git push", {"push"}),
        ("! time -p git push", {"push"}),
        ("time -p time -p git push", {"push"}),
        ("x=$(time -p git fetch)", {"fetch"}),
        ("time -p -p git push", set()),
        ("time -- -p git push", set()),
        ("echo a | time -p -p git push", {"push"}),
        ("echo a |\n time -p -p git push", {"push"}),
        ("A=1 time -p -p git push", {"push"}),
        ("find . -exec time -p -p git push \\;", {"push"}),
    ],
)
def test_a_bash_script_runs_the_git_subcommand_after_gits_own_options(line: str, expected: set[str]) -> None:
    assert git_subcommands.bash_git_subcommands(line, no_powershell) == expected


# A command starts wherever Bash starts one: at a line's start, after an operator, in a group, a subshell, or a
# substitution, in each case item after its pattern's `)`, whichever of ;; ;& ;;& or a newline ends the item before
# it, and in a function's body after its `()` or its name. A case word, a pattern, and a function's name run nothing,
# and the `)` ending a subshell or a substitution starts no command.
@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ('case "$x" in\n  up) git push ;;\nesac', {"push"}),
        ("case x in y) git push;; esac", {"push"}),
        ("case x in (y) git push;; esac", {"push"}),
        ("case x in a|b) git push;; esac", {"push"}),
        ("case x in a) git fetch;& b) git push;;& c) git pull;; esac", {"fetch", "push", "pull"}),
        ("case x in a) git fetch\n;;\nb) git push\n;;\nesac", {"fetch", "push"}),
        ("case x\nin\n a) git push;;\nesac", {"push"}),
        ("case x in a) case y in b) git push;; esac;; c) git fetch;; esac", {"push", "fetch"}),
        ("if true; then case x in a) git push;; esac; fi", {"push"}),
        ("case x in a) git push;; esac\ngit fetch", {"push", "fetch"}),
        ("case git in push) true;; esac", set()),
        ("case x in git) true;; esac", set()),
        ("f() { git push; }", {"push"}),
        ("f () { git push; }", {"push"}),
        ("function f { git push; }", {"push"}),
        ("function f() { git push; }", {"push"}),
        ("f() (git push)", {"push"}),
        ("function git { true; }", set()),
        ("(git push)", {"push"}),
        ("(cd x) && git push", {"push"}),
        ('x="$(date) git push"', set()),
        # A backquote closes the substitution it opened, so the text after the closing one is read by where it stands:
        # a case word's `in`, a pattern, or the rest of the command the substitution stands in.
        ("case `uname` in\n  Linux) git push ;;\nesac", {"push"}),
        ("case `uname` in Linux) git push;; esac", {"push"}),
        ("case `uname\n` in Linux) git push;; esac", {"push"}),
        ("case `echo $(uname)` in Linux) git push;; esac", {"push"}),
        ("echo `date` git push", set()),
        # A substitution in a quoted case word leaves the rest of that word before its `in`.
        ('case "`uname`" in Linux) git push;; esac', {"push"}),
        ('case "$(uname)" in Linux) git push;; esac', {"push"}),
        ('case "a$(uname)b" in Linux) git push;; esac', {"push"}),
    ],
)
def test_a_git_command_is_read_wherever_bash_starts_a_command(script: str, expected: set[str]) -> None:
    assert git_subcommands.bash_git_subcommands(script, no_powershell) == expected


# A here-document's body is data Bash hands the command, never commands of its own: the lines after the line that
# opens it, up to the line that is its delimiter, with its leading tabs removed after <<-, read for each document the
# line opens in turn. Bash expands the command substitutions of an unquoted body, so those are read; a delimiter with
# any part quoted or escaped leaves the body as written. A << inside quotes, a comment, or arithmetic opens nothing,
# and neither does a <<< here-string.
@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("cat <<EOF\ngit push\nEOF\n", set()),
        ("cat <<EOF\ngit push\nEOF\ngit fetch\n", {"fetch"}),
        ("cat << EOF\ngit push\nEOF\ngit fetch", {"fetch"}),
        ("cat<<EOF\ngit push\nEOF\ngit fetch", {"fetch"}),
        ("cat 2<<EOF\ngit push\nEOF\ngit fetch", {"fetch"}),
        ("cat <<-EOF\n\tgit push\n\t\tEOF\ngit fetch", {"fetch"}),
        ("cat <<-EOF\n git push\n  EOF\nEOF\ngit fetch", {"fetch"}),
        ("cat <<EOF\n\tgit push\n\tEOF\nEOF\ngit fetch", {"fetch"}),
        ("cat <<'EOF'\ngit push\nEOF\ngit fetch", {"fetch"}),
        ('cat <<"EOF"\ngit push\nEOF\ngit fetch', {"fetch"}),
        ("cat <<\\EOF\ngit push\nEOF\ngit fetch", {"fetch"}),
        ("cat <<E'O'F\ngit push\nEOF\ngit fetch", {"fetch"}),
        ("cat <<$'EOF'\ngit push\nEOF\ngit fetch", {"fetch"}),
        ("cat <<EOF; git fetch\ngit push\nEOF", {"fetch"}),
        ("cat <<EOF | git fetch\ngit push\nEOF", {"fetch"}),
        ("cat <<A <<B\ngit push\nA\ngit pull\nB\ngit fetch", {"fetch"}),
        ("x=$(cat <<EOF\ngit push\nEOF\n)\ngit fetch", {"fetch"}),
        ("cat <<EOF\nEOF2\n EOF\ngit push\nEOF\ngit fetch", {"fetch"}),
        # An unquoted body joins a line its backslash continues, so a delimiter after one is body text.
        ("cat <<EOF\nfoo \\\nEOF\ngit push\nEOF\ngit fetch", {"fetch"}),
        ("cat <<'EOF'\nfoo \\\nEOF\ngit fetch", {"fetch"}),
        # An unquoted body's substitutions run; a quoted body's are text.
        ("cat <<EOF\nnow $(git rev-parse HEAD)\nEOF", {"rev-parse"}),
        ("cat <<EOF\nit's $(git rev-parse HEAD)\nEOF", {"rev-parse"}),
        ("cat <<EOF\n'$(git log -1)'\nEOF", {"log"}),
        ("cat <<EOF\n`git describe`\nEOF", {"describe"}),
        ("cat <<EOF\n$(git \\\n push)\nEOF", {"push"}),
        ('cat <<EOF\n$(echo ")"; git push)\nEOF', {"push"}),
        ("cat <<EOF\n$(cat <<IN\ngit fetch\nIN\ngit push)\nEOF", {"push"}),
        ("cat <<EOF\n\\$(git push) $((1 + 2))\nEOF", set()),
        ("cat <<'EOF'\n$(git push) `git fetch`\nEOF", set()),
        ('cat <<-"EOF"\n\t$(git push)\n\tEOF', set()),
        # No here-document opens.
        ("cat <<<EOF\ngit push", {"push"}),
        ("echo '<<EOF'\ngit push", {"push"}),
        ('echo "<<EOF"\ngit push', {"push"}),
        ("echo \\<<EOF\ngit push", {"push"}),
        ("echo hi # <<EOF\ngit push", {"push"}),
        ("echo $((1 << 2))\ngit push", {"push"}),
        ("(( x = 1 << 2 ))\ngit push", {"push"}),
        # A body the text never closes runs to the text's end, as Bash reads it. A delimiter line ending in a CR closes
        # the body, as Git Bash reads it, so the git command after it, which Git Bash runs, is read.
        ("cat <<EOF\ngit push", set()),
        ("cat <<EOF\ngit push\nEOF\r\ngit fetch", {"fetch"}),
    ],
)
def test_a_here_document_s_body_is_read_as_data(script: str, expected: set[str]) -> None:
    assert git_subcommands.bash_git_subcommands(script, no_powershell) == expected


# Bash ends a line at LF alone, and its blanks are the space and the tab, so a line separator, a form feed, or another
# character str.splitlines() breaks at stays inside its line, a comment's included.
@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ('echo "a\u2028#"; git push origin main', {"push"}),
        ('echo "a\x85#"; git push origin main', {"push"}),
        ("# note\x0cgit push origin main", set()),
        ("# note\u2029git push origin main", set()),
        ("# note\x1cgit push origin main", set()),
        ("\x0c# x; git push origin main", {"push"}),
        ("\u2028# x; git push origin main", {"push"}),
        # A backslash continues a line only where it escapes the LF, never where another backslash escapes it.
        ("echo \\\\\ngit push", {"push"}),
        ("echo \\\\\\\n  git push", set()),
        # A comment ends at the LF, so a backslash inside one continues nothing.
        ("# note \\\ngit push", {"push"}),
        ("  # note \\\ngit push", {"push"}),
        ("echo a # note \\\ngit push", {"push"}),
        ("echo 'a # b' \\\n  git push", set()),
    ],
)
def test_a_bash_line_ends_at_lf_alone(script: str, expected: set[str]) -> None:
    assert git_subcommands.bash_git_subcommands(script, no_powershell) == expected


# Each step of the grammar is held where reading one character too far, too short, or not at all changes what Bash
# runs: a here-document that opens, or does not, and closes on a later line; a substitution's
# end, after which body text is data; and a continued or commented line.
@pytest.mark.parametrize(
    ("script", "expected"),
    [
        # A whole-line comment is one only after spaces and tabs, read from the line's start.
        ("  # note; git push", set()),
        ("X# note; git push", {"push"}),
        # A `#` inside a word, and a `<<` inside a quote, after an escape, or in arithmetic, open nothing, so the line
        # after the body stands as a command.
        ("echo X#<<EOF\ngit push\nEOF", set()),
        ('echo "<<EOF"\ngit push\nEOF', {"push"}),
        ("echo '<<EOF'\ngit push\nEOF", {"push"}),
        ("echo \\<<EOF\ngit push\nEOF", {"push"}),
        ("echo $((1 << 2))\ngit push\n2", {"push"}),
        ("(( x = 1 << 2 ))\ngit push\n2", {"push"}),
        ("x=(a); echo $((1 << 2))\ngit push\n2", {"push"}),
        # Each quote, escape, and arithmetic ends where Bash ends it, so a `<<` right after it opens a here-document.
        ("cat \\a<<EOF\ngit push\nEOF", set()),
        ("echo 'a' <<EOF 'b'\ngit push\nEOF", set()),
        ("echo ''<<EOF\ngit push\nEOF", set()),
        ("echo 'a'<<EOF\ngit push\nEOF", set()),
        ('echo "a" <<EOF\ngit push\nEOF', set()),
        ("echo $((1))<<EOF\ngit push\nEOF", set()),
        # `$((` is one step from its `$`, so the `#` right after its `))` goes on the word and starts no comment.
        ("echo $((1))#x <<EOF\ngit push\nEOF", set()),
        ("cat <<<EOF\ngit push\nEOF", {"push"}),
        # The delimiter's quoting and escapes, and `<<-`'s tabs, decide where the body ends and whether it expands.
        ("cat <<XEOF\n$(git push)\nXEOF", {"push"}),
        ('cat <<"E\\$F"\ngit push\nE$F\ngit fetch', {"fetch"}),
        ("cat <<EOF\n\tEOF\ngit push\nEOF", set()),
        ("cat <<-XEOF\nEOF\ngit push\nXEOF", set()),
        # A body's substitution ends at its own `)` or backquote, past quotes, escapes, and nested pairs, so the text
        # after it is data; one never closed runs to the body's end.
        ("cat <<EOF\n$(echo a); git push\nEOF", set()),
        ("cat <<EOF\n$(echo \\) ; git push)\nEOF", {"push"}),
        ("cat <<EOF\n$(echo ')' ; git push)\nEOF", {"push"}),
        ("cat <<EOF\n$(echo ')'); echo 'x'; git push\nEOF", set()),
        ('cat <<EOF\n$(echo ")"); git push\nEOF', set()),
        ("cat <<EOF\n$(); git push; $(git fetch)\nEOF", {"fetch"}),
        ("cat <<EOF\n\\a$(git push)\nEOF", {"push"}),
        ("cat <<EOF\n`echo a`; git push\nEOF", set()),
        ("cat <<EOF\n`ech`; git push\nEOF", set()),
        ("cat <<EOF\n``; git push; `git fetch`\nEOF", {"fetch"}),
        ("cat <<EOF\n`echo a`$(git push)\nEOF", {"push"}),
        # A continued line joins the next one in place of its backslash, and the line after the joined one is read.
        ("git push \\", {"push"}),
        ("git fetch\\\n origin", {"fetch"}),
        ("git push \\\norigin\ngit fetch", {"push", "fetch"}),
        ("cat <<EOF\n$(git fetch\\\n)\nEOF", {"fetch"}),
        ("cat <<EOF\na \\\nb\n$(git fetch)\nEOF", {"fetch"}),
        ("cat <<EOF\nfoo \\", set()),
        # A line ending in an empty command hands nothing on, so `time` on the next line is still the keyword.
        ("true;\ntime -p -p git push", set()),
        # A body's substitutions add to what the script's commands run.
        ("git fetch\ncat <<EOF\n$(echo a)\nEOF", {"fetch"}),
    ],
)
def test_each_step_of_the_grammar_ends_where_bash_ends_it(script: str, expected: set[str]) -> None:
    assert git_subcommands.bash_git_subcommands(script, no_powershell) == expected


# Bash runs none of a script it refuses, so a reader that guessed at one could miss a git command; the grammar refuses
# each, and so does the reader, for its caller to fail.
@pytest.mark.parametrize(
    "script",
    [
        "if git rev-parse --git-dir; then",
        "x=1 git push 'unclosed",
        "cat << \ngit push",
        "eval 'git push \"'",
        "bash -c 'if git push; then'",
    ],
)
def test_a_script_the_grammar_refuses_raises_its_error(script: str) -> None:
    with pytest.raises(bash_syntax.BashSyntaxError):
        git_subcommands.bash_git_subcommands(script, no_powershell)


# Bash expands a here-document's body in order and stops at a substitution it cannot read, running none after it and
# not the command the body is for, while the script goes on, so such a substitution runs nothing and the git commands
# before it, in the body and around the command, are read.
@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("git fetch\ncat <<EOF\n$(git push\nEOF\ngit pull", {"fetch", "pull"}),
        ("cat <<EOF\n`git push\nEOF\ngit pull", {"pull"}),
        ("cat <<EOF\n`echo \\` ; git push`\nEOF", set()),
        ('cat <<EOF\n$(git log) $(echo "a) $(git push)\nEOF\ngit pull', {"log", "pull"}),
        ("cat <<EOF\n$(git log) $(if) $(git push)\nEOF", {"log"}),
    ],
)
def test_a_body_s_substitution_bash_cannot_read_runs_nothing(script: str, expected: set[str]) -> None:
    assert git_subcommands.bash_git_subcommands(script, no_powershell) == expected


def test_a_body_s_substitution_reads_a_powershell_start_up_through_the_caller_s_reader() -> None:
    read: list[str] = []
    reader = listed({"git push": [["git", "push"]]}, read)
    assert git_subcommands.bash_git_subcommands("cat <<EOF\n$(pwsh -c 'git push')\nEOF", reader) == {"push"}
    assert read == ["git push"]


# Bash runs the program a path names, and Git Bash the one named with .exe, so git named either way is git, as is a
# shell whose -c script runs it; a name is matched as written, so Git and GIT.exe are no git in Bash.
@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("/usr/bin/git push", {"push"}),
        ("git.exe push origin", {"push"}),
        ("'/c/Program Files/Git/cmd/git.exe' fetch", {"fetch"}),
        ('"$HOME/bin/git" push', {"push"}),
        ('/bin/bash -c "git push"', {"push"}),
        ("bash.exe -c 'git push'", {"push"}),
        ("/usr/bin/env git push", {"push"}),
        ("env /usr/bin/git push", {"push"}),
        ("/usr/bin/find . -exec /usr/bin/git push {} +", {"push"}),
        ("Git push", set()),
        ("/usr/bin/Git push", set()),
        ("GIT.exe push", set()),
        ("legit.exe push", set()),
        ("/usr/bin/git/ push", set()),
    ],
)
def test_git_or_a_shell_named_by_a_path_or_with_exe_is_read_as_bash_runs_it(script: str, expected: set[str]) -> None:
    assert git_subcommands.bash_git_subcommands(script, no_powershell) == expected


# Bash recognizes a reserved word, time and its -p, and function only spelled with no quote or escape, and an
# assignment only with its name and `=` unquoted: `'if' git push` runs a program named `if`, never git, while a
# quoted value leaves an assignment one, and a quoted `time` is the time program, which runs git. A quoted case word
# starts no case, so the git command after it is read.
@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("if git push; then :; fi", {"push"}),
        ("'if' git push", set()),
        ('"{" git push', set()),
        ("\\! git push", set()),
        ("'while' git push", set()),
        ("th'en' git push", set()),
        ("'function' f git push", set()),
        ('"A=1" git push', set()),
        ("A='1 2' git push", {"push"}),
        ('A="x" B=y git push', {"push"}),
        ("'time' git push", {"push"}),
        ("time '-p' git push", set()),
        ("! { git fetch; }", {"fetch"}),
        ("'case' x in; git push", {"push"}),
        ("\\git push", {"push"}),
        ("'git' push", {"push"}),
    ],
)
def test_a_quoted_or_escaped_keyword_or_assignment_is_read_as_the_program(script: str, expected: set[str]) -> None:
    assert git_subcommands.bash_git_subcommands(script, no_powershell) == expected


# Every wrapper of command_wrappers' catalogue runs the git command after it, sudo and setsid as env and nohup do.
@pytest.mark.parametrize(
    "script",
    [
        "sudo git push",
        "sudo -u me A=1 git push",
        "doas -u me git push",
        "setsid -w git push",
        "ionice -c 2 git push",
        "chrt -f 10 git push",
        "taskset -c 0 git push",
        "chroot / git push",
        "unbuffer -p git push",
        "chronic -e git push",
        "strace -f -o log git push",
        "ltrace -o log git push",
    ],
)
def test_the_git_command_a_wrapper_runs_is_read(script: str) -> None:
    assert git_subcommands.bash_git_subcommands(script, no_powershell) == {"push"}


def test_a_command_s_words_are_read_beside_their_spellings() -> None:
    assert git_subcommands.spelled_words("'if' A=\"1 2\" \\! git") == [
        ("if", "'if'"),
        ("A=1 2", 'A="1 2"'),
        ("!", "\\!"),
        ("git", "git"),
    ]
    assert git_subcommands.spelled_words("git 'push") == [("git", "git"), ("push", "'push")]
    assert git_subcommands.spelled_words("") == []


# The script a pwsh or Windows PowerShell start-up runs is PowerShell, whose commands the caller's reader lists: by
# -Command, -CommandWithArgs, or Windows PowerShell's first positional word, named by a path or with .exe too, and
# behind a wrapper or in find's action.
@pytest.mark.parametrize(
    ("line", "script"),
    [
        ("pwsh -c 'git push'", "git push"),
        ("pwsh -NoProfile -Command git push", "git push"),
        ("pwsh -CommandWithArgs 'git push' extra", "git push"),
        ("powershell 'git push'", "git push"),
        ("pwsh.exe -c 'git push'", "git push"),
        ("/usr/bin/pwsh -c 'git push'", "git push"),
        ("env pwsh -c 'git push'", "git push"),
        ("find . -exec pwsh -c 'git push' \\;", "git push"),
        ("x=$(pwsh -c 'git push')", "git push"),
    ],
)
def test_the_script_a_powershell_start_up_runs_is_read_through_the_powershell_reader(line: str, script: str) -> None:
    read: list[str] = []
    reader = listed({script: [["git", "push"]]}, read)
    assert git_subcommands.bash_git_subcommands(line, reader) == {"push"}
    assert read == [script]


# A start-up that runs no script, a file, an encoded script, one read from standard input, or one a word that is no
# literal holds, gives the reader nothing to read.
@pytest.mark.parametrize(
    "words",
    [
        ["pwsh"],
        ["pwsh", "-File", "push.ps1"],
        ["pwsh", "push.ps1", "git", "push"],
        ["pwsh", "-EncodedCommand", "ZwBpAHQAIABwAHUAcwBoAA=="],
        ["pwsh", "-Command", "-"],
        ["pwsh", "-Command", UNREAD_WORD],
        ["pwsh", "-Command", "git", UNREAD_WORD],
        ["pwsh", UNREAD_WORD, "-Command", "git push"],
    ],
)
def test_a_start_up_whose_script_no_reader_sees_gives_no_subcommand(words: list[str]) -> None:
    assert git_subcommands.powershell_git_subcommands([words], no_powershell) == set()


# A PowerShell script's commands, as powershell_commands lists them, are read through the same wrappers, find's
# actions, shell -c scripts, and start-ups as a Bash script's; a word that is no literal is UNREAD_WORD.
@pytest.mark.parametrize(
    ("commands", "expected"),
    [
        ([["git", "push"]], {"push"}),
        ([["git", "-C", UNREAD_WORD, "fetch", "origin"]], {"fetch"}),
        ([["git", UNREAD_WORD]], {UNREAD_WORD}),
        ([["env", "git", "push"]], {"push"}),
        ([["find", ".", "-exec", "git", "push", "{}", "+"]], {"push"}),
        ([["bash", "-c", "git push"]], {"push"}),
        ([["command", "-v", "git", "push"]], set()),
        ([["time", "-p", "-p", "git", "push"]], {"push"}),
        ([["echo", "git", "push"]], set()),
        # PowerShell finds the program in any case, so Git and GIT, .exe already dropped, run git.
        ([["Git", "push"]], {"push"}),
        ([["GIT", "-C", "dir", "fetch"]], {"fetch"}),
        ([[UNREAD_WORD, "push"]], set()),
        # A program named by a path, either slash, or with .exe in any case is the program its file name names.
        ([["C:/Program Files/Git/cmd/git", "push"]], {"push"}),
        ([["C:\\Program Files\\Git\\cmd\\GIT.EXE", "fetch"]], {"fetch"}),
        ([["/usr/bin/git", "push"]], {"push"}),
        ([["C:/Program Files/Git/bin/bash.exe", "-c", "git push"]], {"push"}),
        ([["/usr/bin/env", "git", "push"]], {"push"}),
        ([["C:/tools/legit.exe", "push"]], set()),
    ],
)
def test_a_powershell_scripts_commands_give_the_git_subcommands_they_run(
    commands: list[list[str]], expected: set[str]
) -> None:
    assert git_subcommands.powershell_git_subcommands(commands, no_powershell) == expected


def test_a_start_up_inside_a_started_script_is_read_in_turn() -> None:
    read: list[str] = []
    reader = listed({"pwsh -c 'git fetch'": [["pwsh", "-c", "git fetch"]], "git fetch": [["git", "fetch"]]}, read)
    assert git_subcommands.powershell_git_subcommands([["pwsh", "-c", "pwsh -c 'git fetch'"]], reader) == {"fetch"}
    assert read == ["pwsh -c 'git fetch'", "git fetch"]


# What COMMANDS_RUN lists for one command becomes its words: the name without .exe, its literal words, UNREAD_WORD for
# each other, and the program Start-Process starts given its argument list's words.
@pytest.mark.parametrize(
    ("item", "words"),
    [
        ({"source": 0, "name": "git", "words": ["push", None]}, ["git", "push", UNREAD_WORD]),
        ({"source": 0, "name": "git.EXE", "words": ["fetch"]}, ["git", "fetch"]),
        ({"source": 0, "name": "git", "arguments": ["-C dir", "push"]}, ["git", "-C", "dir", "push"]),
        ({"source": 0, "name": "git", "arguments": ["push", None]}, ["git", "push", UNREAD_WORD]),
        ({"source": 0, "unread": "& $tool"}, None),
    ],
)
def test_a_listed_command_becomes_its_words(item: dict[str, object], words: list[str] | None) -> None:
    assert powershell_commands.command_words(item) == words


# pwsh's own parser lists what a PowerShell source runs, Start-Process's program included, for every source in one
# call; the cmdlets PowerShell ships are its own, not external commands.
def test_pwsh_lists_the_external_commands_each_source_runs() -> None:
    commands = powershell_commands.read(
        [
            "git push origin\nWrite-Output done",
            "Start-Process git.exe -ArgumentList 'fetch', $remote",
            "function git { }\ngit push",
            "",
        ]
    )
    assert commands == [[["git", "push", "origin"]], [["git", "fetch", UNREAD_WORD]], [], []]
    assert powershell_commands.read([]) == []


def test_a_git_push_a_start_up_runs_in_a_powershell_script_is_read_through_pwsh() -> None:
    def reader(script: str) -> Sequence[Sequence[str]]:
        return powershell_commands.read([script])[0]

    (commands,) = powershell_commands.read(["env pwsh -c 'Start-Process git -ArgumentList push'"])
    assert git_subcommands.powershell_git_subcommands(commands, reader) == {"push"}


def test_pwsh_that_cannot_be_found_or_cannot_list_the_commands_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powershell_commands, "find_executable", lambda name: None)
    with pytest.raises(powershell_commands.PowerShellUnread, match="pwsh was not found"):
        powershell_commands.read(["git push"])


def test_pwsh_s_last_line_is_split_on_newlines_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    # A word holding U+0085 or U+2028, which a JSON string may carry unescaped, stays in the one line pwsh printed.
    listing = '[{"source": 0, "name": "git", "words": ["commit", "-m", "a\u0085b\u2028c"]}]'
    stdout = f"warming up\r\n{listing}\r\n\r\n".encode()
    monkeypatch.setattr(powershell_commands, "find_executable", lambda name: name)
    monkeypatch.setattr(
        powershell_commands, "run_bounded", lambda *_, **__: subprocess.CompletedProcess([], 0, stdout, b"")
    )
    assert powershell_commands.read(["git commit"]) == [[["git", "commit", "-m", "a\u0085b\u2028c"]]]


def test_a_source_json_cannot_write_is_refused_before_pwsh_starts() -> None:
    with pytest.raises(powershell_commands.PowerShellUnread, match="cannot be handed to pwsh"):
        powershell_commands.read(["git push \ud800"])


def test_pwsh_that_prints_no_list_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powershell_commands, "COMMANDS_RUN", "[Console]::Out.WriteLine('not json'); exit 3")
    with pytest.raises(powershell_commands.PowerShellUnread, match="exit code 3"):
        powershell_commands.read(["git push"])
    monkeypatch.setattr(powershell_commands, "COMMANDS_RUN", "[Console]::Out.WriteLine('{}')")
    with pytest.raises(
        powershell_commands.PowerShellUnread, match=r"it exited 0 and its last line is not a list of commands: \{\}$"
    ):
        powershell_commands.read(["git push"])


def test_pwsh_that_does_not_finish_in_time_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powershell_commands, "COMMANDS_RUN", "Start-Sleep -Seconds 30")
    with pytest.raises(powershell_commands.PowerShellUnread, match="did not finish within 1 seconds"):
        powershell_commands.read(["git push"], timeout=1)
