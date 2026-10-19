"""Regression suite for tools/bash_syntax.py: Bash command text read by Bash's own grammar."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import bash_agreement

from tools import bash_syntax
from tools.bash_syntax import BashSyntaxError


def parse(text: str) -> bash_syntax.CommandList:
    """The reader's tree of text, once Bash's own syntax check has agreed with the reader on whether it reads it, so
    every fixture here, well-formed or malformed, is held to Bash."""
    bash_agreement.assert_reader_agrees([text])
    return bash_syntax.parse(text)


def simple_commands(node: object) -> list[list[str]]:
    """Every simple command in a tree, in the order the text holds them, as the raw text of its words."""
    return [[word.raw for word in command.words] for command in bash_agreement.simple_commands(node)]


def first_simple(text: str) -> bash_syntax.Simple:
    tree = parse(text)
    pipeline = tree.items[0][0].first
    command = pipeline.commands[0]
    if not isinstance(command, bash_syntax.Simple):
        raise AssertionError(f"{text!r} does not start with a simple command")
    return command


class WordTests(unittest.TestCase):
    def test_quotes_and_escapes_are_removed_and_their_text_kept_literal(self) -> None:
        command = first_simple("git commit -m 'it''s' \"a \\\"b\\\" $\" c\\ d")
        texts = ["".join(part.text for part in word.parts) for word in command.words]
        self.assertEqual(["git", "commit", "-m", "its", 'a "b" $', "c d"], texts)

    def test_a_parameter_is_kept_apart_from_the_text_around_it(self) -> None:
        command = first_simple('cd "$HUB/sub" ${TREE} $1 ~/x')
        kinds = [[part.kind for part in word.parts] for word in command.words[1:]]
        self.assertEqual([["parameter", "text"], ["parameter"], ["dynamic"], ["home", "text"]], kinds)
        self.assertEqual("HUB", command.words[1].parts[0].text)

    def test_substitutions_keep_their_command_text(self) -> None:
        command = first_simple(
            "echo $(cd /t && git commit) `git switch x` <(git show a) ${X:-$(git am)} $((1 + $(id)))"
        )
        found = [text for word in command.words for text in word.substitutions]
        self.assertEqual(["cd /t && git commit", "git switch x", "git show a", "git am", "id"], found)

    def test_a_descriptor_before_a_redirection_is_not_a_word(self) -> None:
        command = first_simple("git switch main 2>&1 >/dev/null {fd}>out")
        self.assertEqual(["git", "switch", "main"], [word.raw for word in command.words])
        self.assertEqual([">&", ">", ">"], [redirect.operator for redirect in command.redirects])

    def test_an_array_assignment_is_one_word_whose_value_only_running_settles_and_whose_substitutions_run(
        self,
    ) -> None:
        word = first_simple('a=($(git am) # a comment\n `git switch x` <(git show a) [k]="v w") git status').words[0]
        self.assertEqual((True, ["text", "dynamic"]), (word.array, [part.kind for part in word.parts]))
        self.assertEqual(("git am", "git switch x", "git show a"), word.substitutions)


class ArrayAssignmentTests(unittest.TestCase):
    def test_the_update_scripts_option_array_is_read(self) -> None:
        # skills/update-coding-agent-skills/scripts/update.sh builds the deployer's options this way.
        text = (
            "DEPLOY_OPTIONS=(--all)\n"
            'if [ -n "$RELEASE_REF" ]; then\n'
            '  DEPLOY_OPTIONS+=(--at "$RELEASE_REF")\n'
            "fi\n"
            '"$PYTHON" -B deploy.py "${DEPLOY_OPTIONS[@]}"'
        )
        self.assertEqual(
            [
                ["DEPLOY_OPTIONS=(--all)"],
                ["[", "-n", '"$RELEASE_REF"', "]"],
                ['DEPLOY_OPTIONS+=(--at "$RELEASE_REF")'],
                ['"$PYTHON"', "-B", "deploy.py", '"${DEPLOY_OPTIONS[@]}"'],
            ],
            simple_commands(parse(text)),
        )

    def test_an_array_is_read_before_a_command_and_among_a_declaration_builtins_arguments(self) -> None:
        tree = parse("a=() b+=(1) git status; x=1 declare -A m=([k]=v) && local -a l=(1) l2=(2); eval e=(1)")
        self.assertEqual(
            [
                ["a=()", "b+=(1)", "git", "status"],
                ["x=1", "declare", "-A", "m=([k]=v)"],
                ["local", "-a", "l=(1)", "l2=(2)"],
                ["eval", "e=(1)"],
            ],
            simple_commands(tree),
        )

    def test_an_array_assignment_to_a_subscript_is_read_where_bash_reads_an_array(self) -> None:
        tree = parse("a[1]=(x) b[k]+=($(git am)) git status; a[1]=(x); declare -a c[1]=(y)")
        self.assertEqual(
            [["a[1]=(x)", "b[k]+=($(git am))", "git", "status"], ["a[1]=(x)"], ["declare", "-a", "c[1]=(y)"]],
            simple_commands(tree),
        )
        words = [word for command in bash_agreement.simple_commands(tree) for word in command.words]
        self.assertEqual([True, True, False, False, True, False, False, True], [word.array for word in words])
        self.assertEqual(("git am",), words[1].substitutions)


# Each assignment before git as (name, whether it names an element, whether it appends).
@pytest.mark.parametrize(
    ("text", "assignments"),
    [
        ("a=x git push", [("a", False, False)]),
        ("a+=x git push", [("a", False, True)]),
        ("a[1]=x git push", [("a", True, False)]),
        ("a[1]+=x git push", [("a", True, True)]),
        (
            'x=1 a[1]="v w" b+=z c[k]+=w git push',
            [("x", False, False), ("a", True, False), ("b", False, True), ("c", True, True)],
        ),
        ("a[x y]=v git push", [("a", True, False)]),
        ('a["x]"]=v git push', [("a", True, False)]),
        ("a[$(echo ']')]=v git push", [("a", True, False)]),
        ("a[b[1]]=v git push", [("a", True, False)]),
        ("a[x;y|z]=v git push", [("a", True, False)]),
        ("a[]=v git push", [("a", True, False)]),
        ("a[1]=(x) git push", [("a", True, False)]),
        ("a[1]+=(x y) git push", [("a", True, True)]),
        ("x=1 >out a[x y]=v git push", [("x", False, False), ("a", True, False)]),
        ("if a[x y]=v git push; then :; fi", [("a", True, False)]),
        ("! a[x y]=v git push", [("a", True, False)]),
        ("time -p a[x y]=v git push", [("a", True, False)]),
        ("true && a[x y]=v git push", [("a", True, False)]),
        ("function f { a[x y]=v git push; }", [("a", True, False)]),
        ("f() { a[x y]=v git push; }", [("a", True, False)]),
        ("case x in x) a[y z]=1 git push;; esac", [("a", True, False)]),
    ],
)
def test_every_assignment_bash_reads_before_a_command_is_one_and_the_command_after_it_is_placed(
    text: str, assignments: list[tuple[str, bool, bool]]
) -> None:
    [command] = [command for command in bash_agreement.simple_commands(parse(text)) if command.words[-1].raw == "push"]
    read = [
        None if word.assignment is None else (word.assignment.name, word.assignment.element, word.assignment.appends)
        for word in command.words
    ]
    assert [*assignments, None, None] == read
    assert [word.raw for word in command.words[-2:]] == ["git", "push"]


def test_an_element_assignment_keeps_its_value_apart_from_its_subscript() -> None:
    [word] = first_simple('a[$(git am) "k"]="v w"').words
    assert word.assignment is not None
    assert tuple((part.kind, part.text, part.quoted) for part in word.assignment.value) == (("text", "v w", True),)
    assert word.substitutions == ("git am",)


@pytest.mark.parametrize(
    ("text", "words"),
    [
        # Where Bash reads no assignment a subscript is ordinary text, and a blank in it ends the word.
        ("echo a[x y]=v", ["echo", "a[x", "y]=v"]),
        ("x=1 echo a[1]=v", ["x=1", "echo", "a[1]=v"]),
        # Where Bash reads one it reads the subscript whole, = or not, and a word that is not one is the command's name.
        ("a[x y] echo", ["a[x y]", "echo"]),
        ("a[x]y]=v echo", ["a[x]y]=v", "echo"]),
    ],
)
def test_a_subscript_is_read_whole_only_where_bash_reads_an_assignment(text: str, words: list[str]) -> None:
    command = first_simple(text)
    assert words == [word.raw for word in command.words]


@pytest.mark.parametrize("text", ["[[ ]]", "[[ -n ]]", "[[ a && ]]"])
def test_a_documented_limit_inside_double_brackets_text_bash_refuses_is_read(text: str) -> None:
    assert not bash_agreement.bash_verdicts([text])[text][0]
    bash_syntax.parse(text)


class HereDocumentTests(unittest.TestCase):
    def test_a_here_document_body_is_data_never_commands(self) -> None:
        tree = parse("cat > run.sh <<EOF\ncd '/wt'\ngit status\nEOF\ngit add run.sh")
        self.assertEqual([["cat"], ["git", "add", "run.sh"]], simple_commands(tree))

    def test_quoted_and_tab_stripped_delimiters_close_their_bodies(self) -> None:
        tree = parse("git commit -F - <<'EOF'\nIt's $(not run)\nEOF\ncat <<-\"END\"\n\tgit reset\n\tEND\ngit status")
        self.assertEqual([["git", "commit", "-F", "-"], ["cat"], ["git", "status"]], simple_commands(tree))
        bodies = [first_simple(text).redirects[0].here for text in ("cat <<'EOF'\n$(git am)\nEOF",)]
        self.assertEqual((), bodies[0].substitutions if bodies[0] is not None else None)

    def test_an_unquoted_body_keeps_its_substitutions_which_run(self) -> None:
        here = first_simple("cat <<EOF\nmessage `git switch x` and $(git am)\nEOF").redirects[0].here
        self.assertEqual(("git switch x", "git am"), here.substitutions if here is not None else None)

    def test_a_body_the_text_never_closes_runs_to_the_end(self) -> None:
        tree = parse("cat <<EOF\ngit commit -m x")
        self.assertEqual([["cat"]], simple_commands(tree))


class TreeTests(unittest.TestCase):
    def test_a_comment_runs_to_the_end_of_its_line_but_a_hash_inside_a_word_does_not_start_one(self) -> None:
        self.assertEqual(
            [["git", "status"], ["echo", "a#b"]], simple_commands(parse("git status # git reset\necho a#b"))
        )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "if git diff --quiet; then echo clean; else git stash; fi",
            [["git", "diff", "--quiet"], ["echo", "clean"], ["git", "stash"]],
        ),
        ("{ git switch feat/demo; }", [["git", "switch", "feat/demo"]]),
        ("for f in a b; do git add $f; done", [["git", "add", "$f"]]),
        ("while true; do git commit; done", [["true"], ["git", "commit"]]),
        ("! git commit -m x", [["git", "commit", "-m", "x"]]),
        ("time -p git commit", [["git", "commit"]]),
        ("case $x in a|b) git add .;; *) echo;; esac", [["git", "add", "."], ["echo"]]),
        ("f() { cd /x; }; f", [["cd", "/x"], ["f"]]),
        ("function g { git am; }", [["git", "am"]]),
        ("[[ -n $x && -d y ]] && git commit", [["[[", "-n", "$x", "-d", "y", "]]"], ["git", "commit"]]),
    ],
)
def test_reserved_words_and_prefixes_start_compound_commands_not_programs(text: str, expected: list[list[str]]) -> None:
    assert expected == simple_commands(parse(text))


@pytest.mark.parametrize(
    "text",
    [
        "git commit -m 'unterminated",
        'echo "open',
        "echo $(git status",
        "if true; then x",
        "fi",
        "echo `x",
        "f() echo",
        "function g git status",
        "f() a=(1)",
        "echo a=(1)",
        "command declare c=(1)",
        "for i in a=(1); do :; done",
        "case a=(1) in *) ;; esac",
        "cat > a=(1)",
        "[[ a=(1) ]]",
        "a=(x | y)",
        "a=( ( 1 ) )",
        "a=(1",
        "cat <<EOF; a=(1\n2)\nbody\nEOF",
        "a[1=v git push",
        "git a[1]=(x)",
        "echo a[x y]=(1)",
    ],
)
def test_text_bash_would_refuse_is_an_error(text: str) -> None:
    with pytest.raises(BashSyntaxError):
        parse(text)
