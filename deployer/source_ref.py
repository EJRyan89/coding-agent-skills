"""`deploy.py --at <ref>`: the commit a ref names in the source checkout, and that commit's tree to deploy from.

The deployer that runs is the checkout's own; only the templates come from the ref, and the pipeline validates them as
it validates any source's. A ref's own deployer is code nobody reviewed for this run, so it never runs.
"""

from __future__ import annotations

import re
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from flat_text import flat_text

from . import fsops, platform_support
from .arguments import usage_error
from .errors import DeployError, see_recovery
from .source_commit import COMMIT_PATTERN

# What --at accepts, and what the manifest records as source_ref: a branch, a tag, a commit name, or a revision
# expression over them such as main~2, never a name starting with "-", which git would read as an option, and nothing
# a shell, a terminal, or a JSON reader gives a meaning to.
REF_PATTERN = re.compile(r"^(?!-)[A-Za-z0-9._/@{}~^+-]{1,255}$")
REF_RULES = "A ref may not start with '-' and may hold only letters, digits, and . _ / @ { } ~ ^ + -."
EARLIER_COMMIT = "Deploying an earlier commit"

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


def _git(source_dir: Path, *arguments: str) -> platform_support.ToolResult:
    git = platform_support.find_executable("git")
    if git is None:
        raise DeployError(
            "ERROR: --at needs Git, which was not found.",
            'Install it as "Installing the tools" in docs/installation.md says, then retry.',
        )
    return platform_support.run_tool([git, "-C", str(source_dir), *arguments])


def resolve(source_dir: Path, ref: str) -> Pin:
    """The commit ref names in the checkout, which must already hold it: the deployer never fetches."""
    check_ref(ref)
    shown = platform_support.normalize(source_dir)
    if not (source_dir / ".git").exists():
        raise DeployError(f"ERROR: --at needs a Git checkout, and {shown} is not one.")
    # --end-of-options keeps a ref from being read as an option even if the pattern above ever let one through.
    result = _git(source_dir, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
    commit = result.output.strip()
    if result.returncode != 0 or not COMMIT_PATTERN.fullmatch(commit):
        raise DeployError(
            f"ERROR: --at {ref} names no commit in {shown}.",
            "The deployer does not fetch: run 'git fetch --tags origin' in the checkout, or name a commit it holds, "
            "then retry.",
            see_recovery(EARLIER_COMMIT),
        )
    return Pin(ref, commit)


def export(source_dir: Path, commit: str) -> Path:
    """A fresh directory under the system temporary directory whose `tree` folder holds the commit's files.

    git archive writes the files as a checkout of the commit would, line endings included, and the extraction refuses
    any member that would land outside the folder.
    """
    if not COMMIT_PATTERN.fullmatch(commit):
        raise ValueError(f"not a full commit name: {commit!r}")
    if not hasattr(tarfile, "data_filter"):
        raise DeployError(
            "ERROR: --at needs a Python whose tarfile has extraction filters (3.11.4 or newer).",
            "Update Python, then retry.",
        )
    root = Path(tempfile.mkdtemp(prefix="deploy-at-"))
    try:
        archive = root / "tree.tar"
        result = _git(source_dir, "archive", "--format=tar", f"--output={archive}", commit)
        if result.returncode != 0:
            raise DeployError(f"ERROR: git archive could not export {commit}: {flat_text(result.output)}")
        fsops.make_directory(root / "tree")
        fsops.extract_archive(archive, root / "tree")
    except BaseException:
        remove(root)
        raise
    return root


def remove(root: Path) -> None:
    """Remove an export, warning rather than failing the run when it cannot: it is outside every managed root."""
    try:
        fsops.remove(root)
    except OSError as exc:
        shown = platform_support.normalize(root)
        print(f"WARNING: Could not remove the exported tree {shown}: {exc}. Delete it by hand.", file=sys.stderr)
