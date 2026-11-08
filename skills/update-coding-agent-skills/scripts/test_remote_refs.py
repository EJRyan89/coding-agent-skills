"""Regression tests for remote_refs.py, the update's tag check and fetch against origin: no prompt and no askpass
helper, a time limit, and a fetch that leaves out a tag shadowing a branch."""

from __future__ import annotations

import http.server
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from remote_refs import ORIGIN_MAIN, SHADOWING_TAGS, main

COMMANDS = ("shadows", "fetch")


def git(*arguments: str | Path) -> str:
    return subprocess.run(
        ["git", *(str(argument) for argument in arguments)], capture_output=True, encoding="utf-8", check=True
    ).stdout


def run(capsys: pytest.CaptureFixture[str], *arguments: str | Path) -> tuple[int, str]:
    code = main([str(argument) for argument in arguments])
    return code, capsys.readouterr().out


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clone of a bare origin whose main holds one commit."""
    seed = tmp_path / "seed folder"
    git("init", "-q", "-b", "main", seed)
    git(
        "-C",
        seed,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "first",
    )
    origin = tmp_path / "origin.git"
    git("clone", "-q", "--bare", seed, origin)
    cloned = tmp_path / "clone folder"
    git("clone", "-q", origin, cloned)
    return cloned


class Unauthorized(http.server.BaseHTTPRequestHandler):
    """Answers every request with 401 and a Basic challenge, so git asks for a credential."""

    def do_GET(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="test"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the base class's parameter name
        pass


@pytest.fixture
def asking_origin(clone: Path) -> Iterator[str]:
    """An HTTP origin that wants a credential, set as the clone's origin; yields its URL."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Unauthorized)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/repository.git"
    git("-C", clone, "remote", "set-url", "origin", url)
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def askpass_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """GIT_ASKPASS, SSH_ASKPASS, and core.askPass each naming a helper that records its start and answers; returns the
    record, which exists once any helper ran."""
    record = tmp_path / "helper ran"
    helper = tmp_path / "askpass helper.sh"
    helper.write_text(
        f"#!/bin/sh\necho started >> '{record.as_posix()}'\necho answer\n", encoding="utf-8", newline="\n"
    )
    helper.chmod(0o755)
    monkeypatch.setenv("GIT_ASKPASS", str(helper))
    monkeypatch.setenv("SSH_ASKPASS", str(helper))
    monkeypatch.setenv("SSH_ASKPASS_REQUIRE", "force")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.askPass")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(helper))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    return record


def test_the_helpers_run_for_a_git_started_without_the_client(
    clone: Path, asking_origin: str, askpass_helpers: Path
) -> None:
    # The control: the origin and the helpers do make a plain git start a helper, so the cases below show the client
    # stopping it rather than a fixture that never asks.
    subprocess.run(["git", "-C", str(clone), "ls-remote", "origin"], capture_output=True, timeout=60, check=False)
    assert askpass_helpers.exists()


@pytest.mark.parametrize("command", COMMANDS)
def test_a_remote_that_wants_a_credential_fails_at_once_with_no_helper_started(
    capsys: pytest.CaptureFixture[str], clone: Path, asking_origin: str, askpass_helpers: Path, command: str
) -> None:
    code, output = run(capsys, command, clone, "--seconds", "60")
    assert code == 1
    assert output.startswith("FAILED "), output
    assert output.count("\n") == 1, output
    assert not askpass_helpers.exists(), "an askpass helper ran"


@pytest.mark.parametrize("command", COMMANDS)
def test_a_remote_that_never_answers_ends_within_the_time_limit(
    capsys: pytest.CaptureFixture[str], clone: Path, command: str
) -> None:
    # A local origin's upload-pack runs through the shell, so a configured one that sleeps first stands for a remote
    # that never answers.
    git("-C", clone, "config", "remote.origin.uploadpack", "sleep 120; git-upload-pack")
    started = time.monotonic()
    code, output = run(capsys, command, clone, "--seconds", "2")
    assert time.monotonic() - started < 60
    assert code == 1
    assert output.startswith("FAILED "), output
    assert "did not finish within 2 seconds" in output, output


def test_shadows_names_each_shadowing_tag_on_origin(capsys: pytest.CaptureFixture[str], clone: Path) -> None:
    origin = git("-C", clone, "remote", "get-url", "origin").strip()
    assert run(capsys, "shadows", clone) == (0, "CHECKED\n")
    git("-C", origin, "tag", "main", "main")
    git("-C", origin, "tag", "origin/main", "main")
    git("-C", origin, "tag", "mainline", "main")
    assert run(capsys, "shadows", clone) == (0, "ON_ORIGIN refs/tags/main\nON_ORIGIN refs/tags/origin/main\nCHECKED\n")


def test_fetch_follows_origin_main_with_its_tags_but_never_a_shadowing_one(
    capsys: pytest.CaptureFixture[str], clone: Path, tmp_path: Path
) -> None:
    seed = tmp_path / "seed folder"
    git(
        "-C",
        seed,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "second",
    )
    origin = git("-C", clone, "remote", "get-url", "origin").strip()
    git(
        "-C",
        seed,
        "push",
        "-q",
        origin,
        "main",
        "main:refs/tags/v1.0.0",
        "main:refs/tags/main",
        "main:refs/tags/origin/main",
    )
    assert run(capsys, "fetch", clone) == (0, "FETCHED\n")
    assert git("-C", clone, "rev-parse", ORIGIN_MAIN) == git("-C", seed, "rev-parse", "main")
    tags = git("-C", clone, "for-each-ref", "--format=%(refname)", "refs/tags").split()
    assert tags == ["refs/tags/v1.0.0"]
    assert not set(tags) & set(SHADOWING_TAGS)


def test_a_missing_origin_is_a_failed_line(capsys: pytest.CaptureFixture[str], clone: Path, tmp_path: Path) -> None:
    git("-C", clone, "remote", "set-url", "origin", tmp_path / "missing.git")
    for command in COMMANDS:
        code, output = run(capsys, command, clone)
        assert code == 1
        assert output.startswith("FAILED "), output
        assert output.count("\n") == 1, output


@pytest.mark.parametrize("seconds", ["0", "-1", "nan", "inf", "soon"])
def test_a_limit_that_is_not_a_positive_number_of_seconds_is_a_usage_error(clone: Path, seconds: str) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["fetch", str(clone), "--seconds", seconds])
    assert raised.value.code == 2
