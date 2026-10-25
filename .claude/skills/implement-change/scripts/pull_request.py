"""The deterministic part of opening a pull request, so the agent only words the title and the body.

    draft  write a body from the repository's pull request template, with one `Closes #<n>` line per --closes in
           place of the template's bare one, to a new folder, and name the file the title goes in
    check  check the title file, the body file, and the branch against --base, a remote's branch such as
           origin/main, which it fetches first
    open   check as `check` does, check that the branch's upstream holds HEAD, then open the pull request with the
           title read from its file, so no title is ever pasted into a shell command

The template is `pull_request_template.md`, in any case, in `.github/`, the top folder, or `docs/`, as GitHub reads
it. The body check refuses what the template leaves for the author: a comment copied from it, a closing line with no
issue, and an empty list item; a closing line that is not `Closes #<n>` naming one issue; and a template heading the
body dropped. A comment the author wrote is not read.

Every line is one fact:

    TEMPLATE "<file>"              draft: the template read, from the top of the repository
    CLOSES <n>                     each issue a `Closes #<n>` line names
    BODY "<file>"                  draft: the body written, for the author to fill
    TITLE "<file>"                 draft: where to write the title, one line; nothing is there yet
    TITLE <title>                  check, open: the title read
    BASE <remote>/<branch> <sha>   check, open: the base's tip after the fetch
    TITLE_EMPTY                    the title file holds no title
    TITLE_LINES <count>            the title file holds more than one line
    PLACEHOLDER <line> <what>      a placeholder the template left in the body, at its line
    CLOSES_LINE <line> <text>      a closing line that is not `Closes #<n>` naming one issue
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
from console import run_main
from flat_text import flat_text
from git_client import GitClient, GitError
from github_client import GitHubClient, GitHubError, subprocess_runner
from markdown_sections import HTML_COMMENT
from typed_text import read_typed

TEMPLATE_NAME = "pull_request_template.md"
# The folders GitHub reads a pull request template from, relative to the top of the repository.
TEMPLATE_FOLDERS = (".github", "", "docs")
# A line GitHub reads as closing an issue: a closing keyword, then an issue reference.
CLOSING_LINE = re.compile(r"(?i)\s*(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+(?:[\w.-]+/[\w.-]+)?#")
BARE_CLOSING_LINE = re.compile(r"(?i)(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+#")
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


def closed_issues(body: str) -> list[int]:
    """The issue each `Closes #<n>` line of the body names, outside comments, or PullRequestError for a number too
    long to read."""
    found = (CLOSES_ONE.fullmatch(line.strip()) for line in blank_comments(body).splitlines())
    refused = PullRequestError(f"a Closes line names an issue number of more than {whole_number.DIGIT_LIMIT} digits")
    return [whole_number.read(match.group(1), refused) for match in found if match]


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
    for number, line in enumerate(blank_comments(body).splitlines(), start=1):
        text = line.strip()
        if BARE_CLOSING_LINE.fullmatch(text):
            problems.append((number, f"PLACEHOLDER {number} a Closes line with no issue"))
        elif CLOSING_LINE.match(line) and not CLOSES_ONE.fullmatch(text):
            problems.append((number, f"CLOSES_LINE {number} {flat_text(text)}"))
        elif EMPTY_LIST_ITEM.fullmatch(line):
            problems.append((number, f"PLACEHOLDER {number} an empty list item"))
    lines = [problem for _, problem in sorted(problems, key=lambda problem: problem[0])]
    kept = set(headings(body))
    return lines + [f"HEADING_MISSING {flat_text(heading)}" for heading in headings(template) if heading not in kept]


def read_title(text: str) -> tuple[str, list[str]]:
    """The title a title file holds, and its problems."""
    title = text.strip()
    if not title:
        return "", ["TITLE_EMPTY"]
    count = len(title.splitlines())
    return title, [f"TITLE_LINES {count}"] if count > 1 else []


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
        result = git.run(arguments, directory=repository)
    except GitError as exc:
        raise PullRequestError(f"git {arguments[0]}: {flat_text(str(exc))}") from exc
    if result.returncode == 0:
        return result.stdout
    if result.returncode == 1 and not result.stderr.strip():
        return None
    reason = flat_text(result.stderr) or f"failed with exit code {result.returncode}"
    raise PullRequestError(f"git {arguments[0]}: {reason}")


def commit_of(git: GitClient, repository: Path, reference: str) -> str | None:
    """The commit `reference` names, or None when it names none."""
    output = quiet_output(git, repository, ["rev-parse", "--verify", "--quiet", f"{reference}^{{commit}}"])
    return None if output is None else output.strip()


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
    holds = git.run(["merge-base", "--is-ancestor", commit, "HEAD"], directory=repository)
    if holds.returncode == 1:
        problems.append(f"BEHIND {base} {commit}")
    elif holds.returncode != 0:
        raise PullRequestError(f"git merge-base: {flat_text(holds.stderr)}")
    if git_output(git, repository, ["rev-list", "--count", f"{commit}..HEAD"]).strip() == "0":
        problems.append(f"NO_COMMITS {base}")
    changed = git_output(git, repository, ["status", "--porcelain"]).splitlines()
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
    facts = ([f"TITLE {flat_text(title)}"] if title else []) + [f"CLOSES {number}" for number in closed_issues(body)]
    base_facts, branch_problems = branch_facts(git, repository, base)
    problems = title_problems + body_problems(body, read_file(template)) + branch_problems
    return title, facts + base_facts, problems


def upstream_problems(git: GitClient, repository: Path) -> list[str]:
    """Whether the branch's upstream holds HEAD. A detached HEAD has no upstream, and an upstream not fetched holds
    nothing."""
    branch = quiet_output(git, repository, ["symbolic-ref", "--quiet", "HEAD"])
    if branch is None:
        return ["UNPUSHED no upstream"]
    upstream = git_output(
        git, repository, ["for-each-ref", "--format=%(upstream) %(upstream:short)", branch.strip()]
    ).split()
    if not upstream:
        return ["UNPUSHED no upstream"]
    reference, name = upstream
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
