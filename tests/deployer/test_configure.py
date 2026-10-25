from __future__ import annotations

import hashlib
import io
from pathlib import Path
from unittest import mock

from harness import DeployerTestCase, forward

from deployer import config, configure, platform_support


class ConfigureTests(DeployerTestCase):
    def digest(self) -> str:
        return hashlib.sha256(self.config_file().read_bytes()).hexdigest()

    def test_configure_writes_a_validated_lf_config(self) -> None:
        self.make_source_json()
        self.repos.mkdir()
        result = self.configure(stdin=f"{forward(self.repos)}\n\n\n\n")
        self.assertEqual(0, result.code, result.output)
        self.assertEqual(
            f"_source_id=test/skills\nREPOS_ROOT={forward(self.repos)}\nUPDATE_CHANNEL=release\n".encode(),
            self.config_file().read_bytes(),
        )
        self.assertEqual([self.config_file()], list(self.config_file().parent.iterdir()))

    def test_unknown_argument_is_rejected_without_touching_config(self) -> None:
        self.make_source_json()
        result = self.configure("--profile")
        self.assertEqual(2, result.code)
        self.assertIn("ERROR: unrecognized arguments: --profile", result.output)
        self.assertFalse(self.config_file().exists())

    def test_rerun_keeps_the_existing_value_when_enter_is_pressed(self) -> None:
        self.make_source_json()
        self.make_config()
        result = self.configure(stdin="\n\n\n\n")
        self.assertEqual(0, result.code, result.output)
        example = "C:/GitHub" if platform_support.WINDOWS else "/home/you/GitHub"
        self.assertIn(
            f"REPOS_ROOT: root directory for your git repositories, such as {example}\n"
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
        self.assertEqual(130, result.code)
        self.assertIn(configure.CANCELLED, result.output)
        self.assertEqual(before, self.digest())

    def test_an_input_that_ends_early_names_the_variable_it_needed(self) -> None:
        self.make_source_json()
        self.repos.mkdir()
        answers = [f"{forward(self.repos)}\n", *["\n"] * (len(config.PROMPTS) - 1)]
        for given, key in enumerate(config.PROMPTS):
            with self.subTest(key=key):
                result = self.configure(stdin="".join(answers[:given]))
                self.assertEqual(130, result.code, result.output)
                self.assertTrue(
                    result.output.endswith(
                        f"\nThe input ended before a value for {key}.\n"
                        "Configuration cancelled; existing config was not changed.\n\n"
                    ),
                    result.output,
                )
                self.assertFalse(self.config_file().exists())
        self.assertEqual(0, self.configure(stdin="".join(answers)).code)

    def test_validation_failure_preserves_the_existing_config(self) -> None:
        self.make_source_json()
        self.make_config()
        before = self.digest()
        result = self.configure(stdin="bad&path\n\n\n\n")
        self.assertEqual(1, result.code)
        self.assertIn("disallowed character '&'", result.output)
        self.assertEqual(before, self.digest())

    def test_a_failed_config_write_preserves_the_existing_config(self) -> None:
        self.make_source_json()
        self.make_config()
        before = self.digest()
        with mock.patch("deployer.fsops.write_atomic", side_effect=OSError("synthetic config write failure")):
            result = self.configure(stdin=f"{forward(self.repos)}\n\n\n\n")
        self.assertEqual(1, result.code, result.output)
        self.assertIn("synthetic config write failure", result.output)
        self.assertEqual(before, self.digest())
        self.assertEqual([self.config_file().name], [path.name for path in self.config_file().parent.iterdir()])

    def test_an_unreadable_existing_config_is_reported_as_a_failed_read_with_its_remedy(self) -> None:
        self.make_source_json()
        self.make_config()
        before = self.digest()
        target = self.config_file()
        # The read fails as a file an editor or a scanner holds does, inside the run only, so the harness still reads
        # the home around it; config.read turns the failure into the deployer's refusal.
        read_bytes = Path.read_bytes

        def denied(path: Path) -> bytes:
            if path == target:
                raise PermissionError(13, "Permission denied", str(target))
            return read_bytes(path)

        def run() -> int:
            with mock.patch.object(Path, "read_bytes", autospec=True, side_effect=denied):
                return configure.run([], self.paths, stdin=io.StringIO(f"{forward(self.repos)}\n\n\n\n"))

        result = self.run_command(run)
        self.assertEqual(1, result.code, result.output)
        self.assertIn(
            f"\nERROR: Could not read the configuration: {forward(target)}: Permission denied\n"
            "Check that the path exists and that you can read it, then retry.\n"
            "Rerun with --debug to see the traceback.\n",
            result.output,
        )
        self.assertNotIn("Could not write", result.output)
        self.assertEqual(before, self.digest())

    def test_a_failed_config_write_names_the_write_and_its_remedy(self) -> None:
        self.make_source_json()
        self.make_config()
        failure = PermissionError(13, "Access is denied", str(self.config_file()))
        with mock.patch("deployer.fsops.write_atomic", side_effect=failure):
            result = self.configure(stdin=f"{forward(self.repos)}\n\n\n\n")
        self.assertEqual(1, result.code, result.output)
        self.assertIn(
            f"\nERROR: Could not write the configuration: {forward(self.config_file())}: Access is denied\n"
            "Check that the path exists and that you can read it, then retry.\n"
            "Rerun with --debug to see the traceback.\n",
            result.output,
        )

    if platform_support.WINDOWS:

        def test_windows_style_path_input_is_normalized(self) -> None:
            self.make_source_json()
            repos = self.root / "My Repos"
            repos.mkdir()
            windows = str(repos).replace("/", "\\")
            for typed in (windows, f'"{windows}"', f"{windows}\\", f"  {windows}/  "):
                with self.subTest(typed=typed):
                    result = self.configure(stdin=f"{typed}\n\n\n\n")
                    self.assertEqual(0, result.code, result.output)
                    self.assertEqual(
                        f"_source_id=test/skills\nREPOS_ROOT={forward(repos)}\nUPDATE_CHANNEL=release\n".encode(),
                        self.config_file().read_bytes(),
                    )

        def test_drive_root_is_still_rejected_after_normalization(self) -> None:
            self.make_source_json()
            drive = forward(self.root)[:2]
            result = self.configure(stdin=f"{drive}\\\n\n\n\n")
            self.assertEqual(1, result.code)
            self.assertIn(f"ERROR: REPOS_ROOT must not be a filesystem root (got: {drive}/)", result.output)
            self.assertFalse(self.config_file().exists())

    else:

        def test_typed_path_input_loses_its_quotes_spaces_and_trailing_slashes(self) -> None:
            self.make_source_json()
            repos = self.root / "My Repos"
            repos.mkdir()
            typed_path = str(repos)
            for typed in (typed_path, f'"{typed_path}"', f"{typed_path}//", f"  {typed_path}/  "):
                with self.subTest(typed=typed):
                    result = self.configure(stdin=f"{typed}\n\n\n\n")
                    self.assertEqual(0, result.code, result.output)
                    self.assertEqual(
                        f"_source_id=test/skills\nREPOS_ROOT={repos}\nUPDATE_CHANNEL=release\n".encode(),
                        self.config_file().read_bytes(),
                    )

        def test_a_backslash_is_a_character_of_the_name_and_refused(self) -> None:
            # Off Windows a backslash separates nothing, so the value is refused rather than read as another path.
            self.make_source_json()
            typed = str(self.root / "My Repos").replace("/", "\\")
            result = self.configure(stdin=f"{typed}\n\n\n\n")
            self.assertEqual(1, result.code)
            self.assertIn(
                "ERROR: Config key REPOS_ROOT contains disallowed character '\\' at position 0", result.output
            )
            self.assertFalse(self.config_file().exists())

        def test_the_root_is_still_rejected_after_normalization(self) -> None:
            self.make_source_json()
            for typed in ("/", "//", "///"):
                with self.subTest(typed=typed):
                    result = self.configure(stdin=f"{typed}\n\n\n\n")
                    self.assertEqual(1, result.code)
                    self.assertIn("ERROR: REPOS_ROOT must not be a filesystem root (got: /)", result.output)
                    self.assertFalse(self.config_file().exists())

    def test_missing_directory_is_rejected_when_configuring(self) -> None:
        self.make_source_json()
        self.make_config()
        before = self.digest()
        missing = forward(self.root / "does-not-exist")
        result = self.configure(stdin=f"{missing}\n\n\n\n")
        self.assertEqual(1, result.code)
        self.assertIn(f"ERROR: REPOS_ROOT directory does not exist: {missing}", result.output)
        self.assertEqual(before, self.digest())

    def test_the_update_channel_is_offered_as_release_and_written_as_chosen(self) -> None:
        self.make_source_json()
        self.repos.mkdir()
        for typed, written in (("", "release"), ("main", "main"), ("  main ", "main"), ("release", "release")):
            with self.subTest(typed=typed):
                result = self.configure(stdin=f"{forward(self.repos)}\n{typed}\n\n\n")
                self.assertEqual(0, result.code, result.output)
                self.assertIn(
                    "UPDATE_CHANNEL: what update-coding-agent-skills deploys: release, the latest release, or main, "
                    "the head of main\n  Current: release\n",
                    result.output,
                )
                self.assertEqual(
                    f"_source_id=test/skills\nREPOS_ROOT={forward(self.repos)}\nUPDATE_CHANNEL={written}\n".encode(),
                    self.config_file().read_bytes(),
                )
                self.config_file().unlink()

    def test_rerun_keeps_a_saved_update_channel_when_enter_is_pressed(self) -> None:
        self.make_source_json()
        self.make_config(extra="UPDATE_CHANNEL=main\n")
        result = self.configure(stdin="\n\n\n\n")
        self.assertEqual(0, result.code, result.output)
        self.assertIn("  Current: main\n", result.output)
        self.assertIn("UPDATE_CHANNEL=main\n", self.config_file().read_text(encoding="utf-8"))

    def test_an_update_channel_other_than_release_or_main_is_refused_and_the_config_kept(self) -> None:
        self.make_source_json()
        self.make_config()
        before = self.digest()
        for typed, message in (
            ("beta", "ERROR: Config key UPDATE_CHANNEL must be one of release, main (got: beta)"),
            ("mainline", "ERROR: Config key UPDATE_CHANNEL must be one of release, main (got: mainline)"),
            ("Main", "ERROR: Config key UPDATE_CHANNEL contains disallowed character 'M' at position 0"),
        ):
            with self.subTest(typed=typed):
                result = self.configure(stdin=f"\n{typed}\n\n\n")
                self.assertEqual(1, result.code, result.output)
                self.assertIn(message, result.output)
                self.assertEqual(before, self.digest())

    def test_a_saved_update_channel_other_than_release_or_main_refuses_the_deployment(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Test")
        for saved in ("UPDATE_CHANNEL=nightly\n", "UPDATE_CHANNEL=\n"):
            with self.subTest(saved=saved):
                self.make_config(extra=saved)
                self.deploy_fails("--all", pattern="Config key UPDATE_CHANNEL must be one of release, main")

    def test_repo_cleanups_additions_are_offered_empty_and_written_as_typed(self) -> None:
        self.make_source_json()
        self.repos.mkdir()
        result = self.configure(
            stdin=f"{forward(self.repos)}\n\n.venv .tox=tox.ini,setup.cfg *.csproj\nhotfix/ support/\n"
        )
        self.assertEqual(0, result.code, result.output)
        self.assertIn(
            "REPO_CLEANUP_REGENERABLE: ignored directories repo-cleanup may delete with a worktree, besides the ones "
            "it ships with, separated by spaces: a directory, or a directory, an equals sign, and the project files "
            "one of which must sit beside it, such as .venv .tox=tox.ini,setup.cfg\n"
            "  Value (Enter for none, Ctrl+C cancels): ",
            result.output,
        )
        self.assertIn(
            "REPO_CLEANUP_PROTECTED: branch prefixes repo-cleanup never deletes, besides the ones it ships with, "
            "separated by spaces, such as hotfix/ support/\n  Value (Enter for none, Ctrl+C cancels): ",
            result.output,
        )
        self.assertEqual(
            f"_source_id=test/skills\nREPOS_ROOT={forward(self.repos)}\nUPDATE_CHANNEL=release\n"
            "REPO_CLEANUP_REGENERABLE=.venv .tox=tox.ini,setup.cfg *.csproj\nREPO_CLEANUP_PROTECTED=hotfix/ support/\n"
            "".encode(),
            self.config_file().read_bytes(),
        )
        # Enter keeps both on a rerun; the script, not the configuration, refuses an entry that could hold work.
        result = self.configure(stdin="\n\n\n\n")
        self.assertEqual(0, result.code, result.output)
        self.assertIn("  Current: .venv .tox=tox.ini,setup.cfg *.csproj\n", result.output)
        self.assertIn("REPO_CLEANUP_PROTECTED=hotfix/ support/\n", self.config_file().read_text(encoding="utf-8"))

    def test_a_lone_dash_clears_a_repo_cleanup_list_already_set_and_the_prompt_says_so(self) -> None:
        self.make_source_json()
        self.repos.mkdir()
        set_both = f"{forward(self.repos)}\n\n.venv\nhotfix/\n"
        self.assertEqual(0, self.configure(stdin=set_both).code)
        result = self.configure(stdin="\n\n - \n-\n")
        self.assertEqual(0, result.code, result.output)
        for current in (".venv", "hotfix/"):
            self.assertIn(
                f"  Current: {current}\n  New value (Enter keeps the current value, - clears it, Ctrl+C cancels): ",
                result.output,
            )
        self.assertIn(
            "  New value (Enter keeps the current value, Ctrl+C cancels): ", result.output, "a required value has no -"
        )
        self.assertEqual(
            f"_source_id=test/skills\nREPOS_ROOT={forward(self.repos)}\nUPDATE_CHANNEL=release\n".encode(),
            self.config_file().read_bytes(),
        )

    def test_a_repo_cleanup_addition_with_a_character_outside_its_allowlist_is_refused_and_the_config_kept(
        self,
    ) -> None:
        self.make_source_json()
        self.make_config()
        before = self.digest()
        for typed, message in (
            (
                "\n\nC:/cache\n\n",
                "ERROR: Config key REPO_CLEANUP_REGENERABLE contains disallowed character ':' at position 1",
            ),
            (
                "\n\na\\b\n\n",
                "ERROR: Config key REPO_CLEANUP_REGENERABLE contains disallowed character '\\' at position 1",
            ),
            (
                "\n\n.tox;x\n\n",
                "ERROR: Config key REPO_CLEANUP_REGENERABLE contains disallowed character ';' at position 4",
            ),
            (
                "\n\n\nhot*/\n",
                "ERROR: Config key REPO_CLEANUP_PROTECTED contains disallowed character '*' at position 3",
            ),
            (
                "\n\n\nhot=fix\n",
                "ERROR: Config key REPO_CLEANUP_PROTECTED contains disallowed character '=' at position 3",
            ),
        ):
            with self.subTest(typed=typed):
                result = self.configure(stdin=typed)
                self.assertEqual(1, result.code, result.output)
                self.assertIn(message, result.output)
                self.assertEqual(before, self.digest())

    def test_configured_variables_take_their_defaults_when_a_configuration_lacks_them(self) -> None:
        self.assertEqual(
            {"UPDATE_CHANNEL": "release", "REPO_CLEANUP_REGENERABLE": "", "REPO_CLEANUP_PROTECTED": ""},
            config.DEFAULTS,
        )
        # Only these may stay empty: a skill that requires one takes the empty value as none.
        self.assertEqual(("REPO_CLEANUP_REGENERABLE", "REPO_CLEANUP_PROTECTED"), config.OPTIONAL_VARIABLES)

    def test_a_skill_requiring_an_unset_optional_variable_deploys_and_another_unset_one_is_refused(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", 'Lists "{{REPO_CLEANUP_PROTECTED}}"', ["REPO_CLEANUP_PROTECTED"])
        self.make_config()
        self.deploy_ok("--all")
        self.assertIn('Lists ""', self.skill_text("alpha"))
        self.make_skill("beta", 'Root "{{REPOS_ROOT}}"', ["REPOS_ROOT"])
        self.write(self.config_file(), "_source_id=test/skills\n")
        self.deploy_fails("--all", pattern="Selected skills require variables not set in config: REPOS_ROOT")

    def test_the_update_channel_is_release_or_main_and_defaults_to_release(self) -> None:
        self.assertEqual({"UPDATE_CHANNEL": ("release", "main")}, config.CHOICE_VARIABLES)
        self.assertEqual("release", config.DEFAULTS["UPDATE_CHANNEL"])
        self.assertLessEqual(set(config.CHOICE_VARIABLES), set(config.CONFIGURED_VARIABLES))

    def test_directory_variables_are_configured_variables(self) -> None:
        self.assertEqual(("REPOS_ROOT",), config.DIRECTORY_VARIABLES)
        self.assertLessEqual(set(config.DIRECTORY_VARIABLES), set(config.CONFIGURED_VARIABLES))

    def test_invalid_existing_config_aborts_before_prompting(self) -> None:
        self.make_source_json()
        self.make_config(extra="not a key-value line\n")
        result = self.configure(stdin=f"{forward(self.repos)}\n\n\n\n")
        self.assertEqual(1, result.code)
        self.assertIn("Config line 3: malformed entry: not a key-value line", result.output)
        self.assertNotIn("REPOS_ROOT", result.output)

    def test_a_key_no_longer_declared_is_dropped_with_a_notice(self) -> None:
        self.make_source_json()
        self.make_config(extra="GH_ORG=someone\n")
        result = self.configure(stdin="\n\n\n\n")
        self.assertEqual(0, result.code, result.output)
        self.assertIn("NOTE: Dropping GH_ORG, which this deployer no longer reads.", result.output)
        self.assertEqual(
            f"_source_id=test/skills\nREPOS_ROOT={forward(self.repos)}\nUPDATE_CHANNEL=release\n".encode(),
            self.config_file().read_bytes(),
        )
        self.deploy_ok("--all", "--dry-run")

    def test_a_dropped_key_stays_when_configure_is_cancelled(self) -> None:
        self.make_source_json()
        self.make_config(extra="GH_ORG=someone\n")
        before = self.digest()
        result = self.configure(stdin="")
        self.assertEqual(130, result.code, result.output)
        self.assertEqual(before, self.digest())

    def test_atomic_replace_failure_preserves_config_and_cleans_temporary_file(self) -> None:
        self.make_source_json()
        self.make_config()
        before = self.digest()
        # The replace inside fsops.write_atomic, after the temporary copy is written.
        with mock.patch.object(Path, "replace", side_effect=OSError("synthetic replace failure")):
            result = self.configure(stdin="\n\n\n\n")
        self.assertEqual(1, result.code)
        self.assertIn("synthetic replace failure", result.output)
        self.assertEqual(before, self.digest())
        self.assertEqual([], list(self.config_file().parent.glob(".*.config.tmp.*")))
