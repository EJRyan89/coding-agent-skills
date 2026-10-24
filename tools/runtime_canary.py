"""Deploy skills into a throwaway home and check that Claude Code, Codex, and Copilot CLI find and run them.

Usage:
  python tools/runtime_canary.py [SKILL ...] [--runtime NAME ...] [--discovery-only] [--timeout SECONDS]
                                 [--codex-sandbox mxc|elevated|unelevated] [--attempts RUNS] [--keep-home]
  python tools/runtime_canary.py --changed-since [REF]
  python tools/runtime_canary.py --remove-home DIR

It makes a fresh home under the temporary directory and deploys this checkout into it with `deploy.py
--canary-home`, which a linked worktree may do, then deploys the fixture source in tests/fixtures/runtime-canary.
That home is the only place anything is deployed. Each installed runtime then starts with the home as its working
directory, so it finds the deployed skills as project skills, and with a fixed prompt naming one skill: the fixture
runtime-canary-probe, then each SKILL given. It prints one fact per line:

  HOME "<dir>"                                  the throwaway home, removed at the end unless the run failed
  DEPLOYED <source id>                          or DEPLOY_FAILED <source id> "<log>", which stops the run
  SKIPPED <runtime> "<reason>"                  the runtime is not installed where `deploy.py check` looks
  DISCOVERED <runtime> <skill> ["<path>"]       the runtime lists the canary's copy, enabled
  SHADOWED <runtime> <skill> "<path>"           it also lists another enabled copy, such as an installed adapter
  PROJECT_COPY <runtime> <skill> "<path>"       it lists the canary home's own deployed skill as a project skill
  DISABLED <runtime> <skill> "<path>"           it lists this copy turned off in its settings
  UNDISCOVERED <runtime> <skill> ["<reason>"]   it does not list the canary's copy enabled; the canary exits 1
  DISCOVERY_NOT_ATTEMPTED <runtime> <skill> "<reason>"  the deployer gave the skill no adapter, so none is looked for
  DISCOVERY_FAILED <runtime> "<reason>"         it cannot read the runtime's skill list; the canary exits 1
  SUPPLIED <runtime> "<key>=<value>"            a setting the canary passes in place of the configuration it ignores
  RUNTIME <runtime> <skill> RAN "<script>" "<cwd>" [exit=<n>] [timed-out] KEPT "<ambient>"
  RUNTIME <runtime> <skill> UNCOUNTED "<reason>" KEPT "<ambient>"  it ran the real profile's installed copy
  RUNTIME <runtime> <skill> BLOCKED "<policy>" KEPT "<ambient>"
  RUNTIME <runtime> <skill> FAILED "<reason>" KEPT "<ambient>"
  RUNTIME <runtime> <skill> UNSUPPORTED "<reason>"  the runtime cannot start the skill headless, so it never ran
  RUNTIME <runtime> <skill> NOT_ATTEMPTED "<reason>"  the skill declares none for the runtime, so it never ran
  TRANSCRIPT <runtime> <skill> "<path>"         the runtime's output for that run
  MATRIX <runtime> <skill> AGREES <level>       what ran is what the skill's declared runtime support expects
  MATRIX <runtime> <skill> DISAGREES <level> "<what ran instead>"
  NOTHING_CHECKED "<reason>"                    no runtime was listed or run, with why for each; the canary exits 1
  RECORD_BEGIN ... RECORD_END                   the lines a pull request records, below
  REMOVED "<dir>"                               the home, removed after a run that passed
  HOME_KEPT "<dir>" "<reason>"                  or kept, after a run that failed or with --keep-home

A SKILL that no source names is a usage error, exit 2, before a home is made, while every source can be read; when
one cannot, a SKILL is not judged by it, and the run's deployment reports the deployer's reason as DEPLOY_FAILED.

The record between RECORD_BEGIN and RECORD_END is what a pull request's Validation section holds: a VERSION
<runtime> <version> line for each runtime found, the version its --version prints or unknown, then each DEPLOY_FAILED,
SKIPPED, discovery, SUPPLIED, RUNTIME, MATRIX, and NOTHING_CHECKED line, with every path rewritten so it names no
machine: the throwaway home as <temp>, this checkout as <source>, and the profile folder the runtimes read as <home>.
The home is removed after a run that exits 0; after one that fails, or with --keep-home, it stays with its transcripts,
and --remove-home deletes it later. A home that cannot be removed is FAILED "<reason>" and exit 1.

With --remove-home DIR it prints REMOVED "<dir>", or FAILED "<reason>" and exits 1.

With --changed-since REF it starts nothing and makes no home: it reads, through git, every file changed since the commit
REF and HEAD share, committed or not, untracked included, and prints SINCE <ref> <commit>, then CHANGED <skill> "<why>"
for each shipped skill the change reaches, or NOTHING_CHANGED "<reason>". REF defaults to the last release HEAD holds,
as ejryan89-skill-core's release_tags.py defines it, the range a release checks; a nearer tag of another name, such as a
pre-release, does not move it, and with no release tag reaching HEAD it prints FAILED. A skill is reached by a changed
file under skills/<skill>/, its deploy-meta/<skill>.json, a shared asset under skills/ that it declares in shared_deps,
or an agents/<agent>.md that it declares in agent_deps. A hidden dependency, which has no adapter and no run of its own,
reaches instead the skills that declare it in skill_deps, directly or through another hidden one. A change to
deployer/render.py, which writes every adapter, reaches one skill the model may start and one only the user may start, a
changed one where it can. A git failure is FAILED "<reason>" and exit 1.

The isolation is of configuration, not of credentials: each runtime keeps its real configuration folder and so
its sign-in, and the canary switches off what each runtime lets it: Claude Code's user settings, skills, and MCP
servers; Codex's config.toml and rules; Copilot's custom instructions and built-in MCP servers. KEPT names what
still loads, among it the personal skills Codex and Copilot keep listing, and no flag keeps a model's shell from
reading the installed copies under the profile's skill folders. So a run that started a script there, from
.claude/skills, .agents/skills, .codex/skills, or .copilot/skills under the profile, is UNCOUNTED: it tested nothing,
and the canary runs the skill again, up to --attempts runs in all. Each such run keeps its own RUNTIME and
TRANSCRIPT lines, a run after the first naming its transcript -run<n>, so the record shows how many runs the skill
took, and MATRIX compares only the last; one still UNCOUNTED disagrees with every declared level. Codex on Windows
refuses every command without a Windows sandbox mode, which it would otherwise read from config.toml, so the canary
supplies that one value itself (--codex-sandbox mxc, elevated, or unelevated; default elevated) and prints it as
SUPPLIED. It also sets Codex's approval policy to never, as a non-interactive run should.
Every prompt carries RUNTIME_CANARY_MARKER, so a session the runtime saves can be found later.

The canary judges nothing from a transcript. The fixture's script writes a marker from its own location, carrying
the identifier the canary writes into the home before each run, a sitecustomize.py on PYTHONPATH records every
Python script the runtime starts, and a BASH_ENV file records every Bash script, however the runtime names Bash. A
recorder counts a script only when it names a file the interpreter can open: a missing file, a folder, `python -c`,
and `python -m` are no script. RAN means the script ran from the canary's copy of the skill or of a skill it
declares in skill_deps, whatever it exited with; a script from the installed copy is UNCOUNTED, never RAN. RAN adds
exit=<n> when a recorder saw the script end (Python 3.12 or later, where a script that does not compile exits 1; a
Bash script that sets no EXIT trap of its own) and timed-out when the session ran out of time after the script
started. The fixture must also exit 0, or it is FAILED with the status, which it is when it found no run identifier
to write. A records file holding a whole line the canary will not read, such as an integer past Python's digit limit
or JSON nested past the recursion limit, fails that run naming the file and the line. A run ends at --timeout, or on
Ctrl+C, with every process the runtime started, and a runtime the canary cannot start for a skill, as when its own
update has moved its program away, is FAILED with why; each line prints as it is made, so none before it is lost.
Copilot's headless mode cannot start a skill only the user may start, so such a skill is UNSUPPORTED there and no model
is called for it.
--discovery-only lists skills without running any model. --remove-home deletes a home an earlier run printed and
nothing else: a runtime-canary- directory directly under the temporary directory that holds the
.runtime-canary/home.json marker each run writes, naming that home; it refuses any other with FAILED.

Each skill's runtime_support in deploy-meta, the matrix docs/skills.md prints, says what to expect: `full` must be
RAN, `partial` must be RAN or, when the runtime lacks user-only-start, UNSUPPORTED, since the limits of the other
capabilities lie past the first script the canary asks for, and `none` is NOT_ATTEMPTED. After the runs, one MATRIX
line per RUNTIME line compares the two, and the canary exits 1 when any disagrees or a skill declares nothing, so the
matrix cannot claim more than a run shows. It also exits 1 on any UNDISCOVERED or DISCOVERY_FAILED line,
--discovery-only included, since a runtime whose skill list cannot be read is one whose discovery shows nothing, and
on NOTHING_CHECKED, which it prints when no runtime it was asked to check printed a DISCOVERED or UNDISCOVERED line
or a RUNTIME line for a run it started: each runtime was SKIPPED, could not be listed, was asked for no skill it
could list or start, or, under --discovery-only, is Claude Code, whose discovery shows only in a model run.

Discovery is held to what `deploy.py verify` holds: the adapters the canary home's manifest records. A skill deployed
with none, as the deployer deploys a hidden dependency such as ejryan89-code-review-core, is DISCOVERY_NOT_ATTEMPTED,
never UNDISCOVERED; a release covers it through the skills that declare it in skill_deps. Since each runtime runs with
the home as its working directory, one may list the home's own .claude/skills copy of a skill as a project skill, as
Copilot does for a skill with no adapter. That copy is PROJECT_COPY, not SHADOWED: no session started outside the
home sees it, and it fails nothing.

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
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

import frontmatter
import json_reader
import runtime_support
from bounded_process import run_bounded, without_repository_variables
from console import run_main
from file_presence import is_present
from git_client import GitClient, GitError
from link_probe import is_plain_directory
from release_tags import last_release
from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))
from review_hosts import COPILOT_SYNC_SESSION_VERSION, copilot_local_session

from deployer import discovery, manifest, pipeline, platform_support, verify
from deployer import source as deploy_source
from deployer.errors import DeployError
from deployer.paths import Paths
from deployer.tools import format_version, parse_version

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

RUNTIMES = ("claude", "codex", "copilot")
# Each runtime's name in a skill's runtime_support declaration.
DECLARED_RUNTIME = {"claude": "claude-code", "codex": "codex", "copilot": "copilot-cli"}
# The outcome a partial skill reaches where the runtime lacks the capability; any other limit lies past the first
# script the canary asks for, so the skill still runs that far.
PARTIAL_OUTCOME = {"user-only-start": "UNSUPPORTED"}
OUTCOME_LINE = re.compile(r"^RUNTIME (\S+) (\S+) ([A-Z_]+)\b")
# The RUNTIME outcomes the canary reaches without starting the runtime, which therefore check nothing.
UNSTARTED = ("NOT_ATTEMPTED", "UNSUPPORTED")
FIXTURE_SOURCE = REPOSITORY_ROOT / "tests" / "fixtures" / "runtime-canary"
FIXTURE_SKILL = "runtime-canary-probe"
STATE = ".runtime-canary"
HOME_PREFIX = "runtime-canary-"
# The file in STATE that names a home the canary made, which --remove-home requires before it deletes anything.
HOME_MARKER = "home.json"
# The file in STATE holding the run's identifier, new for each run, which the fixture's probe copies into its marker.
RUN_ID = "run-id"
DEFAULT_TIMEOUT = 600
# The limit on the `--version` call that reads each runtime's version for the record.
VERSION_TIMEOUT = 60
# Set for every runtime the canary starts; only Copilot CLI reads it. Copilot otherwise runs the newest package it has
# downloaded rather than the installed one, so the version the record names could differ from the one the runs used.
PINNED_VERSION = {"COPILOT_AUTO_UPDATE": "false"}
# The line kinds the record holds, besides each runtime's VERSION: what a pull request's Validation section needs to
# show what each runtime found and ran. HOME, DEPLOYED, and TRANSCRIPT name only this machine's throwaway files.
RECORDED = (
    "DEPLOY_FAILED",
    "SKIPPED",
    "DISCOVERED",
    "UNDISCOVERED",
    "DISCOVERY_FAILED",
    "DISCOVERY_NOT_ATTEMPTED",
    "SHADOWED",
    "PROJECT_COPY",
    "DISABLED",
    "SUPPLIED",
    "RUNTIME",
    "MATRIX",
    "NOTHING_CHECKED",
)
# The deployer module that writes every runtime adapter, so a change to it reaches one skill of each kind of start.
ADAPTER_RENDERER = "deployer/render.py"
GIT = GitClient()
RUNTIME_CANARY_MARKER = "RUNTIME-CANARY-RUN"
# What each runtime still loads from the user's real configuration once the canary's flags have switched off all
# they can. Claude Code: checked with /context under --setting-sources project,local. Codex and Copilot: their
# skill listings, the flags each offers, Codex's AGENTS.md page, under which every run reads the Codex home's
# AGENTS.override.md or AGENTS.md while --ignore-user-config skips config.toml only, and Copilot's hooks reference,
# under which user hooks load without admission.
KEPT = {
    "claude": "user CLAUDE.md; auto-memory folder",
    "codex": "global AGENTS.override.md or AGENTS.md in ~/.codex; personal skills in ~/.agents/skills and "
    "~/.codex/skills",
    "copilot": "personal skills in ~/.agents/skills and ~/.copilot/skills; MCP servers in ~/.copilot/mcp-config.json; "
    "user hooks in ~/.copilot/hooks and the hooks key of ~/.copilot/settings.json; session history",
}
# How many times a skill is run on a runtime while each run starts the real profile's installed copy, which no flag
# keeps out of a model's reach, so that no such run counts.
DEFAULT_ATTEMPTS = 3
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
# value is left bare, which Codex reads as a string, so cmd.exe never sees a quote. mxc is the implementation OpenAI
# recommends where Windows offers it, elevated its fallback. See docs/codex-support.md.
CODEX_SANDBOXES = ("mxc", "elevated", "unelevated")
DEFAULT_CODEX_SANDBOX = "elevated"
# codex exec asks for no approval, but the canary says so itself, beside the sandbox, as OpenAI asks of a
# non-interactive run, so a changed default cannot make a run wait for an answer or skip the sandbox.
CODEX_APPROVAL_POLICY = "approval_policy=never"
CODEX_SUPPORT = "docs/codex-support.md#windows-sandbox-mode"
# Whether Codex here has a Windows sandbox mode to be given: on Windows only.
CODEX_HAS_WINDOWS_SANDBOX = platform_support.WINDOWS
# In Copilot CLI 1.0.91, `copilot -p` neither expands a leading /<skill> for a user-only skill nor lets the model's
# skill tool start one. Recheck on each Copilot CLI upgrade, and drop this case once a headless run can start one.
COPILOT_SUPPORT = "docs/copilot-support.md#headless-sessions"
USER_ONLY_UNSUPPORTED = {"copilot": f"copilot -p cannot start a user-only skill; see {COPILOT_SUPPORT}"}
SCRIPT_LOG = "RUNTIME_CANARY_SCRIPT_LOG"
RECORDER = '''"""Written by tools/runtime_canary.py: record each Python script a runtime starts and how it exited."""
import atexit
import json
import os
import sys

# A copy, since runpy rewrites sys.argv in place. The exit record repeats the start record, so the two pair up.
_record = {"argv": list(sys.argv), "cwd": os.getcwd(), "pid": os.getpid()}
_started = []
_unwound = []


def _write(record):
    with open(os.environ["RUNTIME_CANARY_SCRIPT_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\\n")


def _script():
    """Whether Python was given a script file it can open: not -c, -m, or standard input, which name no script,
    and not a missing file or a folder, which Python refuses before running anything."""
    first = _record["argv"][0] if _record["argv"] else ""
    if first in ("", "-", "-c", "-m"):
        return False
    path = os.path.join(_record["cwd"], first)
    try:
        with open(path, "rb"):
            pass
    except OSError:
        return False
    return os.path.isfile(path)


def _main(code):
    main = getattr(sys.modules.get("__main__"), "__file__", None)
    return code.co_name == "<module>" and main and os.path.abspath(code.co_filename) == os.path.abspath(main)


def _starting(code, offset):
    if _main(code):
        _started.append(code)
    return _monitoring.DISABLE


def _unwinding(code, offset, exception):
    if _main(code):
        _unwound.append(exception)


def _exited():
    exception = _unwound[-1] if _unwound else None
    if isinstance(exception, KeyboardInterrupt):
        return
    if isinstance(exception, SystemExit):
        code = exception.code
        status = 0 if code is None else code if isinstance(code, int) else 1
    else:
        # An exception left the main module, or Python never started it because the file did not compile.
        status = 1 if exception is not None or not _started else 0
    try:
        _write({**_record, "exit": status})
    except Exception:
        pass


try:
    if _script():
        _write(_record)
        # sys.monitoring, from Python 3.12, reports the main module starting once Python has compiled it and
        # leaving by an exception, SystemExit included. Its start event is switched off for each function after
        # the first call, and the unwind event costs nothing while none is raised. Without it, or with its tool
        # slot taken, the exit goes unrecorded.
        _monitoring = sys.monitoring
        _monitoring.use_tool_id(4, "runtime-canary")
        _monitoring.register_callback(4, _monitoring.events.PY_START, _starting)
        _monitoring.register_callback(4, _monitoring.events.PY_UNWIND, _unwinding)
        _monitoring.set_events(4, _monitoring.events.PY_START | _monitoring.events.PY_UNWIND)
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
# any BASH_ENV of the user's, which is ambient configuration the canary leaves out. Bash reads it before it opens the
# script, so a script that is not a readable file, which Bash then refuses, is left out too. Its EXIT trap records
# the status the script exits with, unless the script replaces the trap with its own; a subshell inherits no EXIT
# trap.
BASH_RECORDER = r"""# Written by tools/runtime_canary.py: record each Bash script a runtime starts and how it exited.
if [ -z "${BASH_EXECUTION_STRING+set}" ] && [ -n "${RUNTIME_CANARY_SCRIPT_LOG-}" ] && [ -f "$0" ] && [ -r "$0" ]
then
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
    # Why the run has no exit status of its own: the program could not start, as when an update moved it away, or
    # the processes it started could not be held or ended. Empty for a run that ended by itself or timed out.
    error: str = ""


# Runs a program and returns what it did, a failure to start it included, and never raises for the run itself.
Runner = Callable[[list[str], Path, dict[str, str], float], Completed]


def text(value: bytes | None) -> str:
    """Output read back as text, with each line ending in a line feed, as a text-mode pipe would give it."""
    return (value or b"").decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


def run_process(arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Completed:
    """Run a runtime for at most `timeout` seconds with no stdin, which Codex exec would read a prompt addition from.

    The run is ejryan89-skill-core's bounded one in a process tree, so its limit and an interrupt end every process it
    started, such as the runtime a launcher script like npm's codex.cmd starts, which a kill of the launcher alone would
    leave running with its model session, and its output goes to files, which nothing waits on such a process to close.
    """
    try:
        finished = run_bounded(arguments, timeout, cwd=cwd, env=environment, tree=platform_support.process_tree())
    except subprocess.TimeoutExpired as exc:
        return Completed(-1, text(exc.stdout), text(exc.stderr), timed_out=True)
    except OSError as exc:
        return Completed(-1, "", "", error=f"cannot run {forward(arguments[0])}: {exc.strerror or exc}")
    return Completed(finished.returncode, text(finished.stdout), text(finished.stderr))


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


class RecordRefused(ValueError):
    """A records file holding a whole line the canary will not read, with the file and why."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def read_records(path: Path) -> list[dict[str, Any]]:
    """The JSON objects in a JSON Lines file, skipping any line a crashed writer left incomplete. The canary creates
    each records file empty before the run, so an empty or missing file is a run that recorded nothing, which the
    verdict reports as no marker; a directory or anything else in the file's place raises NotAFile, an OSError, which
    the run reports as a record it cannot read. A line that is JSON json_reader refuses, or holds an integer past
    Python's digit limit, raises RecordRefused, naming the file and the line."""
    records = []
    if is_present(path):
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            try:
                record = json_reader.loads(line)
            except json.JSONDecodeError:
                continue
            except ValueError as exc:
                raise RecordRefused(path, f"line {number}: {exc}") from exc
            if isinstance(record, dict):
                records.append(record)
    return records


def unreadable(exc: OSError | RecordRefused, path: Path) -> str:
    """Why the records a run wrote cannot be read, naming the file."""
    if isinstance(exc, RecordRefused):
        return f"cannot read what the run recorded in {forward(exc.path)}: {exc.reason}"
    return f"cannot read what the run recorded in {forward(exc.filename or path)}: {exc.strerror or exc}"


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


def discoverable(source_dir: Path) -> bool:
    """Whether the deployer can read the source, so its skills are known."""
    paths = Paths(source_dir, source_dir)
    try:
        deploy_source.discover(paths, deploy_source.load_source_id(paths))
    except DeployError:
        return False
    return True


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


def without_adapter(home: Path) -> dict[str, str]:
    """Each skill deployed into the canary home with no runtime adapter, as the deployer deploys a hidden dependency,
    with why no runtime is asked to list it. `deploy.py verify` looks for the adapters the manifest records and no
    other, so neither does the canary."""
    owned = manifest.load(Paths(REPOSITORY_ROOT, home).manifest_file)
    adapters = set(verify.adapter_names(owned))
    reason = "the deployer gave it no runtime adapter, so deploy.py verify expects no runtime to list it"
    return dict.fromkeys(sorted(set(owned.skill_owners) - adapters), reason)


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


def outcome_of(line: str) -> str | None:
    """The outcome a RUNTIME line names, or None for any other line."""
    match = OUTCOME_LINE.match(line)
    return match.group(3) if match else None


def compare(lines: Sequence[str], declared: Declarations) -> list[str]:
    """One MATRIX line per runtime and skill, from its last RUNTIME line, since an UNCOUNTED run is followed by
    another: whether what ran is what the skill's declared support expects."""
    last: dict[tuple[str, str], str] = {}
    for line in lines:
        match = OUTCOME_LINE.match(line)
        if match is not None:
            last[match.group(1), match.group(2)] = match.group(3)
    result = []
    for (runtime, skill), outcome in last.items():
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


def checks(line: str) -> bool:
    """Whether the line shows its runtime checked: a listing that found the canary's copy or missed it, or a run the
    canary started. DISCOVERY_FAILED read nothing, and NOT_ATTEMPTED and UNSUPPORTED started nothing."""
    match = OUTCOME_LINE.match(line)
    if match is not None:
        return match.group(3) not in UNSTARTED
    return line.startswith(("DISCOVERED ", "UNDISCOVERED "))


def unchecked(runtime: str, lines: Sequence[str], discovery_only: bool) -> str:
    """Why a runtime whose lines include none that checks was not checked."""
    if any(line.startswith(f"DISCOVERY_FAILED {runtime} ") for line in lines):
        return f"{runtime}'s skill list could not be read"
    if runtime not in discovery.RUNTIMES and discovery_only:
        return (
            f"{runtime} has no skill listing, so its discovery needs a model run, which --discovery-only does not start"
        )
    return f"{runtime} was asked for no skill it could list or start"


def environment(base: Mapping[str, str], script_log: Path, home: Path) -> dict[str, str]:
    """The runtime's own environment, sign-in included, plus the Python and Bash recorders and PINNED_VERSION, without
    git's repository-selecting variables, so the runtime and the skill it runs read the canary home, not the
    repository a shell's `GIT_DIR` names."""
    recorder = home / STATE / "recorder"
    existing = base.get("PYTHONPATH")
    return {
        **without_repository_variables(base),
        "PYTHONPATH": f"{recorder}{os.pathsep}{existing}" if existing else str(recorder),
        "BASH_ENV": forward(recorder / "bash_env.sh"),
        SCRIPT_LOG: str(script_log),
        **PINNED_VERSION,
    }


def starts_only_by_user(skill_file: Path) -> bool:
    """Whether the SKILL.md, a skill's or its runtime adapter's, lets only the user start the skill."""
    try:
        document = frontmatter.read(skill_file)
        return (document.string("disable-model-invocation") or "").casefold() == "true"
    except (OSError, UnicodeError, frontmatter.FrontmatterError):
        return False


def user_only(home: Path, skill: str) -> bool:
    """Whether the skill's runtime adapter in the canary home lets only the user start it."""
    return starts_only_by_user(home / ".agents" / "skills" / skill / "SKILL.md")


def unsupported(runtime: str, skill: str, home: Path) -> str:
    """Why the runtime cannot start the skill from the canary's headless prompt at all, or "" when it can."""
    return USER_ONLY_UNSUPPORTED.get(runtime, "") if user_only(home, skill) else ""


def prompt(runtime: str, skill: str) -> str:
    return f"{MENTION[runtime]}{skill} {INSTRUCTION}"


def supplied(runtime: str, codex_sandbox: str) -> list[str]:
    """The `key=value` settings the canary passes a runtime in place of the user configuration it ignores: Codex's
    Windows sandbox mode on Windows, and nothing elsewhere, where Codex has no Windows sandbox to choose."""
    return [f"windows.sandbox={codex_sandbox}"] if runtime == "codex" and CODEX_HAS_WINDOWS_SANDBOX else []


def run_command(
    runtime: str,
    executable: str,
    skill: str,
    home: Path,
    codex_sandbox: str = DEFAULT_CODEX_SANDBOX,
    version: tuple[int, ...] | None = None,
) -> list[str]:
    """The command that runs `skill` on the runtime, whose `--version` named `version`, or nothing it could read."""
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
            "-c",
            CODEX_APPROVAL_POLICY,
            *overrides,
            "--json",
            "-C",
            str(home),
            text,
        ]
    # The isolation of the Copilot review host in ejryan89-code-review-core, except --disallow-temp-dir, since the home
    # is a temporary directory, and with shell allowed only for the platform's Python command and Bash, which is what
    # the skills run. A Copilot whose version cannot be read is taken to be the latest, which it updates itself to.
    return [
        executable,
        "--no-custom-instructions",
        "--no-ask-user",
        *copilot_local_session(version or COPILOT_SYNC_SESSION_VERSION),
        "--disable-builtin-mcps",
        "--stream=off",
        "--output-format=json",
        f"--allow-tool=shell({platform_support.PYTHON}:*)",
        "--allow-tool=shell(bash:*)",
        "--deny-tool=write",
        "--deny-tool=url",
        "--deny-tool=memory",
        "-p",
        text,
    ]


# Every `claude -p` session the canaries and the skill evaluations start depends on what --bare skips: discovery of the
# home's skills and agents, and the user's sign-in. Claude Code's headless page says --bare will become the default for
# -p, and no option keeps discovery yet, so a session whose init event lists no skill fails with this reason rather than
# counting as a result.
NOTHING_DISCOVERED = "Claude Code discovered no skill, as a session in bare mode does"


def discovery_failure(output: str) -> str | None:
    """Why a Claude Code session counts for nothing because it discovered no skill, or None. A session that printed no
    init event has its own failure to report."""
    skills, _ = claude_stream(output)
    return NOTHING_DISCOVERED if skills is not None and not skills else None


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
                names = event.get("skills")
                skills = {str(name).lstrip("/") for name in names} if isinstance(names, list) else set()
            if event.get("type") == "result":
                listed = event.get("permission_denials")
                denials += [denied(entry) for entry in listed] if isinstance(listed, list) else []
    return skills, denials


def denied(entry: object) -> str:
    """One permission denial, `<tool>: <command>`, named from whatever the entry holds, of any JSON type."""
    denial = entry if isinstance(entry, dict) else {"tool_input": entry}
    tool_input = denial.get("tool_input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    detail = command if isinstance(command, str) and command else json.dumps(tool_input, sort_keys=True)
    return f"{denial.get('tool_name', '?')}: {detail}"


# Codex logs each command its execution policy refuses: "`<command>` rejected: blocked by policy", and each its
# sandbox could not start at all in the same CreateProcess error, as `Rejected(\"Failed to create unified exec process:
# <message> | cwd=<dir> | cmd=<command>\")`, whose message alone is kept.
CODEX_REJECTED = re.compile(
    r"` rejected: (?P<policy>[a-z][a-z ]*[a-z])"
    r'|Rejected\(\\?"Failed to create unified exec process: (?P<unstarted>.+?)(?: \| .*?)?\\?"\)'
)


def rejections(runtime: str, completed: Completed) -> list[str]:
    """The reasons the runtime's own policy or sandbox gave for refusing commands, before any model judgment."""
    if runtime != "codex":
        return []
    return [
        match["policy"] or f"its sandbox could not start a process: {match['unstarted']}"
        for match in CODEX_REJECTED.finditer(completed.stderr)
    ]


# Every line Codex logs for a command its tool router could not run, CODEX_REJECTED's shapes among them.
CODEX_EXEC_FAILED = re.compile(r"\berror=exec_command failed: ")


def unread_rejections(runtime: str, completed: Completed) -> list[str]:
    """Each command failure the runtime logged in a shape CODEX_REJECTED does not read, so the verdict names it
    rather than passing over what may be a refusal by its policy or sandbox."""
    if runtime != "codex":
        return []
    return [
        line.strip()
        for line in completed.stderr.splitlines()
        if CODEX_EXEC_FAILED.search(line) and not CODEX_REJECTED.search(line)
    ]


def blocked_cause(runtime: str, codex_sandbox: str) -> str:
    """The likely reason a runtime's policy refused every command, and where the fix is documented."""
    if runtime != "codex" or not CODEX_HAS_WINDOWS_SANDBOX:
        return ""
    return (
        f"Codex ran with windows.sandbox={codex_sandbox}, so its Windows sandbox likely did not start; "
        f"see {CODEX_SUPPORT}"
    )


def discovery_lines(
    runtime: str, skills: list[str], found: discovery.Listing, home: Path, unadapted: Mapping[str, str] | None = None
) -> list[str]:
    """DISCOVERED for the canary's copy listed enabled, as `deploy.py verify` requires for FOUND, SHADOWED for each
    other enabled copy, PROJECT_COPY for the home's own deployed copy listed as a project skill, DISABLED for each copy
    the runtime lists turned off, and UNDISCOVERED without the first, or DISCOVERY_NOT_ATTEMPTED for a skill unadapted
    names, which the deployer gave no adapter."""
    adapters = home / ".agents" / "skills"
    deployed = home / ".claude" / "skills"
    unadapted = unadapted or {}
    lines = []
    for skill in skills:
        copies = found.get(skill, [])
        canary = [copy.directory for copy in copies if copy.enabled and inside(copy.directory, adapters)]
        lines += [f"DISCOVERED {runtime} {skill} {quote(forward(path))}" for path in canary]
        for copy in copies:
            if not copy.enabled:
                lines.append(f"DISABLED {runtime} {skill} {quote(forward(copy.directory))}")
            elif inside(copy.directory, deployed):
                lines.append(f"PROJECT_COPY {runtime} {skill} {quote(forward(copy.directory))}")
            elif copy.directory not in canary:
                lines.append(f"SHADOWED {runtime} {skill} {quote(forward(copy.directory))}")
        if skill in unadapted:
            lines.append(f"DISCOVERY_NOT_ATTEMPTED {runtime} {skill} {quote(unadapted[skill])}")
        elif not canary:
            lines.append(f"UNDISCOVERED {runtime} {skill}")
    return lines


def recorded_scripts(started: list[dict[str, Any]]) -> list[tuple[str, str, int | None]]:
    """Each script file the recorders saw start, with its directory and the status it exited with, if recorded.
    A command string, a module run with -m, and standard input name no script file, so none counts."""

    def key(record: dict[str, Any]) -> str:
        return json.dumps([record.get("argv"), record.get("cwd"), record.get("pid")])

    exits = {
        key(record): record["exit"]
        for record in started
        if isinstance(record.get("exit"), int) and not isinstance(record["exit"], bool)
    }
    scripts = []
    for record in started:
        argv = record.get("argv") or []
        if "exit" in record or not argv or not isinstance(argv[0], str) or argv[0] in ("", "-c", "-m", "-"):
            continue
        cwd = str(record.get("cwd", ""))
        scripts.append((forward(Path(cwd) / argv[0] if cwd else argv[0]), forward(cwd), exits.get(key(record))))
    return scripts


def installed_roots(profile: Path) -> list[Path]:
    """The skill folders of the user's real profile a runtime can still reach, since no flag hides a folder from a
    model's shell: the deployed copies and their adapters, and the personal skills Codex and Copilot load."""
    return [profile / folder / "skills" for folder in (".claude", ".agents", ".codex", ".copilot")]


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
    probes: list[dict[str, Any]],
    started: list[dict[str, Any]],
    completed: Completed,
    denials: list[str],
    timeout: float,
    allowed: list[str] | None = None,
    installed: Sequence[Path] | None = None,
    rejected: Sequence[str] = (),
    unread: Sequence[str] = (),
    cause: str = "",
) -> str:
    """RAN with the script and directory, BLOCKED by the runtime's policy, UNCOUNTED when the run started a script
    under one of the `installed` folders, the real profile's by default, or FAILED with what went wrong.

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
    # The fixture refuses to write a marker it cannot give the run's identifier, and exits 1.
    if ran and skill == FIXTURE_SKILL and ran[0][2] not in (None, 0):
        return ran_line(skill, *ran[0], completed.timed_out)
    if ran and skill == FIXTURE_SKILL:
        return f"FAILED {quote(f'ran {ran[0][0]} but it wrote no marker; was the write denied?')}"
    if ran:
        return ran_line(skill, *ran[0], completed.timed_out)
    roots = installed_roots(platform_support.home_directory()) if installed is None else installed
    for script, _, _ in scripts:
        if any(inside(script, root) for root in roots):
            return f"UNCOUNTED {quote(f'ran the installed copy {script}, not the canary copy')}"
    if scripts:
        return f"FAILED {quote(f'ran {scripts[0][0]} instead of a script under {forward(skills_root / skill)}')}"
    if rejected:
        count = f"{len(rejected)} command{'' if len(rejected) == 1 else 's'}"
        reason = f"{rejected[0]}; the runtime refused {count} before any script ran"
        return f"BLOCKED {quote(f'{reason}; {cause}' if cause else reason)}"
    if completed.error:
        reason = completed.error
    elif completed.timed_out:
        reason = f"timed out after {timeout:g} seconds before running a script from the skill"
    elif completed.returncode != 0:
        reason = f"exited with code {completed.returncode} before running a script from the skill"
    else:
        reason = "never ran a script from the skill"
    if denials:
        reason += f"; denied: {'; '.join(denials)}"
    if unread:
        count = f"{len(unread)} command failure{'' if len(unread) == 1 else 's'}"
        reason += (
            f"; the runtime logged {count} in a shape the canary does not read, so whether its policy or sandbox "
            f"refused them is unknown: {unread[0]}"
        )
    return f"FAILED {quote(reason)}"


def marked(probes: list[dict[str, Any]], run: str) -> list[dict[str, Any]]:
    """The probe records that carry the run's identifier, which the fixture's probe reads from the home it ran in."""
    return [record for record in probes if record.get("run") == run]


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
    version: tuple[int, ...] | None = None,
    attempt: int = 1,
) -> list[str]:
    """The lines of one run of the skill; a run after the first names its files with its number."""
    state = home / STATE
    probe_log = state / "probe.jsonl"
    name = f"{runtime}-{skill}" if attempt == 1 else f"{runtime}-{skill}-run{attempt}"
    script_log = state / "scripts" / f"{name}.jsonl"
    # Created empty here, so the canary owns them and the probe and recorders only append. A file the Codex Windows
    # sandbox creates belongs to its sandbox user, and the permissions that sandbox sets leave it unreadable here.
    probe_log.write_text("", encoding="utf-8")
    script_log.write_text("", encoding="utf-8")
    # A marker counts only with this run's identifier, so a line a runtime's shell appended to the file is not one.
    run = secrets.token_hex(16)
    (state / RUN_ID).write_text(run, encoding="utf-8")
    completed = runner(
        run_command(runtime, executable, skill, home, codex_sandbox, version),
        home,
        environment(base, script_log, home),
        timeout,
    )
    transcript = state / "transcripts" / f"{name}.jsonl"
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
        probes, started = marked(read_records(probe_log), run), read_records(script_log)
    except (OSError, RecordRefused) as exc:
        outcome = f"FAILED {quote(unreadable(exc, probe_log))}"
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
            unread=unread_rejections(runtime, completed),
            cause=blocked_cause(runtime, codex_sandbox),
        )
    if runtime == "claude" and (nothing := discovery_failure(completed.stdout)):
        outcome = f"FAILED {quote(nothing)}"
    return [
        *lines,
        f"RUNTIME {runtime} {skill} {outcome} KEPT {quote(KEPT[runtime])}",
        f"TRANSCRIPT {runtime} {skill} {quote(forward(transcript))}",
    ]


@dataclass(frozen=True, kw_only=True)
class RuntimeChecks:
    """What each runtime's checks in one canary run share: the skills in the order checked, the home, the
    environment and runner, the run's options, and what the deployment declared and adapted."""

    skills: list[str]
    home: Path
    base: Mapping[str, str]
    runner: Runner
    discovery_only: bool
    timeout: float
    closure: Mapping[str, list[str]]
    codex_sandbox: str = DEFAULT_CODEX_SANDBOX
    talk: discovery.Converse | None = None
    declared: Declarations | None = None
    unadapted: Mapping[str, str] | None = None
    attempts: int = DEFAULT_ATTEMPTS


def check_runtime(
    runtime: str, executable: str, run: RuntimeChecks, version: tuple[int, ...] | None = None
) -> Iterator[str]:
    """Each line of the checks of the runtime whose `--version` named `version`, given as soon as it is known, so a run
    that fails later loses none."""
    # Claude Code has no listing; its run reports the skills it found.
    if runtime in discovery.RUNTIMES:
        listing_environment = environment(run.base, run.home / STATE / "scripts" / "listing.jsonl", run.home)
        try:
            found = discovery.list_skills(runtime, executable, run.home, listing_environment, run.timeout, run.talk)
        except discovery.ListingError as exc:
            yield f"DISCOVERY_FAILED {runtime} {quote(f'cannot read its skill list: {exc}')}"
        else:
            yield from discovery_lines(runtime, run.skills, found, run.home, run.unadapted)
    if run.discovery_only:
        return
    yield from (f"SUPPLIED {runtime} {quote(setting)}" for setting in supplied(runtime, run.codex_sandbox))
    for skill in run.skills:
        support = ((run.declared or {}).get(skill) or {}).get(DECLARED_RUNTIME[runtime])
        if support is not None and support.level == runtime_support.NONE:
            yield f"RUNTIME {runtime} {skill} NOT_ATTEMPTED {quote(support.reason)}"
            continue
        reason = unsupported(runtime, skill, run.home)
        if reason:
            yield f"RUNTIME {runtime} {skill} UNSUPPORTED {quote(reason)}"
            continue
        reachable = run.closure.get(skill, [skill])
        # A run that started the installed copy tested nothing, so the skill runs again, up to run.attempts runs, and
        # each such run keeps its UNCOUNTED line, so the record says how many runs the skill took.
        for attempt in range(1, run.attempts + 1):
            lines = run_skill(
                runtime,
                executable,
                skill,
                run.home,
                run.base,
                run.runner,
                run.timeout,
                reachable,
                run.codex_sandbox,
                version,
                attempt,
            )
            yield from lines
            if not any(outcome_of(line) == "UNCOUNTED" for line in lines):
                break


def canary(
    skills: list[str],
    runtimes: list[str],
    home: Path,
    *,
    sources: list[Path],
    runner: Runner | None = None,
    which: Callable[[str], str | None] = platform_support.find_executable,
    base_environment: Mapping[str, str] | None = None,
    discovery_only: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    codex_sandbox: str = DEFAULT_CODEX_SANDBOX,
    talk: discovery.Converse | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
) -> int:
    """Deploy the sources into home, then check each runtime; print the facts, then the record, and return the exit
    code."""
    said: list[str] = []
    versions: dict[str, str] = {}

    def say(line: str) -> None:
        print(line, flush=True)
        said.append(line)

    code = checked(
        skills,
        runtimes,
        home,
        sources=sources,
        runner=runner or run_process,
        which=which,
        base=dict(os.environ if base_environment is None else base_environment),
        discovery_only=discovery_only,
        timeout=timeout,
        codex_sandbox=codex_sandbox,
        talk=talk,
        attempts=attempts,
        say=say,
        versions=versions,
    )
    for line in record(said, versions, places(home)):
        print(line, flush=True)
    return code


def checked(
    skills: list[str],
    runtimes: list[str],
    home: Path,
    *,
    sources: list[Path],
    runner: Runner,
    which: Callable[[str], str | None],
    base: dict[str, str],
    discovery_only: bool,
    timeout: float,
    codex_sandbox: str,
    talk: discovery.Converse | None,
    attempts: int,
    say: Callable[[str], None],
    versions: dict[str, str],
) -> int:
    """The canary's run: each line goes to `say` as it is made, and each runtime found to `versions`."""
    state = home / STATE
    say(f"HOME {quote(forward(home))}")
    try:
        for source_dir in sources:
            identifier = source_id(source_dir)
            code, log = deploy(home, source_dir)
            if code != 0:
                state.mkdir(exist_ok=True)
                log_file = state / f"deploy-{identifier.replace('/', '-')}.log"
                log_file.write_text(log, encoding="utf-8")
                say(f"DEPLOY_FAILED {identifier} {quote(forward(log_file))}")
                return 1
            say(f"DEPLOYED {identifier}")
    finally:
        claim_home(home)
    for folder in ("recorder", "scripts", "transcripts"):
        (state / folder).mkdir(parents=True, exist_ok=True)
    (state / "recorder" / "sitecustomize.py").write_text(RECORDER, encoding="utf-8")
    # Line feeds only: Git Bash reads a carriage return as part of the command.
    (state / "recorder" / "bash_env.sh").write_text(BASH_RECORDER, encoding="utf-8", newline="\n")
    ordered = [FIXTURE_SKILL, *(skill for skill in dict.fromkeys(skills) if skill != FIXTURE_SKILL)]
    closure = dependencies(sources)
    declared = declarations(sources)
    run = RuntimeChecks(
        skills=ordered,
        home=home,
        base=base,
        runner=runner,
        discovery_only=discovery_only,
        timeout=timeout,
        closure=closure,
        codex_sandbox=codex_sandbox,
        talk=talk,
        declared=declared,
        unadapted=without_adapter(home),
        attempts=attempts,
    )
    printed = []
    reasons = []
    for runtime in runtimes:
        executable = which(runtime)
        if executable is None:
            say(f"SKIPPED {runtime} {quote(f'{runtime} is not installed')}")
            reasons.append(f"{runtime} is not installed")
            continue
        version = read_version(executable, runner, base, home)
        versions[runtime] = version_text(version)
        lines = []
        for line in check_runtime(runtime, executable, run, version):
            say(line)
            lines.append(line)
        printed += lines
        if not any(checks(line) for line in lines):
            reasons.append(unchecked(runtime, lines, discovery_only))
    matrix = compare(printed, declared)
    for line in matrix:
        say(line)
    # A runtime whose skill list could not be read proves nothing about it, so it fails the run as a miss does, and
    # so does a run that checked no runtime, as `deploy.py verify` fails when it finds none to verify.
    nothing = not any(checks(line) for line in printed)
    if nothing:
        what = "discovery" if discovery_only else "discovery or run"
        say(f"NOTHING_CHECKED {quote(f'no runtime was checked for {what}: ' + '; '.join(reasons))}")
    unestablished = nothing or any(line.startswith(("UNDISCOVERED ", "DISCOVERY_FAILED ")) for line in printed)
    return 1 if unestablished or any(" DISAGREES " in line for line in matrix) else 0


def read_version(executable: str, runner: Runner, base: Mapping[str, str], home: Path) -> tuple[int, ...] | None:
    """The version the runtime's `--version` prints, read as `deploy.py check` reads it, or None."""
    environment = {**without_repository_variables(base), **PINNED_VERSION}
    completed = runner([executable, "--version"], home, environment, VERSION_TIMEOUT)
    return parse_version(completed.stdout) if completed.returncode == 0 and not completed.error else None


def version_text(version: tuple[int, ...] | None) -> str:
    return format_version(version) if version else "unknown"


def runtime_version(executable: str, runner: Runner, base: Mapping[str, str], home: Path) -> str:
    """The version the runtime's `--version` prints, read as `deploy.py check` reads it, or `unknown`."""
    return version_text(read_version(executable, runner, base, home))


def places(home: Path) -> dict[Path, str]:
    """The paths a record must not name, each with the placeholder it names instead: the throwaway home, this
    checkout, and the profile folder the runtimes read skills from."""
    return {home: "<temp>", REPOSITORY_ROOT: "<source>", platform_support.home_directory(): "<home>"}


def placeholders(line: str, paths: Mapping[Path, str]) -> str:
    """The line with each of the paths, and anything under it, named by its placeholder instead, the longest path first
    so one inside another, as the home under the temporary directory in the profile, keeps its own. A path is matched
    as the line writes it, with forward slashes, as given, or escaped in a quoted string, and without regard to case,
    since a runtime may name a path in another case where the file system folds it and naming too much costs a record
    nothing, but only up to the end of a name, so a sibling whose name it starts is kept."""
    for path, placeholder in sorted(paths.items(), key=lambda item: len(str(item[0])), reverse=True):
        forms = sorted({forward(path), str(path), quote(str(path))[1:-1]}, key=len, reverse=True)
        alternatives = "|".join(re.escape(form) for form in forms)
        pattern = re.compile(f"(?:{alternatives})" + r"(?![^\\/\s\"'])", re.IGNORECASE)
        # A replacement string reads a backslash as an escape, so one in a placeholder is doubled.
        line = pattern.sub(placeholder.replace("\\", "\\\\"), line)
    return line


def record(lines: Sequence[str], versions: Mapping[str, str], paths: Mapping[Path, str]) -> list[str]:
    """The block a pull request's Validation section holds: each runtime's version, then the lines of the kinds
    RECORDED names, in the order the run printed them, with every one of the paths named by its placeholder."""
    body = [f"VERSION {runtime} {version}" for runtime, version in versions.items()]
    body += [placeholders(line, paths) for line in lines if line.split(" ", 1)[0] in RECORDED]
    return ["RECORD_BEGIN", *body, "RECORD_END"]


def shipped_through(src: deploy_source.Source, skill: str) -> list[tuple[str, str]]:
    """The shipped skills a change to `skill` reaches, each with the hidden dependency it went through, if any: the
    skill itself when it is selectable, and otherwise each skill that declares it in skill_deps, directly or through
    another hidden one."""
    reached: list[tuple[str, str]] = []
    pending, seen = [skill], {skill}
    while pending:
        name = pending.pop(0)
        if src.skills[name].selectable:
            reached.append((name, "" if name == skill else skill))
            continue
        for declarer in sorted(other for other, item in src.skills.items() if name in item.skill_deps):
            if declarer not in seen:
                seen.add(declarer)
                pending.append(declarer)
    return reached


def reached_by(path: str, src: deploy_source.Source) -> list[tuple[str, str]]:
    """The skills a changed repository path reaches directly, each with how, before hidden ones are followed."""
    parts = path.split("/")
    if parts[0] == "skills" and len(parts) > 2 and parts[1] in src.skills:
        return [(parts[1], "")]
    if parts[0] == "skills" and len(parts) > 1 and parts[1] in src.shared_assets:
        asset = parts[1]
        return [(name, f"the shared asset {asset}") for name, item in src.skills.items() if asset in item.shared_deps]
    if len(parts) == 2 and parts[0] == "deploy-meta" and parts[1].endswith(".json") and parts[1][:-5] in src.skills:
        return [(parts[1][:-5], "")]
    if len(parts) == 2 and parts[0] == "agents" and parts[1].endswith(".md"):
        agent = parts[1][:-3]
        return [(name, f"the agent {agent}") for name, item in src.skills.items() if agent in item.agent_deps]
    return []


def changed_skills(
    paths: Iterable[str], src: deploy_source.Source, user_only_skill: Callable[[str], bool]
) -> dict[str, str]:
    """Each shipped skill the changed paths reach, by name, with the first path that reaches it and how."""
    named: dict[str, str] = {}
    renderer = False
    for path in sorted(paths):
        renderer = renderer or path == ADAPTER_RENDERER
        for skill, via in reached_by(path, src):
            for shipped, hidden in shipped_through(src, skill):
                how = [part for part in (via, f"the hidden dependency {hidden}" if hidden else "") if part]
                named.setdefault(shipped, f"{path}, through {' and '.join(how)}" if how else path)
    if renderer:
        selectable = sorted(name for name, item in src.skills.items() if item.selectable)
        for only_user, kind in ((False, "the model may start"), (True, "only the user may start")):
            if any(user_only_skill(name) == only_user for name in named):
                continue
            chosen = next((name for name in selectable if user_only_skill(name) == only_user), None)
            if chosen is not None:
                named[chosen] = f"{ADAPTER_RENDERER} writes every adapter, so one skill {kind}"
    return dict(sorted(named.items()))


def changed_paths(root: Path, ref: str, client: GitClient = GIT) -> tuple[str, list[str]]:
    """The commit `ref` and HEAD share, and every path changed since it: committed, staged, unstaged, or untracked,
    both sides of a rename."""
    base = client.output(["merge-base", ref, "HEAD"], directory=root).strip()
    tracked = client.output(["diff", "--name-only", "--no-renames", "-z", base, "--"], directory=root)
    untracked = client.output(["ls-files", "--others", "--exclude-standard", "-z"], directory=root)
    return base, sorted({path for path in (tracked + untracked).split("\0") if path})


def changed_since(ref: str | None, root: Path = REPOSITORY_ROOT, client: GitClient = GIT) -> int:
    """Print the shipped skills the change since `ref`, the last release HEAD holds when None, reaches; start
    nothing."""
    try:
        since = ref or last_release(client, root, "HEAD")
        if since is None:
            print(f"FAILED {quote('no release tag reaches HEAD; pass --changed-since REF')}")
            return 1
        base, paths = changed_paths(root, since, client)
        source_paths = Paths(root, root)
        src = deploy_source.discover(source_paths, deploy_source.load_source_id(source_paths))
    except GitError as exc:
        print(f"FAILED {quote(str(exc))}")
        return 1
    except DeployError as exc:
        print(f"FAILED {quote(' '.join(exc.lines))}")
        return 1
    print(f"SINCE {since} {base}")
    named = changed_skills(paths, src, lambda name: starts_only_by_user(src.skills[name].directory / "SKILL.md"))
    for skill, why in named.items():
        print(f"CHANGED {skill} {quote(why)}")
    if not named:
        print(f"NOTHING_CHANGED {quote(f'no change since {since} reaches a shipped skill')}")
    return 0


def create_home() -> Path:
    """A new, empty folder under the temporary directory, named like tempfile.mkdtemp's but made with the mode
    platform_support.SANDBOX_READABLE_DIRECTORY_MODE gives, so every Codex sandbox the canary may choose can read it.
    mkdtemp's own 0o700 gives the folder, on Windows, an access control list that Codex's unelevated sandbox cannot
    read, though MXC's can; elsewhere the mode is mkdtemp's."""
    parent = Path(tempfile.gettempdir())
    for _ in range(tempfile.TMP_MAX):
        home = parent / f"{HOME_PREFIX}{secrets.token_hex(6)}"
        try:
            home.mkdir(platform_support.SANDBOX_READABLE_DIRECTORY_MODE)
        except FileExistsError:
            continue
        return home.resolve()
    raise FileExistsError(f"no unused name for a canary home under {parent}")


def claim_home(home: Path) -> None:
    """Mark the home as one the canary made, naming it, which --remove-home requires. The deployer takes a
    --canary-home only while it is empty or holds the deployer's own marker, so this is written after the
    deployments, a failed one included."""
    (home / STATE).mkdir(exist_ok=True)
    record = {"made_by": "tools/runtime_canary.py", "home": forward(home)}
    (home / STATE / HOME_MARKER).write_text(json.dumps(record) + "\n", encoding="utf-8")


def claimed(home: Path) -> bool:
    """Whether the home holds the marker claim_home writes, naming this home, with no link on the way to it."""
    state, marker = home / STATE, home / STATE / HOME_MARKER
    try:
        # A path whose metadata cannot be read is judged as a link is: not this tool's claim.
        if any(platform_support.is_link_or_junction(path) for path in (state, marker)):
            return False
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    named = record.get("home") if isinstance(record, dict) else None
    return isinstance(named, str) and lexical(named) == lexical(home)


def remove_home(directory: str) -> int:
    """Delete a home the canary made, a HOME_PREFIX directory directly in the temporary directory that holds the
    marker naming it, and nothing else."""
    home = Path(directory)
    temporary = Path(tempfile.gettempdir()).resolve()
    resolved = home.resolve()
    if (
        not is_plain_directory(home)
        or not resolved.is_dir()
        or resolved.parent != temporary
        or not resolved.name.startswith(HOME_PREFIX)
    ):
        print(f"FAILED {quote(f'{forward(home)} is not a home the canary made under {forward(temporary)}')}")
        return 1
    if not claimed(resolved):
        marker = f"{STATE}/{HOME_MARKER}"
        print(f"FAILED {quote(f'{forward(resolved)} holds no {marker} naming it, so the canary did not make it')}")
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
    parser.add_argument("--keep-home", action="store_true", help="keep the home after a run that passed")
    parser.add_argument(
        "--changed-since",
        nargs="?",
        const="",
        metavar="REF",
        help="name the shipped skills changed since REF (default: the last release), start nothing, and exit",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"limit for each runtime run (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=DEFAULT_ATTEMPTS,
        metavar="RUNS",
        help=f"runs of a skill on a runtime while each starts the installed copy (default: {DEFAULT_ATTEMPTS})",
    )
    parser.add_argument(
        "--codex-sandbox",
        choices=CODEX_SANDBOXES,
        default=DEFAULT_CODEX_SANDBOX,
        help=f"Windows sandbox mode Codex runs with (default: {DEFAULT_CODEX_SANDBOX})",
    )
    options = parser.parse_args(arguments)
    if options.attempts < 1:
        parser.error(f"--attempts takes at least one run: {options.attempts}")
    if options.remove_home is not None or options.changed_since is not None:
        option = "--remove-home" if options.remove_home is not None else "--changed-since"
        if options.skills:
            parser.error(f"{option} takes no SKILL")
        if options.keep_home or (options.remove_home is not None and options.changed_since is not None):
            parser.error(f"{option} makes no home, so it takes no other home option")
        if options.remove_home is not None:
            return remove_home(options.remove_home)
        if options.changed_since.startswith("-"):
            parser.error(f"--changed-since takes a commit, not an option: {options.changed_since}")
        return changed_since(options.changed_since or None)
    sources = [REPOSITORY_ROOT, FIXTURE_SOURCE]
    # A source the deployer cannot read names no skills, so a SKILL is judged only when every source can be read;
    # otherwise the deployment stops the run with the deployer's own reason.
    unknown = [skill for skill in options.skills if skill not in dependencies(sources)]
    if unknown and all(discoverable(source) for source in sources):
        parser.error(f"unknown skill: {', '.join(unknown)}")
    home = create_home()
    code = canary(
        options.skills,
        options.runtimes or list(RUNTIMES),
        home,
        sources=sources,
        discovery_only=options.discovery_only,
        timeout=options.timeout,
        codex_sandbox=options.codex_sandbox,
        attempts=options.attempts,
    )
    return finish(home, code, options.keep_home)


def finish(home: Path, code: int, keep: bool) -> int:
    """Remove the home after a run that passed, unless asked to keep it, and keep it after one that failed, for its
    transcripts; the run's exit code, or 1 when the home could not be removed."""
    if keep or code != 0:
        why = "--keep-home was given" if keep else "the run failed, so its transcripts stay for reading"
        print(f"HOME_KEPT {quote(forward(home))} {quote(f'{why}; remove it with --remove-home')}")
        return code
    return remove_home(str(home))


if __name__ == "__main__":
    run_main(lambda: main(sys.argv[1:]), errors="backslashreplace")
