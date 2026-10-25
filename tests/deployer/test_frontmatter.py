"""The shared SKILL.md and agent frontmatter reader (skills/ejryan89-skill-core/scripts/frontmatter.py)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import frontmatter
from frontmatter import FrontmatterError

from deployer import render, source

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def parse(block: str) -> frontmatter.Frontmatter:
    return frontmatter.parse(f"---\n{block}\n---\nBody.\n")


class HomeTests(unittest.TestCase):
    def test_the_deployer_reads_frontmatter_from_this_checkouts_skill_core(self) -> None:
        # deployer/__init__.py locates ejryan89-skill-core from its own file, never through the configured source path,
        # so the deployer, the tools, and the skills share one module object and one FrontmatterError.
        expected = REPOSITORY_ROOT / "skills" / "ejryan89-skill-core" / "scripts" / "frontmatter.py"
        self.assertEqual(expected, Path(frontmatter.__file__).resolve())
        # render and source import frontmatter and export none, so their own bindings are read through vars.
        self.assertIs(frontmatter, vars(render)["frontmatter"])
        self.assertIs(frontmatter, vars(source)["frontmatter"])
        self.assertFalse((REPOSITORY_ROOT / "deployer" / "frontmatter.py").exists())


class ScalarTests(unittest.TestCase):
    def test_plain_and_quoted_scalars(self) -> None:
        cases = {
            "key: Run the report.": "Run the report.",
            "key: Bash(git status:*), Read  # trailing comment": "Bash(git status:*), Read",
            "key: a#b": "a#b",
            'key: "Say \\"hi\\" — \\u00e9\\tthen\\\\stop"': 'Say "hi" — é\tthen\\stop',
            'key: "with # inside"  # comment': "with # inside",
            # A literal tab, which YAML allows inside double quotes and JSON only as an escape.
            'key: "a\tb"': "a\tb",
            "key: 'It''s single'": "It's single",
            "key: true": "true",
            "key:": None,
            "key:   # only a comment": None,
            'key: ""': "",
        }
        for block, expected in cases.items():
            with self.subTest(block=block):
                self.assertEqual(expected, parse(block).value("key"))

    def test_scalars_continued_on_indented_lines_are_folded(self) -> None:
        cases = {
            "key: first line\n  second line\n\n  new paragraph": "first line second line\nnew paragraph",
            'key: "quoted across\n  two lines"': "quoted across two lines",
            "key: 'single across\n  two lines'": "single across two lines",
            "key:\n  starts on the next line": "starts on the next line",
        }
        for block, expected in cases.items():
            with self.subTest(block=block):
                self.assertEqual(expected, parse(block).value("key"))

    def test_block_scalars_and_chomping(self) -> None:
        body = "  first\n  second\n\n  third\n\n"
        cases = {
            f"key: >-\n{body}": "first second\nthird",
            f"key: >\n{body}": "first second\nthird\n",
            # Keep chomping keeps the empty line after "third" and the one parse() adds before the closing delimiter.
            f"key: >+\n{body}": "first second\nthird\n\n\n",
            f"key: |-\n{body}": "first\nsecond\n\nthird",
            f"key: |\n{body}": "first\nsecond\n\nthird\n",
            "key: |\n    deeper\n      more\n": "deeper\n  more\n",
            "key: >- # comment\n  folded": "folded",
            "key: >-": "",
        }
        for block, expected in cases.items():
            with self.subTest(block=block):
                self.assertEqual(expected, parse(block).value("key"))


class SequenceTests(unittest.TestCase):
    def test_flow_and_block_sequences(self) -> None:
        cases = {
            'key: ["Bash", "Read"]': ["Bash", "Read"],
            "key: [Bash, 'Read', \"Grep\", ]": ["Bash", "Read", "Grep"],
            "key: []": [],
            "key: [Bash(git status:*)]  # comment": ["Bash(git status:*)"],
            "key:\n  - Grep\n  - 'mcp__srv__find'\n  - \"Read\"": ["Grep", "mcp__srv__find", "Read"],
            "key:\n- zero indent\n- items": ["zero indent", "items"],
        }
        for block, expected in cases.items():
            with self.subTest(block=block):
                self.assertEqual(expected, parse(block).value("key"))

    def test_only_a_true_disable_model_invocation_makes_a_skill_user_only(self) -> None:
        cases = {"true": True, "True": True, '"TRUE"': True, "false": False, "yes": False, "": False}
        for value, expected in cases.items():
            with self.subTest(value=value):
                document = frontmatter.parse(f"---\nname: a\ndisable-model-invocation: {value}\n---\n")
                self.assertIs(expected, document.user_only())
        self.assertFalse(frontmatter.parse("---\nname: a\n---\n").user_only())
        with self.assertRaisesRegex(FrontmatterError, "must be a single value"):
            frontmatter.parse("---\ndisable-model-invocation: [true]\n---\n").user_only()

    def test_string_refuses_a_sequence(self) -> None:
        with self.assertRaisesRegex(FrontmatterError, "key must be a single value"):
            parse("key: [a]").string("key")


class RefusalTests(unittest.TestCase):
    def test_unsupported_forms_are_refused_when_read(self) -> None:
        cases = {
            "key:\n  nested: value": "nested mapping",
            "key:\n  - matcher: x": "one-line scalar",
            "key:\n  - a\n    - b": "indented alike",
            "key:\n  - a\n  other": "nested mapping",
            "key: {a: b}": "flow mapping",
            "key: &anchor value": "anchor",
            "key: *alias": "alias",
            "key: !tag value": "tag",
            "key: |2\n    text": "indentation indicator",
            "key: >\n  text\n    more indented": "more-indented",
            "key: |\n    text\n  less": "less indented",
            'key: "unterminated': "not closed",
            "key: 'unterminated": "not closed",
            'key: "\\x41"': "escape",
            "key: [a, b": "not closed",
            "key: [a, [b]]": "not a scalar",
            "key: [a]\n  more": "one line",
        }
        for block, message in cases.items():
            with self.subTest(block=block), self.assertRaisesRegex(FrontmatterError, f"^key: .*{message}"):
                parse(block).value("key")

    def test_values_yaml_reads_otherwise_or_not_at_all_are_refused_when_read(self) -> None:
        # Each is a block a runtime's YAML parser refuses or reads as something other than the reader's string.
        cases = {
            "key: run it: now": "holding ': ' or ending in ':'",
            "key: foo:": "holding ': ' or ending in ':'",
            "key: foo:\tbar": "holding ': ' or ending in ':'",
            "key: foo\n  bar: baz": "holding ': ' or ending in ':'",
            "key: foo:\n  bar": "holding ': ' or ending in ':'",
            "key: - x": "starting with '-' and a space",
            "key: -": "starting with '-' and a space",
            "key: ? x": "starting with '\\?' and a space",
            "key: : x": "starting with ':' and a space",
            "key:\n  ? x": "starting with '\\?' and a space",
            "key: ,x": "starting with ',', a flow indicator",
            "key: ]x": "starting with '\\]', a flow indicator",
            "key: }x": "starting with '}', a flow indicator",
            "key:\n  [a, b]": "flow sequence that does not start on its key's line",
            "key:\n  |\n  text": "block scalar that does not start on its key's line",
            "key:\n  - a b: c": "holding ': ' or ending in ':'",
            "key: [a: b]": "holding ': ' or ending in ':'",
            "key: [a:]": "holding ': ' or ending in ':'",
            "key: [- a]": "starting with '-' and a space",
            "key:\n  - a\n  \t- b": "indented with a tab",
            "key: |\n  one\n# two\n  three": "comment at column 0 ends a block scalar",
            "key: >-\n  one\n# two\n  three": "comment at column 0 ends a block scalar",
            "key: foo\n# note\n  bar": "comment at column 0 ends the value",
            "key: 'foo\n# note\n  bar'": "comment at column 0 ends the value",
            "key: foo\n  # note\n  bar": "comment ends a plain value",
            "key: foo # note\n  bar": "comment ends a plain value",
        }
        for block, message in cases.items():
            with self.subTest(block=block), self.assertRaisesRegex(FrontmatterError, f"^key: .*{message}"):
                parse(block).value("key")

    def test_yaml_values_near_the_refused_ones_are_still_read(self) -> None:
        cases = {
            "key: a:b": "a:b",
            "key: http://example.com/x": "http://example.com/x",
            "key: Bash(git status:*)": "Bash(git status:*)",
            "key: a - b ? c": "a - b ? c",
            "key: -1": "-1",
            "key: -x": "-x",
            "key: ?x": "?x",
            "key: :x": ":x",
            "key: foo # see: bar": "foo",
            "key: 'run it: now'": "run it: now",
            'key: "- x"': "- x",
            "key: [a:b, 'c: d']": ["a:b", "c: d"],
            "key:\n  - 'a: b'\n  - a:b": ["a: b", "a:b"],
            "key:\n# before the value\n  value": "value",
            "key:\n  # before the value\n  value": "value",
            "key:\n  # before the value\n  'quoted'": "quoted",
            "key:\n  - a\n# between items\n  - b\n  # and again\n  - c": ["a", "b", "c"],
            "key: |\n  one\n  # indented, so text\n  three": "one\n# indented, so text\nthree\n",
            "key: |+\n  one\n\n# after the block": "one\n\n",
            "key: plain\n  # a last comment": "plain",
            "key: |\n  \tcode after the indentation": "\tcode after the indentation\n",
        }
        for block, expected in cases.items():
            with self.subTest(block=block):
                self.assertEqual(expected, parse(block).value("key"))

    def test_a_comment_at_column_0_after_a_value_ends_it_before_the_next_key(self) -> None:
        document = parse("first: |\n  one\n# ends the block\nsecond: two\n# ends the plain value\nthird:\n  - x")
        self.assertEqual(
            ("one\n", "two", ["x"]), (document.value("first"), document.value("second"), document.value("third"))
        )

    def test_unread_keys_are_never_parsed(self) -> None:
        document = parse(
            "name: reviewer\n"
            "tools: Read, Grep\n"
            "hooks:\n"
            "  PreToolUse:\n"
            '    - matcher: "Read|Grep"\n'
            "      hooks:\n"
            "        - type: command\n"
            "          command: 'python -B \"$HOME/x.py\"'"
        )
        self.assertEqual("reviewer", document.string("name"))
        self.assertEqual("Read, Grep", document.string("tools"))
        self.assertEqual(["name", "tools", "hooks"], document.keys())
        with self.assertRaisesRegex(FrontmatterError, "hooks: a nested mapping"):
            document.value("hooks")

    def test_lines_gives_a_keys_text_as_written_for_a_value_it_does_not_read(self) -> None:
        document = parse("name: reviewer\nhooks:\n  PreToolUse:\n    - matcher: Read\n\nmodel: inherit")
        self.assertEqual(["  PreToolUse:", "    - matcher: Read", ""], document.lines("hooks"))
        self.assertEqual([], document.lines("name"))
        self.assertIsNone(document.lines("absent"))
        duplicated = parse("hooks: a\nhooks: b")
        with self.assertRaisesRegex(FrontmatterError, "hooks appears more than once"):
            duplicated.lines("hooks")

    def test_duplicate_keys_are_refused_when_read(self) -> None:
        document = parse("name: a\nname: b\nother: c")
        self.assertEqual("c", document.value("other"))
        with self.assertRaisesRegex(FrontmatterError, "name appears more than once"):
            document.string("name")

    def test_malformed_structure_is_refused_when_parsed(self) -> None:
        for block, message in (
            ("  indented first line", "line 1 belongs to no key"),
            ("name: a\nnot a key", "line 2 is not a 'key: value' line"),
            ("tools:\n\t- a\n\t- b", "line 2 is indented with a tab"),
            ("name: a\nhooks:\n  Pre:\n\t  - x", "line 4 is indented with a tab"),
            ("name: a\n\tmore", "line 2 is indented with a tab"),
        ):
            with self.subTest(block=block), self.assertRaisesRegex(FrontmatterError, message):
                parse(block)

    def test_comments_and_blank_lines_between_keys_are_ignored(self) -> None:
        document = parse("# leading comment\n\nname: a\n# between\n\ndescription: b")
        self.assertEqual(["name", "description"], document.keys())
        self.assertEqual(("a", "b"), (document.string("name"), document.string("description")))
        self.assertIsNone(document.value("absent"))
        self.assertNotIn("absent", document)


class DocumentTests(unittest.TestCase):
    def test_split_finds_the_body(self) -> None:
        self.assertEqual((["name: a"], 3), frontmatter.split(["---", "name: a", "---", "Body."]))
        self.assertIsNone(frontmatter.split(["Body only."]))
        self.assertIsNone(frontmatter.split([]))
        with self.assertRaisesRegex(FrontmatterError, "not closed"):
            frontmatter.split(["---", "name: a"])

    def test_parse_without_frontmatter_has_no_keys(self) -> None:
        self.assertEqual([], frontmatter.parse("Just a body.\n").keys())

    def test_read_needs_frontmatter_and_accepts_crlf_and_a_byte_order_mark(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "With Spaces" / "SKILL.md"
            path.parent.mkdir()
            path.write_bytes('﻿---\r\nname: "crlf"\r\ndescription: >-\r\n  one\r\n  two\r\n---\r\n'.encode())
            document = frontmatter.read(path)
            self.assertEqual(("crlf", "one two"), (document.string("name"), document.string("description")))
            path.write_text("No frontmatter.\n", encoding="utf-8")
            with self.assertRaisesRegex(FrontmatterError, "no frontmatter"):
                frontmatter.read(path)

    def test_read_refuses_a_file_it_cannot_decode_or_read_as_frontmatter_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "With Spaces" / "SKILL.md"
            path.parent.mkdir()
            path.write_bytes(b"\xff---\nname: a\n---\n")
            with self.assertRaisesRegex(FrontmatterError, "is not UTF-8"):
                frontmatter.read(path)
            path.unlink()
            path.mkdir()
            with self.assertRaisesRegex(FrontmatterError, "cannot be read"):
                frontmatter.read(path)
            with (
                mock.patch.object(Path, "read_text", side_effect=PermissionError(13, "Permission denied")),
                self.assertRaises(FrontmatterError) as raised,
            ):
                frontmatter.read(path)
            self.assertEqual(f"{path} cannot be read: Permission denied", str(raised.exception))
