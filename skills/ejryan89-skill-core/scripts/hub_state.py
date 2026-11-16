"""How a repository's main checkout differs from the state it keeps between changes: clean, on its default branch,
and at the commit its remote's default branch held when git last fetched it.

Where parallel sessions work beside a main checkout, the hub, each change belongs in a worktree, and the hub only
follows the remote. A write to the hub a session makes by mistake, which no guard saw, shows here as one of these
differences. The shepherd-pull-requests tick and repo-cleanup run this check after their own steps, so such a write
is reported at the next run, naming what differs.

Each difference is a kind and a sentence:

    changed   it has <n> uncommitted or untracked path(s): <path>, ...   status lists them, the first few named
    branch    it is on <branch>, not <default>, or on a detached HEAD
    unknown   git holds no <remote>/<default> to compare it with
    ahead     it is <n> commit(s) ahead of <remote>/<default>
    behind    it is <n> commit(s) behind <remote>/<default>

A checkout git cannot read raises GitError with git's reason.

The hub's git configuration is shared by every worktree, so a program that writes it, which no guard reads, can turn
the hub guard off by setting `coding-agent-skills.hubGuard` to anything but true, or add a hook path or an alias. The
check reads that file's own settings with `git config --local`, not following an include, and keeps, between the
shepherd's ticks, a record of them in the hub's git directory, RECORD_NAME beside its configuration file: whether the
guard was on, whether any tick saw it on (the clone's opting in), and a SHA-256 digest of each setting's values, never
the values themselves, keyed by the setting's name with a backslash doubled and each byte that is not UTF-8 written as
`\\x<hex>`, so a name in any encoding has one key JSON can hold. `changes` compares the hub with the record, and with
`keep` writes the record anew; a first tick, with no record, reports nothing for it. A record that cannot be read is
named, and the one written in its place records the clone as opted in, since whether it was is unknown: a clone that
never opted in is then named `lapsed` until the record is deleted. `lapse` names a hub whose guard is off while the
record says the clone opted in, and reads nothing else, so a clone no tick recorded is never named. A
`branch.<name>.<key>` setting of any branch but the hub's own is left out of the digests, since git writes one in the
shared file for a worktree's branch at its first `push -u` and drops it with the branch, and so is the guard's own,
which its own line covers.

Beside the configuration the hub's git directory holds other files every worktree shares: a hook in `hooks/` runs at
a commit or a push in any of them, and `info/exclude` and `info/attributes` apply in each, an exclude entry hiding an
untracked hub file from the status `differences` reads. The record keeps a SHA-256 digest of each too, never its
contents: of each entry of `hooks/` by its name, escaped as a setting's name is, but the samples git writes there,
whose `.sample` names it never runs, and of each info file by its path. A hook's digest covers its bytes and whether
its owner may run it, since git runs a hook only when it is executable, so making one executable is a change; an info
file's covers its bytes alone, since git reads it whatever its mode. Each is read through a link, as git reads it, so
a link to nothing is no file, and an entry of `hooks/` that is not a regular file is digested as its kind alone. A
first tick, and the first after a record written before these files were read, which holds none, names none of them
and records them. A file that cannot be read, or a `hooks/` that cannot be listed, raises GitError with the reason.
Their kinds:

    unguarded  its coding-agent-skills.hubGuard was true at the previous tick and is no longer
    configured its git configuration changed since the previous tick: <key> added|changed|removed, ...
    hooks      its git hooks or info files changed since the previous tick: <path> added|changed|removed, ...
    lapsed     its coding-agent-skills.hubGuard is not true, though a tick recorded that this clone opted in
    record     its record of the previous tick, <path>, could not be read|written: <reason>

Only `changes` with `keep`, which the shepherd's tick calls, writes the record; `lapse`, which repo-cleanup calls,
only reads it. Its limits:

- The record is a detector's memory, not a control: whatever can write the configuration can delete or rewrite the
  record beside it.
- A setting in a file the configuration includes, the worktree-specific `config.worktree`, or the global or system
  file is not read.
- A branch's own settings, other than the hub's own branch's, its `remote`, `pushRemote`, and `merge` among them, are
  not read, so a worktree branch whose pushes were sent elsewhere is not named.
- Deleting the record forgets the clone's opting in, which is how a clone that turned the guard off on purpose stops
  `lapsed`.
- The hooks a `core.hooksPath` setting points at, elsewhere than `hooks/`, are not read; the setting itself is named
  as a configuration change. Nor is anything a hook runs beyond its own file, or what a folder in `hooks/` holds.
- No other file in the git directory is read, and a hook or an info file that stays unreadable is named at each tick
  as a hub that could not be read, its other changes unrecorded until it can be read.
- Two names that differ only in whitespace are named alike in a line, though the record tells them apart.
- A hook is read whole at each tick, so a very large file in `hooks/` costs its size in memory.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import json_reader
import json_writer
import whole_number
from file_presence import ABSENT, is_present
from flat_text import flat_text
from git_client import GitClient, GitError, output_line, output_lines
from typed_fields import FieldError, FieldReader

# How many changed paths a difference names before it counts the rest.
NAMED_PATHS = 3
# The status letters of an entry that names the path it came from in a field of its own after its own.
FROM_FIELD = frozenset("RC")
# The setting by which a clone opts in to the hub guard, tools/hub_guard.py, which reads it from here.
GUARD_SETTING = "coding-agent-skills.hubGuard"
# The record of the previous tick's configuration, in the hub's git directory beside its configuration file.
RECORD_NAME = "hub-state.json"
RECORD_VERSION = 1
# How many changed settings or files a difference names before it counts the rest.
NAMED_CHANGES = 3
# The section of the settings git writes in the shared configuration for each branch, `branch.<name>.<key>`.
BRANCH_SECTION = "branch"
# The folder of hooks in the git directory every worktree shares, and the suffix of the samples git writes there,
# which it never runs, since it runs a hook only by the hook's own name.
HOOKS_FOLDER = "hooks"
SAMPLE_SUFFIX = ".sample"
# The files in that git directory git reads in every worktree for paths to ignore and for paths' attributes.
INFO_FILES = ("info/exclude", "info/attributes")

GIT_STATUS_ALLOWED = {
    "guarded": (
        "reads the opt-in as tools/hub_guard.py's guard_enabled does: on only when git reads the value as true, so "
        "an unset one (exit 1) and one git cannot read as a boolean are off, as the guard runs then"
    ),
}


@dataclass(frozen=True)
class Difference:
    """One way the checkout differs: its kind, one of the module's, and a sentence that says it on one line."""

    kind: str
    text: str


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def changed_paths(fields: list[str]) -> list[str]:
    """The paths `git status --porcelain -z` lists, each once: a rename's or a copy's path, never the one it came
    from."""
    paths: list[str] = []
    entries = iter(fields)
    for entry in entries:
        paths.append(entry[3:])
        if entry[:1] in FROM_FIELD or entry[1:2] in FROM_FIELD:
            next(entries, None)
    return paths


def tracking_commit(git: GitClient, checkout: Path, tracking: str) -> str | None:
    """The commit the one ref `tracking` names, read by its full name alone, or None where git holds no such ref."""
    listed = git.output(["for-each-ref", "--format=%(objectname)%00%(refname)", tracking], directory=checkout)
    for line in output_lines(listed):
        commit, _, name = line.partition("\0")
        if name == tracking:
            return commit
    return None


def differences(git: GitClient, checkout: Path, branch: str, remote: str = "origin") -> list[Difference]:
    """How `checkout` differs from a clean checkout of `branch` at the commit `<remote>/<branch>` holds: none when it
    stands so."""
    found: list[Difference] = []
    paths = changed_paths(git.names(["status", "--porcelain", "--untracked-files=normal"], directory=checkout))
    if paths:
        named = ", ".join(flat_text(path) for path in paths[:NAMED_PATHS])
        rest = f", and {len(paths) - NAMED_PATHS} more" if len(paths) > NAMED_PATHS else ""
        found.append(
            Difference("changed", f"it has {plural(len(paths), 'uncommitted or untracked path')}: {named}{rest}")
        )
    # Exit 1 with nothing on stderr is git's answer that HEAD is detached; any other failure raises with git's reason.
    name = output_line(git.quiet_output(["symbolic-ref", "--quiet", "--short", "HEAD"], directory=checkout) or "")
    if name != branch:
        found.append(Difference("branch", f"it is on {flat_text(name) or 'a detached HEAD'}, not {branch}"))
    tracking = f"refs/remotes/{remote}/{branch}"
    commit = tracking_commit(git, checkout, tracking)
    if commit is None:
        found.append(Difference("unknown", f"git holds no {remote}/{branch} to compare it with"))
        return found
    counts = output_line(git.output(["rev-list", "--left-right", "--count", f"HEAD...{commit}"], directory=checkout))
    unread = GitError(f"git rev-list answered {counts!r} for how far HEAD is from {remote}/{branch}, not two counts")
    left, _, right = counts.partition("\t")
    ahead, behind = whole_number.read(left, unread), whole_number.read(right, unread)
    if ahead:
        found.append(Difference("ahead", f"it is {plural(ahead, 'commit')} ahead of {remote}/{branch}"))
    if behind:
        found.append(Difference("behind", f"it is {plural(behind, 'commit')} behind {remote}/{branch}"))
    return found


class RecordError(ValueError):
    """A record of the previous tick that is not one `write_record` writes."""


class RecordField(FieldError, RecordError):
    """A field of the record of another kind than the record's."""


@dataclass(frozen=True)
class Configuration:
    """What the check reads of a hub's shared git directory: whether the guard is on, a digest of each other setting's
    values, keyed by the setting's name as git lists it, and a digest of each watched file, keyed by its path in that
    directory, None in a record written before files were read."""

    guarded: bool
    settings: dict[str, str]
    files: dict[str, str] | None


@dataclass(frozen=True)
class Record:
    """The configuration the previous tick read, and whether any tick saw the guard on."""

    configuration: Configuration
    opted_in: bool


def guarded(git: GitClient, checkout: Path) -> bool:
    """Whether the hub guard is on: the opt-in read from the repository's own configuration file, as the guard reads
    it."""
    result = git.run(["config", "--local", "--type=bool", "--get", GUARD_SETTING], directory=checkout)
    return result.returncode == 0 and output_line(result.stdout) == "true"


def is_watched(key: str, branch: str) -> bool:
    """Whether a setting, named as git lists it, is one whose change the check reports: any but the guard's own and
    the settings of a branch other than `branch`, the hub's own."""
    section, _, rest = key.partition(".")
    if section.lower() == BRANCH_SECTION and "." in rest:
        return rest.rpartition(".")[0] == branch
    return key.lower() != GUARD_SETTING.lower()


def raw_bytes(text: str) -> bytes:
    """The bytes git printed for `text`, which GitClient decodes with surrogateescape, so a byte that is not UTF-8
    comes back as the byte it was."""
    return text.encode("utf-8", "surrogateescape")


def escaped_name(raw: bytes) -> str:
    """A name, given as its bytes, in a form JSON holds and a line prints, one for each name: a backslash doubled,
    then each byte that is not UTF-8 written as `\\x<hex>`."""
    return raw.replace(b"\\", b"\\\\").decode("utf-8", "backslashreplace")


def setting_key(key: str) -> str:
    """The setting's name, as git printed it, escaped as `escaped_name` escapes its bytes."""
    return escaped_name(raw_bytes(key))


def unreadable(exc: OSError) -> GitError:
    """The GitError a watched file or folder that cannot be read raises, naming the reason, so the tick names the hub
    as one it could not read, as it names a configuration git cannot read."""
    return GitError(flat_text(str(exc)))


def file_digest(path: Path, *, executable: bool) -> str | None:
    """A digest of what git reads of the file at `path`, following a link: its bytes, and with `executable` whether
    its owner may run it, since git runs only a hook that is executable; a folder or anything else that is not a
    regular file is digested as its kind alone. None where nothing is there, a link to nothing among them; a file that
    cannot be read raises GitError with the reason."""
    try:
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode):
            return hashlib.sha256(b"other").hexdigest()
        content = path.read_bytes()
    except ABSENT:
        return None
    except OSError as exc:
        raise unreadable(exc) from exc
    runs = b"x" if executable and mode & stat.S_IXUSR else b"-"
    return hashlib.sha256(b"file\0" + runs + b"\0" + content).hexdigest()


def watched_files(common: Path) -> dict[str, str]:
    """A digest of each entry of the hooks folder in the git directory `common` but git's samples, and of each info
    file git reads, keyed by its path in that directory with a hook's name escaped as `escaped_name` escapes its
    bytes; one that cannot be read, or a hooks folder that cannot be listed, raises GitError with the reason."""
    hooks = common / HOOKS_FOLDER
    try:
        with os.scandir(hooks) as listing:
            names = sorted(entry.name for entry in listing)
    except ABSENT:
        names = []
    except OSError as exc:
        raise unreadable(exc) from exc
    files: dict[str, str] = {}
    for name in names:
        digest = None if name.endswith(SAMPLE_SUFFIX) else file_digest(hooks / name, executable=True)
        if digest is not None:
            files[f"{HOOKS_FOLDER}/{escaped_name(os.fsencode(name))}"] = digest
    for name in INFO_FILES:
        digest = file_digest(common / name, executable=False)
        if digest is not None:
            files[name] = digest
    return files


def configuration(git: GitClient, checkout: Path, branch: str, common: Path) -> Configuration:
    """The guard's state, a digest of each watched setting the configuration file of `checkout`'s repository holds,
    `branch` being the hub's own, and of each watched file in its git directory `common`; a configuration git cannot
    read raises GitError with git's reason, and a watched file that cannot be read with the reason."""
    values: dict[str, list[str]] = {}
    for entry in git.names(["config", "--local", "--list"], directory=checkout):
        # With -z git ends a setting's name with a newline, which no name holds, and prints no newline for a name
        # given without a value, which git reads as true where an empty value reads as false: the newline is
        # digested with the value, so rewriting one as the other is a change.
        key, separator, value = entry.partition("\n")
        if is_watched(key, branch):
            values.setdefault(setting_key(key), []).append(separator + value)
    settings = {key: hashlib.sha256(raw_bytes("\0".join(listed))).hexdigest() for key, listed in values.items()}
    return Configuration(guarded(git, checkout), settings, watched_files(common))


def common_directory(git: GitClient, checkout: Path) -> Path:
    """The git directory every worktree of `checkout`'s repository shares."""
    common = git.rev_parse_path("--git-common-dir", directory=checkout)
    if common is None:
        raise GitError(f"{checkout.as_posix()} is not in a git repository")
    return common


def record_path(git: GitClient, checkout: Path) -> Path:
    """Where the record of `checkout`'s repository lies: in the git directory every worktree of it shares."""
    return common_directory(git, checkout) / RECORD_NAME


def read_record(path: Path) -> Record | None:
    """The record at `path`, or None where there is none; RecordError for one that cannot be read or is not one
    `write_record` writes."""
    try:
        if not is_present(path):
            return None
        value = json_reader.read_file(path)
    except OSError as exc:
        raise RecordError(flat_text(str(exc))) from exc
    except json_reader.CONTENT_ERRORS as exc:
        raise RecordError(f"it is not JSON: {flat_text(str(exc))}") from exc
    return record_of(value)


def record_of(value: Any) -> Record:
    reader = FieldReader(RecordField)
    fields = reader.json_object(
        value, "record", required=("version", "guarded", "opted_in", "settings"), optional=("files",)
    )
    version = reader.integer(fields["version"], "record.version")
    if version != RECORD_VERSION:
        raise RecordField("record.version", f"{RECORD_VERSION}", str(version))

    def digests(field: str) -> dict[str, str]:
        listed = reader.json_object(fields[field], f"record.{field}")
        return {
            key: reader.hex_digest(digest, f"record.{field}[{flat_text(key)}]", 64) for key, digest in listed.items()
        }

    # A record written before the check read files holds none, and the next tick compares none.
    files = digests("files") if "files" in fields else None
    configured = Configuration(reader.boolean(fields["guarded"], "record.guarded"), digests("settings"), files)
    return Record(configured, reader.boolean(fields["opted_in"], "record.opted_in"))


def write_record(path: Path, record: Record) -> None:
    """Replace the record at `path`; an OSError where it cannot be written."""
    value = {
        "version": RECORD_VERSION,
        "guarded": record.configuration.guarded,
        "opted_in": record.opted_in,
        "settings": record.configuration.settings,
    }
    if record.configuration.files is not None:
        value["files"] = record.configuration.files
    json_writer.write(path, value, indent=2, sort_keys=True)


def named_changes(previous: Mapping[str, str], current: Mapping[str, str]) -> str:
    """Each key added, changed, or removed between two maps of digests, the first few named and the rest counted, or
    nothing where none was."""
    changed = [
        f"{flat_text(key)} {'added' if key not in previous else 'removed' if key not in current else 'changed'}"
        for key in sorted(previous.keys() | current.keys())
        if previous.get(key) != current.get(key)
    ]
    rest = f", and {len(changed) - NAMED_CHANGES} more" if len(changed) > NAMED_CHANGES else ""
    return ", ".join(changed[:NAMED_CHANGES]) + rest


def record_problem(path: Path, what: str, reason: str) -> Difference:
    said = flat_text(reason)
    return Difference("record", f"its record of the previous tick, {path.as_posix()}, could not be {what}: {said}")


def configuration_differences(previous: Configuration, current: Configuration) -> list[Difference]:
    """How the configuration changed since `previous`: the guard turned off, each watched setting added, changed, or
    removed, and each watched file, the first few of each named; no file where `previous` holds none."""
    found: list[Difference] = []
    if previous.guarded and not current.guarded:
        found.append(Difference("unguarded", f"its {GUARD_SETTING} was true at the previous tick and is no longer"))
    if named := named_changes(previous.settings, current.settings):
        found.append(Difference("configured", f"its git configuration changed since the previous tick: {named}"))
    if (
        previous.files is not None
        and current.files is not None
        and (named := named_changes(previous.files, current.files))
    ):
        found.append(Difference("hooks", f"its git hooks or info files changed since the previous tick: {named}"))
    return found


def changes(git: GitClient, checkout: Path, branch: str, *, keep: bool) -> list[Difference]:
    """How the hub's configuration changed since the record of the previous tick, nothing where there is none, `branch`
    being the hub's own; with `keep`, the record is then replaced by what this tick read, recording the clone as opted
    in where the previous record could not be read. A configuration git cannot read, or a watched file that cannot be
    read, raises GitError."""
    common = common_directory(git, checkout)
    path = common / RECORD_NAME
    current = configuration(git, checkout, branch, common)
    found: list[Difference] = []
    unread = False
    try:
        previous = read_record(path)
    except RecordError as exc:
        found.append(record_problem(path, "read", str(exc)))
        previous, unread = None, True
    if previous is not None:
        found.extend(configuration_differences(previous.configuration, current))
    if keep:
        # Whether a record that cannot be read said the clone opted in is unknown, so it is taken to have said so:
        # lapse then names a guard that is off rather than forget an opting in.
        opted_in = current.guarded or unread or (previous is not None and previous.opted_in)
        try:
            write_record(path, Record(current, opted_in))
        except (OSError, ValueError) as exc:
            found.append(record_problem(path, "written", str(exc)))
    return found


def lapse(git: GitClient, checkout: Path) -> Difference | None:
    """The hub guard off in a clone whose record says it opted in; None where it is on, where no record says so, and
    where there is no record. A configuration git cannot read raises GitError."""
    path = record_path(git, checkout)
    try:
        record = read_record(path)
    except RecordError as exc:
        return record_problem(path, "read", str(exc))
    if record is None or not record.opted_in or guarded(git, checkout):
        return None
    return Difference("lapsed", f"its {GUARD_SETTING} is not true, though a tick recorded that this clone opted in")
