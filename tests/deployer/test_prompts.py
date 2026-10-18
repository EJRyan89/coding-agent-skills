"""Prompts go to stderr, so a script reading deploy.py's stdout receives results and never a question."""

from __future__ import annotations

from harness import DeployerTestCase, forward


class PromptStreamTests(DeployerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_source_json()
        self.make_skill("alpha", "Alpha")

    def run_split(self, *arguments: str, stdin: str) -> tuple[int, str, str]:
        result = self.run_cli(*arguments, stdin=stdin)
        return result.code, result.stdout, result.stderr

    def test_configure_prompts_reach_stderr_and_not_stdout(self) -> None:
        self.repos.mkdir(parents=True, exist_ok=True)
        code, out, err = self.run_split("configure", stdin=f"{forward(self.repos)}\n")
        self.assertEqual(0, code, out + err)
        for prompt in ("REPOS_ROOT: ", "Value (Ctrl+C cancels): "):
            with self.subTest(prompt=prompt):
                self.assertNotIn(prompt, out)
                self.assertIn(prompt, err)
        self.make_config()
        code, out, err = self.run_split("configure", stdin="\n")
        self.assertEqual(0, code, out + err)
        for prompt in ("REPOS_ROOT: ", "  Current: ", "New value (Enter keeps the current value, Ctrl+C cancels): "):
            with self.subTest(prompt=prompt):
                self.assertNotIn(prompt, out)
                self.assertIn(prompt, err)
        self.assertIn("Saved.", out)

    def test_the_selection_menu_reaches_stderr_and_not_stdout(self) -> None:
        self.make_config()
        code, out, err = self.run_split("--dry-run", stdin="1\n")
        self.assertEqual(0, code, out + err)
        for prompt in (
            "Select what to deploy:",
            "  [ ] 1. alpha",
            "[*] = chosen in an earlier deployment",
            "Enter numbers separated by spaces, 'all', or 'none'. Ctrl+C cancels.",
            "Selection: ",
        ):
            with self.subTest(prompt=prompt):
                self.assertNotIn(prompt, out)
                self.assertIn(prompt, err)
        self.assertIn("=== DRY RUN ===", out)
