"""The update's two calls to the clone's `origin`: the tag check and the fetch, run through ejryan89-skill-core's
`GitClient` rather than from Bash.

A person starts the update, but the agent that runs it shows no terminal and answers no dialog, so a prompt would hold
it with no end. `GitClient` runs each command as every git command a skill's Python runs: with no stdin, no terminal
prompt, no askpass helper, whether `GIT_ASKPASS`, `core.askPass`, or `SSH_ASKPASS` names one, no Git Credential
Manager dialog, none of git's repository-selecting variables, and a time limit, so a remote that wants a credential
the helpers cannot give without asking, or that never answers, fails at once or within the limit, and the update
stops with `FETCH_FAILED` before anything is merged or deployed. `update.sh` runs this from the copy of the skill the
deployment installed, beside the ejryan89-skill-core it imports.

Usage:
  python remote_refs.py shadows <clone> [--seconds <seconds>]
  python remote_refs.py fetch <clone> [--seconds <seconds>]

`shadows` lists the tags on origin named like a branch the update reads, `refs/tags/main` and `refs/tags/origin/main`,
printing `ON_ORIGIN <tag>` for each, then `CHECKED`. `fetch` fetches origin's main into `refs/remotes/origin/main`,
with origin's tags but those two, and prints `FETCHED`. Either prints a last line `FAILED <reason>` instead, git's
error or the time limit it ran out, on one line. `--seconds` is each command's time limit, retries of a transient
fault included; it defaults to the client's.

Exit status:
  0  CHECKED, after any ON_ORIGIN lines; or FETCHED
  1  a last line FAILED <reason>: git could not reach origin, was refused, failed, or ran out its time limit
  2  a usage error
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from console import run_main
from flat_text import flat_text
from git_client import DEFAULT_TIMEOUT_SECONDS, GitClient, GitError, output_lines
from seconds_option import positive_seconds

MAIN = "refs/heads/main"
ORIGIN_MAIN = "refs/remotes/origin/main"
# Tags git would resolve before the branches the update names by their short names, main and origin/main.
SHADOWING_TAGS = ("refs/tags/main", "refs/tags/origin/main")
FETCH_HELP = "fetch origin's main and its tags but those named like main or origin/main"


def shadowing_tags(git: GitClient, clone: Path, seconds: float) -> list[str]:
    """The tags on origin among `SHADOWING_TAGS`; raises GitError when git cannot list them."""
    listed = git.output(["ls-remote", "--tags", "--refs", "origin", *SHADOWING_TAGS], directory=clone, timeout=seconds)
    names = (line.split("\t", 1)[-1] for line in output_lines(listed))
    return [name for name in names if name in SHADOWING_TAGS]


def fetch(git: GitClient, clone: Path, seconds: float) -> None:
    """Fetch origin's main and its tags, but a tag that would shadow a branch, which origin may have published since
    the check; raises GitError when the fetch fails."""
    refspecs = [f"+{MAIN}:{ORIGIN_MAIN}", *(f"^{tag}" for tag in SHADOWING_TAGS)]
    git.output(["fetch", "--quiet", "--tags", "origin", *refspecs], directory=clone, timeout=seconds)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Check origin for shadowing tags, or fetch origin's main.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, purpose in (("shadows", "list the tags on origin named like main or origin/main"), ("fetch", FETCH_HELP)):
        command = commands.add_parser(name, help=purpose)
        command.add_argument("clone", type=Path, help="the coding-agent-skills clone")
        command.add_argument(
            "--seconds",
            type=positive_seconds,
            default=DEFAULT_TIMEOUT_SECONDS,
            help="the command's time limit in seconds",
        )
    options = parser.parse_args(argv)
    git = GitClient()
    try:
        if options.command == "shadows":
            for tag in shadowing_tags(git, options.clone, options.seconds):
                print(f"ON_ORIGIN {flat_text(tag)}")
            print("CHECKED")
        else:
            fetch(git, options.clone, options.seconds)
            print("FETCHED")
    except GitError as error:
        print(f"FAILED {flat_text(str(error))}")
        return 1
    return 0


if __name__ == "__main__":
    run_main(lambda: main(sys.argv[1:]))
