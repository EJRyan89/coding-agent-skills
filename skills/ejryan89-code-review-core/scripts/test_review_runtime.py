"""The review runtime contract: requests, snapshots, a prepare's git runner, and reviewer files read from a commit."""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import bounded_process
import github_client
import review_runtime
from executable_locator import located_command
from git_client import GitError, GitResult, GitStream, subprocess_runner
from review_config import is_unreviewed_suffix, verdict_policy
from review_records import calculate_verdict
from review_runtime import (
    SOURCE_SNAPSHOT_MANIFEST,
    PrepareGitRunner,
    RuntimeContractError,
    RuntimeContractFieldError,
    build_adapter_request,
    choose_dispatch,
    load_manifest_from_commit,
    materialize_reviewer,
    materialize_source_snapshot,
    negotiate_capabilities,
    resolve_reviewer_commit,
    resolve_runtime,
    validate_adapter_manifest,
    verify_checkout_remote,
    verify_source_snapshot,
)
from test_review_records import field_pattern


class RuntimeContractTests(unittest.TestCase):
    @staticmethod
    def _manifest() -> dict[str, Any]:
        return {
            "schema_version": 1,
            "id": "fixture-review",
            "protocol_version": 1,
            "supports": ["initial", "re-review"],
            "required_capabilities": ["agent-delegation", "read-diff", "write-result"],
            "entrypoint": ".claude/skills/fixture-review/SKILL.md",
            "resources": [".claude/skills/fixture-review/references/rules.md"],
            "agent_profiles": [".claude/agents/fixture-review.md"],
        }

    @staticmethod
    def _git(path: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(path), *arguments], capture_output=True, text=True, encoding="utf-8", check=False
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def _repository(self, root: Path) -> tuple[Path, str, str]:
        checkout = root / "checkout"
        checkout.mkdir()
        self._git(checkout, "init", "-b", "main")
        self._git(checkout, "remote", "add", "origin", "https://github.com/example/one.git")
        manifest_path = checkout / ".review" / "adapter.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text(json.dumps(self._manifest()), encoding="utf-8")
        for relative, content in {
            ".claude/skills/fixture-review/SKILL.md": "# Trusted fixture\n",
            ".claude/skills/fixture-review/references/rules.md": "Rules\n",
            ".claude/agents/fixture-review.md": "Agent\n",
        }.items():
            target = checkout / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        self._git(checkout, "add", ".")
        self._git(
            checkout, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "trusted"
        )
        trusted = self._git(checkout, "rev-parse", "HEAD")
        (checkout / "head.txt").write_text("untrusted change", encoding="utf-8")
        self._git(checkout, "add", "head.txt")
        self._git(
            checkout, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "head"
        )
        head = self._git(checkout, "rev-parse", "HEAD")
        return checkout, trusted, head

    def test_manifest_with_unhashable_supports_is_rejected_not_a_crash(self) -> None:
        manifest = self._manifest()
        manifest["supports"] = [["initial"]]
        with self.assertRaisesRegex(
            RuntimeContractFieldError,
            field_pattern("entrypoint-manifest.supports[0]", 'one of "initial", "re-review"', "an array"),
        ):
            validate_adapter_manifest(manifest)

    def test_manifest_rejects_unsafe_or_duplicate_files(self) -> None:
        manifest = self._manifest()
        manifest["resources"] = ["../outside.md"]
        with self.assertRaisesRegex(RuntimeContractError, "unsafe"):
            validate_adapter_manifest(manifest)
        manifest = self._manifest()
        manifest["resources"] = [manifest["entrypoint"]]
        with self.assertRaisesRegex(RuntimeContractError, "more than once"):
            validate_adapter_manifest(manifest)

    def test_capability_negotiation_fails_closed(self) -> None:
        negotiate_capabilities("claude-code", ["agent-delegation"])
        with self.assertRaisesRegex(RuntimeContractError, "lacks"):
            negotiate_capabilities("copilot-cli", ["agent-delegation"])
        with self.assertRaisesRegex(RuntimeContractError, "Unknown runtime"):
            negotiate_capabilities("unknown", [])

    def test_an_inline_review_offers_only_reading_the_diff_and_writing_the_result(self) -> None:
        # Literal capability names: what an inline reviewer can do is part of the manifest contract.
        self.assertEqual(
            {"read-diff", "write-result"}, negotiate_capabilities("claude-code", ["read-diff"], dispatch="inline")
        )
        self.assertEqual(
            {"read-diff", "write-result"}, negotiate_capabilities("copilot-cli", ["write-result"], dispatch="inline")
        )
        cases = (
            ("claude-code", ["agent-delegation"], "Inline review lacks required capabilities: agent-delegation"),
            ("codex", ["agent-delegation", "read-diff"], "Inline review lacks required capabilities: agent-delegation"),
            # The runtime's own lack is named first, as it is for every other dispatch.
            ("copilot-cli", ["agent-delegation"], "Runtime copilot-cli lacks required capabilities: agent-delegation"),
            ("copilot-cli", ["isolated-added-root"], "Inline review lacks required capabilities: isolated-added-root"),
        )
        for runtime, required, message in cases:
            with self.subTest(runtime=runtime, required=required):
                with self.assertRaises(RuntimeContractError) as caught:
                    negotiate_capabilities(runtime, required, dispatch="inline")
                self.assertEqual(message, str(caught.exception))

    def test_dispatch_is_inline_when_asked_or_when_the_runtime_cannot_delegate(self) -> None:
        cases = (
            ("claude-code", "generic", False, "subagents"),
            ("claude-code", "specialists", False, "subagents"),
            ("claude-code", "entrypoint", False, "subagents"),
            ("codex", "specialists", False, "subagents"),
            ("copilot-cli", "entrypoint", False, "copilot-host"),
            ("copilot-cli", "generic", False, "inline"),
            ("copilot-cli", "specialists", False, "inline"),
            ("claude-code", "generic", True, "inline"),
            ("codex", "entrypoint", True, "inline"),
            ("copilot-cli", "entrypoint", True, "inline"),
        )
        for runtime, kind, inline, dispatch in cases:
            with self.subTest(runtime=runtime, kind=kind, inline=inline):
                self.assertEqual(dispatch, choose_dispatch(runtime, kind, inline=inline))

    def test_runtime_auto_detection_uses_priority_and_the_shared_locator(self) -> None:
        with mock.patch(
            "review_runtime.find_executable",
            side_effect=lambda command: "C:/tools/codex.exe" if command == "codex" else None,
        ) as located:
            self.assertEqual("codex", resolve_runtime("auto"))
        self.assertEqual([mock.call("claude"), mock.call("codex")], located.call_args_list)
        with (
            mock.patch("review_runtime.find_executable", return_value=None),
            self.assertRaisesRegex(RuntimeContractError, "No supported"),
        ):
            resolve_runtime("auto")

    if sys.platform == "win32":

        def test_runtime_auto_detection_finds_a_cli_only_in_wingets_links_folder(self) -> None:
            # A sanitized environment: PATH is one empty folder and the local application data folder a fresh one, so
            # only the fixture's programs answer, as `deploy.py check` finds them.
            with tempfile.TemporaryDirectory(prefix="runtime locator ") as temporary:
                root = Path(temporary)
                on_path = root / "on path"
                links = root / "local app data" / "Microsoft" / "WinGet" / "Links"
                on_path.mkdir()
                links.mkdir(parents=True)
                environment = {
                    "PATH": str(on_path),
                    "PATHEXT": ".COM;.EXE",
                    "LOCALAPPDATA": str(root / "local app data"),
                }
                with mock.patch.dict(os.environ, environment):
                    self.assertRaisesRegex(RuntimeContractError, "No supported", resolve_runtime, "auto")
                    for command, runtime in (("copilot", "copilot-cli"), ("codex", "codex"), ("claude", "claude-code")):
                        with self.subTest(command=command):
                            (links / f"{command}.exe").write_bytes(b"fixture")
                            self.assertEqual(runtime, resolve_runtime("auto"))
                    for program in links.iterdir():
                        program.unlink()
                    (on_path / "codex.exe").write_bytes(b"fixture")
                    self.assertEqual("codex", resolve_runtime("auto"), "a CLI on PATH is found there")

    else:

        def test_runtime_auto_detection_finds_a_cli_on_path_and_none_in_a_links_folder(self) -> None:
            # Off Windows no package manager's link folder is searched, so a CLI only in a WinGet-shaped folder that the
            # local application data variable names is not found, and one on PATH is.
            with tempfile.TemporaryDirectory(prefix="runtime locator ") as temporary:
                root = Path(temporary)
                on_path = root / "on path"
                links = root / "local app data" / "Microsoft" / "WinGet" / "Links"
                on_path.mkdir()
                links.mkdir(parents=True)
                for command in ("copilot", "codex", "claude"):
                    (links / command).write_bytes(b"#!/bin/sh\n")
                    (links / command).chmod(0o755)
                environment = {"PATH": str(on_path), "LOCALAPPDATA": str(root / "local app data")}
                with mock.patch.dict(os.environ, environment):
                    self.assertRaisesRegex(RuntimeContractError, "No supported", resolve_runtime, "auto")
                    (on_path / "codex").write_bytes(b"#!/bin/sh\n")
                    (on_path / "codex").chmod(0o755)
                    self.assertEqual("codex", resolve_runtime("auto"), "a CLI on PATH is found there")

    def test_runtime_auto_prefers_the_stated_host_over_path(self) -> None:
        # A Codex session on a machine that also has claude installed must not resolve to claude-code.
        with mock.patch("review_runtime.find_executable", return_value="C:/tools/claude.exe") as which:
            self.assertEqual("codex", resolve_runtime("auto", "codex"))
            self.assertEqual("copilot-cli", resolve_runtime("auto", "copilot-cli"))
            which.assert_not_called()
            self.assertEqual("claude-code", resolve_runtime("auto", None), "no host keeps the PATH search")
        with self.assertRaisesRegex(RuntimeContractError, "Unknown runtime host"):
            resolve_runtime("auto", "cursor")

    def test_configured_runtime_overrides_the_stated_host(self) -> None:
        with mock.patch("review_runtime.find_executable", return_value=None):
            self.assertEqual("copilot-cli", resolve_runtime("copilot-cli", "claude-code"))
            self.assertEqual("codex", resolve_runtime("codex", None))

    def test_git_symlink_entries_are_rejected_before_materialization(self) -> None:
        manifest = self._manifest()

        def runner(arguments: Sequence[str], timeout: float) -> GitResult:
            if "ls-tree" in arguments:
                return GitResult(0, f"120000 blob {'a' * 40}\t{manifest['entrypoint']}\n", "")
            raise AssertionError(f"Unexpected command: {arguments}")

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "materialized"
            with self.assertRaisesRegex(RuntimeContractError, "not a regular file"):
                materialize_reviewer(Path(temporary) / "checkout", "b" * 40, manifest, destination, runner=runner)
            self.assertFalse(destination.exists())

    def test_reviewer_is_loaded_from_trusted_commit_not_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout, trusted, head = self._repository(root)
            verify_checkout_remote(checkout, "example/one")
            resolved = resolve_reviewer_commit(checkout, trusted, head_sha=head)
            self.assertEqual(trusted, resolved)
            manifest = load_manifest_from_commit(checkout, trusted, ".review/adapter.json")
            destination = root / "materialized"
            hashes = materialize_reviewer(checkout, trusted, manifest, destination)
            self.assertEqual(3, len(hashes))
            self.assertEqual("# Trusted fixture\n", (destination / manifest["entrypoint"]).read_text(encoding="utf-8"))
            self.assertEqual(
                trusted, json.loads((destination / "materialization.json").read_text(encoding="utf-8"))["source_commit"]
            )
            self.assertEqual(
                manifest["entrypoint"],
                json.loads((destination / "materialization.json").read_text(encoding="utf-8"))["entrypoint"],
            )

    def test_trusted_files_are_materialized_as_their_exact_committed_bytes(self) -> None:
        crlf, latin1 = b"line one\r\nline two\r\n", b"caf\xe9\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout, trusted, _ = self._repository(root)
            manifest = self._manifest()
            for relative, content in ((manifest["entrypoint"], crlf), (manifest["resources"][0], latin1)):
                (checkout / relative).write_bytes(content)
            # Not autocrlf: the blob must hold the CRLF itself, whatever the machine's Git configuration says.
            self._git(
                checkout, "-c", "core.autocrlf=false", "add", "--", manifest["entrypoint"], manifest["resources"][0]
            )
            self._git(
                checkout, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "bytes"
            )
            commit = self._git(checkout, "rev-parse", "HEAD")
            self.assertEqual(
                crlf,
                review_runtime._read_git_file(checkout, commit, manifest["entrypoint"], subprocess_runner),
            )
            self.assertEqual(
                latin1,
                review_runtime._read_git_file(checkout, commit, manifest["resources"][0], subprocess_runner),
            )
            destination = root / "materialized"
            hashes = materialize_reviewer(checkout, commit, manifest, destination)
            self.assertEqual(crlf, (destination / manifest["entrypoint"]).read_bytes())
            self.assertEqual(latin1, (destination / manifest["resources"][0]).read_bytes())
            expected = {
                manifest["entrypoint"]: hashlib.sha256(b"line one\r\nline two\r\n").hexdigest(),
                manifest["resources"][0]: hashlib.sha256(b"caf\xe9\n").hexdigest(),
                manifest["agent_profiles"][0]: hashlib.sha256(b"Agent\n").hexdigest(),
            }
            self.assertEqual(expected, hashes)
            self.assertEqual(
                expected,
                json.loads((destination / "materialization.json").read_text(encoding="utf-8"))["source_hashes"],
            )
            self.assertNotEqual(trusted, commit)

    def test_the_git_runner_decodes_in_the_caller_and_keeps_every_byte(self) -> None:
        # Decoding in subprocess.run happens in a reader thread on Windows, where a bad byte leaves stdout None, and
        # in the caller on POSIX. The shared git runner takes bytes from the bounded layer and decodes them itself,
        # which gives both platforms the same, lossless path.
        finished = bounded_process.Finished(0, b"caf\xe9\r\nok\r\n", b"fatal: caf\xe9\n")
        with mock.patch("git_client.run_bounded", return_value=finished) as run:
            result = subprocess_runner(["git", "show", "HEAD:latin1.md"], 60)
        git = located_command(["git"])[0]
        self.assertEqual(([git, "show", "HEAD:latin1.md"], 60), run.call_args.args)
        self.assertEqual(b"caf\xe9\r\nok\r\n", result.stdout.encode("utf-8", "surrogateescape"))
        self.assertEqual("fatal: caf�\n", result.stderr)
        self.assertEqual(0, result.returncode)

    def test_a_git_failure_never_carries_an_undecodable_byte_into_its_message(self) -> None:
        def runner(arguments: Sequence[str], timeout: float) -> GitResult:
            return GitResult(128, "caf\udce9", "")

        with self.assertRaises(RuntimeContractError) as raised:
            review_runtime._run_git(Path("checkout"), runner, "rev-parse", "HEAD")
        self.assertEqual("caf�", str(raised.exception))

    def test_source_snapshot_uses_exact_head_and_excludes_agent_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout, _, head = self._repository(root)
            (checkout / "CLAUDE.md").write_text("Untrusted instructions\n", encoding="utf-8")
            (checkout / "AGENTS.override.md").write_text("Untrusted override\n", encoding="utf-8")
            (checkout / "src").mkdir()
            (checkout / "src" / "Example.cs").write_text("class Example {}\n", encoding="utf-8")
            (checkout / "src" / "AGENTS.override.md").write_text("Nested override\n", encoding="utf-8")
            (checkout / "src" / "CLAUDE.local.md").write_text("Nested local instructions\n", encoding="utf-8")
            self._git(checkout, "add", ".")
            self._git(
                checkout,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "source",
            )
            head = self._git(checkout, "rev-parse", "HEAD")
            destination = root / "snapshot"
            metadata = materialize_source_snapshot(checkout, "example/one", head, destination)
            self.assertTrue((destination / "src" / "Example.cs").is_file())
            self.assertFalse((destination / "CLAUDE.md").exists())
            self.assertFalse((destination / ".claude").exists())
            self.assertEqual("agent-instruction", metadata["excluded_paths"]["CLAUDE.md"])
            for override in ("AGENTS.override.md", "src/AGENTS.override.md", "src/CLAUDE.local.md"):
                self.assertFalse((destination / override).exists())
                self.assertEqual("agent-instruction", metadata["excluded_paths"][override])
            self.assertIn(".claude/skills/fixture-review/SKILL.md", metadata["excluded_paths"])
            verify_source_snapshot(destination, expected_repository="example/one", expected_commit=head)
            (destination / "src" / "Example.cs").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeContractError, "hash mismatch"):
                verify_source_snapshot(destination, expected_repository="example/one", expected_commit=head)
            # The structural check skips content but still holds the file set to the manifest.
            arguments: dict[str, Any] = {
                "expected_repository": "example/one",
                "expected_commit": head,
                "contents": False,
            }
            verify_source_snapshot(destination, **arguments)
            (destination / "src" / "Extra.cs").write_text("class Extra {}\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeContractError, r"extra=\['src/Extra.cs'\]"):
                verify_source_snapshot(destination, **arguments)
            (destination / "src" / "Extra.cs").unlink()
            (destination / "src" / "Example.cs").unlink()
            for contents in (False, True):
                with self.assertRaisesRegex(RuntimeContractError, "file is missing: src/Example.cs"):
                    verify_source_snapshot(destination, **{**arguments, "contents": contents})

    def test_source_snapshot_writes_more_files_than_its_write_queue_holds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            checkout.mkdir()
            self._git(checkout, "init", "-b", "main")
            self._git(checkout, "remote", "add", "origin", "https://github.com/example/one.git")
            # More files than SNAPSHOT_WRITE_WORKERS * 4 writes in flight, across several directories.
            count = review_runtime.SNAPSHOT_WRITE_WORKERS * 4 * 3 + 1
            for index in range(count):
                target = checkout / f"dir{index % 7}" / f"file{index}.txt"
                target.parent.mkdir(exist_ok=True)
                target.write_text(f"content {index}\n" * (index + 1), encoding="utf-8")
            self._git(checkout, "add", ".")
            self._git(
                checkout,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "many files",
            )
            head = self._git(checkout, "rev-parse", "HEAD")
            destination = root / "snapshot"
            metadata = materialize_source_snapshot(checkout, "example/one", head, destination)
            self.assertEqual(count, len(metadata["source_hashes"]))
            # The full content check re-reads every written file against the hashes computed before writing.
            verify_source_snapshot(destination, expected_repository="example/one", expected_commit=head)

    def _plumbing_repository(self, root: Path, entries: dict[bytes, tuple[str, bytes]]) -> tuple[Path, str]:
        """A checkout whose head commit holds `entries`, each raw path -> (mode, content), written with git plumbing,
        so a path need not be one Windows can create and no attribute, filter, or line-ending setting touches a byte.
        A gitlink's content is the commit it names."""
        checkout = root / "checkout"
        checkout.mkdir()
        self._git(checkout, "init", "-b", "main")
        self._git(checkout, "remote", "add", "origin", "https://github.com/example/one.git")
        records = b""
        for path, (mode, content) in entries.items():
            if mode == "160000":
                object_id = content
            else:
                object_id = self._git_bytes(checkout, "hash-object", "-w", "--stdin", data=content).strip()
            records += mode.encode("ascii") + b" " + object_id + b"\t" + path + b"\0"
        self._git_bytes(checkout, "update-index", "-z", "--index-info", data=records)
        tree = self._git(checkout, "write-tree")
        identity = ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid")
        head = self._git(checkout, *identity, "commit-tree", tree, "-m", "fixture")
        self._git(checkout, "update-ref", "refs/heads/main", head)
        return checkout, head

    @staticmethod
    def _git_bytes(path: Path, *arguments: str, data: bytes = b"") -> bytes:
        result = subprocess.run(["git", "-C", str(path), *arguments], input=data, capture_output=True, check=False)
        if result.returncode != 0:
            raise AssertionError(result.stderr.decode("utf-8", "replace"))
        return result.stdout

    @staticmethod
    def _archived(archive: tarfile.TarFile, name: str) -> bytes:
        member = archive.extractfile(name)
        if member is None:
            raise AssertionError(f"{name} is not a file in the archive")
        return member.read()

    def test_source_snapshot_holds_the_exact_blobs_whatever_attributes_and_configuration_say(self) -> None:
        files = {
            b".gitattributes": (
                b"hidden.py export-ignore\nstamp.py export-subst\nupper.txt filter=upper\n*.txt text eol=crlf\n"
            ),
            b"hidden.py": b"print('hidden')\n",
            b"stamp.py": b"VERSION = '$Format:%H$'\n",
            b"upper.txt": b"lower case\n",
            b"lines.txt": b"one\ntwo\n",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            checkout, head = self._plumbing_repository(
                root, {path: ("100644", content) for path, content in files.items()}
            )
            self._git(checkout, "config", "core.autocrlf", "true")
            self._git(checkout, "config", "filter.upper.smudge", "tr a-z A-Z")
            # `git archive`, which the snapshot was once extracted from, drops or rewrites every file but one here.
            with tarfile.open(fileobj=io.BytesIO(self._git_bytes(checkout, "archive", "--format=tar", head))) as tar:
                self.assertNotIn("hidden.py", tar.getnames())
                self.assertEqual(f"VERSION = '{head}'\r\n".encode("ascii"), self._archived(tar, "stamp.py"))
                self.assertEqual(b"LOWER CASE\r\n", self._archived(tar, "upper.txt"))
                self.assertEqual(b"one\r\ntwo\r\n", self._archived(tar, "lines.txt"))
            snapshot = root / "snapshot"
            metadata = materialize_source_snapshot(checkout, "example/one", head, snapshot, changed_paths=["hidden.py"])
            for path, content in files.items():
                name = path.decode("ascii")
                self.assertEqual(content, (snapshot / name).read_bytes(), name)
                self.assertEqual(hashlib.sha256(content).hexdigest(), metadata["source_hashes"][name], name)
            self.assertEqual({}, metadata["excluded_paths"])
            verify_source_snapshot(snapshot, expected_repository="example/one", expected_commit=head)
            diff = root / "diff.patch"
            diff.write_text(
                "diff --git a/hidden.py b/hidden.py\nnew file mode 100644\nindex 0000000..1111111\n"
                "--- /dev/null\n+++ b/hidden.py\n@@ -0,0 +1 @@\n+print('hidden')\n",
                encoding="utf-8",
            )
            self.assertEqual(
                [], review_runtime.review_coverage(diff, metadata)["unavailable_sources"], "reviewers have its source"
            )

    def test_an_undecodable_name_is_an_unsafe_path_and_a_coverage_gap_only_where_changed(self) -> None:
        # Two bytes that open a UTF-8 sequence and never finish it: one U+FFFD each, as diff.patch shows them.
        name = "src/bad\ufffd\ufffd.py"
        self.assertEqual(
            name, github_client.replace_undecodable(b"src/bad\xe2\x82.py".decode("utf-8", "surrogateescape"))[0]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            checkout, head = self._plumbing_repository(
                root, {b"src/ok.py": ("100644", b"ok\n"), b"src/bad\xe2\x82.py": ("100644", b"bad\n")}
            )
            snapshot = root / "snapshot"
            metadata = materialize_source_snapshot(checkout, "example/one", head, snapshot, changed_paths=[name])
            self.assertEqual(["src/ok.py"], list(metadata["source_hashes"]))
            self.assertEqual({name: "unsafe-path"}, metadata["excluded_paths"])
            self.assertEqual(["ok.py"], [path.name for path in (snapshot / "src").iterdir()])
            verify_source_snapshot(snapshot, expected_repository="example/one", expected_commit=head)
            # validate-reviewer measures with the same rules, so it reports the exclusion rather than failing.
            measured = review_runtime.measure_source_snapshot(
                checkout, head, destination=snapshot, changed_paths=[name]
            )
            self.assertEqual((1, 3, {"unsafe-path": 1}), (measured.files, measured.bytes, measured.excluded))
            diff = root / "diff.patch"
            for changed, unavailable, verdict in ((name, [name], "INCOMPLETE"), ("src/ok.py", [], "APPROVED")):
                diff.write_text(
                    f"diff --git a/{changed} b/{changed}\nindex 1111111..2222222 100644\n"
                    f"--- a/{changed}\n+++ b/{changed}\n@@ -1 +1 @@\n-old\n+new\n",
                    encoding="utf-8",
                )
                self.assertEqual(
                    unavailable, review_runtime.review_coverage(diff, metadata)["unavailable_sources"], changed
                )
                self.assertEqual(verdict, calculate_verdict([], verdict_policy({}), unavailable), changed)
            # git quotes the name with octal escapes, which the diff parser decodes with one U+FFFD for the whole
            # truncated sequence, not one per byte; the two forms still match.
            octal = "".join(f"{chr(92)}{byte:o}" for byte in bytes([0xE2, 0x82]))
            quoted = f"src/bad{octal}.py"
            diff.write_text(
                f'diff --git "a/{quoted}" "b/{quoted}"\nindex 1111111..2222222 100644\n'
                f'--- "a/{quoted}"\n+++ "b/{quoted}"\n@@ -1 +1 @@\n-old\n+new\n',
                encoding="utf-8",
            )
            unavailable = review_runtime.review_coverage(diff, metadata)["unavailable_sources"]
            self.assertEqual([f"src/bad{chr(0xFFFD)}.py"], unavailable)
            self.assertEqual("INCOMPLETE", calculate_verdict([], verdict_policy({}), unavailable))

    def test_source_snapshot_excludes_links_and_writes_nothing_for_a_submodule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            checkout, head = self._plumbing_repository(
                root,
                {
                    b"src/A.cs": ("100644", b"class A {}\n"),
                    b"tools/run.sh": ("100755", b"echo run\n"),
                    b"tools/cache": ("120000", b"/home/dev/.cache/tool"),
                    b"vendor/lib": ("160000", b"d" * 40),
                },
            )
            destination = root / "snapshot"
            metadata = materialize_source_snapshot(checkout, "example/one", head, destination)
            self.assertEqual({"tools/cache": "symbolic-link"}, metadata["excluded_paths"])
            self.assertEqual(["src/A.cs", "tools/run.sh"], sorted(metadata["source_hashes"]))
            self.assertEqual(
                sorted([SOURCE_SNAPSHOT_MANIFEST, "src/A.cs", "tools/run.sh"]),
                sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()),
            )
            self.assertFalse((destination / "tools" / "cache").exists(), "nothing is written for a link")
            self.assertFalse((destination / "vendor").exists(), "a submodule has no blob, so nothing is written")
            verify_source_snapshot(destination, expected_repository="example/one", expected_commit=head)
            # Deliberate, like agent-instruction: reviewed from the diff, never a coverage gap.
            diff = root / "diff.patch"
            diff.write_text(
                "diff --git a/tools/cache b/tools/cache\nnew file mode 120000\nindex 0000000..1111111\n"
                "--- /dev/null\n+++ b/tools/cache\n@@ -0,0 +1 @@\n+/home/dev/.cache/tool\n"
                "\\ No newline at end of file\n",
                encoding="utf-8",
            )
            self.assertEqual([], review_runtime.review_coverage(diff, metadata)["unavailable_sources"])
        self.assertEqual(
            {
                "agent-instruction",
                "binary",
                "configured",
                "file-size-limit",
                "unsafe-path",
                "symbolic-link",
                "non-regular",
                "unextractable",
            },
            review_runtime.SNAPSHOT_EXCLUSION_REASONS,
        )
        self.assertEqual(
            {"binary", "configured", "file-size-limit", "unsafe-path", "unextractable"},
            review_runtime.COVERAGE_GAP_REASONS,
        )

    def test_a_changed_binary_of_a_type_the_review_does_not_read_is_unreviewed_and_any_other_binary_a_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            entries = {
                b"src/app.py": ("100644", b"print('app')\n"),
                b"dist/Setup.MSI": ("100644", b"MSI\0installer"),
                b"assets/logo.png": ("100644", b"\x89PNG\r\n\x1a\n\0image"),
                b"data/blob.bin": ("100644", b"BIN\0data"),
                b"docs/notes.zip": ("100644", b"A text placeholder named like an archive.\n"),
                b"assets/old.png": ("100644", b"PNG\0unchanged"),
            }
            checkout, head = self._plumbing_repository(root, entries)
            changed = ["src/app.py", "dist/Setup.MSI", "assets/logo.png", "data/blob.bin", "docs/notes.zip"]
            metadata = materialize_source_snapshot(
                checkout, "example/one", head, root / "snapshot", changed_paths=changed
            )
            binary = ["assets/logo.png", "assets/old.png", "data/blob.bin", "dist/Setup.MSI"]
            self.assertEqual(dict.fromkeys(binary, "binary"), metadata["excluded_paths"])
            self.assertIn("docs/notes.zip", metadata["source_hashes"], "a text file is reviewed whatever its name")
            diff = root / "diff.patch"
            diff.write_text(
                "".join(
                    f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n"
                    + (
                        f"Binary files a/{path} and b/{path} differ\n"
                        if path in binary
                        else f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new\n"
                    )
                    for path in changed
                ),
                encoding="utf-8",
            )
            # The shipped list names the installer and the image, matched ignoring case, and nothing names the
            # unchanged image, which no review is missing.
            coverage = review_runtime.review_coverage(diff, metadata)
            self.assertEqual(
                {
                    "unavailable_sources": ["data/blob.bin"],
                    "unreviewed_files": {"assets/logo.png": ".png", "dist/Setup.MSI": ".msi"},
                },
                coverage,
            )
            self.assertEqual("INCOMPLETE", calculate_verdict([], verdict_policy({}), coverage["unavailable_sources"]))
            # The repository's own suffix makes the last binary unreviewed too, and the review ends on its findings.
            coverage = review_runtime.review_coverage(diff, metadata, [".bin", ".dat"])
            self.assertEqual([], coverage["unavailable_sources"])
            self.assertEqual(
                {"assets/logo.png": ".png", "data/blob.bin": ".bin", "dist/Setup.MSI": ".msi"},
                coverage["unreviewed_files"],
            )
            self.assertEqual("APPROVED", calculate_verdict([], verdict_policy({}), coverage["unavailable_sources"]))
            self.assertEqual([], review_runtime.review_coverage(diff, metadata, [".bin"])["unavailable_sources"])

    def test_a_gap_reason_of_a_name_merged_with_a_binary_one_keeps_it_a_gap(self) -> None:
        # Two undecodable spellings of one name read as one; the gap one of them has is never hidden by the other.
        one, two = "dist/bad�.zip", "dist/bad��.zip"
        with tempfile.TemporaryDirectory() as temporary:
            diff = Path(temporary) / "diff.patch"
            diff.write_text(f"diff --git a/{one} b/{one}\nBinary files a/{one} and b/{one} differ\n", encoding="utf-8")
            for excluded in ({one: "binary", two: "file-size-limit"}, {one: "file-size-limit", two: "binary"}):
                with self.subTest(excluded=excluded):
                    coverage = review_runtime.review_coverage(diff, {"excluded_paths": excluded})
                    self.assertEqual({"unavailable_sources": [one], "unreviewed_files": {}}, coverage)

    def test_the_shipped_unreviewed_suffixes_and_how_a_name_matches_one(self) -> None:
        self.assertEqual(
            {
                *(".7z", ".bz2", ".cab", ".gz", ".jar", ".rar", ".tar", ".tgz", ".xz", ".zip", ".zst"),
                *(".apk", ".appx", ".deb", ".dmg", ".msi", ".msix", ".msp", ".nupkg", ".pkg", ".rpm", ".snupkg"),
                *(".vsix", ".whl"),
                *(".bmp", ".gif", ".icns", ".ico", ".jpeg", ".jpg", ".png", ".psd", ".tif", ".tiff", ".webp"),
                *(".eot", ".otf", ".ttf", ".woff", ".woff2"),
                *(".a", ".class", ".dll", ".dylib", ".exe", ".lib", ".o", ".obj", ".pdb", ".pyc", ".pyd", ".so"),
                ".wasm",
                *(".aac", ".avi", ".flac", ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".ogg", ".wav", ".webm"),
            },
            review_runtime.UNREVIEWED_SUFFIXES,
        )
        self.assertTrue(all(is_unreviewed_suffix(suffix) for suffix in review_runtime.UNREVIEWED_SUFFIXES))
        for path, suffixes, expected in (
            ("dist/Setup.MSI", [".msi"], ".msi"),
            ("dist/setup.msi.txt", [".msi"], None),
            ("pkg/tool.tar.lz", [".lz", ".tar.lz"], ".tar.lz"),
            ("pkg/.zip", [".zip"], None),
            ("zip", [".zip"], None),
            ("src/archive_zip", [".zip"], None),
        ):
            with self.subTest(path=path):
                self.assertEqual(expected, review_runtime.unreviewed_suffix(path, suffixes))

    # The changed-file limit the oversized-file tests set: twice the probe, so a file past it has bytes past the probe.
    OVERSIZED_LIMIT = 2 * review_runtime.BINARY_PROBE_BYTES

    def _oversized_head(self) -> tuple[dict[bytes, tuple[str, bytes]], list[str]]:
        """A head whose files, but one small source file, are over OVERSIZED_LIMIT, and the paths it changes."""
        probe, pad = review_runtime.BINARY_PROBE_BYTES, b"x" * self.OVERSIZED_LIMIT
        entries = {
            b"src/app.py": ("100644", b"print('app')\n"),
            b"dist/Setup.MSI": ("100644", b"MSI\0" + pad),  # a shipped suffix, in another case
            b"models/net.onnx": ("100644", b"PK\0" + pad),  # only the repository's suffix names it
            b"docs/notes.zip": ("100644", b"A text file named like an archive.\n" + pad),
            b"media/late.png": ("100644", b"p" * probe + b"\0" + pad),  # its first NUL just past the probe
            b"data/model.dat": ("100644", b"DAT\0" + pad),  # no suffix names it
            b"docs/manual.pdf": ("100644", b"%PDF-1.7\n\0" + pad),  # a document, whatever the repository lists
            b"assets/old.png": ("100644", b"PNG\0" + pad),  # unchanged
        }
        changed = [path.decode("ascii") for path in entries if path != b"assets/old.png"]
        return entries, changed

    def _oversized_reasons(self, configured: bool) -> dict[str, str]:
        """Why the snapshot of `_oversized_head` leaves each file out, with the repository's suffixes or without."""
        return {
            "dist/Setup.MSI": "binary",
            "models/net.onnx": "binary" if configured else "file-size-limit",
            "docs/notes.zip": "file-size-limit",
            "media/late.png": "file-size-limit",
            "data/model.dat": "file-size-limit",
            "docs/manual.pdf": "file-size-limit",
            "assets/old.png": "file-size-limit",
        }

    @contextlib.contextmanager
    def _oversized_limits(self) -> Iterator[None]:
        """Both per-file limits at OVERSIZED_LIMIT, so every file of `_oversized_head` but its source file is over."""
        with (
            mock.patch.object(review_runtime, "MAX_CHANGED_FILE_BYTES", self.OVERSIZED_LIMIT),
            mock.patch.object(review_runtime, "MAX_SOURCE_FILE_BYTES", self.OVERSIZED_LIMIT),
        ):
            yield

    @staticmethod
    def _changed_diff(root: Path, changed: Sequence[str]) -> Path:
        """A diff changing each of `changed`, every one but the source file as git's binary line."""
        diff = root / "diff.patch"
        diff.write_text(
            "".join(
                f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n"
                + (
                    f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new\n"
                    if path.endswith(".py")
                    else f"Binary files a/{path} and b/{path} differ\n"
                )
                for path in changed
            ),
            encoding="utf-8",
        )
        return diff

    def test_a_changed_listed_file_over_the_limit_is_probed_from_the_checkout_and_only_its_first_bytes_read(
        self,
    ) -> None:
        entries, changed = self._oversized_head()
        probe = review_runtime.BINARY_PROBE_BYTES
        # Each oversized file by its blob id, as git hashes it.
        blobs = {
            review_runtime._git_blob_id("sha1", len(content), [content]): path.decode("ascii")
            for path, (_, content) in entries.items()
            if len(content) > self.OVERSIZED_LIMIT
        }
        probed: dict[str, int] = {}
        asked: list[str] = []

        def prefix_reader(checkout: Path, blob: str, size: int) -> bytes:
            prefix = review_runtime.git_blob_prefix(checkout, blob, size)
            probed[blobs[blob]] = len(prefix)
            return prefix

        def blob_reader(checkout: Path, wanted: Sequence[str]) -> Iterator[bytes]:
            asked.extend(wanted)
            yield from review_runtime.git_blob_reader(checkout, wanted)

        for configured in ((), (".onnx", ".pdf")):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                checkout, head = self._plumbing_repository(root, entries)
                probed.clear()
                asked.clear()
                with self._oversized_limits():
                    metadata = materialize_source_snapshot(
                        checkout,
                        "example/one",
                        head,
                        root / "snapshot",
                        changed_paths=changed,
                        blob_reader=blob_reader,
                        unreviewed_suffixes=configured,
                        prefix_reader=prefix_reader,
                    )
                self.assertEqual(self._oversized_reasons(bool(configured)), metadata["excluded_paths"])
                self.assertEqual(["src/app.py"], list(metadata["source_hashes"]))
                # Only a changed file a listed suffix names is probed, never a document, and only its first bytes;
                # no oversized blob is read whole.
                listed = ["dist/Setup.MSI", "docs/notes.zip", "media/late.png"]
                listed += ["models/net.onnx"] if configured else []
                self.assertEqual(dict.fromkeys(listed, probe), probed)
                self.assertEqual([], [blobs[blob] for blob in asked if blob in blobs])
                coverage = review_runtime.review_coverage(self._changed_diff(root, changed), metadata, configured)
                unreviewed = {"dist/Setup.MSI": ".msi", **({"models/net.onnx": ".onnx"} if configured else {})}
                self.assertEqual(unreviewed, coverage["unreviewed_files"])
                gaps = ["data/model.dat", "docs/manual.pdf", "docs/notes.zip", "media/late.png"]
                self.assertEqual(
                    sorted(gaps + ([] if configured else ["models/net.onnx"])), coverage["unavailable_sources"]
                )

    def test_a_changed_listed_file_over_the_limit_is_probed_from_the_tarball_and_only_its_first_bytes_read(
        self,
    ) -> None:
        entries, changed = self._oversized_head()
        probe = review_runtime.BINARY_PROBE_BYTES
        read: dict[str, int] = {}
        opened = tarfile.TarFile.extractfile

        class Counted:
            """A tarball entry's stream that counts the bytes the snapshot reads from it."""

            def __init__(self, stream: Any, name: str) -> None:
                self.stream, self.name = stream, name

            def read(self, size: int = -1) -> bytes:
                data: bytes = self.stream.read(size)
                read[self.name] = read.get(self.name, 0) + len(data)
                return data

            def __enter__(self) -> Counted:
                return self

            def __exit__(self, *exc: object) -> None:
                self.stream.close()

        def extractfile(archive: tarfile.TarFile, member: tarfile.TarInfo) -> Counted:
            return Counted(opened(archive, member), member.name.split("/", 1)[1])

        def fetcher(repository: str, commit: str, target: Path) -> None:
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                for path, (_, content) in entries.items():
                    info = tarfile.TarInfo(f"example-one-0123456/{path.decode('ascii')}")
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
            target.write_bytes(gzip.compress(buffer.getvalue()))

        for configured in ((), (".onnx", ".pdf")):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                read.clear()
                with self._oversized_limits(), mock.patch.object(tarfile.TarFile, "extractfile", extractfile):
                    metadata = review_runtime.materialize_source_snapshot_from_github(
                        "example/one",
                        "a" * 40,
                        root / "snapshot",
                        fetcher=fetcher,
                        changed_paths=changed,
                        unreviewed_suffixes=configured,
                    )
                self.assertEqual(self._oversized_reasons(bool(configured)), metadata["excluded_paths"])
                listed = ["dist/Setup.MSI", "docs/notes.zip", "media/late.png"]
                listed += ["models/net.onnx"] if configured else []
                # The kept file is read whole; a probed entry only to the probe, and no other oversized one at all.
                self.assertEqual({"src/app.py": 13, **dict.fromkeys(listed, probe)}, read)

    def test_a_blob_prefix_is_read_only_to_the_probe_and_its_size_must_be_the_listed_one(self) -> None:
        probe = review_runtime.BINARY_PROBE_BYTES
        large, small = b"L\0" + b"x" * (3 * probe), b"S\0small"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            checkout, head = self._plumbing_repository(
                root, {b"large.bin": ("100644", large), b"small.bin": ("100644", small)}
            )
            sizes: list[int] = []
            original = GitStream.read

            def counted(stream: GitStream, size: int) -> bytes:
                sizes.append(size)
                return original(stream, size)

            for name, content in (("large.bin", large), ("small.bin", small)):
                with self.subTest(name=name):
                    sizes.clear()
                    blob = self._git(checkout, "rev-parse", f"{head}:{name}")
                    with mock.patch.object(GitStream, "read", counted):
                        prefix = review_runtime.git_blob_prefix(checkout, blob, len(content))
                    self.assertEqual(content[:probe], prefix)
                    self.assertEqual([min(probe, len(content))], sizes, "nothing past the probe is asked for")
            blob = self._git(checkout, "rev-parse", f"{head}:small.bin")
            with self.assertRaisesRegex(RuntimeContractError, "Source blob size changed"):
                review_runtime.git_blob_prefix(checkout, blob, len(small) + 1)
            with self.assertRaisesRegex(RuntimeContractError, "Not a git object id"):
                review_runtime.git_blob_prefix(checkout, "HEAD", len(small))

    def test_source_snapshot_still_refuses_a_link_at_the_reserved_manifest_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            checkout, head = self._plumbing_repository(
                root, {SOURCE_SNAPSHOT_MANIFEST.encode("ascii"): ("120000", b"/etc/passwd")}
            )
            destination = root / "snapshot"
            with self.assertRaisesRegex(RuntimeContractError, "reserved snapshot path"):
                materialize_source_snapshot(checkout, "example/one", head, destination)
            self.assertFalse(destination.exists())

    def test_a_blob_reader_stopped_early_ends_git_and_releases_the_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            # Far more than a pipe buffers, so git is still writing when the reader stops.
            checkout, head = self._plumbing_repository(root, {b"big.txt": ("100644", b"x" * (1024 * 1024))})
            blob = self._git(checkout, "rev-parse", f"{head}:big.txt")
            reader = review_runtime.git_blob_reader(checkout, [blob] * 32)
            self.assertEqual(1024 * 1024, len(next(reader)))
            reader.close()
            # Git for Windows' git.exe is a launcher: ending only it leaves the real git running in the checkout, and
            # Windows refuses to move a folder a running process works in.
            checkout.rename(root / "moved")
            self.assertFalse(checkout.exists())

    def test_source_snapshot_fails_closed_when_the_blob_reader_answers_short_or_wrong(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            checkout, head = self._plumbing_repository(
                root, {b"a.txt": ("100644", b"a\n"), b"b.txt": ("100644", b"b\n")}
            )
            blob = self._git(checkout, "rev-parse", f"{head}:a.txt")
            self.assertEqual([b"a\n", b"a\n"], list(review_runtime.git_blob_reader(checkout, [blob, blob])))
            with self.assertRaisesRegex(RuntimeContractError, "missing"):
                list(review_runtime.git_blob_reader(checkout, ["0" * 40]))

            def short(_checkout: Path, _blobs: Sequence[str]) -> Iterator[bytes]:
                yield b"a\n"

            def wrong(_checkout: Path, blobs: Sequence[str]) -> Iterator[bytes]:
                for _blob in blobs:
                    yield b"rewritten\n"

            for reader, message in ((short, "fewer blobs"), (wrong, "size")):
                destination = root / "snapshot"
                with self.subTest(message=message), self.assertRaisesRegex(RuntimeContractError, message):
                    materialize_source_snapshot(checkout, "example/one", head, destination, blob_reader=reader)
                self.assertFalse(destination.exists())

    def test_source_snapshot_materialization_enforces_file_count_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            checkout, head = self._plumbing_repository(
                root, {b"one.txt": ("100644", b"x"), b"two.txt": ("100644", b"x")}
            )
            destination = root / "snapshot"
            with (
                mock.patch.object(review_runtime, "MAX_SOURCE_SNAPSHOT_FILES", 1),
                self.assertRaisesRegex(RuntimeContractError, "file-count"),
            ):
                materialize_source_snapshot(checkout, "example/one", head, destination)
            self.assertFalse(destination.exists())

    def test_source_snapshot_excludes_binary_and_oversized_files_from_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout, _, _ = self._repository(root)
            (checkout / "assets").mkdir()
            (checkout / "assets" / "image.bin").write_bytes(b"PNG\0" + b"x" * 32)
            (checkout / "assets" / "large.txt").write_bytes(b"y" * 65)
            (checkout / "src.txt").write_bytes(b"z" * 8)
            self._git(checkout, "add", ".")
            self._git(
                checkout,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "assets",
            )
            head = self._git(checkout, "rev-parse", "HEAD")
            destination = root / "snapshot"
            with (
                mock.patch.object(review_runtime, "MAX_SOURCE_FILE_BYTES", 64),
                mock.patch.object(review_runtime, "MAX_SOURCE_SNAPSHOT_BYTES", 64),
            ):
                metadata = materialize_source_snapshot(checkout, "example/one", head, destination)
                verify_source_snapshot(destination, expected_repository="example/one", expected_commit=head)
            self.assertEqual("binary", metadata["excluded_paths"]["assets/image.bin"])
            self.assertEqual("file-size-limit", metadata["excluded_paths"]["assets/large.txt"])
            self.assertFalse((destination / "assets").exists())
            self.assertTrue((destination / "src.txt").is_file())

    def test_adapter_request_requires_verified_source_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout, _, head = self._repository(root)
            snapshot = root / "snapshot"
            materialize_source_snapshot(checkout, "example/one", head, snapshot)
            diff = root / "diff.patch"
            diff.write_text("diff --git a/a b/a\n", encoding="utf-8")
            request = build_adapter_request(
                mode="initial",
                repository="example/one",
                pull_number=12,
                base_ref="main",
                base_sha="a" * 40,
                head_sha=head,
                title="Fixture",
                url="https://github.com/example/one/pull/12",
                diff_path=diff,
                source_snapshot_root=snapshot,
            )
            self.assertEqual(str(snapshot), request["source_snapshot"]["root"])
            self.assertEqual(head, request["source_snapshot"]["source_commit"])
            extra = snapshot / "extra.txt"
            extra.write_text("not declared", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeContractError, "file set mismatch"):
                build_adapter_request(
                    mode="initial",
                    repository="example/one",
                    pull_number=12,
                    base_ref="main",
                    base_sha="a" * 40,
                    head_sha=head,
                    title="Fixture",
                    url="https://github.com/example/one/pull/12",
                    diff_path=diff,
                    source_snapshot_root=snapshot,
                )

    def test_pull_ref_head_and_remote_mismatch_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkout, _, head = self._repository(Path(temporary))
            with self.assertRaisesRegex(RuntimeContractError, "Pull-request refs"):
                resolve_reviewer_commit(checkout, "refs/pull/1/head", head_sha=head)
            with self.assertRaisesRegex(RuntimeContractError, "pull-request head"):
                resolve_reviewer_commit(checkout, "HEAD", head_sha=head)
            with self.assertRaisesRegex(RuntimeContractError, "origin mismatch"):
                verify_checkout_remote(checkout, "different/one")


class AnsweringGit:
    """A git runner that records each command and answers it from `answers`, one result or error per call in order,
    else with success and the command's arguments as its output, so each answer says which call gave it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.answers: dict[tuple[str, ...], list[GitResult | GitError]] = {}

    def __call__(self, command: Sequence[str], timeout: float) -> GitResult:
        key = tuple(command)
        self.calls.append(key)
        answers = self.answers.get(key[3:])
        if answers:
            answer = answers.pop(0)
            if isinstance(answer, GitError):
                raise answer
            return answer
        return GitResult(0, f"{' '.join(key[3:])} #{len(self.calls)}\n", "")


FULL_COMMIT = "1" * 40


FULL_SHA256_COMMIT = "2" * 64


def in_checkout(*arguments: str, checkout: str = "C:/checkout") -> tuple[str, ...]:
    return ("git", "-C", checkout, *arguments)


class CheckoutRemoteTests(unittest.TestCase):
    """The checkout's origin is the configured repository only when the whole URL is a github.com remote's."""

    def verify(self, url: str, repository: str = "example/one") -> None:
        git = AnsweringGit()
        git.answers[("remote", "get-url", "origin")] = [GitResult(0, f"{url}\n", "")]
        verify_checkout_remote(Path("C:/checkout"), repository, git)

    def test_each_github_com_form_of_the_repository_is_accepted(self) -> None:
        for url in (
            "https://github.com/example/one.git",
            "https://github.com/Example/One",
            "git@github.com:example/one.git",
            "git@github.com:example/one",
            "ssh://git@github.com/example/one.git",
        ):
            with self.subTest(url=url):
                self.verify(url)

    def test_a_remote_on_another_host_is_refused_whatever_its_path_names(self) -> None:
        for url in (
            "https://example.invalid/github.com/example/one",
            "https://example.invalid/mirror/github.com:example/one.git",
            "git@example.invalid:github.com/example/one",
            "https://github.com.example.invalid/example/one",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(RuntimeContractError, "not a recognizable GitHub"):
                self.verify(url)
        with self.assertRaisesRegex(RuntimeContractError, "origin mismatch: expected example/two, found example/one"):
            self.verify("https://github.com/example/one.git", "example/two")


class PrepareGitRunnerTests(unittest.TestCase):
    """One prepare's git runner asks an identical query of origin or of a full object id once, until a command that
    is not a read; a ref, an abbreviation, a failed answer, and another prepare's runner are asked again."""

    ORIGIN = ("remote", "get-url", "origin")
    COMMIT = ("rev-parse", "--verify", f"{FULL_COMMIT}^{{commit}}")
    REUSED = (
        ORIGIN,
        COMMIT,
        ("rev-parse", "--verify", f"{FULL_SHA256_COMMIT}^{{commit}}"),
        ("rev-parse", "--verify", FULL_COMMIT),
        ("cat-file", "-e", f"{FULL_COMMIT}^{{commit}}"),
        ("ls-tree", "-z", FULL_COMMIT, "--", "review/rules.md", "review/python.md"),
        ("ls-tree", "-r", "-z", "-l", "--full-tree", FULL_COMMIT),
        ("ls-tree", "-r", "--name-only", "-z", FULL_SHA256_COMMIT),
    )
    # Reads whose answer a ref, an abbreviation, or the network can change, or that no prepare repeats.
    ASKED_EACH_TIME = (
        ("rev-parse", "--verify", "refs/heads/reviewers^{commit}"),
        ("rev-parse", "--verify", "abc1234^{blob}"),
        ("rev-parse", "--verify", f"{FULL_COMMIT[:39]}^{{commit}}"),
        ("rev-parse", "--verify", f"{FULL_COMMIT}~1"),
        ("cat-file", "-e", "main^{commit}"),
        ("cat-file", "-s", FULL_COMMIT),
        ("cat-file", "blob", FULL_COMMIT),
        ("ls-tree", "-z", "HEAD", "--", FULL_COMMIT),
        ("ls-remote", "--symref", "origin", "HEAD"),
        ("remote", "get-url", "upstream"),
    )
    # Any command not known to be a read is taken as one that may write, even one that only reads.
    NOT_READS = (
        in_checkout("fetch", "--no-tags", "--quiet", "origin", "refs/pull/12/head"),
        in_checkout("remote", "set-url", "origin", "https://github.com/other/repo.git"),
        in_checkout("remote", "get-url", "--push", "origin"),
        in_checkout("config", "remote.origin.url", "https://github.com/other/repo.git"),
        in_checkout("update-ref", "refs/heads/reviewers", FULL_COMMIT),
        ("git", "rev-parse", "--verify", f"{FULL_COMMIT}^{{commit}}"),  # in no named checkout
    )

    def test_an_identical_query_is_asked_once(self) -> None:
        for arguments in self.REUSED:
            with self.subTest(arguments=arguments):
                git = AnsweringGit()
                runner = PrepareGitRunner(git)
                first = runner(in_checkout(*arguments), 30)
                self.assertIs(first, runner(in_checkout(*arguments), 60), "the first answer, whatever the time limit")
                self.assertEqual([in_checkout(*arguments)], git.calls)

    def test_another_checkout_or_another_object_is_asked_again(self) -> None:
        git = AnsweringGit()
        runner = PrepareGitRunner(git)
        commands = [
            in_checkout(*self.ORIGIN),
            in_checkout(*self.ORIGIN, checkout="C:/other"),
            in_checkout(*self.COMMIT),
            in_checkout("rev-parse", "--verify", f"{FULL_COMMIT}^{{tree}}"),
            in_checkout("rev-parse", "--verify", f"{'3' * 40}^{{commit}}"),
            in_checkout("ls-tree", "-z", FULL_COMMIT, "--", "review/rules.md"),
            in_checkout("ls-tree", "-z", FULL_COMMIT, "--", "review/python.md"),
        ]
        for command in commands * 2:
            runner(command, 30)
        self.assertEqual(commands, git.calls)

    def test_a_ref_an_abbreviation_and_any_other_read_are_asked_every_time(self) -> None:
        for arguments in self.ASKED_EACH_TIME:
            with self.subTest(arguments=arguments):
                git = AnsweringGit()
                runner = PrepareGitRunner(git)
                runner(in_checkout(*arguments), 30)
                runner(in_checkout(*arguments), 30)
                self.assertEqual([in_checkout(*arguments)] * 2, git.calls)

    def test_a_read_leaves_the_kept_answers(self) -> None:
        git = AnsweringGit()
        runner = PrepareGitRunner(git)
        reads = [in_checkout(*arguments) for arguments in self.ASKED_EACH_TIME]
        for command in (in_checkout(*self.ORIGIN), *reads, in_checkout(*self.ORIGIN)):
            runner(command, 30)
        self.assertEqual([in_checkout(*self.ORIGIN), *reads], git.calls)

    def test_a_failed_answer_is_asked_again(self) -> None:
        git = AnsweringGit()
        present = ("cat-file", "-e", f"{FULL_COMMIT}^{{commit}}")
        git.answers[present] = [GitResult(1, "", "fatal: missing")]
        runner = PrepareGitRunner(git)
        self.assertEqual(
            [1, 0, 0], [runner(in_checkout(*present), 30).returncode for _ in range(3)], "asked twice, then kept"
        )
        self.assertEqual([in_checkout(*present)] * 2, git.calls)

    def test_a_command_that_is_not_a_read_drops_every_kept_answer(self) -> None:
        for write in self.NOT_READS:
            with self.subTest(write=write):
                git = AnsweringGit()
                runner = PrepareGitRunner(git)
                queries = [in_checkout(*self.ORIGIN), in_checkout(*self.COMMIT)]
                for command in (*queries, *queries, write, *queries, *queries):
                    runner(command, 30)
                self.assertEqual([*queries, write, *queries], git.calls)

    def test_a_write_that_fails_or_cannot_run_drops_them_too(self) -> None:
        fetch = ("fetch", "--no-tags", "--quiet", "origin", "refs/heads/main")
        for answer in (GitResult(1, "", "fatal: cannot fetch"), GitError("git timed out", kind="timeout")):
            with self.subTest(answer=answer):
                git = AnsweringGit()
                git.answers[fetch] = [answer]
                runner = PrepareGitRunner(git)
                runner(in_checkout(*self.ORIGIN), 30)
                with contextlib.suppress(GitError):
                    runner(in_checkout(*fetch), 30)
                runner(in_checkout(*self.ORIGIN), 30)
                self.assertEqual([in_checkout(*self.ORIGIN), in_checkout(*fetch), in_checkout(*self.ORIGIN)], git.calls)

    def test_another_runner_keeps_nothing_of_the_first(self) -> None:
        git = AnsweringGit()
        PrepareGitRunner(git)(in_checkout(*self.ORIGIN), 30)
        PrepareGitRunner(git)(in_checkout(*self.ORIGIN), 30)
        self.assertEqual([in_checkout(*self.ORIGIN)] * 2, git.calls)

    def test_a_kept_answer_is_still_judged_on_every_check(self) -> None:
        git = AnsweringGit()
        git.answers[self.ORIGIN] = [GitResult(0, "https://github.com/example/one.git\n", "")]
        git.answers[self.COMMIT] = [GitResult(0, f"{FULL_COMMIT}\n", "")]
        runner = PrepareGitRunner(git)
        checkout = Path("C:/checkout")
        verify_checkout_remote(checkout, "example/one", runner)
        with self.assertRaisesRegex(RuntimeContractError, "origin mismatch: expected other/repo, found example/one"):
            verify_checkout_remote(checkout, "other/repo", runner)
        self.assertEqual(FULL_COMMIT, resolve_reviewer_commit(checkout, FULL_COMMIT, head_sha="4" * 40, runner=runner))
        with self.assertRaisesRegex(RuntimeContractError, "resolves to the pull-request head"):
            resolve_reviewer_commit(checkout, FULL_COMMIT, head_sha=FULL_COMMIT, runner=runner)
        self.assertEqual(
            [in_checkout(*self.ORIGIN, checkout=str(checkout)), in_checkout(*self.COMMIT, checkout=str(checkout))],
            git.calls,
        )


def git_blob_id(content: bytes) -> str:
    """The SHA-1 git blob id of `content`, computed here rather than by the code under test."""
    return hashlib.sha1(b"blob %d\0" % len(content) + content, usedforsecurity=False).hexdigest()


def entrypoint_files(*resources: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": "fixture-review",
        "protocol_version": 1,
        "supports": ["initial"],
        "required_capabilities": ["read-diff", "write-result"],
        "entrypoint": "review/SKILL.md",
        "resources": list(resources),
        "agent_profiles": [],
    }


class CommittedReadTests(unittest.TestCase):
    """Reviewer files come from a commit's tree in batches, one `git ls-tree` per commit for their entries and one
    `git cat-file --batch` for their bytes by the blob ids those entries gave, with every refusal before any read; and a
    diff's base blob is sized before its content is asked for."""

    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.git("init", "-q", "-b", "main")
        self.calls: list[list[str]] = []
        self.batches: list[list[str]] = []

    def git(self, *arguments: str, data: bytes | None = None, index: Path | None = None) -> str:
        environment = None if index is None else {**os.environ, "GIT_INDEX_FILE": str(index)}
        return (
            subprocess.run(
                ["git", "-C", str(self.checkout), *arguments],
                input=data,
                capture_output=True,
                check=True,
                env=environment,
            )
            .stdout.decode("ascii")
            .strip()
        )

    def commit(self, entries: Mapping[str, bytes | tuple[str, str]], *parents: str) -> str:
        """A commit whose tree holds exactly `entries`: a path's bytes as a regular file, or (mode, object id)."""
        index = self.root / f"index-{len(list(self.root.glob('index-*')))}"
        for path, entry in entries.items():
            mode, object_id = entry if isinstance(entry, tuple) else ("100644", self.blob(entry))
            self.git("update-index", "--add", "--cacheinfo", f"{mode},{object_id},{path}", index=index)
        tree = self.git("write-tree", index=index)
        return self.git("commit-tree", tree, *(part for parent in parents for part in ("-p", parent)), "-m", "fixture")

    def blob(self, content: bytes) -> str:
        return self.git("hash-object", "-w", "--stdin", data=content)

    def runner(self, arguments: Sequence[str], timeout: float) -> GitResult:
        self.calls.append(list(arguments[3:]))
        return subprocess_runner(arguments, timeout)

    def reader(self, checkout: Path, blobs: Sequence[str]) -> Iterator[bytes]:
        self.batches.append(list(blobs))
        yield from review_runtime.git_blob_reader(checkout, blobs)

    def materialize(self, commit: str, manifest: dict[str, Any], name: str = "reviewer", **options: Any) -> Path:
        destination = self.root / name
        self.hashes = materialize_reviewer(
            self.checkout, commit, manifest, destination, runner=self.runner, blob_reader=self.reader, **options
        )
        return destination

    def test_each_commit_s_entries_come_from_one_ls_tree_and_every_blob_from_one_batch(self) -> None:
        crlf, latin1, same = b"Shared\r\nrules\r\n", b"caf\xe9\n", b"same text\n"
        trusted_g1, base_g1, g2 = b"trusted guideline\n", b"base guideline\n", b"guideline two\n"
        trusted = self.commit(
            {
                "docs/shared.md": crlf,
                "docs/same.md": same,
                "agents/p.md": latin1,
                "docs/g1.md": trusted_g1,
                "docs/g2.md": g2,
                "docs/copy.md": same,
            }
        )
        base = self.commit({"docs/g1.md": base_g1, "README.md": b"base\n"})
        manifest = {
            "schema_version": 2,
            "id": "fixture",
            "protocol_version": 1,
            "kind": "specialists",
            "supports": ["initial"],
            "required_capabilities": ["agent-delegation"],
            "resources": ["docs/shared.md", "docs/same.md"],
            "specialists": [
                {
                    "id": "p-review",
                    "category": "P",
                    "profile": "agents/p.md",
                    "include": [r"\.p$"],
                    "exclude": [],
                    "resources": ["docs/g1.md", "docs/g2.md", "docs/copy.md"],
                    "when": None,
                }
            ],
            "conditions": {},
        }
        destination = self.materialize(trusted, manifest, guideline_commit=base)

        at_trusted = ["docs/shared.md", "docs/same.md", "agents/p.md", "docs/g2.md", "docs/copy.md"]
        self.assertEqual(
            [
                ["rev-parse", "--verify", f"{base}^{{commit}}"],
                ["ls-tree", "-z", base, "--", "docs/g1.md", "docs/g2.md", "docs/copy.md"],
                ["ls-tree", "-z", trusted, "--", *at_trusted],
            ],
            self.calls,
        )
        # One batch, each blob once: docs/copy.md holds the bytes docs/same.md does.
        self.assertEqual([[git_blob_id(blob) for blob in (crlf, same, latin1, base_g1, g2)]], self.batches)
        expected = {
            "docs/shared.md": crlf,
            "docs/same.md": same,
            "agents/p.md": latin1,
            "docs/g1.md": base_g1,
            "docs/g2.md": g2,
            "docs/copy.md": same,
        }
        self.assertEqual(expected, {path: (destination / path).read_bytes() for path in expected})
        self.assertEqual({path: hashlib.sha256(content).hexdigest() for path, content in expected.items()}, self.hashes)
        self.assertEqual(
            {"docs/g1.md": base, "docs/g2.md": trusted, "docs/copy.md": trusted},
            json.loads((destination / "materialization.json").read_text(encoding="utf-8"))["guideline_sources"],
        )

    def test_a_refused_entry_anywhere_in_a_batch_fails_before_any_blob_is_read(self) -> None:
        submodule = self.commit({"lib.txt": b"vendored\n"})
        trusted = self.commit(
            {
                "review/SKILL.md": b"# Review\n",
                "docs/a.md": b"first\n",
                "docs/z.md": b"last\n",
                "docs/link.md": ("120000", self.blob(b"a.md")),
                "vendor/lib": ("160000", submodule),
            }
        )
        cases = (
            ("docs/link.md", "Declared reviewer file is not a regular file: docs/link.md", 1),
            ("vendor/lib", "Declared reviewer file is not a regular file: vendor/lib", 1),
            ("docs/gone.md", "Declared reviewer file is missing: docs/gone.md", 1),
            # A folder beside a path under it takes a call of its own, or ls-tree would list the folder's entries.
            ("docs", "Declared reviewer file is not a regular file: docs", 2),
        )
        for index, (refused, message, listings) in enumerate(cases):
            with self.subTest(refused):
                self.calls.clear()
                self.batches.clear()
                destination = self.root / f"refused-{index}"
                with self.assertRaises(RuntimeContractError) as raised:
                    self.materialize(trusted, entrypoint_files("docs/a.md", refused, "docs/z.md"), destination.name)
                self.assertEqual(message, str(raised.exception))
                self.assertEqual(listings, len(self.calls))
                self.assertEqual([], self.batches)
                self.assertFalse(destination.exists())

    def test_a_guideline_the_base_holds_as_no_regular_file_is_refused_there(self) -> None:
        trusted = self.commit({"docs/g.md": b"trusted\n"})
        manifest = {
            "schema_version": 2,
            "id": "fixture",
            "protocol_version": 1,
            "kind": "specialists",
            "supports": ["initial"],
            "required_capabilities": ["agent-delegation"],
            "resources": [],
            "specialists": [
                {
                    "id": "g-review",
                    "category": "G",
                    "profile": "suite:design-review",
                    "include": [r"\.g$"],
                    "exclude": [],
                    "resources": ["docs/g.md"],
                    "when": None,
                }
            ],
            "conditions": {},
        }
        base = self.commit({"docs/g.md": ("120000", self.blob(b"../secret"))})
        with self.assertRaisesRegex(RuntimeContractError, "^Declared reviewer file is not a regular file: docs/g.md$"):
            self.materialize(trusted, manifest, guideline_commit=base)
        self.assertEqual([], self.batches)
        self.assertEqual([base], [call[2] for call in self.calls if call[0] == "ls-tree"], "never the trusted copy")

    def test_bytes_that_do_not_hash_to_the_listed_blob_are_refused(self) -> None:
        trusted = self.commit({"review/SKILL.md": b"# Review\n", "docs/a.md": b"rules\n"})

        def swapped(checkout: Path, blobs: Sequence[str]) -> Iterator[bytes]:
            yield b"# Review\n"
            yield b"other rules\n"

        def missing(checkout: Path, blobs: Sequence[str]) -> Iterator[bytes]:
            yield b"# Review\n"
            raise RuntimeContractError(f"Blob {blobs[1]} is missing from the checkout's object store")

        for name, reader in (("swapped", swapped), ("missing", missing)):
            with self.subTest(name), self.assertRaises(RuntimeContractError) as raised:
                materialize_reviewer(
                    self.checkout, trusted, entrypoint_files("docs/a.md"), self.root / name, blob_reader=reader
                )
            self.assertEqual("Cannot read declared reviewer file: docs/a.md", str(raised.exception))
            self.assertFalse((self.root / name).exists())

    def test_a_long_list_of_paths_is_split_across_listings_and_read_in_one_batch(self) -> None:
        names = [f"docs/{index:02d}-{'x' * 30}.md" for index in range(6)]
        trusted = self.commit({"review/SKILL.md": b"# Review\n", **{name: name.encode() for name in names}})
        with mock.patch.object(review_runtime, "LS_TREE_PATH_UNITS", 100):
            destination = self.materialize(trusted, entrypoint_files(*names))
        listed = [call[4:] for call in self.calls]
        self.assertEqual(["review/SKILL.md", *names], [path for call in listed for path in call])
        self.assertTrue(all(sum(len(path) + 3 for path in call) <= 100 for call in listed), listed)
        self.assertEqual(4, len(listed))
        self.assertEqual(1, len(self.batches))
        self.assertEqual([name.encode() for name in names], [(destination / name).read_bytes() for name in names])

    def test_a_single_reviewer_file_is_read_by_its_listed_blob_through_the_runner(self) -> None:
        content = b"{\r\n}\r\n"
        trusted = self.commit({".review/manifest.json": content})
        self.assertEqual(
            content, review_runtime._read_git_file(self.checkout, trusted, ".review/manifest.json", self.runner)
        )
        self.assertEqual(
            [["ls-tree", "-z", trusted, "--", ".review/manifest.json"], ["cat-file", "blob", git_blob_id(content)]],
            self.calls,
        )

    def test_the_changed_file_limit_is_sixteen_mebibytes(self) -> None:
        self.assertEqual(16_777_216, review_runtime.MAX_CHANGED_FILE_BYTES)

    def test_a_base_blob_over_the_limit_is_refused_by_its_size_without_its_content_being_asked_for(self) -> None:
        at_limit, over = b"a" * 16_777_216, b"a" * 16_777_217
        for content in (at_limit, over):
            self.blob(content)
        limit = review_runtime.MAX_CHANGED_FILE_BYTES
        for content, expected in ((at_limit, at_limit), (over, None)):
            blob = git_blob_id(content)
            with self.subTest(size=len(content)):
                self.calls.clear()
                self.assertEqual(
                    expected, review_runtime.read_blob(self.checkout, blob[:7], self.runner, maximum=limit)
                )
                read = [["cat-file", "blob", blob]] if expected is not None else []
                self.assertEqual(
                    [["rev-parse", "--verify", f"{blob[:7]}^{{blob}}"], ["cat-file", "-s", blob], *read], self.calls
                )
        self.calls.clear()
        self.assertEqual(over, review_runtime.read_blob(self.checkout, git_blob_id(over)[:7], self.runner))
        self.assertNotIn("-s", [call[1] for call in self.calls], "no limit asks for no size")

    def test_a_base_blob_id_that_names_no_single_blob_is_refused(self) -> None:
        commit = self.commit({"docs/a.md": b"rules\n"})
        tree = self.git("rev-parse", f"{commit}^{{tree}}")
        for abbreviation in ("not-an-id", "abc", tree[:12], "0123456789abcdef"):
            with self.subTest(abbreviation), self.assertRaises(RuntimeContractError):
                review_runtime.read_blob(self.checkout, abbreviation, self.runner, maximum=1024)

        def unsized(arguments: Sequence[str], timeout: float) -> GitResult:
            if list(arguments[3:5]) == ["cat-file", "-s"]:
                return GitResult(0, "12 bytes\n", "")
            return subprocess_runner(arguments, timeout)

        blob = git_blob_id(b"rules\n")
        with self.assertRaisesRegex(RuntimeContractError, f"^The checkout did not return the size of blob {blob}$"):
            review_runtime.read_blob(self.checkout, blob[:7], unsized, maximum=1024)
