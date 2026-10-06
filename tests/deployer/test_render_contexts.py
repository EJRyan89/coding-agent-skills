from __future__ import annotations

import json
import unittest

# The harness puts the repository root on sys.path so the deployer package imports.
import harness

from deployer import hashing, render
from deployer.errors import DeployError


class SubstitutionContextTests(unittest.TestCase):
    def test_json_values_are_escaped_inside_strings(self) -> None:
        rendered = render.render_file("s/config.json", b'{"root": "{{X}}"}', {"X": 'a"b\\c'})
        self.assertEqual({"root": 'a"b\\c'}, json.loads(rendered))

    def test_json_token_outside_a_string_must_still_parse(self) -> None:
        with self.assertRaisesRegex(DeployError, "Rendered JSON validation failed: s/config.json"):
            render.render_file("s/config.json", b'{"root": {{X}}}', {"X": "value"})

    def test_toml_values_are_escaped_and_parsed(self) -> None:
        rendered = render.render_file("s/config.toml", b'root = "{{X}}"\n', {"X": 'C:/a "b"'})
        self.assertIn(b'root = "C:/a \\"b\\""', rendered)

    def test_shell_powershell_python_and_yaml_reject_unsafe_characters(self) -> None:
        cases = {
            "s/run.sh": "a'b",
            "s/run.ps1": "a$b",
            "s/tool.py": 'a"b',
            "s/config.yml": "a: b",
        }
        for logical, value in cases.items():
            with self.subTest(logical=logical):
                with self.assertRaisesRegex(DeployError, "cannot be safely substituted"):
                    render.render_file(logical, b"value {{X}}\n", {"X": value})

    def test_markdown_applies_fence_context_and_leaves_prose_raw(self) -> None:
        prose = render.render_file("s/SKILL.md", b"Owner: {{X}}\n", {"X": "O'Brien"})
        self.assertEqual(b"Owner: O'Brien\n", prose)
        with self.assertRaisesRegex(DeployError, r"\(shell context\)"):
            render.render_file("s/SKILL.md", b"```bash\necho {{X}}\n```\n", {"X": "O'Brien"})
        json_block = render.render_file("s/SKILL.md", b'```json\n{"a": "{{X}}"}\n```\n', {"X": 'q"'})
        self.assertIn(b'{"a": "q\\""}', json_block)

    def test_markdown_frontmatter_takes_the_yaml_context(self) -> None:
        skill = b'---\nname: s\ndescription: "Sweep {{X}}"\n---\n\nUnder {{X}}\n'
        rendered = render.render_file("s/SKILL.md", skill, {"X": "C:/My Repos (work)"})
        self.assertEqual(
            b'---\nname: s\ndescription: "Sweep C:/My Repos (work)"\n---\n\nUnder C:/My Repos (work)\n', rendered
        )
        with self.assertRaisesRegex(DeployError, r"s/SKILL\.md \(yaml context\)"):
            render.render_file("s/SKILL.md", skill, {"X": 'C:/a"b'})
        self.assertEqual(
            b'---\nUnder "q"\n',
            render.render_file("s/notes.md", b"---\nUnder {{X}}\n", {"X": '"q"'}),
            "an opening rule that is never closed is not frontmatter",
        )

    def test_undeclared_tokens_and_unknown_suffixes_are_left_untouched(self) -> None:
        self.assertEqual(b"{{Y}}", render.render_file("s/a.md", b"{{Y}}", {"X": "v"}))
        self.assertEqual(b"{{X}}", render.render_file("s/image.bin", b"{{X}}", {"X": "v"}))


class NameValidationTests(unittest.TestCase):
    def test_trailing_newlines_never_pass_name_or_hash_validation(self) -> None:
        from deployer import names, source

        self.assertIsNotNone(names.safe_name_problem("alpha\n", "item"))
        self.assertFalse(source.is_valid_name("alpha\n"))
        self.assertIsNone(source.SOURCE_ID_PATTERN.fullmatch("owner/repo\n"))
        self.assertIsNone(hashing.HASH_PATTERN.fullmatch("sha256:" + "0" * 64 + "\n"))


class HashTests(unittest.TestCase):
    def test_tree_hash_is_independent_of_insertion_order_and_sensitive_to_paths(self) -> None:
        first = hashing.tree_hash({"a.txt": b"1", "b/c.txt": b"2"})
        second = hashing.tree_hash({"b/c.txt": b"2", "a.txt": b"1"})
        renamed = hashing.tree_hash({"a.txt": b"1", "b/d.txt": b"2"})
        self.assertEqual(first, second)
        self.assertNotEqual(first, renamed)
        self.assertRegex(first, hashing.HASH_PATTERN)


if __name__ == "__main__":
    unittest.main()
