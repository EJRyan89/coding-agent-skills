"""Regression suite for tools/bash_syntax.py: Bash command text read by Bash's own grammar."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))


from tools import bash_syntax
from tools.bash_syntax import BashSyntaxError, parse


def children(item: object) -> list[object]:
    """The nodes a tree node holds, in the order the text holds them."""
    if isinstance(item, bash_syntax.CommandList):
        return [and_or for and_or, _ in item.items]
    if isinstance(item, bash_syntax.AndOr):
        return [item.first, *(pipeline for _, pipeline in item.rest)]
    if isinstance(item, bash_syntax.Pipeline):
        return list(item.commands)
    if isinstance(item, (bash_syntax.Subshell, bash_syntax.Group, bash_syntax.FunctionDefinition)):
        return [item.body]
    if isinstance(item, bash_syntax.If):
        held: list[object] = [part for clause in item.clauses for part in clause]
        return held if item.otherwise is None else [*held, item.otherwise]
    if isinstance(item, bash_syntax.Loop):
        return [item.body] if item.condition is None else [item.condition, item.body]
    if isinstance(item, bash_syntax.Case):
        return list(item.branches)
    return []


def simple_commands(node: object) -> list[list[str]]:
    """Every simple command in a tree, in the order the text holds them, as the raw text of its words."""
    if isinstance(node, bash_syntax.Simple):
        return [[word.raw for word in node.words]]
    return [command for child in children(node) for command in simple_commands(child)]


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
    ],
)
def test_text_bash_would_refuse_is_an_error(text: str) -> None:
    with pytest.raises(BashSyntaxError):
        parse(text)
