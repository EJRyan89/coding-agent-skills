"""The GitHub CLI client every skill script that runs gh shares.

One runner, one failure classifier, and one bounded backoff policy, so a failure means the same thing, and a rate
limit is waited out the same way, in every skill. Tests inject the runner, the sleeper, and the clock.

GitHub output is untrusted bytes: a diff carries files in any encoding, and a title can hold anything. The runner
decodes with surrogateescape, which keeps each byte that is not UTF-8 as a lone surrogate, and the client replaces
each with U+FFFD before a caller sees it, counting them in `CommandResult.replaced`.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Protocol, TypeVar

T = TypeVar("T")

# Checked in this order; the first class whose marker the lowered stderr contains wins, and `api` is the rest.
RATE_LIMIT_MARKERS = ("rate limit", "http 429")
AUTHENTICATION_MARKERS = ("http 401", "authentication", "not logged", "gh auth login")
SEARCH_CAP_MARKERS = ("only the first 1000 search results",)
FORBIDDEN_MARKERS = ("http 403",)
NOT_FOUND_MARKERS = ("http 404", "not found")
NETWORK_MARKERS = (
    "error connecting to",
    "check your internet connection",
    "no such host",
    "could not resolve host",
    "connection reset",
    "connection refused",
    "i/o timeout",
    "tls handshake timeout",
    "network is unreachable",
)
GRAPHQL_KINDS = {"RATE_LIMITED": "rate_limit", "NOT_FOUND": "not_found", "FORBIDDEN": "forbidden"}
RETRYABLE_KINDS = frozenset({"rate_limit"})
MISSING_CLI = "GitHub CLI executable 'gh' was not found; install GitHub CLI and authenticate first"


class GitHubError(RuntimeError):
    """A gh failure, or a response a caller rejected, with its class in `kind`.

    `retryable` marks a failure the client waits out with its backoff policy: a rate limit, or whatever a caller's
    parse flags, such as a search that timed out.
    """

    def __init__(
        self, message: str, *, kind: str = "api", retryable: bool = False, returncode: int | None = None
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.returncode = returncode  # gh's exit code, when gh ran and failed


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    replaced: int = 0  # bytes of stdout that were not UTF-8 and became U+FFFD


Runner = Callable[[Sequence[str]], CommandResult]
Downloader = Callable[[Sequence[str], Path], CommandResult]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]


class Pacer(Protocol):
    """Spaces a caller's requests: `before` runs ahead of each attempt and `after` with its outcome."""

    def before(self) -> None: ...

    def after(self, error: GitHubError | None) -> None: ...


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


def subprocess_runner(arguments: Sequence[str]) -> CommandResult:
    """Run a command; output that is not UTF-8 is kept losslessly, one lone surrogate per undecodable byte."""
    try:
        process = subprocess.run(list(arguments), capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise GitHubError(MISSING_CLI, kind="prerequisite") from exc
    except OSError as exc:
        raise GitHubError(f"GitHub CLI could not be started: {exc}", kind="execution") from exc
    return CommandResult(process.returncode, _decode(process.stdout), _decode(process.stderr))


def subprocess_downloader(arguments: Sequence[str], target: Path) -> CommandResult:
    """Run a command with its stdout written to `target` byte for byte; the result's stdout is empty."""
    try:
        with target.open("wb") as handle:
            process = subprocess.run(list(arguments), stdout=handle, stderr=subprocess.PIPE, check=False)
    except FileNotFoundError as exc:
        raise GitHubError(MISSING_CLI, kind="prerequisite") from exc
    except OSError as exc:
        raise GitHubError(f"GitHub CLI could not be started: {exc}", kind="execution") from exc
    return CommandResult(process.returncode, "", _decode(process.stderr))


# surrogateescape decodes each byte that is not UTF-8 to one of these, and never yields one otherwise.
_UNDECODABLE = re.compile("[\udc80-\udcff]")


def replace_undecodable(text: str) -> tuple[str, int]:
    """`text` with each undecodable byte the runner kept as U+FFFD, and how many bytes were replaced."""
    return _UNDECODABLE.subn("\ufffd", text)


def classify_failure(stderr: str, status: int | None = None, headers: Mapping[str, str] | None = None) -> str:
    """The class of a failed gh command, from its stderr and, for `gh api -i`, the response status and headers."""
    lowered = stderr.casefold()
    headers = headers or {}
    if status == 429 or (status == 403 and headers.get("x-ratelimit-remaining") == "0"):
        return "rate_limit"
    for kind, markers in (
        ("rate_limit", RATE_LIMIT_MARKERS),
        ("authentication", AUTHENTICATION_MARKERS),
        ("search_cap", SEARCH_CAP_MARKERS),
        ("forbidden", FORBIDDEN_MARKERS),
        ("not_found", NOT_FOUND_MARKERS),
        ("network", NETWORK_MARKERS),
    ):
        if any(marker in lowered for marker in markers):
            return kind
    return "api"


def graphql_failure(payload: Any) -> GitHubError | None:
    """The error a GraphQL response body reports, classified by its error types, or None when it reports none."""
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not errors:
        return None
    errors = errors if isinstance(errors, list) else [errors]
    types = sorted({str(error.get("type")) for error in errors if isinstance(error, dict) and error.get("type")})
    messages = "; ".join(
        str(error.get("message", error)) if isinstance(error, dict) else str(error) for error in errors
    )
    kind = next((GRAPHQL_KINDS[name] for name in GRAPHQL_KINDS if name in types), "api")
    return GitHubError(
        f"GraphQL request failed ({', '.join(types) or 'unknown'}): {messages}",
        kind=kind,
        retryable=kind in RETRYABLE_KINDS,
    )


class Response(NamedTuple):
    status: int | None
    headers: dict[str, str]
    body: str


def split_response(stdout: str) -> Response:
    """Separate the status line and headers `gh api -i` prints from the body; other output is all body."""
    if not stdout.startswith("HTTP/"):
        return Response(None, {}, stdout)
    parts = re.split(r"\r?\n\r?\n", stdout, maxsplit=1)
    head, body = parts[0], parts[1] if len(parts) > 1 else ""
    lines = head.splitlines()
    status_parts = lines[0].split()
    status = int(status_parts[1]) if len(status_parts) > 1 and status_parts[1].isdigit() else None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().casefold()] = value.strip()
    return Response(status, headers, body)


@dataclass(frozen=True)
class Backoff:
    """How long to wait before retrying a rate limit, and how often."""

    base: float = 5.0
    retries: int = 5
    longest: float = 300.0

    def wait(self, attempt: int, headers: Mapping[str, str], now: float) -> float:
        """GitHub's Retry-After, else the quota's reset time when it is spent, else exponential from `base`.

        A wait over `longest` fails as a rate limit instead, so no command sleeps for long.
        """
        retry_after = headers.get("retry-after", "")
        if retry_after.isdigit():
            wait = float(retry_after)
        elif headers.get("x-ratelimit-remaining") == "0" and headers.get("x-ratelimit-reset", "").isdigit():
            wait = max(1.0, float(headers["x-ratelimit-reset"]) - now + 1.0)
        else:
            wait = self.base * 2**attempt
        if wait > self.longest:
            raise GitHubError(f"GitHub asked to wait {wait:.0f}s before retrying; rerun later", kind="rate_limit")
        return wait


BACKOFF = Backoff()


def _identity(result: CommandResult) -> CommandResult:
    return result


class GitHubClient:
    """Runs gh, classifies its failures, and waits out rate limits with one bounded backoff policy."""

    def __init__(
        self,
        runner: Runner = subprocess_runner,
        *,
        sleeper: Sleeper | None = None,
        clock: Clock | None = None,
        backoff: Backoff = BACKOFF,
        downloader: Downloader = subprocess_downloader,
    ) -> None:
        self.runner = runner
        # Looked up per client, so a test that patches time.sleep reaches a client built deep inside a call.
        self.sleeper = sleeper or time.sleep
        self.clock = clock or time.time
        self.backoff = backoff
        self.downloader = downloader

    def request(
        self,
        arguments: Sequence[str],
        parse: Callable[[CommandResult], T],
        *,
        retry: bool = True,
        headers: bool = False,
        pacer: Pacer | None = None,
    ) -> T:
        """Run `gh <arguments>` and return what `parse` makes of a successful result.

        A failure raises GitHubError with its class. A retryable one, a rate limit or what `parse` flags, is retried
        after the policy's wait, up to its retries, unless `retry` is False. With `headers`, `gh api -i` is run, and
        its status and headers are split from the body, read for rate limits and SSO, and used for the wait.
        """
        return self._attempts(self.runner, arguments, parse, retry=retry, headers=headers, pacer=pacer)

    def run(
        self, arguments: Sequence[str], *, retry: bool = True, headers: bool = False, pacer: Pacer | None = None
    ) -> CommandResult:
        """Run `gh <arguments>` and return the successful result; see `request`."""
        return self.request(arguments, _identity, retry=retry, headers=headers, pacer=pacer)

    def json(self, arguments: Sequence[str], *, retry: bool = True, graphql: bool = False) -> Any:
        """Run `gh <arguments>` and return its JSON output; with `graphql`, errors in the body fail too."""

        def parse(result: CommandResult) -> Any:
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise GitHubError(f"GitHub returned malformed JSON: {exc}", kind="malformed") from exc
            problem = graphql_failure(payload) if graphql else None
            if problem is not None:
                raise problem
            return payload

        return self.request(arguments, parse, retry=retry)

    def download(self, arguments: Sequence[str], target: Path, *, retry: bool = True) -> None:
        """Run `gh <arguments>` with its output written to `target`, which a retry overwrites."""

        def write(command: Sequence[str]) -> CommandResult:
            return self.downloader(command, target)

        self._attempts(write, arguments, _identity, retry=retry, headers=False, pacer=None)

    def _attempts(
        self,
        execute: Runner,
        arguments: Sequence[str],
        parse: Callable[[CommandResult], T],
        *,
        retry: bool,
        headers: bool,
        pacer: Pacer | None,
    ) -> T:
        if headers:
            if not arguments or arguments[0] != "api":
                raise ValueError("headers are only available from gh api")
            command = ["gh", "api", "-i", *arguments[1:]]
        else:
            command = ["gh", *arguments]
        attempts = self.backoff.retries + 1 if retry else 1
        for attempt in range(attempts):
            if pacer is not None:
                pacer.before()
            result, response = self._decoded(execute(command), headers)
            error: GitHubError | None = None
            try:
                value = self._judge(command, result, response, parse)
            except GitHubError as exc:
                error = exc
            if pacer is not None:
                pacer.after(error)
            if error is None:
                return value
            if not (retry and error.retryable):
                raise error
            if attempt == attempts - 1:
                raise GitHubError(
                    f"{error} (gave up after {self.backoff.retries} retries)",
                    kind=error.kind,
                    retryable=True,
                    returncode=error.returncode,
                )
            self.sleeper(self.backoff.wait(attempt, response.headers, self.clock()))
        raise AssertionError("unreachable")

    @staticmethod
    def _decoded(raw: CommandResult, headers: bool) -> tuple[CommandResult, Response]:
        stdout, replaced = replace_undecodable(raw.stdout)
        stderr = replace_undecodable(raw.stderr)[0]
        response = split_response(stdout) if headers else Response(None, {}, stdout)
        return CommandResult(raw.returncode, response.body, stderr, replaced), response

    @staticmethod
    def _judge(
        command: Sequence[str], result: CommandResult, response: Response, parse: Callable[[CommandResult], T]
    ) -> T:
        sso = response.headers.get("x-github-sso", "")
        if "partial-results" in sso.casefold():
            raise GitHubError(
                "GitHub omitted results from organizations this token is not SSO-authorized for "
                f"({sso}); authorize the token for SSO and rerun",
                kind="sso_partial",
            )
        if result.returncode != 0:
            kind = classify_failure(result.stderr, response.status, response.headers)
            message = result.stderr.strip() or f"gh {command[1]} failed with exit code {result.returncode}"
            raise GitHubError(message, kind=kind, retryable=kind in RETRYABLE_KINDS, returncode=result.returncode)
        return parse(result)
