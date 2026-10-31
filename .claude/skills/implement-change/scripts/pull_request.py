"""The deterministic part of opening a pull request, so the agent only words the title and the body.

    draft  write a body from the repository's pull request template, with one `Closes #<n>` line per --closes in
           place of the template's bare one, to a new folder, and name the file the title goes in
    check  check the title file, the body file, and the branch against --base, a remote's branch such as
           origin/main, which it fetches first
    open   check as `check` does, check that the branch's upstream holds HEAD, then open the pull request with the
           title read from its file, so no title is ever pasted into a shell command

The template is `pull_request_template.md`, in any case, in `.github/`, the top folder, or `docs/`, as GitHub reads
it. The body check refuses what the template leaves for the author: a comment copied from it, a closing line with no
issue, and an empty list item; every closing reference GitHub reads, through ejryan89-skill-core's closing_references,
that is not a `Closes #<n>` line of its own, in prose, a list, a quote, with a colon, or inside a comment the author
wrote alike, since the squash commit carries the body as written; and a template heading the body dropped. The title
check refuses a title holding a closing reference, which the squash commit carries as its title, so every issue the
pull request closes is named by a `Closes #<n>` line of the body.

Every line is one fact:

    TEMPLATE "<file>"              draft: the template read, from the top of the repository
    CLOSES <n>                     each issue the title or the body closes, by any closing reference GitHub reads
    BODY "<file>"                  draft: the body written, for the author to fill
    TITLE "<file>"                 draft: where to write the title, one line; nothing is there yet
    TITLE <title>                  check, open: the title read
    BASE <remote>/<branch> <sha>   check, open: the base's tip after the fetch
    TITLE_EMPTY                    the title file holds no title
    TITLE_LINES <count>            the title file holds more than one line
    TITLE_CLOSES <title>           the title holds a closing reference
    PLACEHOLDER <line> <what>      a placeholder the template left in the body, at its line
    CLOSES_LINE <line> <text>      a line holding a closing reference that is not `Closes #<n>` alone
    HEADING_MISSING <heading>      a template heading the body does not keep
    BEHIND <base> <sha>            the branch does not hold the base's tip: bring it up to date
    NO_COMMITS <base>              the branch has no commit the base does not hold
    UNCOMMITTED <count>            changed or untracked files the pull request would leave out
    UNPUSHED no upstream           open: the branch has no upstream: push it
    UNPUSHED <upstream>            open: the upstream does not hold HEAD: push it
    READY                          check: nothing to fix
    PULL_REQUEST <url>             open: the pull request opened

Exit status 0 means the body was drafted, the check found nothing, or the pull request opened; 1 one line per
problem, and open opens nothing, or a last line FAILED <reason>; 2 a usage error.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))

import whole_number
from closing_references import (
    CLOSING_KEYWORDS,
    CLOSING_PREFIX,
    CLOSING_REFERENCE,
    REFERENCE_START,
    pull_request_references,
    pull_request_text,
    reference_digits,
)
from console import run_main
from flat_text import flat_text
from git_client import GitClient, GitError
from github_client import GitHubClient, GitHubError, subprocess_runner
from markdown_sections import HTML_COMMENT
from typed_text import read_typed

TEMPLATE_NAME = "pull_request_template.md"
# The folders GitHub reads a pull request template from, relative to the top of the repository.
TEMPLATE_FOLDERS = (".github", "", "docs")
# A closing keyword and the start of an issue reference, `#`, `<owner>/<repo>#`, `GH-`, or a URL, which is refused,
# as a line that opens with it or anywhere in a title, unless it is `Closes #<n>` even where what follows is no
# reference CLOSING_REFERENCE reads.
CLOSING_START = re.compile(rf"(?i){CLOSING_PREFIX}{REFERENCE_START}")
CLOSING_LINE = re.compile(rf"(?i)\s*{CLOSING_PREFIX}{REFERENCE_START}")
BARE_CLOSING_LINE = re.compile(rf"(?i){CLOSING_KEYWORDS}\b:?\s+#")
CLOSES_ONE = re.compile(r"Closes #([1-9][0-9]*)")
EMPTY_LIST_ITEM = re.compile(r"\s*(?:[-*+]|[0-9]+[.)])\s*")
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$", re.MULTILINE)


class PullRequestError(Exception):
    """A failure the script names on a FAILED line."""


@dataclass(frozen=True)
class Base:
    remote: str
    branch: str

    def __str__(self) -> str:
        return f"{self.remote}/{self.branch}"


def base_argument(value: str) -> Base:
    remote, separator, branch = value.partition("/")
    if not (remote and separator and branch):
        raise argparse.ArgumentTypeError(f"{value!r} is not a remote's branch, such as origin/main")
    return Base(remote, branch)


def issue_argument(value: str) -> int:
    refused = argparse.ArgumentTypeError(f"{value!r} is not an issue number")
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise refused
    return whole_number.read(value, refused)


def blank_comments(text: str) -> str:
    """`text` with every comment's characters but its line breaks made spaces, so each line keeps its number."""
    return HTML_COMMENT.sub(lambda match: re.sub(r"[^\n]", " ", match.group()), text)


def find_template(top: Path) -> Path | None:
    """The pull request template GitHub reads for the repository at `top`, or None."""
    for folder in TEMPLATE_FOLDERS:
        directory = top / folder
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if entry.name.casefold() == TEMPLATE_NAME and entry.is_file():
                return entry
    return None


def draft_body(template: str, closes: Sequence[int]) -> str:
    """The template with one `Closes #<n>` line per issue in place of its bare closing line, or at the top."""
    lines = template.splitlines(keepends=True)
    blanked = blank_comments(template).splitlines()
    closing = [f"Closes #{number}\n" for number in closes]
    bare = [index for index, line in enumerate(blanked) if BARE_CLOSING_LINE.fullmatch(line.strip())]
    if not bare:
        return "".join(closing) + ("\n" if closing else "") + template
    first = bare[0]
    kept = [line for index, line in enumerate(lines) if index not in bare[1:]]
    return "".join(kept[:first] + closing + kept[first + 1 :])


def closed_issues(body: str, title: str | None = None) -> list[int]:
    """Each issue the title or the body closes, comments included, once, in order, by every closing reference GitHub
    reads, as closing_references reads them, or PullRequestError for a number too long to read."""
    if any(
        whole_number.parse(reference_digits(match)) is None
        for match in CLOSING_REFERENCE.finditer(pull_request_text(title, body))
    ):
        raise PullRequestError(
            f"a closing reference names an issue number of more than {whole_number.DIGIT_LIMIT} digits"
        )
    return list(dict.fromkeys(reference.number for reference in pull_request_references(title, body)))


def blank_template_comments(body: str, template: str) -> str:
    """`body` with each comment copied from `template` blanked as blank_comments blanks one, and every other comment
    kept, since the squash commit carries the comments the author wrote."""
    for comment in {match.group() for match in HTML_COMMENT.finditer(template)}:
        body = body.replace(comment, re.sub(r"[^\n]", " ", comment))
    return body


def headings(text: str) -> list[str]:
    """Each heading outside comments, as its hashes, a space, and its text."""
    return [f"{match.group(1)} {match.group(2)}" for match in HEADING.finditer(blank_comments(text))]


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def body_problems(body: str, template: str) -> list[str]:
    """One line per placeholder the template left in the body, closing line, and template heading it dropped."""
    problems: list[tuple[int, str]] = []
    for comment in {match.group() for match in HTML_COMMENT.finditer(template)}:
        start = body.find(comment)
        while start != -1:
            number = line_of(body, start)
            problems.append((number, f"PLACEHOLDER {number} a template comment"))
            start = body.find(comment, start + len(comment))
    # Both views blank characters but never a line feed, so split at line feeds they hold the same lines.
    read = blank_template_comments(body, template).split("\n")
    for number, (line, written) in enumerate(zip(blank_comments(body).split("\n"), read, strict=True), start=1):
        text = line.strip()
        if BARE_CLOSING_LINE.fullmatch(text):
            problems.append((number, f"PLACEHOLDER {number} a Closes line with no issue"))
        elif (CLOSING_REFERENCE.search(written) or CLOSING_LINE.match(written)) and not (
            CLOSES_ONE.fullmatch(written.strip()) or BARE_CLOSING_LINE.fullmatch(written.strip())
        ):
            problems.append((number, f"CLOSES_LINE {number} {flat_text(written.strip())}"))
        elif EMPTY_LIST_ITEM.fullmatch(line):
            problems.append((number, f"PLACEHOLDER {number} an empty list item"))
    lines = [problem for _, problem in sorted(problems, key=lambda problem: problem[0])]
    kept = set(headings(body))
    return lines + [f"HEADING_MISSING {flat_text(heading)}" for heading in headings(template) if heading not in kept]


def read_title(text: str) -> tuple[str, list[str]]:
    """The title a title file holds, and its problems: none, more than one line, or a closing reference, which the
    squash commit's title carries as GitHub reads it in the body."""
    title = text.strip()
    if not title:
        return "", ["TITLE_EMPTY"]
    count = len(title.splitlines())
    problems = [f"TITLE_LINES {count}"] if count > 1 else []
    if CLOSING_REFERENCE.search(title) or CLOSING_START.search(title):
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
        raise PullRequestError(f"git {arguments[0]}: {flat_text(str(exc))}") from exc


def quiet_output(git: GitClient, repository: Path, arguments: Sequence[str]) -> str | None:
    """The stdout of a git command run with `--quiet`, or None when it exits 1 and says nothing, which such a command
    does only when what it reads is not there; any other failure is git's, with its reason."""
    try:
        return git.quiet_output(arguments, directory=repository)
    except GitError as exc:
        raise PullRequestError(f"git {arguments[0]}: {flat_text(str(exc))}") from exc


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
        raise PullRequestError("no pull request template in .github/, the top folder, or docs/")
    return top, template


def draft(git: GitClient, repository: Path, closes: Sequence[int]) -> list[str]:
    top, template = template_of(git, repository)
    body = draft_body(read_file(template), closes)
    folder = Path(tempfile.mkdtemp(prefix="implement-change-"))
    (folder / "body.md").write_text(body, encoding="utf-8", newline="\n")
    return [
        f'TEMPLATE "{template.relative_to(top).as_posix()}"',
        *(f"CLOSES {number}" for number in closes),
        f'BODY "{folder / "body.md"}"',
        f'TITLE "{folder / "title.txt"}"',
    ]


def branch_facts(git: GitClient, repository: Path, base: Base) -> tuple[list[str], list[str]]:
    """The base's tip after a fetch, and whether the branch holds it, adds to it, and has nothing uncommitted."""
    try:
        git.output(["fetch", "--quiet", base.remote, base.branch], directory=repository)
    except GitError as exc:
        raise PullRequestError(f"cannot fetch {base.branch} from {base.remote}: {flat_text(str(exc))}") from exc
    reference = f"refs/remotes/{base}"
    commit = commit_of(git, repository, reference)
    if commit is None:
        raise PullRequestError(f"{base} names no commit after the fetch")
    problems = []
    # merge-base --is-ancestor exits 1 saying nothing when HEAD does not hold the base; any other failure is git's.
    if quiet_output(git, repository, ["merge-base", "--is-ancestor", commit, "HEAD"]) is None:
        problems.append(f"BEHIND {base} {commit}")
    if git_output(git, repository, ["rev-list", "--count", f"{commit}..HEAD"]).strip() == "0":
        problems.append(f"NO_COMMITS {base}")
    changed = git_output(git, repository, ["status", "--porcelain", "--no-renames", "-z"]).split("\0")[:-1]
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
    closed = closed_issues(body, title or None)
    facts = ([f"TITLE {flat_text(title)}"] if title else []) + [f"CLOSES {number}" for number in closed]
    base_facts, branch_problems = branch_facts(git, repository, base)
    problems = title_problems + body_problems(body, read_file(template)) + branch_problems
    return title, facts + base_facts, problems


def upstream_problems(git: GitClient, repository: Path) -> list[str]:
    """Whether the branch's upstream holds HEAD. A detached HEAD has no upstream, and an upstream not fetched holds
    nothing."""
    branch = quiet_output(git, repository, ["symbolic-ref", "--quiet", "HEAD"])
    if branch is None:
        return ["UNPUSHED no upstream"]
    reference, _, name = (
        git_output(git, repository, ["for-each-ref", "--format=%(upstream)%00%(upstream:short)", branch.strip()])
        .removesuffix("\n")
        .partition("\0")
    )
    if not reference:
        return ["UNPUSHED no upstream"]
    head = git_output(git, repository, ["rev-parse", "HEAD"]).strip()
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
    draft_parser.add_argument("--closes", type=issue_argument, action="append", default=[], metavar="ISSUE")
    files = argparse.ArgumentParser(add_help=False)
    files.add_argument("--title-file", type=Path, required=True)
    files.add_argument("--body-file", type=Path, required=True)
    files.add_argument("--base", type=base_argument, required=True, help="a remote's branch, such as origin/main")
    commands.add_parser("check", parents=[files], help="check the title, the body, and the branch")
    commands.add_parser("open", parents=[files], help="check, then open the pull request")
    return parser


def run(options: argparse.Namespace, git: GitClient, github: GitHubClient | None) -> int:
    repository: Path = options.repository
    if options.command == "draft":
        print("\n".join(draft(git, repository, options.closes)))
        return 0
    title, facts, problems = check(git, repository, options.title_file, options.body_file, options.base)
    if options.command == "open":
        problems += upstream_problems(git, repository)
    print("\n".join(facts + problems))
    if problems:
        return 1
    if options.command == "check":
        print("READY")
        return 0
    client = github or GitHubClient(runner=partial(subprocess_runner, cwd=repository))
    print(open_pull_request(client, title, options.body_file, options.base))
    return 0


def main(
    arguments: Sequence[str] | None = None, *, git: GitClient | None = None, github: GitHubClient | None = None
) -> int:
    options = build_parser().parse_args(arguments)
    try:
        return run(options, git or GitClient(), github)
    except PullRequestError as exc:
        print(f"FAILED {exc}")
        return 1


if __name__ == "__main__":
    run_main(main)
