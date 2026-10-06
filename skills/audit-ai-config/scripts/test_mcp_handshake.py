#!/usr/bin/env python3
"""Regression tests for the opt-in MCP stdio handshake."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mcp_handshake

FAKE_SERVER = """\
import json, sys, time
mode = sys.argv[1]
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
for line in sys.stdin:
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
            session.process.wait(timeout=10)
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
                    "HANDSHAKE_FAILED crash source=.mcp.json server exited before responding: boom: stderr after stdout closed"
                ],
            ),
            self._run(),
        )

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
        self.assertTrue(lines[0].startswith("CONFIG_ERROR .codex/config.toml Could not read or parse: "), lines)

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

    def test_a_usage_error_exits_2(self) -> None:
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
            mcp_handshake.main(["--root", str(self.root), "--timeout", "soon"])
        self.assertEqual(2, raised.exception.code)
        self.assertIn("--timeout", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
