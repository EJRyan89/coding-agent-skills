"""Regression suite for ejryan89-skill-core's bash_syntax.py: Bash command text read by Bash's own grammar."""

from __future__ import annotations

import functools
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import bash_agreement
import bash_syntax
from bash_syntax import BashSyntaxError

from deployer import platform_support


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

    def test_a_substitution_says_when_a_case_pattern_no_paren_opened_closes_in_it(self) -> None:
        # Bash 3.2 ends the substitution at such a pattern's `)`, so a reader can tell the Bashes apart there; a
        # pattern opened with `(`, a backquote, which Bash 3.2 reads as Bash 4 does, and a case outside any
        # substitution are no such text.
        texts = {
            "x=$(case a in a) b;; esac)": True,
            "x=$( (case a in b|a) c;; esac) )": True,
            "x=$(echo $(case a in a) b;; esac))": False,
            "x=$(case a in (a) b;; (c) d;; esac)": False,
            "x=`case a in a) b;; esac`": False,
            "case a in a) x=$(b);; esac": False,
        }
        for text, bare in texts.items():
            with self.subTest(text=text):
                word = first_simple(text).words[0] if not text.startswith("case") else None
                sources = list(word.substitutions) if word is not None else []
                self.assertEqual(bare, any(getattr(source, "bare_case", False) for source in sources))
        inner = first_simple("x=$(echo $(case a in a) b;; esac))").words[0].substitutions[0]
        self.assertTrue(getattr(first_simple(inner).words[1].substitutions[0], "bare_case", False))

    def test_a_refusal_says_when_a_case_pattern_no_paren_opened_ended_the_substitution_for_bash_3_2(self) -> None:
        # Bash 4 and later refuse this, the `#` taking the substitution's close; Bash 3.2 ends the substitution at `a)`
        # and runs the git call.
        with self.assertRaises(BashSyntaxError) as raised:
            parse("x=$(case x in a) ; git commit -m y #;; esac)")
        self.assertTrue(raised.exception.bare_case)
        with self.assertRaises(BashSyntaxError) as raised:
            parse("x=$(echo ')")
        self.assertFalse(raised.exception.bare_case)

    def test_a_here_document_s_unclosed_expansion_says_when_its_pattern_no_paren_opened(self) -> None:
        bare = bash_syntax.here_documents(first_simple("cat <<EOF\n$(case x in a) $(git push) #;; esac)\nEOF"))[0]
        self.assertEqual((True, True), (bare.unclosed, bare.bare_case))
        other = bash_syntax.here_documents(first_simple('cat <<EOF\n$(echo "a)\nEOF'))[0]
        self.assertEqual((True, False), (other.unclosed, other.bare_case))

    def test_a_refusal_and_a_body_say_when_a_substitution_at_any_depth_held_a_bare_case_pattern(self) -> None:
        # The inner substitution reads, its pattern's `)` no `(` opened, and the one around it is then refused.
        with self.assertRaises(BashSyntaxError) as raised:
            parse("echo $(echo $(case x in [)]) $(git push) #;; esac))")
        self.assertTrue(raised.exception.holds_bare_case)
        with self.assertRaises(BashSyntaxError) as raised:
            parse("echo $(echo $(case x in (a) :;; esac) #)")
        self.assertFalse(raised.exception.holds_bare_case)
        body = bash_syntax.here_documents(first_simple("cat <<EOF\n$(cat <(case x in x) ; :  #;; esac))\n)\nEOF"))[0]
        self.assertTrue(body.holds_bare_case)

    def test_parse_reads_a_case_pattern_s_paren_in_a_substitution_as_bash_3_2_does_when_asked(self) -> None:
        # Bash 3.2 ends the substitution at `a)`, so the git call after it stands in the command around it.
        text = "echo $(case x in a) $(git push) #;; esac)"
        with self.assertRaises(BashSyntaxError):
            bash_syntax.parse(text)
        command = bash_syntax.parse(text, bash_3_2=True).items[0][0].first.commands[0]
        assert isinstance(command, bash_syntax.Simple)
        self.assertEqual(
            ("case x in a", "git push"), tuple(text for word in command.words for text in word.substitutions)
        )

    def test_each_substitution_keeps_the_form_that_opened_it(self) -> None:
        command = first_simple('echo $(a) `b` <(c) >(d) "$(e)"')
        forms = [getattr(text, "form", None) for word in command.words for text in word.substitutions]
        self.assertEqual(["$(", "`", "<(", ">(", "$("], forms)

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
        ("2>/dev/null a[x y]=v git push", [("a", True, False)]),
        ("2>&1 >out x=1 a[x y]=v git push", [("x", False, False), ("a", True, False)]),
        ("x=1 2>/dev/null y=2 git push", [("x", False, False), ("y", False, False)]),
        ("if a[x y]=v git push; then :; fi", [("a", True, False)]),
        ("! a[x y]=v git push", [("a", True, False)]),
        ("time -p a[x y]=v git push", [("a", True, False)]),
        ("time -- a[x y]=v git push", [("a", True, False)]),
        ("time -p -- a[x y]=v git push", [("a", True, False)]),
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
        # A redirection keeps the place only while it is all the command holds, so after an assignment it ends it.
        ("x=1 >out a[x y]=v echo", ["x=1", "a[x", "y]=v", "echo"]),
        ("2>&1 x=1 >out a[x y]=v echo", ["x=1", "a[x", "y]=v", "echo"]),
        # A reserved word is one only as a command's first word, so after an assignment or a redirection it is the
        # command's name and the words after it its arguments.
        ("x=1 time a[x y]=v", ["x=1", "time", "a[x", "y]=v"]),
        ("x=1 ! a[x y]=v", ["x=1", "!", "a[x", "y]=v"]),
        (">out time -p a[x y]=v", ["time", "-p", "a[x", "y]=v"]),
        # The time keyword's options are read once each and in order, so a word after them is the command's name.
        ("time -- -- a[x y]=v", ["--", "a[x", "y]=v"]),
        ("time -- -p a[x y]=v", ["-p", "a[x", "y]=v"]),
        ("time -p -p a[x y]=v", ["-p", "a[x", "y]=v"]),
        ("time -p -- -- a[x y]=v", ["--", "a[x", "y]=v"]),
        ("2>&1 if a[x y]=v", ["if", "a[x", "y]=v"]),
    ],
)
def test_a_subscript_is_read_whole_only_where_bash_reads_an_assignment(text: str, words: list[str]) -> None:
    command = first_simple(text)
    assert words == [word.raw for word in command.words]


# Each redirection Bash reads before a command's name, alone, among the assignments before it, and after a reserved
# word, with the command written with the redirection after its name.
@pytest.mark.parametrize(
    ("text", "after"),
    [
        (">out git push", "git push >out"),
        ("2>/dev/null git push", "git push 2>/dev/null"),
        ("<in git push", "git push <in"),
        (">>out git push", "git push >>out"),
        ("&>out git push", "git push &>out"),
        ("&>>out git push", "git push &>>out"),
        ("<<<'a b' git push", "git push <<<'a b'"),
        ("2>&1 git push", "git push 2>&1"),
        (">&2 git push", "git push >&2"),
        ("3<&- git push", "git push 3<&-"),
        ("<>file git push", "git push <>file"),
        (">|out git push", "git push >|out"),
        ("{fd}>out git push", "git push {fd}>out"),
        ("<<EOF git push\n$(git am)\nEOF", "git push <<EOF\n$(git am)\nEOF"),
        ("<<-'EOF' git push\n\tbody\n\tEOF", "git push <<-'EOF'\n\tbody\n\tEOF"),
        ('2>/dev/null >"a b" <in git push', 'git push 2>/dev/null >"a b" <in'),
        ("2>/dev/null x=1 git push", "x=1 git push 2>/dev/null"),
        ("x=1 2>/dev/null git push", "x=1 git push 2>/dev/null"),
        ("x=1 2>/dev/null y=2 >out git push", "x=1 y=2 git push 2>/dev/null >out"),
        ("2>/dev/null a[x y]=v b=(1) git push", "a[x y]=v b=(1) git push 2>/dev/null"),
        (">$(git am) git push", "git push >$(git am)"),
        ("! 2>/dev/null git push", "! git push 2>/dev/null"),
        ("time 2>/dev/null git push", "time git push 2>/dev/null"),
        ("true && 2>/dev/null git push", "true && git push 2>/dev/null"),
        ("echo | 2>/dev/null git push", "echo | git push 2>/dev/null"),
        ("if 2>/dev/null git push; then :; fi", "if git push 2>/dev/null; then :; fi"),
        ("f() { 2>/dev/null git push; }", "f() { git push 2>/dev/null; }"),
        ("2>/dev/null declare -a a=(1)", "declare -a a=(1) 2>/dev/null"),
    ],
)
def test_a_redirection_before_a_commands_name_builds_the_command_it_builds_after_it(text: str, after: str) -> None:
    def read(text: str) -> list[tuple[object, ...]]:
        return [
            (
                [(word.raw, word.assignment, word.array, word.substitutions) for word in command.words],
                [(redirect.operator, redirect.target, redirect.here) for redirect in command.redirects],
            )
            for command in bash_agreement.simple_commands(parse(text))
        ]

    assert read(after) == read(text)


@pytest.mark.parametrize("text", [">out", ">out | git push", "2>/dev/null; git push", "x=1 >out"])
def test_a_command_of_redirections_and_assignments_alone_has_no_name(text: str) -> None:
    command = first_simple(text)
    assert [word.raw for word in command.words if word.assignment is None] == []
    assert len(command.redirects) == 1


@pytest.mark.parametrize("text", ["[[ ]]", "[[ -n ]]", "[[ a && ]]"])
def test_a_documented_limit_inside_double_brackets_text_bash_refuses_is_read(text: str) -> None:
    assert not bash_agreement.bash_verdicts([text])[text][0]
    bash_syntax.parse(text)


def test_a_documented_limit_a_cr_outside_quotes_is_read_as_a_blank() -> None:
    assert simple_commands(parse("git push\r\na\rb")) == [["git", "push"], ["a", "b"]]


# Deeper than the reader follows under Python's default recursion limit, and shallow enough for the Bash that checks
# every fixture, which reads each text through `eval` and overflows its own stack a little deeper.
DEPTH = 300


@pytest.mark.parametrize(
    "text",
    [
        "$( " * DEPTH + "git commit" + " )" * DEPTH,
        "( " * DEPTH + "git commit" + " )" * DEPTH,
        "{ " * DEPTH + "git commit;" + " }" * DEPTH,
    ],
)
def test_a_documented_limit_text_nested_past_the_recursion_limit_which_bash_reads_is_an_error(text: str) -> None:
    assert bash_agreement.bash_verdicts([text])[text][0]
    with pytest.raises(bash_syntax.NotRead, match=r"^it nests deeper than the reader follows$"):
        bash_syntax.parse(text)


# Texts where reading a file and evaluating a string could part: an empty text, a comment, an unterminated
# here-document, quote, or compound command, a stray closer, an exit before an error, an alias, and extended globs.
EDGE_TEXTS = [
    "",
    "# only a comment",
    "}",
    "{",
    "cat <<EOF\nno end",
    "cat <<-EOF\n\tbody\n\tEOF",
    "echo 'open",
    'echo "open',
    "exit 3\necho )",
    "return 5",
    "case x in",
    "if true; then",
    "fi",
    "x=(1 2",
    "echo \\",
    "a |",
    "@(a|b)",
    "[[ a == @(a|b) ]]",
    "echo $((1+))",
    "echo ${x",
    "alias e=echo\ne )",
    "trap 'x' EXIT\n(",
    "echo $(",
    "echo `",
    "git status\n",
]


def test_one_bash_reading_every_text_gives_the_verdicts_bash_n_gives_each_as_a_file() -> None:
    batched = bash_agreement.bash_verdicts(EDGE_TEXTS)
    verdicts = {text: bash_agreement.bash_n_verdict(text)[0] for text in EDGE_TEXTS}
    assert {text: read for text, (read, _) in batched.items()} == verdicts
    assert {True, False} == set(verdicts.values())


# Ends the subshell reading a text that holds "ended" with exit 0 before its reading finishes, as an MSYS Bash reads a
# subshell Windows ends, and reads every other text as before.
ENDED_SUBSHELL = 'eval() { case $1 in *ended*) exit 0 ;; esac; builtin eval "$1"; }\n'


def test_a_text_whose_batch_reading_ends_takes_the_verdict_of_its_own_bash_n(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bash_agreement, "READ_EACH", ENDED_SUBSHELL + bash_agreement.READ_EACH)
    texts = ["echo the batch reads (", "echo the batch ended )", "echo the batch ended too"]
    verdicts = bash_agreement.bash_verdicts(texts)
    assert {text: read for text, (read, _) in verdicts.items()} == {texts[0]: False, texts[1]: False, texts[2]: True}
    assert "syntax error" in verdicts[texts[1]][1]


def test_a_batch_whose_every_reading_ends_still_holds_the_reader_to_bash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bash_agreement, "READ_EACH", ENDED_SUBSHELL + bash_agreement.READ_EACH)
    # Bash and the reader both refuse the first and read the second, so only an end read as a verdict disagrees.
    bash_agreement.assert_reader_agrees(["echo first ended )", "echo second ended"])


class HereDocumentTests(unittest.TestCase):
    def test_a_body_whose_expansion_never_closes_says_so(self) -> None:
        unclosed = bash_syntax.here_documents(first_simple('cat <<EOF\n$(git am) $(echo "a)\nEOF'))[0]
        self.assertEqual((True, ("git am",)), (unclosed.unclosed, unclosed.substitutions))
        closed = bash_syntax.here_documents(first_simple("cat <<EOF\n$(git am)\nEOF"))[0]
        self.assertFalse(closed.unclosed)

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

    def test_an_unquoted_body_joins_a_line_its_backslash_continues_before_it_looks_for_the_delimiter(self) -> None:
        # An odd count of backslashes escapes the LF, so the delimiter after it is body text; an even count, or a quoted
        # delimiter, leaves the line as it is.
        tree = parse("cat <<EOF\nfoo \\\nEOF\ngit commit\nEOF\ngit status")
        self.assertEqual([["cat"], ["git", "status"]], simple_commands(tree))
        here = first_simple("cat <<EOF\nfoo \\\nEOF\ngit commit\nEOF").redirects[0].here
        self.assertEqual("foo EOF\ngit commit", here.body if here is not None else None)
        for text in ("cat <<EOF\nfoo \\\\\nEOF\ngit status", "cat <<'EOF'\nfoo \\\nEOF\ngit status"):
            with self.subTest(text=text):
                self.assertEqual([["cat"], ["git", "status"]], simple_commands(parse(text)))

    def test_a_quote_inside_a_body_s_substitution_or_expansion_quotes_as_bash_reads_it(self) -> None:
        here = first_simple('cat <<EOF\n"$(echo ")"; git am)" ${x:-"a}"}\nEOF').redirects[0].here
        self.assertEqual(('echo ")"; git am',), here.substitutions if here is not None else None)
        self.assertEqual(["$(", '${x:-"a}"}'], [part.text for part in here.expansions] if here is not None else None)

    def test_a_quoted_delimiter_ending_in_a_cr_closes_at_its_line(self) -> None:
        text = 'cat <<"EOF\r"\nhi\nEOF\r\ngit status'
        self.assertEqual([["cat"], ["git", "status"]], simple_commands(parse(text)))

    def test_a_script_with_crlf_line_ends_closes_each_body_at_its_delimiter_line(self) -> None:
        # Bash reads the word as `EOF<CR>` and the line as `EOF<CR>`, and Git Bash drops both CRs, so either way the
        # body closes there and the command after it runs; quoted or not.
        for text in ("cat <<EOF\r\nhi\r\nEOF\r\ngit status\r\n", "cat <<'EOF'\r\nhi\r\nEOF\r\ngit status\r\n"):
            with self.subTest(text=text):
                self.assertEqual([["cat"], ["git", "status"]], simple_commands(parse(text)))

    def test_a_documented_limit_a_delimiter_line_ending_in_a_cr_closes_the_body_as_git_bash_reads_it(self) -> None:
        # Bash itself counts the body's lines, the text running only cat, wc, and `:` wherever it runs: Git Bash drops
        # the CR and closes the body at `:<CR>`, running the `:` after it; Bash elsewhere keeps the CR, so the body runs
        # to the next `:`.
        text = "cat <<: | wc -l\nbody\n:\r\n:\n"
        here = first_simple(text).redirects[0].here
        self.assertEqual("body", here.body if here is not None else None)
        self.assertEqual([["cat"], ["wc", "-l"], [":"]], simple_commands(parse(text)))
        result = platform_support.run_tool([bash_agreement.find_bash(), "-c", text])
        lines = "1" if platform_support.WINDOWS else "2"
        self.assertEqual((0, lines), (result.returncode, result.output.strip()))


class AssigningExpansionTests(unittest.TestCase):
    """A word, and an unquoted here-document's body, record each variable an expansion in it assigns."""

    def test_each_expansion_that_assigns_is_recorded_by_the_name_it_assigns(self) -> None:
        for text, assigns in (
            (": ${x:=1}", ("x",)),
            (": ${x=1}", ("x",)),
            (': "${x:=1}"', ("x",)),
            (": ${a[1]:=1}", ("a[1]",)),
            (": ${!r:=1}", ("!r",)),
            (": ${z:-${x:=1}}", ("x",)),
            (": ${x:-1} ${#x} ${x/a=b/c}", ()),
            (": '${x:=1}' \\${x:=1}", ()),
            # Bash's names are ASCII, so an expansion holding another letter assigns none.
            (": ${xé:=1}", ()),
        ):
            with self.subTest(text=text):
                self.assertEqual(assigns, tuple(name for word in first_simple(text).words for name in word.assigns))
        here = first_simple("cat <<EOF\n${h:=1}\nEOF").redirects[0].here
        self.assertEqual(("h",), here.assigns if here is not None else None)
        # A word built anywhere else assigns nothing.
        self.assertEqual((), bash_syntax.Word("x", ()).assigns)

    def test_an_arithmetic_for_keeps_its_header_words_kind_body_and_redirections(self) -> None:
        loop = parse("for (( i = ${n:=1}; i < 2; i++ )); do echo; done > out").items[0][0].first.commands[0]
        self.assertIsInstance(loop, bash_syntax.Loop)
        assert isinstance(loop, bash_syntax.Loop)
        self.assertEqual("for", loop.kind)
        self.assertTrue(loop.header)
        self.assertTrue(all(isinstance(word, bash_syntax.Word) for word in loop.header))
        self.assertEqual(["n"], [name for word in loop.header for name in word.assigns])
        self.assertEqual([["echo"]], simple_commands(loop.body))
        self.assertEqual([">"], [redirect.operator for redirect in loop.redirects])


class TreeTests(unittest.TestCase):
    def test_a_comment_runs_to_the_end_of_its_line_but_a_hash_inside_a_word_does_not_start_one(self) -> None:
        self.assertEqual(
            [["git", "status"], ["echo", "a#b"]], simple_commands(parse("git status # git reset\necho a#b"))
        )

    def test_a_case_keeps_each_word_of_every_pattern_which_bash_expands_as_it_tests_it(self) -> None:
        tree = parse("case $x in a|$(git reset)) :;; (b*|$((n=1))) :;; esac")
        [(and_or, _)] = tree.items
        case = and_or.first.commands[0]
        assert isinstance(case, bash_syntax.Case)
        self.assertEqual(["a", "$(git reset)", "b*", "$((n=1))"], [word.raw for word in case.patterns])
        self.assertEqual(("git reset",), case.patterns[1].substitutions)


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
        ("time -- git checkout main", [["git", "checkout", "main"]]),
        ("time -p -- git checkout main", [["git", "checkout", "main"]]),
        ("! time -- git checkout main", [["git", "checkout", "main"]]),
        ("time -- ! git commit", [["git", "commit"]]),
        ("time -- time -p -- git commit", [["git", "commit"]]),
        ("time -- if git commit; then :; fi", [["git", "commit"], [":"]]),
        ("time -p -- { git commit; }", [["git", "commit"]]),
        # Bash reads -p only right after time, -- only right after time or its -p, and each only unquoted, so any
        # other such word is the command's name.
        ("time -- -- git commit", [["--", "git", "commit"]]),
        ("time -- -p git commit", [["-p", "git", "commit"]]),
        ("time -p -p git commit", [["-p", "git", "commit"]]),
        ("time -p -- -- git commit", [["--", "git", "commit"]]),
        # Bash reads `time` as its keyword only where a pipeline starts, so after a pipe, or the one newline right after
        # one, it is the time program, which takes -p and -- as its own options, and a subscript after it is no
        # assignment's.
        ("echo a | time -p -p git commit", [["echo", "a"], ["time", "-p", "-p", "git", "commit"]]),
        ("echo a |& time git commit", [["echo", "a"], ["time", "git", "commit"]]),
        ("echo a |\n time -- git commit", [["echo", "a"], ["time", "--", "git", "commit"]]),
        ("echo a | # note\n time -p a[x y]=v", [["echo", "a"], ["time", "-p", "a[x", "y]=v"]]),
        ('time "--" git commit', [['"--"', "git", "commit"]]),
        ("time -p '--' git commit", [["'--'", "git", "commit"]]),
        ("! -p git commit", [["-p", "git", "commit"]]),
        ("case $x in a|b) git add .;; *) echo;; esac", [["git", "add", "."], ["echo"]]),
        ("f() { cd /x; }; f", [["cd", "/x"], ["f"]]),
        ("function g { git am; }", [["git", "am"]]),
        ("[[ -n $x && -d y ]] && git commit", [["[[", "-n", "$x", "-d", "y", "]]"], ["git", "commit"]]),
    ],
)
def test_reserved_words_and_prefixes_start_compound_commands_not_programs(text: str, expected: list[list[str]]) -> None:
    assert expected == simple_commands(parse(text))


@pytest.mark.parametrize("text", ["echo a |\n\n time git commit", "echo a |\n\n time -p git commit"])
def test_time_after_a_pipe_and_more_than_one_newline_is_the_keyword_bash_refuses_there(text: str) -> None:
    assert not bash_agreement.bash_verdicts([text])[text][0]
    with pytest.raises(BashSyntaxError, match=r"^`time` where a command should start$"):
        parse(text)


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
        ">out f() { :; }",
        ">out ( : )",
        ">out { :; }",
        "x=1 { :; }",
        ">out function f { :; }",
        "x=1 if true; then :; fi",
        "x=1 >out a=(1) git push",
        "2>&1 x=1 >out a=(1) git push",
        "x=1 >out declare a=(1)",
        "x=1 2>&1 declare a=(1)",
        "2>",
        ">out <",
        # A function's name is never an assignment, and the function keyword names one.
        "a=b () { :; }",
        "function",
        # `case` is a reserved word only where a command starts, so this `)` closes the substitution.
        "x=$(echo case a in a) :;; esac)",
        # A compound command that the text, or a substitution, ends before its word.
        "echo; case",
        "for",
        "select",
    ],
)
def test_text_bash_would_refuse_is_an_error(text: str) -> None:
    with pytest.raises(BashSyntaxError):
        parse(text)


class ArithmeticCommandTests(unittest.TestCase):
    """`(( ))` where Bash reads a reserved word, or right after `for`, is one arithmetic word, whose `<<` shifts."""

    def test_an_arithmetic_command_is_one_word_whose_shift_opens_no_here_document(self) -> None:
        tree = parse("(( x = 1 << 2 )) > out\ngit status\n2")
        command = tree.items[0][0].first.commands[0]
        assert isinstance(command, bash_syntax.Arithmetic)
        self.assertEqual("(( x = 1 << 2 ))", command.expression.raw)
        self.assertEqual([">"], [redirect.operator for redirect in command.redirects])
        self.assertEqual([["git", "status"], ["2"]], simple_commands(tree))

    def test_an_arithmetic_command_keeps_its_substitutions_and_assigning_expansions(self) -> None:
        command = parse("(( $(git am) + `git log` + ${n:=1} ))").items[0][0].first.commands[0]
        assert isinstance(command, bash_syntax.Arithmetic)
        self.assertEqual(("git am", "git log"), command.expression.substitutions)
        self.assertEqual(("n",), command.expression.assigns)

    def test_only_double_parentheses_make_an_arithmetic_word(self) -> None:
        word = first_simple("x=${y:=1} git status").words[0]
        self.assertEqual((("y",), False), (word.assigns, word.arithmetic))
        self.assertEqual([["x=${y:=1}", "git", "status"]], simple_commands(parse("x=${y:=1} git status")))

    def test_double_parentheses_that_close_apart_are_two_subshells(self) -> None:
        self.assertEqual([["cd", "x"]], simple_commands(parse("((cd x) )")))
        # A quoted string's parentheses are text, so its `))` closes nothing.
        self.assertEqual([["echo", '"))"'], ["git", "status"]], simple_commands(parse('((echo "))"; git status) )')))
        self.assertEqual([["echo", "'))'"], ["git", "status"]], simple_commands(parse("((echo '))'; git status) )")))

    def test_a_quoted_string_in_double_parentheses_keeps_its_substitutions(self) -> None:
        # Bash 5 runs a substitution inside either quote there, a `))` inside a nested one's quotes included.
        for text, found in (
            ('((x="$(git am)"))', ("git am",)),
            ("(( x = '$(git am)' + `git log` ))", ("git am", "git log")),
            ('(( x = "$(echo "))"; git am)" ))', ('echo "))"; git am',)),
            ('(( x = "\\"$(git am)" ))', ("git am",)),
            ('(( x = "$(( $(git am) ))" ))', ("git am",)),
        ):
            with self.subTest(text=text):
                command = parse(text).items[0][0].first.commands[0]
                assert isinstance(command, bash_syntax.Arithmetic)
                self.assertEqual(found, command.expression.substitutions)

    def test_an_unclosed_quote_in_double_parentheses_falls_back_to_subshells_and_bash_refuses_both(self) -> None:
        text = '(( echo "a ))'
        bash_agreement.assert_reader_agrees([text])
        with self.assertRaisesRegex(BashSyntaxError, "^a double quote is not closed$"):
            bash_syntax.parse(text)

    def test_an_arithmetic_command_is_a_function_s_body(self) -> None:
        tree = parse("f() ((1)); function g ((2)); git status")
        commands = [item[0].first.commands[0] for item in tree.items[:2]]
        bodies = [command.body for command in commands if isinstance(command, bash_syntax.FunctionDefinition)]
        self.assertEqual(2, len([body for body in bodies if isinstance(body, bash_syntax.Arithmetic)]))
        self.assertEqual([["git", "status"]], simple_commands(tree))

    def test_an_arithmetic_for_s_header_is_one_word_whose_shift_opens_no_here_document(self) -> None:
        loop = parse("for ((i = 1 << 2; i < 9; i++)); do echo; done\ngit status").items[0][0].first.commands[0]
        assert isinstance(loop, bash_syntax.Loop)
        self.assertEqual(["((i = 1 << 2; i < 9; i++))"], [word.raw for word in loop.header])

    def test_double_parentheses_after_a_word_are_no_arithmetic_command(self) -> None:
        for text in ("echo ((1))", "x=1 ((1))"):
            with self.subTest(text=text):
                bash_agreement.assert_reader_agrees([text])


# Bash reads any word but an assignment as a function's name, yet defines the function only by one with no quote,
# escape, or expansion, refusing any other as not a valid identifier when it runs the definition; the name is None
# for such a word, and its body is read, as every function's body is.
@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("f () { git push; }", "f"),
        ("f-x () { git push; }", "f-x"),
        ("function a=b { git push; }", "a=b"),
        ("'f' () { git push; }", None),
        ('"f" () { git push; }', None),
        ("\\f () { git push; }", None),
        ("f''g () { git push; }", None),
        ("$x () { git push; }", None),
        ("f$(echo) () { git push; }", None),
        ("function 'g' () { git push; }", None),
        ('function f"g" { git push; }', None),
        ("function $x { git push; }", None),
    ],
)
def test_a_function_is_named_only_by_a_word_bash_defines_it_by(text: str, name: str | None) -> None:
    bash_agreement.assert_reader_agrees([text])
    definitions = [
        command for command in bash_syntax.every_command(text) if isinstance(command, bash_syntax.FunctionDefinition)
    ]
    assert [definition.name for definition in definitions] == [name]
    assert ["git", "push"] in [bash_syntax.command_words(command) for command in bash_syntax.simple_commands(text)]


# Functions in each form Bash defines one, with braces in a quoted string and a comment, a header-shaped line in a
# here-document's body, a nested definition, and redirections continued onto a later line.
FUNCTION_SPANS_SCRIPT = (
    "f() {\n"  # 1
    '  echo "}" # }\n'  # 2
    "  git push\n"  # 3
    "}\n"  # 4
    "cat <<'BODY'\n"  # 5
    "k() {\n"  # 6: the body's text, which defines nothing
    "BODY\n"  # 7
    "function g\n"  # 8
    "{\n"  # 9
    "  :\n"  # 10
    "}\n"  # 11
    "h ()\n"  # 12
    "(\n"  # 13
    "  :\n"  # 14
    ")\n"  # 15
    "outer() {\n"  # 16
    "  inner() { :; }\n"  # 17
    "}\n"  # 18
    "redirected() {\n"  # 19
    "  :\n"  # 20
    "} \\\n"  # 21
    "  >/dev/null\n"  # 22
    "function keyword() { :; }\n"  # 23
)
FUNCTION_SPANS = {
    "f": (1, 4),
    "g": (8, 11),
    "outer": (16, 18),
    "inner": (17, 17),
    "h": (12, 15),
    "redirected": (19, 22),
    "keyword": (23, 23),
}


def test_a_function_spans_from_the_line_bash_defines_it_on_to_the_last_line_its_body_reads(tmp_path: Path) -> None:
    bash_agreement.assert_reader_agrees([FUNCTION_SPANS_SCRIPT])
    definitions = [
        command
        for command in bash_syntax.every_command(FUNCTION_SPANS_SCRIPT)
        if isinstance(command, bash_syntax.FunctionDefinition)
    ]
    spans = {definition.name: (definition.line, definition.end) for definition in definitions}
    assert spans == FUNCTION_SPANS
    script = tmp_path / "spans.sh"
    names = " ".join(FUNCTION_SPANS)
    script.write_bytes(f"{FUNCTION_SPANS_SCRIPT}outer\nshopt -s extdebug\ndeclare -F {names}\n".encode())
    result = platform_support.run_tool([bash_agreement.find_bash(), platform_support.normalize(script)])
    assert result.returncode == 0, result.output
    # The here-document's body, which cat prints, comes first; declare -F prints each name, its line, and the script.
    defined = {words[0]: int(words[1]) for words in (line.split() for line in result.output.splitlines()[1:])}
    assert defined == {name: start for name, (start, _) in FUNCTION_SPANS.items()}


@pytest.mark.parametrize(
    ("text", "span"),
    [
        # A here-document after the body, or one opened on the body's closing line, ends the function on the
        # operator's line, as the module states.
        ("f() { cat; } <<EOF\nbody\nEOF\n", (1, 1)),
        ("f() { cat <<EOF; }\nbody\nEOF\n", (1, 1)),
        # A header a continuation splits starts on its first word's line, where Bash reports the next.
        ("f \\\n() { :; }\n", (1, 2)),
        ("function \\\nf { :; }\n", (1, 2)),
    ],
)
def test_a_documented_limit_a_function_s_span_around_a_here_document_or_a_split_header(
    text: str, span: tuple[int, int]
) -> None:
    bash_agreement.assert_reader_agrees([text])
    [definition] = [
        command for command in bash_syntax.every_command(text) if isinstance(command, bash_syntax.FunctionDefinition)
    ]
    assert (definition.line, definition.end) == span


@pytest.mark.parametrize(
    ("text", "kept", "dropped"),
    [
        ("run_thing # days_old\n", {"run_thing"}, {"days_old"}),
        ("# days_old\rrun_thing\n", set(), {"days_old", "run_thing"}),
        # A quoted `#` and one inside a word start no comment.
        ("echo '# kept' x#y\n", {"#", "kept", "x#y"}, set()),
        # A comment inside a command or process substitution, in backquotes, in double quotes, or inside a ${ }, is
        # one Bash reads in the subshell, and so is one among an array assignment's elements.
        ('out=$(\n  # days_old\n  "$S" --check\n)\n', {"--check"}, {"days_old"}),
        ("echo `true # days_old`\n", {"true"}, {"days_old"}),
        ("cat <(echo # days_old\n)\n", {"echo"}, {"days_old"}),
        ('echo "$(f # days_old\n)"\n', {"f"}, {"days_old"}),
        ('echo "${x:-$(f # days_old\n)}"\n', {"f"}, {"days_old"}),
        ("a=( x # days_old\n y )\n", {"x", "y"}, {"days_old"}),
        # A here-document's body that is Bash, as a script handed to a shell is, loses its comments too; one that is
        # not is data, kept whole.
        ("bash <<'EOF'\n# days_old\nrun_thing\nEOF\n", {"run_thing"}, {"days_old"}),
        ("cat <<EOF\ndays_old # in the body\nEOF\n", {"days_old"}, {"body"}),
        ("cat <<EOF\nit's days_old\nEOF\n", {"it's", "days_old"}, set()),
        # A word is kept as written: a parameter keeps its `$` and braces, so it never joins the text beside it into
        # another name, an ANSI-C string keeps its text, and a ${ }'s text outside its substitutions stays.
        ('log="$TMP/${days}_old.log"\n', {"log"}, {"days_old"}),
        ('days_old"$sfx"\n', {"days_old"}, set()),
        ("bash -c $'days_old\\n'\n", {"days_old"}, set()),
        ('echo "${x:-a days_old $(d)}"\n', {"days_old", "d"}, set()),
    ],
)
def test_uncommented_text_drops_each_comment_bash_reads_and_keeps_everything_else(
    text: str, kept: set[str], dropped: set[str]
) -> None:
    # A name counts as a reader of the text counts one: a whole word, as review-repository's untested route reads it.
    bash_agreement.assert_reader_agrees([text])
    found = bash_syntax.uncommented_text(text)

    def names(word: str) -> bool:
        return re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", found) is not None

    assert [word for word in sorted(kept) if not names(word)] == []
    assert [word for word in sorted(dropped) if names(word)] == []


def test_a_quoted_copy_of_a_substitution_cut_in_its_place_leaves_the_same_names() -> None:
    # The quoted copy stands first, so it is the one cut: the word keeps the substitution's text where the copy stood,
    # the same words as cutting the substitution itself would keep, the copy's `# x` being data that names `x`.
    text = "echo '$(d # x\n)'\"$(d # x\n)\"\n"
    bash_agreement.assert_reader_agrees([text])
    found = bash_syntax.uncommented_text(text)
    cut_in_place = "echo '$(d # x\n)'\"$( )\" d"
    assert sorted(re.findall(r"[\w-]+", found)) == sorted(re.findall(r"[\w-]+", cut_in_place))


@pytest.mark.parametrize("text", ["echo 'unclosed\n", 'echo "$(f\n', "cat <(echo\n"])
def test_uncommented_text_raises_on_text_the_grammar_cannot_read(text: str) -> None:
    with pytest.raises(BashSyntaxError):
        bash_syntax.uncommented_text(text)


class Refused(Exception):
    """A caller's own refusal of Bash the grammar refuses."""


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("echo 'unclosed", "a single quote is not closed"),
        # NotRead, the grammar's refusal of text Bash may run, is refused the same way.
        ("for x in a; { git push; }", "a loop whose body is a `{ }` group is not read"),
    ],
)
def test_refusing_raises_the_caller_s_refusal_from_the_grammar_s_error(text: str, reason: str) -> None:
    with pytest.raises(Refused) as caught, bash_syntax.refusing(lambda error: Refused(f"refused: {error}")):
        bash_syntax.every_command(text)
    assert str(caught.value) == f"refused: {reason}"
    assert isinstance(caught.value.__cause__, BashSyntaxError)


def test_refusing_passes_bash_the_grammar_reads_and_any_other_error_through() -> None:
    with bash_syntax.refusing(Refused):
        assert bash_syntax.command_words(bash_syntax.simple_commands("git push")[0]) == ["git", "push"]
    with pytest.raises(KeyError), bash_syntax.refusing(Refused):
        raise KeyError("not the grammar's")


def test_a_documented_limit_a_function_name_holding_a_tilde_which_bash_defines_is_read_as_refused() -> None:
    text = "~f () { echo defined; }; ~f"
    bash_agreement.assert_reader_agrees([text])
    result = platform_support.run_tool([bash_agreement.find_bash(), "-c", text], separate_errors=True)
    assert result.output.splitlines() == ["defined"]
    definitions = [
        command for command in bash_syntax.every_command(text) if isinstance(command, bash_syntax.FunctionDefinition)
    ]
    assert [definition.name for definition in definitions] == [None]


@pytest.mark.parametrize(
    "definition",
    [
        "'f' () { echo body; }",
        '"f" () { echo body; }',
        "\\f () { echo body; }",
        "f''g () { echo body; }",
        "$x () { echo body; }",
        "f$(echo) () { echo body; }",
        "function 'g' () { echo body; }",
        'function f"g" { echo body; }',
        "function $x { echo body; }",
    ],
)
def test_a_function_name_bash_refuses_defines_nothing_when_bash_runs_it(definition: str) -> None:
    # The name Bash refuses as it runs the definition defines no function, so a later command of that name runs a
    # program, and the body never runs.
    result = platform_support.run_tool(
        [bash_agreement.find_bash(), "-c", f"{definition}; echo ran"], separate_errors=True
    )
    assert result.output.splitlines() == ["ran"]
    assert "not a valid identifier" in result.errors


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Every command Bash may run, a substitution's anywhere included, from its program's name.
        ("A=1 B=$(git am) env C=2 git push", [["env", "C=2", "git", "push"], ["git", "am"]]),
        ('cat > "$(git out)" <<EOF\n$(git body)\nEOF', [["cat"], ["git", "out"], ["git", "body"]]),
        ("cat <<'EOF'\n$(git body)\nEOF", [["cat"]]),
        ("case $(git a) in $(git b)) git c;; esac", [["git", "a"], ["git", "b"], ["git", "c"]]),
        ("for i in $(git l); do git y; done", [["git", "l"], ["git", "y"]]),
        ("for ((i = $(git h); i < 2; i++)); do :; done", [["git", "h"], [":"]]),
        ("(( $(git x) ))", [["git", "x"]]),
        ("f() { git push; }", [["git", "push"]]),
        ("if a; then b; elif c; then d; else e; fi", [["a"], ["b"], ["c"], ["d"], ["e"]]),
        (
            "echo <(git d) ${x:-$(git e)} $(( $(git f) ))",
            [["echo", "<(", "${x:-$(git e)}", "$(("], ["git", "d"], ["git", "e"], ["git", "f"]],
        ),
        ("x=1", [[]]),
        # A case pattern's `)` inside a substitution closes the pattern, not the substitution, as Bash 4 and later
        # read it, a pattern opened with `(` or not, in a subshell, a nested case, or a backquote. Bash 3.2, macOS's
        # /bin/bash, ends the substitution at the pattern's `)`, so its syntax check refuses the first six. It does not
        # refuse them before running them: where the rest of the line parses, as when a newline ends the pattern's
        # commands, it runs `x= b` in the shell around the substitution and refuses only the next line. It reads the
        # empty case and the backquoted one as these do. The grammar keeps the one reading of Bash 4 and later and
        # marks such a substitution, bare_case, which the hub guard refuses while a hub is guarded.
        (
            'os=$(case "$(uname)" in Darwin) echo mac;; *) echo linux;; esac)',
            [[], ["uname"], ["echo", "mac"], ["echo", "linux"]],
        ),
        ("x=$( (case a in a) echo;; esac) )", [[], ["echo"]]),
        ("x=$(case a in (a) b;; c|d) (e);; esac; f)", [[], ["b"], ["e"], ["f"]]),
        ("x=$(case in in in) b;; esac)", [[], ["b"]]),
        ("x=$(case a in a) b\nesac)", [[], ["b"]]),
        ("x=$(case a in a) case b in b) c;; esac;; esac)", [[], ["c"]]),
        ("x=$(case a in esac); b", [[], ["b"]]),
        ("x=`case a in a) b;; esac`", [[], ["b"]]),
        # A word's quotes are removed and each expansion kept in the form that names it.
        ("git \"$sub\" ${t} ~/x 'a b' $'c'", [["git", "$sub", "$t", "~/x", "a b", "c"]]),
    ],
)
def test_simple_commands_read_every_command_bash_may_run_from_its_program(text: str, expected: list[list[str]]) -> None:
    bash_agreement.assert_reader_agrees([text])
    found = [bash_syntax.command_words(command) for command in bash_syntax.simple_commands(text)]
    assert found == expected


@pytest.mark.parametrize(
    "text",
    [
        'cat <<EOF\n$(git log) $(echo "a) $(git am)\nEOF\ngit status',
        "cat <<EOF\n$(if) `fi` $(git am)\nEOF",
        "cat <<EOF\n`if` $(git am)\nEOF",
        "cat <<EOF\n$(if [ -n x ]; then git commit -m x; fi })\nEOF",
    ],
)
def test_a_body_s_substitution_or_expansion_the_reader_refuses_is_not_read(text: str) -> None:
    # Bash runs whatever a substitution in an unquoted body holds, so one the reader refuses, or an expansion the body
    # never closes, leaves the text unread, whether Bash refuses it too or not, never one that runs nothing.
    bash_agreement.assert_reader_agrees([text])
    with pytest.raises(bash_syntax.NotRead):
        bash_syntax.simple_commands(text)


def test_command_words_mark_only_a_word_whose_part_names_its_form_alone_as_unsettled() -> None:
    text = 'git "$@" ${x:-y} $t x$(a) `b` <(c) $((1)) $\'\\n\' \'q\' "(" "(("'
    bash_agreement.assert_reader_agrees([text])
    found = bash_syntax.command_words(bash_syntax.simple_commands(text)[0], "?")
    assert found == ["git", "$@", "${x:-y}", "$t", "x?", "?", "?", "?", "?", "q", "(", "(("]


# A substitution that ends where its compound command's word should stand, as `$(case)`, is refused, not an IndexError.
@pytest.mark.parametrize("text", ["echo $(if)", "echo `fi`", "x=$(case)", "x=$(for)"])
def test_simple_commands_raise_where_a_substitution_cannot_be_read(text: str) -> None:
    with pytest.raises(BashSyntaxError):
        bash_syntax.simple_commands(text)


def test_simple_commands_raise_where_nesting_runs_past_the_recursion_limit() -> None:
    with pytest.raises(bash_syntax.NotRead, match=r"^it nests deeper than the reader follows$"):
        bash_syntax.simple_commands("echo $(" * DEPTH + "git am" + ")" * DEPTH)
    # The walk into each substitution recurses too, past where parse read the text whole.
    with (
        mock.patch.object(bash_syntax, "_tree_commands", side_effect=RecursionError("maximum recursion depth")),
        pytest.raises(bash_syntax.NotRead, match=r"^it nests deeper than the reader follows$"),
    ):
        bash_syntax.simple_commands("git am")


@pytest.mark.parametrize(
    "text",
    [
        "for ((i = 0; i < 1; i++)) { echo; }",
        "for ((i = 0; i < 1; i++)); { echo; }",
        "for x in a; { echo; }",
        "for x\n{ echo; }",
        "select x in a; { echo; }",
    ],
)
def test_a_documented_limit_a_loop_with_a_group_body_which_bash_reads_is_not_read(text: str) -> None:
    assert bash_agreement.bash_verdicts([text])[text][0]
    with pytest.raises(bash_syntax.NotRead, match=r"^a loop whose body is a `\{ \}` group is not read$"):
        bash_syntax.parse(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('(( x = "${y:-"))"}" ))', []),
        ('echo $(( ")" ))', [["echo", '$(( ")" ))']]),
        ('echo $(( ")" + $(git push) ))', [["echo", '$(( ")" + $(git push) ))'], ["git", "push"]]),
        ("echo $(( ${y:-'))'} ))", [["echo", "$(( ${y:-'))'} ))"]]),
        ("(( ${y:-'))'} )); git push", [["git", "push"]]),
        ('echo $[ "]" + $(git push) ]', [["echo", '$[ "]" + $(git push) ]'], ["git", "push"]]),
    ],
)
def test_a_quoted_parenthesis_or_bracket_in_arithmetic_closes_nothing_as_bash_reads_it(
    text: str, expected: list[list[str]]
) -> None:
    parse(text)
    assert simple_commands_of(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("echo $((echo a); (git push))", [["echo", "$((echo a); (git push))"], ["echo", "a"], ["git", "push"]]),
        ('x="$((echo a) | git push)"', [['x="$((echo a) | git push)"'], ["echo", "a"], ["git", "push"]]),
    ],
)
def test_a_double_parenthesis_whose_first_close_another_does_not_follow_is_a_substitution_of_a_subshell(
    text: str, expected: list[list[str]]
) -> None:
    parse(text)
    assert simple_commands_of(text) == expected


# A script whose every command is `:` with a tag, each on the line the comment beside it names, and the line the
# reader places each on: the line its first word stands on.
TRACED_SCRIPT = (
    "PS4='+$LINENO '; set -x\n"
    ": one\n"  # 2
    ": two \\\n"  # 3, continued on 4
    "  continued\n"
    "cat >/dev/null <<EOF; : after-body\n"  # 5
    "body $(: in-body) `: in-backquote`\n"  # 6, the body's line
    "EOF\n"
    ": three; : four\n"  # 8
    "if : cond\n"  # 9
    "then : then-branch\n"  # 10
    "fi\n"
    "f() {\n"
    "  : in-function\n"  # 13
    "}\n"
    "f\n"
    "x=$(\n"  # 16
    ": in-substitution\n"  # 17
    ")\n"
    "2>/dev/null : redirected\n"  # 19
    "while : loop; do break; done\n"  # 20
    "case x in\n"
    "  x) : in-case ;;\n"  # 22
    "esac\n"
    "{ : in-group; }\n"  # 24
    "( : in-subshell )\n"  # 25
    "\\\n"
    ": after-continuation\n"  # 27
    ">/dev/null : redirect-first\n"  # 28
    "</dev/null : input-first\n"  # 29
    "\\\n"
    "2>/dev/null : descriptor-after-continuation\n"  # 31
    ": continued \\\n"  # 32, its redirection on 33
    ">/dev/null\n"
    "A=1\\\n"  # 34: a first token that runs into the continuation
    " : crossing-assignment\n"
    ">/dev/null\\\n"  # 36
    " : crossing-redirection\n"
)
TRACED_LINES = {
    "one": 2,
    "two": 3,
    "after-body": 5,
    "in-body": 6,
    "in-backquote": 6,
    "three": 8,
    "four": 8,
    "cond": 9,
    "then-branch": 10,
    "in-function": 13,
    "in-substitution": 17,
    "redirected": 19,
    "loop": 20,
    "in-case": 22,
    "in-group": 24,
    "in-subshell": 25,
    "after-continuation": 27,
    "redirect-first": 28,
    "input-first": 29,
    "descriptor-after-continuation": 31,
    "continued": 32,
    "crossing-assignment": 34,
    "crossing-redirection": 36,
}
# The commands whose line Bash's $LINENO gives otherwise: one in a substitution or in an unquoted here-document's
# body, which Bash 3.2 and 5.3 trace on the line the command holding it starts or ends on. The reader gives the line
# the command's own text stands on, where an author finds it.
SUBSTITUTED = frozenset({"in-body", "in-backquote", "in-substitution"})
# The commands whose first token itself runs into a continuation, with no blank before its backslash, which Bash 3.2
# and 5.3 trace on the line after it. The reader gives the line the token starts on.
CROSSING = frozenset({"crossing-assignment", "crossing-redirection"})


def tagged_lines(text: str) -> dict[str, int]:
    return {
        words[1]: command.line
        for command in bash_syntax.simple_commands(text)
        if (words := bash_syntax.command_words(command))[:1] == [":"] and len(words) > 1
    }


def test_each_command_keeps_the_line_its_first_word_stands_on_as_bash_traces_it(tmp_path: Path) -> None:
    script = tmp_path / "traced.sh"
    script.write_bytes(TRACED_SCRIPT.encode("utf-8"))
    result = platform_support.run_tool([bash_agreement.find_bash(), platform_support.normalize(script)])
    traced = {
        words[2]: int(words[0].lstrip("+"))
        for words in (line.split() for line in result.output.splitlines())
        if len(words) > 2 and words[0].startswith("+") and words[1] == ":"
    }
    assert result.returncode == 0, result.output
    assert tagged_lines(TRACED_SCRIPT) == TRACED_LINES
    assert {tag: line for tag, line in traced.items() if tag not in SUBSTITUTED | CROSSING} == {
        tag: line for tag, line in TRACED_LINES.items() if tag not in SUBSTITUTED | CROSSING
    }
    # Bash traces these on another line, which the reader does not take.
    assert all(traced[tag] != TRACED_LINES[tag] for tag in SUBSTITUTED)
    assert {tag: traced[tag] for tag in CROSSING} == {tag: TRACED_LINES[tag] + 1 for tag in CROSSING}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Bash ends a line at LF alone, so a CR is part of the line, CRLF or alone.
        (": a\r\n: b\r\n", {"a": 1, "b": 2}),
        (": a\r: b\n: c\n", {"a": 1, "c": 2}),
        # A command led by a redirection, on a later line or after a continuation, starts at the redirection.
        (": a\n>/dev/null : b\n", {"a": 1, "b": 2}),
        (": a\n</dev/null : b\n", {"a": 1, "b": 2}),
        (": a\n\\\n2>/dev/null : b\n", {"a": 1, "b": 3}),
        # A command whose first token, an assignment or a redirection, runs into a continuation keeps that token's
        # line, where Bash 3.2 and 5.3 trace the line after it (the trace test holds this); with a blank before the
        # backslash, both trace the token's line too.
        ("A=1\\\n : x\n", {"x": 1}),
        (">/dev/null\\\n : x\n", {"x": 1}),
        ("A=1 \\\n: x\n", {"x": 1}),
        (">/dev/null \\\n: x\n", {"x": 1}),
        # A substitution in an unquoted body after a line its backslash continues, and in a body whose tabs `<<-`
        # strips, keeps its own line.
        ("cat <<EOF\na \\\nb $(: x)\nEOF\n: after\n", {"x": 3, "after": 5}),
        ("cat <<-EOF\n\t\t$(: x)\n\t$(\n: y\n)\n\tEOF\n", {"x": 2, "y": 4}),
        # A backquote's escaped backquote, which the reader removes, leaves the line of the text after it.
        ("echo `echo \\`: x\\`\n: y`\n", {"x": 1, "y": 2}),
        # A substitution in an arithmetic command, a redirection's target, and a nested substitution.
        ("((\n$(: x)\n))\n: y >$(\n: z\n)\n", {"x": 2, "y": 4, "z": 5}),
        ("echo $(echo $(\n\n: x))\n", {"x": 3}),
    ],
)
def test_a_command_keeps_its_script_line_wherever_it_stands(text: str, expected: dict[str, int]) -> None:
    bash_agreement.assert_reader_agrees([text])
    assert tagged_lines(text) == expected


def test_a_substitution_and_a_body_keep_the_script_line_of_each_character() -> None:
    word = first_simple("echo x$(\n: a)`: b`").words[1]
    found = [(text, bash_syntax.source_lines(text)) for text in word.substitutions]
    assert found == [("\n: a", (1, 2, 2, 2, 2)), (": b", (2, 2, 2, 2))]
    redirect = first_simple("cat <<EOF\na\n\nb\nEOF\n").redirects[0]
    assert redirect.here is not None
    assert isinstance(redirect.here.body, bash_syntax.Source)
    assert redirect.here.body.lines == (2, 2, 3, 4, 4)
    empty = first_simple("cat <<EOF\nEOF\n").redirects[0].here
    assert empty is not None
    assert isinstance(empty.body, bash_syntax.Source)
    assert empty.body.lines == (3,)


# Bash's reserved words as the newest Bash prints them with `compgen -k`, pinned so a runner whose Bash is older, as
# macOS's /bin/bash 3.2, which has no coproc, still holds the reader to every one, and the Bash release they were
# printed by, from which on every pinned word must still be one the runner's Bash prints.
PINNED_RESERVED_WORDS = frozenset(
    {"if", "then", "else", "elif", "fi", "case", "esac", "for", "select", "while", "until", "do", "done", "in"}
    | {"function", "time", "{", "}", "!", "[[", "]]", "coproc"}
)
PINNED_FROM_BASH = (5, 3)


def runner_bash() -> tuple[tuple[int, int], frozenset[str]]:
    """The validated Bash's release, as its major and minor numbers, and the reserved words it prints."""
    return bash_agreement.compgen("-k")


def test_declaration_builtins_are_the_builtins_after_which_bash_reads_an_array_assignment() -> None:
    # Bash reads NAME=( ) as an array among a builtin's arguments only for the builtins that take assignments, so
    # `bash -n` reads `<builtin> a=(1)` for exactly those, on every Bash.
    texts = {name: f"{name} a=(1)" for name in bash_agreement.compgen("-b")[1]}
    verdicts = bash_agreement.bash_verdicts(list(texts.values()))
    assert {name for name, text in texts.items() if verdicts[text][0]} == set(bash_syntax.DECLARATION_BUILTINS)


def refused_at_a_commands_start(word: str) -> bool:
    """Whether the reader refuses `word` where a command starts, with a command after it."""
    try:
        bash_syntax.parse(f"{word} git status")
    except BashSyntaxError:
        return True
    return False


def test_every_reserved_word_bash_prints_is_read_refused_or_not_read_with_a_reason() -> None:
    catalogue = runner_bash()[1] | PINNED_RESERVED_WORDS
    undecided = sorted(
        word
        for word in catalogue
        if word not in bash_syntax.RESERVED
        and word not in bash_syntax.NOT_READ
        and not refused_at_a_commands_start(word)
    )
    assert undecided == [], (
        "read each in bash_syntax.RESERVED, refuse it where a command starts, or name it in bash_syntax.NOT_READ "
        "with the reason, and add it to PINNED_RESERVED_WORDS"
    )
    assert sorted(runner_bash()[1] - PINNED_RESERVED_WORDS) == [], "pin each in PINNED_RESERVED_WORDS"


def test_a_bash_as_new_as_the_pin_prints_every_pinned_word() -> None:
    release, words = runner_bash()
    if release >= PINNED_FROM_BASH:
        assert sorted(PINNED_RESERVED_WORDS - words) == [], (
            f"Bash {release} no longer reserves these: remove each from PINNED_RESERVED_WORDS, and from "
            "bash_syntax.RESERVED or NOT_READ, and pin PINNED_FROM_BASH to this release"
        )


def reserved_words_not_read() -> set[str]:
    """The keys of bash_syntax.NOT_READ that name a reserved word, the rest naming an option or an expansion form."""
    return bash_syntax.NOT_READ.keys() - option_keys(bash_syntax.NOT_READ) - expansion_keys(bash_syntax.NOT_READ)


def test_every_reserved_word_the_reader_reads_or_leaves_unread_is_one_bash_reserves() -> None:
    catalogue = runner_bash()[1] | PINNED_RESERVED_WORDS
    assert sorted(bash_syntax.RESERVED - catalogue) == []
    assert sorted(reserved_words_not_read() - catalogue) == [], "remove each: no Bash reserves it"


def test_no_reserved_word_is_both_decided_and_not_read() -> None:
    decided = {
        word for word in reserved_words_not_read() if word in bash_syntax.RESERVED or refused_at_a_commands_start(word)
    }
    assert sorted(decided) == [], "remove each from bash_syntax.NOT_READ: the reader reads or refuses it"


@pytest.mark.parametrize("text", ["]]", "]] x", "! ]]", "x | ]]", "[[ -n a", "[[ -n a\ngit commit -m x"])
def test_a_closing_double_bracket_where_a_command_starts_or_an_unclosed_one_is_refused_as_bash_refuses_it(
    text: str,
) -> None:
    assert not bash_agreement.bash_n_verdict(text)[0]
    with pytest.raises(BashSyntaxError, match=r"^(`\]\]` where a command should start|a `\[\[` is not closed)$"):
        bash_syntax.parse(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[[ -n a\n]]", [["[[", "-n", "a", "]]"]]),
        ("[[ -n a\n&& -n b ]]", [["[[", "-n", "a", "-n", "b", "]]"]]),
        ("[[\n-n a ]]", [["[[", "-n", "a", "]]"]]),
        ("[[ -n a\n\n]]", [["[[", "-n", "a", "]]"]]),
        ("[[ -n a # a comment\n]]", [["[[", "-n", "a", "]]"]]),
        ("[[ -n a\n]]; git commit -m x", [["[[", "-n", "a", "]]"], ["git", "commit", "-m", "x"]]),
        (
            'if [[ -n "$a" &&\n -n "$b"\n]]; then git commit -m x; fi',
            [["[[", "-n", '"$a"', "-n", '"$b"', "]]"], ["git", "commit", "-m", "x"]],
        ),
    ],
)
def test_a_double_bracket_expression_runs_over_line_breaks_to_its_close_as_bash_reads_it(
    text: str, expected: list[list[str]]
) -> None:
    assert simple_commands(parse(text)) == expected


def bash_reads_coprocesses() -> bool:
    """Whether the validated Bash reads `coproc` as a reserved word, as Bash 4 and later do."""
    return "coproc" in runner_bash()[1]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("coproc git commit -m x", [["git", "commit", "-m", "x"]]),
        ("coproc x=1 git push", [["x=1", "git", "push"]]),
        ("coproc >o git push", [["git", "push"]]),
        ("coproc time git push", [["time", "git", "push"]]),
        ("coproc foo", [["foo"]]),
        ("coproc foo bar", [["foo", "bar"]]),
        ("coproc { git push; } > o", [["git", "push"]]),
        ("coproc N { x=1 git push; }", [["x=1", "git", "push"]]),
        ("coproc ( git push )", [["git", "push"]]),
        ("coproc N ( git push )", [["git", "push"]]),
        ("coproc N if true; then git push; fi", [["true"], ["git", "push"]]),
        ("coproc if true; then git push; fi", [["true"], ["git", "push"]]),
        ("coproc N for x in a; do git push; done", [["git", "push"]]),
        ("coproc N [[ -n a ]]", [["[[", "-n", "a", "]]"]]),
        ("coproc N (( 1 ))", []),
        ("coproc git push | cat", [["git", "push"], ["cat"]]),
        ("x | coproc git push", [["x"], ["git", "push"]]),
        ("git commit -m x && coproc true", [["git", "commit", "-m", "x"], ["true"]]),
        ("coproc true; git commit -m x", [["true"], ["git", "commit", "-m", "x"]]),
        ("git commit -m x\ncoproc true", [["git", "commit", "-m", "x"], ["true"]]),
        ("echo $(coproc git push)", [["echo", "$(coproc git push)"], ["git", "push"]]),
        ("cat <<EOF\n$(coproc git push)\nEOF\n", [["cat"], ["git", "push"]]),
    ],
)
def test_a_coprocess_is_read_as_the_subshell_bash_runs_its_command_in(text: str, expected: list[list[str]]) -> None:
    # Bash 3.2 reads `coproc` as a program's name and refuses a compound command after it, so a Bash that has no
    # coprocesses is not asked to agree; the reader reads each as Bash 4 and later do.
    if bash_reads_coprocesses():
        bash_agreement.assert_reader_agrees([text])
    assert simple_commands_of(text) == expected


def test_a_coprocess_s_assignments_are_read_as_its_command_s_own() -> None:
    for text in ("coproc x=1 git push", "coproc N { x=1 git push; }"):
        command = bash_syntax.simple_commands(text)[0]
        assert [word.assignment is not None for word in command.words] == [True, False, False], text
        assert bash_syntax.command_words(command) == ["git", "push"], text


@pytest.mark.parametrize(
    ("text", "refusal"),
    [
        ("coproc", "`coproc` names no command"),
        ("coproc ! true", "`!` where a command should start"),
        ("coproc function f { :; }", "`function` where a command should start"),
    ],
)
def test_a_coprocess_bash_refuses_is_refused(text: str, refusal: str) -> None:
    if bash_reads_coprocesses():
        assert not bash_agreement.bash_n_verdict(text)[0]
    with pytest.raises(BashSyntaxError, match=f"^{re.escape(refusal)}$"):
        bash_syntax.parse(text)


@pytest.mark.parametrize("text", ["'coproc' git push", "\\coproc git push", "x=1 coproc git push", "echo coproc"])
def test_coproc_quoted_after_an_assignment_or_as_an_argument_is_a_word(text: str) -> None:
    bash_agreement.assert_reader_agrees([text])
    assert "coproc" in [word.raw.strip("'\\") for word in first_simple(text).words]


@pytest.mark.parametrize(
    "text",
    [
        "cat <<EOF\n$(for ((i=0;i<1;i++)) { git push; })\n$(git status)\nEOF\n",
        "cat <<EOF\n$(for x in a; { git push; })\nEOF\n",
        "cat <<EOF\n" + "$( " * 400 + "git push" + " )" * 400 + "\nEOF\n",
    ],
)
def test_text_bash_runs_in_a_here_documents_body_that_the_reader_does_not_read_is_refused(text: str) -> None:
    # Bash runs each, so the reader refuses it.
    with pytest.raises(bash_syntax.NotRead):
        bash_syntax.simple_commands(text)


def simple_commands_of(text: str) -> list[list[str]]:
    return [[word.raw for word in command.words] for command in bash_syntax.simple_commands(text)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('cat <<EOF\n$(echo $(( ")" )); git push)\nEOF\n', [["cat"], ["echo", '$(( ")" ))'], ["git", "push"]]),
        ('cat <<EOF\n$( (( x = "${y:-"))"}" )); git push)\nEOF\n', [["cat"], ["git", "push"]]),
        ('cat <<EOF\n`echo $(( ")" ))`; $(git push)\nEOF\n', [["cat"], ["echo", '$(( ")" ))'], ["git", "push"]]),
        ("cat <<EOF\n$(: $(( 1 )); git push)\nEOF\n", [["cat"], [":", "$(( 1 ))"], ["git", "push"]]),
    ],
)
def test_arithmetic_with_a_quoted_parenthesis_in_a_here_documents_body_is_read_as_bash_reads_it(
    text: str, expected: list[list[str]]
) -> None:
    assert simple_commands_of(text) == expected


@pytest.mark.parametrize(
    "text",
    ["if [[ -n x ]] then git commit -m x; fi", "{ [[ -n x ]] }; git commit -m x", "[[ x =~ ( ]] ) ]]; git commit -m x"],
)
def test_a_documented_limit_a_reserved_word_after_a_close_or_a_close_in_a_regex_group_is_an_error(text: str) -> None:
    with pytest.raises(BashSyntaxError):
        bash_syntax.parse(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("! (( x > 1 ))", []),
        ("{ (( x > 1 )); }", []),
        ("if (( x > 1 )); then (( x > 1 )); elif (( x > 1 )); then (( x > 1 )); else (( x > 1 )); fi", []),
        ("while (( x > 1 )); do (( x > 1 )); done", []),
        ("until (( x > 1 )); do (( x > 1 )); done", []),
        ("time (( x > 1 ))", []),
        ("coproc (( x > 1 ))", []),
        ("if true; then a=(1) git push; fi", [["true"], ["git", "push"]]),
    ],
)
def test_after_each_reserved_word_that_opens_a_command_a_command_starts(text: str, expected: list[list[str]]) -> None:
    # Bash reads `((` as an arithmetic command, and NAME=( ) as an array assignment, only where a command starts, which
    # each of these words opens; elsewhere `(( x > 1 ))` would be two subshells running a program x with its output
    # sent to a file named 1.
    if "coproc" not in text or bash_reads_coprocesses():
        bash_agreement.assert_reader_agrees([text])
    assert [bash_syntax.command_words(command) for command in bash_syntax.simple_commands(text)] == expected


def test_a_here_documents_unclosed_expansion_is_refused_by_name() -> None:
    with pytest.raises(
        bash_syntax.NotRead, match=r"^a here-document's body holds an expansion the reader does not close$"
    ):
        bash_syntax.simple_commands('cat <<EOF\n$(echo "a)\nEOF\n')


def test_a_body_s_refused_substitution_is_refused_by_name_with_the_grammar_s_reason() -> None:
    with pytest.raises(bash_syntax.NotRead, match=r"^a here-document body's substitution is not read \(.+\)$"):
        bash_syntax.simple_commands("cat <<EOF\n$(if)\nEOF\n")


# Bash's `set -o` and `shopt` options, keyed as bash_syntax keys them, as the newest Bash prints them with `set -o` and
# `shopt -p`, pinned so a runner whose Bash is older, as macOS's /bin/bash 3.2, still holds the reader to every one, and
# from PINNED_FROM_BASH on every one must still be one the runner's Bash prints; and the options Git Bash adds, which
# no other Bash prints.
PINNED_OPTIONS = frozenset(
    f"set -o {name}"
    for name in (
        *("allexport", "braceexpand", "emacs", "errexit", "errtrace", "functrace", "hashall", "histexpand", "history"),
        *("ignoreeof", "interactive-comments", "keyword", "monitor", "noclobber", "noexec", "noglob", "nolog"),
        *("notify", "nounset", "onecmd", "physical", "pipefail", "posix", "privileged", "verbose", "vi", "xtrace"),
    )
) | frozenset(
    f"shopt {name}"
    for name in (
        *("array_expand_once", "assoc_expand_once", "autocd", "bash_source_fullpath", "cdable_vars", "cdspell"),
        *("checkhash", "checkjobs", "checkwinsize", "cmdhist", "compat31", "compat32", "compat40", "compat41"),
        *("compat42", "compat43", "compat44", "complete_fullquote", "direxpand", "dirspell", "dotglob", "execfail"),
        *("expand_aliases", "extdebug", "extglob", "extquote", "failglob", "force_fignore", "globasciiranges"),
        *("globskipdots", "globstar", "gnu_errfmt", "histappend", "histreedit", "histverify", "hostcomplete"),
        *("huponexit", "inherit_errexit", "interactive_comments", "lastpipe", "lithist", "localvar_inherit"),
        *("localvar_unset", "login_shell", "mailwarn", "no_empty_cmd_completion", "nocaseglob", "nocasematch"),
        *("noexpand_translation", "nullglob", "patsub_replacement", "progcomp", "progcomp_alias", "promptvars"),
        *("restricted_shell", "shift_verbose", "sourcepath", "varredir_close", "xpg_echo"),
    )
)
GIT_BASH_OPTIONS = frozenset({"set -o igncr", "shopt completion_strip_exe"})
OPTION_KEY = re.compile(r"(set -o|shopt) (\S+)")


@functools.cache
def runner_options() -> tuple[tuple[int, int], frozenset[str]]:
    """The validated Bash's release, as its major and minor numbers, and the options its `set -o` and `shopt -p`
    print, keyed as bash_syntax keys them."""
    script = 'echo "${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"; set -o; shopt -p'
    result = platform_support.run_tool([bash_agreement.find_bash(), "-c", script], separate_errors=True)
    assert result.returncode == 0, result.errors
    release, *lines = result.output.splitlines()
    major, minor = release.split()
    options = {f"shopt {words[2]}" if words[0] == "shopt" else f"set -o {words[0]}" for words in map(str.split, lines)}
    return (int(major), int(minor)), frozenset(options)


def option_keys(table: object) -> set[str]:
    """The keys of a table of bash_syntax's that name an option."""
    assert isinstance(table, (dict, frozenset))
    return {key for key in table if OPTION_KEY.fullmatch(key)}


def test_every_option_bash_prints_is_read_refused_or_not_read_with_a_reason() -> None:
    catalogue = runner_options()[1] | PINNED_OPTIONS | GIT_BASH_OPTIONS
    tables = (bash_syntax.OPTIONS_READ, bash_syntax.OPTIONS_REFUSED, option_keys(bash_syntax.NOT_READ))
    undecided = sorted(option for option in catalogue if sum(option in table for table in tables) != 1)
    assert undecided == [], (
        "decide each in exactly one of bash_syntax.OPTIONS_READ, OPTIONS_REFUSED, and NOT_READ, with the reason, and "
        "add it to PINNED_OPTIONS"
    )
    assert sorted(runner_options()[1] - PINNED_OPTIONS - GIT_BASH_OPTIONS) == [], "pin each in PINNED_OPTIONS"


def test_a_bash_as_new_as_the_pin_prints_every_pinned_option() -> None:
    release, options = runner_options()
    if release >= PINNED_FROM_BASH:
        assert sorted(PINNED_OPTIONS - options) == [], (
            f"Bash {release} no longer has these: remove each from PINNED_OPTIONS and from bash_syntax's tables, and "
            "pin PINNED_FROM_BASH to this release"
        )


def test_every_option_the_reader_decides_is_one_bash_has() -> None:
    catalogue = runner_options()[1] | PINNED_OPTIONS | GIT_BASH_OPTIONS
    decided = bash_syntax.OPTIONS_READ | bash_syntax.OPTIONS_REFUSED.keys() | option_keys(bash_syntax.NOT_READ)
    assert sorted(decided - catalogue) == [], "remove each: no Bash has it"


def test_each_letter_set_turns_on_a_refused_option_by_is_the_one_bash_reads() -> None:
    # Every letter set takes, tried alone in a subshell of its own, with each refused set option it turns on.
    names = " ".join(option_name for key in bash_syntax.OPTIONS_REFUSED if (option_name := set_option(key)))
    script = (
        'for letter in {a..z} {A..Z}; do ( set -"$letter" > /dev/null 2>&1 || exit 0; '
        f'for name in {names}; do [[ -o $name ]] && echo "$letter $name"; done ); done'
    )
    result = platform_support.run_tool([bash_agreement.find_bash(), "-c", script], separate_errors=True)
    assert result.returncode == 0, result.errors
    found = {tuple(line.split()) for line in result.output.splitlines()}
    assert found == set(bash_syntax.SET_LETTERS.items())


def set_option(key: str) -> str | None:
    """The name of the set option a key names; None for a shopt option's key."""
    match = OPTION_KEY.fullmatch(key)
    assert match is not None
    return match.group(2) if match.group(1) == "set -o" else None


def turning_on(key: str) -> list[str]:
    """Every form of a command that turns the option a key names on, each spelling set and shopt take."""
    name = bash_syntax.option_name(key)
    if set_option(key) is None:
        forms = [f"shopt -s {name}", f"shopt -s nullglob {name}", f"shopt -qs {name}", f"shopt -s -- {name}"]
    else:
        forms = [f"set -o {name}", f"set -eo {name}", f"set -o pipefail -o {name}", f"set -o -e -o {name}"]
        forms += [f"shopt -so {name}", f"shopt -s -o {name}", f"shopt -o -s nullglob {name}"]
        forms += [f"set -{letter}" for letter, option in bash_syntax.SET_LETTERS.items() if option == name]
        forms += [f"set -e{letter}u x" for letter, option in bash_syntax.SET_LETTERS.items() if option == name]
    first = forms[0]
    wrapped = [f"builtin {first}", f"builtin -- {first}", f"command {first}", f"command -p -- {first}"]
    return [*forms, *wrapped, f"'{first[:1]}'{first[1:]}"]


def placed(command: str) -> list[str]:
    """A command where a script may stand it: alone with a git call after it, in a group, a substitution, a function's
    body, after `&&`, and in an `if`."""
    return [
        f"{command}\ngit commit -m x",
        f"{{ {command}; }}",
        f"x=$({command})",
        f"f() {{ {command}; }}",
        f"true && {command}",
        f"if true; then {command}; fi",
    ]


@pytest.mark.parametrize("key", sorted(bash_syntax.OPTIONS_REFUSED))
def test_a_command_that_turns_on_a_refused_option_is_not_read_wherever_it_stands(key: str) -> None:
    texts = [text for command in turning_on(key) for text in placed(command)]
    bash_agreement.assert_reader_agrees(texts)
    name = bash_syntax.option_name(key)
    for text in texts:
        with pytest.raises(bash_syntax.NotRead, match=rf"^it turns on {re.escape(name)}: "):
            bash_syntax.simple_commands(text)
        with pytest.raises(bash_syntax.NotRead, match=rf"^it turns on {re.escape(name)}: "):
            bash_syntax.every_command(text)


def turning_off_or_reading(key: str) -> list[str]:
    """Commands that turn the option a key names off, print it, or test it, which set and shopt read as they do."""
    name = bash_syntax.option_name(key)
    if set_option(key) is None:
        return [f"shopt -u {name}", f"shopt -p {name}", f"shopt -q {name}", f"shopt {name}", f"shopt -s -u {name}"]
    return [f"set +o {name}", f"shopt -uo {name}", f"shopt -po {name}", f"set -- -o {name}"]


@pytest.mark.parametrize("key", sorted(PINNED_OPTIONS | GIT_BASH_OPTIONS))
def test_turning_an_option_off_or_printing_it_is_read_and_turning_on_a_read_one_too(key: str) -> None:
    texts = turning_off_or_reading(key)
    if key not in bash_syntax.OPTIONS_REFUSED:
        texts += turning_on(key)
    for text in texts:
        command = bash_syntax.command_words(bash_syntax.simple_commands(text)[0])
        assert command[0] in ("set", "shopt", "builtin", "command"), text


@pytest.mark.parametrize(
    "text",
    [
        "set +k",
        "set +H",
        "set -o",
        "set +o",
        "shopt",
        "shopt -s",
        "shopt -p",
        "set -- -k",
        "set - -k",
        "set x -k",
        "set -e x -k",
        "shopt -x extglob",
        "shopt -s -u extglob",
        "shopt extglob",
        "command -v set -k",
        "command -pV shopt -s extglob",
        "echo set -o posix",
        "echo shopt -s extglob",
        "set -o pipefail -o",
        "set -e -- -k",
        'set -- "$@"',
        'set x "$@"',
        "shopt -s nullglob",
        "unset POSIXLY_CORRECT",
        'x="$POSIXLY_CORRECT"',
        "echo POSIXLY_CORRECT=1",
        "git BASH_COMPAT=1 status",
        "shopt -s 'null*'",
        'shopt -s "{nullglob,dotglob}"',
        "set -o \\{posix,x}",
    ],
)
def test_set_and_shopt_that_turn_no_refused_option_on_and_other_commands_are_read(text: str) -> None:
    # Each turns a refused option off, prints or tests options, sets positional parameters, is refused by Bash as
    # setting nothing, or only names set or shopt as data.
    bash_agreement.assert_reader_agrees([text])
    assert bash_syntax.simple_commands(text) != []


@pytest.mark.parametrize(
    ("text", "variable"),
    [
        ("POSIXLY_CORRECT=1", "POSIXLY_CORRECT"),
        ("POSIXLY_CORRECT=1 :", "POSIXLY_CORRECT"),
        ("x=1 POSIXLY_CORRECT= git status", "POSIXLY_CORRECT"),
        ("export POSIXLY_CORRECT=1", "POSIXLY_CORRECT"),
        ("declare -x POSIXLY_CORRECT=y", "POSIXLY_CORRECT"),
        ("f() { local POSIXLY_CORRECT=y; }", "POSIXLY_CORRECT"),
        ("builtin readonly BASH_COMPAT=42", "BASH_COMPAT"),
        ('typeset BASH_COMPAT="31"', "BASH_COMPAT"),
        (': "${POSIXLY_CORRECT:=1}"', "POSIXLY_CORRECT"),
        ("echo x > ${BASH_COMPAT=31}", "BASH_COMPAT"),
        ("x=$(BASH_COMPAT=31)", "BASH_COMPAT"),
        # A word a compound command expands, an unquoted here-document's body, and arithmetic assign too.
        ("case ${POSIXLY_CORRECT:=1} in *) :;; esac", "POSIXLY_CORRECT"),
        ("case x in ${BASH_COMPAT:=31}) :;; esac", "BASH_COMPAT"),
        ("for i in ${POSIXLY_CORRECT:=1}; do :; done", "POSIXLY_CORRECT"),
        ("select i in ${BASH_COMPAT=31}; do :; done", "BASH_COMPAT"),
        ("cat <<EOF\n${POSIXLY_CORRECT:=1}\nEOF", "POSIXLY_CORRECT"),
        ("(( ${POSIXLY_CORRECT:=1} ))", "POSIXLY_CORRECT"),
        ("for (( ${BASH_COMPAT:=31}; ; )); do break; done", "BASH_COMPAT"),
        ("{ :; } > ${POSIXLY_CORRECT:=1}", "POSIXLY_CORRECT"),
    ],
)
def test_assigning_a_variable_that_turns_on_a_refused_option_is_not_read(text: str, variable: str) -> None:
    bash_agreement.assert_reader_agrees([text])
    reason = re.escape(f"it assigns {variable}, which {bash_syntax.OPTION_VARIABLES[variable]}")
    with pytest.raises(bash_syntax.NotRead, match=rf"^{reason}$"):
        bash_syntax.simple_commands(text)


def test_a_quoted_here_document_s_body_assigns_nothing() -> None:
    assert bash_syntax.simple_commands("cat <<'EOF'\n${POSIXLY_CORRECT:=1}\nEOF") != []


@pytest.mark.parametrize(
    ("text", "builtin"),
    [
        ('set -o "$x"', "set"),
        ("set $flags", "set"),
        ('set "$@"', "set"),
        ("set -e $x", "set"),
        ('set -o "posix$x"', "set"),
        ('shopt -s "$x"', "shopt"),
        ('shopt "$x" extglob', "shopt"),
        ("shopt -p $(echo x)", "shopt"),
        ('builtin set -o "$x"', "set"),
        # A glob names the files it matches and a brace expansion makes several words, so either may name any option.
        ("shopt -s {nullglob,extglob}", "shopt"),
        ("shopt -s ext{glob,quote}", "shopt"),
        ("shopt -s ext?lob", "shopt"),
        ("shopt -s extglo[b]", "shopt"),
        ("shopt -s ext*", "shopt"),
        ("set -o {posix,errexit}", "set"),
        ("set -{k,e}", "set"),
        ("set -o posi{1..2}", "set"),
    ],
)
def test_set_or_shopt_given_a_word_the_text_does_not_settle_is_not_read(text: str, builtin: str) -> None:
    # Any such word may be an option or an option's name, or, unquoted, split into several, so it may turn any on.
    bash_agreement.assert_reader_agrees([text])
    with pytest.raises(bash_syntax.NotRead, match=rf"^{builtin} is given a word the text does not settle"):
        bash_syntax.simple_commands(text)


def test_parse_reads_a_command_that_turns_on_a_refused_option_as_any_other() -> None:
    # The hub guard reads a tree by parse and follows no option's effect, so the tree stays Bash's reading up to the
    # option and past it.
    tree = parse("shopt -s extglob\ngit commit -m x")
    assert simple_commands(tree) == [["shopt", "-s", "extglob"], ["git", "commit", "-m", "x"]]


# How Bash runs each script, from an empty folder, with the option turned on and turned off, showing that it reads or
# runs the text after the option otherwise; each where the runner's Bash has the option, or, for a variable, from the
# release that reads it, with the variable assigned and not.
COMPAT_PROBE = ('[[ a =~ "." ]] && echo matched', "matched\n", "")
OPTION_PROBES: dict[str, tuple[str, str, str, str]] = {
    "set -o keyword": ("set -k", "printf '%s\\n' a=b last", "last\n", "a=b\nlast\n"),
    "set -o histexpand": (
        "set -o history -o histexpand",
        "set -o history\necho marker\n!!",
        "marker\nmarker\n",
        "marker\n",
    ),
    "set -o posix": ("set -o posix", "alias e='echo aliased'\ne", "aliased\n", ""),
    # A carriage return inside a quoted word, measured by the word's length, which no output path can lose.
    "set -o igncr": ("set -o igncr", "v='x\ry'\nprintf '%s' \"${#v}\"", "2", "2"),
    "shopt expand_aliases": ("shopt -s expand_aliases", "alias e='echo aliased'\ne", "aliased\n", ""),
    "shopt extglob": ("shopt -s extglob", "!(echo marker)", "", "marker\n"),
    "shopt compat31": ("shopt -s compat31", *COMPAT_PROBE),
    **{
        f"shopt compat{level}": (
            f"shopt -s compat{level}",
            f'[[ "${{BASH_COMPAT-}}" == {level} ]] && echo level',
            "level\n",
            "",
        )
        for level in ("32", "40", "41", "42", "43", "44")
    },
}
VARIABLE_PROBES = {
    "POSIXLY_CORRECT": ((3, 2), "POSIXLY_CORRECT=1", "alias e='echo aliased'\ne", "aliased\n", ""),
    "BASH_COMPAT": ((4, 3), "BASH_COMPAT=31", *COMPAT_PROBE),
}


def run_probe(folder: Path, text: str) -> str:
    """What the runner's Bash prints to its standard output running text as a script from an empty folder."""
    empty = folder / "empty"
    empty.mkdir(exist_ok=True)
    script = folder / "probe.sh"
    script.write_bytes(f'cd "$1" || exit 1\n{text}\n'.encode())
    result = platform_support.run_tool(
        [bash_agreement.find_bash(), platform_support.normalize(script), platform_support.normalize(empty)],
        separate_errors=True,
    )
    return result.output


def test_every_refused_option_has_a_probe() -> None:
    assert sorted(OPTION_PROBES) == sorted(bash_syntax.OPTIONS_REFUSED)
    assert sorted(VARIABLE_PROBES) == sorted(bash_syntax.OPTION_VARIABLES)


# The refused options under which the only Bash that has them reads the probe the same whatever their setting, each
# with why it stays refused.
SAME_READING_ON_THE_RUNNER = {
    "set -o igncr": (
        "Git Bash, the one Bash with igncr, drops every carriage return it reads whatever igncr's setting, as the "
        "word's length shows; the refusal stands for a build that keeps them, as Cygwin's documents igncr"
    ),
}


@pytest.mark.parametrize("key", sorted(OPTION_PROBES))
def test_bash_reads_or_runs_the_text_after_a_refused_option_otherwise(key: str, tmp_path: Path) -> None:
    turn_on, text, on, off = OPTION_PROBES[key]
    assert (on == off) == (key in SAME_READING_ON_THE_RUNNER)
    if key not in runner_options()[1]:
        # The runner's Bash has no such option, so it refuses to turn it on and reads the text as at its default.
        result = platform_support.run_tool([bash_agreement.find_bash(), "-c", turn_on], separate_errors=True)
        assert result.returncode != 0, result.output
        return
    name = bash_syntax.option_name(key)
    turn_off = f"set +o {name}" if set_option(key) else f"shopt -u {name}"
    assert (run_probe(tmp_path, f"{turn_on}\n{text}"), run_probe(tmp_path, f"{turn_off}\n{text}")) == (on, off)


@pytest.mark.parametrize("variable", sorted(VARIABLE_PROBES))
def test_bash_reads_or_runs_the_text_after_assigning_an_option_s_variable_otherwise(
    variable: str, tmp_path: Path
) -> None:
    since, assignment, text, on, off = VARIABLE_PROBES[variable]
    expected = (on, off) if runner_options()[0] >= since else (off, off)
    assert (run_probe(tmp_path, f"{assignment}\n{text}"), run_probe(tmp_path, text)) == expected


# A corpus of the forms an option might change Bash's reading of, defined as a function after an alias whose name it
# uses, and the read options Bash cannot print its reading under, each with why.
CORPUS = r"""alias e='echo aliased'
f() {
  e one # a comment $(echo commented)
  echo "${x:-'}'}" "${x:-$'}'}" '${y}' "$(echo sub)" `echo back` $'a\tb' "$((1 + 2))"
  !(echo negated)
  time -p echo timed
  echo {a,b} *.none ~ a=b
  x=1 y[0]=2 echo assigned
  case a in a) echo case ;; esac
  [[ a == a && -n x ]] && echo condition
  cat <<EOF
body $(echo body)
EOF
}"""
CORPUS_UNPRINTED = {
    "set -o noexec": "runs nothing more, declare -f included",
    "set -o onecmd": "ends the shell after the next command, before declare -f",
}


def corpus_readings(folder: Path, options: list[str]) -> dict[str, str]:
    """Bash's reading of CORPUS, as `declare -f` prints it, with no option set, keyed "", and with each option of
    options turned on and off, keyed by the option and its setting."""
    corpus = folder / "corpus.sh"
    corpus.write_bytes(CORPUS.encode())
    script = r"""corpus=$(< "$1"); shift
read_corpus() { eval "$corpus"; declare -f f; }
echo "== "; ( read_corpus )
for option in "$@"; do
  kind=${option%% *} name=${option##* }
  for setting in on off; do
    echo "== $option $setting"
    (
      if [[ $kind == set ]]; then
        if [[ $setting == on ]]; then set -o "$name"; else set +o "$name"; fi
      elif [[ $setting == on ]]; then shopt -s "$name"; else shopt -u "$name"; fi
      read_corpus
    ) 2> /dev/null
  done
done"""
    result = platform_support.run_tool(
        [bash_agreement.find_bash(), "-c", script, "corpus", platform_support.normalize(corpus), *options]
    )
    readings: dict[str, str] = {}
    for section in result.output.split("== ")[1:]:
        heading, _, reading = section.partition("\n")
        readings[heading] = reading
    return readings


def test_bash_reads_the_same_commands_whatever_a_read_option_s_setting(tmp_path: Path) -> None:
    probed = sorted(bash_syntax.OPTIONS_READ - CORPUS_UNPRINTED.keys())
    readings = corpus_readings(tmp_path, [*probed, "shopt extglob", "shopt expand_aliases", "set -o posix"])
    assert "aliased" not in readings[""]
    assert "negated" in readings[""]
    changed = [
        f"{key} {setting}"
        for key in probed
        for setting in ("on", "off")
        if readings[f"{key} {setting}"] != readings[""]
    ]
    assert changed == []
    # The corpus shows a reading the refused options change.
    for key in ("shopt extglob", "shopt expand_aliases", "set -o posix"):
        assert readings[f"{key} on"] != readings[""], key
    assert sorted(CORPUS_UNPRINTED.keys() - bash_syntax.OPTIONS_READ) == []


# Bash's expansion and quoting forms as the newest Bash's manual names them, in bash(1)'s EXPANSION and QUOTING, keyed
# as bash_syntax keys them, and `$[expression]`, which Bash 3.2's manual names and every Bash since still reads, pinned
# so a runner whose Bash is older, as macOS's /bin/bash 3.2, which takes no `@` transformation, still holds the reader
# to every one.
PINNED_EXPANSIONS = frozenset(
    {"$parameter", "${parameter}", "${parameter:-word}", "${parameter-word}", "${parameter:=word}"}
    | {"${parameter=word}", "${parameter:?word}", "${parameter?word}", "${parameter:+word}", "${parameter+word}"}
    | {"${parameter:offset}", "${parameter:offset:length}", "${!prefix*}", "${!prefix@}", "${!name[@]}"}
    | {"${!name[*]}", "${#parameter}", "${parameter#word}", "${parameter##word}", "${parameter%word}"}
    | {"${parameter%%word}", "${parameter/pattern/string}", "${parameter//pattern/string}"}
    | {"${parameter/#pattern/string}", "${parameter/%pattern/string}", "${parameter^pattern}"}
    | {"${parameter^^pattern}", "${parameter,pattern}", "${parameter,,pattern}", "${!parameter}"}
    | {f"${{parameter@{operator}}}" for operator in "UuLQEPAKak"}
    | {"$(command)", "`command`", "$(< file)", "${ command;}", "${| command;}", "$((expression))", "$[expression]"}
    | {"<(list)", ">(list)", "{string,string}", "{x..y}", "{x..y..incr}", "~", "~login", "~+", "~-", "~N", "~+N"}
    | {"~-N", "*", "?", "[...]", "$'string'", '$"string"'}
)
# The keys of bash_syntax's tables that name an expansion form, the rest naming a reserved word or an option.
EXPANSION_KEY = re.compile(r"[$`<>~*?].*|\{.+\}|\[\.\.\.\]", re.DOTALL)


def expansion_keys(table: object) -> set[str]:
    """The keys of a table of bash_syntax's that name an expansion form."""
    assert isinstance(table, (dict, frozenset))
    return {key for key in table if EXPANSION_KEY.fullmatch(key)}


@functools.cache
def runner_transformations() -> tuple[tuple[int, int], frozenset[str]]:
    """The validated Bash's release, as its major and minor numbers, and the `${parameter@operator}` forms it takes:
    each letter after `@` that it does not refuse as a bad substitution."""
    script = r"""echo "${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"
x=a
for letter in {A..Z} {a..z}; do
  if ( eval ": \"\${x@$letter}\"" ) 2> /dev/null; then echo "$letter"; fi
done"""
    result = platform_support.run_tool([bash_agreement.find_bash(), "-c", script], separate_errors=True)
    assert result.returncode == 0, result.errors
    major, minor, *letters = result.output.split()
    return (int(major), int(minor)), frozenset(f"${{parameter@{letter}}}" for letter in letters)


def decided_expansions() -> set[str]:
    decided = bash_syntax.EXPANSIONS_READ | bash_syntax.EXPANSIONS_REFUSED.keys()
    return set(decided) | expansion_keys(bash_syntax.NOT_READ)


def test_every_expansion_form_bash_names_is_read_refused_or_not_read_with_a_reason() -> None:
    catalogue = PINNED_EXPANSIONS | runner_transformations()[1]
    tables = (bash_syntax.EXPANSIONS_READ, bash_syntax.EXPANSIONS_REFUSED, expansion_keys(bash_syntax.NOT_READ))
    undecided = sorted(form for form in catalogue if sum(form in table for table in tables) != 1)
    assert undecided == [], (
        "decide each in exactly one of bash_syntax.EXPANSIONS_READ, EXPANSIONS_REFUSED, and NOT_READ, with the "
        "reason, add it to PINNED_EXPANSIONS, and give it a sample in FORM_SAMPLES"
    )
    assert sorted(runner_transformations()[1] - PINNED_EXPANSIONS) == [], "pin each in PINNED_EXPANSIONS"


def test_a_bash_as_new_as_the_pin_takes_every_pinned_transformation() -> None:
    release, forms = runner_transformations()
    if release >= PINNED_FROM_BASH:
        pinned = {form for form in PINNED_EXPANSIONS if form.startswith("${parameter@")}
        assert sorted(pinned - forms) == [], (
            f"Bash {release} no longer takes these: remove each from PINNED_EXPANSIONS, FORM_SAMPLES, and "
            "bash_syntax's tables, and pin PINNED_FROM_BASH to this release"
        )


def test_every_expansion_form_the_reader_decides_is_one_bash_names() -> None:
    assert sorted(decided_expansions() - PINNED_EXPANSIONS - runner_transformations()[1]) == [], (
        "remove each: no Bash names it"
    )
    other_keys = frozenset(bash_syntax.OPTIONS_READ | bash_syntax.OPTIONS_REFUSED.keys() | bash_syntax.RESERVED)
    assert sorted(expansion_keys(other_keys)) == [], "no reserved word or option is keyed as an expansion form"


# Runs each text from a file of its own, in a subshell of its own, from an empty folder, with `s` set to abc, `a` an
# array, `u` unset, and `m` recording each marker it is given, so a text's markers are the commands Bash runs for it;
# what a text's own subshell returns is no verdict, so the run succeeds once it reaches its end.
RUN_EACH = r"""folder=$1
cd "$folder/empty" || exit 1
m() { printf '%s %s\n' "$index" "$1" >> "$folder/marks"; }
for ((index = 0; index < $2; index++)); do
  IFS= read -r -d '' text < "$folder/$index.sh"
  ( s=abc a=(abc def); unset u; eval "$text"; wait ) < /dev/null > /dev/null 2>&1
done
exit 0"""


def bash_runs(folder: Path, texts: list[str]) -> list[set[str]]:
    """The markers the runner's Bash runs `m` with for each text, one Bash start for them all."""
    (folder / "empty").mkdir()
    for index, text in enumerate(texts):
        (folder / f"{index}.sh").write_bytes(text.encode())
    result = platform_support.run_tool(
        [bash_agreement.find_bash(), "-c", RUN_EACH, "run-each", platform_support.normalize(folder), str(len(texts))]
    )
    assert result.returncode == 0, result.output
    ran: list[set[str]] = [set() for _ in texts]
    marks = folder / "marks"
    for line in marks.read_text(encoding="utf-8").splitlines() if marks.exists() else []:
        number, _, mark = line.partition(" ")
        ran[int(number)].add(mark)
    return ran


def listed_marks(text: str) -> set[str] | BashSyntaxError:
    """The markers of the `m` commands the reader lists for text, or the refusal it raises."""
    try:
        commands = bash_syntax.simple_commands(text)
    except BashSyntaxError as error:
        return error
    return {words[1] for words in map(bash_syntax.command_words, commands) if words[:1] == ["m"] and len(words) > 1}


ALL_RELEASES = (3, 2)
# A text for each form Bash's manual names: a command substitution running `m` in each of its places that holds a
# command, or, for a form that holds none, after it in the same word, with the markers Bash runs for it from the release
# named on; for a form the reader does not read, the command Bash runs that no reader of the text lists.
FORM_SAMPLES: dict[str, tuple[str, set[str], tuple[int, int]]] = {
    "$parameter": ("echo $s$(m 1)", {"1"}, ALL_RELEASES),
    "${parameter}": ("echo ${s}$(m 1)", {"1"}, ALL_RELEASES),
    "${parameter:-word}": ("echo ${u:-$(m 1)}", {"1"}, ALL_RELEASES),
    "${parameter-word}": ("echo ${u-$(m 1)}", {"1"}, ALL_RELEASES),
    "${parameter:=word}": ("echo ${u:=$(m 1)}", {"1"}, ALL_RELEASES),
    "${parameter=word}": ("echo ${u=$(m 1)}", {"1"}, ALL_RELEASES),
    "${parameter:?word}": ("echo ${u:?$(m 1)}", {"1"}, ALL_RELEASES),
    "${parameter?word}": ("echo ${u?$(m 1)}", {"1"}, ALL_RELEASES),
    "${parameter:+word}": ("echo ${s:+$(m 1)}", {"1"}, ALL_RELEASES),
    "${parameter+word}": ("echo ${s+$(m 1)}", {"1"}, ALL_RELEASES),
    "${parameter:offset}": ("echo ${s:$(m 1)0}", {"1"}, ALL_RELEASES),
    "${parameter:offset:length}": ("echo ${s:$(m 1)0:$(m 2)1}", {"1", "2"}, ALL_RELEASES),
    "${!prefix*}": ("echo ${!s*}$(m 1)", {"1"}, ALL_RELEASES),
    "${!prefix@}": ("echo ${!s@}$(m 1)", {"1"}, ALL_RELEASES),
    "${!name[@]}": ("echo ${!a[@]}$(m 1)", {"1"}, ALL_RELEASES),
    "${!name[*]}": ("echo ${!a[*]}$(m 1)", {"1"}, ALL_RELEASES),
    "${#parameter}": ("echo ${#s}$(m 1)", {"1"}, ALL_RELEASES),
    "${parameter#word}": ("echo ${s#$(m 1)a}", {"1"}, ALL_RELEASES),
    "${parameter##word}": ("echo ${s##$(m 1)a}", {"1"}, ALL_RELEASES),
    "${parameter%word}": ("echo ${s%$(m 1)c}", {"1"}, ALL_RELEASES),
    "${parameter%%word}": ("echo ${s%%$(m 1)c}", {"1"}, ALL_RELEASES),
    "${parameter/pattern/string}": ("echo ${s/$(m 1)a/$(m 2)}", {"1", "2"}, ALL_RELEASES),
    "${parameter//pattern/string}": ("echo ${s//$(m 1)a/$(m 2)}", {"1", "2"}, ALL_RELEASES),
    "${parameter/#pattern/string}": ("echo ${s/#$(m 1)a/$(m 2)}", {"1", "2"}, ALL_RELEASES),
    "${parameter/%pattern/string}": ("echo ${s/%$(m 1)c/$(m 2)}", {"1", "2"}, ALL_RELEASES),
    "${parameter^pattern}": ("echo ${s^$(m 1)a}", {"1"}, (4, 0)),
    "${parameter^^pattern}": ("echo ${s^^$(m 1)a}", {"1"}, (4, 0)),
    "${parameter,pattern}": ("echo ${s,$(m 1)a}", {"1"}, (4, 0)),
    "${parameter,,pattern}": ("echo ${s,,$(m 1)a}", {"1"}, (4, 0)),
    **{
        f"${{parameter@{operator}}}": (f"echo ${{s@{operator}}}$(m 1)", {"1"}, since)
        for operators, since in (("QEAa", (4, 4)), ("UuLK", (5, 1)), ("k", (5, 2)))
        for operator in operators
    },
    "${parameter@P}": ("p='$(m 1)'; echo ${p@P}", {"1"}, (4, 4)),
    "${!parameter}": ("v='a[$(m 1)]'; echo ${!v}", {"1"}, ALL_RELEASES),
    "$(command)": ("echo $(m 1)", {"1"}, ALL_RELEASES),
    "`command`": ("echo `m 1`", {"1"}, ALL_RELEASES),
    "$(< file)": ("echo $(< /dev/null)$(m 1)", {"1"}, ALL_RELEASES),
    "${ command;}": ("echo ${ m 1; }", {"1"}, (5, 3)),
    "${| command;}": ("echo ${| m 1; }", {"1"}, (5, 3)),
    "$((expression))": ("echo $(( $(m 1)1 ))", {"1"}, ALL_RELEASES),
    "$[expression]": ("echo $[ $(m 1)1 ]", {"1"}, ALL_RELEASES),
    "<(list)": ("cat <(m 1)", {"1"}, ALL_RELEASES),
    # Bash 3.2 does not wait for a process substitution.
    ">(list)": ("echo > >(m 1)", {"1"}, (4, 4)),
    "{string,string}": ("echo {a,$(m 1)}", {"1"}, ALL_RELEASES),
    "{x..y}": ("echo {1..2}$(m 1)", {"1"}, ALL_RELEASES),
    "{x..y..incr}": ("echo {1..4..2}$(m 1)", {"1"}, (4, 0)),
    "~": ("echo ~/$(m 1)", {"1"}, ALL_RELEASES),
    "~login": ("echo ~root/$(m 1)", {"1"}, ALL_RELEASES),
    "~+": ("echo ~+/$(m 1)", {"1"}, ALL_RELEASES),
    "~-": ("echo ~-/$(m 1)", {"1"}, ALL_RELEASES),
    "~N": ("echo ~0/$(m 1)", {"1"}, ALL_RELEASES),
    "~+N": ("echo ~+0/$(m 1)", {"1"}, ALL_RELEASES),
    "~-N": ("echo ~-0/$(m 1)", {"1"}, ALL_RELEASES),
    "*": ("echo *$(m 1)", {"1"}, ALL_RELEASES),
    "?": ("echo ?$(m 1)", {"1"}, ALL_RELEASES),
    "[...]": ("echo [a]$(m 1)", {"1"}, ALL_RELEASES),
    "$'string'": ("echo $'\\''$(m 1)", {"1"}, ALL_RELEASES),
    '$"string"': ('echo $"\\"$(m 1)"', {"1"}, ALL_RELEASES),
}


def test_every_expansion_form_has_a_sample() -> None:
    assert sorted(FORM_SAMPLES) == sorted(PINNED_EXPANSIONS)


def test_bash_runs_each_command_the_reader_lists_in_each_expansion_form_and_no_other(tmp_path: Path) -> None:
    release = runner_transformations()[0]
    keys = sorted(FORM_SAMPLES)
    failures = []
    for key, ran in zip(keys, bash_runs(tmp_path, [FORM_SAMPLES[key][0] for key in keys]), strict=True):
        text, expected, since = FORM_SAMPLES[key]
        if release >= since and not expected <= ran:
            failures.append(f"{key}: Bash {release} does not run {sorted(expected - ran)} for {text!r}")
        listed = listed_marks(text)
        if key in bash_syntax.EXPANSIONS_READ and (not isinstance(listed, set) or not ran | expected <= listed):
            failures.append(f"{key}: the reader lists {listed!r} for {text!r}, where Bash runs {sorted(ran)}")
        elif key in bash_syntax.EXPANSIONS_REFUSED and not isinstance(listed, bash_syntax.NotRead):
            failures.append(f"{key}: the reader does not refuse {text!r}: {listed!r}")
        elif key in bash_syntax.NOT_READ and (not isinstance(listed, set) or not expected - listed):
            failures.append(f"{key}: the reader lists {listed!r} for {text!r}, which Bash runs as the reason says")
    assert failures == []


# The places of a `${ }` form that hold text Bash expands, each with the text a generated row gives it when another
# place holds the payload.
SLOTS = {"word": "a", "pattern": "a", "string": "b", "offset": "0", "length": "1"}
# The payloads a generated row puts in a place: each quoting form that may hold a `}` Bash reads as quoted, and each
# way to hold a substitution, which Bash may run; and the text after a payload that may hold a `}`, inside the `${ }`,
# which Bash reads as a double-quoted string after a quoted `}` and the reader once read as quoted text after the `}`
# that ended its expansion, hiding the substitution inside it.
CLOSING_PAYLOADS = ("'}'", '"}"', '$"}"', "\\}", "${s:+'}'}", "$(: })", "`: }`", "$'}'", "$'\\'}'")
RUNNING_PAYLOADS = ("'$(m 2)'", '"$(m 2)"', "$(m 2)", "$'\\x24(m 2)'")
AFTER_A_CLOSE = "\"'$(m 3)'\""
# Where a generated row stands, its expansion written as EXPANSION, with text after it in the same word that runs a
# substitution or quotes one; and the forms tried in the contexts that read the expansion as a double-quoted string
# does and in arithmetic, each kind of place once.
CONTEXTS = ("echo EXPANSION'$(m 4)'\"$(m 5)\"", "echo \"EXPANSION'$(m 4)'\"'$(m 5)'")
DOUBLE_QUOTED_CONTEXTS = ("cat <<EOF\nEXPANSION'$(m 4)'\nEOF", "echo $(( EXPANSION ))")
EVERY_CONTEXT_FORMS = frozenset(
    {"${parameter:-word}", "${parameter#word}", "${parameter/pattern/string}", "${parameter:offset:length}"}
)


def generated_rows() -> list[tuple[str, str]]:
    """Each `${ }` form the reader reads, its every place holding each payload, in each context: the payload and the
    text. The parameter is `u`, unset, for a form that expands its word when the parameter is unset, and `s`
    otherwise."""
    rows: list[tuple[str, str]] = []
    for key in sorted(bash_syntax.EXPANSIONS_READ):
        places = [slot for slot in SLOTS if slot in key]
        if not key.startswith("${") or not places:
            continue
        parameter = "u" if re.search(r"parameter:?[-=?]", key) else "s"
        contexts = CONTEXTS + (DOUBLE_QUOTED_CONTEXTS if key in EVERY_CONTEXT_FORMS else ())
        payloads = [payload + after for payload in CLOSING_PAYLOADS for after in ("", AFTER_A_CLOSE)]
        for place in places:
            for payload in [*payloads, *RUNNING_PAYLOADS]:
                expansion = key.replace("parameter", parameter)
                for slot, filler in SLOTS.items():
                    expansion = expansion.replace(slot, payload if slot == place else filler)
                rows.extend((payload, context.replace("EXPANSION", expansion)) for context in contexts)
    return rows


def test_every_command_bash_runs_in_a_generated_expansion_row_is_one_the_reader_lists(tmp_path: Path) -> None:
    rows = generated_rows()
    texts = [text for _, text in rows]
    hidden, refused = [], []
    for (payload, text), ran in zip(rows, bash_runs(tmp_path, texts), strict=True):
        listed = listed_marks(text)
        if isinstance(listed, bash_syntax.NotRead):
            # Only an ANSI-C string that spells what Bash reads as syntax there is refused.
            if not payload.startswith("$'"):
                refused.append(f"{text!r}: {listed}")
        elif isinstance(listed, set) and not ran <= listed:
            hidden.append(f"{text!r}: Bash runs {sorted(ran)}, the reader lists {sorted(listed)}")
    assert hidden == []
    assert refused == []
    # Bash and the reader agree on reading every row the reader does not refuse as unread, but where Bash 3.2 ends a
    # `${ }` at the `}` a command substitution in it holds, a limit bash_syntax states.
    bash_3_2 = runner_transformations()[0] < (4, 0)
    bash_agreement.assert_reader_agrees(
        text
        for payload, text in rows
        if not isinstance(listed_marks(text), bash_syntax.NotRead) and not (bash_3_2 and payload.startswith("$(: }"))
    )


def test_a_quote_inside_a_double_quoted_expansion_ends_no_expansion_so_the_command_after_it_is_read() -> None:
    # Bash runs `echo HIDDEN`, since the single quotes inside the double-quoted `${ }` quote its `}`.
    text = "x=; echo \"${x:-'}'\"'$(echo HIDDEN)'\"}\""
    parse(text)
    assert simple_commands_of(text) == [["x="], ["echo", "\"${x:-'}'\"'$(echo HIDDEN)'\"}\""], ["echo", "HIDDEN"]]


@pytest.mark.parametrize(
    "text",
    [
        "echo \"${x:-$'}'\"'$(git push)'\"}\"",
        "echo \"${x:-$'\\x24(git push)'}\"",
        "echo \"${x#$'\\''}\"",
        "cat <<EOF\n${x:-$'\\x22'}\nEOF\n",
        "echo $(( ${x:-$'\\x24(git push)'} ))",
    ],
)
def test_an_ansi_c_string_in_a_double_quoted_expansion_spelling_what_bash_reads_as_syntax_is_not_read(
    text: str,
) -> None:
    with pytest.raises(bash_syntax.NotRead, match=r"^an ANSI-C string inside a double-quoted `\$\{ \}` spells"):
        bash_syntax.simple_commands(text)


@pytest.mark.parametrize(
    "text",
    ["echo ${x:$'\\x24(git push)'}", "echo $(( $'\\x24(git push)' ))", "(( $'\\140git push\\140' ))"],
)
def test_an_ansi_c_string_bash_expands_as_double_quoted_text_spelling_a_substitution_is_not_read(text: str) -> None:
    with pytest.raises(bash_syntax.NotRead, match=r"^an ANSI-C string Bash expands as a double-quoted string"):
        bash_syntax.simple_commands(text)


@pytest.mark.parametrize(
    "text",
    [
        "echo \"${x:-$'\\n'}\"",
        "echo \"${x:-$'X'}\"",
        "echo ${x:-$'\\x24(git push)'}",
        'echo "${x:-"$\'\\x24(git push)\'"}"',
        "echo $'\\x24(git push)'",
        "echo $(( $'1' ))",
    ],
)
def test_an_ansi_c_string_bash_quotes_or_whose_text_bash_reads_as_text_is_read(text: str) -> None:
    parse(text)
    assert [words[0] for words in simple_commands_of(text)] == ["echo"]


def test_bash_runs_what_an_ansi_c_string_in_a_double_quoted_expansion_spells_with_extquote_on_and_not_off(
    tmp_path: Path,
) -> None:
    # The refusal stands under either setting: with extquote off Bash runs the substitution after the `$'}'`, which
    # the `}` it spells ends the expansion before when it is on, and with extquote on it runs what `\x24` spells.
    after = "echo \"${u:-$'}'\"'$(m 1)'\"}\"\necho \"${u:-$'\\x24(m 2)'}\""
    ran = bash_runs(tmp_path, [f"shopt -s extquote\n{after}", f"shopt -u extquote\n{after}"])
    assert ran == [{"2"}, {"1"}]


@pytest.mark.parametrize(
    "text",
    [
        "echo ${ git push; }",
        "echo ${\tgit push; }",
        "echo ${\ngit push\n}",
        "echo ${| git push; }",
        'echo "${ git push; }"',
        "echo ${x:-${ git push; }}",
        "echo $(( ${ git push; } ))",
        "(( ${ git push; } ))",
        "cat <<EOF\n${ git push; }\nEOF\n",
    ],
)
def test_a_command_substitution_run_in_the_current_shell_is_not_read_wherever_it_stands(text: str) -> None:
    with pytest.raises(bash_syntax.NotRead, match=r"^Bash 5\.3 runs the command after `\$\{"):
        bash_syntax.simple_commands(text)


def test_deprecated_arithmetic_is_one_word_whose_substitutions_are_read() -> None:
    text = "echo $[ 1 + $(git push) ] x"
    parse(text)
    [command] = [command for command in bash_syntax.simple_commands(text) if command.words[0].raw == "echo"]
    assert [word.raw for word in command.words] == ["echo", "$[ 1 + $(git push) ]", "x"]
    assert command.words[1].parts == (bash_syntax.Part("dynamic", "$["),)
    assert bash_syntax.command_words(command, "%unread%") == ["echo", "%unread%", "x"]
    assert simple_commands_of(text)[1:] == [["git", "push"]]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("echo ${x:'$(git push)'}", [["echo", "${x:'$(git push)'}"], ["git", "push"]]),
        ("echo ${x:0:'`git push`'}", [["echo", "${x:0:'`git push`'}"], ["git", "push"]]),
        ("echo \"${x:-'$(git push)'}\"", [["echo", "\"${x:-'$(git push)'}\""], ["git", "push"]]),
        ("echo \"${x/a/'$(git push)'}\"", [["echo", "\"${x/a/'$(git push)'}\""], ["git", "push"]]),
        ("echo ${x:-'$(git push)'}", [["echo", "${x:-'$(git push)'}"]]),
        ("echo \"${x#'$(git push)'}\"", [["echo", "\"${x#'$(git push)'}\""]]),
        ("echo \"${x^^'$(git push)'}\"", [["echo", "\"${x^^'$(git push)'}\""]]),
    ],
)
def test_a_substitution_in_single_quotes_is_read_where_bash_expands_the_word_as_double_quoted_text(
    text: str, expected: list[list[str]]
) -> None:
    parse(text)
    assert simple_commands_of(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "echo $(( $$'\\x24(git push)' ))",
        "echo ${x:-$$'\\x24(git push)'}",
        "cat <<EOF\n${x:0:$$'\\x24(git push)'}\nEOF\n",
        "echo \"${x%%$$'`git push`'}\"",
    ],
)
def test_a_process_id_before_a_quote_bash_may_read_as_an_ansi_c_string_spelling_syntax_is_not_read(text: str) -> None:
    # Bash 3.2, and Bash 5.3 in a here-document's body, read the `$'` after `$$` here as an ANSI-C string and run what
    # it spells; Bash 4 and later elsewhere read `$$` whole and the quote as plain.
    with pytest.raises(bash_syntax.NotRead):
        bash_syntax.simple_commands(text)


@pytest.mark.parametrize("text", ["echo $(( $$ + 1 ))", "echo \"${x:-$$'a'}\"", "echo ${x:-$$'b'}", "echo $$'c'"])
def test_a_process_id_before_a_quote_whose_readings_agree_is_read(text: str) -> None:
    parse(text)
    assert simple_commands_of(text) == [["echo", text.removeprefix("echo ")]]


@pytest.mark.parametrize(
    "text",
    [
        "git commit -m x; echo ${ :; }",
        "git commit -m x; echo ${| :; }",
        "git commit -m x; echo \"${x:-$'\\x24'}\"",
        "git commit -m x; echo $(( $'\\x24' ))",
        "git commit -m x; echo ${x:$$'\\a'}",
        'git commit -m x; y=( "${ :; }" )',
    ],
)
def test_parse_reads_a_refused_form_and_the_commands_beside_it_and_the_walk_refuses_it(text: str) -> None:
    # parse reads the tree as Bash does, so a reader by parse, as the hub guard, still reads the git call beside the
    # form; the form's refusal rides on its word, which simple_commands and every_command raise.
    commands = bash_agreement.simple_commands(bash_syntax.parse(text))
    assert [word.raw for word in commands[0].words] == ["git", "commit", "-m", "x"]
    assert commands[1].words[-1].refusal
    with pytest.raises(bash_syntax.NotRead, match=re.escape(commands[1].words[-1].refusal)):
        bash_syntax.simple_commands(text)
    with pytest.raises(bash_syntax.NotRead):
        bash_syntax.every_command(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("((printf %s $'\\x24'); git commit -m x)", [["printf", "%s", "$'\\x24'"], ["git", "commit", "-m", "x"]]),
        (
            "echo $((printf %s $'\\x24'); git commit -m x)",
            [
                ["echo", "$((printf %s $'\\x24'); git commit -m x)"],
                ["printf", "%s", "$'\\x24'"],
                ["git", "commit", "-m", "x"],
            ],
        ),
    ],
)
def test_an_arithmetic_reading_bash_does_not_take_leaves_no_refusal(text: str, expected: list[list[str]]) -> None:
    # Bash reads these as subshells, not arithmetic, where the ANSI-C string spells `$` as plain text.
    parse(text)
    assert simple_commands_of(text) == expected


def test_a_command_substitution_that_sets_reply_is_refused_with_its_own_reason() -> None:
    with pytest.raises(bash_syntax.NotRead, match=r"^Bash 5\.3 runs the command after `\$\{\|` in the current shell"):
        bash_syntax.simple_commands("echo ${| git push; }")
    with pytest.raises(bash_syntax.NotRead, match=r"^Bash 5\.3 runs the command after `\$\{` and a blank"):
        bash_syntax.simple_commands("echo ${\\\n git push; }")


@pytest.mark.parametrize(
    "text",
    [
        "echo \"${a['$(git push)']#x}\"",
        "echo \"${a['$(git push)']%x}\"",
        "echo \"${a['$(git push)']^^}\"",
        "echo \"${a['$(git push)']@Q}\"",
        "echo \"${!a['$(git push)']}\"",
        "echo ${a['$(git push)']}",
        "echo ${#a['$(git push)']}",
        "cat <<E\n${a['$(git push)']#x}\nE\n",
        "echo $(( ${a['$(git push)']#x} ))",
        "echo \"${x:-${a['$(git push)']#x}}\"",
        "[[ -n \"${a['$(git push)']#x}\" ]]",
        "case \"${a['$(git push)']#x}\" in *) :;; esac",
        "for i in \"${a['$(git push)']#x}\"; do :; done",
        "y=( \"${a['$(git push)']#x}\" )",
        "a['$(git push)']=1",
        "b[${x:-'$(git push)'}]=1",
    ],
)
def test_a_substitution_in_single_quotes_in_a_subscript_is_read_as_bash_5_3_runs_it(text: str) -> None:
    # Bash 5.3 expands an indexed array's subscript as arithmetic, as a double-quoted string, wherever it stands.
    parse(text)
    assert ["git", "push"] in simple_commands_of(text)


@pytest.mark.parametrize(
    "text",
    [
        "echo $[ a[b[1]] + $(git push) ]",
        "echo $[ \\] + $(git push) ]",
        "echo $(( 1 \\) + $(git push) ))",
    ],
)
def test_arithmetic_reads_nested_brackets_and_escapes_as_bash_reads_them(text: str) -> None:
    parse(text)
    assert simple_commands_of(text)[1:] == [["git", "push"]]


def test_a_double_quoted_word_bash_expands_holding_a_substitution_the_reader_cannot_close_is_not_read() -> None:
    text = "echo \"${x:-'$(a'}\""
    parse(text)
    with pytest.raises(
        bash_syntax.NotRead, match=r"^a `\$\{ \}` Bash expands as a double-quoted string is not read \("
    ):
        bash_syntax.simple_commands(text)


@pytest.mark.parametrize(
    ("text", "spelled"),
    [
        ("\\x24", "$"),
        ("\\044", "$"),
        ("\\u0024", "$"),
        ("\\U00000060", "`"),
        ("\\U00110000", ""),
        ("\\\\", "\\"),
        ("\\'", "'"),
        ('\\"', '"'),
        ("\\n", "\x00"),
        ("\\E", "\x00"),
        ("\\cA", "\x00A"),
        ("\\q", "\\q"),
        # An escape Bash does not know, as `\X`, keeps its backslash, which a letter's escape does not.
        ("\\X", "\\X"),
        ("a}", "a}"),
    ],
)
def test_an_ansi_c_string_spells_each_character_bash_reads_its_escapes_as(text: str, spelled: str) -> None:
    assert bash_syntax.ansi_c_text(text) == spelled


@pytest.mark.parametrize(
    "text",
    [
        "a[$'\\x24(git commit -m x)']=1",
        "declare a[$'\\x24(git commit -m x)']=1",
        "declare A[$'\\x24(git commit -m x)']=1",
        "a=([$'\\x24(git commit -m x)']=1)",
        "a[$'\\x60git commit -m x\\x60']=1",
        "a[1+$'\\x24(git commit -m x)']=1",
    ],
)
def test_an_ansi_c_string_spelling_a_substitution_in_an_assignment_s_subscript_is_not_read(text: str) -> None:
    # Bash 5.3 expands the subscript as arithmetic, putting what the string spells there before it expands it.
    parse(text)
    with pytest.raises(bash_syntax.NotRead, match=r"^an ANSI-C string Bash expands as a double-quoted string"):
        bash_syntax.simple_commands(text)


def test_an_upper_case_name_is_read_as_a_lower_case_one_is(tmp_path: Path) -> None:
    # The forms the reader tells apart by a name's first character: a `${ ` Bash 5.3 runs in the current shell, and
    # a pattern in double quotes, whose single quotes quote.
    texts = ["echo ${X}$(m 1)", "echo \"${HOME#'$(m 2)'}\""]
    assert [listed_marks(text) for text in texts] == bash_runs(tmp_path, texts) == [{"1"}, set()]


@pytest.mark.parametrize("text", ["a[1]=$'\\x24(x)'", "declare a[$'\\n']=1", "a=([$'1']=1)", "echo $'\\x24(x)'"])
def test_an_ansi_c_string_outside_a_subscript_or_spelling_no_substitution_is_read(text: str) -> None:
    parse(text)
    bash_syntax.simple_commands(text)


@pytest.mark.parametrize(
    "text",
    [
        "echo ${x:-<(git commit -m x)}",
        "cat ${x:-<(git commit -m x)}",
        "echo ${x/<(git commit -m x)/y}",
        "x=${y:-<(git commit -m x)}",
        "echo ${x:->(git commit -m x)}",
        "echo ${x,,<(git commit -m x)}",
        "echo ${a[<(git commit -m x)]}",
        "x[<(git commit -m x)]=1",
    ],
)
def test_a_process_substitution_in_an_unquoted_expansion_s_word_is_read(text: str) -> None:
    parse(text)
    assert simple_commands_of(text)[-1] == ["git", "commit", "-m", "x"]


def test_a_process_substitution_in_a_double_quoted_expansion_s_word_is_text() -> None:
    text = 'echo "${x:-<(git commit -m x)}"'
    parse(text)
    assert simple_commands_of(text) == [["echo", text.removeprefix("echo ")]]


def test_a_double_quoted_word_read_again_holding_a_substitution_it_cannot_close_is_not_read() -> None:
    # The first reading skips the escaped `$`, so it closes the expansion; read again as Bash expands the word, a
    # substitution starts there that never closes.
    text = 'echo "${x:-\\$(a}"'
    parse(text)
    with pytest.raises(
        bash_syntax.NotRead, match=r"^a `\$\{ \}` Bash expands as a double-quoted string is not read \("
    ):
        bash_syntax.simple_commands(text)


def test_a_substitution_in_a_nested_expansion_s_quotes_is_read_where_the_outer_word_is_read_again() -> None:
    # Bash 3.2 runs the first substitution, inside the nested pattern's single quotes, as it expands the subscript.
    text = "(( a[${a['${!x,,'$(git push)'$}\\'$(git status)],,:}] ))"
    parse(text)
    assert ["git", "push"] in simple_commands_of(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Bash reads arithmetic by its parentheses and quotes alone, so a `${ }` that never closes is refused only as it
        # is expanded, and the next line runs.
        ("(( ${x/'}'\\} ))\ngit push", [["git", "push"]]),
        ("echo $(( ${x/'}'\\} ))\ngit push", [["echo", "$(( ${x/'}'\\} ))"], ["git", "push"]]),
        # A `)` in a `${ }` closes the arithmetic, so Bash reads the two subshells instead.
        ("(( ${x:-)} )); git push", [["${x:-)}"], ["git", "push"]]),
        # Bash 3.2 runs a process substitution in arithmetic.
        ("(( ${x[<(git push)]} ))", [["git", "push"]]),
        # Bash counts a `$[`'s bracket in arithmetic as any other, so an unclosed one there opens nothing.
        ("for (( i=${x/$[}[$((git push) )1+]; 0; )); do :; done", [["git", "push"], [":"]]),
    ],
)
def test_arithmetic_is_read_by_its_parentheses_and_quotes_alone_as_bash_reads_it(
    text: str, expected: list[list[str]]
) -> None:
    parse(text)
    assert simple_commands_of(text) == expected


@pytest.mark.parametrize(
    "text",
    ["(( ${x/a/$$'$\"$['}}'$(git push)' ))", "(( ${x##'$(($'}'$(git push)'\"$(git status)\" ))"],
)
def test_a_single_quoted_string_in_arithmetic_ends_at_its_quote_before_any_substitution_in_it_is_read(
    text: str,
) -> None:
    # Bash reads single-quoted text in arithmetic as opaque and then expands what it holds, running the `$(git push)`
    # after it; a reading that took the `$[` or `$((` inside for an opening ran past the quote and missed it.
    parse(text)
    with pytest.raises(bash_syntax.NotRead):
        bash_syntax.simple_commands(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("cat <<E\n${#a[<(git push)%]#} x\nE\n", [["cat"], ["git", "push"]]),
        ('echo "${a[<(git push)]}"', [["echo", '"${a[<(git push)]}"'], ["git", "push"]]),
        ("echo $(( '$(git push)' ))", [["echo", "$(( '$(git push)' ))"], ["git", "push"]]),
    ],
)
def test_a_process_substitution_bash_3_2_runs_in_a_subscript_and_a_substitution_in_arithmetic_s_quotes_are_read(
    text: str, expected: list[list[str]]
) -> None:
    parse(text)
    assert simple_commands_of(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "echo \"${x:-'${ git push; }'}\"",
        "echo ${x:'${ git push; }'}",
        "(( a['${| git push; }'] ))",
        "echo $(( '${ git push; }' ))",
        "a['${ git push; }']=1",
        "cat <<E\n${x:'${ git push; }'}\nE\n",
        "echo $((echo ${x:'${ git push; }'}) )",
    ],
)
def test_a_command_substitution_run_in_the_current_shell_in_text_bash_reads_again_is_not_read(text: str) -> None:
    # Bash 5.3 runs it as it expands the word, the single quotes around it being text there.
    with pytest.raises(bash_syntax.NotRead, match=r"^Bash 5\.3 runs the command after `\$\{"):
        bash_syntax.simple_commands(text)


@pytest.mark.parametrize("text", ["a[$$'\\x24(git push)']=1", "declare a[x[1]$'\\x24(git push)']=1"])
def test_a_quoted_string_bash_may_read_as_ansi_c_in_an_assignment_s_subscript_is_not_read(text: str) -> None:
    with pytest.raises(bash_syntax.NotRead):
        bash_syntax.simple_commands(text)


def test_a_bare_case_pattern_bash_reads_again_in_a_double_quoted_word_is_marked_on_the_refusal() -> None:
    # The substitution stands between the word's single quotes, which only the reading again sees through.
    text = "echo \"${x:-'$(case x in a) :;; esac)'}\" )"
    with pytest.raises(BashSyntaxError) as raised:
        bash_syntax.parse(text)
    assert raised.value.holds_bare_case


def test_a_documented_limit_a_double_parenthesis_a_close_in_an_expansion_ends_is_read_as_a_subshell_s() -> None:
    text = "echo $(( ${x:-)} ))"
    assert not bash_agreement.bash_n_verdict(text)[0]
    assert simple_commands_of(text) == [["echo", "$(( ${x:-)} ))"], ["${x:-)}"]]


def test_a_documented_limit_an_unclosed_bracket_arithmetic_in_an_expansion_is_refused_with_its_line() -> None:
    # Bash 3.2 ends the `${` at its `}` and runs the git call; Bash 4 and later refuse the line, as the reader does.
    text = "git commit -m x; echo ${x%$[}"
    if runner_transformations()[0] >= (4, 0):
        assert not bash_agreement.bash_n_verdict(text)[0]
    with pytest.raises(BashSyntaxError):
        bash_syntax.parse(text)


def test_a_documented_limit_a_continued_line_between_a_dollar_and_its_parenthesis_is_refused() -> None:
    text = "echo $\\\n(git push)"
    assert bash_agreement.bash_n_verdict(text)[0]
    with pytest.raises(BashSyntaxError):
        bash_syntax.parse(text)


def test_a_single_quote_arithmetic_never_closes_is_refused_as_bash_refuses_it() -> None:
    text = "echo $(( 'abc ))"
    assert not bash_agreement.bash_n_verdict(text)[0]
    with pytest.raises(BashSyntaxError, match=r"^a quote in an arithmetic command is not closed$"):
        bash_syntax.parse(text)


def test_a_process_substitution_that_never_closes_in_arithmetic_s_quotes_is_text_and_the_rest_is_read() -> None:
    # The `<(` inside the single quotes opens nothing that closes, so it is read as text, and the substitution after it
    # in the same quotes, which Bash runs as it expands the expression, is still read.
    text = "echo $(( '<(x $(git push)' ))"
    parse(text)
    assert simple_commands_of(text) == [["echo", "$(( '<(x $(git push)' ))"], ["git", "push"]]
