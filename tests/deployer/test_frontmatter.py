"""The shared SKILL.md and agent frontmatter reader (deployer/frontmatter.py)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from deployer import frontmatter
from deployer.frontmatter import FrontmatterError


def parse(block: str) -> frontmatter.Frontmatter:
    return frontmatter.parse(f"---\n{block}\n---\nBody.\n")


class ScalarTests(unittest.TestCase):
    def test_plain_and_quoted_scalars(self) -> None:
        cases = {
            "key: Run the report.": "Run the report.",
            "key: Bash(git status:*), Read  # trailing comment": "Bash(git status:*), Read",
            "key: a#b": "a#b",
            'key: "Say \\"hi\\" — \\u00e9\\tthen\\\\stop"': 'Say "hi" — é\tthen\\stop',
            'key: "with # inside"  # comment': "with # inside",
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
            with self.subTest(block=block):
                with self.assertRaisesRegex(FrontmatterError, f"^key: .*{message}"):
                    parse(block).value("key")

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

    def test_duplicate_keys_are_refused_when_read(self) -> None:
        document = parse("name: a\nname: b\nother: c")
        self.assertEqual("c", document.value("other"))
        with self.assertRaisesRegex(FrontmatterError, "name appears more than once"):
            document.string("name")

    def test_malformed_structure_is_refused_when_parsed(self) -> None:
        for block, message in (
            ("  indented first line", "line 1 belongs to no key"),
            ("name: a\nnot a key", "line 2 is not a 'key: value' line"),
        ):
            with self.subTest(block=block):
                with self.assertRaisesRegex(FrontmatterError, message):
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


if __name__ == "__main__":
    unittest.main()
