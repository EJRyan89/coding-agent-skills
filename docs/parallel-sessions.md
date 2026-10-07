# Parallel sessions

Several agent sessions often work in this repository at the same time. Sessions that share one checkout also
share its HEAD, its index, and its working tree. When one session switches branch or commits, it takes along
whatever another session has left uncommitted. This has happened here. The rules in `CLAUDE.md` prevent it by
giving each task its own git worktree. This page covers the reasoning and what the guard does and does not
catch.

## Layout

```text
coding-agent-skills/                hub: always on main, always clean, never edited
  .claude/worktrees/                gitignored
    feat-<name>/                    branch feat/<name>
    fix-<name>/                     branch fix/<name>
    inv-<topic>/                    investigations that may produce no commits
```

The trees go in `.claude/worktrees/` because Claude Code uses that folder. `EnterWorktree name:` and the
desktop app both create session worktrees there by default. `EnterWorktree path:` will switch a session that is
already in a worktree only to another tree under that folder.

The folder is gitignored, so the hub stays clean:

- `git status` in the hub does not list the trees.
- The hub's dirty count stays at zero.
- `tests/run_validation.py` skips the folder when it scans for private references, because that scan reads only
  files git does not ignore.

`tools/worktrees.py new` refuses to create a tree until the hub ignores the folder.

Two things follow from nesting:

- Deleting or re-cloning the hub folder deletes every worktree inside it.
- `git clean -x` in the hub would remove them. The guard refuses `clean` in the hub.

`python tools/worktrees.py new <kind> <name>` creates a worktree from the hub's local `main`, with no upstream.
Pull `main` in the hub first if it may be behind. `python tools/worktrees.py list` shows one row per worktree:

| Column | Meaning |
|---|---|
| state | `hub`, `in progress`, `no commits`, or `missing` |
| ahead/behind | Relative to `main`. |
| dirty | Number of uncommitted entries. |

The hub row reads `off main` or `dirty` when the hub has drifted. Merged state is not reported: squash merges
leave every branch looking unmerged, so a branch's pull request is the only reliable record.

## Retiring a worktree

After a task's pull request merges, run the `repo-cleanup` skill on the hub (by repository name, or by the hub's
absolute path). In one pass it does the following:

- **Updates the hub.** It fetches with prune and fast-forwards `main`.
- **Removes finished worktrees.** A worktree is removed when it is clean and its branch's remote is gone. The
  branch must also have no pull request at all, or one that merged or closed at exactly the branch's local tip.
- **Deletes their branches, squash merges included.** `git branch -d` refuses a squash-merged branch, so
  `repo-cleanup` uses `-D`. It does so only when GitHub shows the pull request merged at that exact tip.
- **Removes leftover folders.** Empty parent folders of a removed tree go, but `.claude` itself always stays,
  because it holds `settings.json`.

Anything else is kept and reported:

- trees with uncommitted changes;
- open pull requests;
- branches with no matching pull request;
- branches still on the remote.

So the trees other sessions are still using survive the sweep.

Run it from a session that is **not** inside the tree being retired. Windows will not delete a folder that is
some process's working directory. Git then removes the files and the worktree record but fails on the folder
itself, and `repo-cleanup` reports the tree as preserved.

To retire a tree by hand instead, run these from the hub:

```bash
git worktree remove '.claude/worktrees/<kind>-<name>'
git branch -D '<kind>/<name>'
```

## What is shared even across worktrees

Worktrees isolate files. These are still shared:

- **The stash stack and every ref.** Never pop or drop a stash you did not create.
  - Prefer a temporary WIP commit.
  - If you must stash, push with a unique message, then apply your entry by SHA.
- **Deployer state under `~/.claude`.** The deploy lock is keyed by the home folder, so it spans worktrees: a
  second deploy fails at once with "Another deployment is running". Deploying records its source path in the
  manifest and in the rendered `update-coding-agent-skills` skill, so `deploy.py` refuses to deploy from a
  linked worktree, whose path disappears when the task ends. Deploy from the hub on `main`. `--dry-run` is
  allowed anywhere, and so is `--canary-home`, which deploys into an empty directory under the temporary
  directory that is thrown away with the source path it records.
- **Nothing in validation.** Every suite isolates its own temporary home and fixtures, and the suites that
  deploy this repository deploy a temporary copy of it, or into a `--canary-home`. Any number of worktrees can run
  `tests/run_validation.py` at once.

## The guard

`.claude/settings.json` declares a Claude Code `PreToolUse` hook that runs `tools/worktrees.py guard`. A hook
in that file can only deny an action; it never grants one.

The guard is **inert unless a clone opts in**:

```bash
git config coding-agent-skills.hubGuard true
```

The setting lives in the shared `.git/config`, so one command covers the hub and all of its worktrees. Because
the setting is not tracked, someone who clones the repository and works in a single checkout is never blocked.

The hook command reads the setting itself, in Bash, and exits before starting Python when the clone has not opted
in. Elsewhere the hook is inert and costs one `git config` call per tool call: `python` never runs, so a `python`
that still resolves to the Microsoft Store alias cannot fail the hook. In an opted-in clone it runs
`tools/worktrees.py guard` under `$CLAUDE_PROJECT_DIR`, which checks the same setting again.
`tests/tools/test_worktrees.py` runs the tracked command both ways.

With the guard enabled:

- **File tools.** `Edit`, `Write`, `MultiEdit`, or `NotebookEdit` is refused when its target lies in the hub.
  This applies whichever session asks, so a worktree session that writes to a hub path by absolute path is also
  stopped. Gitignored files, such as `.claude/settings.local.json`, are exempt.
- **Refused git commands.** Any of the following that would run in the hub:
  - `add`, `am`, `apply`, `bisect`, `checkout`, `checkout-index`, `cherry-pick`, `clean`, `commit`
  - `merge`, `mv`, `pull`, `read-tree`, `rebase`, `reset`, `restore`, `revert`, `rm`
  - `sparse-checkout`, `stash`, `submodule update`, `switch`, `symbolic-ref`, `update-index`, `update-ref`
- **Still allowed in the hub.** Only these forms of a refused command, which keep the hub on `main` and current:
  - `switch main` and `checkout main`, optionally with `-q`
  - `merge --ff-only origin/main`, and `pull --ff-only` with no target or with `origin main`; besides
    `--ff-only`, each may take only `-q`, `--quiet`, `-v`, `--verbose`, or `--no-rebase`, and a fast-forward to
    any other target is refused with a reason that names it
  - `stash list` and `stash show`
  - `apply --check`
  - `submodule` with any subcommand other than `update`
- **Aliases.** A subcommand that is not refused itself may be one of your git aliases, so the guard reads it
  with `git config --get alias.<name>` in the hub and judges what it runs, through further aliases. A shell
  alias (`!...`) is read as Bash from the top of the hub. An alias that cannot be read, nests more than four
  deep, or runs something the guard cannot judge is refused.
- **Both shell tools.** The same commands are refused, with the same allowances, whether they run through the
  `Bash` tool or the `PowerShell` tool.
- **How the target is found.** The guard follows where each git call runs, so the hub can still act on a
  worktree and a worktree session cannot reach into the hub. In both shells it follows `git -C <dir>` and
  `--work-tree`. A `--git-dir`, or a `GIT_DIR` that is set, leaves the directory unknown. Each shell also has
  its own rules:
  - **Bash.** The guard follows `cd <dir>` and a `GIT_WORK_TREE=` prefix. Git Bash paths such as `/c/...` are
    understood.
  - **PowerShell.** The guard follows `cd`, `chdir`, `sl`, and `Set-Location`, with `-Path` or `-LiteralPath`
    written out, abbreviated, or omitted:
    - a bare `cd` goes home, and a leading `~` means home;
    - `cd -` and `cd +` leave the directory unknown;
    - `Push-Location` and `Pop-Location` keep a stack, and a pop past its bottom leaves the directory unknown;
    - a location set inside a script block outlives the block, as it does in PowerShell;
    - `$env:NAME`, and strings whose only expansions are environment variables that are set, are expanded;
    - once `$env:GIT_DIR` or `$env:GIT_WORK_TREE` is assigned, the directory is unknown.
  - **Nested shells.** `bash -c` and `pwsh -c` (or `powershell -Command`) with a fixed script are read in
    either shell. The nested script runs as its own process, so its `cd` does not outlive it. PowerShell's
    `Invoke-Expression` (`iex`) with a fixed string is read as part of the same session.
- **How PowerShell is read.** A PowerShell command is read by PowerShell's own parser, not by a second
  hand-written grammar. Here-strings, backtick escapes, and comments are where a tokenizer would misread a
  quote and miss the command beside it. `tools/worktrees-powershell.ps1` only reports what the parser found,
  in the order PowerShell would run it, and `tools/worktrees.py` makes every decision.
  - **When pwsh starts.** Starting `pwsh` costs far more than the guard's git calls, so the guard starts it
    only when the text names `git` and, as a word, one of the refused subcommands or one of the aliases git
    lists where the command starts. `git status`, `git log`, and every non-git command never wait for it.
  - **A command that doesn't parse is allowed.** PowerShell parses a whole script before it runs any of it, so
    none of such a command runs. A Bash command is different: the lines before a syntax error do run.

The guard catches mistakes, not determined effort. It does not see:

- shell redirection or other programs writing files;
- git run indirectly from inside a script, or through `Start-Process`, `cmd /c`, or `pwsh -EncodedCommand`;
- a git subcommand spelled with escapes or computed at run time, or a directory the text does not settle;
- Bash commands whose quoting it cannot tokenize;
- a PowerShell command when `pwsh` cannot be started, or does not finish reading it within 20 seconds.

It also fails open: input it cannot judge is allowed, with a note on stderr, so that a guard defect cannot
block every tool. The one exception is an alias that runs in the hub: one it cannot read is refused. Runtimes other than Claude Code do not run the hook, so for them the `CLAUDE.md` rules are
the guard.

`.claude/settings.json` is tracked and reaches every developer's sessions. It may therefore declare hooks and
`attribution`, which turns off Claude Code's commit trailers, pull request footer, and session links for this
repository, and nothing else. A rule about what a developer must allow belongs in that developer's own user
settings or `settings.local.json`. `tests/run_validation.py` fails if any other top-level key appears, or if a
`settings.local.json` is ever committed. It also fails when the guard's hook stops matching any tool that can
edit a file or run git (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `Bash`, and `PowerShell`). A tool
missing from the matcher is never shown to the guard, so the hub would be open through it.

## Session context in a worktree

Claude Code keys session transcripts by the absolute path of the session's working directory, so a worktree
session is filed separately from the hub's. Tracked context travels with the worktree: `CLAUDE.md`,
`.claude/settings.json`, and anything else committed. If the hub's project memory index is not in a worktree
session's context, read it explicitly and say that you did, rather than working without it.

### Claude Code's worktree isolation

A session that enters a worktree with `EnterWorktree` also runs under Claude Code's own worktree isolation. It
is separate from [the guard](#the-guard): Claude Code enforces it in every repository, and nothing in this
repository can configure or relax it. It refuses any shell command it cannot prove stays inside the worktree,
with a message that begins "This session is isolated in the worktree". It fails closed by design, so it also
refuses harmless commands, including read-only ones that never touch the hub.

The message usually names its reason, such as "runs python with a value computed at runtime" or "names git in
a form too complex to verify", and sometimes suggests the fix. Which part of a refused command triggered it is
not always clear, but these workarounds have held up:

- **Literal paths.** Write a path out instead of computing it: not `python -B "$TEMP/edit.py"`, and not a glob
  with backslash-escaped spaces.
- **Quoted operands.** In a loop, double-quote a variable that stands where an option may
  (`"skills/$f/SKILL.md"`), or put `--` before it.
- **One plain command per call.** When git or `gh` is involved, or the command is long, run it on its own:
  no `&&`, `;`, redirects, or pipes, and no trailing `; echo exit=$?`.
- **Bodies from files.** Pass a pull request or issue body with `gh pr create --body-file <file>` (or
  `gh issue create --body-file`) whenever it contains backticks or `$`. This repository already requires
  `--body-file`; see "Issues and pull requests" in `CLAUDE.md`.
- **Helper scripts on disk.** Write a helper script to a file and run it by its literal path.
- **Dedicated tools.** Use the `Glob`, `Grep`, and `Read` tools instead of `ls`, `find`, `grep`, or `sed` to
  search or read files; they are not shell commands and are not refused.
