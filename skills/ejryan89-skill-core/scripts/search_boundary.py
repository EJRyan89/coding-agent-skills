"""A throwaway folder a runtime starts in, made the git root that ends the runtime's upward search for configuration.

Copilot CLI reads project skills in `.github/skills/`, `.agents/skills/`, and `.claude/skills/`, and project agents in
`.github/agents/` and `.claude/agents/`, from its working directory up to the git root, and Claude Code reads
`.claude/skills/` and `.claude/agents/` the same way. With no git root each reads every folder above the working
directory, where a throwaway folder's parents, the system's shared temporary directory on Linux among them, are not the
caller's: a skill or an agent planted there would load as trusted project configuration, or stand in for a deployed
one of its name. A folder that is a git repository of its own ends both searches at itself, whatever repository holds
it. Codex, started outside any repository, reads only its working directory, so it needs nothing here.

It does not end Claude Code's search for instructions: `CLAUDE.md`, `CLAUDE.local.md`, `.claude/CLAUDE.md`, and
`.claude/rules/` are read from every folder above the working directory up to the file system's root, past any git
root. A Claude Code session started in such a folder therefore runs with CLAUDE_INSTRUCTIONS_AND_MEMORY_OFF_VARIABLES
in its environment, which switches off every instruction file and auto memory, the user's own included, wherever it
lies, and keeps the session from writing auto memory into the user's own configuration.

Nothing commits in such a folder, so a hook the user's own git templates copy into it never runs.
"""

from __future__ import annotations

from pathlib import Path

from git_client import GitClient

# The settings every Claude Code session started in such a folder runs with, one constant rather than one per setting,
# since no session needs one without the other and a caller that spread only one would leave the other's gap open.
# CLAUDE_CODE_DISABLE_CLAUDE_MDS keeps the session from reading any CLAUDE.md, CLAUDE.local.md, or rules file as
# instructions, or its auto memory, since its search for them does not stop at a git root; checked against Claude Code
# with a local stand-in for its API, so no model started: with it, no such file above or in the working directory
# reached the request; without it, each one in every folder above the git root did. It stops loading only ("prevent
# loading any CLAUDE.md memory files into context, including user, project, and auto memory files"), so
# CLAUDE_CODE_DISABLE_AUTO_MEMORY keeps the session from writing auto memory too: "When disabled, Claude does not create
# or load auto memory files" (both on https://code.claude.com/docs/en/env-vars). Without it a session could save
# memories under ~/.claude/projects/<project>/memory/ in the user's own configuration, keyed by the folder's git
# repository ("Storage location" and "Enable or disable auto memory", https://code.claude.com/docs/en/memory).
# A module-level *_VARIABLES collection, so tools/runtime_changelogs.py watches the changelogs for both.
CLAUDE_INSTRUCTIONS_AND_MEMORY_OFF_VARIABLES = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}


def bound_search(folder: Path, git: GitClient | None = None) -> None:
    """Make the existing `folder` a git repository of its own, so a runtime started in it reads project skills and
    agents from it alone. Raise GitError, with git's reason, when git cannot."""
    (git or GitClient()).output(["init", "--quiet"], directory=folder)
