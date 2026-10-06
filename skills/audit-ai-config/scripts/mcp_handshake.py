#!/usr/bin/env python3
"""Opt-in MCP stdio handshake for audit-ai-config: initialize and list tools, never call one.

    python -B mcp_handshake.py --root <repository> [--server NAME] [--timeout SECONDS]

This STARTS repository-configured commands, so run it only after the user explicitly
authorizes operational validation of a trusted repository. Each stdio server declared in
.mcp.json, .github/mcp.json, .vscode/mcp.json, or .codex/config.toml is started with its
configured command, arguments, working directory, and environment; it receives initialize,
notifications/initialized, and tools/list, and is then stopped. Identical definitions in
several files are started once. A handshake passes only when the initialize result has the fields
the MCP schema requires and a protocol version this client supports, and every tools/list page, followed
through nextCursor, is a valid list of tools. One line per server:

    CONFIG_ERROR <file> <reason>        the file could not be read or parsed; its servers were not checked
    CONFIG_WARNING <file> <reason>
    HANDSHAKE_OK <name> source=<files> protocol=<version> tools=<count|none>
    HANDSHAKE_FAILED <name> source=<files> <reason>
    SKIPPED <name> source=<files> transport=<transport>
    NO_SERVERS                          every config was readable and none declares a server
    FAILED <reason>                     last line: the root is not a Git repository, or no server has the
                                        name --server gives

Exit 0 when every config was readable and no started server failed; 1 when a config could not be read, any
server failed, or the run printed FAILED; 2 for a usage error.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import tomllib
from typing import Any

from audit_ai_config import _parse_mcp_json

PROTOCOL_VERSION = "2025-06-18"
# Revisions whose initialize and tools/list shapes this client validates; a server may answer with any of them.
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2024-11-05", "2025-03-26", PROTOCOL_VERSION})
MAX_TOOL_PAGES = 100
JSON_SOURCES = ((".mcp.json", "mcpServers"), (".github/mcp.json", "mcpServers"), (".vscode/mcp.json", "servers"))


Problem = tuple[str, str, str]  # (ERROR or WARNING, source, message)


def declared_servers(root: Path) -> tuple[list[tuple[str, str, dict[str, Any]]], list[Problem]]:
    """Return (name, source, definition) for every server definition, in file order, and each config problem.

    A config that cannot be read is a problem to report, never an empty server list: otherwise a malformed
    file would read as "no servers" and the handshake would pass.
    """
    found: list[tuple[str, str, dict[str, Any]]] = []
    problems: list[Problem] = []
    for rel, wrapper in JSON_SOURCES:
        servers, findings = _parse_mcp_json(root / rel, rel, wrapper)
        problems.extend((finding.severity, rel, finding.message) for finding in findings)
        found.extend((name, rel, entry) for name, entry in servers.items() if isinstance(entry, dict))
    codex = root / ".codex/config.toml"
    if codex.is_file():
        try:
            with codex.open("rb") as handle:
                table = tomllib.load(handle).get("mcp_servers", {})
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
            problems.append(("ERROR", ".codex/config.toml", f"Could not read or parse: {error}"))
            table = {}
        if isinstance(table, dict):
            found.extend((name, ".codex/config.toml", entry) for name, entry in table.items() if isinstance(entry, dict))
        else:
            problems.append(("ERROR", ".codex/config.toml", "mcp_servers must be a table"))
    return found, problems


def transport(entry: dict[str, Any]) -> str:
    declared = entry.get("type")
    if isinstance(declared, str) and declared.strip():
        return declared.strip().lower()
    if isinstance(entry.get("command"), str) and entry["command"].strip():
        return "stdio"
    return "http" if isinstance(entry.get("url"), str) else "unknown"


def connection(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: entry[key] for key in ("command", "args", "cwd", "env", "url", "type") if key in entry}


def group_servers(root: Path, only: str | None) -> tuple[list[tuple[str, list[str], dict[str, Any]]], list[Problem]]:
    groups: dict[tuple[str, str], tuple[str, list[str], dict[str, Any]]] = {}
    servers, problems = declared_servers(root)
    for name, source, entry in servers:
        if only is not None and name != only:
            continue
        key = (name, json.dumps(connection(entry), sort_keys=True))
        if key in groups:
            groups[key][1].append(source)
        else:
            groups[key] = (name, [source], entry)
    return list(groups.values()), problems


class Session:
    """A started server whose stdout lines arrive on a queue, so reads can time out."""

    def __init__(self, arguments: list[str], cwd: Path, env: dict[str, str]) -> None:
        self.process = subprocess.Popen(
            arguments, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
        )
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.stderr_tail: list[str] = []
        self.pumps = [
            threading.Thread(target=self._pump_stdout, daemon=True),
            threading.Thread(target=self._pump_stderr, daemon=True),
        ]
        for pump in self.pumps:
            pump.start()

    def _pump_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _pump_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.stderr_tail = (self.stderr_tail + [line.strip()])[-3:]

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def response(self, request_id: int, deadline: float) -> dict[str, Any]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("timed out waiting for a response")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                # Stdout can close before the server's last stderr line is read; exited() waits for it.
                raise RuntimeError(self.exited() or "server exited before responding")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"server returned an error: {json.dumps(message['error'])}")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("response has no result object")
                return result

    def exited(self) -> str | None:
        """'server exited before responding[: <last stderr line>]' once the server has exited, else None.

        A server that exits at once can be gone before the first request is written, so the write fails with a
        broken pipe; that is the same failure as exiting before a response, and its stderr says why.
        """
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return None
        self.pumps[1].join(timeout=2)
        detail = f": {self.stderr_tail[-1]}" if self.stderr_tail else ""
        return f"server exited before responding{detail}"

    def close(self) -> None:
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        for pump in self.pumps:
            pump.join(timeout=2)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


def initialize_result(result: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The protocol version and capabilities of a valid InitializeResult, or RuntimeError."""
    version = result.get("protocolVersion")
    if not isinstance(version, str):
        raise RuntimeError("initialize result has no protocolVersion")
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise RuntimeError(f"server chose unsupported protocol version {json.dumps(version)}")
    capabilities = result.get("capabilities")
    if not isinstance(capabilities, dict):
        raise RuntimeError("initialize result has no capabilities object")
    if "tools" in capabilities and not isinstance(capabilities["tools"], dict):
        raise RuntimeError("initialize result capabilities.tools is not an object")
    info = result.get("serverInfo")
    if not isinstance(info, dict) or not all(isinstance(info.get(key), str) for key in ("name", "version")):
        raise RuntimeError("initialize result has no serverInfo name and version")
    if "instructions" in result and not isinstance(result["instructions"], str):
        raise RuntimeError("initialize result instructions is not a string")
    return version, capabilities


def tool_count(session: Session, deadline: float) -> int:
    """Count the tools across every tools/list page, or raise RuntimeError for an invalid result."""
    count = 0
    cursor: str | None = None
    seen: set[str] = set()
    for request_id in range(2, 2 + MAX_TOOL_PAGES):
        request: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": "tools/list"}
        if cursor is not None:
            request["params"] = {"cursor": cursor}
        session.send(request)
        result = session.response(request_id, deadline)
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise RuntimeError("tools/list result has no tools array")
        for tool in tools:
            if (
                not isinstance(tool, dict)
                or not isinstance(tool.get("name"), str)
                or not isinstance(tool.get("inputSchema"), dict)
                or tool["inputSchema"].get("type") != "object"
            ):
                raise RuntimeError("tools/list returned a tool without a name and object inputSchema")
        count += len(tools)
        cursor = result.get("nextCursor")
        if cursor is None:
            return count
        if not isinstance(cursor, str) or cursor in seen:
            raise RuntimeError("tools/list returned an invalid or repeated nextCursor")
        seen.add(cursor)
    raise RuntimeError(f"tools/list did not finish within {MAX_TOOL_PAGES} pages")


def handshake(root: Path, entry: dict[str, Any], timeout: float) -> str:
    """Return 'protocol=<v> tools=<n>' or raise RuntimeError with the failure reason."""
    command = shutil.which(entry["command"])
    if command is None:
        raise RuntimeError(f"command not found: {entry['command']}")
    args = entry.get("args") or []
    env_values = entry.get("env") or {}
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise RuntimeError("args must be a list of strings")
    if not isinstance(env_values, dict) or not all(isinstance(v, str) for v in env_values.values()):
        raise RuntimeError("env must map names to strings")
    cwd = root / entry["cwd"] if isinstance(entry.get("cwd"), str) else root
    try:
        session = Session([command, *args], cwd, {**os.environ, **env_values})
    except OSError as error:
        raise RuntimeError(f"could not start: {error}") from error
    deadline = time.monotonic() + timeout
    try:
        session.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "audit-ai-config", "version": "1"},
        }})
        version, capabilities = initialize_result(session.response(1, deadline))
        session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = str(tool_count(session, deadline)) if "tools" in capabilities else "none"
        return f"protocol={version} tools={tools}"
    except OSError as error:
        raise RuntimeError(session.exited() or f"could not talk to the server: {error}") from error
    finally:
        session.close()


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--server", help="handshake only the server with this name")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds per server (default 30)")
    args = parser.parse_args(arguments)
    if not (args.root / ".git").exists():
        print(f"FAILED {args.root} is not a Git repository")
        return 1
    groups, problems = group_servers(args.root.resolve(), args.server)
    for severity, source, message in problems:
        print(f"CONFIG_{severity} {source} {message}")
    failed = any(severity == "ERROR" for severity, _, _ in problems)
    if not groups:
        if args.server is not None:
            print(f"FAILED no MCP server named {args.server}")
            return 1
        if not failed:
            print("NO_SERVERS")
        return 1 if failed else 0
    for name, sources, entry in groups:
        label = f"{name} source={','.join(sources)}"
        kind = transport(entry)
        if kind not in {"stdio", "local"} or not isinstance(entry.get("command"), str):
            print(f"SKIPPED {label} transport={kind}")
            continue
        try:
            print(f"HANDSHAKE_OK {label} {handshake(args.root.resolve(), entry, args.timeout)}")
        except RuntimeError as error:
            failed = True
            print(f"HANDSHAKE_FAILED {label} {error}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
