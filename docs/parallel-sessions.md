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
The kind is a pull request title type, which the title check then holds the branch's pull request to, or `inv`;
`new` refuses any other kind and names the ones it takes. Pull `main` in the hub first if it may be behind. `python tools/worktrees.py list` shows one row per worktree:

| Column | Meaning |
|---|---|
| state | `hub`, `in progress`, `no commits`, `missing`, `unreadable` when git cannot read the checkout, or `unknown` when git cannot count its commits against `main` |
| ahead/behind | Relative to `main`; `?` when git cannot count them. |
| dirty | Number of paths with uncommitted changes, untracked files included and a rename counted once. |

The survey reads each checkout as `repo-cleanup` does, through ejryan89-skill-core's `git_worktrees`, and never writes a
checkout's index, so it takes no lock a session committing there could meet. The hub row reads `off main` or `dirty`
when the hub has drifted. Merged state is not reported: squash merges leave every branch looking unmerged, so a
branch's pull request is the only reliable record.

## Retiring a worktree

After a task's pull request merges, run the `repo-cleanup` skill on the hub (by repository name, or by the hub's
absolute path). In one pass it does the following:

- **Updates the hub.** It fetches with prune and fast-forwards `main`.
- **Removes finished worktrees.** A clean worktree is removed when its branch's pull request merged, whether or
  not its remote branch still exists; when its remote branch is gone and it has no pull request; or when it was
  pushed under its own name, has no pull request, and `main` holds both its tip and its remote branch's. A merged
  pull request counts at the branch's exact local tip, at one of its commits followed only by "Update branch"
  merges, or when its merge commit made the branch's whole change. A clean worktree whose pull request closed
  without a merge is removed too, but its branch is kept and asked about.
- **Deletes their branches, squash merges included.** `git branch -d` refuses a squash-merged branch, so
  `repo-cleanup` uses `-D` for a branch whose pull request merged by one of the proofs above, and when `-d` refuses
  only because `HEAD` lacks a branch `main` holds. A branch not checked out in a worktree is deleted by the same
  rules.
- **Removes leftover folders.** Empty parent folders of a removed tree go, but `.claude` itself always stays,
  because it holds `settings.json`.

It offers, and acts only when you confirm:

- a clean worktree whose branch has no remote and no pull request, since it may hold a task just started;
- a clean detached worktree whose commit is on `main` or a remote branch;
- a folder under `.claude/worktrees` that Git no longer tracks as a worktree;
- a branch whose pull request closed without a merge, and other unmerged branches.

Anything else is kept and reported:

- trees with uncommitted changes, an operation such as a rebase or bisect in progress, refs only that tree sees,
  edits hidden from `git status`, or ignored files other than regenerable build output such as `__pycache__` or a
  project's `bin` and `obj`, which is deleted with the tree;
- open pull requests;
- branches whose pull request status is unmatched or could not be read.

So a tree another session is still using is kept, or only offered, while its branch is unpushed or has an open pull
request. One whose branch was pushed before it had a commit of its own, with no pull request yet, does not: `main`
holds both its tips, so it is removed and its branch deleted. `tools/worktrees.py new` creates the branch without
an upstream, so this needs a push before the first commit. [`repo-cleanup`](skills.md#repo-cleanup) in the skill
reference states every rule in full.

Run it from a session that is **not** inside the tree being retired. Windows will not delete a folder that is
some process's working directory. Git then removes the files and the worktree record but fails on the folder
itself. `repo-cleanup` keeps the branch and says so, and offers the folder as a leftover to delete once nothing
holds it.

To retire a tree by hand instead, run these from the hub:

```bash
git worktree remove '.claude/worktrees/<kind>-<name>'
git branch -D '<kind>/<name>'
```

## An orchestrator and a shepherd

When many tasks run at once, one session orchestrates: it plans the work, starts a session per task in a worktree of
its own, and starts the next task as each pull request merges. A second session, in the hub, shepherds the pull
requests on a repeating prompt that runs the
[`shepherd-pull-requests`](../.claude/skills/shepherd-pull-requests/SKILL.md) repository skill. Its tested script does
one tick: it fast-forwards the hub, arms squash auto-merge, and merges `main` into one armed behind branch whose check
is green or pending, never by a rebase. It updates none while an armed pull request that is current with `main` and
green or pending merges next, and otherwise the one furthest along in `Validation`, the oldest among equals, leaving
the other behind branches alone, since an update cancels the run in flight on the branch it updates; it holds every
update but that pull request's own while one that changes the validation policies or the runner's selection is open.
It checks that a red run tested the pull request's
current head before judging it, reruns a failure of the runner or a service once per head, and names every other
failure for the session that owns the branch. Each worktree whose pull request merged is named for `repo-cleanup`,
run from outside it, judged by the commit it has checked out and never by its branch's name, so a later task on a
merged branch's name is not offered. It admits only pull requests whose head branch is in this repository and that
would merge into `main`: one from a fork or any other repository, or onto another branch, is named as not admitted
and never armed, judged, updated, or rerun, so it merges only when the maintainer merges it. A script rather than a prompt repeats these steps, so neither session spends its context on
them, and the orchestrator keeps its own for the decisions.

Each tick also checks the pre-merge review, which is report-only. For each admitted pull request in step, not in
conflict, and with a check neither red nor missing, the script reads the machine's code-review archive for a
`review-repository` review of its head. A head is covered by a review of that head, or of an earlier head from which
every commit since is a merge, such as a merge of `main`. The script prints the `review-prs` arguments that review
every head no review covers, and the shepherd runs `review-prs` once with them. The review it records covers that
head from the next tick on, so a tick starts a review only for a head none covers, and never holds, disarms, or delays
a merge for one. A covered head's line names the record's verdict and report, which the shepherd gives the
orchestrating session and the session that owns the branch. It posts nothing on the pull request: a public text
passes a private-term scan the skill cannot run, so the orchestrating session relays the findings and posts any public
summary itself after that scan.
[Pull requests](implementing-changes.md#pull-requests) states what the author answers and how a kind of finding is
promoted to blocking.

Hold a pull request from merging when it comes to owe a gate that calls a model, such as an `evaluate-skill`
citation, that it does not yet cite. Its owner or the orchestrator adds a line of its own to the pull request's body,
`Shepherd-hold: <reason>`, with `gh pr edit <number> --body-file '<file>'`. The next tick turns off its auto-merge and
arms it no more, names it with the reason on a `HELD` line, and still judges and reruns its checks, while it arms and
updates every other pull request as before. Release it by deleting the line once the gate is cited; the next tick arms
it again.

## What is shared even across worktrees

Worktrees isolate files. These are still shared:

- **The stash stack and every ref.** Never pop or drop a stash you did not create.
  - Prefer a temporary WIP commit.
  - If you must stash, push with a unique message, then apply your entry by SHA.
- **Deployer state under `~/.claude`.** The deploy lock is keyed by the home folder, so it spans worktrees: a
  second deploy, or a `--dry-run`, fails at once with "Another deployment is running". Deploying records its
  source path in the manifest and in the rendered `update-coding-agent-skills` skill, so `deploy.py` refuses to
  deploy from a linked worktree, whose path disappears when the task ends. Deploy from the hub on `main`. A
  `--dry-run` refuses any checkout other than the one the manifest records, as the deployment does, so once the hub
  has deployed into the home it refuses in a linked worktree too. `--canary-home` works anywhere: it deploys into a
  directory under the temporary directory, empty or one an earlier canary deployment marked, that is thrown away
  with the source path it records.
- **Nothing in validation.** Every suite isolates its own temporary home and fixtures, and the suites that
  deploy this repository deploy a temporary copy of it, or into a `--canary-home`. Any number of worktrees can run
  `tests/run_validation.py` at once.

## The guard

`.claude/settings.json` declares a Claude Code `PreToolUse` hook that runs `tools/hub_guard.py`. A hook in that
file can only deny an action; it never grants one. `tools/worktrees.py guard`, the hook's command before the guard
had a module of its own, runs the same guard, since a Claude Code session keeps the hook command it started with.

The guard is **inert unless a clone opts in**:

```bash
git config coding-agent-skills.hubGuard true
```

The setting lives in the shared `.git/config`, so one command covers the hub and all of its worktrees. Because
the setting is not tracked, someone who clones the repository and works in a single checkout is never blocked.

The hook command reads the setting itself, in Bash, and exits before starting Python when the clone has not opted
in: when git reports the setting unset or false, it exits in silence, and when git cannot read it at all, as with a
value that is no boolean or a configuration file git cannot parse, it lets git's own message through and adds a note
on stderr that names git's exit status, so an opted-in clone left unguarded is never passed unseen. Elsewhere the hook
is inert and costs one `awk`, one `env`, and one `git config` call per tool call: `python`
never runs, so a `python` that still resolves to the Microsoft Store alias cannot fail the hook. In an opted-in clone
it runs `tools/hub_guard.py` under `$CLAUDE_PROJECT_DIR`, which checks the same setting again. Neither read can be
pointed at another repository's setting or outranked: each reads only the repository's own file, with
`git config --local`, which passes over the command-line configuration `GIT_CONFIG_PARAMETERS` and `GIT_CONFIG_COUNT`
carry; the hook command runs its call through `env -u` for each of `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, and
`GIT_CONFIG` it holds, in any case, since Windows reads an environment name in any case; and `tools/hub_guard.py` runs
its own git calls, and the `pwsh` that reads a PowerShell command, without any of git's repository-selecting
variables, the set ejryan89-skill-core's `bounded_process.py` names, through its `run_bounded`, which ends every
process `pwsh` starts with it. `tests/tools/test_hub_guard.py` runs the tracked command both ways.

The command is also written to end at once under PowerShell, for the runtime that runs it there (see
[Other runtimes](#other-runtimes)). Its first two lines are `set \` and `exit 0`: Bash, like any POSIX shell, joins
them into `set exit 0`, which only sets the positional parameters, and goes on, while PowerShell reads `set \` as
setting a variable named `\` and then exits with status 0. PowerShell parses a whole command before it runs any of
it, so the rest is written in what both shells parse, `test` rather than `[`, `env -u` rather than a loop of
`unset`, and `awk` printing to `/dev/stderr` rather than `>&2`, which PowerShell refuses to parse.
`HookCommandUnderPowerShellTests` in `tests/tools/test_hub_guard.py` runs the tracked command under `pwsh`, opted in,
not, and with an opt-in git cannot read, and holds that it exits 0 with no output and starts no program. The hook
declares `onFailure: "block"`, which [The guard's contract](#the-guards-contract) explains.

With the guard enabled:

- **File tools.** `Edit`, `Write`, or `NotebookEdit` is refused when its target lies in the hub's
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
  - the interactive tools `citool`, `difftool`, `gui`, and `mergetool`
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
  `GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT`), then with `git config --get alias.<name>` in the hub for a call that
  acts on the hub, or where the call acts for one outside every hub (a worktree shares its hub's configuration, so a
  hub's alias is read from a worktree too), and judges what it runs, through further aliases. A shell alias (`!...`)
  is read as Bash from the top of the work tree it runs in. An alias that cannot be read, nests more than four deep,
  or runs something the guard cannot judge is refused while a guarded hub is in view, and one that writes a hub is
  refused wherever it is called.
- **Both shell tools.** The same commands are refused, with the same allowances, whether they run through the
  `Bash` tool or the `PowerShell` tool, or as a `Monitor` tool's command, which runs in the `Bash` tool's shell.

### Other runtimes

The guard is Claude Code's. For Codex and Copilot CLI the `CLAUDE.md` rules are the guard:

- **Codex** runs hooks of its own but not this one, which lives in Claude Code's settings.
- **Copilot CLI** reads `.claude/settings.json` as a source of repository hooks, but only in a folder it trusts
  (`--trust-folder`, or one the user trusted); in any other folder it runs no repository hook. In a trusted folder it
  runs this hook, matcher included, with a Claude-format event that names its shell tool `Bash` whichever shell that
  tool runs, and with `$CLAUDE_PROJECT_DIR` set. On Windows it runs the command under PowerShell, where the command
  exits with status 0 before it starts anything, so the guard neither refuses nor allows any of Copilot's calls and
  Copilot runs them as it would without the hook. A command that errored there would be worse than none:
  Copilot treats a `preToolUse` hook that fails as a denial, so it would refuse every shell call. On Linux, which this
  repository does not claim yet, Copilot runs the command under Bash, so in an opted-in clone the guard reads
  Copilot's shell calls as Bash commands, which Copilot's Bash tool runs there; that path is not checked.

### The guard's contract

The hook is given the text of a command, not what runs under it. The guard fails closed: it reads a command only as
far as it understands it, and every decision it makes is one of three. What it understands is judged; a part it does
not understand is refused while a guarded hub is in view; and what no shell's text shows is named below. What it
understands is a set of tables in `tools/hub_guard.py`, each a catalogue a shell publishes, with every entry either
understood or in `NOT_READ` with the reason it is not read. The guard's suites take each catalogue from the shell
itself, `compgen -b`, `compgen -k`, `set -o`, `shopt`, and `compgen -v` from Bash and the AST node types by
reflection from PowerShell, and fail on an entry neither table holds, so an entry a later shell adds is refused until
a row decides it.

#### What it understands

- **Where a git call acts.** git writes the `HEAD` and index of its repository and the files of its work tree, so a
  call acts on the hub when either one is the hub's. The repository is the one `--git-dir` or `GIT_DIR` names, or
  else the one git finds from where it starts, which inside a git directory, such as `-C <hub>/.git`, is that
  directory's own repository; the work tree is the one `--work-tree`, `GIT_WORK_TREE`, or a `core.worktree` setting
  names, or else where git starts, after each `-C`. A relative path is read from where git finally starts, and a
  relative `core.worktree` from the repository it is set for. `GIT_COMMON_DIR` names only the store of refs and
  objects every worktree shares, so it does not change which checkout a call writes. The guard reads git's own
  options as git does, with no abbreviation and with a value attached after `=` or as the next word (`-C`, `-c`,
  `--attr-source`, `--config-env`, `--git-dir`, `--namespace`, `--shallow-file`, `--super-prefix`, `--work-tree`),
  and `core.worktree` and `alias.<name>` among the command-line configuration a call sees: `GIT_CONFIG_PARAMETERS`,
  `GIT_CONFIG_COUNT` with its pairs, then each `-c` and `--config-env`. The location and configuration variables
  reach a call from the environment Claude Code gives the hook; the command itself sets none of them, since every
  form that would, an assignment before git, `export`, `env`, and a PowerShell assignment among them, is a part the
  guard does not read. On Windows git reads an environment name in any case, so `git_dir` is `GIT_DIR` there; when
  two spellings disagree, the call is judged by the upper-case one, the spelling git reads, with a note.
- **Bash, by its grammar.** `tools/bash_syntax.py` reads Bash as Bash parses it: single and double quotes, `$'...'`,
  escapes and line continuations, comments, and here-documents, and the command tree of lists, `&&` and `||`,
  pipelines, `( )` subshells, `{ }` groups, `if`, `case`, `while`, `until`, `for`, `select`, `!`, and `time`. The
  guard understands a simple command whose name is literal once quotes are removed and is a builtin
  `BUILTINS_UNDERSTOOD` names or a program, wherever the tree holds it, as the runner rule below judges it, and reads
  each of its words as literal text, `~`, or an expansion `EXPANSIONS_UNDERSTOOD` names, which leaves its word one
  the text does not settle. Such a word may stand among a program's arguments and among a git call's words after its
  subcommand, where a refused subcommand given one is refused in the hub, since it may be any option. Each command
  or process substitution is read as the commands it runs, and so is one in an unquoted here-document's body. A
  quoted here-document's body, as `<<'EOF'` gives it, and a here-string's quoted word do not expand: each is data
  handed to the command's input, read as nothing it runs.
- **Directories.** `cd`, `pushd`, and `popd` to a literal directory move the shell, and each git call after them is
  placed there: along `&&` where the change succeeded, along `||` where it failed, and after `;` where the target is
  a directory now, or else nowhere the text settles, since an earlier command may make it one. A pipeline's commands
  and a background job run in subshells of their own, so a change there moves nothing after them. A target the text
  does not settle, `cd -`, a `CDPATH` search, and `pushd`'s rotations leave the directory unknown, and a compound
  command that holds a change leaves every git call in it and after it in a directory the text does not settle.
- **Assignments.** A bare assignment is understood to a free name: one `BASH_VARIABLES` gives no reason, git does not
  read, and the environment does not hold. Before a command, an assignment is understood to a name git does not read
  and `BASH_VARIABLES` gives no reason, and before git only to a name `GIT_PREFIXES` lists, with a value it allows,
  such as `GIT_EDITOR=true`, or to such a name of Bash's own, such as `LC_ALL`.
- **Shell options.** `set` and `shopt` are understood for the options `SET_OPTIONS` and `SHOPT_OPTIONS` decide change
  nothing the guard reads, such as `errexit`, `pipefail`, `nounset`, and `nullglob`, and so is a shell the
  environment starts under them, through `SHELLOPTS` and `BASHOPTS`. Each other option, `keyword`, `posix`, `lastpipe`,
  and `cdable_vars` among them, and one neither table names, is refused, and so is a shell the environment hands
  `POSIXLY_CORRECT` or `BASH_COMPAT`, `xtrace` where the environment's `PS4` holds an expansion, whatever turns it
  on, `set`, `shopt -o`, or the environment's `SHELLOPTS`, and a command named
  after a function the environment hands the shell as `BASH_FUNC_<name>%%`.
- **gh.** `gh pr checkout`, `gh pr co`, and `gh repo sync` check a branch out in the checkout gh runs in, and are
  judged as a checkout git makes there, a runner's command among them, from Bash and PowerShell alike.
- **Redirections.** A redirection that writes a file at a path the text settles is refused when the file lies in a
  guarded hub's tracked files or anywhere in its git directory, as a file tool's edit is; a duplication such as
  `2>&1` and a device such as `/dev/null` write none. In PowerShell every command's file redirection is read so,
  an expression standing as a command, as `'x' > <file>`, included, and one the reader does not reach is refused.
  A redirection that names its descriptor by a variable, as
  `{fd}>out`, `{fd}<in`, or `{fd}>&-`, assigns that variable, any one, and is refused in every operator. A target the text does not settle to a literal path, or a
  relative one in a directory it does not settle, may lie in a hub and is refused, but for one under the system's
  temporary folder that `TMP`, `TEMP`, or `TMPDIR` names from the environment, as `> "$TMP/out.txt"` does.
- **Programs.** A command may start any program a literal name names. The programs that run another command their
  words name are `RUNNERS`: `bash`, `busybox`, `chroot`, `chronic`, `chrt`, `cmd`, `csh`, `dash`, `doas`, `env`,
  `fish`, `flock`, `ionice`, `ksh`, `ltrace`, `mksh`, `nice`, `nohup`, `parallel`, `powershell`, `pwsh`, `runuser`,
  `script`, `setsid`, `sh`, `ssh`, `stdbuf`, `strace`, `su`, `sudo`, `taskset`, `tcsh`, `time`, `timeout`,
  `unbuffer`, `watch`, `wsl`, `xargs`, and `zsh`. While a hub is guarded, a runner whose command is or may be git is
  refused, and a runner around a command that is not git is understood. Its command may be git where one of its words
  names git, by its name or a path, or names another runner, which may run git in turn, or is a word the text does
  not settle, which may be either. A runner's own options are read by an allow-list, `RUNNER_OPTIONS`, which names
  for each of `chronic`, `chroot`, `chrt`, `doas`, `env`, `ionice`, `ltrace`, `nice`, `nohup`, `setsid`, `stdbuf`,
  `strace`, `sudo`, `taskset`, `time`, `timeout`, `unbuffer`, and `xargs` the options known to be safe: ones that
  neither build its command from text, as `xargs`'s `-I` and `env`'s `-S` do, nor change where it runs, as `env`'s
  `-C` does, nor write a file or run another program. Every other option word of a runner is refused, in every
  spelling getopt reads: exactly, with its value attached by `=` or directly, abbreviated, or as a letter in a
  bundle, as `-0IX` is. A long option is understood only spelled in full, since an abbreviation of a safe option may
  be one of another option too. `env`'s `NAME=VALUE` words are read among its options as assignments for its
  command, a value-taking option's value never ending them, and judged as ones before it, from Bash and PowerShell
  alike; such a word before any other runner's command, as `sudo`
  takes, is refused, since it may set a variable the guard does not judge. `bash -c` with a literal script is read as
  Bash, and `pwsh -Command` or
  `-c` with one literal script, after `-NoProfile`, `-NonInteractive`, or `-NoLogo`, as PowerShell, each refused
  where the script runs git and in every other form, a script file among them. Some are refused whatever they run,
  `RUNNERS_ALWAYS_REFUSED`: `busybox`, `cmd`, `csh`, `dash`, `fish`, `flock`, `ksh`, `mksh`, `parallel`,
  `powershell`, `runuser`, `script`, `sh`, `ssh`, `su`, `tcsh`, `watch`, `wsl`, and `zsh`. Of these, the shells are
  ones whose scripts the guard does not read, `sh` among them since it runs Bash in POSIX mode, or dash, either of
  which keeps an assignment made before a special builtin after it, and the rest run a command line handed to them as
  one string. The assignments before a runner, and `env`'s own, are judged as ones before a command that is not
  git. The programs that have options which run a command, write a file the command does not name, or change where
  the program acts, `PROGRAM_OPTIONS`: `find`, `rg`, `sort`, and `tar`, are read by an allow-list the same way, from
  Bash and PowerShell alike, its safe options ones that do none of these, and so is refused any word of theirs that
  the text does not settle. `tar`'s extraction, which writes the files an archive names, and its `-C`, which chooses
  where, are refused, and so is an archive it names `host:path`, which it opens through a remote shell, unless
  `--force-local`, given as an option rather than as another option's value or after `--`, makes it a file. `tar`'s
  table holds only letters GNU tar and bsdtar, Windows' `tar.exe`, read alike, so `-H`, `-W`, and `-s`, which one of
  them reads with a value and the other without, are refused. A runner's command
  that is one of them is held to its allow-list too, and after `xargs`, which adds words from its input, it is refused
  unless its words hold a `--` that ends its options, which `find` has none of. A variable one of them reads options
  from, `PROGRAM_VARIABLES`: `RIPGREP_CONFIG_PATH`, `TAPE`, and `TAR_OPTIONS`, is refused where it is assigned
  before the program or a runner, where `env` sets it, and where the environment holds it. Among the options
  refused are `find`'s `-exec`, `-execdir`, `-ok`, `-okdir`, `-delete`, and `-fprint` family, `rg`'s `--pre` and
  `--hostname-bin`, `sort`'s `--compress-program` and `--output`, and `tar`'s options that run a program.
  `tests/tools/test_prefix_programs.py` holds each allow-list to every option the program's `--help` or getopt table
  lists, pinned in `tests/fixtures/hub-guard/option-catalogues.json`, in each spelling, and holds that fixture to the
  `--help` of each of `env`, `nice`, `nohup`, `stdbuf`, `timeout`, and `xargs` the system installs. git keeps its
  own subcommands' judgment.
- **PowerShell.** A PowerShell command is read by PowerShell's own parser, not by a second hand-written grammar,
  and is understood when every node type in its tree is one `POWERSHELL_UNDERSTOOD` names, in the forms that invoke
  nothing: pipelines of commands joined by `&&` and `||`, with redirections, whose elements are constants, strings,
  lists, parameters, and the values variables, value expressions, and properties compute, which leave their word one
  the text does not settle, as Bash's expansions do; `if` and `foreach`, whose conditions and bodies are read; a cast
  to a type `POWERSHELL_CASTS` names, such as `[int]` or `[string]`; and a script block handed to `ForEach-Object`,
  `Where-Object`, `Sort-Object`, `Select-Object`, or `Group-Object`. Each command inside an expression or a body is
  read as a command that runs within the statement holding it. An assignment, and a `foreach`'s variable, is
  understood to a plain variable PowerShell neither sets nor reads itself (`POWERSHELL_VARIABLES`, held to what
  `Get-Variable` lists in a pwsh started with no profile), and not to one on a drive, as `$env:GIT_DIR` is. A method
  call, a static member such as `[IO.File]::Delete`, a member the text does not name, a cast to another type, a
  script block handed to any other command, and `ForEach-Object` given a member's name are not read. A command is
  understood when it is a program, judged by the runner rule above, `bash` and `pwsh` refused whatever they
  run since the guard reads their scripts only from Bash, but for a name PowerShell keeps for its own commands, an
  alias, a function of its own, or a verb-noun name (held to `Get-Alias`, `Get-Command`, and `Get-Verb`), which is understood only for one of these cmdlets, by
  its name or an alias PowerShell gives it: `Set-Location`, `Push-Location`, `Pop-Location`,
  `Get-ChildItem`, `Get-Content`, `Select-String`, `Select-Object`, `Sort-Object`, `Measure-Object`, `Write-Output`,
  `ForEach-Object`, `Where-Object`, `Group-Object`,
  `Write-Host`, `Out-String`, `Out-Null`, `Out-File`, `Get-Location`, `Get-Item`, `Get-Command`, `Get-Process`,
  `Format-Table`, `Format-List`, `Start-Sleep`, `Remove-Item`, `Copy-Item`, `Move-Item`, `New-Item`, `Set-Content`,
  `Add-Content`, `Resolve-Path`, `Test-Path`, `Join-Path`, `Split-Path`, `Get-Date`, `Get-FileHash`, `ConvertFrom-Json`,
  `ConvertTo-Json`, and the function `mkdir`. Every other node type and command is a part the guard does not read. The location cmdlets are
  followed as Bash's `cd`, `pushd`, and `popd` are, to a literal file-system path given by position, `-Path`, or
  `-LiteralPath`, a leading `~` read as home; a path on another provider, as `env:` or `alias:`, given by position or
  attached to a parameter by a colon, as `-Path:env:GIT_DIR`, is not read, nor is a path a cmdlet that writes, such as
  `Set-Content`, is given that the text does not settle, and a
  location a pipeline of several commands sets is unknown after it, since each of them runs in the session.
  `tools/hub_guard-powershell.ps1` only reports what the parser found, and `tools/hub_guard.py` makes every decision.
  A command that does not parse is allowed: PowerShell parses a whole script before it runs any of it, so none of it
  runs. pwsh starts for every PowerShell tool call and for a Bash command that hands it a literal script; no other
  Bash command waits for it.

#### What it refuses

A guarded hub is in view when it is the hub of the session's own directory, of its project, or of a place a git call
the command makes names, or the one whose git directory holds that directory or place. While one is, the guard
refuses:

- a part of the command it does not understand, whatever else the command holds and whether or not it names git:
  each entry of `NOT_READ`, among them a nested shell, `eval`, `source` and `.`, `exec`, `alias`, a function
  definition, `export` and the other declarations, `trap`, `hash`, `enable`, arithmetic of every kind, a subscript,
  an indirection, Bash 5.3's `${ command; }`, a command name the text does not settle, a runner whose command is or
  may be git, one `RUNNERS_ALWAYS_REFUSED` names, and an option of a runner or of a program `PROGRAM_OPTIONS` names
  that its allow-list does not know to be safe, a redirection to a target the text does not settle, an
  assignment the rules above do not understand, an option the tables refuse, Bash its grammar cannot read (the lines
  before a syntax error do run, unlike PowerShell's), substitutions nested more than four deep, and in PowerShell
  every node type outside `POWERSHELL_UNDERSTOOD` and each form above that invokes something, method calls, static
  members, `Invoke-Expression`, `&` with a name the text does not settle, and every cmdlet the list above does not
  name among them, an assignment to the environment or to PowerShell's own variables, a dot-sourced command, and a
  path on a provider other than the file system;
- a git call that would write the checkout it runs in where the text does not settle its directory, a git call whose
  subcommand the text does not settle, wherever it runs, since it may be an alias that writes a hub, a git call after
  an option of git's own the guard does not read, and a refused subcommand given a word the text does not settle;
- an alias that cannot be read, nests more than four deep, or runs a part the guard does not read;
- a command the guard does not finish reading and judging within 15 seconds, `pwsh` included, and the lookups of the
  hubs the places it names reach, each git call of theirs given no more than the time left; a PowerShell command when
  `pwsh` cannot be found or started; and a command either reader fails on with an error of its own.

The refusal names the first part it did not understand, or what it could not place, and asks for the command to be
written as plain commands, or for the directory to be written out, as `git -C <dir>` or `cd <dir> &&`. With no guarded
hub in view, the same is allowed with a note on stderr, unless the command holds a part the guard does not read and
a path one of its literal words names, as `export GIT_DIR=<hub>/.git` or a string `eval` is handed does, lies in a
guarded hub: that hub is then in view. A place whose lookup the reading time cuts short, between git calls or inside
one, or whose lookup git answers with an error for, may be a guarded hub's, so the guard then refuses the command as
if one were in view; it looks up every other place first, so the hubs they reach stay in view. A defect in the guard
itself, once it has found a guarded hub in view, is refused the same way, since a failure says nothing about where a
call acts; a defect before that, such as an event it cannot decode, an event that names no command or file path, or a
hub of the session's own directory or project it cannot look up, an opt-in git cannot read among them, is allowed
with a note on stderr, so that it cannot block every tool and is never passed unseen. A reading the time cuts short
before the lookups of the places it names is such a failure: refused while a hub of the session's own directory or
project is in view, and otherwise allowed with a note. A directory of the session's below a checkout, which a `.git`
in it or above it marks, where git answers with an error is one such hub: it is noted and passed over, never read as
a place outside every repository, and the other hubs in view still count; a git call that starts there is a place
git answers with an error for, refused as above. A successful answer that is not the lines the guard asked for is
read as such an error. A file an edit names in such a checkout, or in a git directory git answers with an error for,
may lie in a guarded hub, so the edit is refused; one where git did not run or finish is allowed with a note.

The hook declares `onFailure: "block"`, so Claude Code blocks a command when the hook times out, cannot start, or
exits with an error, rather than letting it run unseen. The hook command reads the project's own opt-in before it
starts the guard and exits 0 on every path where the clone has not opted in, git's answer that the setting is unset
or false and a setting git cannot read among them, so a block can fall only where the clone is guarded, and there it
is the refusal a failure is owed. One case blocks a clone that has not opted in: a `git config` that does not answer
within the hook's own timeout, which leaves the hook timed out before it can exit.

#### What it does not see

The hook sees the command text. It does not see:

- inside a program: what a program does from inside itself, in its own language, as a Python script with any
  script name, in the hub or in a worktree, an awk or sed program, a test suite, a script file, `make`'s recipes, or
  `npm`'s scripts do, and an agent runtime it starts, which runs git under its own guards; the guard reads only the
  command line, and refuses only the runners above where the command line they are given is or may be git;
- a file a program writes;
- what the shell's own startup files set: an alias, a function, or an option they define, or a variable the Bash
  tool's shell holds that the hook's environment does not, or holds with another value;
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
edit a file or run git (`Edit`, `Write`, `NotebookEdit`, `Bash`, `PowerShell`, and `Monitor`, whose
command runs in the shell the `Bash` tool uses and which the guard reads as Bash). A tool missing from the matcher is
never shown to the guard, so the hub would be open through it. It fails too when the matcher names anything else,
such as a tool Claude Code no longer has.

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
