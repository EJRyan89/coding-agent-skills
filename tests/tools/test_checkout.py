"""Regression suite for tools/checkout.py: the check that a tool's --root is a checkout of this repository."""

from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

import skill_roots

from tools.checkout import checkout_problem, checkout_root


def make_checkout(root: Path) -> Path:
    (root / ".claude" / "skills").mkdir(parents=True)
    (root / "source.json").write_text("{}\n", encoding="utf-8")
    return root


class CheckoutTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="checkout-test.")
        self.base = Path(self._temporary.name).resolve()
        self.home = self.base / "Fake Home"
        (self.home / ".claude" / "skills").mkdir(parents=True)
        (self.home / ".agents" / "skills").mkdir(parents=True)
        patcher = mock.patch.object(Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_a_checkout_outside_every_skills_directory_passes(self) -> None:
        root = make_checkout(self.base / "Repo With Spaces")
        self.assertIsNone(checkout_problem(root))
        self.assertEqual(root, checkout_root(str(root)))

    def test_a_directory_without_source_json_or_claude_skills_is_not_a_checkout(self) -> None:
        bare = self.base / "Bare"
        (bare / ".claude" / "skills").mkdir(parents=True)
        no_skills = self.base / "No Skills"
        no_skills.mkdir()
        (no_skills / "source.json").write_text("{}\n", encoding="utf-8")
        for root in (bare, no_skills, self.home):
            with self.subTest(root=root.name):
                self.assertEqual(
                    f"{root} is not a checkout of this repository: it needs a source.json file and a .claude/skills "
                    "directory",
                    checkout_problem(root),
                )
                with self.assertRaisesRegex(argparse.ArgumentTypeError, "is not a checkout of this repository"):
                    checkout_root(str(root))

    def test_a_home_holding_the_markers_is_refused_for_its_deployed_skills(self) -> None:
        (self.home / "source.json").write_text("{}\n", encoding="utf-8")
        skills = self.home / ".claude" / "skills"
        self.assertEqual(
            f"{skills} lies in the skills directory {skills}, so {self.home} is not a checkout to write into",
            checkout_problem(self.home),
        )

    def test_a_checkout_inside_a_skills_directory_is_refused(self) -> None:
        own = self.base / "Source" / "skills"
        own.mkdir(parents=True)
        for directory in (self.home / ".agents" / "skills", self.home / ".claude" / "skills", own):
            root = make_checkout(directory / "Nested Checkout")
            with self.subTest(directory=directory), mock.patch.object(skill_roots, "SKILLS_ROOT", own):
                self.assertEqual(
                    f"{root} lies in the skills directory {directory}, so {root} is not a checkout to write into",
                    checkout_problem(root),
                )
                with self.assertRaisesRegex(argparse.ArgumentTypeError, "lies in the skills directory"):
                    checkout_root(str(root))


if __name__ == "__main__":
    unittest.main()
