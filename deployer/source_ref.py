"""`deploy.py --at <ref>`: the commit a ref names in the source checkout, and that commit's tree to deploy from.

The deployer that runs is the checkout's own; only the templates come from the ref, and the pipeline validates them as
it validates any source's. A ref's own deployer is code nobody reviewed for this run, so it never runs.
"""

from __future__ import annotations

import re
import sys
import tarfile
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import file_presence
from flat_text import flat_text
from git_client import GitClient, GitError, GitResult
from release_tags import TAGS, last_release

from . import fsops, platform_support, tools
from .arguments import usage_error
from .errors import DeployError, see_recovery
from .source_commit import COMMIT_PATTERN

# What --at accepts, and what the manifest records as source_ref: a branch, a tag, a commit name, or a revision
# expression over them such as main~2, never a name starting with "-", which git would read as an option, and nothing
# a shell, a terminal, or a JSON reader gives a meaning to.
REF_PATTERN = re.compile(r"^(?!-)[A-Za-z0-9._/@{}~^+-]{1,255}$")
REF_RULES = "A ref may not start with '-' and may hold only letters, digits, and . _ / @ { } ~ ^ + -."
EARLIER_COMMIT = "Deploying an earlier commit"
# The attributes git archive applies and a checkout does not, with what git archive does to a path one is set on: an
# export honouring them would not hold the commit's tree, so --at refuses a commit that sets either.
ARCHIVE_ONLY_ATTRIBUTES = {
    "export-ignore": "leave it out",
    "export-subst": "replace its $Format:...$ placeholders",
}
# One entry of `git ls-tree -z` without -l: mode, kind, object name, a tab, and the path, whatever bytes it holds.
TREE_ENTRY = re.compile(
    r"(?P<mode>[0-7]{6}) (?P<kind>blob|tree|commit) (?:[0-9a-f]{40}|[0-9a-f]{64})\t(?P<name>.+)", re.DOTALL
)
# The first Git whose `check-attr` takes `--source`, reading a commit's attributes; docs/installation.md states it.
ATTRIBUTE_SOURCE_GIT = "2.40"
# The path characters one `git check-attr` call is given, well inside what a Windows command line holds.
ATTRIBUTE_PATH_UNITS = 8000

# Filesystem writes that tests/run_validation.py allows outside deployer/fsops.py, with the reason.
FSOPS_ALLOWED = {
    "tempfile.mkdtemp": "--at exports a commit's tree into a fresh directory under the system temporary directory, "
    "outside every managed root, which the run removes through fsops when it ends; it changes no deployed state",
}


@dataclass(frozen=True)
class Pin:
    """The ref --at names, as given, and the full name of the commit it resolved to."""

    ref: str
    commit: str


def check_ref(ref: str) -> None:
    """Refuse a ref --at cannot pass to git as a revision, before git or anything else runs."""
    if not REF_PATTERN.fullmatch(ref):
        raise usage_error(f"--at must name a branch, tag, or commit of this checkout (got: {ref!r})", REF_RULES)


class TagsUnreadable(Exception):
    """Git could not read the release tags reaching a pinned commit; the message is Git's reason, on one line."""


def _run_git(
    arguments: Sequence[str], timeout: float | None = None, *, separate_errors: bool = False
) -> platform_support.ToolResult:
    git = platform_support.find_executable("git")
    if git is None:
        raise DeployError(
            "ERROR: --at needs Git, which was not found.",
            'Install it as "Installing the tools" in docs/installation.md says, then retry.',
        )
    return platform_support.run_tool([git, *arguments], timeout=timeout, separate_errors=separate_errors)


def _git(source_dir: Path, *arguments: str) -> platform_support.ToolResult:
    return _run_git(["-C", str(source_dir), *arguments])


def _git_runner(command: Sequence[str], timeout: float) -> GitResult:
    """GitClient's runner over the deployer's own, for resolve and release_tags: the command GitClient builds, git
    first, run by run_tool with stdout and stderr kept apart, so a warning git prints beside an answer is never part of
    it and a failure's reason is git's stderr."""
    result = _run_git(command[1:], timeout, separate_errors=True)
    return GitResult(result.returncode, result.output, result.errors)


def _client() -> GitClient:
    return GitClient(_git_runner, timeout=platform_support.TOOL_DEADLINE_SECONDS)


def resolve(source_dir: Path, ref: str) -> Pin:
    """The commit ref names in the checkout, which must already hold it: the deployer never fetches.

    Only git's answer that the ref names no commit is refused as such, with the advice to fetch; any other failure, such
    as a broken ref git warns about or a configuration it cannot parse, is refused with git's reason.
    """
    check_ref(ref)
    shown = platform_support.normalize(source_dir)
    # A .git whose stat fails is refused as no checkout: its False stops --at before anything runs, as git would.
    if not (source_dir / ".git").exists():
        raise DeployError(f"ERROR: --at needs a Git checkout, and {shown} is not one.")
    # commit_of ends the options before the ref, so a ref is never read as an option even if the pattern above ever
    # let one through, and answers None only when git says the ref names no commit.
    try:
        commit = _client().commit_of(ref, directory=source_dir)
    except GitError as error:
        reason = flat_text(str(error))
        if error.kind == "not_repository":
            raise DeployError(f"ERROR: --at needs a Git checkout, and {shown} is not one: {reason}") from error
        raise DeployError(
            f"ERROR: Git could not read --at {ref} in {shown}: {reason}",
            "Put right what Git reports in the checkout, then retry.",
            see_recovery(EARLIER_COMMIT),
        ) from error
    if commit is None:
        raise DeployError(
            f"ERROR: --at {ref} names no commit in {shown}.",
            "The deployer does not fetch: run 'git fetch --tags origin' in the checkout, or name a commit it holds, "
            "then retry.",
            see_recovery(EARLIER_COMMIT),
        )
    return Pin(ref, commit)


def is_release_pin(source_dir: Path, ref: str | None, commit: str | None) -> bool:
    """Whether a pin is update-coding-agent-skills' own: its ref is refs/tags/<the last release its commit holds>.

    The release channel pins each release by its tag's full name, so a pin with a short name, another tag, or a tag a
    later release on the same commit has overtaken is the user's. The last release is the one release_tags names, the
    definition every reader of a release asks. Raises TagsUnreadable with Git's reason when Git cannot read the tags
    reaching the commit, or the checkout's .git when it cannot tell whether one is there, so no owner is guessed for a
    pin whose evidence was never produced.
    """
    if ref is None or commit is None or not ref.startswith(TAGS):
        return False
    try:
        checkout = file_presence.is_occupied(source_dir / ".git")
    except OSError as error:
        raise TagsUnreadable(flat_text(f"{error.filename}: {error.strerror or error}")) from error
    if not checkout:
        return False
    try:
        release = last_release(_client(), source_dir, commit)
    except GitError as error:
        raise TagsUnreadable(flat_text(str(error))) from error
    return release is not None and ref == f"{TAGS}{release}"


def replaceable_release_pin(source_dir: Path, replaced: str, ref: str | None, commit: str | None) -> bool:
    """is_release_pin for a run that would replace the pin `replaced` describes. A pin whose release tags Git cannot
    read is refused with Git's reason, never taken for the user's, so the refusal names the fault rather than advise
    --unpin over a pin the user may never have made."""
    try:
        return is_release_pin(source_dir, ref, commit)
    except TagsUnreadable as error:
        raise DeployError(
            f"ERROR: Refusing to replace {replaced}: Git could not read the release tags reaching it, so whether it is "
            f"update-coding-agent-skills' own pin is unknown: {error}",
            "Put right what Git reports in the checkout, then retry; nothing was replaced.",
            see_recovery(EARLIER_COMMIT),
        ) from error


def recorded_ref(source_dir: Path, pin: Pin | None, ref: str | None, commit: str | None, *, unpin: bool) -> str | None:
    """The ref the manifest records a deployment pinned with, given the pin it records now: --at's, or None without.

    The ref's form tells update-coding-agent-skills' own pin from the user's (is_release_pin), so the user's form wins
    at the pinned commit. A user's pin keeps its ref as written through a deployment at its commit unless that passes
    --unpin, since recording the update's refs/tags/<tag> over it would let the next release move it. Over the update's
    own pin, as over any other, the ref --at names is recorded, so a user's pin there is recorded as the user's. When
    Git cannot read the pinned commit's tags, whose the recorded ref is cannot be told, and the run has deployed by
    then: a ref --at names in a form only a user gives, a short name or a commit, is recorded, since the pin is the
    user's whoever made the recorded one, and a ref --at names in full is not, so the recorded ref is kept as written.
    """
    if pin is None:
        return None
    if ref is None or pin.commit != commit or unpin:
        return pin.ref
    try:
        own = is_release_pin(source_dir, ref, commit)
    except TagsUnreadable:
        own = not pin.ref.startswith(TAGS)
    return pin.ref if own else ref


def export(source_dir: Path, commit: str) -> Path:
    """A fresh directory under the system temporary directory whose `tree` folder holds the commit's files.

    git archive writes the files as a checkout of the commit would, line endings, `ident`, and filters included, so
    the tree is the one a deployment from a checkout of the commit reads. Only the attributes git archive applies and
    a checkout does not, export-ignore and export-subst, would make it another tree, so a commit on any path of whose
    tree git archive would act on either is refused before anything is made (see `archive_attributes`). A tree holding
    a link or a special file is refused before anything is extracted, and the extraction's data filter refuses any
    member that would land outside the folder; either refusal names the member.
    """
    if not COMMIT_PATTERN.fullmatch(commit):
        raise ValueError(f"not a full commit name: {commit!r}")
    require_extraction_filters()
    refuse_archive_attributes(source_dir, commit)
    root = Path(tempfile.mkdtemp(prefix="deploy-at-"))
    try:
        archive = root / "tree.tar"
        result = _git(source_dir, "archive", "--format=tar", f"--output={archive}", commit)
        if result.returncode != 0:
            raise DeployError(f"ERROR: git archive could not export {commit}: {flat_text(result.output)}")
        fsops.make_directory(root / "tree")
        try:
            _refuse_links(archive, commit)
            fsops.extract_archive(archive, root / "tree")
        except tarfile.TarError as exc:
            # The data filter's refusal names the member and why; any other TarError is an archive tarfile cannot read.
            reason = flat_text(str(exc))
            raise DeployError(f"ERROR: The deployer refused to extract the tree of {commit}: {reason}.") from exc
    except BaseException:
        remove(root)
        raise
    return root


def _tree_paths(source_dir: Path, commit: str) -> list[str]:
    """Each path of the commit's tree, folders and submodules included, as git archive asks for its attributes: a
    folder or a submodule with a `/` after it, which a pattern such as `skills/beta/ export-ignore` matches."""
    paths: list[str] = []
    try:
        records = _client().names(["ls-tree", "-r", "-t", "--full-tree", commit], directory=source_dir)
    except GitError as error:
        raise _attributes_unreadable(commit, flat_text(str(error))) from error
    for record in records:
        entry = TREE_ENTRY.fullmatch(record)
        if entry is None:
            raise _attributes_unreadable(commit, f"git ls-tree listed an entry the deployer cannot read: {record!r}")
        name = entry["name"]
        paths.append(f"{name}/" if entry["kind"] in {"tree", "commit"} else name)
    return paths


def _attribute_calls(paths: Sequence[str]) -> list[list[str]]:
    """The paths split into as few `git check-attr` calls as hold them, each within ATTRIBUTE_PATH_UNITS."""
    calls: list[list[str]] = []
    units = ATTRIBUTE_PATH_UNITS
    for path in paths:
        if units + len(path) + 1 > ATTRIBUTE_PATH_UNITS:
            calls.append([])
            units = 0
        calls[-1].append(path)
        units += len(path) + 1
    return calls


def archive_attributes(source_dir: Path, commit: str) -> list[tuple[str, str]]:
    """Each (path, attribute) of the commit's tree on which an attribute of ARCHIVE_ONLY_ATTRIBUTES is set, in tree
    order, read by `git check-attr --source`, which reads the commit's own .gitattributes files with the checkout's
    .git/info/attributes and Git's attributes files, as git archive reads them, macros included. An attribute counts
    when Git answers `set`, the one value git archive acts on, and export-subst only on a file, the one entry whose
    bytes git archive rewrites.

    Raises DeployError with Git's reason when Git cannot list the tree or read its attributes, and when its answer is
    not the one asked for, path by path and attribute by attribute, so no unread answer passes as none set.
    """
    found: list[tuple[str, str]] = []
    for call in _attribute_calls(_tree_paths(source_dir, commit)):
        arguments = ["check-attr", "-z", f"--source={commit}", *ARCHIVE_ONLY_ATTRIBUTES, "--", *call]
        try:
            fields = _client().names(arguments, directory=source_dir)
        except GitError as error:
            raise _attributes_unreadable(commit, flat_text(str(error))) from error
        asked = [(path, attribute) for path in call for attribute in ARCHIVE_ONLY_ATTRIBUTES]
        answered = [tuple(fields[index : index + 3]) for index in range(0, len(fields), 3)]
        if len(fields) != 3 * len(asked) or [answer[:2] for answer in answered] != asked:
            raise _attributes_unreadable(
                commit, "git check-attr did not answer for each path and attribute it was asked"
            )
        found.extend(
            (path, attribute)
            for path, attribute, value in answered
            if value == "set" and not (attribute == "export-subst" and path.endswith("/"))
        )
    return found


def refuse_archive_attributes(source_dir: Path, commit: str) -> None:
    """Refuse a commit any path of whose tree export-ignore or export-subst is set on, naming the first, since git
    archive would leave that path out of the export or rewrite it, and the deployed tree would not be the commit's."""
    found = archive_attributes(source_dir, commit)
    if not found:
        return
    path, attribute = found[0]
    more = f", among {len(found)} such paths and attributes" if len(found) > 1 else ""
    raise DeployError(
        f"ERROR: The tree of {commit} sets {attribute} on {path!r}{more}; git archive would "
        f"{ARCHIVE_ONLY_ATTRIBUTES[attribute]}, so the exported tree would not be the commit's.",
        "Deploy a commit whose .gitattributes, the checkout's .git/info/attributes, and Git's attributes files set "
        "neither export-ignore nor export-subst.",
        see_recovery(EARLIER_COMMIT),
    )


def _attributes_unreadable(commit: str, reason: str) -> DeployError:
    return DeployError(
        f"ERROR: Git could not read the attributes of the tree of {commit}: {reason}",
        "Put right what Git reports in the checkout, then retry; reading a commit's attributes needs Git "
        f"{ATTRIBUTE_SOURCE_GIT} or newer.",
        see_recovery(EARLIER_COMMIT),
    )


def require_extraction_filters() -> None:
    """Refuse a Python whose tarfile cannot extract with the data filter, before --at runs git or makes a directory."""
    if tools.python_version() < tools.MINIMUM_PYTHON:
        floor, current = tools.format_version(tools.MINIMUM_PYTHON), tools.format_version(tools.python_version())
        raise DeployError(
            f"ERROR: --at needs Python {floor} or newer, whose tarfile extracts with filters; "
            f"this is Python {current}.",
            'See "Installing the tools" in docs/installation.md.',
        )


def _refuse_links(archive: Path, commit: str) -> None:
    """Refuse a tree holding anything but files and folders, as the deployer refuses a link in any source tree.

    The data filter refuses a link that leads outside the tree, but links are how its known bypasses reach outside,
    and a Python that has the filter need not have their fixes, so no link is extracted at all.
    """
    with tarfile.open(name=archive, mode="r:") as handle:
        for member in handle:
            if member.isfile() or member.isdir():
                continue
            kind = "a symbolic link" if member.issym() else "a hard link" if member.islnk() else "a special file"
            raise DeployError(
                f"ERROR: The tree of {commit} holds {member.name!r}, {kind}, which the deployer does not extract.",
                "A deployable tree holds only files and folders; deploy a commit without links or special files.",
            )


def remove(root: Path) -> None:
    """Remove an export, warning rather than failing the run when it cannot: it is outside every managed root."""
    try:
        fsops.remove(root)
    except OSError as exc:
        shown = platform_support.normalize(root)
        print(f"WARNING: Could not remove the exported tree {shown}: {exc}. Delete it by hand.", file=sys.stderr)
