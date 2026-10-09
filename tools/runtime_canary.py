"""Deploy skills into a throwaway home and check that Claude Code, Codex, and Copilot CLI find and run them.

Usage:
  python tools/runtime_canary.py [SKILL ...] [--runtime NAME ...] [--discovery-only] [--timeout SECONDS]
                                 [--codex-sandbox elevated|unelevated]
  python tools/runtime_canary.py --remove-home DIR

It makes a fresh home under the temporary directory and deploys this checkout into it with `deploy.py
--canary-home`, which a linked worktree may do, then deploys the fixture source in tests/fixtures/runtime-canary.
That home is the only place anything is deployed. Each installed runtime then starts with the home as its working
directory, so it finds the deployed skills as project skills, and with a fixed prompt naming one skill: the fixture
runtime-canary-probe, then each SKILL given. It prints one fact per line:

  HOME "<dir>"                                  the throwaway home; delete it with --remove-home when done
  DEPLOYED <source id>                          or DEPLOY_FAILED <source id> "<log>", which stops the run
  SKIPPED <runtime> "<reason>"                  the runtime is not on PATH
  DISCOVERED <runtime> <skill> ["<path>"]       the runtime lists the canary's copy, enabled
  SHADOWED <runtime> <skill> "<path>"           it also lists another enabled copy, such as an installed adapter
  DISABLED <runtime> <skill> "<path>"           it lists this copy turned off in its settings
  UNDISCOVERED <runtime> <skill> ["<reason>"]   it does not list the canary's copy enabled; the canary exits 1
  DISCOVERY_FAILED <runtime> "<reason>"
  SUPPLIED <runtime> "<key>=<value>"            a setting the canary passes in place of the configuration it ignores
  RUNTIME <runtime> <skill> RAN "<script>" "<cwd>" [exit=<n>] [timed-out] KEPT "<ambient>"
  RUNTIME <runtime> <skill> BLOCKED "<policy>" KEPT "<ambient>"
  RUNTIME <runtime> <skill> FAILED "<reason>" KEPT "<ambient>"
  RUNTIME <runtime> <skill> UNSUPPORTED "<reason>"  the runtime cannot start the skill headless, so it never ran
  RUNTIME <runtime> <skill> NOT_ATTEMPTED "<reason>"  the skill declares none for the runtime, so it never ran
  TRANSCRIPT <runtime> <skill> "<path>"         the runtime's output for that run
  MATRIX <runtime> <skill> AGREES <level>       what ran is what the skill's declared runtime support expects
  MATRIX <runtime> <skill> DISAGREES <level> "<what ran instead>"

With --remove-home DIR it prints REMOVED "<dir>", or FAILED "<reason>" and exits 1.

The isolation is of configuration, not of credentials: each runtime keeps its real configuration folder and so
its sign-in, and the canary switches off what each runtime lets it: Claude Code's user settings, skills, and MCP
servers; Codex's config.toml and rules; Copilot's custom instructions and built-in MCP servers. KEPT names what
still loads. Codex on Windows refuses every command without a Windows sandbox mode, which it would otherwise read
from config.toml, so the canary supplies that one value itself (--codex-sandbox, default elevated) and prints it
as SUPPLIED. Every prompt carries RUNTIME_CANARY_MARKER, so a session the runtime saves can be found later.

The canary judges nothing from a transcript. The fixture's script writes a marker from its own location, a
sitecustomize.py on PYTHONPATH records every Python process the runtime starts, and a BASH_ENV file records every
Bash script, however the runtime names Bash. RAN means the script ran from the canary's copy of the skill or of a
skill it declares in skill_deps, whatever it exited with; a script from the installed copy is a failure, never RAN.
RAN adds exit=<n> when a recorder saw the script end (Python 3.12 or later; a Bash script that sets no EXIT trap of
its own) and timed-out when the session ran out of time after the script started. The fixture must also exit 0, or
it is FAILED with the status. Copilot's headless mode
cannot start a skill only the user may start, so such a skill is UNSUPPORTED there and no model is called for it.
--discovery-only lists skills without running any model. --remove-home deletes a home an earlier run printed and
nothing else: only a runtime-canary- directory directly under the temporary directory.

Each skill's runtime_support in deploy-meta, the matrix docs/skills.md prints, says what to expect: `full` must be
RAN, `partial` must be RAN or, when the runtime lacks user-only-start, UNSUPPORTED, since the limits of the other
capabilities lie past the first script the canary asks for, and `none` is NOT_ATTEMPTED. After the runs, one MATRIX
line per RUNTIME line compares the two, and the canary exits 1 when any disagrees or a skill declares nothing, so the
matrix cannot claim more than a run shows. It also exits 1 on any UNDISCOVERED line, --discovery-only included.

This is a manual check, never part of tests/run_validation.py: each run calls a model, and a model may choose
not to run the command. See .claude/skills/runtime-canary/SKILL.md.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "skill-core" / "scripts"))

import frontmatter

from deployer import discovery, pipeline, platform_support, runtime_support
from deployer import source as deploy_source
from deployer.errors import DeployError
from deployer.paths import Paths

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

RUNTIMES = ("claude", "codex", "copilot")
# Each runtime's name in a skill's runtime_support declaration.
DECLARED_RUNTIME = {"claude": "claude-code", "codex": "codex", "copilot": "copilot-cli"}
# The outcome a partial skill reaches where the runtime lacks the capability; any other limit lies past the first
# script the canary asks for, so the skill still runs that far.
PARTIAL_OUTCOME = {"user-only-start": "UNSUPPORTED"}
OUTCOME_LINE = re.compile(r"^RUNTIME (\S+) (\S+) ([A-Z_]+)\b")
FIXTURE_SOURCE = REPOSITORY_ROOT / "tests" / "fixtures" / "runtime-canary"
FIXTURE_SKILL = "runtime-canary-probe"
STATE = ".runtime-canary"
HOME_PREFIX = "runtime-canary-"
DEFAULT_TIMEOUT = 600
RUNTIME_CANARY_MARKER = "RUNTIME-CANARY-RUN"
# What each runtime still loads from the user's real configuration once the canary's flags have switched off all
# they can. Claude Code: checked with /context under --setting-sources project,local. Codex and Copilot: their
# skill listings, and the flags each offers.
KEPT = {
    "claude": "user CLAUDE.md; auto-memory folder",
    "codex": "personal skills in ~/.agents/skills and ~/.codex/skills",
    "copilot": "personal skills in ~/.agents/skills and ~/.copilot/skills; MCP servers in ~/.copilot/mcp-config.json; "
    "session history",
}
# How a prompt starts a skill by name: `$<skill>` in Codex, `/<skill>` elsewhere. See "Starting a skill" in
# docs/skills.md. npm installs Codex as a .cmd file that cmd.exe runs, so the text avoids cmd's special characters.
MENTION = {"claude": "/", "codex": "$", "copilot": "/"}
INSTRUCTION = (
    "Do not carry out this skill's task. Run only the first command in its instructions that starts a script "
    "from the skill's own directory or a sibling skill's, with --help as that script's only arguments, then stop "
    f"and report what it printed. This is a {RUNTIME_CANARY_MARKER} session."
)
# Without a [windows] sandbox value, which --ignore-user-config leaves out, Codex on Windows runs read-only even
# under --sandbox workspace-write and refuses every command. A -c override still applies beside that flag. The
# value is left bare, which Codex reads as a string, so cmd.exe never sees a quote. See docs/codex-support.md.
CODEX_SANDBOXES = ("elevated", "unelevated")
DEFAULT_CODEX_SANDBOX = "elevated"
CODEX_SUPPORT = "docs/codex-support.md#windows-sandbox-mode"
# In Copilot CLI 1.0.91, `copilot -p` neither expands a leading /<skill> for a user-only skill nor lets the model's
# skill tool start one. Recheck on each Copilot CLI upgrade, and drop this case once a headless run can start one.
COPILOT_SUPPORT = "docs/copilot-support.md#headless-sessions"
USER_ONLY_UNSUPPORTED = {"copilot": f"copilot -p cannot start a user-only skill; see {COPILOT_SUPPORT}"}
SCRIPT_LOG = "RUNTIME_CANARY_SCRIPT_LOG"
RECORDER = '''"""Written by tools/runtime_canary.py: record each Python process a runtime starts and how it exited."""
import atexit
import json
import os
import sys

# A copy, since runpy rewrites sys.argv in place. The exit record repeats the start record, so the two pair up.
_record = {"argv": list(sys.argv), "cwd": os.getcwd(), "pid": os.getpid()}
_unwound = []


def _write(record):
    with open(os.environ["RUNTIME_CANARY_SCRIPT_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\\n")


def _unwinding(code, offset, exception):
    main = getattr(sys.modules.get("__main__"), "__file__", None)
    if code.co_name == "<module>" and main and os.path.abspath(code.co_filename) == os.path.abspath(main):
        _unwound.append(exception)


def _exited():
    exception = _unwound[-1] if _unwound else None
    if isinstance(exception, KeyboardInterrupt):
        return
    if isinstance(exception, SystemExit):
        code = exception.code
        status = 0 if code is None else code if isinstance(code, int) else 1
    else:
        status = 0 if exception is None else 1
    try:
        _write({**_record, "exit": status})
    except Exception:
        pass


try:
    _write(_record)
    # sys.monitoring, from Python 3.12, reports the main module leaving by an exception, SystemExit included, and
    # costs nothing while none is raised. Without it, or with its tool slot taken, the exit goes unrecorded.
    _monitoring = sys.monitoring
    _monitoring.use_tool_id(4, "runtime-canary")
    _monitoring.register_callback(4, _monitoring.events.PY_UNWIND, _unwinding)
    _monitoring.set_events(4, _monitoring.events.PY_UNWIND)
    atexit.register(_exited)
except Exception:
    pass
'''
# Platform-specific names that tests/run_validation.py allows outside deployer/platform_support.py, with the reason.
PLATFORM_ALLOWED = {
    "cygpath": "BASH_RECORDER runs it only where `command -v` finds it, and otherwise keeps the path it has",
}
# Bash reads BASH_ENV before it runs a script, whether the runtime started `bash` from PATH or Git Bash by its full
# path, and before each `bash -c` command, which is not a script and is left out. Git Bash names the temporary
# directory /tmp, so cygpath, where it exists, gives the Windows paths the canary compares. It takes the place of
# any BASH_ENV of the user's, which is ambient configuration the canary leaves out. Its EXIT trap records the status
# the script exits with, unless the script replaces the trap with its own; a subshell inherits no EXIT trap.
BASH_RECORDER = r"""# Written by tools/runtime_canary.py: record each Bash script a runtime starts and how it exited.
if [ -z "${BASH_EXECUTION_STRING+set}" ] && [ -n "${RUNTIME_CANARY_SCRIPT_LOG-}" ]; then
  case ${0##*[/\\]} in
    bash | bash.exe | sh | sh.exe) ;;
    *)
      runtime_canary_script=$0
      runtime_canary_cwd=$PWD
      if command -v cygpath >/dev/null 2>&1; then
        runtime_canary_script=$(cygpath -m -a -- "$0" 2>/dev/null) || runtime_canary_script=$0
        runtime_canary_cwd=$(cygpath -m -- "$PWD" 2>/dev/null) || runtime_canary_cwd=$PWD
      fi
      runtime_canary_script=${runtime_canary_script//\\/\\\\}
      runtime_canary_script=${runtime_canary_script//\"/\\\"}
      runtime_canary_cwd=${runtime_canary_cwd//\\/\\\\}
      runtime_canary_cwd=${runtime_canary_cwd//\"/\\\"}
      runtime_canary_record="{\"argv\": [\"$runtime_canary_script\"], \"cwd\": \"$runtime_canary_cwd\", \"pid\": $$"
      { printf '%s}\n' "$runtime_canary_record" >>"$RUNTIME_CANARY_SCRIPT_LOG"; } 2>/dev/null || :
      printf -v runtime_canary_trap '{ printf %q %q "$?" >>%q; } 2>/dev/null || :' \
        '%s, "exit": %s}\n' "$runtime_canary_record" "$RUNTIME_CANARY_SCRIPT_LOG"
      trap "$runtime_canary_trap" EXIT
      unset runtime_canary_script runtime_canary_cwd runtime_canary_record runtime_canary_trap
      ;;
  esac
fi
"""


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


Runner = Callable[[list[str], Path, dict[str, str], float], Completed]


def run_process(arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Completed:
    try:
        # Codex exec reads a prompt addition from stdin until it closes.
        process = subprocess.run(
            arguments,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:

        def text(value: str | bytes | None) -> str:
            return value.decode("utf-8", "replace") if isinstance(value, bytes) else value or ""

        return Completed(-1, text(exc.stdout), text(exc.stderr), timed_out=True)
    return Completed(process.returncode, process.stdout, process.stderr)


def forward(path: str | os.PathLike[str]) -> str:
    return platform_support.normalize(path)


def quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def lexical(path: str | os.PathLike[str]) -> str:
    # Lexical: Path.absolute keeps "..", which would pass commonpath, and Path.resolve follows junctions.
    return os.path.normcase(os.path.abspath(path))  # noqa: PTH100 - lexical, as the comment says


def inside(path: str, directory: Path) -> bool:
    candidate, root = lexical(path), lexical(directory)
    return candidate != root and os.path.commonpath([candidate, root]) == root


def read_records(path: Path) -> list[dict]:
    """The JSON objects in a JSON Lines file, skipping any line a crashed writer left incomplete."""
    records = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            with contextlib.suppress(json.JSONDecodeError):
                record = json.loads(line)
                if isinstance(record, dict):
                    records.append(record)
    return records


def deploy(home: Path, source_dir: Path) -> tuple[int, str]:
    """Deploy every root of a source, opt-in ones included, into the canary home; return the code and output."""
    paths = Paths(source_dir, home)
    try:
        src = deploy_source.discover(paths, deploy_source.load_source_id(paths))
        opt_in = [root for root in [*src.bundles, *deploy_source.root_names(src)] if deploy_source.is_opt_in(src, root)]
    except DeployError as exc:
        return 1, "\n".join(exc.lines) + "\n"
    arguments = ["--canary-home", str(home), "--all", *(item for root in opt_in for item in ("--include", root))]
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        code = pipeline.run(arguments, paths, stdin=io.StringIO(""))
    return code, captured.getvalue()


def source_id(source_dir: Path) -> str:
    try:
        return deploy_source.load_source_id(Paths(source_dir, source_dir))
    except DeployError:
        return source_dir.name


def dependencies(sources: list[Path]) -> dict[str, list[str]]:
    """Each skill in the sources with the skills it may run scripts from: itself and its skill_deps closure."""
    closure: dict[str, list[str]] = {}
    for source_dir in sources:
        paths = Paths(source_dir, source_dir)
        with contextlib.suppress(DeployError):
            src = deploy_source.discover(paths, deploy_source.load_source_id(paths))
            closure.update({name: deploy_source.expand(src, [], [name]) for name in src.skills})
    return closure


Declarations = Mapping[str, Mapping[str, runtime_support.Support] | None]


def declarations(sources: list[Path]) -> dict[str, dict[str, runtime_support.Support] | None]:
    """Each skill in the sources with its declared runtime support, or None when it declares none."""
    declared: dict[str, dict[str, runtime_support.Support] | None] = {}
    for source_dir in sources:
        paths = Paths(source_dir, source_dir)
        with contextlib.suppress(DeployError):
            src = deploy_source.discover(paths, deploy_source.load_source_id(paths))
            declared.update({name: skill.runtime_support for name, skill in src.skills.items()})
    return declared


def expected(support: runtime_support.Support) -> str:
    """The outcome a run must reach for the declared support."""
    if support.level == runtime_support.NONE:
        return "NOT_ATTEMPTED"
    if support.level == runtime_support.PARTIAL:
        return next((PARTIAL_OUTCOME[need] for need in support.needs if need in PARTIAL_OUTCOME), "RAN")
    return "RAN"


def compare(lines: Sequence[str], declared: Declarations) -> list[str]:
    """One MATRIX line per RUNTIME line: whether what ran is what the skill's declared support expects."""
    result = []
    for line in lines:
        match = OUTCOME_LINE.match(line)
        if match is None:
            continue
        runtime, skill, outcome = match.groups()
        support = (declared.get(skill) or {}).get(DECLARED_RUNTIME[runtime])
        if support is None:
            result.append(f"MATRIX {runtime} {skill} DISAGREES undeclared {quote('it declares no runtime_support')}")
            continue
        want = expected(support)
        if outcome == want:
            result.append(f"MATRIX {runtime} {skill} AGREES {support.level}")
        else:
            reason = f"{support.level} expects {want}, but the run was {outcome}"
            result.append(f"MATRIX {runtime} {skill} DISAGREES {support.level} {quote(reason)}")
    return result


def environment(base: Mapping[str, str], script_log: Path, home: Path) -> dict[str, str]:
    """The runtime's own environment, sign-in included, plus the Python and Bash recorders."""
    recorder = home / STATE / "recorder"
    existing = base.get("PYTHONPATH")
    return {
        **base,
        "PYTHONPATH": f"{recorder}{os.pathsep}{existing}" if existing else str(recorder),
        "BASH_ENV": forward(recorder / "bash_env.sh"),
        SCRIPT_LOG: str(script_log),
    }


def user_only(home: Path, skill: str) -> bool:
    """Whether the skill's runtime adapter in the canary home lets only the user start it."""
    try:
        document = frontmatter.read(home / ".agents" / "skills" / skill / "SKILL.md")
        return (document.string("disable-model-invocation") or "").casefold() == "true"
    except (OSError, UnicodeError, frontmatter.FrontmatterError):
        return False


def unsupported(runtime: str, skill: str, home: Path) -> str:
    """Why the runtime cannot start the skill from the canary's headless prompt at all, or "" when it can."""
    return USER_ONLY_UNSUPPORTED.get(runtime, "") if user_only(home, skill) else ""


def prompt(runtime: str, skill: str) -> str:
    return f"{MENTION[runtime]}{skill} {INSTRUCTION}"


def supplied(runtime: str, codex_sandbox: str) -> list[str]:
    """The `key=value` settings the canary passes a runtime in place of the user configuration it ignores."""
    return [f"windows.sandbox={codex_sandbox}"] if runtime == "codex" else []


def run_command(
    runtime: str, executable: str, skill: str, home: Path, codex_sandbox: str = DEFAULT_CODEX_SANDBOX
) -> list[str]:
    text = prompt(runtime, skill)
    if runtime == "claude":
        # Project settings only, so the installed skills under ~/.claude/skills do not load beside the canary's. No
        # permission flag: the skill's allowed-tools must grant the command, as it would for a user.
        return [
            executable,
            "-p",
            text,
            "--output-format",
            "stream-json",
            "--verbose",
            "--setting-sources",
            "project,local",
            "--strict-mcp-config",
            "--no-session-persistence",
        ]
    if runtime == "codex":
        overrides = [item for setting in supplied(runtime, codex_sandbox) for item in ("-c", setting)]
        return [
            executable,
            "exec",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--sandbox",
            "workspace-write",
            *overrides,
            "--json",
            "-C",
            str(home),
            text,
        ]
    # The isolation of the Copilot review host in code-review-core, except --disallow-temp-dir, since the home is
    # a temporary directory, and with shell allowed only for Python and Bash, which is what the skills run.
    return [
        executable,
        "--no-custom-instructions",
        "--no-ask-user",
        "--no-remote",
        "--no-remote-export",
        "--disable-builtin-mcps",
        "--stream=off",
        "--output-format=json",
        "--allow-tool=shell(python:*)",
        "--allow-tool=shell(bash:*)",
        "--deny-tool=write",
        "--deny-tool=url",
        "--deny-tool=memory",
        "-p",
        text,
    ]


def claude_stream(output: str) -> tuple[set[str] | None, list[str]]:
    """The skills Claude Code's init event lists, if it printed one, and the permissions its result denied."""
    skills: set[str] | None = None
    denials: list[str] = []
    for line in output.splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            event = json.loads(line)
            if not isinstance(event, dict):
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                skills = {str(name).lstrip("/") for name in event.get("skills") or []}
            if event.get("type") == "result":
                for denial in event.get("permission_denials") or []:
                    tool_input = denial.get("tool_input") or {}
                    detail = tool_input.get("command") or json.dumps(tool_input, sort_keys=True)
                    denials.append(f"{denial.get('tool_name', '?')}: {detail}")
    return skills, denials


# Codex logs each command its execution policy refuses: "`<command>` rejected: blocked by policy".
CODEX_REJECTED = re.compile(r"` rejected: ([a-z][a-z ]*[a-z])")


def rejections(runtime: str, completed: Completed) -> list[str]:
    """The reasons the runtime's own policy gave for refusing commands, before any model judgment."""
    return CODEX_REJECTED.findall(completed.stderr) if runtime == "codex" else []


def blocked_cause(runtime: str, codex_sandbox: str) -> str:
    """The likely reason a runtime's policy refused every command, and where the fix is documented."""
    if runtime != "codex":
        return ""
    return (
        f"Codex ran with windows.sandbox={codex_sandbox}, so its Windows sandbox likely did not start; "
        f"see {CODEX_SUPPORT}"
    )


def discovery_lines(runtime: str, skills: list[str], found: discovery.Listing, home: Path) -> list[str]:
    """DISCOVERED for the canary's copy listed enabled, as `deploy.py verify` requires for FOUND, SHADOWED for each
    other enabled copy, DISABLED for each copy the runtime lists turned off, and UNDISCOVERED without the first."""
    adapters = home / ".agents" / "skills"
    lines = []
    for skill in skills:
        copies = found.get(skill, [])
        canary = [copy.directory for copy in copies if copy.enabled and inside(copy.directory, adapters)]
        lines += [f"DISCOVERED {runtime} {skill} {quote(forward(path))}" for path in canary]
        for copy in copies:
            if not copy.enabled:
                lines.append(f"DISABLED {runtime} {skill} {quote(forward(copy.directory))}")
            elif copy.directory not in canary:
                lines.append(f"SHADOWED {runtime} {skill} {quote(forward(copy.directory))}")
        if not canary:
            lines.append(f"UNDISCOVERED {runtime} {skill}")
    return lines


def recorded_scripts(started: list[dict]) -> list[tuple[str, str, int | None]]:
    """Each script file the recorders saw start, with its directory and the status it exited with, if recorded."""

    def key(record: dict) -> str:
        return json.dumps([record.get("argv"), record.get("cwd"), record.get("pid")])

    exits = {
        key(record): record["exit"]
        for record in started
        if isinstance(record.get("exit"), int) and not isinstance(record["exit"], bool)
    }
    scripts = []
    for record in started:
        argv = record.get("argv") or []
        if "exit" in record or not argv or not isinstance(argv[0], str) or argv[0] in ("", "-c", "-"):
            continue
        cwd = str(record.get("cwd", ""))
        scripts.append((forward(Path(cwd) / argv[0] if cwd else argv[0]), forward(cwd), exits.get(key(record))))
    return scripts


def ran_line(skill: str, script: str, cwd: str, status: int | None, timed_out: bool) -> str:
    """RAN with the exit status when it was recorded and `timed-out` when the session ran out of time afterwards, or
    FAILED for the fixture when it exited otherwise than 0."""
    if skill == FIXTURE_SKILL and status not in (None, 0):
        return f"FAILED {quote(f'ran {script} but it exited with code {status}')}"
    exited = "" if status is None else f" exit={status}"
    return f"RAN {quote(script)} {quote(cwd)}{exited}{' timed-out' if timed_out else ''}"


def verdict(
    skill: str,
    home: Path,
    *,
    probes: list[dict],
    started: list[dict],
    completed: Completed,
    denials: list[str],
    timeout: float,
    allowed: list[str] | None = None,
    installed: Path | None = None,
    rejected: Sequence[str] = (),
    cause: str = "",
) -> str:
    """RAN with the script and directory, BLOCKED by the runtime's policy, or FAILED with what went wrong.

    `started` holds the Python and Bash recorders' records: one when a script starts and, when the recorder saw it
    end, the same record again with its `exit` status. A script counts when it is under the canary's copy of the
    skill or of a skill in `allowed`, its skill_deps, whatever it exited with, since the model chooses the arguments;
    RAN carries the status and whether the session then timed out. Only the fixture, whose arguments the canary
    knows, must also exit 0.
    """
    skills_root = home / ".claude" / "skills"
    directories = [skills_root / name for name in (allowed or [skill])]
    scripts = recorded_scripts(started)
    ran = [script for script in scripts if any(inside(script[0], directory) for directory in directories)]
    for record in probes:
        if inside(str(record.get("script", "")), skills_root / skill):
            script = forward(record["script"])
            status = next((status for path, _, status in ran if lexical(path) == lexical(script)), None)
            return ran_line(skill, script, forward(record.get("cwd", "")), status, completed.timed_out)
    if ran and skill == FIXTURE_SKILL:
        return f"FAILED {quote(f'ran {ran[0][0]} but it wrote no marker; was the write denied?')}"
    if ran:
        return ran_line(skill, *ran[0], completed.timed_out)
    installed = installed or Path.home() / ".claude" / "skills"
    for script, _, _ in scripts:
        if inside(script, installed):
            return f"FAILED {quote(f'ran the installed copy {script}, not the canary copy')}"
    if scripts:
        return f"FAILED {quote(f'ran {scripts[0][0]} instead of a script under {forward(skills_root / skill)}')}"
    if rejected:
        count = f"{len(rejected)} command{'' if len(rejected) == 1 else 's'}"
        reason = f"{rejected[0]}; the runtime refused {count} before any script ran"
        return f"BLOCKED {quote(f'{reason}; {cause}' if cause else reason)}"
    if completed.timed_out:
        reason = f"timed out after {timeout:g} seconds before running a script from the skill"
    elif completed.returncode != 0:
        reason = f"exited with code {completed.returncode} before running a script from the skill"
    else:
        reason = "never ran a script from the skill"
    if denials:
        reason += f"; denied: {'; '.join(denials)}"
    return f"FAILED {quote(reason)}"


def run_skill(
    runtime: str,
    executable: str,
    skill: str,
    home: Path,
    base: Mapping[str, str],
    runner: Runner,
    timeout: float,
    allowed: list[str],
    codex_sandbox: str = DEFAULT_CODEX_SANDBOX,
) -> list[str]:
    state = home / STATE
    probe_log = state / "probe.jsonl"
    script_log = state / "scripts" / f"{runtime}-{skill}.jsonl"
    # Created empty here, so the canary owns them and the probe and recorders only append. A file the Codex Windows
    # sandbox creates belongs to its sandbox user, and the permissions that sandbox sets leave it unreadable here.
    probe_log.write_text("", encoding="utf-8")
    script_log.write_text("", encoding="utf-8")
    completed = runner(
        run_command(runtime, executable, skill, home, codex_sandbox), home, environment(base, script_log, home), timeout
    )
    transcript = state / "transcripts" / f"{runtime}-{skill}.jsonl"
    transcript.write_text(completed.stdout, encoding="utf-8")
    if completed.stderr:
        transcript.with_suffix(".stderr.txt").write_text(completed.stderr, encoding="utf-8")
    lines = []
    denials: list[str] = []
    if runtime == "claude":
        listed, denials = claude_stream(completed.stdout)
        if listed is not None:
            lines.append(f"DISCOVERED claude {skill}" if skill in listed else f"UNDISCOVERED claude {skill}")
    try:
        probes, started = read_records(probe_log), read_records(script_log)
    except OSError as exc:
        reason = f"cannot read what the run recorded in {forward(exc.filename or probe_log)}: {exc.strerror or exc}"
        outcome = f"FAILED {quote(reason)}"
    else:
        outcome = verdict(
            skill,
            home,
            probes=probes,
            started=started,
            completed=completed,
            denials=denials,
            timeout=timeout,
            allowed=allowed,
            rejected=rejections(runtime, completed),
            cause=blocked_cause(runtime, codex_sandbox),
        )
    return [
        *lines,
        f"RUNTIME {runtime} {skill} {outcome} KEPT {quote(KEPT[runtime])}",
        f"TRANSCRIPT {runtime} {skill} {quote(forward(transcript))}",
    ]


def check_runtime(
    runtime: str,
    executable: str,
    skills: list[str],
    home: Path,
    base: Mapping[str, str],
    runner: Runner,
    discovery_only: bool,
    timeout: float,
    closure: Mapping[str, list[str]],
    codex_sandbox: str = DEFAULT_CODEX_SANDBOX,
    talk: discovery.Converse | None = None,
    declared: Declarations | None = None,
) -> list[str]:
    lines = []
    # Claude Code has no listing; its run reports the skills it found.
    if runtime in discovery.RUNTIMES:
        listing_environment = environment(base, home / STATE / "scripts" / "listing.jsonl", home)
        try:
            found = discovery.list_skills(runtime, executable, home, listing_environment, timeout, talk)
            lines += discovery_lines(runtime, skills, found, home)
        except discovery.ListingError as exc:
            lines.append(f"DISCOVERY_FAILED {runtime} {quote(f'cannot read its skill list: {exc}')}")
    if discovery_only:
        return lines
    lines += [f"SUPPLIED {runtime} {quote(setting)}" for setting in supplied(runtime, codex_sandbox)]
    for skill in skills:
        support = ((declared or {}).get(skill) or {}).get(DECLARED_RUNTIME[runtime])
        if support is not None and support.level == runtime_support.NONE:
            lines.append(f"RUNTIME {runtime} {skill} NOT_ATTEMPTED {quote(support.reason)}")
            continue
        reason = unsupported(runtime, skill, home)
        if reason:
            lines.append(f"RUNTIME {runtime} {skill} UNSUPPORTED {quote(reason)}")
            continue
        lines += run_skill(
            runtime, executable, skill, home, base, runner, timeout, closure.get(skill, [skill]), codex_sandbox
        )
    return lines


def canary(
    skills: list[str],
    runtimes: list[str],
    home: Path,
    *,
    sources: list[Path],
    runner: Runner | None = None,
    which: Callable[[str], str | None] = shutil.which,
    base_environment: Mapping[str, str] | None = None,
    discovery_only: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    codex_sandbox: str = DEFAULT_CODEX_SANDBOX,
    talk: discovery.Converse | None = None,
) -> int:
    """Deploy the sources into home, then check each runtime; print the facts and return the exit code."""
    runner = runner or run_process
    base = dict(os.environ if base_environment is None else base_environment)
    state = home / STATE
    print(f"HOME {quote(forward(home))}", flush=True)
    for source_dir in sources:
        identifier = source_id(source_dir)
        code, log = deploy(home, source_dir)
        if code != 0:
            state.mkdir(exist_ok=True)
            log_file = state / f"deploy-{identifier.replace('/', '-')}.log"
            log_file.write_text(log, encoding="utf-8")
            print(f"DEPLOY_FAILED {identifier} {quote(forward(log_file))}")
            return 1
        print(f"DEPLOYED {identifier}", flush=True)
    for folder in ("recorder", "scripts", "transcripts"):
        (state / folder).mkdir(parents=True, exist_ok=True)
    (state / "recorder" / "sitecustomize.py").write_text(RECORDER, encoding="utf-8")
    # Line feeds only: Git Bash reads a carriage return as part of the command.
    (state / "recorder" / "bash_env.sh").write_text(BASH_RECORDER, encoding="utf-8", newline="\n")
    ordered = [FIXTURE_SKILL, *(skill for skill in dict.fromkeys(skills) if skill != FIXTURE_SKILL)]
    closure = dependencies(sources)
    declared = declarations(sources)
    printed = []
    for runtime in runtimes:
        executable = which(runtime)
        if executable is None:
            print(f"SKIPPED {runtime} {quote(f'{runtime} is not on PATH')}")
            continue
        for line in check_runtime(
            runtime,
            executable,
            ordered,
            home,
            base,
            runner,
            discovery_only,
            timeout,
            closure,
            codex_sandbox,
            talk,
            declared,
        ):
            print(line, flush=True)
            printed.append(line)
    matrix = compare(printed, declared)
    for line in matrix:
        print(line, flush=True)
    undiscovered = any(line.startswith("UNDISCOVERED ") for line in printed)
    return 1 if undiscovered or any(" DISAGREES " in line for line in matrix) else 0


def create_home() -> Path:
    return Path(tempfile.mkdtemp(prefix=HOME_PREFIX)).resolve()


def remove_home(directory: str) -> int:
    """Delete a home the canary made, a HOME_PREFIX directory directly in the temporary directory, and nothing else."""
    home = Path(directory)
    temporary = Path(tempfile.gettempdir()).resolve()
    resolved = home.resolve()
    if (
        home.is_symlink()
        or not resolved.is_dir()
        or resolved.parent != temporary
        or not resolved.name.startswith(HOME_PREFIX)
    ):
        print(f"FAILED {quote(f'{forward(home)} is not a home the canary made under {forward(temporary)}')}")
        return 1
    try:
        shutil.rmtree(resolved)
    except OSError as exc:
        print(f"FAILED {quote(f'cannot remove {forward(resolved)}: {exc}')}")
        return 1
    print(f"REMOVED {quote(forward(resolved))}")
    return 0


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python tools/runtime_canary.py",
        description="Deploy into a throwaway home and check that each runtime finds and runs the skills.",
    )
    parser.add_argument("skills", nargs="*", metavar="SKILL", help="shipped skills to check besides the fixture")
    parser.add_argument(
        "--runtime",
        action="append",
        choices=RUNTIMES,
        dest="runtimes",
        help="check only this runtime; repeatable (default: all three)",
    )
    parser.add_argument("--discovery-only", action="store_true", help="list skills; run no model")
    parser.add_argument("--remove-home", metavar="DIR", help="delete a home an earlier run printed as HOME, and exit")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"limit for each runtime run (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--codex-sandbox",
        choices=CODEX_SANDBOXES,
        default=DEFAULT_CODEX_SANDBOX,
        help=f"Windows sandbox mode Codex runs with (default: {DEFAULT_CODEX_SANDBOX})",
    )
    options = parser.parse_args(arguments)
    if options.remove_home is not None:
        if options.skills:
            parser.error("--remove-home takes no SKILL")
        platform_support.use_utf8_output()
        return remove_home(options.remove_home)
    sources = [REPOSITORY_ROOT, FIXTURE_SOURCE]
    unknown = [skill for skill in options.skills if skill not in dependencies(sources)]
    if unknown:
        parser.error(f"unknown skill: {', '.join(unknown)}")
    platform_support.use_utf8_output()
    return canary(
        options.skills,
        options.runtimes or list(RUNTIMES),
        create_home(),
        sources=sources,
        discovery_only=options.discovery_only,
        timeout=options.timeout,
        codex_sandbox=options.codex_sandbox,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
