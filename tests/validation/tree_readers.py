"""Every reader of a commit's tree is named here, with the bytes it promises.

The code-review snapshot and deploy.py --at write a commit's tree to disk with different bytes on purpose: the snapshot
lists the tree with git ls-tree and reads each blob with git cat-file, so it holds the committed bytes, while --at runs
git archive, which writes each file as a checkout would. The row on readers of a commit's tree in docs/design.md's
Decisions records why both stay. This policy names every place in the repository that runs git archive, ls-tree,
cat-file, show, or grep, or names a GitHub endpoint that reads a commit's tree, by its module and its scope, with the
bytes it promises, and fails on a reader the catalogue does not name, on one whose reads no longer give the bytes its
entry promises, and on an entry that names no reader, so a change to one reader is made with the others in view.
archive gives a checkout's bytes, and so does cat-file, show, or grep given --filters or --textconv; ls-tree gives
entries, and so does cat-file given -e, -s, -t, or --batch-check; any other cat-file, show, or grep gives the committed
bytes. GitHub's tarball and zipball endpoints and github.com's archive downloads give a checkout's bytes, since GitHub
builds them as git archive does; its contents, readme, and blobs endpoints and its raw files give the committed bytes;
its trees endpoint gives entries. An archive gives the committed bytes only in a scope that calls the check
CHECKED_READERS names for it, a function its module defines, such as verify_github_tarball, which refuses a tarball
whose members are not exactly the blobs of the commit's tree; the policy fails an entry there whose scope no longer
calls its check, or reads no archive.

What counts as a reader's git words, in every Python file outside the suites, the root conftest.py, and tests/fixtures/:
the items of each list or tuple literal, and the positional arguments of each call whose name says git, as
ejryan89-skill-core's runtime_support.names_git reads it (`_git(source, "archive", ...)`, `GitClient().stream([...])`),
in order from the first string literal, with each other value standing for an unread word, and each call of a method
named file_at, GitClient's reader of a file's committed bytes; a first word that names the git program itself, as in
`["git", "archive", ...]`, is passed over, and git_client.subcommand reads past git's own options to the subcommand. The
command string of each call_tables PROCESS_STARTS runner that runs it through a shell, and each Bash or PowerShell
script outside the suites, are read by ejryan89-skill-core's git_subcommands, through skill_layout's
script_git_subcommands for a script. A GitHub endpoint is read from the text each string expression in a Python file
renders, as python_checks reads one: a string literal, an f-string, or a `%`, `+`, or `.format` of one, each part it
formats read as one word, so `f"...{repository}/tarball/{commit}"` after the API's repository path is read in the scope
that writes it, whatever call then hands it to GitHubClient or gh api.

Stated limits, each one a fixture in tests/validation/test_tree_readers.py shows:
- A subcommand that only a value names, as `verb` in `git.output([verb, commit])`, is not read.
- A cat-file mode that only a value names is read as one that gives the committed bytes.
- A call named for git whose words start with a string literal before the subcommand, as
  `git_output("/repo", "cat-file", ...)`, takes that string for the subcommand.
- The words of a call whose name says no git, as `runner(arguments)`, are read only where a list or tuple literal holds
  them, and a file_at reached through a name bound to the method, as `read = client.file_at`, is not read.
- A command given as one string to a runner without a shell, or split from one string by str.split or shlex.split, is
  not read, and a command a runner runs through a shell is read only where it is one string literal.
- A shell's -c script inside a list, and a command in a Markdown fence, are not read.
- A Bash or PowerShell script's git, and a command a runner runs through a shell, are read by their subcommand alone, so
  cat-file, show, or grep there given --filters, --textconv, -e, -s, -t, or --batch-check is read as giving the
  committed bytes.
- A git a shell function or array runs with the words it is given, as `g() { git -C x "$@"; }`,
  `cmd=(git archive HEAD); "${cmd[@]}"`, `"$GIT" archive`, or `& git @args`, is not read at its callers.
- A grep over files rather than a commit, given --no-index or no commit, as search_source's snapshot search runs, is
  read as one over a commit.
- A command that writes a commit's tree into a working tree, worktree add, checkout, switch, or restore, is not read: it
  writes what a checkout writes and hands its caller no bytes of its own, and a later read of that working tree reads
  files, not git.
- A list, a tuple, or a call's arguments whose words are exactly a NOT_READERS entry is passed over wherever it sits,
  except in the catalogue's own NOT_READERS assignment, which is never read, for git words or endpoints,
  so a literal `("archive", "checkout")` meant as `git archive checkout` would be excused anywhere.
- A NOT_READERS entry added in another statement than the catalogue's own assignment, as `NOT_READERS[words] = reason`
  or `NOT_READERS.update(...)`, is read, so it excuses itself.
- An endpoint is read in the scope that writes its text, so one a module-level name holds is read at that name, as
  branch_protection's ISSUE_CONFIG_ENDPOINT is, not where GitHub is called with it, and an archive's check is held in
  the scope that writes the endpoint.
- An endpoint whose text no string expression spells, as one held in bytes, joined by str.join, built by a helper from
  its parts, or whose kind only a value names, as `f"...{repository}/{kind}/{commit}"`, is not read.
- A contents or readme endpoint is read as giving a file's committed bytes, even for a folder, whose entries it lists.
- A gh command other than gh api that reads a commit's tree, as `gh release download --archive=zip` or `gh repo clone`,
  is not read, and neither is a GitHub endpoint in a Bash or PowerShell script or a Markdown fence.
- An endpoint after a repository path a name holds, as `f"{base}/contents/{path}"` or `base + f"/contents/{path}"`, is
  not read.
- A file read through GraphQL's `object(expression:)`, or through github.com's `blob/<commit>/<path>?raw=true`, is not
  read.
- A check is held by the name a call in its scope gives it, so a call of the function its module defines by that name
  counts wherever in the scope it is made, in a branch that never runs or before the download, and whatever it is
  given, and it holds every archive its scope reads, a second archive beside the one it checks included.
"""

from __future__ import annotations

import ast
import functools
import re
import unittest
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import git_client
import runtime_support
from bash_syntax import BashSyntaxError, refusing
from call_tables import PROCESS_STARTS
from exit_tables import NOT_SCANNED
from git_subcommands import UNREAD_PART, bash_git_subcommands, powershell_program
from path_containment import within
from python_checks import rendered_text
from skill_layout import UnreadScript, script_git_subcommands
from skill_scripts import powershell_script_commands
from validation_support import (
    MODULE_SCOPE,
    REPOSITORY_ROOT,
    CallableResolver,
    call_argument,
    is_test_script,
    python_trees,
    repository_files,
    scoped_nodes,
    statement_scope,
)

POLICY = "tests/validation/tree_readers.py"
DECISION = "the row on readers of a commit's tree in docs/design.md's Decisions"

# The bytes a reader of a commit's tree promises the code that calls it.
COMMITTED = "committed"
CHECKOUT = "checkout"
ENTRIES = "entries"
PROMISES = {
    COMMITTED: "each blob as the object store holds it, with no attribute, configuration, or filter applied",
    CHECKOUT: "each file as a checkout of the commit writes it, with line endings, ident, and filter drivers applied",
    ENTRIES: "the tree's entries, their paths, modes, object names, kinds, or sizes, and never a file's bytes",
}
# The git subcommands that read a commit's tree.
READER_SUBCOMMANDS = frozenset({"archive", "ls-tree", "cat-file", "show", "grep"})
# The cat-file options that answer with an object's kind, size, or presence rather than its bytes, and the options of
# cat-file, show, and grep that apply the attributes and filters a checkout applies; an option counts alone or with a
# value after =, as --batch-check=%(objectsize) does.
CAT_FILE_ENTRIES = frozenset({"-e", "-s", "-t", "--batch-check"})
CONVERTING_OPTIONS = frozenset({"--filters", "--textconv"})
CAT_FILE = "cat-file"
GIT = "git"
# GitClient's method that reads a file's committed bytes at a commit, whose every call is a reader of them.
FILE_AT = "file_at"
# The GitHub endpoints that read a commit's tree, found in the text a string expression renders, as python_checks reads
# one, each part it formats read as one word: the REST API's archives, contents, readme, blobs, and trees, whether
# `gh api` is given the path or a URL names it, after a repository given as one word or as its owner and name; and
# github.com's raw files and archive downloads. Each kind is named in GITHUB_READS by the group that matches it.
GITHUB_ENDPOINTS = re.compile(
    r"(?:^|/)repos/(?:[^/?#]+/){1,2}?(?P<api>tarball|zipball|contents|readme|git/blobs|git/trees)(?=[/?#]|$)"
    r"|(?P<raw>raw\.githubusercontent\.com/|github\.com/[^/?#]+/[^/?#]+/raw/)"
    r"|(?P<download>codeload\.github\.com/|github\.com/[^/?#]+/[^/?#]+/archive/)"
)
# What each kind of endpoint reads and the bytes it gives: GitHub builds its archives with git archive, so they give a
# checkout's bytes, its contents, readme, blobs, and raw files give a file's committed bytes, and its trees the entries.
GITHUB_READS = {
    "tarball": ("GitHub's tarball endpoint", CHECKOUT),
    "zipball": ("GitHub's zipball endpoint", CHECKOUT),
    "download": ("GitHub's archive download", CHECKOUT),
    "contents": ("GitHub's contents endpoint", COMMITTED),
    "readme": ("GitHub's readme endpoint", COMMITTED),
    "git/blobs": ("GitHub's blobs endpoint", COMMITTED),
    "raw": ("GitHub's raw download", COMMITTED),
    "git/trees": ("GitHub's trees endpoint", ENTRIES),
}
# The reads that give an archive of the tree, which a check its scope calls can hold to the committed bytes.
ARCHIVE_READS = frozenset({"git archive", *(how for how, given in GITHUB_READS.values() if given == CHECKOUT)})
# The scripts besides Python that the policy reads through git_subcommands.
SHELL_SCRIPTS = frozenset({".sh", ".bash", ".ps1"})

# Each reader of a commit's tree, by its module's path from the repository root and its scope, as scoped_nodes names
# it, with the bytes it promises: one of PROMISES.
TREE_READERS: dict[str, dict[str, str]] = {
    ".claude/skills/audit-repository/scripts/audit_repository.py": {
        "tracked_files": ENTRIES,
        "search_construct": COMMITTED,
    },
    ".claude/skills/change-skill/scripts/change_skill.py": {"Tree.base": COMMITTED},
    # deploy.py --at: the tree a deployment from a checkout of the commit reads, and the paths whose attributes it
    # asks git archive about.
    "deployer/source_ref.py": {"export": CHECKOUT, "_tree_paths": ENTRIES},
    # A pull request's modes, read from GitHub's trees endpoint at a commit: the entries.
    "skills/ejryan89-code-review-core/scripts/pr_change.py": {"ChangeDetector._read_modes": ENTRIES},
    # A file at a commit read through GitHub's contents and blobs endpoints: the bytes as committed.
    "skills/ejryan89-code-review-core/scripts/review_github.py": {"GitHubClient.get_file_blob": COMMITTED},
    "skills/ejryan89-code-review-core/scripts/review_reviewers.py": {"repository_files": ENTRIES},
    # The code-review snapshot and the reviewer files read at a trusted commit: the bytes as committed.
    "skills/ejryan89-code-review-core/scripts/review_runtime.py": {
        "_tree_entries": ENTRIES,
        "_runner_blob_reader.read": COMMITTED,
        "git_blob_reader": COMMITTED,
        "git_blob_prefix": COMMITTED,
        "_commit_members": ENTRIES,
        "read_blob": COMMITTED,
        "_blob_binary_test": COMMITTED,
        "search_source": COMMITTED,
        # GitHub's tarball, held to the committed bytes by the check CHECKED_READERS names.
        "github_tarball_fetcher": COMMITTED,
    },
    "skills/ejryan89-skill-core/scripts/git_client.py": {"GitClient.file_at": COMMITTED},
    "skills/review-document/scripts/review_document.py": {"committed_blob": ENTRIES, "committed_base": COMMITTED},
    "skills/review-insights/scripts/review_guidance.py": {"read_committed": COMMITTED},
    # The update's pin check from main's tree, for a clone on another tree: the deploy.py a checkout of main would run.
    "skills/update-coding-agent-skills/scripts/update.sh": {"<module>": CHECKOUT},
    "tests/validation/silent_reverts.py": {"current_blobs": ENTRIES},
    "tests/validation/upgrade_notes.py": {"revision_snapshot": COMMITTED},
    # The issue forms' configuration on the default branch, read through GitHub's contents endpoint.
    "tools/branch_protection.py": {"ISSUE_CONFIG_ENDPOINT": COMMITTED},
    # The probe's own file at its checkout's commit, compared with the running file once line endings are LF, so a
    # commit is recorded only when it holds the probe that ran: the bytes as committed.
    "tools/context_probe.py": {"checkout_commit": COMMITTED},
    "tools/mutation_gate.py": {"tree_files": ENTRIES},
    "tools/release_notes.py": {"_notes_at": COMMITTED},
}
# Each reader whose promise rests on a check, by its module's path and its scope, with the check: a function its module
# defines that its scope calls by name. An archive's read gives the committed bytes in a scope that calls its check,
# and a checkout's anywhere else.
CHECKED_READERS: dict[str, dict[str, str]] = {
    # GitHub's tarball is built as git archive builds one, so export-ignore, export-subst, and eol could reach it; the
    # check refuses a tarball whose members are not exactly the blobs the commit's tree lists.
    "skills/ejryan89-code-review-core/scripts/review_runtime.py": {"github_tarball_fetcher": "verify_github_tarball"},
}
# The words that spell a reader's subcommand but never reach git, with the reason: a list, a tuple, or a call's
# positional arguments whose items are all string literals, exactly these words in this order, is passed over wherever
# it sits. A GitHub endpoint among those words is still read, since an entry names git's words only.
NOT_READERS_NAME = "NOT_READERS"
NOT_READERS: dict[tuple[str, ...], str] = {
    ("archive", "checkout"): "the folders a synthesis scenario's fixture copies, which a skill evaluation names",
}


def scanned_files(root: Path, files: Iterable[Path] | None = None) -> list[Path]:
    """The Python, Bash, and PowerShell files the policy reads, among the repository's files or the files given: every
    one outside the suites, the root conftest.py, and tests/fixtures/, in order of their path from the root."""
    listed = repository_files(root) if files is None else files
    return [
        path
        for path in sorted(listed, key=lambda each: each.relative_to(root).as_posix())
        if (path.suffix == ".py" or path.suffix in SHELL_SCRIPTS)
        and not is_test_script(path)
        and not within(path, root / NOT_SCANNED)
        and path.relative_to(root).as_posix() != "conftest.py"
    ]


def _words(values: Sequence[ast.expr]) -> list[str]:
    """The words a sequence of values gives git, from its first string literal on, each other value read as UNREAD_PART,
    with a first word that names the git program passed over."""
    words: list[str] = []
    for value in values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            words.append(value.value)
        elif words:
            words.append(UNREAD_PART)
    if words and powershell_program(words[0]) == GIT:
        words = words[1:]
    return words


def _reads(words: Sequence[str]) -> tuple[str, str] | None:
    """The tree-reading subcommand git words run, as `git <subcommand>`, and the bytes it gives, or None for words that
    run none."""
    index = git_client.subcommand_index(words)
    if index is None or words[index] not in READER_SUBCOMMANDS:
        return None
    subcommand = words[index]
    reader = f"{GIT} {subcommand}"
    options = {word.partition("=")[0] for word in words[index + 1 :]}
    if subcommand == "archive" or CONVERTING_OPTIONS & options:
        return reader, CHECKOUT
    if subcommand == "ls-tree" or (subcommand == CAT_FILE and CAT_FILE_ENTRIES & options):
        return reader, ENTRIES
    return reader, COMMITTED


def _github_reads(node: ast.AST) -> list[tuple[str, str]]:
    """Each GitHub endpoint that reads a commit's tree in the text a string expression renders, with the bytes it
    gives; none for any other node."""
    text = rendered_text(node) if isinstance(node, ast.expr) else None
    if text is None:
        return []
    return [GITHUB_READS[match.group("api") or match.lastgroup or ""] for match in GITHUB_ENDPOINTS.finditer(text)]


def _shell_command(call: ast.Call, resolver: CallableResolver) -> str | None:
    """The command string a call_tables runner runs through a shell, or None for any other call."""
    start = PROCESS_STARTS.get(resolver.name(call.func))
    if start is None:
        return None
    shell = next((keyword.value for keyword in call.keywords if keyword.arg == "shell"), None)
    shell_given = shell is not None and not (isinstance(shell, ast.Constant) and not shell.value)
    command = call_argument(call, *start.command)
    through_shell = start.through_shell or (start.popen_keywords and shell_given)
    if through_shell and isinstance(command, ast.Constant) and isinstance(command.value, str):
        return command.value
    return None


def _excused(
    values: Sequence[ast.expr], not_readers: Mapping[tuple[str, ...], str], used: set[tuple[str, ...]]
) -> bool:
    """Whether a sequence of values is exactly the words of a NOT_READERS entry, every one a string literal, the same
    in number and order; an entry it matches is added to `used`."""
    words = tuple(value.value for value in values if isinstance(value, ast.Constant) and isinstance(value.value, str))
    if len(words) != len(values) or words not in not_readers:
        return False
    used.add(words)
    return True


class UnreadCommand(ValueError):
    """A shell command a Python module runs whose git subcommands cannot be read, since it is Bash the grammar
    refuses."""

    def __init__(self, line: int, reason: str) -> None:
        super().__init__(reason)
        self.line, self.reason = line, reason


def _unread_command(line: int, error: BashSyntaxError) -> UnreadCommand:
    return UnreadCommand(line, f"not Bash the grammar reads ({error})")


def python_tree_reads(
    tree: ast.Module,
    not_readers: Mapping[tuple[str, ...], str] = NOT_READERS,
    used: set[tuple[str, ...]] | None = None,
) -> list[tuple[int, str, str, str]]:
    """Each read of a commit's tree a Python module's git words and GitHub endpoints make, as (line, scope, reader,
    bytes), with the reader as `git <subcommand>` or the GitHub endpoint it names, in the order a walk of the module
    meets them, past each sequence of git's words that is exactly the words of a `not_readers` entry, which is added to
    `used`. Raises UnreadCommand, naming its line, for a command a call runs through a shell that is Bash the grammar
    refuses, as script_git_subcommands raises UnreadScript for such a script."""
    resolver = CallableResolver(tree)
    excusing: set[tuple[str, ...]] = set() if used is None else used
    found: list[tuple[int, str, str, str]] = []
    for node, scope in scoped_nodes(tree.body):
        readings: list[tuple[str, str] | None] = []
        if isinstance(node, ast.List | ast.Tuple):
            if not _excused(node.elts, not_readers, excusing):
                readings.append(_reads(_words(node.elts)))
        elif isinstance(node, ast.Call):
            if runtime_support.names_git(node.func) and not _excused(node.args, not_readers, excusing):
                readings.append(_reads(_words(node.args)))
            if isinstance(node.func, ast.Attribute) and node.func.attr == FILE_AT:
                readings.append((f"{GIT} {CAT_FILE}", COMMITTED))
            command = _shell_command(node, resolver)
            if command is not None:
                with refusing(functools.partial(_unread_command, node.lineno)):
                    subcommands = bash_git_subcommands(command, powershell_script_commands)
                readings += [_reads([subcommand]) for subcommand in subcommands]
        readings += _github_reads(node)
        found += [(getattr(node, "lineno", 0), scope, *reading) for reading in readings if reading is not None]
    return found


def _script_reads(path: Path) -> list[tuple[int, str, str, str]]:
    """Each read of a commit's tree a Bash or PowerShell script makes, with the script itself as its scope."""
    readings = [_reads([subcommand]) for subcommand in sorted(script_git_subcommands(path))]
    return [(1, MODULE_SCOPE, *reading) for reading in readings if reading is not None]


def _promise(reads: Iterable[tuple[str, str]]) -> str | None:
    """The bytes a scope's reads promise: a checkout's when any gives them, else the committed bytes when any gives
    those, else the entries; None when it gives both a checkout's and the committed bytes."""
    given = {given for _, given in reads}
    if {CHECKOUT, COMMITTED} <= given:
        return None
    return CHECKOUT if CHECKOUT in given else COMMITTED if COMMITTED in given else ENTRIES


def checked_scopes(tree: ast.Module, checks: Mapping[str, str]) -> set[str]:
    """The scopes `checks` names that call their check by its name, where the module defines that check as a function
    at its top level."""
    defined = {
        statement.name for statement in tree.body if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    return {
        scope
        for node, scope in scoped_nodes(tree.body)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == checks.get(scope)
        and node.func.id in defined
    }


def _found_reads(
    root: Path,
    paths: list[Path],
    not_readers: Mapping[tuple[str, ...], str],
    checks: Mapping[str, Mapping[str, str]],
    problems: list[str],
) -> tuple[dict[tuple[str, str], list[tuple[int, str, str]]], set[tuple[str, ...]], set[tuple[str, str]]]:
    """Each scope's reads of a commit's tree as (line, reader, bytes), the NOT_READERS words a sequence spells, and the
    scopes that call the check `checks` names for them; a script, or a module's shell command, whose git cannot be read
    is added to `problems`, and the module's reads go unread."""
    found: dict[tuple[str, str], list[tuple[int, str, str]]] = defaultdict(list)
    for path in (path for path in paths if path.suffix in SHELL_SCRIPTS):
        name = path.relative_to(root).as_posix()
        try:
            script_reads = _script_reads(path)
        except UnreadScript as error:
            problems.append(f"{name}: cannot read the git it runs: {error.reason}")
            continue
        for line, scope, reader, given in script_reads:
            found[(name, scope)].append((line, reader, given))
    used: set[tuple[str, ...]] = set()
    checked: set[tuple[str, str]] = set()
    for _, name, tree in python_trees([path for path in paths if path.suffix == ".py"], root, problems):
        if name == POLICY:
            # The catalogue's own keys spell the words they excuse, so they would excuse themselves.
            body = [statement for statement in tree.body if statement_scope(statement) != NOT_READERS_NAME]
            tree = ast.Module(body=body, type_ignores=[])
        try:
            module_reads = python_tree_reads(tree, not_readers, used)
        except UnreadCommand as error:
            problems.append(f"{name}:{error.line}: cannot read the git it runs: {error.reason}")
            continue
        for line, scope, reader, given in module_reads:
            found[(name, scope)].append((line, reader, given))
        checked |= {(name, scope) for scope in checked_scopes(tree, checks.get(name, {}))}
    return found, used, checked


def _reader_problem(name: str, scope: str, reads: list[tuple[int, str, str]], listed: str | None) -> str | None:
    """What is wrong with a scope's reads of a commit's tree against the bytes its entry promises, or None."""
    line, reader, _ = reads[0]
    promised = _promise((reader, given) for _, reader, given in reads)
    if promised is None:
        return (
            f"{name}:{line} {scope} reads both a checkout's bytes and the committed bytes of a commit's tree; "
            f"split it into one reader for each, each named in {POLICY}'s TREE_READERS; see {DECISION}"
        )
    if listed is None:
        excuse = ", or in NOT_READERS if the words never reach git" if reader.startswith(f"{GIT} ") else ""
        return (
            f"{name}:{line} {scope} reads a commit's tree with {reader}, which TREE_READERS in {POLICY} does not "
            f"name; name it there with the bytes it promises ({', '.join(sorted(PROMISES))}){excuse}; see {DECISION}"
        )
    if listed != promised:
        return (
            f"{name}:{line} {scope} reads {promised} bytes of a commit's tree, but TREE_READERS in {POLICY} "
            f"promises {listed}; keep the bytes its callers rely on, or change its entry with every caller in "
            f"view; see {DECISION}"
        )
    return None


def _check_problems(
    checks: Mapping[str, Mapping[str, str]],
    found: Mapping[tuple[str, str], list[tuple[int, str, str]]],
    checked: set[tuple[str, str]],
) -> list[str]:
    """Each entry of `checks` whose scope reads no archive of a commit's tree, or does not call the check it names, a
    function its module defines."""
    problems: list[str] = []
    for name, scopes in sorted(checks.items()):
        for scope, check in sorted(scopes.items()):
            if not any(reader in ARCHIVE_READS for _, reader, _ in found.get((name, scope), [])):
                problems.append(
                    f"{POLICY}: CHECKED_READERS names {name} {scope}, which reads no archive of a commit's tree; "
                    "remove the entry"
                )
            elif (name, scope) not in checked:
                problems.append(
                    f"{name} {scope} does not call {check!r} by its name, a function its module defines, which "
                    f"CHECKED_READERS in {POLICY} names as the check that holds its archive to the committed bytes; "
                    f"without it the archive gives a checkout's bytes; see {DECISION}"
                )
    return problems


def tree_reader_problems(
    root: Path,
    files: Iterable[Path] | None = None,
    readers: Mapping[str, Mapping[str, str]] = TREE_READERS,
    not_readers: Mapping[tuple[str, ...], str] = NOT_READERS,
    checks: Mapping[str, Mapping[str, str]] = CHECKED_READERS,
) -> list[str]:
    """Report each reader of a commit's tree that `readers` does not name, each one whose reads give other bytes than
    its entry promises, an archive's read giving the committed bytes only in a scope that calls the check `checks`
    names for it, each scope that gives both a checkout's and the committed bytes, each entry of `readers` that names no
    reader, each entry of `checks` whose scope reads no archive or does not call its check, each entry of `not_readers`
    that no sequence spells or that gives no reason, and each promise PROMISES does not name."""
    problems: list[str] = []
    found, used, checked = _found_reads(root, scanned_files(root, files), not_readers, checks, problems)
    for (name, scope), reads in sorted(found.items(), key=lambda item: (item[0][0], item[1][0][0], item[0][1])):
        if (name, scope) in checked:
            reads = [(line, reader, COMMITTED if reader in ARCHIVE_READS else given) for line, reader, given in reads]
        problem = _reader_problem(name, scope, reads, readers.get(name, {}).get(scope))
        if problem is not None:
            problems.append(problem)
    problems += [
        f"{POLICY}: TREE_READERS names {name} {scope}, which reads no commit's tree; remove the entry"
        for name, scopes in sorted(readers.items())
        for scope in sorted(scopes)
        if (name, scope) not in found
    ]
    problems += _check_problems(checks, found, checked)
    problems += [
        f"{POLICY}: NOT_READERS names the words {words!r}, which no sequence spells; remove the entry"
        for words in sorted(not_readers)
        if words not in used
    ]
    problems += [
        f"{POLICY}: NOT_READERS gives the words {words!r} no reason; state why they never reach git"
        for words, reason in sorted(not_readers.items())
        if not reason.strip()
    ]
    problems += [
        f"{POLICY}: TREE_READERS promises {promise!r} for {name} {scope}, which PROMISES does not name"
        for name, scopes in sorted(readers.items())
        for scope, promise in sorted(scopes.items())
        if promise not in PROMISES
    ]
    return problems


class TreeReaderPolicies(unittest.TestCase):
    def test_every_reader_of_a_commit_s_tree_is_named_with_the_bytes_it_promises(self) -> None:
        # The code-review snapshot promises the committed bytes and deploy.py --at a checkout's, on purpose; a reader
        # added or changed is checked against the others here.
        self.assertEqual([], tree_reader_problems(REPOSITORY_ROOT))
