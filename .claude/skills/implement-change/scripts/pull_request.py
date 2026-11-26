"""The deterministic part of opening a pull request, so the agent only words the title and the body.

    draft  write a body from the repository's pull request template, with one `Closes #<n>` line per --closes in
           place of the template's bare one, to a new folder, and name the file the title goes in
    check  check the title file, the body file, and the branch against --base, a remote's branch such as
           origin/main, which it fetches first
    open   check as `check` does, check that the branch's upstream holds HEAD, then open the pull request with the
           title read from its file, so no title is ever pasted into a shell command
    type-checks
           name each type-check job validation runs that reads a Python file changed since --base, committed,
           uncommitted, or untracked, and print the validation runner's command that runs those jobs and no other,
           so the strict run continuous integration fails is run before the push; a change that reaches every job
           prints why, and the command that runs them all

The type-check jobs come from ejryan89-skill-core's type_check_roots, the reading validation builds its jobs from: the
shared run of the roots checked together and, for a root `[tool.validation] mypy-strict` in `pyproject.toml` lists, its
own strict run, or a skill's own run for a file under its `scripts/`. A suite under `tests/` is checked by the strict
run of `tests`, whatever root the module it tests sits in. A change to a file that configures or pins every check
(`pyproject.toml`, `requirements/requirements-dev.in`, `requirements/requirements-dev.txt`), or to a Python file in a
folder other roots import (`deployer/`, `tools/`, and each folder `[tool.mypy] mypy_path` lists), reaches every
type-check job, since a module that imports it may sit in any root.

The template is `pull_request_template.md`, in any case, in `.github/`, the top folder, or `docs/`, as GitHub reads
it. The body check refuses what the template leaves for the author: a comment copied from it, a closing line with no
issue, and an empty list item; every closing reference GitHub reads, through ejryan89-skill-core's closing_references,
that is not a `Closes #<n>` line of its own, in prose, a list, a quote, with a colon, or inside a comment the author
wrote alike, since the squash commit carries the body as written; a `Closes #<n>` line of its own that the rendered
body does not show, inside a comment the author wrote or a code block, which closes its issue on merge without GitHub
linking it; a template heading the body dropped; and, for a
title of type `fix` and a template holding a `## Forms considered` section, that section missing, empty, or still the
template's placeholder, read by ejryan89-skill-core's forms_considered, the reader validation's body check holds a fix
body to. A comment is one GitHub hides, read by ejryan89-skill-core's hidden_comments, so a `<!--` that a code span or
a code block shows is the body's text, checked as any other. The title check refuses a title holding a closing
reference, which the squash commit carries as its title, so every issue the pull request closes is named by a
`Closes #<n>` line of the body.

The issues `check` and `open` name closed are those of the base's repository, the GitHub repository its remote's URL
names, the URL git fetches from or else the URL as configured: a reference that names another repository, by its name
or its URL, closes none of its issues and is refused as any other closing line. When neither URL is a github.com one,
only a reference that names no repository is named closed, since another may name any repository.

Every line is one fact:

    TEMPLATE "<file>"              draft: the template read, from the top of the repository
    CLOSES <n>                     draft: each --closes; check, open: each issue of the base's repository the title
                                   or the body closes, by any closing reference GitHub reads
    BODY "<file>"                  draft: the body written, for the author to fill
    TITLE "<file>"                 draft: where to write the title, one line; nothing is there yet
    TITLE <title>                  check, open: the title read
    BASE <remote>/<branch> <sha>   check, open: the base's tip after the fetch
    TITLE_EMPTY                    the title file holds no title
    TITLE_LINES <count>            the title file holds more than one line
    TITLE_CLOSES <title>           the title holds a closing reference
    PLACEHOLDER <line> <what>      a placeholder the template left in the body, at its line
    CLOSES_LINE <line> <text>      a line holding a closing reference that is not `Closes #<n>` alone
    CLOSES_HIDDEN <line> <text>    a `Closes #<n>` line in a comment or a code block, which GitHub never links
    HEADING_MISSING <heading>      a template heading the body does not keep
    FORMS_CONSIDERED <fault>       a fix's `## Forms considered` section is missing, placeholder, or empty
    BEHIND <base> <sha>            the branch does not hold the base's tip: bring it up to date
    NO_COMMITS <base>              the branch has no commit the base does not hold
    UNCOMMITTED <count>            changed or untracked files the pull request would leave out
    UNPUSHED no upstream           open: the branch has no upstream: push it
    UNPUSHED <upstream>            open: the upstream does not hold HEAD: push it
    READY                          check: nothing to fix
    PULL_REQUEST <url>             open: the pull request opened
    BASE <remote>/<branch> <sha>   type-checks: the base's tip after the fetch, which the changed files are read from
    TYPE_CHECK <job>               type-checks: each job that reads a changed Python file
    TYPE_CHECK_ALL <path> <why>    type-checks: a changed file that reaches every type-check job, and why
    RUN <command>                  type-checks: the command that runs those jobs, or all of them, to run as printed
    NO_TYPE_CHECK                  type-checks: no changed Python file sits where a type check reads it

Exit status:
  0  the body was drafted, the check found nothing, the pull request opened, or the type checks were named
  1  one line per problem, and open opens nothing, or a last line FAILED <reason>
  2  a usage error
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import override

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))

import git_worktrees
import private_folder
import whole_number
from closing_references import (
    BARE_CLOSING_LINE,
    CLOSING_REFERENCE,
    blank_spans,
    pull_request_references,
    pull_request_text,
    reference_digits,
    stray_closing_lines,
    template_comment_spans,
    title_holds_closing_reference,
)
from console import run_main
from file_presence import NotADirectory, is_directory, is_present
from flat_text import LINE_UNSAFE, flat_text, single_line
from forms_considered import forms_fault, forms_section, is_fix_title
from git_client import GitClient, GitError, command_name, output_line
from github_client import GitHubClient, GitHubError, subprocess_runner
from github_remote import github_repository
from hidden_comments import hidden_spans
from markdown_sections import LINE_END, LIST_MARKER, MARKDOWN_SPACE
from text_lines import line_index, line_starts, text_lines
from type_check_roots import (
    JOB_PREFIX,
    MYPY_PATH_PREFIX,
    PYPROJECT,
    every_job_reasons,
    file_type_check_jobs,
    imported_folders,
    strict_type_check_areas,
)
from typed_text import read_typed

TEMPLATE_NAME = "pull_request_template.md"
# The folders GitHub reads a pull request template from, relative to the top of the repository.
TEMPLATE_FOLDERS = (".github", "", "docs")
# A list item with no text: its marker, as markdown_sections reads one, alone between spaces and tabs.
EMPTY_LIST_ITEM = re.compile(rf"[{MARKDOWN_SPACE}]*{LIST_MARKER}[{LINE_END}]*")
HEADING = re.compile(r" {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*")
# The validation runner's command, from the top of the repository, that a `-k` per job follows.
VALIDATION_COMMAND = "python -B tests/run_validation.py"


class PullRequestError(Exception):
    """A failure the script names on a FAILED line."""


@dataclass(frozen=True)
class Base:
    remote: str
    branch: str

    @override
    def __str__(self) -> str:
        return f"{self.remote}/{self.branch}"


def base_argument(value: str) -> Base:
    remote, separator, branch = value.partition("/")
    if not (remote and separator and branch):
        raise argparse.ArgumentTypeError(f"{value!r} is not a remote's branch, such as origin/main")
    return Base(remote, branch)


def blank_comments(text: str) -> str:
    """`text` with the characters of every comment GitHub hides, but its line breaks, CR and LF, made spaces, so each
    line keeps its number; a `<!--` that code shows is text, as hidden_comments reads it."""
    return blank_spans(text, hidden_spans(text))


def find_template(top: Path) -> Path | None:
    """The pull request template GitHub reads for the repository at `top`, or None; a folder whose stat fails is a
    PullRequestError, never a folder with no template."""
    for folder in TEMPLATE_FOLDERS:
        directory = top / folder
        try:
            if not is_directory(directory):
                continue
        except NotADirectory:
            # A file named like the folder, such as a docs file, holds no template, as GitHub reads it.
            continue
        except OSError as exc:
            raise PullRequestError(f'cannot read "{directory}": {flat_text(str(exc))}') from exc
        try:
            for entry in sorted(directory.iterdir()):
                if entry.name.casefold() == TEMPLATE_NAME and is_present(entry, other_is_absent=True):
                    return entry
        except OSError as exc:
            raise PullRequestError(f'cannot read "{exc.filename or directory}": {flat_text(str(exc))}') from exc
    return None


def draft_body(template: str, closes: Sequence[int]) -> str:
    """The template with one `Closes #<n>` line per issue in place of its bare closing line, or at the top."""
    lines = text_lines(template, keepends=True)
    blanked = text_lines(blank_comments(template))
    closing = [f"Closes #{number}\n" for number in closes]
    bare = [index for index, line in enumerate(blanked) if BARE_CLOSING_LINE.fullmatch(line.strip())]
    if not bare:
        return "".join(closing) + ("\n" if closing else "") + template
    first = bare[0]
    kept = [line for index, line in enumerate(lines) if index not in bare[1:]]
    return "".join(kept[:first] + closing + kept[first + 1 :])


def closed_issues(body: str, title: str | None = None, *, repository: str | None = None) -> list[int]:
    """Each issue of `repository`, `owner/name`, the title or the body closes, comments included, once, in order, by
    every closing reference GitHub reads, as closing_references reads them, or PullRequestError for a number too long to
    read. A reference that names another repository, by its name or its URL, closes none of its issues; with the
    repository unknown, None, only a reference that names no repository surely closes one of them."""
    if any(
        whole_number.parse(reference_digits(match)) is None
        for match in CLOSING_REFERENCE.finditer(pull_request_text(title, body))
    ):
        raise PullRequestError(
            f"a closing reference names an issue number of more than {whole_number.DIGIT_LIMIT} digits"
        )
    return list(
        dict.fromkeys(
            reference.number
            for reference in pull_request_references(title, body)
            if reference.repository is None or (repository is not None and reference.in_repository(repository))
        )
    )


def headings(text: str) -> list[str]:
    """Each heading outside comments, as its hashes, a space, and its text."""
    # Each line is read whole where CommonMark ends one, at CR, LF, and CRLF.
    lines = text_lines(blank_comments(text))
    return [f"{match.group(1)} {match.group(2)}" for line in lines if (match := HEADING.fullmatch(line))]


def body_problems(body: str, template: str) -> list[str]:
    """One line per placeholder the template left in the body, closing line, and template heading it dropped."""
    problems: list[tuple[int, str]] = []
    # Where each line starts, its lines ending where CommonMark ends one, at CR, LF, and CRLF, read once, so each
    # comment's line is found over them rather than counted from the body's start.
    starts = line_starts(body)
    for first, _ in template_comment_spans(body, template):
        number = line_index(starts, first) + 1
        problems.append((number, f"PLACEHOLDER {number} a template comment"))
    # closing_references numbers a line as text_lines splits the body, which blanking characters but never a CR or an
    # LF leaves where it was.
    stray = {line.number: line for line in stray_closing_lines(body, template)}
    for number, line in enumerate(text_lines(blank_comments(body)), start=1):
        text = line.strip()
        if BARE_CLOSING_LINE.fullmatch(text):
            problems.append((number, f"PLACEHOLDER {number} a Closes line with no issue"))
        elif number in stray:
            kind = "CLOSES_HIDDEN" if stray[number].hidden else "CLOSES_LINE"
            problems.append((number, f"{kind} {number} {flat_text(stray[number].text)}"))
        elif EMPTY_LIST_ITEM.fullmatch(line):
            problems.append((number, f"PLACEHOLDER {number} an empty list item"))
    lines = [problem for _, problem in sorted(problems, key=lambda problem: problem[0])]
    kept = set(headings(body))
    return lines + [f"HEADING_MISSING {flat_text(heading)}" for heading in headings(template) if heading not in kept]


def forms_lines(title: str, body: str, template: str) -> list[str]:
    """`FORMS_CONSIDERED <fault>` when the title is a fix's, the template holds a `## Forms considered` section, and
    the body's is missing, still the template's placeholder, or empty, by the reader validation holds a fix body to."""
    if not is_fix_title(title) or forms_section(template) is None:
        return []
    fault = forms_fault(body, template)
    return [] if fault is None else [f"FORMS_CONSIDERED {fault.value}"]


def read_title(text: str) -> tuple[str, list[str]]:
    """The title a title file holds, and its problems: none, more than one line, or a closing reference, which the
    squash commit's title carries as GitHub reads it in the body."""
    title = text.strip()
    if not title:
        return "", ["TITLE_EMPTY"]
    count = len(text_lines(title))
    problems = [f"TITLE_LINES {count}"] if count > 1 else []
    if title_holds_closing_reference(title):
        problems.append(f"TITLE_CLOSES {flat_text(title)}")
    return title, problems


def read_file(path: Path) -> str:
    """A title or body file's UTF-8 text, without the byte-order mark an editor on Windows may save before it, so the
    checks judge the text as typed."""
    try:
        return read_typed(path)
    except (OSError, UnicodeDecodeError) as exc:
        raise PullRequestError(f'cannot read "{path}": {flat_text(str(exc))}') from exc


def git_output(git: GitClient, repository: Path, arguments: Sequence[str]) -> str:
    try:
        return git.output(arguments, directory=repository)
    except GitError as exc:
        raise PullRequestError(f"{command_name(arguments)}: {flat_text(str(exc))}") from exc


def quiet_output(git: GitClient, repository: Path, arguments: Sequence[str]) -> str | None:
    """The stdout of a git command run with `--quiet`, or None when it exits 1 and says nothing, which such a command
    does only when what it reads is not there; any other failure is git's, with its reason."""
    try:
        return git.quiet_output(arguments, directory=repository)
    except GitError as exc:
        raise PullRequestError(f"{command_name(arguments)}: {flat_text(str(exc))}") from exc


def commit_of(git: GitClient, repository: Path, reference: str) -> str | None:
    """The commit `reference` names, or None when it names none."""
    try:
        return git.commit_of(reference, directory=repository)
    except GitError as exc:
        raise PullRequestError(f"git rev-parse: {flat_text(str(exc))}") from exc


def top_of(git: GitClient, repository: Path) -> Path:
    try:
        top = git.output(["rev-parse", "--path-format=absolute", "--show-toplevel"], directory=repository)
    except GitError as exc:
        if exc.kind == "not_repository":
            raise PullRequestError(f'"{repository}" is not in a git repository') from exc
        raise PullRequestError(f"git rev-parse: {flat_text(str(exc))}") from exc
    return Path(top.rstrip("\r\n"))


def template_of(git: GitClient, repository: Path) -> tuple[Path, Path]:
    """The top of the repository and its pull request template."""
    top = top_of(git, repository)
    template = find_template(top)
    if template is None:
        raise PullRequestError(
            f"no pull request template, a {TEMPLATE_NAME} file, in .github/, the top folder, or docs/"
        )
    return top, template


def draft(git: GitClient, repository: Path, closes: Sequence[int]) -> list[str]:
    top, template = template_of(git, repository)
    body = draft_body(read_file(template), closes)
    folder = Path(tempfile.mkdtemp(prefix="implement-change-", dir=private_folder.temporary_directory()))
    (folder / "body.md").write_text(body, encoding="utf-8", newline="\n")
    return [
        f'TEMPLATE "{template.relative_to(top).as_posix()}"',
        *(f"CLOSES {number}" for number in closes),
        f'BODY "{folder / "body.md"}"',
        f'TITLE "{folder / "title.txt"}"',
    ]


def fetched_base(git: GitClient, repository: Path, base: Base) -> str:
    """The base's tip after a fetch."""
    try:
        git.output(["fetch", "--quiet", base.remote, base.branch], directory=repository)
    except GitError as exc:
        raise PullRequestError(f"cannot fetch {base.branch} from {base.remote}: {flat_text(str(exc))}") from exc
    commit = commit_of(git, repository, f"refs/remotes/{base}")
    if commit is None:
        raise PullRequestError(f"{base} names no commit after the fetch")
    return commit


def base_repository(git: GitClient, repository: Path, base: Base) -> str | None:
    """The GitHub repository, `owner/name`, of the base's remote, which the pull request merges into: from the URL git
    fetches the base from, `insteadOf` applied, or else from the URL as configured, before a rewrite to a mirror or a
    path; None when neither is a github.com URL."""
    fetched = github_repository(git_output(git, repository, ["remote", "get-url", base.remote]))
    if fetched is not None:
        return fetched
    configured = quiet_output(git, repository, ["config", "--get", f"remote.{base.remote}.url"])
    return None if configured is None else github_repository(configured)


def branch_facts(git: GitClient, repository: Path, base: Base) -> tuple[list[str], list[str]]:
    """The base's tip after a fetch, and whether the branch holds it, adds to it, and has nothing uncommitted."""
    commit = fetched_base(git, repository, base)
    problems = []
    # merge-base --is-ancestor exits 1 saying nothing when HEAD does not hold the base; any other failure is git's.
    if quiet_output(git, repository, ["merge-base", "--is-ancestor", commit, "HEAD"]) is None:
        problems.append(f"BEHIND {base} {commit}")
    if output_line(git_output(git, repository, ["rev-list", "--count", f"{commit}..HEAD"])) == "0":
        problems.append(f"NO_COMMITS {base}")
    # Read without the index refresh a status otherwise writes back, so the check never takes the index lock; a
    # rename counts as the path it left and the one it made.
    try:
        changed = git_worktrees.changes(git, repository, "--no-renames")
    except GitError as exc:
        raise PullRequestError(f"{command_name(git_worktrees.STATUS_ARGUMENTS)}: {flat_text(str(exc))}") from exc
    if changed:
        problems.append(f"UNCOMMITTED {len(changed)}")
    return [f"BASE {base} {commit}"], problems


def check(
    git: GitClient, repository: Path, title_file: Path, body_file: Path, base: Base
) -> tuple[str, list[str], list[str]]:
    """The title, the fact lines, and the problem lines of the title file, the body file, and the branch."""
    _, template = template_of(git, repository)
    title, title_problems = read_title(read_file(title_file))
    body = read_file(body_file)
    base_facts, branch_problems = branch_facts(git, repository, base)
    closed = closed_issues(body, title or None, repository=base_repository(git, repository, base))
    facts = ([f"TITLE {flat_text(title)}"] if title else []) + [f"CLOSES {number}" for number in closed]
    template_text = read_file(template)
    body_lines = body_problems(body, template_text) + forms_lines(title, body, template_text)
    problems = title_problems + body_lines + branch_problems
    return title, facts + base_facts, problems


def changed_files(git: GitClient, top: Path, commit: str) -> list[str]:
    """Each file changed since the merge base with `commit`, committed, uncommitted, or untracked, as a path from `top`
    with `/` between its parts, read NUL-separated so a name git would quote is read as itself. A rename counts as
    the deletion and the addition, so a module moved out of a root still reaches that root's jobs."""
    names: set[str] = set()
    for arguments in (
        ["diff", "-z", "--name-only", "--no-renames", f"{commit}...HEAD"],
        ["diff", "-z", "--name-only", "--no-renames", "HEAD"],
        ["ls-files", "-z", "--others", "--exclude-standard"],
    ):
        names.update(name for name in git_output(git, top, arguments).split("\0") if name)
    return sorted(names)


def configured_paths(top: Path, reader: Callable[[Path], list[str] | None], what: str) -> list[str]:
    """The paths `reader` reads from `pyproject.toml` at `top`, as validation reads them, or a PullRequestError naming
    `what` it lacks."""
    pyproject = top / PYPROJECT
    try:
        paths = reader(pyproject)
    except (OSError, ValueError, RecursionError) as exc:
        # tomllib's TOMLDecodeError and a UnicodeDecodeError are ValueErrors, and tomllib recurses per level.
        raise PullRequestError(f'cannot read "{pyproject}": {flat_text(str(exc))}') from exc
    if paths is None:
        raise PullRequestError(f"{PYPROJECT}: {what}")
    return paths


def run_command(jobs: Sequence[str]) -> str:
    """The validation runner's command that runs exactly `jobs`: a `-k` per job, each name in single quotes, which
    Bash and PowerShell both read literally, and none of which quotes a job's own `-k` pattern as a substring of
    another's, since each name ends at its closing parenthesis. A name no single quotes or single line can hold is a
    PullRequestError."""
    quoted = [name for name in jobs if "'" in name]
    if quoted:
        raise PullRequestError(f"a type-check job's name holds a single quote: {flat_text(quoted[0])}")
    # Printed as it is, such a name would split the RUN line; flattened, the command would select another job.
    broken = [name for name in jobs if LINE_UNSAFE.search(name)]
    if broken:
        raise PullRequestError(
            f"a type-check job's name holds a line break or a control character: {flat_text(broken[0])}"
        )
    return " ".join([VALIDATION_COMMAND, *(f"-k '{name}'" for name in jobs)])


def type_checks(git: GitClient, repository: Path, base: Base) -> list[str]:
    """The base's tip, each type-check job a changed Python file reaches, and the command that runs them."""
    top = top_of(git, repository)
    commit = fetched_base(git, repository, base)
    changed = changed_files(git, top, commit)
    strict = configured_paths(top, strict_type_check_areas, "[tool.validation] mypy-strict is not a list of paths")
    imported = configured_paths(
        top, imported_folders, f"[tool.mypy] mypy_path is not a list of paths starting {MYPY_PATH_PREFIX}"
    )
    facts = [f"BASE {base} {commit}"]
    reasons = every_job_reasons(changed, imported)
    if reasons:
        # Every job's name starts with the prefix, so the runner's pattern of it alone selects them all.
        # A file name may hold a line break, which would start a line of its own, so each is printed flat.
        widened = [f"TYPE_CHECK_ALL {flat_text(path)} {flat_text(why)}" for path, why in reasons]
        return [*facts, *widened, f"RUN {VALIDATION_COMMAND} -k '{JOB_PREFIX}'"]
    jobs = file_type_check_jobs(top, changed, strict)
    if not jobs:
        return [*facts, "NO_TYPE_CHECK"]
    return [*facts, *(f"TYPE_CHECK {job}" for job in jobs), f"RUN {run_command(jobs)}"]


def upstream_problems(git: GitClient, repository: Path) -> list[str]:
    """Whether the branch's upstream holds HEAD. A detached HEAD has no upstream, and an upstream not fetched holds
    nothing."""
    branch = quiet_output(git, repository, ["symbolic-ref", "--quiet", "HEAD"])
    if branch is None:
        return ["UNPUSHED no upstream"]
    reference, _, name = (
        git_output(git, repository, ["for-each-ref", "--format=%(upstream)%00%(upstream:short)", output_line(branch)])
        .removesuffix("\n")
        .partition("\0")
    )
    if not reference:
        return ["UNPUSHED no upstream"]
    head = output_line(git_output(git, repository, ["rev-parse", "HEAD"]))
    return [] if commit_of(git, repository, reference) == head else [f"UNPUSHED {name}"]


def open_pull_request(github: GitHubClient, title: str, body_file: Path, base: Base) -> str:
    arguments = ["pr", "create", "--title", title, "--body-file", str(body_file), "--base", base.branch]
    try:
        result = github.run(arguments, retry=False)
    except GitHubError as exc:
        raise PullRequestError(f"gh pr create: {flat_text(str(exc))}") from exc
    lines = result.stdout.split()
    if not lines:
        raise PullRequestError("gh pr create printed no pull request")
    return f"PULL_REQUEST {lines[-1]}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Draft, check, and open a pull request from its title and body files.")
    parser.add_argument("--repository", type=Path, default=Path(), help="the checkout (default: here)")
    commands = parser.add_subparsers(dest="command", required=True)
    draft_parser = commands.add_parser("draft", help="write a body from the pull request template")
    draft_parser.add_argument("--closes", type=whole_number.argument(1), action="append", default=[], metavar="ISSUE")
    files = argparse.ArgumentParser(add_help=False)
    files.add_argument("--title-file", type=Path, required=True)
    files.add_argument("--body-file", type=Path, required=True)
    files.add_argument("--base", type=base_argument, required=True, help="a remote's branch, such as origin/main")
    commands.add_parser("check", parents=[files], help="check the title, the body, and the branch")
    commands.add_parser("open", parents=[files], help="check, then open the pull request")
    type_check_parser = commands.add_parser(
        "type-checks", help="print the command that runs the type checks of the Python files changed since --base"
    )
    type_check_parser.add_argument(
        "--base", type=base_argument, required=True, help="a remote's branch, such as origin/main"
    )
    return parser


def run(options: argparse.Namespace, git: GitClient, github: GitHubClient | None) -> int:
    repository: Path = options.repository
    if options.command == "draft":
        print("\n".join(single_line(line) for line in draft(git, repository, options.closes)))
        return 0
    if options.command == "type-checks":
        print("\n".join(single_line(line) for line in type_checks(git, repository, options.base)))
        return 0
    title, facts, problems = check(git, repository, options.title_file, options.body_file, options.base)
    if options.command == "open":
        problems += upstream_problems(git, repository)
    print("\n".join(single_line(line) for line in facts + problems))
    if problems:
        return 1
    if options.command == "check":
        print("READY")
        return 0
    client = github or GitHubClient(runner=partial(subprocess_runner, cwd=repository))
    print(single_line(open_pull_request(client, title, options.body_file, options.base)))
    return 0


def main(
    arguments: Sequence[str] | None = None, *, git: GitClient | None = None, github: GitHubClient | None = None
) -> int:
    options = build_parser().parse_args(arguments)
    try:
        return run(options, git or GitClient(), github)
    except PullRequestError as exc:
        print(f"FAILED {flat_text(str(exc))}")
        return 1


if __name__ == "__main__":
    run_main(main)
