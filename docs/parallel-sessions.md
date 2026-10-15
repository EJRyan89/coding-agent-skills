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
  allowed anywhere, and so is `--canary-home`, which deploys into a directory under the temporary directory,
  empty or one an earlier canary deployment marked, that is thrown away with the source path it records.
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
in. Elsewhere the hook is inert and costs one `awk` and one `git config` call per tool call: `python` never runs, so
a `python` that still resolves to the Microsoft Store alias cannot fail the hook. In an opted-in clone it runs
`tools/worktrees.py guard` under `$CLAUDE_PROJECT_DIR`, which checks the same setting again. Neither read can be
pointed at another repository's setting or outranked: each reads only the repository's own file, with
`git config --local`, which passes over the command-line configuration `GIT_CONFIG_PARAMETERS` and `GIT_CONFIG_COUNT`
carry; the hook command unsets `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, and `GIT_CONFIG` for its call, each in
any case, since Windows reads an environment name in any case; and `tools/worktrees.py` runs its own git calls
without any of git's repository-selecting variables, the set skill-core's `bounded_process.py` names.
`tests/tools/test_worktrees.py` runs the tracked command both ways.

With the guard enabled:

- **File tools.** `Edit`, `Write`, `MultiEdit`, or `NotebookEdit` is refused when its target lies in the hub's
  tracked files or anywhere in its git directory, `.git`, which holds the hub's `HEAD`, index, and configuration
  beside what every worktree shares. This applies whichever session asks, so a worktree session that writes to a hub
  path by absolute path is also stopped. Gitignored files, such as `.claude/settings.local.json`, are exempt.
- **Refused git commands.** Any of the following that would act on the hub, because some subcommand or option of
  each can move `HEAD` or the branch it names, or write the index or tracked files. A command that writes only
  objects, other refs (`notes`, `replace`, `tag`), or other worktrees (`worktree`) is not refused, and nor is
  `fetch` unless it is given `--update-head-ok` (or `-u`), which lets it move the branch `HEAD` names.
  - `add`, `am`, `apply`, `bisect`, `branch`, `checkout`, `checkout-index`, `cherry-pick`, `clean`, `commit`
  - `fast-import`, `filter-branch`, `history`, `merge`, `merge-file`, `merge-index`, `merge-one-file`, `mv`
  - `pull`, `read-tree`, `rebase`, `replay`, `rerere`, `reset`, `restore`, `revert`, `rm`, `sparse-checkout`
  - `stage`, `stash`, `submodule`, `switch`, `symbolic-ref`, `update-index`, `update-ref`
  - the interactive tools `citool`, `difftool`, `gitk`, `gui`, and `mergetool`
  - the importers `archimport`, `cvsimport`, `p4`, `quiltimport`, and `svn`
- **Still allowed in the hub.** Only these forms of a refused command, which keep the hub on `main` and current:
  - `switch main` and `checkout main`, optionally with `-q`
  - `merge --ff-only origin/main`, and `pull --ff-only` with no target or with `origin main`; besides
    `--ff-only`, each may take only `-q`, `--quiet`, `-v`, `--verbose`, or `--no-rebase`, and a fast-forward to
    any other target is refused with a reason that names it
  - `stash list` and `stash show`
  - `apply --check`, with `--check` among the options before any `--` and no `--apply`, which applies the patch
    all the same
  - `submodule` with no subcommand, `submodule status`, and `submodule summary`; every other subcommand, `foreach`
    included, is refused
  - `branch` without `-m`, `-M`, or `--move`, which rename a branch, `HEAD`'s own included; an abbreviated
    `--move` or a bundle such as `-fm` counts as one. Attached option values are not bundles: `-uorigin/main`
    is allowed like `-u origin/main` and `--set-upstream-to=origin/main`, while a separate rename option
    before or after the upstream value is still refused
- **Aliases.** A subcommand that is neither refused itself nor one of git's builtins, which no alias can stand for,
  may be an alias, so the guard reads it, first among the `alias.<name>` settings the command line gives (`-c`,
  `GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT`), then with `git config --get alias.<name>` in the hub, and judges what
  it runs, through further aliases. A shell alias (`!...`) is read as Bash from the top of the hub. An alias that
  cannot be read, nests more than four deep, or runs something the guard cannot judge is refused.
- **Both shell tools.** The same commands are refused, with the same allowances, whether they run through the
  `Bash` tool or the `PowerShell` tool.

Runtimes other than Claude Code do not run the hook, so for them the `CLAUDE.md` rules are the guard.

### The guard's contract

The hook is given the text of a command, not what runs under it. The guard reads that text with each shell's own
rules, and every decision it makes is one of three: what its readers settle is judged, what they cannot settle is
refused while a guarded hub is in view, and what they do not see at all is named below.

#### What the readers settle

- **Where a git call acts.** git writes the `HEAD` and index of its repository and the files of its work tree, so a
  call acts on the hub when either one is the hub's. The repository is the one `--git-dir` or `GIT_DIR` names, or
  else the one git finds from where it starts; the work tree is the one `--work-tree`, `GIT_WORK_TREE`, or a
  `core.worktree` setting names, or else where git starts, after each `-C`. A relative path is read from where git
  finally starts, and a relative `core.worktree` from the repository it is set for. `GIT_COMMON_DIR` names only the
  store of refs and objects every worktree shares, so it does not change which checkout a call writes. The guard
  reads git's own options as git does, with no abbreviation and with a value attached after `=` or as the next word
  (`-C`, `-c`, `--attr-source`, `--config-env`, `--git-dir`, `--namespace`, `--super-prefix`, `--work-tree`), and
  `core.worktree` and `alias.<name>` among the command-line configuration a call sees: `GIT_CONFIG_PARAMETERS`,
  `GIT_CONFIG_COUNT` with its pairs, then each `-c` and `--config-env`.
- **Location variables.** `GIT_DIR`, `GIT_WORK_TREE`, and the two configuration variables reach a git call from
  the environment Claude Code gives the hook, and from whatever the command sets: in Bash, for one command
  (`GIT_DIR=<dir> git ...`, `env GIT_DIR=<dir> git ...`) or exported for the rest (`export`, `declare -x`,
  `typeset -x`, an assignment under `set -a` or to a variable already exported), and removed by `unset`,
  `export -n`, `env -i`, and `env -u`; in PowerShell, through `$env:<name>` and `${<path>}` on the environment
  provider (cast, in parentheses, or as any target of a multiple assignment), a `foreach` loop's variable,
  `PSVariable.Set`, `[Environment]::SetEnvironmentVariable`, and the cmdlets that write the provider: `Set-Item`,
  `New-Item`, and `Set-Content` at their path, `Copy-Item` at its destination, `New-Item` on the provider's root
  with `-Name`, and `Rename-Item` with `-NewName`, while `Remove-Item` and `Clear-Item` remove one, and so does
  assigning `$null`. The provider's paths are the `env:` drive (`env:<name>`, `env:\<name>`, `env:/<name>`) and
  `Environment::<name>` (also `\Environment::<name>` and `Microsoft.PowerShell.Core\Environment::<name>`); after
  `Environment::` a separator becomes part of the variable's name, which git never reads. On Windows git reads an
  environment name in any case, so `git_dir` is `GIT_DIR` there; elsewhere only the upper-case name counts. Bash
  keeps each spelling a variable of its own, and when two spellings reaching git disagree, the call is judged by
  the upper-case one, the spelling git reads, with a note.
- **Bash, by its grammar.** `tools/bash_syntax.py` reads Bash as Bash parses it: single and double quotes, `$'...'`,
  escapes and line continuations, comments, and here-documents, whose bodies are data and never commands; and the
  command tree of lists, `&&` and `||`, pipelines, `( )` subshells, `{ }` groups, `if`, `while`, `until`, `for`,
  `select`, `case`, `[[ ]]`, `!`, `time`, and function definitions, with redirections. A word's value comes from its
  literal text, `~`, and each `$NAME` the command sets or the environment holds, and `$PWD` is where the shell is; a
  substitution, arithmetic, a special parameter, a glob, a brace expansion, or a value that would split leaves the
  word unsettled. The guard follows `cd` (with `--`; bare goes home, `cd -` and a `CDPATH` search leave the
  directory unknown), `pushd`, `popd`, and `env -C`, and runs a function's body where a command calls it.
  Commands after `exec`, `command`, `builtin`, `nohup`, `nice`, `timeout`, `stdbuf`, and `env` are read as the
  commands they start, and `eval` of fixed words is read as part of the shell. Every command or process
  substitution runs, as Bash runs it, in a subshell before the command that holds it, and so does one in an
  unquoted here-document's body.
- **PowerShell, by its parser.** A PowerShell command is read by PowerShell's own parser, not by a second
  hand-written grammar, and each command's arguments by the parameters PowerShell's binder gives them, so a
  parameter abbreviated, aliased, or left positional, and each path of an array, is read; a command the binder
  refuses, or one given `-WhatIf`, runs nothing. `tools/worktrees-powershell.ps1` only reports what the parser and
  binder found, in the order PowerShell would run it, and `tools/worktrees.py` makes every decision. The guard
  follows `cd`, `chdir`, `sl`, and `Set-Location` (bare goes home, a leading `~` means home, `cd -` and `cd +` leave
  the location unknown, and one on a provider other than the file system leaves it unknown),
  `Push-Location` and `Pop-Location` with their stack, `$env:NAME` and strings whose only expansions are
  environment variables that are set, and a function's body where a command calls it. A location set inside a
  script block outlives the block, as it does in PowerShell. `Invoke-Expression` (`iex`) with a fixed string is read
  as part of the same session. A command that does not parse is allowed: PowerShell parses a whole script before it
  runs any of it, so none of it runs.
- **Paths that do not all run.** A command that runs only when another succeeds or fails, only in some branch, or
  any number of times in a loop or script block, is read as running, and afterwards the guard keeps only what every
  path agrees on: a directory or variable one path changes is unknown after it. A directory change whose target is
  not a directory fails and leaves the shell where it was, so it is unknown after it too, except along `&&`, which
  goes on only when it succeeded.
- **Nested shells.** `bash -c` (alone or in a bundle such as `-lc`), `bash <<< '<script>'`, a here-document handed
  to `bash`, and `pwsh -c` (or `powershell -Command`) or `pwsh -CommandWithArgs` (`-cwa`) with a fixed script are
  read in either shell, as a process of its own that starts with the variables its parent exported; its directory
  and what it exports do not outlive it. Nesting of shells, `eval`, function calls, and `Invoke-Expression` is
  followed to four deep; deeper is a command the guard cannot read.
- **When pwsh starts.** Starting `pwsh` costs far more than the guard's git calls, so the guard starts it only when
  the text names `git` and either, as a word, a refused subcommand, `fetch`, or an alias git defines where the
  command starts or in a hub in view, or command-line alias configuration; or something that computes a word
  follows `git` in its statement; or the command starts outside every repository. `git status`, `git log`, and
  every non-git command never wait for it.

#### What it refuses because it cannot settle it

A guarded hub is in view when it is the hub of the session's own directory, of its project, or of a place the
command names. While one is, the guard refuses:

- a git call that would write the checkout it runs in, where the text does not settle its repository or work tree:
  a directory or location variable named by a value the command computes or does not set, a directory that only
  some paths change, or a shell after a sourced file, which may change the directory and any variable;
- a git call whose subcommand the text does not settle, or that comes after an option of git's own the guard does
  not read;
- a command with a part it cannot read that may start git, when the command names git: Bash its grammar cannot read
  (the lines before a syntax error do run, unlike PowerShell's), a script handed to `bash`, `pwsh`, `eval`,
  `Invoke-Expression`, or `env -S` that the text does not settle or that nests more than four deep, and a PowerShell
  command when `pwsh` cannot be started or does not finish reading it within 20 seconds;
- an alias that cannot be read, as above.

The refusal names what it could not settle and asks for the directory to be written out, as `git -C <dir>` or
`cd <dir> &&`. With no guarded hub in view, the same calls are allowed with a note on stderr. A defect in the guard
itself is allowed with a note too, so that it cannot block every tool.

#### What it does not see

The hook sees the command text. It does not see:

- git run indirectly: from a script or program (`bash script.sh`, `xargs`, `find -exec`, `make`, a Python script),
  through `Start-Process`, `cmd /c`, `pwsh -File`, or `pwsh -EncodedCommand`, from a `trap`, or by a program whose
  name is computed at run time;
- shell redirection or other programs writing files;
- what the shell's own startup files set, or a variable the Bash tool's shell holds that the hook's environment does
  not, or holds with another value;
- a work tree set in a repository's own configuration, or `GIT_INDEX_FILE`, which moves only the index: the guard
  judges such a call by the checkout whose `HEAD` and tree it writes;
- a ref moved from another checkout, such as `git update-ref refs/heads/main` run in a worktree: refs live in the
  store every worktree shares, and the guard judges a call by the checkout whose `HEAD`, index, and tree it writes.

`.claude/settings.json` is tracked and reaches every developer's sessions. It may therefore name its `$schema`,
declare hooks, and set `attribution`, which turns off Claude Code's commit trailers, pull request footer, and session
links for this repository, and nothing else. A rule about what a developer must allow belongs in that developer's own
user settings or `settings.local.json`. `tests/run_validation.py` fails if any other top-level key appears, if
`$schema` names another schema, if `attribution` is anything but `{"commit": "", "pr": "", "sessionUrl": false}`, or
if a `settings.local.json` is ever committed. It also fails when the guard's hook stops matching any tool that can
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
