"""Regression tests for the deployed skill roots scripts refuse to write inside."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_roots import deployed_skill_roots


class DeployedSkillRootsTests(unittest.TestCase):
    def test_the_two_roots_the_deployer_owns_under_the_home(self) -> None:
        home = Path("a home") / "with spaces"
        with mock.patch("pathlib.Path.home", return_value=home):
            self.assertEqual((home / ".claude" / "skills", home / ".agents" / "skills"), deployed_skill_roots())


if __name__ == "__main__":
    unittest.main()
