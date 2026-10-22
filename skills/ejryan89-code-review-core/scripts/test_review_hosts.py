"""The Copilot CLI host a run's reviewer works on: the command it starts, finding Copilot and reading its version, the
paths and reviewer resources it refuses, and the result it promotes. A host's claim is tested in
test_review_pipeline.py."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from review_hosts import (
    HostSuperseded,
    ProcessResult,
    copilot_command,
    find_copilot,
    parse_copilot_version,
    run_copilot,
)
from review_runtime import SOURCE_SNAPSHOT_MANIFEST, RuntimeContractError


class RuntimeHostTests(unittest.TestCase):
    @staticmethod
    def _promote_fixture_result(staging: Path, result: Path) -> bool:
        """Rename a result in this test's owned temporary fixture."""
        staging.replace(result)
        return True

    @staticmethod
    def _materialized_reviewer(root: Path) -> tuple[Path, Path]:
        materialized = root / "trusted"
        materialized.mkdir()
        entrypoint = materialized / "SKILL.md"
        content = b"# Trusted reviewer\n"
        entrypoint.write_bytes(content)
        rules = materialized / "rules.md"
        rules_content = b"# Trusted rules\n"
        rules.write_bytes(rules_content)
        (materialized / "materialization.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "adapter_id": "fixture",
                    "entrypoint": "SKILL.md",
                    "source_commit": "a" * 40,
                    "source_hashes": {
                        "SKILL.md": hashlib.sha256(content).hexdigest(),
                        "rules.md": hashlib.sha256(rules_content).hexdigest(),
                    },
                }
            ),
            encoding="utf-8",
        )
        return materialized, entrypoint

    @staticmethod
    def _write_request_with_snapshot(root: Path, request: Path) -> Path:
        source = root / "source"
        source.mkdir()
        source_file = source / "Example.cs"
        source_content = b"class Example {}\n"
        source_file.write_bytes(source_content)
        head_sha = "b" * 40
        manifest_path = source / SOURCE_SNAPSHOT_MANIFEST
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "repository": "example/one",
                    "source_commit": head_sha,
                    "source_hashes": {"Example.cs": hashlib.sha256(source_content).hexdigest()},
                    "excluded_paths": {},
                }
            ),
            encoding="utf-8",
        )
        diff_path = root / "diff.patch"
        diff_path.write_text("diff --git a/Example.cs b/Example.cs\n", encoding="utf-8")
        request.write_text(
            json.dumps(
                {
                    "protocol_version": 1,
                    "mode": "initial",
                    "repository": "example/one",
                    "pull_number": 12,
                    "pull_request": {
                        "title": "Fixture",
                        "url": "https://github.com/example/one/pull/12",
                        "base_ref": "main",
                        "base_sha": "a" * 40,
                        "head_sha": head_sha,
                    },
                    "diff_path": str(diff_path),
                    "source_snapshot": {
                        "root": str(source),
                        "manifest_path": str(manifest_path),
                        "source_commit": head_sha,
                    },
                    "prior_findings": [],
                    "github_comments": [],
                    "coverage": {"unavailable_sources": []},
                }
            ),
            encoding="utf-8",
        )
        return source

    def test_copilot_command_is_noninteractive_and_least_privilege(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            command = copilot_command(
                "copilot",
                version=(1, 0, 103),
                run_directory=root,
                materialized_root=root,
                result_path=root / "result.json",
            )
            self.assertIn("--no-custom-instructions", command)
            self.assertIn("--no-ask-user", command)
            self.assertIn("--disable-builtin-mcps", command)
            self.assertIn("--disallow-temp-dir", command)
            self.assertIn("--deny-tool=shell", command)
            self.assertIn("--deny-tool=url", command)
            self.assertNotIn("--allow-all", command)
            available_tools = command.index("--available-tools")
            self.assertEqual(
                ["view", "grep", "glob", "edit", "create"], command[available_tools + 1 : available_tools + 6]
            )
            self.assertIn(f"--allow-tool=write({(root / 'result.json').as_posix()})", command)

    def test_copilot_rejects_result_outside_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            run_directory = root / "run"
            run_directory.mkdir()
            with self.assertRaisesRegex(RuntimeContractError, "result path must be inside the run directory"):
                copilot_command(
                    "copilot",
                    version=(1, 0, 103),
                    run_directory=run_directory,
                    materialized_root=root,
                    result_path=root / "result.json",
                )

    def test_copilot_rejects_diff_outside_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            run_directory = root / "run"
            run_directory.mkdir()
            materialized, _ = self._materialized_reviewer(root)
            request = run_directory / "request.json"
            self._write_request_with_snapshot(run_directory, request)
            external_diff = root / "external.diff"
            external_diff.write_text("diff --git a/a b/a\n", encoding="utf-8")
            payload = json.loads(request.read_text(encoding="utf-8"))
            payload["diff_path"] = str(external_diff)
            request.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeContractError, "diff must be a file inside"):
                run_copilot(
                    run_directory=run_directory,
                    materialized_root=materialized,
                    request_path=request,
                    result_path=run_directory / "result.json",
                    promote=self._promote_fixture_result,
                    diagnostic_path=run_directory / "diagnostic.jsonl",
                    isolation_root=run_directory / "isolation",
                    executable="copilot",
                )

    if sys.platform == "win32":

        def test_copilot_version_parser_and_winget_fallback(self) -> None:
            self.assertEqual((1, 0, 88), parse_copilot_version("GitHub Copilot CLI 1.0.88"))
            with tempfile.TemporaryDirectory() as temporary:
                local_app_data = Path(temporary)
                winget = local_app_data / "Microsoft" / "WinGet" / "Links" / "copilot.exe"
                winget.parent.mkdir(parents=True)
                winget.write_bytes(b"fixture")
                with mock.patch.dict(
                    os.environ, {"LOCALAPPDATA": str(local_app_data), "PATH": str(winget.parent.parent)}
                ):
                    self.assertEqual(str(winget), find_copilot())
                on_path = local_app_data / "on path" / "copilot.exe"
                on_path.parent.mkdir()
                on_path.write_bytes(b"fixture")
                environment = {"LOCALAPPDATA": str(local_app_data), "PATH": str(on_path.parent), "PATHEXT": ".EXE"}
                with mock.patch.dict(os.environ, environment):
                    self.assertEqual(
                        str(on_path.with_suffix(".EXE")), find_copilot(), "the program on PATH comes first"
                    )

    else:

        def test_copilot_version_parser_and_path_lookup(self) -> None:
            self.assertEqual((1, 0, 88), parse_copilot_version("GitHub Copilot CLI 1.0.88"))
            with tempfile.TemporaryDirectory() as temporary:
                local_app_data = Path(temporary)
                winget = local_app_data / "Microsoft" / "WinGet" / "Links" / "copilot"
                winget.parent.mkdir(parents=True)
                winget.write_bytes(b"#!/bin/sh\n")
                winget.chmod(0o755)
                with (
                    mock.patch.dict(
                        os.environ, {"LOCALAPPDATA": str(local_app_data), "PATH": str(winget.parent.parent)}
                    ),
                    self.assertRaisesRegex(RuntimeContractError, "^GitHub Copilot CLI is not available$"),
                ):
                    find_copilot()  # no link folder is searched off Windows
                on_path = local_app_data / "on path" / "copilot"
                on_path.parent.mkdir()
                on_path.write_bytes(b"#!/bin/sh\n")
                on_path.chmod(0o755)
                with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data), "PATH": str(on_path.parent)}):
                    self.assertEqual(str(on_path), find_copilot(), "the program on PATH is found")

    def test_copilot_jsonl_is_diagnostic_not_the_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            materialized, entrypoint = self._materialized_reviewer(root)
            request = root / "request.json"
            self._write_request_with_snapshot(root, request)
            result_path = root / "result.json"
            staging = root / "copilot-result-1.json"
            diagnostic = root / "diagnostic.jsonl"
            isolation = root / "isolation"
            invocations: list[tuple[list[str], Path, dict[str, str], str]] = []

            def runner(
                arguments: Sequence[str], cwd: Path, environment: Mapping[str, str], stdin: str
            ) -> ProcessResult:
                invocations.append((list(arguments), cwd, dict(environment), stdin))
                if "--version" in arguments:
                    return ProcessResult(0, "GitHub Copilot CLI 1.2.3\n", "")
                self.assertFalse(result_path.exists())
                staging.write_text('{"protocol_version": 1}', encoding="utf-8")
                return ProcessResult(0, '{"type":"assistant.message","text":"done"}\n', "")

            run_copilot(
                run_directory=root,
                materialized_root=materialized,
                request_path=request,
                result_path=result_path,
                staging_path=staging,
                promote=self._promote_fixture_result,
                diagnostic_path=diagnostic,
                isolation_root=isolation,
                runner=runner,
                executable="copilot",
                base_environment={"GH_TOKEN": "test-token"},
            )
            self.assertEqual(2, len(invocations))
            self.assertEqual((["copilot", "--version"], ""), (invocations[0][0], invocations[0][3]))
            self.assertIn("assistant.message", diagnostic.read_text(encoding="utf-8"))
            self.assertEqual({"protocol_version": 1}, json.loads(result_path.read_text()))
            self.assertFalse(staging.exists(), "the staging file is renamed into place, never copied")
            command, cwd, environment, prompt = invocations[-1]
            # Copilot may write only its staging file; the result appears whole, by rename, or not at all.
            self.assertIn(f"--allow-tool=write({staging.as_posix()})", command)
            self.assertNotIn(f"--allow-tool=write({result_path.as_posix()})", command)
            self.assertIn(str(staging), prompt)
            self.assertEqual(isolation / "workspace", cwd)
            self.assertEqual(str(isolation / "home"), environment["HOME"])
            self.assertEqual(str(isolation / "home"), environment["USERPROFILE"])
            self.assertEqual(str(isolation / "copilot-home"), environment["COPILOT_HOME"])
            self.assertEqual("false", environment["COPILOT_AUTO_UPDATE"], "the checked version is the one that runs")
            self.assertEqual("test-token", environment["GH_TOKEN"])
            self.assertIn(f"REVIEWER_ROOT={materialized.resolve()}", prompt)
            self.assertIn(f"Follow REVIEWER_ROOT/{entrypoint.relative_to(materialized).as_posix()}", prompt)
            self.assertEqual([], [argument for argument in command if "REVIEWER_ROOT" in argument])

    def test_copilot_result_is_promoted_only_when_valid_and_allowed(self) -> None:
        for case in ("partial", "refused"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                materialized, _ = self._materialized_reviewer(root)
                request = root / "request.json"
                self._write_request_with_snapshot(root, request)
                result_path = root / "result.json"
                staging = root / "copilot-result-1.json"
                promoted: list[tuple[Path, Path]] = []

                def runner(
                    arguments: Sequence[str],
                    cwd: Path,
                    environment: Mapping[str, str],
                    stdin: str,
                    *,
                    case: str = case,
                    staging: Path = staging,
                ) -> ProcessResult:
                    del cwd, environment, stdin
                    if "--version" in arguments:
                        return ProcessResult(0, "GitHub Copilot CLI 1.2.3\n", "")
                    content = '{"protocol_version": 1, "fin' if case == "partial" else '{"protocol_version": 1}'
                    staging.write_text(content, encoding="utf-8")
                    return ProcessResult(0, "", "")

                def promote(source: Path, target: Path, *, promoted: list[tuple[Path, Path]] = promoted) -> bool:
                    promoted.append((source, target))
                    return False

                error, reason = (
                    (RuntimeContractError, "not valid JSON") if case == "partial" else (HostSuperseded, "set aside")
                )
                with self.assertRaisesRegex(error, reason):
                    run_copilot(
                        run_directory=root,
                        materialized_root=materialized,
                        request_path=request,
                        result_path=result_path,
                        staging_path=staging,
                        promote=promote,
                        diagnostic_path=root / "diagnostic.jsonl",
                        isolation_root=root / "isolation",
                        runner=runner,
                        executable="copilot",
                    )
                self.assertFalse(result_path.exists())
                self.assertTrue(staging.is_file(), "a result never promoted stays in its staging file")
                self.assertEqual([] if case == "partial" else [(staging, result_path)], promoted)

    def test_copilot_rejects_modified_or_extra_reviewer_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            materialized, _ = self._materialized_reviewer(root)
            request = root / "request.json"
            self._write_request_with_snapshot(root, request)
            (materialized / "rules.md").write_text("# Modified rules\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeContractError, "hash does not match"):
                run_copilot(
                    run_directory=root,
                    materialized_root=materialized,
                    request_path=request,
                    result_path=root / "result.json",
                    promote=self._promote_fixture_result,
                    diagnostic_path=root / "diagnostic.jsonl",
                    isolation_root=root / "isolation",
                    executable="copilot",
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            materialized, _ = self._materialized_reviewer(root)
            request = root / "request.json"
            self._write_request_with_snapshot(root, request)
            (materialized / "extra.md").write_text("# Extra\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeContractError, "file set"):
                run_copilot(
                    run_directory=root,
                    materialized_root=materialized,
                    request_path=request,
                    result_path=root / "result.json",
                    promote=self._promote_fixture_result,
                    diagnostic_path=root / "diagnostic.jsonl",
                    isolation_root=root / "isolation",
                    executable="copilot",
                )

    def test_copilot_rejects_old_version_before_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            materialized, _ = self._materialized_reviewer(root)
            request = root / "request.json"
            self._write_request_with_snapshot(root, request)

            def runner(
                arguments: Sequence[str], cwd: Path, environment: Mapping[str, str], stdin: str
            ) -> ProcessResult:
                del cwd, environment, stdin
                self.assertIn("--version", arguments)
                return ProcessResult(0, "GitHub Copilot CLI 1.0.87\n", "")

            with self.assertRaisesRegex(RuntimeContractError, "1.0.88 or newer"):
                run_copilot(
                    run_directory=root,
                    materialized_root=materialized,
                    request_path=request,
                    result_path=root / "result.json",
                    promote=self._promote_fixture_result,
                    diagnostic_path=root / "diagnostic.jsonl",
                    isolation_root=root / "isolation",
                    runner=runner,
                    executable="copilot",
                )

    def test_copilot_timeout_is_a_contract_error_with_its_partial_output(self) -> None:
        for timed_out, partial in (("review", b'{"type":"assistant.message"}\n'), ("version", None)):
            with self.subTest(timed_out=timed_out), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                materialized, _ = self._materialized_reviewer(root)
                request = root / "request.json"
                self._write_request_with_snapshot(root, request)
                diagnostic = root / "diagnostic.jsonl"

                def runner(
                    arguments: Sequence[str],
                    cwd: Path,
                    environment: Mapping[str, str],
                    stdin: str,
                    *,
                    timed_out: str = timed_out,
                    partial: bytes | None = partial,
                ) -> ProcessResult:
                    del cwd, environment, stdin
                    if "--version" in arguments and timed_out == "review":
                        return ProcessResult(0, "GitHub Copilot CLI 1.2.3\n", "")
                    raise subprocess.TimeoutExpired(arguments, 1800, output=partial, stderr="still working")

                with self.assertRaisesRegex(
                    RuntimeContractError,
                    f"^GitHub Copilot CLI timed out after 1800s; see {re.escape(str(diagnostic))}$",
                ):
                    run_copilot(
                        run_directory=root,
                        materialized_root=materialized,
                        request_path=request,
                        result_path=root / "result.json",
                        promote=self._promote_fixture_result,
                        diagnostic_path=diagnostic,
                        isolation_root=root / "isolation",
                        runner=runner,
                        executable="copilot",
                    )
                text = diagnostic.read_text(encoding="utf-8")
                if partial:
                    self.assertIn("assistant.message", text)
                self.assertIn("STDERR:\nstill working", text)
                self.assertFalse((root / "result.json").exists())

    def test_copilot_rejects_entrypoint_outside_materialized_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            materialized, _ = self._materialized_reviewer(root)
            metadata_path = materialized / "materialization.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["entrypoint"] = "../untrusted.md"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            (root / "untrusted.md").write_text("# Untrusted\n", encoding="utf-8")
            request = root / "request.json"
            self._write_request_with_snapshot(root, request)

            with self.assertRaisesRegex(RuntimeContractError, "entrypoint is unsafe"):
                run_copilot(
                    run_directory=root,
                    materialized_root=materialized,
                    request_path=request,
                    result_path=root / "result.json",
                    promote=self._promote_fixture_result,
                    diagnostic_path=root / "diagnostic.jsonl",
                    isolation_root=root / "isolation",
                    executable="copilot",
                )

    def test_copilot_rejects_modified_entrypoint_and_reused_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            materialized, entrypoint = self._materialized_reviewer(root)
            request = root / "request.json"
            self._write_request_with_snapshot(root, request)
            entrypoint.write_text("# Modified reviewer\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeContractError, "hash does not match"):
                run_copilot(
                    run_directory=root,
                    materialized_root=materialized,
                    request_path=request,
                    result_path=root / "result.json",
                    promote=self._promote_fixture_result,
                    diagnostic_path=root / "diagnostic.jsonl",
                    isolation_root=root / "isolation",
                    executable="copilot",
                )

            content = b"# Trusted reviewer\n"
            entrypoint.write_bytes(content)
            isolation = root / "isolation"
            isolation.mkdir()
            (isolation / "ambient-config.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeContractError, "must be empty"):
                run_copilot(
                    run_directory=root,
                    materialized_root=materialized,
                    request_path=request,
                    result_path=root / "result.json",
                    promote=self._promote_fixture_result,
                    diagnostic_path=root / "diagnostic.jsonl",
                    isolation_root=isolation,
                    executable="copilot",
                )
