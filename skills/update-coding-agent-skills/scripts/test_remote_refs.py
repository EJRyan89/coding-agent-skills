"""Regression tests for remote_refs.py, the update's tag check and fetch against origin: no prompt and no askpass
helper, a time limit, and a fetch that leaves out a tag shadowing a branch."""

from __future__ import annotations

import http.server
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from git_client import GitClient, GitError, GitErrorKind
from remote_refs import main

COMMANDS = ("shadows", "fetch")
SCRIPT = Path(__file__).resolve().parent / "remote_refs.py"


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
    assert output.startswith("ORIGIN_FAILED "), output
    assert output.count("\n") == 1, output
    assert not askpass_helpers.exists(), "an askpass helper ran"


@pytest.mark.parametrize("command", COMMANDS)
def test_a_remote_that_never_answers_ends_within_the_time_limit(
    capsys: pytest.CaptureFixture[str], clone: Path, command: str
) -> None:
    # A local origin's upload-pack runs through the shell, so a configured one that sleeps first stands for a remote
    # that does not answer within the limit. It answers after the sleep, so a call given no limit, or the client's
    # default, succeeds instead of failing.
    git("-C", clone, "config", "remote.origin.uploadpack", "sleep 15; git-upload-pack")
    started = time.monotonic()
    code, output = run(capsys, command, clone, "--seconds", "2")
    assert time.monotonic() - started < 60
    assert code == 1
    assert output.startswith("ORIGIN_FAILED "), output
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
    assert git("-C", clone, "rev-parse", "refs/remotes/origin/main") == git("-C", seed, "rev-parse", "main")
    tags = git("-C", clone, "for-each-ref", "--format=%(refname)", "refs/tags").split()
    assert tags == ["refs/tags/v1.0.0"]


def test_a_missing_origin_is_a_failed_line(capsys: pytest.CaptureFixture[str], clone: Path, tmp_path: Path) -> None:
    git("-C", clone, "remote", "set-url", "origin", tmp_path / "missing.git")
    for command in COMMANDS:
        code, output = run(capsys, command, clone)
        assert code == 1
        assert output.startswith("ORIGIN_FAILED "), output
        assert output.count("\n") == 1, output


def test_the_script_finds_the_core_deployed_beside_it(clone: Path) -> None:
    # Run isolated, so nothing but the script's own path statement puts ejryan89-skill-core where it imports from.
    finished = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "shadows", str(clone)],
        capture_output=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert (finished.returncode, finished.stdout) == (0, "CHECKED\n"), finished.stderr


def test_a_command_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main([])
    assert raised.value.code == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("seconds", ["0", "-1", "nan", "inf", "soon"])
def test_a_limit_that_is_not_a_positive_number_of_seconds_is_a_usage_error(clone: Path, seconds: str) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["fetch", str(clone), "--seconds", seconds])
    assert raised.value.code == 2


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("kind", ["prerequisite", "execution", "not_repository"])
def test_a_git_that_could_not_run_is_a_failed_line_never_origins_failure(
    capsys: pytest.CaptureFixture[str], clone: Path, command: str, kind: GitErrorKind
) -> None:
    # A git that is missing, could not start, or found no repository said nothing of origin, so update.sh must not
    # report it as FETCH_FAILED, which tells the user to sign in to origin.
    with mock.patch.object(GitClient, "output", side_effect=GitError("git could not run", kind=kind)):
        assert run(capsys, command, clone) == (1, "FAILED git could not run\n")


def test_a_fetch_that_fails_in_the_clone_is_a_failed_line_never_origins_failure(
    capsys: pytest.CaptureFixture[str], clone: Path, tmp_path: Path
) -> None:
    # Origin answered, but the clone could not take what it sent: a stale lock on the ref the fetch writes. That says
    # nothing of origin, so update.sh must not report it as FETCH_FAILED, which sends the user to sign in to origin.
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
    git("-C", seed, "push", "-q", git("-C", clone, "remote", "get-url", "origin").strip(), "main")
    lock = clone / ".git" / "refs" / "remotes" / "origin" / "main.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    code, output = run(capsys, "fetch", clone)
    assert code == 1
    assert output.startswith("FAILED error: cannot lock ref 'refs/remotes/origin/main'"), output
    assert output.count("\n") == 1, output


@pytest.mark.parametrize("command", COMMANDS)
def test_a_failure_git_names_in_the_clone_is_a_failed_line_and_any_other_failure_is_origins(
    capsys: pytest.CaptureFixture[str], clone: Path, command: str
) -> None:
    local = GitError("error: insufficient permission for adding an object to repository database .git/objects")
    with mock.patch.object(GitClient, "output", side_effect=local):
        assert run(capsys, command, clone)[1].startswith("FAILED error: insufficient permission"), command
    remote = GitError("remote: error: cannot lock ref 'refs/heads/main'\nfatal: the remote end hung up unexpectedly")
    with mock.patch.object(GitClient, "output", side_effect=remote):
        assert run(capsys, command, clone)[1].startswith("ORIGIN_FAILED remote: error: cannot lock ref"), command


def unwritable_fetch_head(clone: Path) -> Path:
    fetch_head = clone / ".git" / "FETCH_HEAD"
    fetch_head.touch()
    return fetch_head


def unwritable_reflog(clone: Path) -> Path:
    git("-C", clone, "config", "core.logAllRefUpdates", "true")
    reflog = clone / ".git" / "logs" / "refs" / "remotes" / "origin" / "main"
    reflog.parent.mkdir(parents=True, exist_ok=True)
    reflog.touch()
    return reflog


@pytest.mark.parametrize("unwritable", [unwritable_fetch_head, unwritable_reflog], ids=["FETCH_HEAD", "reflog"])
def test_a_fetch_that_cannot_write_a_file_of_the_clone_is_a_failed_line_whatever_git_calls_it(
    capsys: pytest.CaptureFixture[str], clone: Path, tmp_path: Path, unwritable: Callable[[Path], Path]
) -> None:
    # Each names its own wording, neither a lock nor an object, so only the path inside .git tells it from origin's.
    seed = tmp_path / "seed folder"
    git("-C", seed, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", "next", "--allow-empty")
    git("-C", seed, "push", "-q", git("-C", clone, "remote", "get-url", "origin").strip(), "main")
    target = unwritable(clone)
    target.chmod(stat.S_IREAD)
    try:
        code, output = run(capsys, "fetch", clone)
    finally:
        target.chmod(stat.S_IREAD | stat.S_IWRITE)
    assert code == 1
    assert output.startswith("FAILED error: "), output
    assert "Permission denied" in output, output


def test_a_remote_that_prints_a_local_looking_failure_of_its_own_is_origins(
    capsys: pytest.CaptureFixture[str], clone: Path
) -> None:
    # A local origin's upload-pack runs through the shell, and its stderr reaches the fetch's with no remote: before
    # it, as a remote's over ssh does; git's own verdict after it keeps the failure origin's.
    git("-C", clone, "config", "remote.origin.uploadpack", "echo 'fatal: unable to write .git/FETCH_HEAD' >&2; exit 1;")
    code, output = run(capsys, "fetch", clone)
    assert code == 1
    assert output.startswith("ORIGIN_FAILED fatal: unable to write .git/FETCH_HEAD"), output
