from __future__ import annotations

import hashlib
import unittest
from unittest import mock

from harness import DeployerTestCase, forward

from deployer import config


class ConfigureTests(DeployerTestCase):
    def digest(self) -> str:
        return hashlib.sha256(self.config_file().read_bytes()).hexdigest()

    def test_configure_writes_a_validated_lf_config(self) -> None:
        self.make_source_json()
        self.repos.mkdir()
        result = self.configure(stdin=f"{forward(self.repos)}\n")
        self.assertEqual(0, result.code, result.output)
        self.assertEqual(
            f"_source_id=test/skills\nREPOS_ROOT={forward(self.repos)}\n".encode(),
            self.config_file().read_bytes(),
        )
        self.assertEqual([self.config_file()], list(self.config_file().parent.iterdir()))

    def test_unknown_argument_is_rejected_without_touching_config(self) -> None:
        self.make_source_json()
        result = self.configure("--profile")
        self.assertEqual(1, result.code)
        self.assertIn("ERROR: unrecognized arguments: --profile", result.output)
        self.assertFalse(self.config_file().exists())

    def test_rerun_keeps_the_existing_value_when_enter_is_pressed(self) -> None:
        self.make_source_json()
        self.make_config()
        result = self.configure(stdin="\n")
        self.assertEqual(0, result.code, result.output)
        self.assertIn(
            "REPOS_ROOT: root directory for your git repositories, such as C:/GitHub\n"
            f"  Current: {forward(self.repos)}\n"
            "  New value (Enter keeps the current value, Ctrl+C cancels): ",
            result.output,
        )
        self.assertIn(f"REPOS_ROOT={forward(self.repos)}\n", self.config_file().read_text(encoding="utf-8"))

    def test_cancelled_reset_preserves_the_existing_config(self) -> None:
        self.make_source_json()
        self.make_config()
        before = self.digest()
        result = self.configure("--reset", stdin="")
        self.assertEqual(1, result.code)
        self.assertIn("Configuration cancelled; existing config was not changed.", result.output)
        self.assertEqual(before, self.digest())

    def test_validation_failure_preserves_the_existing_config(self) -> None:
        self.make_source_json()
        self.make_config()
        before = self.digest()
        result = self.configure(stdin="bad&path\n")
        self.assertEqual(1, result.code)
        self.assertIn("disallowed character '&'", result.output)
        self.assertEqual(before, self.digest())

    def test_a_failed_config_write_preserves_the_existing_config(self) -> None:
        self.make_source_json()
        self.make_config()
        before = self.digest()
        with mock.patch("deployer.fsops.write_private", side_effect=OSError("synthetic config write failure")):
            result = self.configure(stdin=f"{forward(self.repos)}\n")
        self.assertEqual(1, result.code, result.output)
        self.assertIn("synthetic config write failure", result.output)
        self.assertEqual(before, self.digest())
        self.assertEqual([self.config_file().name], [path.name for path in self.config_file().parent.iterdir()])

    def test_windows_style_path_input_is_normalized(self) -> None:
        self.make_source_json()
        repos = self.root / "My Repos"
        repos.mkdir()
        windows = str(repos).replace("/", "\\")
        for typed in (windows, f'"{windows}"', f"{windows}\\", f"  {windows}/  "):
            with self.subTest(typed=typed):
                result = self.configure(stdin=f"{typed}\n")
                self.assertEqual(0, result.code, result.output)
                self.assertEqual(
                    f"_source_id=test/skills\nREPOS_ROOT={forward(repos)}\n".encode(),
                    self.config_file().read_bytes(),
                )

    def test_missing_directory_is_rejected_when_configuring(self) -> None:
        self.make_source_json()
        self.make_config()
        before = self.digest()
        missing = forward(self.root / "does-not-exist")
        result = self.configure(stdin=f"{missing}\n")
        self.assertEqual(1, result.code)
        self.assertIn(f"ERROR: REPOS_ROOT directory does not exist: {missing}", result.output)
        self.assertEqual(before, self.digest())

    def test_drive_root_is_still_rejected_after_normalization(self) -> None:
        self.make_source_json()
        drive = forward(self.root)[:2]
        result = self.configure(stdin=f"{drive}\\\n")
        self.assertEqual(1, result.code)
        self.assertIn(f"ERROR: REPOS_ROOT must not be a filesystem root (got: {drive}/)", result.output)
        self.assertFalse(self.config_file().exists())

    def test_directory_variables_are_configured_variables(self) -> None:
        self.assertEqual(("REPOS_ROOT",), config.DIRECTORY_VARIABLES)
        self.assertLessEqual(set(config.DIRECTORY_VARIABLES), set(config.CONFIGURED_VARIABLES))

    def test_invalid_existing_config_aborts_before_prompting(self) -> None:
        self.make_source_json()
        self.make_config(extra="UNKNOWN=1\n")
        result = self.configure(stdin=f"{forward(self.repos)}\n")
        self.assertEqual(1, result.code)
        self.assertIn("Config key UNKNOWN is not a recognized variable", result.output)
        self.assertNotIn("REPOS_ROOT", result.output)

    def test_atomic_replace_failure_preserves_config_and_cleans_temporary_file(self) -> None:
        self.make_source_json()
        self.make_config()
        before = self.digest()
        # The replace inside fsops.write_private, after the temporary copy is written.
        with mock.patch("deployer.fsops.os.replace", side_effect=OSError("synthetic replace failure")):
            result = self.configure(stdin="\n")
        self.assertEqual(1, result.code)
        self.assertIn("synthetic replace failure", result.output)
        self.assertEqual(before, self.digest())
        self.assertEqual([], list(self.config_file().parent.glob(".*.config.tmp.*")))


if __name__ == "__main__":
    unittest.main()
