"""The deployer's readers of its own state files and the source's JSON, held to one corpus of what they cannot read.

Each reader refuses a file that is not JSON it can read, such as one holding an integer past Python's digit limit or
lists nested past the recursion limit, and a directory where its file belongs, by name and before anything is planned
or changed: neither may escape as an unclassified failure, nor be read as no file at all.
"""

from __future__ import annotations

import errno
import functools
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from harness import SOURCE_ID, ZERO_HASH, DeployerTestCase, install_entry, journal_line, probe_returning

from deployer import discovery, fsops, manifest, platform_support, render, state_files
from deployer.errors import DeployError
from deployer.journal import Journal, valid_entry

# An integer past the digit limit and lists nested past the recursion limit, each of which json_reader refuses as a
# ValueError, and tomllib raises ValueError and RecursionError for.
UNREADABLE = {
    "digits": '{"manifest_version": ' + "1" * 5000 + "}",
    "nesting": "[" * 100000 + "]" * 100000,
}
RUN_ID = "20260101-000000-bad1"
# Each refusal's remedy, written out here rather than read from the deployer, so a changed remedy fails a test.
STATE_FILE_SECTION = 'See "A state file that does not read" in docs/recovery.md.'
MANIFEST_REMEDY = (
    "Put back a copy you trust, or move it out of the skills folder and rerun with --dry-run.",
    STATE_FILE_SECTION,
)
# The command that runs Python is the platform's: python on Windows, python3 on Linux.
CONFIGURATION_REMEDY = (f"Move it aside, then run '{platform_support.PYTHON} deploy.py configure'.", STATE_FILE_SECTION)


def shown(path: object) -> str:
    return re.escape(platform_support.normalize(str(path)))


class ReaderTests(DeployerTestCase):
    def test_json_past_the_limits_or_not_json_reads_as_none_and_json_as_its_value(self) -> None:
        refused = {"a repeated key": '{"a": 1, "a": 2}', "NaN": '{"a": NaN}', "Infinity": "[-Infinity]"}
        for case, text in {**UNREADABLE, **refused, "not json": "{", "a lone surrogate": '"\\ud800'}.items():
            with self.subTest(case=case):
                self.assertIsNone(state_files.parse_json(text))
        self.assertEqual({"a": [1, 2]}, state_files.parse_json('{"a": [1, 2]}'))
        self.assertEqual(7, state_files.parse_json("7"))

    def test_a_file_is_present_nothing_is_absent_and_anything_else_is_refused_by_name(self) -> None:
        path = self.root / "state.json"
        self.assertFalse(state_files.is_present(path, "The state"))
        path.write_text("{}", encoding="utf-8")
        self.assertTrue(state_files.is_present(path, "The state"))
        path.unlink()
        path.mkdir()
        with self.assertRaises(DeployError) as raised:
            state_files.is_present(path, "The state", "Move it aside.", "Then retry.")
        self.assertEqual(
            (f"ERROR: The state at {platform_support.normalize(path)} is not a file.", "Move it aside.", "Then retry."),
            raised.exception.lines,
        )

    def test_a_journal_entry_with_any_key_of_another_type_is_malformed_never_an_error(self) -> None:
        # Each entry as the deployer writes it, in a root it records and kept, so a key a writer adds gets its rows.
        record = Journal(self.root / "journal.jsonl", RUN_ID)
        record.create()
        record.backup("agents", "alpha", ZERO_HASH, retain=True)
        record.install("agents", "alpha", ZERO_HASH)
        record.preserve("agents", "alpha")
        for entry in record.entries:
            self.assertTrue(valid_entry(entry, RUN_ID), entry)
            for key, kept in entry.items():
                for value in ([], ["agents"], {}, {"agents": 1}, 1, None, True):
                    if type(value) is type(kept):
                        continue
                    with self.subTest(op=entry["op"], key=key, value=value):
                        self.assertFalse(valid_entry({**entry, key: value}, RUN_ID))

    def test_a_state_file_whose_stat_fails_is_refused_with_the_reason_not_read_as_absent(self) -> None:
        path = self.root / "state.json"
        denied = PermissionError(errno.EACCES, "Permission denied", str(path))
        with mock.patch.object(Path, "stat", side_effect=denied), self.assertRaises(DeployError) as raised:
            state_files.is_present(path, "The manifest")
        self.assertEqual(
            (
                f"ERROR: Could not tell whether the manifest is there: {platform_support.normalize(path)}: "
                "Permission denied",
                "Check that the path exists and that you can read it, then retry.",
                "Rerun with --debug to see the traceback.",
            ),
            raised.exception.lines,
        )
        self.assertIs(denied, raised.exception.__cause__)
        with self.assertRaises(DeployError) as raised:
            state_files.is_present(self.root / "state\0.json", "The manifest")
        self.assertIn("NUL character", raised.exception.lines[0])


class StateFileTests(DeployerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()

    def test_a_manifest_the_deployer_cannot_read_is_refused_by_name_with_the_step_to_take(self) -> None:
        self.deploy_ok("--all")
        for case, text in UNREADABLE.items():
            with self.subTest(case=case):
                self.write(self.manifest_file, text)
                for arguments in (("--all",), ("--all", "--dry-run")):
                    result = self.deploy_fails(*arguments, pattern=re.escape("ERROR: Manifest is malformed: "))
                    self.assert_lines(result.output, *MANIFEST_REMEDY)
                    self.assertNotIn("FAILED", result.output)
                checked = self.run_cli("check")
                self.assertIn("ERROR: Manifest is malformed: ", checked.output)

    def test_a_manifest_an_earlier_release_wrote_with_a_lone_surrogate_is_refused_as_that_with_the_step_to_take(
        self,
    ) -> None:
        # Up to v0.11.1 the deployer wrote its state through json.dumps, which escapes a lone surrogate.
        self.deploy_ok("--all")
        data = self.manifest()
        source = data["sources"][SOURCE_ID]
        source["source_dir"] = source["source_dir"] + "\udcff"
        self.write(self.manifest_file, json.dumps(data, indent=2) + "\n")
        refusal = (
            f"ERROR: Manifest {platform_support.normalize(self.manifest_file)} holds a lone surrogate, which an"
            " earlier release of the deployer could write and this one does not read."
        )
        for arguments in (("--all",), ("--all", "--dry-run")):
            with self.subTest(arguments=arguments):
                result = self.deploy_fails(*arguments, pattern=re.escape(refusal))
                self.assert_lines(result.output, *MANIFEST_REMEDY)
                self.assertNotIn("Manifest is malformed", result.output)
                self.assertNotIn("FAILED", result.output)
        self.assertIn(refusal, self.run_cli("check").output)

    def test_a_manifest_holding_text_beyond_ascii_reads_as_utf_8_whatever_the_locale(self) -> None:
        # The deployer escapes what it writes to ASCII, but a copy put back by hand may hold UTF-8 itself, which a
        # locale's code page, such as Windows' cp1252 outside UTF-8 mode, would read as other characters.
        self.deploy_ok("--all")
        data = self.manifest()
        source_dir = data["sources"][SOURCE_ID]["source_dir"] + "/Zoë Ünal checkout"
        data["sources"][SOURCE_ID]["source_dir"] = source_dir
        self.manifest_file.write_bytes((json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        loaded = manifest.load(self.manifest_file)
        self.assertEqual(source_dir, loaded.data["sources"][SOURCE_ID]["source_dir"])

    def test_a_lone_surrogate_is_told_apart_from_json_that_is_malformed_otherwise(self) -> None:
        cases = [
            ("a lone surrogate", '{"source_dir": "/src\\udcff"}', True),
            ("a lone high surrogate", '["\\ud800"]', True),
            ("a surrogate pair", '["\\ud83d\\ude00"]', False),
            ("plain", '{"a": 1}', False),
            ("null", "null", False),
            ("not json", '{"a": "\\udcff"', False),
            ("a surrogate and a repeated key", '{"a": "\\udcff", "a": 1}', False),
        ]
        cases += [(name, text, False) for name, text in UNREADABLE.items()]
        for case, text, expected in cases:
            with self.subTest(case=case):
                self.assertIs(expected, state_files.holds_lone_surrogate(text))

    def test_a_directory_in_place_of_the_manifest_is_refused_before_a_dry_run_promises_a_fresh_install(self) -> None:
        self.manifest_file.mkdir(parents=True)
        refusal = f"ERROR: The manifest at {shown(self.manifest_file)} is not a file."
        for arguments in (("--all", "--dry-run"), ("--all",)):
            with self.subTest(arguments=arguments):
                result = self.deploy_fails(*arguments, pattern=refusal)
                self.assertNotIn("FRESH INSTALL", result.output)
                self.assertNotIn("INSTALLED", result.output)
                self.assert_lines(result.output, *MANIFEST_REMEDY)

    def test_a_directory_in_place_of_the_configuration_is_refused_by_deployment_and_configure(self) -> None:
        config_file = self.config_file()
        config_file.unlink()
        config_file.mkdir()
        refusal = f"ERROR: The configuration at {shown(config_file)} is not a file."
        result = self.deploy_fails("--all", "--dry-run", pattern=refusal)
        self.assertNotIn("No config found", result.output)
        self.assert_lines(result.output, *CONFIGURATION_REMEDY)
        for arguments in ((), ("--reset",)):
            with self.subTest(arguments=arguments):
                configured = self.configure(*arguments, stdin=f"{self.repos}\n")
                self.assertEqual(1, configured.code, configured.output)
                self.assertRegex(configured.output, refusal)
                self.assert_lines(configured.output, *CONFIGURATION_REMEDY)
                self.assertNotIn("REPOS_ROOT", configured.output)

    def test_a_journal_line_the_deployer_cannot_read_stops_recovery_and_keeps_the_run(self) -> None:
        self.deploy_ok("--all")
        for case, text in UNREADABLE.items():
            with self.subTest(case=case):
                journal = self.staging_run(RUN_ID) / "journal.jsonl"
                self.write(journal, text + "\n")
                result = self.deploy_fails("--all", pattern=f"Malformed journal entry in run {RUN_ID}")
                self.assertNotIn("the next deployment retries it", result.output)
                self.assertTrue(journal.is_file())
                dry = self.deploy_fails("--all", "--dry-run", pattern=f"ERROR: Run {RUN_ID} cannot be recovered")
                self.assertNotIn("FAILED", dry.output)

    def test_a_journal_line_an_earlier_release_wrote_with_a_lone_surrogate_stops_recovery_and_keeps_the_run(
        self,
    ) -> None:
        self.deploy_ok("--all")
        entry = install_entry("alpha", ZERO_HASH)
        entry["to"] += "\udcff"
        journal = self.staging_run(RUN_ID) / "journal.jsonl"
        self.write(journal, journal_line(entry))
        result = self.deploy_fails("--all", pattern=f"Malformed journal entry in run {RUN_ID}")
        self.assertIn("\\udcff", result.stderr, "the line is shown as the journal holds it")
        self.assertNotIn("the next deployment retries it", result.output)
        self.assertNotIn("FAILED", result.output)
        self.assertTrue(journal.is_file())
        self.deploy_fails("--all", "--dry-run", pattern=f"ERROR: Run {RUN_ID} cannot be recovered")

    def test_a_journal_entry_whose_root_or_from_is_a_list_an_object_or_a_number_stops_recovery_as_malformed(
        self,
    ) -> None:
        self.deploy_ok("--all")
        journal = self.staging_run(RUN_ID) / "journal.jsonl"
        for key, value in (("root", []), ("root", {}), ("root", 1), ("from", []), ("from", {})):
            with self.subTest(key=key, value=value):
                entry = install_entry("alpha", ZERO_HASH)
                entry[key] = value
                self.write(journal, journal_line(entry))
                # The second deployment meets the same run, which recovery kept for the user rather than retrying.
                for _ in range(2):
                    result = self.deploy_fails("--all", pattern=f"Malformed journal entry in run {RUN_ID}")
                    self.assertNotIn("the next deployment retries it", result.output)
                    self.assertNotIn("TypeError", result.output)
                dry = self.deploy_fails("--all", "--dry-run", pattern=f"ERROR: Run {RUN_ID} cannot be recovered")
                self.assertNotIn("FAILED", dry.output)
                self.assertTrue(journal.is_file())

    def test_a_dry_run_refuses_each_state_the_deployment_refuses_before_recovery_pending_run_or_not(self) -> None:
        # The deployment judges the lock, reads the configuration and the manifest, and checks the recorded checkout
        # before it recovers anything, so a dry run makes the same checks before it reports a pending recovery or plans.
        # A journal it cannot read is the journal's own refusal, held above. A live holder's own journal is never
        # reported as pending: the lock refuses first.
        self.deploy_ok("--all")
        saved = self.manifest_file.read_bytes()
        config_file = self.config_file()
        configured = config_file.read_bytes()

        def damage_manifest(directory: bool) -> None:
            self.manifest_file.unlink()
            if directory:
                self.manifest_file.mkdir()
            else:
                self.write(self.manifest_file, "{")

        def damage_configuration() -> None:
            config_file.unlink()
            config_file.mkdir()

        def other_checkout() -> None:
            data = json.loads(saved)
            other = self.root / "other-clone"
            other.mkdir(exist_ok=True)
            data["sources"][SOURCE_ID]["source_dir"] = platform_support.normalize(other)
            self.write(self.manifest_file, json.dumps(data))

        def held_lock(info: str) -> None:
            self.write(self.lock_dir / "info.json", info)

        live = probe_returning(alive=True, start_time=1)
        # A manifest moved aside beside a run's journal: the manifest alone says whether the run was committed, so
        # neither answer rolls it back. With no run pending, a missing manifest is a home nothing was deployed to.
        missing = (
            f"ERROR: The manifest {platform_support.normalize(self.manifest_file)} is missing, but run {RUN_ID}'s "
            "journal remains, so whether that run was committed cannot be told."
        )
        pending_only = {"manifest missing beside a pending run"}
        rows: dict[str, tuple[Callable[[], None], str, dict[str, Any]]] = {
            "manifest missing beside a pending run": (self.manifest_file.unlink, missing, {}),
            "malformed manifest": (functools.partial(damage_manifest, False), "ERROR: Manifest is malformed: ", {}),
            "manifest not a file": (functools.partial(damage_manifest, True), "ERROR: The manifest at ", {}),
            "configuration not a file": (damage_configuration, "ERROR: The configuration at ", {}),
            "another checkout recorded": (other_checkout, f"ERROR: Source '{SOURCE_ID}' is deployed from ", {}),
            "lock metadata with no PID": (
                functools.partial(held_lock, '{"token": "x"}'),
                "ERROR: Lock metadata is malformed (no valid PID): ",
                {},
            ),
            "lock held by a live deployment": (
                functools.partial(held_lock, '{"pid": 4242, "start_time": 1, "token": "4242-1-ab12"}'),
                "ERROR: Another deployment is running (PID 4242).",
                {"probe": live},
            ),
        }
        # Each row with a pending run beside it, and with none, where the dry run would otherwise plan.
        for case, (damage, refusal, options) in rows.items():
            for pending in (True,) if case in pending_only else (True, False):
                with self.subTest(case=case, pending=pending):
                    if pending:
                        journal = self.staging_run(RUN_ID) / "journal.jsonl"
                        self.write(journal, journal_line(install_entry("ghost", ZERO_HASH)))
                    damage()
                    answers = []
                    for arguments in (("--all",), ("--all", "--dry-run")):
                        result = self.deploy_fails(*arguments, pattern=re.escape(refusal), **options)
                        self.assertNotIn("Pending recovery", result.output)
                        self.assertNotIn("Recovering", result.output)
                        self.assertNotIn("UNCHANGED", result.output)
                        answers.append([line for line in result.output.splitlines() if line.startswith("ERROR: ")])
                    self.assertEqual(answers[0], answers[1])
                    for path, content in ((self.manifest_file, saved), (config_file, configured)):
                        if path.is_dir():
                            path.rmdir()
                        path.write_bytes(content)
                    fsops.remove(self.staging_run(RUN_ID))
                    fsops.remove(self.lock_dir)

    def test_a_directory_in_place_of_a_journal_stops_recovery_and_keeps_what_the_run_holds(self) -> None:
        self.deploy_ok("--all")
        run = self.staging_run(RUN_ID)
        (run / "journal.jsonl").mkdir()
        kept = run / "alpha"
        self.write(kept / "SKILL.md", "What the run moved aside")
        shown_run = platform_support.normalize(run)
        cleanup = (
            f"  When no .deploying-bak remains, delete {shown_run} and rerun with --dry-run. "
            'See "When recovery fails" in docs/recovery.md.'
        )
        for committed, state in (
            (False, "was not committed to the manifest, so put back the copies it replaced."),
            (True, "was committed to the manifest, so keep the copies it installed."),
        ):
            with self.subTest(committed=committed):
                if committed:
                    data = self.manifest()
                    data["last_run_id"] = RUN_ID
                    self.write_manifest(data)
                result = self.deploy_fails("--all", pattern=re.escape(f"ERROR: Run {RUN_ID}'s journal is not a file."))
                self.assert_lines(
                    result.stderr,
                    f"  ERROR: Run {RUN_ID}'s journal is not a file. Retaining {shown_run} for manual inspection.",
                    f"  Run {RUN_ID} {state} A replaced copy sits beside its item as <name>.deploying-bak; a retained "
                    f"backup is under .backups/{RUN_ID}/ in the same root.",
                    cleanup,
                )
                self.assertTrue((kept / "SKILL.md").is_file())
        self.deploy_fails("--all", "--dry-run", pattern=f"ERROR: Run {RUN_ID} cannot be recovered")

    def test_a_dry_run_lists_a_pending_run_after_a_run_with_no_journal(self) -> None:
        self.deploy_ok("--all")
        # pending_runs visits the runs sorted by name, so the run with no journal is always visited first.
        empty = "20260101-000000-aaa0"
        self.staging_run(empty)
        later = "20260101-000000-bad2"
        self.assertLess(empty, later)
        self.write(self.staging_run(later) / "journal.jsonl", journal_line(install_entry("alpha", ZERO_HASH)))
        result = self.deploy_ok("--all", "--dry-run")
        self.assert_lines(result.output, f"Pending recovery: the next deployment will recover run {later}.")
        self.assertNotIn(empty, result.output)

    def test_source_json_with_a_byte_order_mark_still_reads(self) -> None:
        source_json = self.source / "source.json"
        source_json.write_bytes(b"\xef\xbb\xbf" + source_json.read_bytes())
        self.deploy_ok("--all", "--dry-run")

    def test_a_dry_run_lists_every_run_after_one_whose_journal_is_not_a_file(self) -> None:
        self.deploy_ok("--all")
        (self.staging_run(RUN_ID) / "journal.jsonl").mkdir()
        later = "20260101-000000-bad2"
        self.write(self.staging_run(later) / "journal.jsonl", journal_line(install_entry("alpha", ZERO_HASH)))
        result = self.deploy_fails("--all", "--dry-run", pattern=f"ERROR: Run {RUN_ID} cannot be recovered")
        self.assert_lines(result.output, f"Pending recovery: the next deployment will recover run {later}.")

    def test_lock_metadata_the_deployer_cannot_read_is_refused_with_the_step_to_take(self) -> None:
        self.deploy_ok("--all")
        # Up to v0.11.1 the lock's metadata was written through json.dumps, which escapes a lone surrogate.
        earlier = {"a lone surrogate": json.dumps({"pid": 1, "token": "old-token\udcff"})}
        for case, text in {**UNREADABLE, **earlier}.items():
            with self.subTest(case=case):
                self.lock_dir.mkdir(parents=True, exist_ok=True)
                self.write(self.lock_dir / "token", "old-token")
                self.write(self.lock_dir / "info.json", text)
                lock = platform_support.normalize(self.lock_dir)
                refusal = (
                    f"ERROR: Lock metadata is malformed: {lock}/info.json is not a JSON object the deployer reads."
                )
                result = self.deploy_fails("--all", pattern=re.escape(refusal))
                self.assertIn(
                    f"If no deployment is running, the lock is stale: remove {lock}, then retry.", result.output
                )

    def test_source_json_and_metadata_the_deployer_cannot_read_are_refused_with_their_documented_lines(self) -> None:
        metadata = self.source / "deploy-meta" / "alpha.json"
        saved = metadata.read_text(encoding="utf-8")
        for case, text in UNREADABLE.items():
            with self.subTest(case=case, file="deploy-meta"):
                self.write(metadata, text)
                self.deploy_fails("--all", "--dry-run", pattern=re.escape("deploy-meta/alpha.json has an invalid"))
        self.write(metadata, saved)
        source_json = self.source / "source.json"
        document = json.loads(source_json.read_text(encoding="utf-8"))
        self.assertEqual(SOURCE_ID, document["id"])
        for case, text in UNREADABLE.items():
            with self.subTest(case=case, file="source.json"):
                self.write(source_json, text)
                refusal = (
                    f"ERROR: source.json is malformed: {shown(source_json)} is not a JSON object the deployer reads."
                )
                self.deploy_fails("--all", "--dry-run", pattern=refusal)
        source_json.unlink()
        source_json.mkdir()
        self.deploy_fails("--all", "--dry-run", pattern=f"ERROR: source.json at {shown(source_json)} is not a file.")


class RenderedAndListedTests(DeployerTestCase):
    def test_rendered_json_and_toml_past_the_limits_fail_their_validation_by_name(self) -> None:
        for value in ("1" * 5000, UNREADABLE["nesting"]):
            with self.assertRaisesRegex(DeployError, "Rendered JSON validation failed: s/config.json"):
                render.render_file("s/config.json", b'{"root": {{X}}}', {"X": value})
        with self.assertRaisesRegex(DeployError, "Rendered TOML validation failed: s/config.toml"):
            render.render_file("s/config.toml", b"root = {{X}}\n", {"X": UNREADABLE["nesting"]})

    def test_a_runtime_listing_past_the_limits_is_refused_as_not_json(self) -> None:
        for case, text in UNREADABLE.items():
            with self.subTest(case=case):
                with self.assertRaisesRegex(discovery.ListingError, "its skill list is not JSON"):
                    discovery.parse_copilot(text)
                with self.assertRaisesRegex(discovery.ListingError, "its prompt input is not JSON"):
                    discovery.parse_codex_catalog([text])
                # A line of the app server's output the reader refuses is no answer, so it is passed over.
                self.assertFalse(discovery.codex_answered(text))
                with self.assertRaisesRegex(discovery.ListingError, "it did not answer the skills/list request"):
                    discovery.parse_codex([text])
