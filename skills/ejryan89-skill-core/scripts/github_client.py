"""The GitHub CLI client every skill script that runs gh shares.

One runner, one failure classifier, and one bounded backoff policy, so a failure means the same thing, and a rate
limit is waited out the same way, in every skill. Tests inject the runner, the sleeper, and the clock.

A transient failure, a gateway briefly down or a connection dropped mid-request, is retried a few times with short
waits on a read alone. A request that may change something on GitHub is never retried after one, because GitHub may
have applied it before the answer was lost: it fails as an `unknown_outcome` that tells the user to check first.

GitHub output is untrusted bytes: a diff carries files in any encoding, and a title can hold anything. The runner
decodes with surrogateescape, which keeps each byte that is not UTF-8 as a lone surrogate, and the client replaces
each with U+FFFD before a caller sees it, counting them in `CommandResult.replaced`.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import IO, Any, Literal, NamedTuple, Protocol, TypeVar

import json_reader
from bounded_process import Finished, run_bounded
from executable_locator import located_command
from flat_text import flat_text

T = TypeVar("T")

# Each class of failure a GitHubError names. The client's own: gh missing (`prerequisite`), unable to start
# (`execution`), or out of time (`timeout`); a class the markers below name; a response it could not parse
# (`malformed`); a read single sign-on cut short (`sso_partial`); a change whose answer was lost (`unknown_outcome`);
# and `api` for the rest. Then the classes callers raise for an answer they reject: a path that is not one file
# (`not_file`), an argument of the wrong kind (`input`), a pull request a review cannot take (`ineligible`), results
# GitHub left incomplete (`incomplete`), a run a tool refuses to read (`refused`), and a diff GitHub's diff endpoint
# refuses as over one of its limits (`diff_limit`).
GitHubErrorKind = Literal[
    "api",
    "prerequisite",
    "execution",
    "timeout",
    "rate_limit",
    "authentication",
    "search_cap",
    "forbidden",
    "not_found",
    "network",
    "malformed",
    "sso_partial",
    "unknown_outcome",
    "not_file",
    "input",
    "ineligible",
    "incomplete",
    "refused",
    "diff_limit",
]

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
FAILURE_MARKERS: tuple[tuple[GitHubErrorKind, tuple[str, ...]], ...] = (
    ("rate_limit", RATE_LIMIT_MARKERS),
    ("authentication", AUTHENTICATION_MARKERS),
    ("search_cap", SEARCH_CAP_MARKERS),
    ("forbidden", FORBIDDEN_MARKERS),
    ("not_found", NOT_FOUND_MARKERS),
    ("network", NETWORK_MARKERS),
)
# Statuses and stderr markers of a failure that may clear on its own within seconds: a gateway or service briefly
# down, or a connection that broke after it was made. gh prints a status as `(HTTP 502)` or `HTTP 502:`. A DNS
# failure (gh's `error connecting to`), a refused connection, and an unreachable network are left out: each means
# this machine cannot reach GitHub at all, which a few seconds' wait does not cure.
TRANSIENT_STATUSES = frozenset({502, 503, 504})
TRANSIENT_MARKERS = (
    "http 502",
    "http 503",
    "http 504",
    "connection reset",
    "i/o timeout",
    "tls handshake timeout",
    "unexpected eof",
    "server sent goaway",
)
# Only these classes can be transient: a rate limit has its own backoff, and every other class is GitHub's answer.
TRANSIENT_KINDS: frozenset[GitHubErrorKind] = frozenset({"api", "network"})
# `gh <noun> <verb>` commands that only read, besides `gh search`; any other command counts as a change.
READ_VERBS = frozenset({"view", "list", "status", "diff", "checks"})
# The `gh api` options that take a value, so a value that looks like an option is never read as one. gh's output
# filters are left out, since no skill script may use them; a filter's value taken for the endpoint can only make a
# GraphQL read count as a change.
API_VALUE_OPTIONS = frozenset(
    {
        "-H",
        "--header",
        "-f",
        "--raw-field",
        "-F",
        "--field",
        "-X",
        "--method",
        "--input",
        "--hostname",
        "-p",
        "--preview",
        "--cache",
    }
)
# The `gh api` options that give a request a body, which gh sends as a POST unless a method is named.
API_BODY_OPTIONS = frozenset({"-f", "--raw-field", "-F", "--field", "--input"})
READ_METHODS = frozenset({"GET", "HEAD"})
# Any mention of `mutation` makes a GraphQL document a change; a field so named costs only its retries.
GRAPHQL_MUTATION = re.compile(r"\bmutation\b")
UNKNOWN_OUTCOME = "GitHub may have applied it, so check before repeating it"
GRAPHQL_KINDS: dict[str, GitHubErrorKind] = {
    "RATE_LIMITED": "rate_limit",
    "NOT_FOUND": "not_found",
    "FORBIDDEN": "forbidden",
}
RETRYABLE_KINDS: frozenset[GitHubErrorKind] = frozenset({"rate_limit"})
DEFAULT_TIMEOUT_SECONDS = 300.0
MISSING_CLI = "GitHub CLI executable 'gh' was not found; install GitHub CLI and authenticate first"


class GitHubError(RuntimeError):
    """A gh failure, or a response a caller rejected, with its class in `kind`.

    `retryable` marks a failure the client waits out with its backoff policy: a rate limit, or whatever a caller's
    parse flags, such as a search that timed out. `transient` marks a gh failure that may clear on its own, which the
    client retries on a read and reports as an `unknown_outcome` on a change.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: GitHubErrorKind = "api",
        retryable: bool = False,
        returncode: int | None = None,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind: GitHubErrorKind = kind
        self.retryable = retryable
        self.returncode = returncode  # gh's exit code, when gh ran and failed
        self.transient = transient


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


def _bounded(
    arguments: Sequence[str], timeout: float, stdout: IO[bytes] | None = None, cwd: Path | None = None
) -> Finished:
    """Run gh through the bounded, non-interactive layer, classifying why it could not run or finish.

    gh runs at the path ejryan89-skill-core's locator finds, the one `deploy.py check` reports, so a GitHub CLI only in
    WinGet's Links folder runs too.
    """
    try:
        return run_bounded(located_command(arguments), timeout, stdout=stdout, cwd=cwd)
    except FileNotFoundError as exc:
        raise GitHubError(MISSING_CLI, kind="prerequisite") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHubError(f"GitHub CLI did not finish within {timeout:g} seconds", kind="timeout") from exc
    except OSError as exc:
        raise GitHubError(f"GitHub CLI could not be started: {exc}", kind="execution") from exc


# The functions that start a command whose program tests/run_validation.py cannot read here, with the reason.
COMMANDS_ALLOWED = {
    "subprocess_runner": "GitHubClient's default runner, which runs the gh commands GitHubClient builds; the "
    "policy reads every GitHubClient as running gh",
    "subprocess_downloader": "GitHubClient's runner for a download, which runs the gh commands GitHubClient builds; "
    "the policy reads every GitHubClient as running gh",
}


def subprocess_runner(
    arguments: Sequence[str], *, timeout: float = DEFAULT_TIMEOUT_SECONDS, cwd: Path | None = None
) -> CommandResult:
    """Run a command, in `cwd` when given, as gh commands that read the current repository need.

    Output that is not UTF-8 is kept losslessly, one lone surrogate per undecodable byte.
    """
    finished = _bounded(arguments, timeout, cwd=cwd)
    return CommandResult(finished.returncode, _decode(finished.stdout), _decode(finished.stderr))


def subprocess_downloader(
    arguments: Sequence[str], target: Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> CommandResult:
    """Run a command with its stdout written to `target` byte for byte; the result's stdout is empty."""
    with target.open("wb") as handle:
        finished = _bounded(arguments, timeout, handle)
    return CommandResult(finished.returncode, "", _decode(finished.stderr))


# surrogateescape decodes each byte that is not UTF-8 to one of these, and never yields one otherwise.
_UNDECODABLE = re.compile("[\udc80-\udcff]")


def replace_undecodable(text: str) -> tuple[str, int]:
    """`text` with each undecodable byte the runner kept as U+FFFD, and how many bytes were replaced."""
    return _UNDECODABLE.subn("\ufffd", text)


def classify_failure(
    stderr: str, status: int | None = None, headers: Mapping[str, str] | None = None
) -> GitHubErrorKind:
    """The class of a failed gh command, from its stderr and, for `gh api -i`, the response status and headers."""
    lowered = stderr.casefold()
    headers = headers or {}
    if status == 429 or (status == 403 and headers.get("x-ratelimit-remaining") == "0"):
        return "rate_limit"
    for kind, markers in FAILURE_MARKERS:
        if any(marker in lowered for marker in markers):
            return kind
    return "api"


def transient_failure(stderr: str, status: int | None = None) -> bool:
    """Whether a failed gh command's stderr or `gh api -i` status names a failure that may clear on its own."""
    lowered = stderr.casefold()
    return status in TRANSIENT_STATUSES or any(marker in lowered for marker in TRANSIENT_MARKERS)


def _option(part: str) -> tuple[str, str | None]:
    """A `gh api` option's name and the value attached to it, as in `--method=GET` or `-XGET`, or None."""
    if part.startswith("--"):
        name, separator, value = part.partition("=")
        return name, value if separator else None
    if len(part) > 2:
        return part[:2], part[2:]
    return part, None


def _api_change(arguments: Sequence[str]) -> str | None:
    method: str | None = None
    body = False
    query: str | None = None
    endpoint: str | None = None
    parts = iter(arguments)
    for part in parts:
        if not part.startswith("-"):
            endpoint = endpoint or part
            continue
        name, value = _option(part)
        if name not in API_VALUE_OPTIONS:
            continue
        if value is None:
            value = next(parts, "")
        if name in ("-X", "--method"):
            method = value.upper()
        elif name in API_BODY_OPTIONS:
            body = True
            key, _, field = value.partition("=")
            if name != "--input" and key == "query":
                query = field
    method = method or ("POST" if body else "GET")
    if method in READ_METHODS:
        return None
    # A GraphQL query is sent as a POST but only reads; a document read from a file is not known, so it is a change.
    if endpoint == "graphql" and query and not query.startswith("@") and not GRAPHQL_MUTATION.search(query):
        return None
    return method


def change_of(arguments: Sequence[str]) -> str | None:
    """What a gh command may change on GitHub, by its method such as `POST` or its command such as `gh pr comment`,
    or None when it only reads. A command not known to read counts as a change, so it is never retried."""
    if not arguments:
        return "gh"
    if arguments[0] == "api":
        return _api_change(arguments[1:])
    if arguments[0] == "search" or (len(arguments) > 1 and arguments[1] in READ_VERBS):
        return None
    return "gh " + " ".join(arguments[:2])


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
    kind: GitHubErrorKind = next((GRAPHQL_KINDS[name] for name in GRAPHQL_KINDS if name in types), "api")
    return GitHubError(
        f"GraphQL request failed ({', '.join(types) or 'unknown'}): {messages}",
        kind=kind,
        retryable=kind in RETRYABLE_KINDS,
    )


def body_reasons(body: str) -> list[str]:
    """The reasons a refused `gh api` request's JSON body gives in `errors[].message`, each on one line.

    gh's stderr names only the status, as in `Validation Failed (HTTP 422)`; GitHub says why in the body, such as
    a search naming a user it cannot see. A body that is not such JSON gives none.
    """
    try:
        payload = json_reader.loads(body)
    except ValueError:
        return []
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not isinstance(errors, list):
        return []
    messages = (error.get("message") for error in errors if isinstance(error, dict))
    return [flat_text(message) for message in messages if isinstance(message, str) and message.strip()]


class Response(NamedTuple):
    status: int | None
    headers: dict[str, str]
    body: str


INT_PARSE_ALLOWED = {
    "split_response": "gh writes the HTTP status line's code",
}


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
    """How long to wait before retrying a rate limit or a transient failure on a read, and how often.

    The two are counted apart, so one request runs at most once plus both retry counts, each run within the client's
    time limit.
    """

    base: float = 5.0
    retries: int = 5
    longest: float = 300.0
    # A gateway blip or a dropped connection clears in seconds; a longer outage should fail fast rather than hold a
    # skill for minutes, leaving the rerun to the user. A command that ran out its time limit is not retried at all.
    transient_base: float = 2.0
    transient_retries: int = 2

    def transient_wait(self, attempt: int) -> float:
        """The wait before transient retry `attempt`, counted from 0, doubling from `transient_base`."""
        return self.transient_base * 2.0**attempt

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


def json_body(result: CommandResult) -> Any:
    """The JSON a successful gh command printed, raising a `malformed` GitHubError when it is not JSON.

    `GitHubClient.json` parses with it, and so does a caller's own parse passed to `request`.
    """
    try:
        return json_reader.loads(result.stdout)
    except ValueError as exc:
        raise GitHubError(f"GitHub returned malformed JSON: {exc}", kind="malformed") from exc


def _identity(result: CommandResult) -> CommandResult:
    return result


class GitHubClient:
    """Runs gh, classifies its failures, and waits out rate limits with one bounded backoff policy.

    Every gh command reads no stdin, shows no prompt, and fails as a `timeout` when it runs longer than `timeout`
    seconds; that bound is the default runners', so an injected runner keeps its own. `call_count` counts the gh
    commands run, retries included, from every thread that shares the client.
    """

    def __init__(
        self,
        runner: Runner | None = None,
        *,
        sleeper: Sleeper | None = None,
        clock: Clock | None = None,
        backoff: Backoff = BACKOFF,
        downloader: Downloader | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.runner = runner or partial(subprocess_runner, timeout=timeout)
        # Looked up per client, so a test that patches time.sleep reaches a client built deep inside a call.
        self.sleeper = sleeper or time.sleep
        self.clock = clock or time.time
        self.backoff = backoff
        self.downloader = downloader or partial(subprocess_downloader, timeout=timeout)
        self.call_count = 0
        self._counting = threading.Lock()

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
            payload = json_body(result)
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
        change = change_of(arguments)
        limits = transients = 0
        while True:
            if pacer is not None:
                pacer.before()
            result, response = self._execute(execute, command, change, headers)
            try:
                value = self._judge(command, result, response, parse)
            except GitHubError as exc:
                error = exc
            else:
                if pacer is not None:
                    pacer.after(None)
                return value
            if pacer is not None:
                pacer.after(error)
            if error.transient:
                if change is not None:
                    raise _unknown_outcome(error, change)
                if not retry:
                    raise error
                if transients == self.backoff.transient_retries:
                    raise _gave_up(error, transients)
                self.sleeper(self.backoff.transient_wait(transients))
                transients += 1
                continue
            if not (retry and error.retryable):
                raise error
            if limits == self.backoff.retries:
                raise _gave_up(error, limits)
            self.sleeper(self.backoff.wait(limits, response.headers, self.clock()))
            limits += 1

    def _execute(
        self, execute: Runner, command: Sequence[str], change: str | None, headers: bool
    ) -> tuple[CommandResult, Response]:
        """Run one attempt, counted, and decode its result."""
        with self._counting:
            self.call_count += 1
        try:
            raw = execute(command)
        except GitHubError as exc:
            # A change stopped at the time limit may have reached GitHub before it was stopped.
            if exc.kind == "timeout" and change is not None:
                raise _unknown_outcome(exc, change) from exc
            raise
        return self._decoded(raw, headers)

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
            if command[1] == "api":
                reasons = [reason for reason in body_reasons(result.stdout) if reason not in message]
                message = f"{message}: {'; '.join(reasons)}" if reasons else message
            raise GitHubError(
                message,
                kind=kind,
                retryable=kind in RETRYABLE_KINDS,
                returncode=result.returncode,
                transient=kind in TRANSIENT_KINDS and transient_failure(result.stderr, response.status),
            )
        return parse(result)


def _gave_up(error: GitHubError, retries: int) -> GitHubError:
    return GitHubError(
        f"{error} (gave up after {retries} retries)",
        kind=error.kind,
        retryable=error.retryable,
        returncode=error.returncode,
        transient=error.transient,
    )


def _unknown_outcome(error: GitHubError, change: str) -> GitHubError:
    return GitHubError(
        f"{error} (the outcome of this {change} request is unknown: {UNKNOWN_OUTCOME})",
        kind="unknown_outcome",
        returncode=error.returncode,
        transient=True,
    )
