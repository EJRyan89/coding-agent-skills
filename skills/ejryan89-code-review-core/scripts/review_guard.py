"""PreToolUse hook for the code-review-reviewer subagent: keep a reviewer inside its own role of its review run.

A reviewer's prompt told it never to read the local checkout, and reviewers did anyway: they searched the live
working copy and the whole workspace, which may hold another branch's code. This hook enforces the boundary the
prompt describes. It reads the hook event as JSON on stdin and prints a deny decision for any call outside it:

- Every path a call names is held, by its text alone and before anything resolves it, to a local absolute path under
  the temporary directory, where prepare makes every run, or this skill's folder. A network or device path, or any
  other path, is denied unresolved, because resolving a path to a host that does not answer can block past the hook's
  timeout, and Claude Code lets a call through when its hook times out.
- First, one Read of a role's prompt file, which binds the reviewer to that role and its run: its claim. The task
  names the prompt, and nothing else may be read before it, so no content from the pull request can choose the
  role. The run is the outermost `code-review-run-*` folder holding `run.json` under the temporary directory, the
  one prepare made; a snapshot can hold a folder of that shape, so a run folder inside another run's tree is denied.
  Every other call is denied until then.
- Read, Grep, and Glob only under the claimed review run folder (a `code-review-run-*` directory holding
  `run.json`: the snapshot, the trusted reviewer files, and the work files) or this skill's `references` folder.
  Nested source folders cannot redefine the claimed root. Grep and Glob must name their folder explicitly, because
  their default is the session's working directory.
- Write and Edit only on the claimed role's result file, as the run's `run.json` names it.
- Bash only for the claimed role's self-check command, exactly as the pipeline writes it, and its `source-file` and
  `source-search` commands (review_source.py) on every snapshot, lazy or written whole (its suite holds both kinds),
  where `review_source.py` itself refuses `source-file` on one that holds every file already, with the one path or
  pattern the reviewer fills in, which may hold no double quote (ASCII or PowerShell's smart variants), backtick,
  dollar sign, backslash, or line break. No quoted script or run in either may end in a backslash, which would
  escape its closing quote.
  The guarded reviewer exposes Bash, not PowerShell; its values conservatively exclude delimiters from either shell.
- A role whose `run.json` entry declares its `self_check`, as the review-insights synthesis run does, runs that one
  command, exactly as declared, and none of the reviewer commands above. The run's author is the script that made
  the run, which the guard already trusts to name the one file the role may write.

Parallel reviewers share one session, so the hook tells them apart by the `agent_id` Claude Code puts in the hook
event of a subagent's tool call, and keeps each agent's claim in a file of its own under `CLAIMS`, which no
reviewer can write: a folder named with the user's id that `private_folder.ensure` finds, before every read and write
of a claim, to be one only the user owns and can write, so no other local account can remove a claim or plant one.
A call is denied while it is not. A retry is a fresh agent that claims the same role. A claim is created whole or not
at all, through `atomic_file.create_bytes`, so no reader, a parallel first call of the same agent included, ever sees
part of one, and a write that fails leaves no claim behind and denies its call, naming why. The create puts the claim
in place by a hard link, so a claims folder whose file system has none denies every first read, naming that remedy;
the create's empty reservation is never allowed, since a claim seen empty would deny its reviewer as unreadable. Each
first read prunes, under a lock, the claims whose run is gone and the leftovers of failed claims, and removes only a
file it judged, so a claim made after the judgment is never removed.

The hook also counts what a reviewer reads: a claim creates the role's read log in the run's `work` folder, and each
allowed Read of a file, or Grep of one file, under the run's source snapshot appends that file's path, relative to
the snapshot, as one JSON string per line, as `source-file` does for each file it fetches or finds. Only the
pipeline's `check` reads the log, and it keeps nothing of it but counts. A log that cannot be written is left
short: counting never denies a call. The log is also the claim's proof: on Claude Code, `check` refuses the result of
a role the guard never claimed (`UNGUARDED`), since no hook held its reviewer.

Other tools pass. Anything the hook cannot evaluate is denied, a call without an agent ID included: a reviewer
reading the wrong code, or writing another role's result, produces a plausible but wrong review, which is worse
than a failed one that check reports and retries.

Installed as a Claude Code plugin, the agent cannot carry this hook: a plugin agent's own hooks are ignored, so the
plugin's hooks file runs it for every matching tool call of every session, and the guard keeps to the plugin's
reviewer itself. A call with no agent ID comes from the main session, and a call of any other agent type is another
agent's; both pass. A subagent's call that carries no agent type cannot be placed, so it is denied, and a call of
the plugin's reviewer, whose type Claude Code namespaces as `<plugin>:code-review-reviewer`, is held as above.

Exit status:
  0  the hook answered: a deny decision printed as JSON, or nothing, which lets the call through
  1  a last line FAILED <reason>, a hook that failed: the reviewer agent's hook declares onFailure: block, so
     Claude Code blocks the call, as it does when the hook cannot start or times out
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import atomic_file
import file_lock
import file_presence
import hook_decision
import json_reader
import json_writer
import private_folder
from console import run_main
from executable_locator import PYTHON
from path_containment import relative_within, resolved_path, within
from path_keys import path_key, same_parts
from skill_roots import plugin_name
from typed_fields import FieldError, FieldReader

EXIT_CONTRACT_EXEMPT = (
    "Claude Code PreToolUse hook protocol: prints a JSON decision or nothing and exits 0, unless run_main reports a "
    "failure, which exits 1 and which the hook's onFailure: block turns into a blocked call"
)
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
REFERENCES = SCRIPT_DIRECTORY.parent / "references"
PIPELINE = SCRIPT_DIRECTORY / "review_pipeline.py"
RUN_PREFIX = "code-review-run-"
RUN_FILE = "run.json"
SOURCE = "source"  # the run's source snapshot
READ_TOOLS = {"Read", "Grep", "Glob"}
WRITE_TOOLS = {"Write", "Edit"}
# A quoted script or run in any allowed command. No quoted value may end in a backslash, which would escape its closing
# quote and leave what follows outside quotes, though the path still normalizes to the known script or run.
SHELL_BREAKS = r'"`$\r\n\u201c\u201d\u201e'
QUOTED_VALUE = rf"[^{SHELL_BREAKS}]*[^{SHELL_BREAKS}\\]"
# Each command names Python as review-prs's rendered grant does, by the platform's command.
SELF_CHECK = re.compile(
    rf'^{re.escape(PYTHON)} -B "(?P<script>{QUOTED_VALUE})" validate-result --run "(?P<run>{QUOTED_VALUE})" '
    rf'--role "(?P<role>[a-z0-9][a-z0-9-]*)"$'
)
# The two commands review_source.py gives a reviewer: a lazy snapshot's both, or any other snapshot's search. The
# script and the run must be the known ones; the path or pattern, the one value the reviewer chooses, holds no
# backslash at all, so it can never end up outside its quotes.
SOURCE_COMMAND = re.compile(
    rf'^{re.escape(PYTHON)} -B "(?P<script>{QUOTED_VALUE})" (?P<command>source-file|source-search) '
    rf'--run "(?P<run>{QUOTED_VALUE})" --role "(?P<role>[a-z0-9][a-z0-9-]*)" '
    rf'--(?P<option>path|pattern)="[^{SHELL_BREAKS}\\]+"$'
)
SOURCE_OPTIONS = {"source-file": "path", "source-search": "pattern"}
SOURCE_SCRIPT = SCRIPT_DIRECTORY / "review_source.py"
SHELL_DRIVE = re.compile(r"^/([A-Za-z])(/|$)")
# A normalized path that starts with two separators names a host or a device: `\\host\share`, `//host/share`, `\\?\`,
# and `\\.\` on Windows, and `//host` where POSIX leaves its meaning to the system.
NETWORK_OR_DEVICE = re.compile(r"^[\\/]{2}")
AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
# The reason a denial gives when its own holds what JSON cannot carry, such as a lone surrogate.
UNWRITABLE_REASON = (
    "Code-review reviewer boundary: the guard denied this call for a reason that holds what JSON cannot carry, such "
    "as a path whose name is not valid Unicode."
)
# The user's own folder of claims, in their runtime directory or else the temporary directory, which on Linux with
# TMPDIR unset is the shared /tmp: every read and write goes through claims_folder, which refuses it unless it is a
# folder only the user owns and can write.
CLAIMS = private_folder.location("code-review-reviewer-claims")
# The subagent every reviewer role runs as, deployed from agents/ with ejryan89-code-review-core, and the plugin these
# scripts run from, if any, which namespaces it.
REVIEWER = "code-review-reviewer"
PLUGIN = plugin_name()
FIRST = "read the prompt file your task names first"
ROLE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
Claim = tuple[Path, dict[str, Any]]
# How old, in seconds, a claim that does not parse must be before a new claim prunes it: many times the hook's
# timeout, so no guard that could still be writing it, such as an older version's beside this one, is running.
UNREADABLE_CLAIM_AGE = 600
# How old, in seconds, a copy a create left beside a claim must be before a new claim prunes it. A copy lives only while
# its create runs, inside one hook call, which Claude Code ends at the hook's timeout, so one past every guard's timeout
# is a killed create's; the age is an unreadable claim's, many times that timeout. Pruning a copy sooner widens nothing,
# since a create whose copy is gone before its link fails and leaves no claim, but it would fail that create.
LEFTOVER_COPY_AGE = UNREADABLE_CLAIM_AGE
# The name `atomic_file.create_bytes` gives the copy it writes a claim through: `.<agent>.json.<random>.tmp`.
LEFTOVER_COPY = ".*.json.*.tmp"
# The file whose lock a guard holds while it prunes, so no two guards prune at once: only a prune removes a claim, and
# a claim's path is free for a new one only once it is removed, so no claim can appear where one a prune judged was
# while that prune holds the lock.
PRUNE_LOCK = ".prune.lock"
# Why a reviewer whose own claim does not parse is denied, ending with the remedy: no call can be judged against it.
UNREADABLE_CLAIM = (
    "this reviewer's claim could not be read; run the role again with a fresh reviewer, which claims it anew"
)
# Why a reviewer is denied when the file system refuses the hard link a claim is made whole through, ending with the
# remedy; `{folder}` is the runtime or temporary directory the claims folder lies in.
NO_HARD_LINKS = (
    "the reviewer guard makes each claim whole through a hard link, which the file system holding {folder} refuses; "
    "give the guard a runtime or temporary directory on a file system with hard links, such as a local disk's"
)


# The presence reads and removals here that read a path they cannot stat, list, or remove as nothing there,
# each with why that is the answer it should give.
PRESENCE_ALLOWED = {
    "_log_read": "counts a read the guard already allowed; a path it cannot stat goes uncounted, as one the JSON "
    "writer refuses does, and never turns the read into a denial",
    "_prune": "a claim, a copy, or a run.json it cannot open, stat, or read is kept, never pruned as one whose run is "
    "gone or removed unjudged",
}


def _spellings(root: Path) -> tuple[Path, ...]:
    """A trusted local folder as it is given and as it resolves, each in the case the file system compares, so a path
    in either spelling is held to it by its text."""
    return tuple(Path(path_key(form, textual=True)) for form in (root.absolute(), resolved_path(root)))


# The folders every path the guard accepts lies under: the temporary directory, where prepare makes every run, and
# this skill's folder, which holds the references and the scripts the allowed commands run. A run folder another local
# account made in a shared temporary directory lies under these roots too, but no reviewer reaches it: a run is chosen
# only by the reviewer's first read, of the prompt its task names in the run prepare made, before it has read anything
# a pull request holds; every later call is held to the run its claim names; and the claims lie in the user's own
# folder, so no other account can plant one binding a reviewer to its run.
TEMPORARY_ROOTS = _spellings(Path(tempfile.gettempdir()))
LOCAL_ROOTS = (*TEMPORARY_ROOTS, *_spellings(Path(__file__).absolute().parents[1]))


class Denied(Exception):
    pass


class DeniedField(FieldError, Denied):
    """A hook event, run, or claim field of the wrong kind; the call is denied."""


READ = FieldReader(DeniedField)


def read_log(run: Path, role: str) -> Path:
    """The file the guard logs a role's snapshot reads in. `check` in review_pipeline.py reduces it to counts."""
    return run / "work" / f"reads-{role}.log"


def claimed(run: Path, role: str) -> bool:
    """Whether the guard claimed this role of the run and its log is still there to show it: the claim creates the
    log, and `check` deletes it once it has counted it."""
    return file_presence.is_present(read_log(run, role), other_is_absent=True)


# Why a result is refused on Claude Code when the guard never claimed its role, ending with the remedy, which names how
# the skill runs the role without the agent. It names each setting Claude Code's settings reference gives that keeps
# the guard's hook from running on either install route: the hook in the agent's frontmatter, as the deployer installs
# it to ~/.claude/agents, or the plugin's hooks file.
UNGUARDED = (
    "the reviewer ran without its guard: the reviewer guard never claimed this role, as when a setting keeps its hook "
    "from running (disableAllHooks; allowManagedHooksOnly, which runs no hook an agent's frontmatter declares and no "
    "plugin's hook unless managed settings force-enable the plugin; or strictPluginOnlyCustomization locking hooks, "
    "which stops the frontmatter hooks of agents in ~/.claude/agents, or agents, which stops loading those agents), "
    "the code-review-reviewer agent or its hook did not load, or another agent type ran the role; let the "
    "code-review-reviewer agent load and run its hook, or {remedy}"
)


def _start_log(run: Path, role: str) -> None:
    """Create the role's read log, so a reviewer the guard held is told from one no guard held, which has none."""
    if ROLE_ID.fullmatch(role):
        with contextlib.suppress(OSError):
            read_log(run, role).parent.mkdir(exist_ok=True)
            read_log(run, role).touch()


def search_entry(tool: str) -> dict[str, str]:
    """The read log's entry for one search of the snapshot by `tool`, beside the path it holds for each file read."""
    return {"search": tool}


def is_search_entry(entry: object) -> bool:
    return isinstance(entry, dict) and set(entry) == {"search"} and isinstance(entry["search"], str)


def _log_read(tool: str, path: Path, claim: Claim) -> None:
    """Append what a reviewer examined under its run's snapshot to its role's read log: a file it read, by its path
    relative to the snapshot, for a Read or a Grep naming one file; or a search, for a Grep of a folder or a Glob
    whose path is the snapshot, a folder in it, or the run folder that holds it. A search names no file, so it is
    counted apart from the files read."""
    run, role = claim[0], claim[1]["id"]
    if not ROLE_ID.fullmatch(role):
        return
    source = run / SOURCE
    snapshot = resolved_path(source)
    entry: str | dict[str, str]
    relative = relative_within(path, snapshot)
    if tool != "Glob" and relative is not None and path.is_file():
        # As the reviewer spelled it: review_dispatch keys each entry as the snapshot's file system compares names.
        entry = relative.as_posix()
    elif tool != "Read" and (within(path, snapshot) or within(snapshot, path)) and path.is_dir():
        entry = search_entry(tool)
    else:
        return
    # The read is already allowed and the log only counts it, so a path the JSON writer refuses, such as one holding
    # a lone surrogate, stays uncounted, as review_source counts it, and never turns the read into a denial. An append
    # cut short leaves part of a line, which joins the next entry appended, and `check` passes over the joined line, so
    # both reads are left uncounted: the log only counts, and only whether it exists shows the claim, which
    # `_start_log` creates empty.
    with (
        contextlib.suppress(OSError, json_writer.JsonWriteRefused),
        read_log(run, role).open("a", encoding="utf-8") as log,
    ):
        log.write(json_writer.dumps(entry) + "\n")


def _path(value: Any, cwd: Path) -> Path:
    try:
        value = READ.string(value, "the path")
    except FieldError as exc:
        raise Denied("give an absolute path inside the review run") from exc
    # Git Bash names a drive as /c/, which only Windows means; elsewhere /c/ is a directory of its own.
    match = SHELL_DRIVE.match(value) if sys.platform == "win32" else None
    text = f"{match.group(1)}:/{value[match.end() :]}" if match else value
    path = Path(text)
    if not path.is_absolute():
        path = cwd / path
    return _resolve_local(Path(os.path.normpath(path)))


def _resolve_local(path: Path) -> Path:
    """Resolve a normalized path only once its text shows it local and under `LOCAL_ROOTS`. Resolving a network path
    to a host that does not answer blocks for longer than the hook's timeout, and Claude Code lets a call through when
    its hook times out, so such a path is denied by its text alone."""
    if NETWORK_OR_DEVICE.match(str(path)):
        raise Denied(f"give a local path, not a network or device path; {path} is one")
    if not any(within(path, root) for root in LOCAL_ROOTS):
        raise Denied(f"give an absolute path inside your review run folder; {path} is outside every folder it may use")
    return Path(os.path.normpath(resolved_path(path)))


def _same(first: Path, second: Path) -> bool:
    """Whether `first`, a path the reviewer gave, is `second`, a path the run or this skill holds, by its exact
    spelling, part by part, neither normalized: a part differing only in the case of ASCII letters matches where the
    directory holding `second`'s part folds case, and one differing in any other letter's case or in Unicode
    normalization only where that directory finds one entry under both names, so a spelling its file system takes as
    another folder, such as the Kelvin sign for `K` on NTFS, never matches; only `second` is asked."""
    return same_parts(first.parts, second.parts)


def run_root(path: Path) -> Path | None:
    """Discover a run for a first prompt read or an explicit command root, never for a claimed source read: the
    outermost `code-review-run-*` folder holding `run.json` under the temporary directory, which prepare made. A pull
    request's snapshot can hold a folder of that shape, so one inside another run's tree is denied, never a run."""
    runs = [
        candidate
        for candidate in (path, *path.parents)
        if candidate.name.startswith(RUN_PREFIX)
        and any(within(candidate, root) for root in TEMPORARY_ROOTS)
        and file_presence.is_present(candidate / RUN_FILE, other_is_absent=True)
    ]
    if len(runs) > 1:
        raise Denied(f"{runs[0]} sits inside the review run {runs[-1]}, so it is not a review run")
    return runs[-1] if runs else None


def _roles(run: Path) -> list[dict[str, Any]]:
    """The roles a prepared run's `run.json` names, each with its ID, prompt file, and result file."""
    try:
        state = json_reader.read_file(run / RUN_FILE)
    except (OSError, ValueError) as exc:
        raise Denied(f"the review run {run} could not be read") from exc
    try:
        return READ.array(READ.json_object(state, "run").get("roles"), "run.roles", _role)
    except FieldError as exc:
        raise Denied(f"the review run {run} could not be read") from exc


def _role(value: Any, path: str) -> dict[str, Any]:
    """One role of a run: its ID, prompt file, and result file, each a non-empty string, the self-check it declares,
    when it declares one, and the paths it may read, a non-empty list of them, when the run withholds a pull-request
    text from it."""
    role = READ.json_object(value, path)
    for field in ("id", "prompt_file", "result_file"):
        READ.string(role.get(field), f"{path}.{field}")
    if "self_check" in role:
        READ.string(role["self_check"], f"{path}.self_check")
    if "reads" in role:
        READ.array(role["reads"], f"{path}.reads", READ.string, nonempty=True)
    return role


def claims_folder() -> Path:
    """The claims folder, made owner only when it is missing; the call is denied, naming why, unless it is a folder
    only this user owns and can write, so no claim is read from or written to a folder another account could change."""
    try:
        return private_folder.ensure(CLAIMS)
    except private_folder.UnsafeFolderError as exc:
        raise Denied(f"the reviewer claims folder is not yours alone: {exc}") from exc
    except OSError as exc:
        raise Denied(f"the reviewer claims folder {CLAIMS} cannot be used: {exc}") from exc


def _load_claim(agent: str) -> Claim | None:
    """The run and role this agent claimed, or None when it has claimed none.

    A claim that does not parse, or holds a field of the wrong kind, denies its agent's calls while it is there,
    naming the remedy, a fresh reviewer, rather than reading as no claim: no call can be judged against it, and an
    older guard beside this one may still be writing it in two steps. `_prune_claims` removes it once it is old, which
    widens no reviewer's reads, since no version of the guard let a call through under such a claim: this one creates
    a claim whole or not at all, and an older one denied, or was blocked on, the call whose write failed or was killed
    part way."""
    claims = claims_folder()
    try:
        claim = json_reader.read_file(claims / f"{agent}.json")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise Denied(UNREADABLE_CLAIM) from exc
    try:
        for key in ("run", "role"):
            READ.string(READ.json_object(claim, "claim").get(key), f"claim.{key}", empty=True)
    except FieldError as exc:
        raise Denied(UNREADABLE_CLAIM) from exc
    run = Path(claim["run"])
    role = next((role for role in _roles(run) if role["id"] == claim["role"]), None)
    if role is None:
        raise Denied(f"the review run no longer has the role {claim['role']}")
    return run, role


def _prune_claims(claims: Path) -> None:
    """Remove each claim in `claims`, the folder claims_folder found the user's own, whose run is gone: finalize
    removes a run's folder, run.json with it. A claim whose run.json it cannot stat, or finds something else in place
    of, is kept: the error leaves it, as one it cannot read or stat is.

    A claim that does not parse, or holds a field of the wrong kind, is removed once its modification time is
    `UNREADABLE_CLAIM_AGE` old, so it is not kept for good. Its agent's next read is then a first read again, which may
    claim any role, another than the prompt it first asked for included. That widens no reviewer's reads, as
    `_load_claim` says: no call of that agent was ever let through, its claiming read included, so it has read nothing
    a pull request holds that could choose the role, just as a fresh agent has not. A younger one is kept, since an
    older guard beside this one may still be writing it in two steps. A copy a killed create left beside a claim,
    named `LEFTOVER_COPY`, is never read as one, and is removed once it is `LEFTOVER_COPY_AGE` old.

    A prune removes only the file it judged, so a claim made after it judged the path is never removed. It holds the
    lock of `PRUNE_LOCK` throughout, so no other guard prunes meanwhile: only a prune removes a claim, and a create
    never replaces one, so while the lock is held nothing can put a newer claim where a judged one is. A guard that
    finds the lock held leaves the prune to its holder, and one whose lock cannot be opened, or is refused for a
    reason that may pass, leaves it to a later claim; pruning is only clearing away, so neither denies the call. A file
    system that offers no lock at all lets no guard hold it, so each prunes unlocked there. Each removal is checked
    too: `_prune` removes a file only while its path still names the one judged. Where no lock holds, as on such a
    file system or beside an older guard, which prunes unlocked, that check is all that keeps a newer claim, and a
    claim removed and remade at the path between the check and the removal is still removed."""
    lock = claims / PRUNE_LOCK
    descriptor: int | None = None
    try:
        descriptor = file_lock.take(lock)
    except file_lock.FileLockError as exc:
        if not exc.unsupported:
            return
        # No guard can hold a lock the file system does not offer, so the file is no one's.
        with contextlib.suppress(OSError):
            lock.unlink()
    except OSError:
        return
    else:
        if descriptor is None:
            return
    try:
        for path in file_presence.matching(claims, "*.json"):
            _prune(path, _stale_claim)
        for path in file_presence.matching(claims, LEFTOVER_COPY):
            _prune(path, _leftover_copy)
    finally:
        if descriptor is not None:
            file_lock.release(descriptor, lock)


def _prune(path: Path, stale: Callable[[os.stat_result, BinaryIO], bool]) -> None:
    """Remove `path` when `stale`, given the status and the open content of the file there, judges it one to prune, and
    only while `path` still names that file, so a file put at `path` after the judgment is kept. The file is held open
    from the judgment to the comparison, so its identity is never reused for another meanwhile, and closed before the
    removal, which Windows refuses while a handle without delete sharing holds it. Any OSError keeps the file."""
    with contextlib.suppress(OSError):
        with path.open("rb") as handle:
            judged = os.fstat(handle.fileno())
            if not stale(judged, handle) or not os.path.samestat(judged, path.lstat()):
                return
        path.unlink()


def _stale_claim(status: os.stat_result, content: BinaryIO) -> bool:
    """Whether a claim is one to prune: one whose run's run.json is gone, or one that does not parse, or holds a field
    of the wrong kind, once its modification time is `UNREADABLE_CLAIM_AGE` old."""
    try:
        claim = json_reader.loads(content.read().decode("utf-8"))
        run = READ.string(READ.json_object(claim, "claim").get("run"), "claim.run", empty=True)
    except (ValueError, FieldError):
        return time.time() - status.st_mtime >= UNREADABLE_CLAIM_AGE
    return not file_presence.is_present(Path(run) / RUN_FILE)


def _leftover_copy(status: os.stat_result, _content: BinaryIO) -> bool:
    """Whether a copy beside the claims is one a killed create left: `LEFTOVER_COPY_AGE` old."""
    return time.time() - status.st_mtime >= LEFTOVER_COPY_AGE


def _claim(agent: str, path: Path) -> None:
    """Bind this agent to the role whose prompt it reads first, by creating its claim whole or not at all. When two
    first calls race, whichever linked its claim into place first holds, and the other read is judged against that
    whole claim.

    A create that fails before its claim is in place leaves none and denies the call, naming the error, so the next
    first read claims anew; where the file system refuses the hard link the claim is put in place by, the denial names
    the remedy too. One that put its claim in place but could not remove the copy it wrote it through
    (`UnfinishedCopyError` with `made` set) still denies this call, after starting the role's read log, since the claim
    holds: the reviewer's next read is judged against it."""
    run = run_root(path)
    role = None if run is None else next((r for r in _roles(run) if _same(path, Path(r["prompt_file"]))), None)
    if run is None or role is None:
        raise Denied(f"{FIRST}; {path} is not a reviewer prompt")
    # Serialized before the claim is created, so a run path the JSON writer refuses leaves no claim behind: the
    # refusal fails the guard's evaluation, which denies the call.
    claim = json_writer.dumps({"run": str(run), "role": role["id"]})
    claims = claims_folder()
    _prune_claims(claims)
    target = claims / f"{agent}.json"
    try:
        atomic_file.create_bytes(target, claim.encode("utf-8"), mode=0o600)
    except FileExistsError:
        claimed = _load_claim(agent)
        if claimed is None:
            raise Denied(UNREADABLE_CLAIM) from None
        # Judged as any later read of the claimed role is, its `reads` and the run's restricted paths included.
        _check_allowed("Read", path, claimed)
        return
    except atomic_file.UnfinishedCopyError as exc:
        if not exc.made:
            raise Denied(_unmade(target, exc)) from exc
        _start_log(run, role["id"])
        raise Denied(f"this reviewer's claim is made, but the copy it was written through is left; {FIRST}") from exc
    except OSError as exc:
        raise Denied(_unmade(target, exc)) from exc
    _start_log(run, role["id"])


def _unmade(target: Path, error: OSError) -> str:
    """Why a claim the create left unmade at `target` denies its call: the error, and, where the file system refused
    the hard link that puts a claim in place, which `atomic_file.create_bytes` raises with an errno of
    `NO_HARD_LINK_ERRNOS` naming `target`, the remedy."""
    reason = f"this reviewer's claim could not be made: {error}"
    if error.errno in atomic_file.NO_HARD_LINK_ERRNOS and error.filename == str(target):
        reason += f"; {NO_HARD_LINKS.format(folder=target.parent.parent)}"
    return reason


def _check_in_run(tool: str, path: Path, run: Path, reads: list[str] | None = None) -> None:
    """The path lies in the suite's references or the run; for a role the run withholds a pull-request text from, in
    the references or one of the paths its `reads` names, each of which lies in the run."""
    if within(path, resolved_path(REFERENCES)):
        return
    if reads is None:
        if within(path, resolved_path(run)):
            return
        raise Denied(f"{tool} may only look inside your own review run folder; {path} is outside it")
    allowed = [resolved_path(read) for read in reads]
    if within(path, resolved_path(run)) and any(within(path, root) for root in allowed):
        return
    raise Denied(f"{tool} may only look at the inputs your prompt names; {path} is not one of them")


def _restricted(run: Path) -> dict[str, list[str]]:
    """The run's restricted paths, each with the roles that may read it: a file of the run that only the roles given
    the text it holds may read, such as the closing issues."""
    try:
        state = READ.json_object(json_reader.read_file(run / RUN_FILE), "run")
        restricted = READ.json_object(state.get("restricted", {}), "run.restricted")
        return {
            READ.string(path, "a key of run.restricted"): READ.array(roles, f"run.restricted.{path}", READ.string)
            for path, roles in restricted.items()
        }
    except (OSError, ValueError, FieldError) as exc:
        raise Denied(f"the review run {run} could not be read") from exc


def _check_allowed(tool: str, path: Path, claim: Claim) -> None:
    """A read the claimed role may make: inside its run, or its `reads` when it has them, and never into a restricted
    path, or a folder holding one, that the run does not give the role."""
    run, role = claim
    _check_in_run(tool, path, run, role.get("reads"))
    for restricted, roles in _restricted(run).items():
        target = resolved_path(restricted)
        if role["id"] not in roles and (within(path, target) or within(target, path)):
            raise Denied(f"{tool} may only look at the inputs your prompt names; {path} holds one it does not")


def _check_read(tool: str, tool_input: dict[str, Any], cwd: Path, claim: Claim | None) -> None:
    key = "file_path" if tool == "Read" else "path"
    path = _path(tool_input.get(key), cwd)
    if tool == "Glob":
        try:
            pattern = READ.string(tool_input.get("pattern"), "tool_input.pattern", empty=True)
        except FieldError as exc:
            raise Denied("Glob patterns must be relative to the run folder path, without '..'") from exc
        if ".." in pattern or PureWindowsPath(pattern).anchor or pattern.startswith("/"):
            raise Denied("Glob patterns must be relative to the run folder path, without '..'")
    if claim is None:
        raise Denied(f"{FIRST}; {path} is not a reviewer prompt")
    _check_allowed(tool, path, claim)
    _log_read(tool, path, claim)


def _check_write(tool_input: dict[str, Any], cwd: Path, claim: Claim) -> None:
    path = _path(tool_input.get("file_path"), cwd)
    result = claim[1]["result_file"]
    if not _same(path, _path(result, cwd)):
        raise Denied(f"write only your own result file, {result}; {path} is not it")


def _check_bash(tool_input: dict[str, Any], cwd: Path, claim: Claim) -> None:
    try:
        text = READ.string(tool_input.get("command"), "tool_input.command", empty=True).strip()
    except FieldError:
        text = ""  # no command, which no allowed form matches
    declared = claim[1].get("self_check")
    if declared is not None:
        if text != declared:
            raise Denied("run no command except the self-check the prompt gives, exactly as written")
        return
    match = SELF_CHECK.match(text)
    if match is not None and _same(_path(match.group("script"), cwd), PIPELINE):
        _check_own_run(match, cwd, claim, "self-check")
        return
    match = SOURCE_COMMAND.match(text)
    if (
        match is not None
        and _same(_path(match.group("script"), cwd), SOURCE_SCRIPT)
        and SOURCE_OPTIONS[match.group("command")] == match.group("option")
    ):
        _check_own_run(match, cwd, claim, match.group("command"))
        return
    raise Denied("run no command except the self-check and source commands the prompt gives, exactly as written")


def _check_own_run(match: re.Match[str], cwd: Path, claim: Claim, name: str) -> None:
    """The command, named `name` in a denial, names the reviewer's own review run folder and role."""
    run = _path(match.group("run"), cwd)
    root = run_root(run)
    if root is None or not _same(root, run):
        raise Denied(f"the {name} must name the review run folder")
    if not _same(run, claim[0]) or match.group("role") != claim[1]["id"]:
        raise Denied(f"the {name} must name your own role and review run")


def _check(tool: str, tool_input: dict[str, Any], cwd: Path, agent: str) -> None:
    claim = _load_claim(agent)
    if claim is None and tool == "Read":
        _claim(agent, _path(tool_input.get("file_path"), cwd))
    elif tool in READ_TOOLS:
        _check_read(tool, tool_input, cwd, claim)
    elif claim is None:
        raise Denied(FIRST)
    elif tool in WRITE_TOOLS:
        _check_write(tool_input, cwd, claim)
    else:
        _check_bash(tool_input, cwd, claim)


def reviewer_agent_type(plugin: str | None) -> str:
    """The subagent type a reviewer role starts as: the deployed agent's name, or a plugin's namespaced one."""
    return REVIEWER if plugin is None else f"{plugin}:{REVIEWER}"


def _another_agents_call(event: dict[str, Any], plugin: str) -> bool:
    """Whether a call the plugin's hook sees is the main session's or another agent's, which the guard leaves alone.
    Denied when it is a subagent's whose type the event does not give."""
    if event.get("agent_id") in (None, ""):
        return True
    agent_type = event.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type:
        raise Denied("the hook event names a subagent but not its type, so the guard cannot tell a reviewer apart")
    return agent_type != reviewer_agent_type(plugin)


def decide(event: dict[str, Any], plugin: str | None = PLUGIN) -> str | None:
    """The reason to deny this tool call, or None to allow it. `plugin` is the plugin the guard runs from, whose
    hook sees every session's calls, or None for the deployed agent's own hook."""
    tool = READ.string(event.get("tool_name", ""), "hook event.tool_name", empty=True)
    if tool not in READ_TOOLS | WRITE_TOOLS | {"Bash"}:
        return None
    try:
        if plugin is not None and _another_agents_call(event, plugin):
            return None
        try:
            tool_input = READ.json_object(event.get("tool_input"), "hook event.tool_input")
        except FieldError as exc:
            raise Denied("the tool input could not be read") from exc
        try:
            agent = READ.string(event.get("agent_id"), "hook event.agent_id")
        except FieldError:
            agent = ""
        if not AGENT_ID.fullmatch(agent):
            raise Denied("the hook event carries no agent ID, so the guard cannot tell which reviewer is calling")
        cwd = READ.optional(READ.string, event.get("cwd"), "hook event.cwd", empty=True)
        _check(tool, tool_input, Path(cwd or Path.cwd()), agent)
    except Denied as exc:
        return f"Code-review reviewer boundary: {exc}."
    return None


def main(stream: str) -> int:
    try:
        try:
            # An event holding a lone surrogate is refused, so the call is denied: Claude Code opens such a path with
            # U+FFFD in its place, another path than the one the guard would judge.
            event = READ.json_object(json_reader.loads(stream), "hook event")
        except FieldError:
            event = None
        reason = "the hook event could not be read" if event is None else decide(event)
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - fail closed: an unevaluated call could read the wrong code
        reason = f"Code-review reviewer boundary: the guard could not evaluate this call ({type(exc).__name__})."
    if reason:
        # A reason JSON cannot carry, such as one quoting a resolved path whose name decoded to a lone surrogate,
        # gives way to UNWRITABLE_REASON, so a denial is always written: a FAILED line instead would block the call,
        # as the reviewer agent's hook declares onFailure: block, but give the model a hook error, not the reason.
        print(hook_decision.denial(reason, UNWRITABLE_REASON))
    return 0


def hook() -> int:
    """Answer the hook event on stdin. A stream that cannot be read, such as bytes the console's code page cannot
    decode, is an unreadable event, and is denied like one, never left to the runner's FAILED line: Claude Code reads
    that as a hook error, which blocks the call where the reviewer agent's hook declares onFailure: block, but the
    denial is written so its reason reaches the model."""
    try:
        stream = sys.stdin.read()
    except (OSError, ValueError):
        stream = ""
    return main(stream)


if __name__ == "__main__":
    run_main(hook)
