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
merged branch's name is not offered. It admits only pull requests whose head branch is in this repository: one from a fork or any
other repository is named as not admitted and never armed, judged, updated, or rerun, so it merges only when the
maintainer merges it. A script rather than a prompt repeats these steps, so neither session spends its context on
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
  second deploy fails at once with "Another deployment is running". Deploying records its source path in the
  manifest and in the rendered `update-coding-agent-skills` skill, so `deploy.py` refuses to deploy from a
  linked worktree, whose path disappears when the task ends. Deploy from the hub on `main`. `--dry-run` is
  allowed anywhere, and so is `--canary-home`, which deploys into a directory under the temporary directory,
  empty or one an earlier canary deployment marked, that is thrown away with the source path it records.
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
not, and with an opt-in git cannot read, and holds that it exits 0 with no output and starts no program.

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
  `export -n`, `env -i`, and `env -u`, while `read`, `printf -v`, `getopts`, `mapfile`, `readarray`, and
  `wait -p` leave each variable they set unsettled, and every variable when a word the text does not settle is among
  their options or operands; a name reference that `declare -n`, `local -n`, or `typeset -n` makes stands for the variable it names,
  so an assignment to it, its expansion, and each of those builtins reach that variable, until `unset -n` or
  `declare +n` ends it, and one a function makes stands for no variable the guard knows once the function returns;
  in PowerShell, through `$env:<name>` and `${<path>}` on the environment provider (cast,
  in parentheses, or as any target of a multiple assignment), a `foreach` loop's variable, `PSVariable.Set`,
  `[Environment]::SetEnvironmentVariable`, and the cmdlets that write the provider: `Set-Item`,
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
  `select`, `case`, `[[ ]]`, `!`, `time`, and function definitions, with redirections and array assignments,
  whose substitutions it reads and whose value it leaves unsettled. Before a command's name it reads every
  assignment Bash reads there, and places the command after them: `NAME=value`, `NAME+=value`, an element's
  `NAME[SUBSCRIPT]=value` and `NAME[SUBSCRIPT]+=value`, whose subscript it reads whole as Bash does, blanks, quotes,
  and nested brackets included, and an array's, `NAME=( )` and `NAME+=( )`, to a subscript or not. Bash refuses an
  element's assignment before a command, sets nothing for it, and runs the command all the same, and the guard reads
  it so; on its own an element's assignment makes the variable an array, whose value it leaves unsettled. No form of
  assignment Bash reads before a command stays outside what the guard sees. A redirection may stand before the
  command's name and among its assignments, as in `2>/dev/null git push` or `x=1 >out git push`, and the guard
  places that call as it places the bare one, with every redirection operator Bash has: `<`, `>`, `>>`, `>|`, `<>`,
  `&>`, `&>>`, `<<<`, a here-document, and a duplication such as `2>&1`, with or without a descriptor. As Bash does,
  it still assigns each assignment after such a redirection, but after one that follows an assignment it reads no
  subscript whole and refuses `NAME=( )`, and it reads a reserved word only as a command's first word, so in
  `2>/dev/null time git push` or `x=1 time git push` Bash runs the program `time`. A word's value comes from its
  literal text, `~`, and each `$NAME` the command sets or the environment holds, and `$PWD` is where the shell is; a
  substitution, arithmetic, a special parameter, a glob, a brace expansion, or a value that would split leaves the
  word unsettled. The guard follows `cd` (with `--`; bare goes home, `cd -` and a `CDPATH` search leave the
  directory unknown), `pushd`, `popd`, and `env -C`, whose last `-C` alone counts, read from where `env` starts, and
  runs a function's body where a command calls it.
  Commands after `exec`, `command`, `builtin`, `nohup`, `nice`, `timeout`, `stdbuf`, the program `time`, and `env`
  are read as the commands they start, a short option's value read attached, as in `nice -n5`, `stdbuf -oL`, and
  `env -C<dir>`, or as the next word; `env`'s after every word holding `=`, which it takes as an assignment whatever
  the name; and `eval` of fixed words is read as part of the shell. `tests/tools/test_prefix_programs.py` runs `env`
  and each of those programs where the system installs it, on the option forms its own `--help` lists, and holds the
  guard, form by form, to reading the command the program starts or refusing the form. Every command or process
  substitution runs, as Bash runs it, in a subshell before the command that holds it, and so does one in an unquoted
  here-document's body.
- **PowerShell, by its parser.** A PowerShell command is read by PowerShell's own parser, not by a second
  hand-written grammar, and each command's arguments by the parameters PowerShell's binder gives them, so a
  parameter abbreviated, aliased, or left positional, and each path of an array, is read; a command the binder
  refuses, or one given `-WhatIf`, runs nothing, except a call to a function the command defines: a simple function
  takes the words it declares no parameter for into `$args` and runs, so its body is read as a path that may run.
  `tools/hub_guard-powershell.ps1` only reports what the parser and
  binder found, in the order PowerShell would run it, and `tools/hub_guard.py` makes every decision. The guard
  follows `cd`, `chdir`, `sl`, and `Set-Location` (bare goes home, a leading `~` means home, `cd -` and `cd +` leave
  the location unknown, and one on a provider other than the file system leaves it unknown),
  `Push-Location` and `Pop-Location` with their stack, `$env:NAME` and strings whose only expansions are
  environment variables, and a function's body where a command calls it. A variable's value is the one the session
  holds at that point, as in Bash: the one the hook was given, or the word a plain assignment in the command gives it
  (`$env:NAME = <word>` or `${env:NAME} = <word>`, on any path of the provider), while an empty value or `$null`
  removes it. A compound, cast, parenthesized, or multiple assignment, a `foreach` loop's variable, `PSVariable.Set`,
  `[Environment]::SetEnvironmentVariable`, and a cmdlet that writes the provider leave its value unsettled, and a
  variable the session does not hold is unsettled too. A cmdlet that writes, removes, or renames a variable at a
  path or name the text does not settle may reach any variable, so every one is unsettled after it, as after Bash's
  `export "$v"=x` or `unset "$v"`; a path whose last part is literal text, as in `"$dir\out.txt"`, can name only the
  variable that part names, so it is passed over when git does not read that one. A command after the programs
  `env`, `nohup`, `nice`, `timeout`, `stdbuf`, and `time` is read as in Bash, as the command it starts. A location
  set inside a script block outlives the block, as it does in PowerShell. `Invoke-Expression` (`iex`) with a fixed
  string is read as part of the same session. A command that does not parse is allowed: PowerShell parses a whole
  script before it runs any of it, so none of it runs.
- **Paths that do not all run.** A command that runs only when another succeeds or fails, only in some branch, or
  any number of times in a loop or script block, is read as running, and afterwards the guard keeps only what every
  path agrees on: a directory or variable one path changes is unknown after it. A directory change whose target is
  not a directory fails and leaves the shell where it was, so it is unknown after it too, except along `&&`, which
  goes on only when it succeeded. The guard reads a loop's or script block's body twice, so it follows loops and
  script blocks, counted together across nested shells and function calls, to eight deep; deeper is a command the
  guard cannot read, and the directory and every variable are unknown after it.
- **Nested shells.** `bash -c` (alone or in a bundle such as `-lc`), `bash <<< '<script>'`, a here-document handed
  to `bash`, and `pwsh -c` (or `powershell -Command`) or `pwsh -CommandWithArgs` (`-cwa`) with a fixed script are
  read in either shell, by every spelling pwsh takes for a parameter: a leading `-`, en dash, em dash, or
  horizontal bar, that dash doubled, as in `--command`, white space around the word, and a leading `/`, as in
  `/Command`, on every platform. `tests/tools/test_hub_guard_shells.py` runs pwsh on these spellings and Bash on
  the builtins above that set a variable, and holds the guard to what each shell does. `bash` runs as its `-c`
  script the first word after all of its options and a `--`, as in `bash -c -e '<script>'`, and Windows PowerShell
  runs a leading word that names no parameter as `-Command`, as in `powershell '<script>'`, where `pwsh` runs it as a
  script file. Each nested shell is read as a process of its own that starts with the variables its parent exported,
  in its parent's directory or the one `pwsh -WorkingDirectory` (`-wd`) names, read from there; its directory and
  what it exports do not outlive it. Nesting of shells, `eval`, function calls, and `Invoke-Expression` is followed to four deep; deeper is
  a command the guard cannot read.
- **When pwsh starts.** Starting `pwsh` costs far more than the guard's git calls, so the guard starts it only when
  the text names `git` and either, as a word, a refused subcommand, `fetch`, or an alias git defines where the
  command starts or in a hub in view, or command-line alias configuration, each also read with the quotes, backticks,
  and backslashes that join a word taken out, so `com'mit'` names `commit`; or something that computes a word
  follows `git` in its statement; or the command starts outside every repository. `git status`, `git log`, and
  every non-git command never wait for it.

#### What it refuses because it cannot settle it

A guarded hub is in view when it is the hub of the session's own directory, of its project, or of a place the
command names. While one is, the guard refuses:

- a git call that would write the checkout it runs in, where the text does not settle its repository or work tree:
  a directory or location variable named by a value the command computes or does not set, or set by a builtin that
  leaves it unsettled or through a name reference the text does not settle, which may reach any variable, a
  directory that only some paths change, or a shell after a sourced file, which may change the directory and any
  variable;
- a git call whose subcommand the text does not settle, or that comes after an option of git's own the guard does
  not read;
- a command with a part it cannot read that may start git, when the command names git: Bash its grammar cannot read
  (the lines before a syntax error do run, unlike PowerShell's), Bash nested deeper than its reader follows, loops
  and script blocks nested more than eight deep, a script handed to `bash`, `pwsh`, `eval`, or `Invoke-Expression`
  that the text does not settle or that nests more than four deep, a script `bash` or `sh` reads from its standard input when that is a pipe or the input the command
  inherits (`... | bash`, `bash -s`), a script `pwsh` reads from its standard input (`-Command -`, `-File -`, a
  lone `-` before any of them, a word after `/` that names no parameter, such as an absolute path, or no inline
  command, script file, or encoded command at all), a callback `mapfile -C` runs, every `env -S`, whatever follows
  it, an option of `env` or of a prefix program above that the guard does not read, a PowerShell command when `pwsh`
  cannot be started or does not finish reading it in the time the guard has left, a command the guard does not
  finish reading and judging within 15 seconds, `pwsh` included, and the lookups of the hubs the places it names
  reach, each git call of theirs given no more than the time left, which keeps it inside the hook's own 30-second
  timeout, past which Claude Code lets the command run, and a command either shell's reader fails on with an error
  of its own;
- a shell alias the command defines, by Bash's `alias` with a value, or by PowerShell's `Set-Alias`, `New-Alias`,
  `Import-Alias`, or an item cmdlet writing the `alias:` drive at a path the text names, and a script block
  PowerShell builds from a string (`[scriptblock]::Create`, `NewScriptBlock`, `InvokeScript`), when the command names
  git: a later word may then run git under another name, or text the parser never read as a command;
- an alias that cannot be read, as above.

The refusal names what it could not settle and asks for the directory to be written out, as `git -C <dir>` or
`cd <dir> &&`. With no guarded hub in view, the same calls are allowed with a note on stderr. A defect in the guard
itself, once it has found a guarded hub in view, is refused the same way when the command names git, since a failure
says nothing about where the call acts; a defect before that, such as an event it cannot decode, an event that names
no command or file path, or a hub it cannot look up, an opt-in git cannot read among them, is allowed with a note on
stderr, so that it cannot block every tool and is never passed unseen. A directory below a checkout, which a `.git`
in it or above it marks, where git answers with an error is one such hub: it is noted and passed over, never read as
a place outside every repository, and the other hubs in view still count. A file an edit names in such a checkout, or
in a git directory git answers with an error for, may lie in a guarded hub, so the edit is refused; one where git did
not run or finish is allowed with a note. The hook command reads the project's own
opt-in before it starts the guard, and when git cannot read it, stops there with a note on stderr the same way.

#### What it does not see

The hook sees the command text. It does not see:

- git run indirectly: from a script or program (`bash script.sh`, `bash < script.sh`, `xargs`, `find -exec`, `make`,
  a Python script), through `Start-Process`, `cmd /c`, `pwsh -File`, or `pwsh -EncodedCommand`, from a `trap`, or by
  a program whose name is computed at run time;
- an alias the `alias:` drive gains at a path the text does not settle, or one the shell's startup files define;
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
