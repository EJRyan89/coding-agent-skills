from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness  # noqa: F401 - imported for its effect: it puts the repository root on sys.path for deployer

from deployer import hashing, platform_support, render
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
            with self.subTest(logical=logical), self.assertRaisesRegex(DeployError, "cannot be safely substituted"):
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


class FenceTests(unittest.TestCase):
    """The one fence detector, which rendering, ShellCheck extraction, and validation's Markdown policies share."""

    def fences(self, text: str) -> list[tuple[int, int | None, str, str]]:
        return [
            (fence.opener, fence.closer, fence.language, fence.context)
            for fence in render.find_fences(text.split("\n"))
        ]

    def test_backtick_and_tilde_fences_follow_commonmark(self) -> None:
        cases = {
            "three backticks": ("```bash\necho\n```", [(0, 2, "bash", "shell")]),
            "tilde fence": ("~~~bash\necho\n~~~", [(0, 2, "bash", "shell")]),
            "closer longer than the opener": ("```sh\necho\n`````", [(0, 2, "sh", "shell")]),
            "four backticks hold a three-backtick line": (
                "````markdown\n```bash\necho\n```\n````",
                [(0, 4, "markdown", "text")],
            ),
            "a tilde closer does not close a backtick fence": ("```text\n~~~\n```", [(0, 2, "text", "text")]),
            "a tilde fence's info may hold backticks": ("~~~bash `x`\necho\n~~~", [(0, 2, "bash", "shell")]),
            "a backtick line with a backtick in its info is inline code": ("```a`b```\nprose", []),
            "a closer with info is not a closer": ("```text\n```bash\n```", [(0, 2, "text", "text")]),
            "an unclosed fence runs to the end": ("```bash\necho", [(0, None, "bash", "shell")]),
            "an indented fence inside a list item": ("1. Run:\n   ```bash\n   echo\n   ```", [(1, 3, "bash", "shell")]),
            "two fences": (
                "```bash\na\n```\ntext\n~~~ps1\nb\n~~~",
                [(0, 2, "bash", "shell"), (4, 6, "ps1", "powershell")],
            ),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(expected, self.fences(text))

    def test_a_tilde_bash_fence_is_substituted_as_shell(self) -> None:
        with self.assertRaisesRegex(DeployError, r"\(shell context\)"):
            render.render_file("s/SKILL.md", b"~~~bash\necho {{X}}\n~~~\n", {"X": "O'Brien"})
        self.assertEqual(
            b"~~~text\necho O'Brien\n~~~\n",
            render.render_file("s/SKILL.md", b"~~~text\necho {{X}}\n~~~\n", {"X": "O'Brien"}),
        )

    def test_an_inner_three_backtick_line_stays_in_a_four_backtick_fence(self) -> None:
        text = b"````text\n```bash\necho {{X}}\n```\n````\n"
        self.assertIn(b"echo O'Brien", render.render_file("s/SKILL.md", text, {"X": "O'Brien"}))

    def test_shellcheck_extraction_uses_the_same_detector(self) -> None:
        markdown = "~~~bash\necho one\n~~~\n\n````text\n```bash\necho never\n```\n````\n\n```sh\necho two\n`````\n"
        with tempfile.TemporaryDirectory() as workspace:
            staged = render.Staged(skills={"s": {"SKILL.md": markdown.encode("utf-8")}})
            units = render._extract_units(staged, Path(workspace))
            self.assertEqual(["s/SKILL.md block 1", "s/SKILL.md block 2"], [unit.origin for unit in units])
            bodies = [unit.path.read_text(encoding="utf-8").removeprefix(render.SHELLCHECK_HEADER) for unit in units]
        self.assertEqual(["echo one\n", "echo two\n"], bodies)

    def test_an_unclosed_tilde_shell_fence_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            staged = render.Staged(skills={"s": {"SKILL.md": b"~~~bash\necho\n"}})
            with self.assertRaisesRegex(DeployError, "Unclosed Bash code fence in s/SKILL.md"):
                render._extract_units(staged, Path(workspace))


class ShellCheckBatchTests(unittest.TestCase):
    def test_units_past_one_command_line_run_in_batches_and_every_finding_is_reported_once(self) -> None:
        units = [render.ShellUnit(Path(f"C:/work/blocks/{n}.sh"), f"s/SKILL.md block {n + 1}", True) for n in range(6)]
        failing = {"C:/work/blocks/1.sh", "C:/work/blocks/4.sh"}
        calls: list[list[str]] = []

        def shellcheck(arguments: list[str], _environment: object = None) -> platform_support.ToolResult:
            calls.append(arguments)
            lines = [f"{path}:3:7: warning: Double quote to prevent globbing. [SC2086]" for path in arguments[2:]]
            found = [line for line in lines if line.split(":3:")[0] in failing]
            return platform_support.ToolResult(1 if found else 0, "".join(f"{line}\n" for line in found))

        # Room for three unit paths besides the program and its option, so six units need two commands.
        budget = len("shellcheck --format=gcc") + 3 * len(" C:/work/blocks/0.sh")
        with (
            mock.patch.object(render, "SHELLCHECK_COMMAND_BUDGET", budget),
            mock.patch.object(platform_support, "run_tool", side_effect=shellcheck),
            self.assertRaises(DeployError) as raised,
        ):
            render._run_shellcheck(units, "shellcheck")
        self.assertEqual(2, len(calls))
        self.assertTrue(all(len(" ".join(call)) <= budget for call in calls), calls)
        self.assertEqual([f"C:/work/blocks/{n}.sh" for n in range(6)], [path for call in calls for path in call[2:]])
        self.assertEqual(
            (
                "ERROR: ShellCheck failed for rendered Bash block: s/SKILL.md block 2",
                "s/SKILL.md block 2:3:7: warning: Double quote to prevent globbing. [SC2086]",
                "s/SKILL.md block 5:3:7: warning: Double quote to prevent globbing. [SC2086]",
            ),
            raised.exception.lines,
        )

    def test_one_failing_unit_keeps_its_message(self) -> None:
        units = [render.ShellUnit(Path("C:/work/files/run.sh"), "s/scripts/run.sh", False)]
        result = platform_support.ToolResult(1, "C:/work/files/run.sh:2:1: error: Parsing stopped. [SC1073]\n")
        with (
            mock.patch.object(platform_support, "run_tool", return_value=result),
            self.assertRaises(DeployError) as raised,
        ):
            render._run_shellcheck(units, "shellcheck")
        self.assertEqual(
            (
                "ERROR: ShellCheck failed for rendered content: s/scripts/run.sh",
                "s/scripts/run.sh:2:1: error: Parsing stopped. [SC1073]",
            ),
            raised.exception.lines,
        )


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
