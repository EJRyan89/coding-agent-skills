"""Start each runtime command line this repository builds, signed out, and report whether its runtime parses it.

Usage:
  python tools/runtime_command_lines.py [--runtime NAME ...] [--timeout SECONDS] [--summary FILE]

Every command line comes from the function that builds it for a real run, never from a copy here: the Copilot CLI
review host's `copilot_command` in ejryan89-code-review-core, `run_command` in tools/runtime_canary.py,
`claude_command` and `trigger_command` in tools/skill_evals_runs.py, the plugin canary's sessions and its
`claude plugin` validate, marketplace add, install, list, and details commands in tools/plugin_canary.py, the last
started in its order on this checkout's plugins assembled as it assembles them, the skill listings and the Codex
catalog in deployer/discovery.py, and the Codex sandbox probe in deployer/tools.py. A builder that reads the
runtime's version is given the version the installed runtime's `--version` prints, so the line checked is the one a
real run would start there.

Each command line is checked twice. Each flag it passes is looked for in its runtime's help for the subcommand it
starts, such as `codex exec --help`. Then it is started in a throwaway home with no stdin, signed out: its environment
keeps only the variables a program needs to start, every home and configuration folder is inside the throwaway home,
every request goes to a proxy nothing listens on, and Copilot CLI's auto-update is off. So no runtime finds a sign-in or
reaches a model, and the start shows only whether the runtime got past parsing its arguments. A start is ended at
--timeout (default 30 seconds) with every process it started.

It prints one fact per line:

  VERSION <runtime> <version>                        what its --version prints, or unknown
  SAMPLE_BEHIND <runtime> <sample> <recorded> <version>  a sample of its output captured under
                                                     tests/fixtures/captured-output records an older version, so it
                                                     is due to be captured again (tools/captured_samples.py); never
                                                     a failure
  MISSING <runtime> "<reason>"                       it is not installed where deploy.py check looks; exit 1
  UNSAFE <runtime> "<reason>"                        it cannot be started signed out, so none of its command lines is
                                                     started; exit 1
  HELP_FAILED <runtime> <command> "<reason>"         the help its flags are looked for in could not be read: it
                                                     could not start, did not finish, or exited non-zero; the line
                                                     is still started and judged, with no UNLISTED; exit 1
  ACCEPTED <runtime> <command>                       it got past its arguments: it ended at sign-in or at the
                                                     unreachable model, still ran at the time limit having started its
                                                     session, or finished with status 0
  ACCEPTED <runtime> <command> UNLISTED "<flags>"    the same, though its help lists none of these flags
  REMOVED <runtime> <command> <flag> "<message>"     it refused the flag as unknown, removed, or renamed; exit 1
  REFUSED <runtime> <command> "<message>"            it refused its arguments another way, such as a value; exit 1
  FAILED <runtime> <command> "<message>"             it could not start, or ended without reaching its session and
                                                     without an argument error; exit 1

A message is the runtime's own, with the throwaway home named <temp>, this checkout <source>, and the profile folder
<home>. With --summary FILE it also appends a Markdown table of the same facts to FILE, as the workflow does with its
step summary. A usage error exits 2.

It is the check .github/workflows/runtime-command-lines.yml runs weekly on the latest release of every runtime, on a
fresh Windows runner and a fresh Linux one. It starts no model, so tests/run_validation.py could run it, but it reads
the runtimes installed on the machine, so it is a scheduled signal and never a required check.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

from console import run_main
from flat_text import single_line
from runtime_support import RUNTIMES as RUNTIME_NAMES
from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))
from review_config import REVIEWER_EFFORTS
from review_hosts import COPILOT_SYNC_SESSION_VERSION, copilot_command, isolated_copilot_environment

from deployer import discovery, platform_support
from deployer import tools as deployer_tools
from tools import captured_samples, plugin_canary, runtime_canary, skill_evals_runs
from tools.runtime_canary import DECLARED_RUNTIME, RUNTIMES, Completed, quote

DEFAULT_TIMEOUT = 30.0
# A port nothing listens on, so a runtime that found a credential after all could still reach no model through it.
UNREACHABLE_PROXY = "http://127.0.0.1:9"
PROXY_VARIABLES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
# What a builder is given where a real run gives its own: the canary's fixture skill, a request, and a plugin.
REQUEST = "runtime command line check"
REVIEWED_SKILL = "review-prs"
PLUGIN = "runtime-command-lines"

# How each runtime's argument parser refuses a flag, naming it: Claude Code's commander ("unknown option"), Codex's and
# Copilot CLI's clap ("unexpected argument ... found"), and Copilot's own refusal of a flag it dropped.
FLAG_REFUSALS = (
    re.compile(r"unknown option '(?P<flag>-[^'\s]+)'"),
    re.compile(r"unexpected argument '(?P<flag>-[^'\s]+)' found"),
    re.compile(r"(?<![\w-])(?P<flag>--?[A-Za-z][\w-]*) (?:was|has been) (?:removed|renamed)"),
)
# How each parser refuses its arguments without naming an unknown flag: a value, a missing value, or a count, and
# Copilot CLI's refusal of any argument it does not know, an unknown flag among them, which names none.
ARGUMENT_REFUSALS = re.compile(
    r"^\s*error: (?:invalid value|option '[^']+' argument|too many arguments|missing required argument|"
    r"the following required arguments|the argument '[^']+' cannot be used|a value is required|"
    r"invalid command format)",
    re.MULTILINE | re.IGNORECASE,
)
# What each runtime prints once it is past its arguments and has started its session, signed out: Claude Code's init
# event or its sign-in refusal, Codex exec's thread start, after which it retries the unreachable model until the time
# limit, and Copilot CLI's sign-in refusal.
SESSION_STARTED = {
    "claude": re.compile(r'"subtype":"init"|Not logged in'),
    "codex": re.compile(r'"type":"thread\.started"'),
    "copilot": re.compile(r"No authentication information found"),
}
FAILING = ("MISSING ", "UNSAFE ", "HELP_FAILED ", "REMOVED ", "REFUSED ", "FAILED ")

# Starts a program with its arguments, working directory, environment, time limit, and all it reads on stdin, and
# returns what it did, a failure to start it included, never raising for the run itself.
Starter = Callable[[list[str], Path, dict[str, str], float, str], Completed]


def start(arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float, stdin: str) -> Completed:
    """Run the program as the runtime canary runs a runtime, within `timeout` in its own process tree, which the limit
    ends with every process it started, with `stdin` all it reads there."""
    return runtime_canary.run_process(arguments, cwd, environment, timeout, stdin.encode("utf-8"))


@dataclass(frozen=True)
class CommandLine:
    """One command line a builder made, with where and how the check starts it."""

    runtime: str
    name: str
    arguments: list[str]
    cwd: Path
    environment: dict[str, str] = field(repr=False)
    # What a real run writes to its stdin, which is all it reads there.
    stdin: str = ""

    @property
    def subcommand(self) -> list[str]:
        """The words after the program that choose its subcommand, whose help lists the flags it takes."""
        words: list[str] = []
        for argument in self.arguments[1:]:
            if argument.startswith("-"):
                break
            words.append(argument)
        return words

    @property
    def flags(self) -> list[str]:
        """Each flag the command line passes, without a value joined to it by `=`, in order, each once."""
        found = [argument.split("=", 1)[0] for argument in self.arguments[1:] if re.match(r"-{1,2}[A-Za-z]", argument)]
        return list(dict.fromkeys(found))


class Unsafe(Exception):
    """The runtime cannot be started signed out, so it is not started."""


def without_gh(search_path: str, executable: str) -> str:
    """`search_path` without each directory that holds GitHub CLI, since Copilot CLI signs in with `gh auth token`,
    which on Windows answers from the credential store whatever its configuration folder says, and skips that only when
    no `gh` is on its search path. Raise Unsafe when the runtime's own directory or Node.js's holds `gh` too."""
    directories = [directory for directory in search_path.split(os.pathsep) if directory]
    node = platform_support.find_on_path("node", search_path)
    needed = {os.path.normcase(Path(program).parent) for program in (executable, node) if program}
    kept = []
    for directory in directories:
        if platform_support.find_on_path("gh", directory) is None:
            kept.append(directory)
        elif os.path.normcase(directory) in needed:
            raise Unsafe(
                f"GitHub CLI is in {directory}, beside the runtime or Node.js, so its sign-in cannot be hidden"
            )
    return os.pathsep.join(kept)


def signed_out(home: Path, executable: str) -> dict[str, str]:
    """An environment in which no runtime finds a sign-in or reaches a model: the variables a program needs to start,
    a PATH without GitHub CLI, every home and runtime configuration folder inside `home`, every request through
    UNREACHABLE_PROXY, and Copilot CLI on the version installed."""
    environment = platform_support.signed_out_environment(os.environ, home)
    environment["PATH"] = without_gh(environment.get("PATH", ""), executable)
    folders = {
        "CLAUDE_CONFIG_DIR": home / ".claude",
        "CODEX_HOME": home / ".codex",
        "COPILOT_HOME": home / ".copilot",
        "GH_CONFIG_DIR": home / ".gh",
    }
    for folder in folders.values():
        folder.mkdir(parents=True, exist_ok=True)
    return {
        **environment,
        **{name: str(folder) for name, folder in folders.items()},
        **dict.fromkeys(PROXY_VARIABLES, UNREACHABLE_PROXY),
        "NO_PROXY": "",
        **runtime_canary.PINNED_VERSION,
    }


def claude_lines(executable: str, root: Path, environment: dict[str, str]) -> list[CommandLine]:
    """The canary's, the skill evaluations', and the plugin canary's Claude Code sessions, then the plugin canary's
    `claude plugin` commands, in the order it starts them, on this checkout's plugins assembled as it assembles them:
    the plugin with options is the one validated and installed, so its install passes --config."""
    home = root / "home"
    agents = home / skill_evals_runs.STATE / skill_evals_runs.AGENTS
    agents.parent.mkdir(parents=True, exist_ok=True)
    agents.write_text("{}", encoding="utf-8")
    plugin = root / "plugin"
    plugin.mkdir(exist_ok=True)
    paths = {PLUGIN: str(plugin)}
    marketplace = root / "marketplace"
    built_plugins = plugin_canary.build(marketplace, [runtime_canary.REPOSITORY_ROOT])
    configured = next(item for item in built_plugins if plugin_canary.option_values(marketplace, item))
    identifier = f"{configured.name}@{plugin_canary.MARKETPLACE}"
    built = {
        "runtime-canary": runtime_canary.run_command("claude", executable, runtime_canary.FIXTURE_SKILL, home),
        "skill-evals-review": skill_evals_runs.claude_command(executable, REQUEST, home, REVIEWED_SKILL),
        "skill-evals-trigger": skill_evals_runs.trigger_command(executable, REQUEST, skill_evals_runs.SESSION_MODEL),
        "plugin-canary-probe": plugin_canary.session_command(executable, REQUEST, paths),
        "plugin-canary-guard": plugin_canary.guard_command(executable, REQUEST, paths),
        "plugin-canary-unguarded": plugin_canary.unguarded_command(executable, REQUEST, paths),
        "plugin-canary-validate": plugin_canary.validate_command(executable, marketplace / configured.name),
        "plugin-canary-marketplace": plugin_canary.marketplace_command(executable, marketplace),
        "plugin-canary-install": plugin_canary.install_command(
            executable, identifier, plugin_canary.option_values(marketplace, configured)
        ),
        "plugin-canary-list": plugin_canary.list_command(executable),
        "plugin-canary-details": plugin_canary.details_command(executable, identifier),
    }
    return [CommandLine("claude", name, arguments, home, environment) for name, arguments in built.items()]


def codex_lines(executable: str, root: Path, environment: dict[str, str]) -> list[CommandLine]:
    """The canary's Codex exec session, the deployer's skill listing and catalog, and `check`'s sandbox probe."""
    home = root / "home"
    built = {
        "runtime-canary": runtime_canary.run_command("codex", executable, runtime_canary.FIXTURE_SKILL, home),
        "skill-listing": discovery.listing_command("codex", executable),
        "skill-catalog": discovery.catalog_command(executable),
        "sandbox-probe": deployer_tools.codex_sandbox_command(executable),
    }
    return [CommandLine("codex", name, arguments, home, environment) for name, arguments in built.items()]


def copilot_lines(
    executable: str, root: Path, environment: dict[str, str], version: tuple[int, ...] | None
) -> list[CommandLine]:
    """The review host's Copilot CLI review, in the isolation it builds, the canary's session, and the skill
    listing. A version that cannot be read is taken to be the latest, as the canary takes it."""
    home = root / "home"
    version = version or COPILOT_SYNC_SESSION_VERSION
    run_directory, materialized = root / "review-run", root / "review-reviewer"
    run_directory.mkdir(exist_ok=True)
    materialized.mkdir(exist_ok=True)
    workspace, review_environment = isolated_copilot_environment(root / "review-isolation", environment)
    review = copilot_command(
        executable,
        version=version,
        run_directory=run_directory,
        materialized_root=materialized,
        result_path=run_directory / "result.json",
        effort=sorted(REVIEWER_EFFORTS)[0],
    )
    return [
        # The host writes the review's prompt on stdin, which is all Copilot reads there.
        CommandLine("copilot", "review-host", review, workspace, review_environment, REQUEST),
        CommandLine(
            "copilot",
            "runtime-canary",
            runtime_canary.run_command("copilot", executable, runtime_canary.FIXTURE_SKILL, home, version=version),
            home,
            environment,
        ),
        CommandLine("copilot", "skill-listing", discovery.listing_command("copilot", executable), home, environment),
    ]


def command_lines(
    runtime: str, executable: str, root: Path, environment: dict[str, str], version: tuple[int, ...] | None
) -> list[CommandLine]:
    (root / "home").mkdir(parents=True, exist_ok=True)
    if runtime == "claude":
        return claude_lines(executable, root, environment)
    if runtime == "codex":
        return codex_lines(executable, root, environment)
    return copilot_lines(executable, root, environment, version)


def own_message(completed: Completed) -> str:
    """What the runtime said last, on one line: why it could not start, else its last line of stderr, else of
    stdout, else its status."""
    for stream in (completed.stderr, completed.stdout):
        lines = [line.strip() for line in stream.splitlines() if line.strip()]
        if lines:
            return completed.error or single_line(lines[-1])
    return completed.error or f"exited with status {completed.returncode} and printed nothing"


def judge(line: CommandLine, completed: Completed, unlisted: Sequence[str], timeout: float) -> str:
    """The one fact the start shows about the command line."""
    label = f"{line.runtime} {line.name}"
    if completed.error:
        return f"FAILED {label} {quote(completed.error)}"
    output = f"{completed.stderr}\n{completed.stdout}"
    for pattern in FLAG_REFUSALS:
        found = pattern.search(output)
        if found:
            message = next(text for text in output.splitlines() if found.group(0) in text)
            return f"REMOVED {label} {found.group('flag')} {quote(single_line(message.strip()))}"
    refused = ARGUMENT_REFUSALS.search(output)
    if refused:
        message = output[refused.start() :].splitlines()[0]
        return f"REFUSED {label} {quote(single_line(message.strip()))}"
    if (completed.returncode == 0 and not completed.timed_out) or SESSION_STARTED[line.runtime].search(output):
        return f"ACCEPTED {label}" + (f" UNLISTED {quote(' '.join(unlisted))}" if unlisted else "")
    if completed.timed_out:
        late = f"it showed no session start within {timeout:g} seconds: {own_message(completed)}"
        return f"FAILED {label} {quote(late)}"
    return f"FAILED {label} {quote(own_message(completed))}"


def listed(flag: str, help_text: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text) is not None


def check(
    runtime: str,
    executable: str,
    root: Path,
    runner: Starter,
    timeout: float,
    environment: dict[str, str],
    samples: Sequence[captured_samples.Sample] = (),
) -> Iterator[str]:
    """VERSION, SAMPLE_BEHIND for each of `samples` of the runtime older than it, then one line per command line the
    repository builds for `runtime`."""
    version = runtime_canary.read_version(
        executable,
        lambda arguments, cwd, variables, limit: runner(arguments, cwd, variables, limit, ""),
        environment,
        root,
    )
    yield f"VERSION {runtime} {runtime_canary.version_text(version)}"
    if version:
        yield from captured_samples.behind(list(samples), DECLARED_RUNTIME[runtime], version, runtime)
    helps: dict[tuple[str, ...], tuple[str, str]] = {}
    for line in command_lines(runtime, executable, root, environment, version):
        words = tuple(line.subcommand)
        if words not in helps:
            shown = runner([executable, *words, "--help"], line.cwd, line.environment, timeout, "")
            helps[words] = (f"{shown.stdout}\n{shown.stderr}", help_failure(shown, timeout))
        help_text, failure = helps[words]
        if failure:
            yield f"HELP_FAILED {line.runtime} {line.name} {quote(failure)}"
        unlisted = [] if failure else [flag for flag in line.flags if not listed(flag, help_text)]
        yield judge(line, runner(line.arguments, line.cwd, line.environment, timeout, line.stdin), unlisted, timeout)


def help_failure(shown: Completed, timeout: float) -> str:
    """Why a runtime's help could not be read, so its flags cannot be looked for there, or "" when it was."""
    if shown.error:
        return shown.error
    if shown.timed_out:
        return f"its help did not finish within {timeout:g} seconds"
    if shown.returncode != 0:
        return f"its help exited with status {shown.returncode}: {own_message(shown)}"
    return ""


def runtime_lines(
    runtime: str,
    executable: str | None,
    root: Path,
    runner: Starter,
    timeout: float,
    samples: Sequence[captured_samples.Sample] = (),
) -> Iterator[str]:
    if executable is None:
        yield f"MISSING {runtime} {quote('it is not installed where deploy.py check looks')}"
        return
    try:
        environment = signed_out(root / "home", executable)
    except Unsafe as exc:
        yield f"UNSAFE {runtime} {quote(str(exc))}"
        return
    yield from check(runtime, executable, root, runner, timeout, environment, samples)


def failed(lines: Sequence[str]) -> bool:
    """Whether any line shows a command line refused, a runtime missing, or a start that could not be made safely."""
    return any(line.startswith(FAILING) for line in lines)


def summary(lines: Sequence[str]) -> str:
    """A Markdown table of the facts, one row per runtime version or command line."""
    rows = ["## Runtime command lines", "", "| Runtime | Command line | Outcome |", "| --- | --- | --- |"]
    for line in lines:
        kind, runtime, rest = [*line.split(" ", 2), "", ""][:3]
        if kind in ("VERSION", "MISSING", "UNSAFE"):
            rows.append(f"| {runtime} | {kind.lower()} | {rest.replace('|', '&#124;')} |")
            continue
        name, _, detail = rest.partition(" ")
        rows.append(f"| {runtime} | {name} | {(kind + ' ' + detail).strip().replace('|', '&#124;')} |")
    count = sum(1 for line in lines if line.startswith(FAILING))
    verdict = f"{count} line(s) failed." if count else "Every runtime accepted every command line."
    return "\n".join([*rows, "", verdict, ""]) + "\n"


def run(
    runtimes: Sequence[str],
    timeout: float,
    root: Path,
    runner: Starter = start,
    which: Callable[[str], str | None] = platform_support.find_executable,
    say: Callable[[str], None] = print,
    samples: Sequence[captured_samples.Sample] | None = None,
) -> list[str]:
    """Check each runtime's command lines from `root`, printing each line as it is made; return the lines. The
    samples aged against each runtime's version are `samples`, or those this checkout records."""
    paths = {root: "<temp>", runtime_canary.REPOSITORY_ROOT: "<source>", platform_support.home_directory(): "<home>"}
    if samples is None:
        samples = captured_samples.read(runtime_canary.REPOSITORY_ROOT, RUNTIME_NAMES)
    lines: list[str] = []
    for runtime in runtimes:
        for line in runtime_lines(runtime, which(runtime), root / runtime, runner, timeout, samples):
            shown = runtime_canary.placeholders(line, paths)
            say(shown)
            lines.append(shown)
    return lines


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--runtime", action="append", choices=RUNTIMES, help="check only this runtime; repeatable")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SECONDS")
    parser.add_argument("--summary", type=Path, metavar="FILE", help="append a Markdown table of the facts to FILE")
    namespace = parser.parse_args(arguments)
    with tempfile.TemporaryDirectory(prefix="runtime-command-lines-") as directory:
        lines = run(namespace.runtime or list(RUNTIMES), namespace.timeout, Path(directory))
    if namespace.summary is not None:
        with namespace.summary.open("a", encoding="utf-8") as stream:
            stream.write(summary(lines))
    return 1 if failed(lines) else 0


if __name__ == "__main__":
    run_main(main)
