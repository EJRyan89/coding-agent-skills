"""A throwaway folder a runtime starts in, made the git root that ends the runtime's upward search for configuration.

Copilot CLI reads project skills in `.github/skills/`, `.agents/skills/`, and `.claude/skills/`, and project agents in
`.github/agents/` and `.claude/agents/`, from its working directory up to the git root, and Claude Code reads
`.claude/skills/` and `.claude/agents/` the same way. With no git root each reads every folder above the working
directory, where a throwaway folder's parents, the system's shared temporary directory on Linux among them, are not the
caller's: a skill or an agent planted there would load as trusted project configuration, or stand in for a deployed
one of its name. A folder that is a git repository of its own ends both searches at itself, whatever repository holds
it. Codex, started outside any repository, reads only its working directory, so it needs nothing here.

It does not end Claude Code's search for `CLAUDE.md`, which continues to the file system's root past any git root.

Nothing commits in such a folder, so a hook the user's own git templates copy into it never runs.
"""

from __future__ import annotations

from pathlib import Path

from git_client import GitClient


def bound_search(folder: Path, git: GitClient | None = None) -> None:
    """Make the existing `folder` a git repository of its own, so a runtime started in it reads project skills and
    agents from it alone. Raise GitError, with git's reason, when git cannot."""
    (git or GitClient()).output(["init", "--quiet"], directory=folder)
