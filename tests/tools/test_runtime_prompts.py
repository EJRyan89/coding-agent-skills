"""Regression suite for tools/runtime_prompts.py: rendering runtime prompt samples and reporting where they drift."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import runtime_prompts


def renderer(prompts: dict[str, str]) -> Callable[[Path], dict[str, str]]:
    """A renderer that names its throwaway folder and the checkout in each prompt, as a real one does."""

    def render(directory: Path) -> dict[str, str]:
        (directory / "run").mkdir()
        source = runtime_prompts.REPOSITORY_ROOT / "skills" / "x.md"
        return {role: f"{text}\nRead {directory / 'run'} and {source}.\n" for role, text in prompts.items()}

    return render


class RuntimePromptsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="runtime-prompts-test.")
        self.root = Path(self._temporary.name).resolve() / "Repo With Spaces"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_samples_name_placeholders_and_write_makes_them_current(self) -> None:
        renderers = {"alpha": renderer({"reviewer": "Review it."}), "beta": renderer({"synthesis": "Sum it up."})}
        self.assertEqual(
            [
                f"docs/runtime-prompts/alpha/reviewer.txt does not exist; {runtime_prompts.WRITE_HINT}",
                f"docs/runtime-prompts/beta/synthesis.txt does not exist; {runtime_prompts.WRITE_HINT}",
            ],
            runtime_prompts.problems(self.root, renderers),
        )
        runtime_prompts.write(self.root, renderers)
        self.assertEqual([], runtime_prompts.problems(self.root, renderers))
        sample = self.root / "docs" / "runtime-prompts" / "alpha" / "reviewer.txt"
        self.assertEqual(
            "Review it.\nRead <temp>\\run and <source>\\skills\\x.md.\n", sample.read_text(encoding="utf-8")
        )

        sample.write_text("Review it, by hand.\n", encoding="utf-8")
        extra = self.root / "docs" / "runtime-prompts" / "gamma" / "old.txt"
        extra.parent.mkdir()
        extra.write_text("Gone.\n", encoding="utf-8")
        self.assertEqual(
            [
                f"docs/runtime-prompts/alpha/reviewer.txt is stale; {runtime_prompts.WRITE_HINT}",
                f"docs/runtime-prompts/gamma/old.txt is no rendered prompt; {runtime_prompts.WRITE_HINT}",
            ],
            runtime_prompts.problems(self.root, renderers),
        )
        runtime_prompts.write(self.root, renderers)
        self.assertEqual([], runtime_prompts.problems(self.root, renderers))
        self.assertFalse(extra.parent.exists(), "a folder left empty goes with its last sample")

    def test_a_prompt_that_still_names_the_temporary_directory_fails_and_leaves_nothing_behind(self) -> None:
        folders: list[Path] = []

        def leaky(directory: Path) -> dict[str, str]:
            folders.append(directory)
            return {"reviewer": f"Read {Path(tempfile.gettempdir()).resolve() / 'elsewhere'}.\n"}

        with self.assertRaisesRegex(runtime_prompts.RenderError, "still names"):
            runtime_prompts.problems(self.root, {"alpha": leaky})
        self.assertFalse(folders[0].exists())

    def test_main_reports_problems_and_writes(self) -> None:
        renderers = {"alpha": renderer({"reviewer": "Review it."})}
        output = io.StringIO()
        with mock.patch.object(runtime_prompts, "RENDERERS", renderers), contextlib.redirect_stdout(output):
            self.assertEqual(1, runtime_prompts.main(["--root", str(self.root)]))
            self.assertEqual(0, runtime_prompts.main(["--root", str(self.root), "--write"]))
        self.assertIn("does not exist", output.getvalue())

    def test_each_skill_renders_its_prompts_without_a_model_and_bounds_their_replies(self) -> None:
        # The real renderers, into a throwaway root: every prompt the skills' scripts write today.
        runtime_prompts.write(self.root)
        samples = sorted(
            path.relative_to(self.root / "docs" / "runtime-prompts").as_posix()
            for path in (self.root / "docs" / "runtime-prompts").rglob("*.txt")
        )
        self.assertEqual(
            ["review-document/design-review.txt", "review-insights/synthesis.txt", "review-prs/generic-review.txt"],
            samples,
        )
        for sample in samples:
            text = (self.root / "docs" / "runtime-prompts" / sample).read_text(encoding="utf-8")
            self.assertRegex(text, r"reply with (?:exactly|only)\W+WROTE <temp>", sample)
            self.assertNotIn(str(runtime_prompts.REPOSITORY_ROOT), text, sample)


if __name__ == "__main__":
    unittest.main()
