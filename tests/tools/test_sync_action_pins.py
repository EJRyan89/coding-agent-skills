"""Regression suite for tools/sync_action_pins.py: copying the reviewed action pins into init-ai-config."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools import sync_action_pins

OLD_CHECKOUT = "1" * 40
OLD_SETUP = "2" * 40
NEW_CHECKOUT = "a" * 40
NEW_SETUP = "b" * 40

WORKFLOW = (
    "jobs:\n  validate:\n    steps:\n"
    f"      - uses: actions/checkout@{NEW_CHECKOUT} # v8.1.0\n"
    f"      - uses: actions/setup-python@{NEW_SETUP} # v9.0.2\n"
)
TEMPLATE = (
    "def workflow() -> str:\n    return (\n"
    f'        "      - uses: actions/checkout@{OLD_CHECKOUT}  # v7.0.1\\n"\n'
    f'        "      - uses: actions/setup-python@{OLD_SETUP}  # v7.0.0\\n"\n'
    "    )\n"
)
TEMPLATE_TEST = f'self.assertIn("actions/checkout@{OLD_CHECKOUT}", content)\n'
REFERENCE = f"steps:\n  - uses: actions/setup-python@{OLD_SETUP} # v7.0.0\n"


class SyncActionPinsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="sync-action-pins-test.")
        self.root = Path(self._temporary.name).resolve() / "Repo With Spaces"
        self.write(".github/workflows/validate.yml", WORKFLOW)
        self.write("skills/init-ai-config/scripts/ai_config_template.py", TEMPLATE)
        self.write("skills/init-ai-config/scripts/test_ai_config_template.py", TEMPLATE_TEST)
        self.write("skills/init-ai-config/references/parity.yml", REFERENCE)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def write(self, relative: str, text: str, newline: str = "\n") -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.replace("\n", newline).encode("utf-8"))

    def read(self, relative: str) -> str:
        return (self.root / relative).read_bytes().decode("utf-8")

    def run_tool(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = sync_action_pins.main(["--root", str(self.root), *arguments])
        return code, output.getvalue()

    def snapshot(self) -> dict[str, bytes]:
        return {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def test_updates_the_sha_and_version_comment_keeping_the_spacing(self) -> None:
        code, output = self.run_tool()
        self.assertEqual(0, code, output)
        self.assertEqual(
            "def workflow() -> str:\n    return (\n"
            f'        "      - uses: actions/checkout@{NEW_CHECKOUT}  # v8.1.0\\n"\n'
            f'        "      - uses: actions/setup-python@{NEW_SETUP}  # v9.0.2\\n"\n'
            "    )\n",
            self.read("skills/init-ai-config/scripts/ai_config_template.py"),
        )
        self.assertEqual(
            f"steps:\n  - uses: actions/setup-python@{NEW_SETUP} # v9.0.2\n",
            self.read("skills/init-ai-config/references/parity.yml"),
        )
        self.assertIn("updated skills/init-ai-config/scripts/ai_config_template.py", output)
        self.assertIn("updated skills/init-ai-config/references/parity.yml", output)

    def test_updates_a_test_literal_that_has_no_version_comment(self) -> None:
        self.assertEqual(0, self.run_tool()[0])
        self.assertEqual(
            f'self.assertIn("actions/checkout@{NEW_CHECKOUT}", content)\n',
            self.read("skills/init-ai-config/scripts/test_ai_config_template.py"),
        )

    def test_check_reports_stale_files_and_writes_nothing(self) -> None:
        before = self.snapshot()
        code, output = self.run_tool("--check")
        self.assertEqual(1, code)
        self.assertEqual(before, self.snapshot())
        for relative in (
            "skills/init-ai-config/scripts/ai_config_template.py",
            "skills/init-ai-config/scripts/test_ai_config_template.py",
            "skills/init-ai-config/references/parity.yml",
        ):
            self.assertIn(f"stale {relative}", output)
        self.assertIn("python tools/sync_action_pins.py", output)

    def test_an_in_sync_tree_is_left_alone(self) -> None:
        self.assertEqual(0, self.run_tool()[0])
        before = self.snapshot()
        for arguments in ((), ("--check",)):
            with self.subTest(arguments=arguments):
                code, output = self.run_tool(*arguments)
                self.assertEqual(0, code, output)
                self.assertIn("action pins are in sync", output)
                self.assertEqual(before, self.snapshot())

    def test_an_action_validate_yml_does_not_pin_fails_and_writes_nothing(self) -> None:
        self.write("skills/init-ai-config/references/extra.yml", f"- uses: actions/cache@{OLD_SETUP} # v4.0.0\n")
        before = self.snapshot()
        code, output = self.run_tool()
        self.assertEqual(1, code)
        self.assertIn("skills/init-ai-config/references/extra.yml pins actions/cache", output)
        self.assertEqual(before, self.snapshot())

    def test_a_reviewed_pin_without_a_version_comment_fails(self) -> None:
        self.write(".github/workflows/validate.yml", f"      - uses: actions/checkout@{NEW_CHECKOUT}\n")
        before = self.snapshot()
        code, output = self.run_tool()
        self.assertEqual(1, code)
        self.assertIn("actions/checkout has no version comment", output)
        self.assertEqual(before, self.snapshot())

    def test_crlf_line_endings_are_kept(self) -> None:
        self.write("skills/init-ai-config/references/parity.yml", REFERENCE, newline="\r\n")
        self.assertEqual(0, self.run_tool()[0])
        self.assertEqual(
            f"steps:\r\n  - uses: actions/setup-python@{NEW_SETUP} # v9.0.2\r\n",
            self.read("skills/init-ai-config/references/parity.yml"),
        )

    def test_this_repository_is_in_sync(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = sync_action_pins.main(["--check"])
        self.assertEqual(0, code, output.getvalue())


if __name__ == "__main__":
    unittest.main()
