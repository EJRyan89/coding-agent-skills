#!/usr/bin/env python3
"""Regression tests for the opt-in MCP stdio handshake."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import audit_ai_config
import bounded_process
import mcp_handshake

# Runs no test of its own; the test function below checks through its assertions, as the classes do.
CHECK = unittest.TestCase()

# The launcher test's --timeout, and how far past --timeout and the stop allowance a loaded machine may end the run.
LAUNCHER_TIMEOUT_SECONDS = 2.0
MARGIN_SECONDS = 10.0
# Characters a line reader may take as the end of a line, each of which a configured value can hold.
LINE_SEPARATORS = ("\n", "\r", "\u0085", "\u2028", "\u2029")


def link_directory(target: Path, link: Path) -> None:
    """A link to a directory: a junction on Windows, which needs no privilege, else a symbolic link."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


FAKE_SERVER = """\
import json, sys, time
mode = sys.argv[1]
if mode == "launcher":
    # Read the first request, start a program that inherits stdout and waits until the file named by the next
    # argument exists, and exit, so the program holds stdout open after the server is gone.
    import subprocess, tempfile
    sys.stdin.buffer.readline()
    waiter = "import os, sys, time\\nfor _ in range(1200):\\n    if os.path.exists(sys.argv[1]): break\\n"
    waiter += "    time.sleep(0.1)\\n"
    subprocess.Popen([sys.executable, "-c", waiter, sys.argv[2]], cwd=tempfile.gettempdir())
    sys.exit(0)
if mode == "exit":
    print("boom: missing dependency", file=sys.stderr)
    sys.exit(3)
if mode == "lateexit":
    __import__("os").close(1)
    time.sleep(0.5)
    print("boom: stderr after stdout closed", file=sys.stderr)
    sys.exit(3)
SCHEMA = {"type": "object"}
INFO = {"name": "fake", "version": "1"}
INITIALIZE = {
    "empty": {},
    "noversion": {"capabilities": {}, "serverInfo": INFO},
    "oldversion": {"protocolVersion": "2023-01-01", "capabilities": {}, "serverInfo": INFO},
    "nocapabilities": {"protocolVersion": "2025-06-18", "serverInfo": INFO},
    "badtoolscapability": {"protocolVersion": "2025-06-18", "capabilities": {"tools": True}, "serverInfo": INFO},
    "noinfo": {"protocolVersion": "2025-06-18", "capabilities": {}},
    "older": {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": INFO},
}
TOOLS = {
    "badtools": {"tools": {"search": {}}},
    "notoolsfield": {},
    "badtool": {"tools": [{"name": "search"}]},
    "nonobject": {"tools": ["search"]},
    "loop": {"tools": [{"name": "search", "inputSchema": SCHEMA}], "nextCursor": "again"},
}
for line in sys.stdin.buffer:
    if mode == "record":
        open("received.bin", "ab").write(line)
    message = json.loads(line)
    if mode == "silent":
        time.sleep(30)
    if message.get("method") == "initialize":
        if mode == "error":
            reply = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "nope"}}
        else:
            capabilities = {} if mode == "notools" else {"tools": {}}
            print("not json", flush=True)
            print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message"}), flush=True)
            result = INITIALIZE.get(mode, {"protocolVersion": "2025-06-18", "capabilities": capabilities,
                                           "serverInfo": INFO})
            reply = {"jsonrpc": "2.0", "id": 1, "result": result}
        print(json.dumps(reply), flush=True)
    elif message.get("method") == "notifications/initialized":
        open("initialized.txt", "w").write("initialized")
    elif message.get("method") == "tools/list":
        if mode == "env" and __import__("os").environ.get("FIXTURE_TOKEN") != "a b":
            sys.exit(4)
        if mode == "norepository":
            # The configured value and a variable that configures git arrive; no repository-selecting one does.
            seen = {name.upper() for name in __import__("os").environ}
            if not {"FIXTURE_TOKEN", "GIT_CONFIG_COUNT"} <= seen or seen & set(sys.argv[2].split(",")):
                sys.exit(4)
        cursor = message.get("params", {}).get("cursor")
        if mode == "paged" and cursor is None:
            result = {"tools": [{"name": "search", "inputSchema": SCHEMA}], "nextCursor": "page 2"}
        elif mode == "paged" and cursor == "page 2":
            result = {"tools": [{"name": "list", "inputSchema": SCHEMA}, {"name": "get", "inputSchema": SCHEMA}]}
        else:
            result = TOOLS.get(mode, {"tools": [{"name": "search", "inputSchema": SCHEMA},
                                                {"name": "list", "inputSchema": SCHEMA}]})
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif message.get("method") == "tools/call":
        open("called.txt", "w").write("called")
"""


class HandshakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo with spaces"
        (self.root / ".git").mkdir(parents=True)
        self.server = self.root / "tools" / "fake server.py"
        self.server.parent.mkdir()
        self.server.write_text(FAKE_SERVER, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _stdio(self, mode: str, **extra: object) -> dict[str, object]:
        return {"command": sys.executable, "args": [str(self.server), mode], **extra}

    def _write(self, rel: str, wrapper: str, servers: dict[str, object]) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({wrapper: servers}), encoding="utf-8")

    def _run(self, *arguments: str) -> tuple[int, list[str]]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = mcp_handshake.main(["--root", str(self.root), "--timeout", "5", *arguments])
        return code, output.getvalue().splitlines()

    def test_successful_handshake_lists_tools_without_calling_one(self) -> None:
        self._write(".mcp.json", "mcpServers", {"docs": self._stdio("ok", cwd="tools")})
        code, lines = self._run()
        self.assertEqual(0, code, lines)
        self.assertEqual(["HANDSHAKE_OK docs source=.mcp.json protocol=2025-06-18 tools=2"], lines)
        self.assertFalse((self.root / "tools" / "called.txt").exists())
        self.assertTrue((self.root / "tools" / "initialized.txt").exists())

    def test_server_without_tools_capability_is_not_asked_for_tools(self) -> None:
        self._write(".mcp.json", "mcpServers", {"docs": self._stdio("notools")})
        self.assertEqual((0, ["HANDSHAKE_OK docs source=.mcp.json protocol=2025-06-18 tools=none"]), self._run())

    def test_supported_older_protocol_and_paged_tools_pass(self) -> None:
        self._write(".mcp.json", "mcpServers", {"older": self._stdio("older"), "paged": self._stdio("paged")})
        self.assertEqual(
            (
                0,
                [
                    "HANDSHAKE_OK older source=.mcp.json protocol=2025-03-26 tools=none",
                    "HANDSHAKE_OK paged source=.mcp.json protocol=2025-06-18 tools=3",
                ],
            ),
            self._run(),
        )

    def test_supported_protocol_versions_are_the_published_revisions(self) -> None:
        self.assertEqual({"2024-11-05", "2025-03-26", "2025-06-18"}, mcp_handshake.SUPPORTED_PROTOCOL_VERSIONS)
        self.assertEqual("2025-06-18", mcp_handshake.PROTOCOL_VERSION)

    def test_malformed_initialize_results_fail_without_acknowledging(self) -> None:
        for mode, reason in (
            ("empty", "initialize result has no protocolVersion"),
            ("noversion", "initialize result has no protocolVersion"),
            ("oldversion", 'server chose unsupported protocol version "2023-01-01"'),
            ("nocapabilities", "initialize result has no capabilities object"),
            ("badtoolscapability", "initialize result capabilities.tools is not an object"),
            ("noinfo", "initialize result has no serverInfo name and version"),
        ):
            with self.subTest(mode=mode):
                self._write(".mcp.json", "mcpServers", {"bad": self._stdio(mode, cwd="tools")})
                self.assertEqual((1, [f"HANDSHAKE_FAILED bad source=.mcp.json {reason}"]), self._run())
                self.assertFalse((self.root / "tools" / "initialized.txt").exists())

    def test_malformed_tool_lists_fail(self) -> None:
        for mode, reason in (
            ("badtools", "tools/list result has no tools array"),
            ("notoolsfield", "tools/list result has no tools array"),
            ("badtool", "tools/list returned a tool without a name and object inputSchema"),
            ("nonobject", "tools/list returned a tool without a name and object inputSchema"),
            ("loop", "tools/list returned an invalid or repeated nextCursor"),
        ):
            with self.subTest(mode=mode):
                self._write(".mcp.json", "mcpServers", {"bad": self._stdio(mode)})
                self.assertEqual((1, [f"HANDSHAKE_FAILED bad source=.mcp.json {reason}"]), self._run())

    def test_configured_environment_reaches_the_server(self) -> None:
        self._write(".mcp.json", "mcpServers", {"docs": self._stdio("env", env={"FIXTURE_TOKEN": "a b"})})
        code, lines = self._run()
        self.assertEqual(0, code, lines)

    def test_a_server_sees_no_repository_selecting_variable_the_shell_exported(self) -> None:
        selecting = (
            "GIT_ALTERNATE_OBJECT_DIRECTORIES,GIT_COMMON_DIR,GIT_CONFIG,GIT_DIR,GIT_GRAFT_FILE,GIT_IMPLICIT_WORK_TREE,"
            "GIT_INDEX_FILE,GIT_NAMESPACE,GIT_NO_REPLACE_OBJECTS,GIT_OBJECT_DIRECTORY,GIT_PREFIX,GIT_REPLACE_REF_BASE,"
            "GIT_SHALLOW_FILE,GIT_WORK_TREE"
        )
        server = {"command": sys.executable, "args": [str(self.server), "norepository", selecting]}
        self._write(".mcp.json", "mcpServers", {"docs": {**server, "env": {"FIXTURE_TOKEN": "a b"}}})
        exported = {name: str(self.root / ".git") for name in selecting.split(",")}
        with mock.patch.dict(os.environ, {**exported, "GIT_CONFIG_COUNT": "0"}):
            code, lines = self._run()
        self.assertEqual((0, ["HANDSHAKE_OK docs source=.mcp.json protocol=2025-06-18 tools=2"]), (code, lines))

    def test_a_server_sees_no_repository_selecting_variable_its_configured_environment_names(self) -> None:
        server = {"command": sys.executable, "args": [str(self.server), "norepository", "GIT_DIR,GIT_WORK_TREE"]}
        configured = {
            "FIXTURE_TOKEN": "a b",
            "GIT_CONFIG_COUNT": "0",
            "GIT_DIR": str(self.root / ".git"),
            "GIT_WORK_TREE": str(self.root),
        }
        self._write(".mcp.json", "mcpServers", {"docs": {**server, "env": configured}})
        # Only the configured environment names the variables, so the server's never seeing them is the strip.
        with mock.patch.dict(os.environ):
            os.environ.pop("GIT_DIR", None)
            os.environ.pop("GIT_WORK_TREE", None)
            code, lines = self._run()
        self.assertEqual((0, ["HANDSHAKE_OK docs source=.mcp.json protocol=2025-06-18 tools=2"]), (code, lines))

    def test_identical_definitions_are_started_once(self) -> None:
        self._write(".mcp.json", "mcpServers", {"docs": self._stdio("ok")})
        self._write(".vscode/mcp.json", "servers", {"docs": self._stdio("ok")})
        code, lines = self._run()
        self.assertEqual(0, code, lines)
        self.assertEqual(["HANDSHAKE_OK docs source=.mcp.json,.vscode/mcp.json protocol=2025-06-18 tools=2"], lines)

    def test_failures_are_reported_per_server(self) -> None:
        self._write(
            ".mcp.json",
            "mcpServers",
            {
                "crash": self._stdio("exit"),
                "refuse": self._stdio("error"),
                "missing": {"command": "no-such-mcp-command-for-tests"},
            },
        )
        code, lines = self._run()
        self.assertEqual(1, code)
        self.assertEqual(3, len(lines), lines)
        self.assertTrue(lines[0].startswith("HANDSHAKE_FAILED crash source=.mcp.json server exited before responding"))
        self.assertIn("boom: missing dependency", lines[0])
        self.assertTrue(lines[1].startswith("HANDSHAKE_FAILED refuse source=.mcp.json server returned an error"))
        self.assertEqual(
            "HANDSHAKE_FAILED missing source=.mcp.json command not found: no-such-mcp-command-for-tests", lines[2]
        )

    def test_a_server_gone_before_the_first_write_reports_its_exit(self) -> None:
        # Force the race CI hit: the server exits before initialize is written, so the write itself fails.
        original = mcp_handshake.Session.send

        def send_after_exit(session: mcp_handshake.Session, message: dict) -> None:
            session.running.wait()
            original(session, message)

        self._write(".mcp.json", "mcpServers", {"crash": self._stdio("exit")})
        with mock.patch.object(mcp_handshake.Session, "send", send_after_exit):
            code, lines = self._run()
        self.assertEqual(1, code)
        self.assertEqual(
            ["HANDSHAKE_FAILED crash source=.mcp.json server exited before responding: boom: missing dependency"], lines
        )

    def test_stderr_written_after_stdout_closes_is_still_reported(self) -> None:
        self._write(".mcp.json", "mcpServers", {"crash": self._stdio("lateexit")})
        self.assertEqual(
            (
                1,
                [
                    "HANDSHAKE_FAILED crash source=.mcp.json server exited "
                    "before responding: boom: stderr after stdout closed"
                ],
            ),
            self._run(),
        )

    def test_a_server_started_without_its_pipes_is_stopped_and_fails(self) -> None:
        process = mock.MagicMock(spec=subprocess.Popen)
        process.stdin, process.stdout = mock.MagicMock(), None
        with (
            mock.patch.object(bounded_process.subprocess, "Popen", return_value=process),
            self.assertRaisesRegex(RuntimeError, r"^could not start: the command started without its pipes$"),
        ):
            mcp_handshake.handshake(self.root, self._stdio("ok"), 5)
        process.kill.assert_called_once_with()

    def test_messages_reach_the_server_as_lines_ended_by_a_line_feed_alone(self) -> None:
        self._write(".mcp.json", "mcpServers", {"docs": self._stdio("record", cwd="tools")})
        self.assertEqual((0, ["HANDSHAKE_OK docs source=.mcp.json protocol=2025-06-18 tools=2"]), self._run())
        received = (self.root / "tools" / "received.bin").read_bytes()
        self.assertNotIn(b"\r", received)
        self.assertTrue(received.endswith(b"\n"), received)
        methods = [json.loads(line)["method"] for line in received.split(b"\n")[:-1]]
        self.assertEqual(["initialize", "notifications/initialized", "tools/list"], methods)

    def test_a_program_a_launcher_leaves_holding_its_output_does_not_hold_the_run_past_the_timeout(self) -> None:
        # Before the handshake ran on the bounded process layer, the stop waited on the reader behind the program
        # for as long as it ran, here until the test releases it.
        release = Path(self.temp.name) / "release"
        launched = {"command": sys.executable, "args": [str(self.server), "launcher", str(release)]}
        self._write(".mcp.json", "mcpServers", {"launched": launched})
        output = io.StringIO()
        started = time.monotonic()
        try:
            with contextlib.redirect_stdout(output):
                code = mcp_handshake.main(["--root", str(self.root), "--timeout", str(LAUNCHER_TIMEOUT_SECONDS)])
            elapsed = time.monotonic() - started
        finally:
            release.touch()
        self.assertEqual(
            (1, ["HANDSHAKE_FAILED launched source=.mcp.json timed out waiting for a response"]),
            (code, output.getvalue().splitlines()),
        )
        self.assertLess(elapsed, LAUNCHER_TIMEOUT_SECONDS + bounded_process.KILL_WAIT_SECONDS + MARGIN_SECONDS)

    def test_the_usage_states_the_stop_allowance_the_process_layer_gives(self) -> None:
        self.assertEqual(10.0, bounded_process.KILL_WAIT_SECONDS)
        self.assertIn("up to ten seconds more are allowed", " ".join((mcp_handshake.__doc__ or "").split()))

    def test_unresponsive_server_times_out(self) -> None:
        self._write(".mcp.json", "mcpServers", {"slow": self._stdio("silent")})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = mcp_handshake.main(["--root", str(self.root), "--timeout", "1"])
        self.assertEqual(1, code)
        self.assertEqual(
            "HANDSHAKE_FAILED slow source=.mcp.json timed out waiting for a response", output.getvalue().strip()
        )

    def test_remote_servers_are_skipped_and_filter_selects_one(self) -> None:
        self._write(".github/mcp.json", "mcpServers", {"remote": {"url": "https://example.invalid/mcp"}})
        (self.root / ".codex").mkdir()
        (self.root / ".codex/config.toml").write_text(
            '[mcp_servers.events]\nurl = "https://example.invalid/sse"\ntype = "sse"\n', encoding="utf-8"
        )
        self.assertEqual(
            (
                0,
                [
                    "SKIPPED remote source=.github/mcp.json transport=http",
                    "SKIPPED events source=.codex/config.toml transport=sse",
                ],
            ),
            self._run(),
        )
        self.assertEqual(
            (0, ["SKIPPED events source=.codex/config.toml transport=sse"]), self._run("--server", "events")
        )

    def test_unreadable_configs_fail_instead_of_reporting_no_servers(self) -> None:
        (self.root / ".mcp.json").write_text('{"mcpServers": {', encoding="utf-8")
        code, lines = self._run()
        self.assertEqual(1, code, lines)
        self.assertEqual(1, len(lines), lines)
        self.assertTrue(lines[0].startswith("CONFIG_ERROR .mcp.json Could not read or parse: "), lines)

        (self.root / ".mcp.json").unlink()
        (self.root / ".codex").mkdir()
        (self.root / ".codex/config.toml").write_text("[mcp_servers.docs\n", encoding="utf-8")
        code, lines = self._run()
        self.assertEqual(1, code, lines)
        self.assertTrue(lines[0].startswith("CONFIG_ERROR .codex/config.toml Invalid TOML: "), lines)

    def test_one_malformed_server_is_a_config_problem_while_the_others_run(self) -> None:
        self._write(".mcp.json", "mcpServers", {"bad": {}, "note": "text", "docs": self._stdio("ok")})
        self.assertEqual(
            (
                1,
                [
                    "CONFIG_ERROR .mcp.json Server 'bad': must have 'command' (STDIO/local) or 'url' (HTTP/SSE)",
                    "CONFIG_WARNING .mcp.json Server 'note': entry must be an object, got str",
                    "SKIPPED bad source=.mcp.json transport=unknown",
                    "HANDSHAKE_OK docs source=.mcp.json protocol=2025-06-18 tools=2",
                ],
            ),
            self._run(),
        )

    def test_codex_servers_are_read_as_the_audit_reads_them(self) -> None:
        (self.root / ".codex").mkdir()
        config = self.root / ".codex/config.toml"
        config.write_text('mcp_servers = "docs"\n', encoding="utf-8")
        self.assertEqual((1, ["CONFIG_ERROR .codex/config.toml 'mcp_servers' must be a table"]), self._run())
        config.write_text("[mcp_servers]\ndocs = 1\n", encoding="utf-8")
        self.assertEqual(
            (0, ["CONFIG_WARNING .codex/config.toml Server 'docs': entry must be a table, got int", "NO_SERVERS"]),
            self._run(),
        )

    def _launcher(self, folder: Path, stem: str) -> str:
        """A launcher of the fake server in `folder` as each platform runs one: a batch file on Windows, and an
        executable shell script elsewhere. Returns its file name."""
        if sys.platform == "win32":
            name = f"{stem}.cmd"
            (folder / name).write_text(f'@"{sys.executable}" "{self.server}" %*\n', encoding="utf-8")
        else:
            name = stem
            (folder / name).write_text(f'#!/bin/sh\nexec "{sys.executable}" "{self.server}" "$@"\n', encoding="utf-8")
            (folder / name).chmod(0o755)
        return name

    def test_a_relative_command_resolves_against_the_configured_directory_not_the_callers(self) -> None:
        name = self._launcher(self.root / "tools", "fake server")
        self._write(
            ".mcp.json",
            "mcpServers",
            {
                "rooted": {"command": f"tools/{name}", "args": ["ok"]},
                "local": {"command": f"./{name}", "args": ["ok"], "cwd": "tools"},
            },
        )
        elsewhere = Path(self.temp.name) / "elsewhere"
        elsewhere.mkdir()
        with contextlib.chdir(elsewhere):
            code, lines = self._run()
        self.assertEqual(
            (
                0,
                [
                    "HANDSHAKE_OK rooted source=.mcp.json protocol=2025-06-18 tools=2",
                    "HANDSHAKE_OK local source=.mcp.json protocol=2025-06-18 tools=2",
                ],
            ),
            (code, lines),
        )

    def test_a_bare_command_is_found_on_path_and_never_in_the_folder_the_script_runs_from(self) -> None:
        # shutil.which searches the working directory first on Windows.
        elsewhere, on_path = Path(self.temp.name) / "elsewhere", Path(self.temp.name) / "on path"
        for folder in (elsewhere, on_path):
            folder.mkdir()
            self._launcher(folder, "fake-mcp-server")
        self._write(".mcp.json", "mcpServers", {"bare": {"command": "fake-mcp-server", "args": ["ok"]}})
        missing = (1, ["HANDSHAKE_FAILED bare source=.mcp.json command not found: fake-mcp-server"])
        found = (0, ["HANDSHAKE_OK bare source=.mcp.json protocol=2025-06-18 tools=2"])
        # Without the variable that stops it, Windows' own search would find the launcher in the working directory.
        stop = "NoDefaultCurrentDirectoryInExePath".casefold()
        environment = {key: value for key, value in os.environ.items() if key.casefold() != stop}
        environment["LOCALAPPDATA"] = str(elsewhere)
        for path, expected in ((str(Path(self.temp.name) / "empty"), missing), (str(on_path), found)):
            with (
                self.subTest(found=expected is found),
                contextlib.chdir(elsewhere),
                mock.patch.dict(os.environ, {**environment, "PATH": path}, clear=True),
            ):
                self.assertEqual(expected, self._run())

    def test_a_relative_command_that_is_not_there_fails_naming_it(self) -> None:
        self._write(".mcp.json", "mcpServers", {"gone": {"command": "./missing.cmd", "cwd": "tools"}})
        self.assertEqual((1, ["HANDSHAKE_FAILED gone source=.mcp.json command not found: ./missing.cmd"]), self._run())

    def test_a_toml_date_in_a_server_definition_fails_that_server_without_a_traceback(self) -> None:
        (self.root / ".codex").mkdir()
        (self.root / ".codex/config.toml").write_text(
            f"[mcp_servers.dated]\ncommand = {json.dumps(sys.executable)}\nargs = [{json.dumps(str(self.server))}]\n"
            "env = { WHEN = 1979-05-27 }\n",
            encoding="utf-8",
        )
        self.assertEqual(
            (1, ["HANDSHAKE_FAILED dated source=.codex/config.toml env must map names to strings"]), self._run()
        )

    def test_a_definition_the_system_refuses_fails_that_server_and_the_next_still_runs(self) -> None:
        for extra, reason in (
            ({"args": [str(self.server), "ok", "a\0b"]}, "args holds a NUL character"),
            ({"cwd": "tools\0"}, "cwd holds a NUL character"),
            ({"env": {"A=B": "x"}}, "env name 'A=B' is empty or holds '='"),
            ({"env": {"": "x"}}, "env name '' is empty or holds '='"),
            ({"env": {"A": "x\0y"}}, "env holds a NUL character"),
            ({"env": {"A\0B": "x"}}, "env holds a NUL character"),
            ({"command": sys.executable + "\0"}, "command holds a NUL character"),
        ):
            with self.subTest(reason=reason):
                self._write(".mcp.json", "mcpServers", {"bad": self._stdio("ok", **extra), "good": self._stdio("ok")})
                self.assertEqual(
                    (
                        1,
                        [
                            f"HANDSHAKE_FAILED bad source=.mcp.json {reason}",
                            "HANDSHAKE_OK good source=.mcp.json protocol=2025-06-18 tools=2",
                        ],
                    ),
                    self._run(),
                )

    def test_a_working_directory_that_is_not_a_string_fails_that_server(self) -> None:
        self._write(".mcp.json", "mcpServers", {"docs": self._stdio("ok", cwd=5)})
        self.assertEqual((1, ["HANDSHAKE_FAILED docs source=.mcp.json cwd must be a string"]), self._run())

    def test_every_line_holds_one_fact_whatever_the_configuration_names(self) -> None:
        for separator in LINE_SEPARATORS:
            with self.subTest(separator=hex(ord(separator))):
                self._write(
                    ".mcp.json",
                    "mcpServers",
                    {
                        f"a{separator}HANDSHAKE_OK fake": {},
                        f"n{separator}NO_SERVERS": "text",
                        "b": {"command": f"nope{separator}NO_SERVERS"},
                        "c": {"command": sys.executable, "type": f"x{separator}HANDSHAKE_OK typed"},
                        f"ok{separator}NO_SERVERS": self._stdio("ok"),
                    },
                )
                self.assertEqual(
                    (
                        1,
                        [
                            "CONFIG_ERROR .mcp.json Server 'a HANDSHAKE_OK fake': must have 'command' (STDIO/local) or "
                            "'url' (HTTP/SSE)",
                            "CONFIG_WARNING .mcp.json Server 'n NO_SERVERS': entry must be an object, got str",
                            "SKIPPED a HANDSHAKE_OK fake source=.mcp.json transport=unknown",
                            "HANDSHAKE_FAILED b source=.mcp.json command not found: nope NO_SERVERS",
                            "SKIPPED c source=.mcp.json transport=x handshake_ok typed",
                            "HANDSHAKE_OK ok NO_SERVERS source=.mcp.json protocol=2025-06-18 tools=2",
                        ],
                    ),
                    self._run(),
                )

    def test_a_refusal_line_holds_one_fact_whatever_the_cwd_names(self) -> None:
        for separator in LINE_SEPARATORS:
            with self.subTest(separator=hex(ord(separator))):
                self._write(".mcp.json", "mcpServers", {"up": self._stdio("ok", cwd=f"../{separator}NO_SERVERS")})
                code, lines = self._run()
                self.assertEqual(1, code, lines)
                self.assertEqual(
                    [
                        "CONFIG_ERROR .mcp.json Server 'up': cwd '../ NO_SERVERS' is outside the repository",
                        "SKIPPED up source=.mcp.json transport=stdio",
                    ],
                    lines,
                )

    def test_a_cwd_outside_the_repository_is_refused_and_never_started(self) -> None:
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        link_directory(outside, self.root / "linked")
        self._write(
            ".mcp.json",
            "mcpServers",
            {
                "parent": self._stdio("record", cwd=".."),
                "absolute": self._stdio("record", cwd=str(outside)),
                "linked": self._stdio("record", cwd="linked"),
                "inside": self._stdio("ok", cwd=str(self.root / "tools")),
            },
        )
        self.assertEqual(
            (
                1,
                [
                    "CONFIG_ERROR .mcp.json Server 'parent': cwd '..' is outside the repository",
                    f"CONFIG_ERROR .mcp.json Server 'absolute': cwd '{outside}' is outside the repository",
                    "CONFIG_ERROR .mcp.json Server 'linked': cwd 'linked' is outside the repository",
                    "SKIPPED parent source=.mcp.json transport=stdio",
                    "SKIPPED absolute source=.mcp.json transport=stdio",
                    "SKIPPED linked source=.mcp.json transport=stdio",
                    "HANDSHAKE_OK inside source=.mcp.json protocol=2025-06-18 tools=2",
                ],
            ),
            self._run(),
        )
        self.assertEqual([], list(Path(self.temp.name).rglob("received.bin")))

    def test_a_relative_command_outside_the_repository_is_refused_in_every_file_that_declares_it(self) -> None:
        name = self._launcher(Path(self.temp.name), "outside server")
        self._launcher(self.root, "outside server")  # what the nested server's ../ reaches, inside the root
        server = {"command": f"../{name}", "args": ["ok"]}
        self._write(".mcp.json", "mcpServers", {"escape": server, "nested": {**server, "cwd": "tools"}})
        self._write(".github/mcp.json", "mcpServers", {"escape": server})
        self.assertEqual(
            (
                1,
                [
                    f"CONFIG_ERROR .mcp.json Server 'escape': command '../{name}' is outside the repository",
                    f"CONFIG_ERROR .github/mcp.json Server 'escape': command '../{name}' is outside the repository",
                    "SKIPPED escape source=.mcp.json,.github/mcp.json transport=stdio",
                    "HANDSHAKE_OK nested source=.mcp.json protocol=2025-06-18 tools=2",
                ],
            ),
            self._run(),
        )

    def test_the_handshake_itself_never_starts_a_server_outside_the_repository(self) -> None:
        for entry in (self._stdio("ok", cwd=".."), {"command": "../server.cmd", "cwd": "tools/.."}):
            with self.subTest(entry=entry), self.assertRaisesRegex(RuntimeError, "is outside the repository"):
                mcp_handshake.handshake(self.root, entry, 5)

    def test_a_readable_server_is_still_checked_beside_an_unreadable_config(self) -> None:
        self._write(".mcp.json", "mcpServers", {"docs": self._stdio("ok")})
        (self.root / ".vscode").mkdir()
        (self.root / ".vscode/mcp.json").write_text("not json", encoding="utf-8")
        code, lines = self._run()
        self.assertEqual(1, code, lines)
        self.assertTrue(lines[0].startswith("CONFIG_ERROR .vscode/mcp.json "), lines)
        self.assertEqual("HANDSHAKE_OK docs source=.mcp.json protocol=2025-06-18 tools=2", lines[1])

    def test_no_servers_is_a_result(self) -> None:
        self.assertEqual((0, ["NO_SERVERS"]), self._run())

    def test_a_non_repository_fails_on_stdout_with_exit_1(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = mcp_handshake.main(["--root", str(self.root / "tools")])
        self.assertEqual(1, code)
        self.assertEqual([f"FAILED {self.root / 'tools'} is not a Git repository"], output.getvalue().splitlines())
        self.assertEqual("", errors.getvalue())

    def test_a_server_name_that_matches_nothing_fails(self) -> None:
        self._write(".mcp.json", "mcpServers", {"docs": self._stdio("ok")})
        self.assertEqual((1, ["FAILED no MCP server named absent"]), self._run("--server", "absent"))
        (self.root / ".mcp.json").unlink()
        self.assertEqual((1, ["FAILED no MCP server named absent"]), self._run("--server", "absent"))

    def test_a_server_name_beside_an_unreadable_config_names_both(self) -> None:
        (self.root / ".mcp.json").write_text('{"mcpServers": {', encoding="utf-8")
        code, lines = self._run("--server", "docs")
        self.assertEqual(1, code, lines)
        self.assertEqual(2, len(lines), lines)
        self.assertTrue(lines[0].startswith("CONFIG_ERROR .mcp.json Could not read or parse: "), lines)
        self.assertEqual("FAILED no MCP server named docs", lines[1])

    def test_output_survives_a_console_that_cannot_encode_it(self) -> None:
        # A Windows pipe defaults to a legacy code page, and a server is reported by the name its config gives it.
        self._write(".mcp.json", "mcpServers", {"docs → ✓": self._stdio("ok", cwd="tools")})
        result = subprocess.run(
            [sys.executable, "-B", str(Path(mcp_handshake.__file__).resolve()), "--root", str(self.root)],
            capture_output=True,
            env={**os.environ, "PYTHONIOENCODING": "cp1252"},
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(
            ["HANDSHAKE_OK docs → ✓ source=.mcp.json protocol=2025-06-18 tools=2"],
            result.stdout.decode("utf-8").splitlines(),
        )

    def test_a_usage_error_exits_2(self) -> None:
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
            mcp_handshake.main(["--root", str(self.root), "--timeout", "soon"])
        self.assertEqual(2, raised.exception.code)
        self.assertIn("--timeout", errors.getvalue())


# Shapes of a JSON MCP configuration that declare no server the handshake would start.
CONFIG_SHAPES = [
    pytest.param('{"servers": {"note": "text", "bad": {}}}', id="servers-a-note-and-an-empty-server"),
    pytest.param('{"mcpServers": {"note": "text", "bad": {}}}', id="mcpServers-a-note-and-an-empty-server"),
    pytest.param('{"mcpServers": {}}', id="mcpServers-empty"),
    pytest.param('{"servers": {}}', id="servers-empty"),
    pytest.param("{}", id="an-empty-object"),
    pytest.param("[]", id="a-list"),
    pytest.param('{"servers": []}', id="servers-a-list"),
    pytest.param('{"mcpServers": []}', id="mcpServers-a-list"),
    pytest.param("{bad", id="not-json"),
]


class OneReaderTests(unittest.TestCase):
    """The audit and the handshake report the same problems, at the same severity, for every shape of each file."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        (self.root / ".git").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_the_json_files_the_audit_and_the_handshake_read(self) -> None:
        self.assertEqual(
            {".mcp.json": "mcpServers", ".github/mcp.json": "mcpServers", ".vscode/mcp.json": "servers"},
            audit_ai_config.MCP_JSON_FILES,
        )

    def test_a_vscode_file_without_its_wrapper_fails_both(self) -> None:
        (self.root / ".vscode").mkdir()
        (self.root / ".vscode/mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = mcp_handshake.main(["--root", str(self.root)])
        self.assertEqual(
            (1, ["CONFIG_ERROR .vscode/mcp.json VS Code MCP must use 'servers' wrapper (not 'mcpServers')"]),
            (code, output.getvalue().splitlines()),
        )


@pytest.mark.parametrize("rel", list(audit_ai_config.MCP_JSON_FILES))
@pytest.mark.parametrize("shape", CONFIG_SHAPES)
def test_each_file_shape_reads_the_same_to_the_audit_and_the_handshake(tmp_path: Path, rel: str, shape: str) -> None:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(shape, encoding="utf-8")
    audited = [(f"CONFIG_{f.severity}", f.message) for f in audit_ai_config.check_mcp(root, {}) if f.path == rel]
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = mcp_handshake.main(["--root", str(root), "--timeout", "5"])
    lines = output.getvalue().splitlines()
    config = [tuple(line.split(f" {rel} ", 1)) for line in lines if line.startswith("CONFIG_")]
    CHECK.assertEqual(audited, config)
    failed = any(severity == "CONFIG_ERROR" for severity, _ in audited)
    CHECK.assertEqual(1 if failed else 0, code, lines)
