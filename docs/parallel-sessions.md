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
| state | `hub`, `in progress`, `no commits`, `missing`, `not a folder` when a file stands in the worktree's place, `unreadable` when git cannot read the checkout, or `unknown` when git cannot count its commits against `main` |
| ahead/behind | Relative to `main`; `?` when git cannot count them. |
| dirty | Number of paths with uncommitted changes, untracked files included and a rename counted once. |

The survey reads each checkout as `repo-cleanup` does, through ejryan89-skill-core's `git_worktrees`, and never writes a
checkout's index, so it takes no lock a session committing there could meet. The hub row reads `off main` or `dirty`
when the hub has drifted. Merged state is not reported: squash merges leave every branch looking unmerged, so a
branch's pull request is the only reliable record.

## Retiring a worktree

After a task's pull request merges, run the `repo-cleanup` skill on the hub (by repository name, or by the hub's
absolute path), from a session in the hub or outside the clone, never one inside the worktree it retires. Retiring
by hand from the hub, `git worktree remove <path>` and `git branch -D <kind>/<name>`, works too. In one pass it does
the following:

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
failure for the session that owns the branch. Each worktree whose pull request merged is named for the user to retire with
`repo-cleanup`, run from outside it, judged by the commit it has checked out and never by its branch's name, so a later task on a
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
While a hub is guarded the guard refuses every `git config` that may write a setting, one a wrapper whose words
settle its program or a literal shell script runs included, so the guard is turned off from a terminal. A program the guard does not read can still write the
configuration file by its own words, as `tee -a <hub>/.git/config` does; the guard is no sandbox, and the check
names what such a write leaves in the hub's work tree and its configuration. A `shepherd-pull-requests` tick that sees
the guard on records the clone's opting in beside the configuration, in the hub's git directory, and `repo-cleanup`
names the guard off for as long as that record stands, so a clone that turns the guard off on purpose also deletes
`.git/hub-state.json` in the hub.

The hook command reads the setting itself, in Bash, and exits before starting Python when the clone has not opted
in: when git reports the setting unset or false, it exits in silence, and when git cannot read it at all, as with a
value that is no boolean or a configuration file git cannot parse, it lets git's own message through and adds a note
on stderr that names git's exit status, so an opted-in clone left unguarded is never passed unseen. Elsewhere the hook
is inert and costs one `awk`, one `env`, and one `git config` call per tool call: no Python
starts, so it needs none, and a `python` that still resolves to the Microsoft Store alias cannot fail the hook. In an
opted-in clone it runs `tools/hub_guard.py` under `$CLAUDE_PROJECT_DIR`, which checks the same setting again, with
`python` when `command -v` finds that name and with `python3` when it does not. `python` comes first because the
Windows installers give Python that name, and a `python3` there may be the Microsoft Store alias; `python3` is the only
name Homebrew and the python.org installer give it on macOS, and the name most Linux distributions give it. The first
of `python` and `python3` that the shell finds on the `PATH` Claude Code runs the hook with must start a Python as new
as [Requirements](installation.md#requirements) names: the hook chooses by name alone and never tries `python3` after a
`python` that starts and fails, since the guard's refusal is a failing exit too. Neither read can be
pointed at another repository's setting or outranked: each reads only the repository's own file, with
`git config --local`, which passes over the command-line configuration `GIT_CONFIG_PARAMETERS` and `GIT_CONFIG_COUNT`
carry; the hook command runs its call through `env -u` for each of `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, and
`GIT_CONFIG` it holds, in any case, since Windows reads an environment name in any case; and `tools/hub_guard.py` runs
its own git calls, and the `pwsh` that reads a PowerShell command, without any of git's repository-selecting
variables, the set ejryan89-skill-core's `bounded_process.py` names, through its `run_bounded`, which ends every
process `pwsh` starts with it. Neither git writes a trace or its output where the environment or the configuration
points: the hook command's `env -u` drops each `GIT_TRACE` variable and `GIT_REDIRECT_STDOUT` and
`GIT_REDIRECT_STDERR` too, and the guard takes them out of its own environment before its first git call, judging a
command's calls from the environment it was started with; and both set `GIT_TRACE2`, `GIT_TRACE2_PERF`, and
`GIT_TRACE2_EVENT` to `0`, which outranks a trace2 target the system or global configuration names.
`tests/tools/test_hub_guard.py` runs the tracked command both ways.

The command is also written to end at once under PowerShell, for the runtime that runs it there (see
[Other runtimes](#other-runtimes)). Its first two lines are `set \` and `exit 0`: Bash, like any POSIX shell, joins
them into `set exit 0`, which only sets the positional parameters, and goes on, while PowerShell reads `set \` as
setting a variable named `\` and then exits with status 0. PowerShell parses a whole command before it runs any of
it, so the rest is written in what both shells parse, `test` rather than `[`, `env -u` rather than a loop of
`unset`, `awk` printing to `/dev/stderr` rather than `>&2`, which PowerShell refuses to parse, and `exec "$p"` to
start the Python the hook chose rather than `"$p"` alone, which PowerShell reads as an expression and refuses at the
option after it.
`HookCommandUnderPowerShellTests` in `tests/tools/test_hub_guard.py` runs the tracked command under `pwsh`, opted in,
not, and with an opt-in git cannot read, and holds that it exits 0 with no output and starts no program. The hook
declares `onFailure: "block"`, which [The guard's contract](#the-guards-contract) explains.

With the guard enabled:

- **File tools.** `Edit`, `Write`, or `NotebookEdit` is refused when its target lies in the hub's
  tracked files or anywhere in its git directory, `.git`, which holds the hub's `HEAD`, index, and configuration
  beside what every worktree shares. This applies whichever session asks, so a worktree session that writes to a hub
  path by absolute path is also stopped, and so is one through a link that leads there, since the target is judged
  with every link on the way to it followed. Gitignored files, such as `.claude/settings.local.json`, are exempt.
  While a hub of the session's own directory or project is guarded, an edit of a file git reads its system or global
  configuration from is refused too, as a command's write to one is (see
  [What the environment has git write](#what-it-understands)).
- **Git in the hub: an allow-list.** A git call that acts on the hub is allowed only in a form `READ_ONLY_FORMS`
  in `tools/hub_guard.py` declares, and every other is refused, whatever it would do: git's surface is too wide to
  list what writes, and successive outside reviews each found a writing spelling a list of writing commands
  missed. A form is a subcommand, or a subcommand and the word of its own subcommand, as `worktree add`, which must
  come first; `reflog delete` is no `reflog`, and `stash` alone, which pushes, is no `stash list`. A call whose place
  the text does not settle is judged the same way while a hub is guarded, since it may act on the hub. Whatever the
  subcommand, and wherever git runs, an option that writes a file into the hub or runs a command, and a `git config`
  that writes, are also judged as [What a git call's words write or run](#what-it-understands) says. Each row
  declares the options its form may take, and a call is allowed only when:
  - every option is one its row declares, spelled in full: an abbreviation git would accept, a negation such as
    `--no-check`, and any option the row does not name are refused, so the last value of an option that keeps a form
    read-only, as `--check` or `--ff-only`, is always the one given
  - a value is attached after `=`, or as the rest of a letter's word, or is the next word, which git takes as the
    value whatever it holds, so a call whose next word starts with a dash, `-` and `--` among them, is refused and
    its value is to be attached; a bundle of letters is read only when every letter is a flag
  - a word the text does not settle stands only after a `--` the row allows, since it may be any option
  - the row's operands hold, and `--` stands only where the row allows one
- **The forms the hub allows.** The rows of `READ_ONLY_FORMS` are `apply`, `blame`, `branch`, `cat-file`,
  `check-attr`, `check-ignore`, `checkout`, `cherry`, `config`, `count-objects`, `describe`, `diff`, `fetch`,
  `for-each-ref`, `grep`, `log`, `ls-files`, `ls-remote`, `ls-tree`, `merge`, `merge-base`, `pull`, `reflog`,
  `reflog show`, `remote`, `remote get-url`, `remote show`, `rev-list`, `rev-parse`, `shortlog`, `show`,
  `show-ref`, `stash list`, `stash show`, `status`, `switch`, `tag`, `version`, `worktree add`, `worktree list`,
  `worktree prune`, and `worktree remove`. Most read and take only options that change what they print; the log, show,
  and diff forms take a declared set of revision and diff options, without `--output`, `--ext-diff`, or `--textconv`.
  The others keep the hub on `main` and current, or retire a task:
  - `pull --ff-only origin main` and `merge --ff-only origin/main`, each also with `-q`, `--quiet`, `-v`, or
    `--verbose`, and the pull with `--no-rebase`; the target is named in full, so a bare `pull --ff-only`, which
    follows whatever upstream a setting names, is refused, and a fast-forward to any other target is refused with a
    reason that names it. The merge is refused while a ref git reads as `origin/main` before
    `refs/remotes/origin/main`, or beside it, exists (`FAST_FORWARD_SHADOWS`: `refs/origin/main`,
    `refs/tags/origin/main`, `refs/heads/origin/main`, and `refs/remotes/origin/main/HEAD`), or when git cannot list
    them. A call from a worktree that writes what the hub shares, as `branch -u <x> main`, `remote set-url`, or
    `update-ref refs/remotes/origin/main`, acts on that worktree and is not refused, so
    `merge --ff-only origin/main` follows whatever that remote-tracking ref holds
  - `fetch` with options that change only what it prints or which remotes it reads, and operands with no `:`, so it
    never moves the branch `HEAD` names (`-u`), sets an upstream (`--set-upstream`), or writes a ref a refspec names,
    as `x:refs/remotes/origin/main` would for a later fast-forward
  - `switch main` and `checkout main`, optionally with `-q`
  - `worktree add` with `-b` naming a task branch, `<kind>/<name>` as `tools/worktrees.py` names one, so it never
    creates a branch git names itself or one that shadows a ref the hub follows, as `-b origin/main` would, and
    optionally `-q`; and `worktree remove`; neither with `-f`, which would check `main` out a second time or remove a
    worktree with changes
  - `branch` listing, or deleting with `-d`, `-D`, or `--delete` any local branch git will delete, not only a task's
    `<kind>/<name>` branch, and with `-r` any remote-tracking ref; git refuses to delete a branch a worktree has
    checked out; an operand without one of those or `--list` creates a branch, and `-u`, `--set-upstream-to`, `-m`,
    `-c`, and `-f` are not declared
  - `tag` listing, its operands read only with `-l` or `--list`
  - `apply` with `--check`, `--stat`, `--numstat`, or `--summary`, which check or describe a patch; `--apply` and
    `--no-check` are not declared
  - `config` in its reading forms
- **Aliases.** A subcommand that no row names and that is not one of git's builtins, which no alias can stand for,
  may be an alias, so the guard reads it, first among the `alias.<name>` settings the command line gives (`-c`,
  `GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT`), then with `git config --get alias.<name>` in the hub for a call that
  acts on the hub, or where the call acts for one outside every hub (a worktree shares its hub's configuration, so a
  hub's alias is read from a worktree too), and judges what it runs, through further aliases, by the same rows. In the
  hub, a name that is no alias is refused. A shell alias (`!...`) is read as Bash from the top of the work tree it
  runs in, its parts the guard does not read allowed as in any command. An alias that cannot be read, or nests more
  than four deep, is refused while a guarded hub is in view, and one that writes a hub is refused wherever it is
  called.
- **Both shell tools.** The same commands are allowed and refused whether they run through the
  `Bash` tool or the `PowerShell` tool, or as a `Monitor` tool's command, which runs in the `Bash` tool's shell.
- **Redirections.** A redirection whose target lies in the hub's tracked files or its git directory is refused, as
  a file tool's edit is. Any other program a command runs, by its own words, is one the guard does not read, so it is
  allowed, a hub tool that rewrites hub files among them, as `python tools/skill_reference.py --write` does.
- **The check.** What the guard does not read, the check finds afterwards: ejryan89-skill-core's `hub_state.py`
  names each way the hub differs from a clean checkout of `main` at `origin/main`, and the `shepherd-pull-requests`
  tick prints each as a `HUB_DIFFERS` line, but behind, which its `HUB` line covers, and `repo-cleanup`'s report each
  as a `MAIN_DIFFERS` line, from the hub or from a worktree alike. The tick also compares the hub's configuration with
  the record the previous tick left, as the contract below says, and prints the guard turned off, each setting
  changed, and each hook and info file changed since then as `HUB_DIFFERS` lines too.

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

The hook is given the text of a command, not what runs under it. The guard catches the accidents a session makes in
the hub, and is no sandbox: it reads a command for its git calls and where each acts, and for the files its
redirections write, and a file tool's event for the file it edits. What it reads it judges; a part of a git call's own
words it does not settle is refused while a guarded hub is in view; and every other part of a command is a program
the guard does not read, which it allows. A write such a program makes in the hub is the check's to find: ejryan89-skill-core's
`hub_state.py`, which the `shepherd-pull-requests` tick and `repo-cleanup` run, names each way the hub differs from a
clean checkout of `main` at `origin/main`, a change or an untracked file, another branch or a detached `HEAD`, or a
commit `origin/main` lacks.

The configuration every worktree shares is the check's too, since a program may write it. The check reads the hub's
own configuration file with `git config --local`, without following an include, and the guard's opt-in as the guard
reads it. Each tick but a `--dry-run` one leaves a record of it in the hub's git directory, `hub-state.json` beside
the configuration file: whether the guard was on, whether any tick saw it on, and a SHA-256 digest of each setting's
values, never a value, keyed by the setting's name in a form that holds a name in any encoding. The next tick names the
guard turned off since then, and each setting added, changed, or removed by its name, leaving out the opt-in's own,
which the guard's line covers, and the `branch.<name>.<key>` settings of every branch but `main`, which git writes in
the shared file at a worktree branch's first `push -u` and drops with the branch; a first tick, with no record, names
neither. `repo-cleanup` names a hub whose guard is off while the record says a tick saw it on. The record keeps
whether any tick saw the guard on, since an unset setting cannot tell a lapse from never opting in, so a clone no
tick recorded is never named.
A record that cannot be read is named, and the tick writes its new record with the clone opted in, since whether the
clone was is unknown, so `repo-cleanup` names `lapsed` for a clone that never opted in until `.git/hub-state.json`
is deleted. Only a `shepherd-pull-requests` tick writes the record; `repo-cleanup` only reads it.

The hub's git directory holds other files every worktree shares, which a program may write as well: a hook in
`hooks/` runs at a commit or a push in any worktree, as one `pre-commit install` run in the hub writes, and
`info/exclude` and `info/attributes` apply in each, an exclude entry hiding an untracked hub file from the status the
check reads. The record keeps a SHA-256 digest of each too, never its contents: of each entry of `hooks/` by its name,
escaped as a setting's name is, but the `.sample` files git writes there and never runs, and of each info file by its
path. A hook's digest covers its bytes and whether its owner may run it, since git runs a hook only when it is
executable, so making a hook executable is a change; an info file's covers its bytes alone. Each is read through a
link, as git reads it, so a link to nothing is no file. The next tick names each one added, changed, or removed by its
path, as `hooks/<name>` or `info/<name>`; a first tick, and the first after a record written before these files were
read, which holds none, names none of them and records them. A hook or info file that cannot be read, or a `hooks/`
that cannot be listed, is named as a hub the check could not read, with the reason, and the record is left until it
can be read.

The configuration's limits:

- The record is a detector's memory, not a control: a session that can write the configuration can rewrite the record
  too.
- A file the configuration includes, the hub's `config.worktree`, and the global and system configuration are not
  read.
- A branch's own settings, other than `main`'s, its `remote`, `pushRemote`, and `merge` among them, are not read, so a
  worktree branch whose pushes were sent elsewhere is not named.
- The hooks a `core.hooksPath` setting points at, elsewhere than `hooks/`, are not read; the setting itself is named
  as a configuration change. Nor is anything a hook runs beyond its own file, or what a folder in `hooks/` holds.
- No other file in the git directory is read.
- Two names that differ only in whitespace are named alike in a line, though the record tells them apart.
- A hook is read whole at each tick, so a very large file in `hooks/` costs its size in memory.

#### What it understands

- **Where a git call acts.** git writes the `HEAD` and index of its repository and the files of its work tree, so a
  call acts on the hub when either one is the hub's. The repository is the one `--git-dir` or `GIT_DIR` names, or
  else the one git finds from where it starts, which inside a git directory, such as `-C <hub>/.git`, is that
  directory's own repository; the work tree is the one `--work-tree`, `GIT_WORK_TREE`, or a `core.worktree` setting
  names, or else where git starts, after each `-C`. A relative path is read from where git finally starts, and a
  relative `core.worktree` from the repository it is set for. `GIT_COMMON_DIR` names only the store of objects,
  configuration, and shared refs every worktree uses, so it does not change which checkout a call writes; the
  environment's store is judged as the bullet on what the environment has git write says. The guard reads git's own
  options as git does, with no abbreviation and with a value attached after `=` or as the next word (`-C`, `-c`,
  `--attr-source`, `--config-env`, `--git-dir`, `--namespace`, `--shallow-file`, `--super-prefix`, `--work-tree`),
  and `core.worktree` and `alias.<name>` among the command-line configuration a call sees: `GIT_CONFIG_PARAMETERS`,
  `GIT_CONFIG_COUNT` with its pairs, then each `-c` and `--config-env`. Of the settings `-c` and `--config-env` hand
  git, only the ones `GIT_SETTINGS_UNDERSTOOD` names and every setting of a section `GIT_SECTIONS_UNDERSTOOD` names
  (`advice`, `alias`, `color`, `column`, `i18n`) are understood, in any case; any other, or one the text does not
  settle, is a part of the git call the guard does not settle, since many run a command, as `core.pager`, `pager.<command>`,
  `core.fsmonitor`, and `diff.external` do, or choose where git writes. The location and configuration variables
  reach a call from the environment Claude Code gives the hook; an assignment before git to one is a part of the git
  call the guard does not settle, and a variable a command sets for a later git call, by `export`, `env`, or a
  PowerShell assignment, is not read ([What it does not see](#what-it-does-not-see)). On Windows git reads an environment name in any case, so `git_dir` is `GIT_DIR` there; when
  two spellings disagree, the call is judged by the upper-case one, the spelling git reads, with a note.
- **The index a git call is given.** Any git call may rewrite the index `GIT_INDEX_FILE` names, even a form that
  only reads the hub: `status` and `diff` refresh it, and a fast-forward writes a tree into it, as does the child
  git that `worktree add` (`reset --hard`) or `worktree remove` (its clean check) starts in another checkout,
  inheriting the variable. So while that index is one a guarded hub holds, anywhere in its git directory or among
  the files of its checkout it does not ignore, the hub's own index included, no git call runs, wherever it starts
  and whatever its subcommand. The one exception is a file in a linked worktree's own git directory, in the hub's
  git directory, for a call whose own repository is that worktree's, as `git rev-parse --git-dir` answers it: git
  writes there for that worktree anyway, its `index` and the lock files beside it, `index.lock` and
  `next-index-<pid>.lock`, which a hook or an editor git starts mid-operation hands on, among them. No other call's,
  a hub's read-only forms included, takes that exception. The index is read as an absolute path with each symbolic link
  followed, as git follows one to write through its lock file. A relative path is not settled, since git reads it
  from the directory each process it starts works in, so a call given one is refused while a hub is guarded; git
  exports a relative index only for a repository whose git directory is relative, which no worktree's is. `gh`
  checking a branch out runs git with the same variables, so the location variables and the index place it too.
- **What the environment has git write.** git takes from its environment more places to write than its checkout, and the
  guard judges each from the environment Claude Code gives the hook, which no command here can change. A file git
  appends a trace to, the absolute path a `GIT_TRACE` variable names, `GIT_TRACE2` and `GIT_TRACE_PACKET` among them, a
  file trace2 makes inside a directory one names, and the file git for Windows sends its output to, which
  `GIT_REDIRECT_STDOUT` and `GIT_REDIRECT_STDERR` name, is judged as a redirection's target is; a trace sent to a
  descriptor or a socket, a relative trace path, which git ignores, and `off` or `2>&1` write no file, and a relative
  output file, which git reads from where it starts, is not settled. A trace2 target, `trace2.normalTarget`,
  `trace2.perfTarget`, or `trace2.eventTarget`, that the system or global configuration names is judged the same way,
  unless `GIT_TRACE2`, `GIT_TRACE2_PERF`, or `GIT_TRACE2_EVENT` holds a value, which outranks it: the guard asks git for
  them with the session's environment, so the files `GIT_CONFIG_SYSTEM`, `GIT_CONFIG_GLOBAL`, `HOME`, and
  `XDG_CONFIG_HOME` choose, and every file their `include.path` names, are read as git reads them (`TRACE2_SETTINGS`);
  trace2 never reads a repository's own configuration or the command line's. A relative path any of those variables
  holds (`CONFIGURATION_LOCATIONS`) is read by each git from wherever it starts, so it is a target the guard cannot
  place. Such a file reaches every git a command starts, the git a program or a `gh` subcommand runs among them, which
  the text does not show, and no git call is owed a trace in a hub, so one that lies in a guarded hub refuses every
  `Bash`, `PowerShell`, and `Monitor` command, whatever it holds; one the guard cannot place, or a configuration git
  cannot read, refuses every command while a hub is guarded and is otherwise noted. Two more files the session's
  environment and configuration name are judged the same way, each written by a git a command starts, a program's own
  and a `gh` subcommand's among them: the file `SSLKEYLOGFILE` names (`KEY_LOG_VARIABLE`), to which libcurl, which
  git's https transport runs, and other programs append the keys of each TLS session, a relative path being one each
  program opens from where it starts, which the guard cannot place; and, from every scope git reads where the guard
  runs, in the session's directory, in its project, and in each hub in view, so a conditional include that holds
  there counts, the cookie jar an `http.cookieFile` or `http.<url>.cookieFile` setting names while any
  `http.saveCookies` or `http.<url>.saveCookies` setting is on, which an https call writes back, and the folder
  `fsmonitor.socketDir` names, where git's own file-system monitor makes its socket (`CONFIGURED_WRITES`), each a path
  git reads with a leading `~/` from the home, a relative one being one each git reads from where it starts. Each git
  call and `gh` checkout the command makes is judged the same way by the configuration git reads where that call
  runs, from every scope with includes followed (`repository_configuration`), where the session's places did not
  already judge it: a cookie jar or a socket folder in a guarded hub that a repository's own configuration names, or a
  conditional include that holds only there, refuses that command, one the guard cannot place, or a configuration git
  cannot read, refuses it while a hub is guarded, and any other command is not touched. The guard
  reads the configuration as it stands when the command starts, so while a hub is guarded it also refuses a command
  that writes a file an include in the configuration one of its git calls reads names, or that runs git outside every
  guarded hub and writes a file named `config` or `config.worktree`, or one in a folder named `.git`, by a redirection
  or `git config --file`, since it may be the configuration of the
  repository that call reads, one the command makes among them (`repository_refusal`); and it refuses a command that
  writes a file git reads its system or global configuration from, by a redirection, by a git call's words, as
  `git config --file <file>` writes, and a file tool's edit of one: each file
  `git var GIT_CONFIG_SYSTEM` and `git var GIT_CONFIG_GLOBAL` name, whether or not it exists, every file the listing of
  those scopes names as an origin, and every file an `include.path` or `includeIf.<condition>.path` in them names, a
  leading `~/` read from the home and a relative path from the including file's directory, while a `~user/` or
  `%(prefix)/` path, which git expands for itself, is one the guard cannot place (`configuration_files`); where git
  cannot name them, or the guard cannot place one, it refuses every file a command writes while a hub is guarded. The
  store `GIT_OBJECT_DIRECTORY` names, where git writes objects, and the common directory `GIT_COMMON_DIR` names, where
  it writes objects, configuration, and shared refs, are refused for every git call and every `gh` checkout when a
  guarded hub holds them, unless each is the call's own, the one `git rev-parse` answers for the call's repository
  without the variable, as a worktree's is the hub's, which git writes for it anyway. A call that acts on a guarded hub
  runs only with the hub's own index and store and the hub's own view of its objects: given any `GIT_INDEX_FILE`, a
  store or common directory other than the hub's own, or any of the variables `OBJECT_VIEW_VARIABLES` names
  (`GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_SHALLOW_FILE`, `GIT_GRAFT_FILE`, and `GIT_REPLACE_REF_BASE`), each set even
  to an empty value, it is refused, since a fast-forward then moves the hub's `HEAD` and tree while the new tree goes
  into the other index, leaving the hub's own out of step, or moves its refs to objects its store lacks. A call whose
  repository is one of a guarded hub's linked worktrees writes the refs the hub's store holds, its branch and the
  remote-tracking refs among them, so it too is refused given a store or common directory other than its own or any of
  those variables, though not given an index, which is its own to write: a commit there would move the branch to a
  commit only the other store holds, a fetch the remote-tracking refs to objects only the alternates hold, and a `gc`
  may prune objects the hub's refs still need. A store a relative path names is read from wherever git works, so it is
  not settled. Each path is read with every symbolic link followed. A place the guard cannot settle is refused while a
  hub is guarded and otherwise noted.
- **What a git call's words write or run.** Wherever git runs, a file it writes at a path its words name is judged
  as a redirection's target is, a relative path read from where git starts. The options that name such a file are
  `--output`, which every subcommand that reads git's diff or revision options takes, and those
  `GIT_WRITING_OPTIONS` names: `am --directory`, `apply --build-fake-ancestor`, `apply --directory`, `archive -o`,
  `bugreport -o`, `bugreport --output-directory`, `checkout--worker --prefix`, `checkout-index --prefix`,
  `clone --separate-git-dir`, `credential-cache --socket`,
  `credential-store --file`, `diagnose -o`, `diagnose --output-directory`, `fast-export --export-marks`,
  `fast-import --export-marks`, `format-patch -o`, `format-patch --output-directory`, `gc --expire-to`,
  `index-pack -o`, `init --separate-git-dir`, `init-db --separate-git-dir`, `mailsplit -o`,
  `read-tree --index-output`, `repack --expire-to`, and `repack --filter-to`. Each is read in every spelling git's
  option parser takes: exactly, with its value after `=` or as the next word, abbreviated to a prefix of its name,
  or, for a letter, with its value attached or at the end of a bundle, as in `-vo <file>`; a word after `--` is an
  operand. `bugreport`, `clone`, `diagnose`, `format-patch` without `--stdout`, `init`, `init-db`, and
  `unpack-file` write in the directory git starts in when no option or operand names another
  (`GIT_DEFAULT_WRITES`), and `bundle create`, `clone`, `index-pack`, `init`, `init-db`, `interpret-trailers`,
  `mailinfo`, `merge-file`, `pack-objects`, `worktree add`, and `worktree move` at a path an operand names
  (`GIT_OPERAND_WRITERS`), each judged the same way. Each row of that table names the operands it writes, the
  options whose value is the next word, which is then no operand, the boolean mode that must be on for it to write
  at all, as `interpret-trailers --in-place`, and the options with which it prints instead, as `merge-file -p`. A
  word that is another option's value, as the second word of `merge-file -L -p`, is never a mode; a mode is read
  exactly or abbreviated, the last of it and its `--no-` form deciding. `apply --unsafe-paths` lets a
  patch write at the paths the patch names, which the guard does not read (`GIT_UNREAD_MODES`, whose row names
  `apply`'s options with a value as a writer's does), so it is refused while a hub is guarded. `format-patch` takes
  git's revision and diff options, many of which take the next word as their value and no table lists, so its
  `--stdout` counts only where the word before it can be no option awaiting a value: none, a word that is no option,
  a count such as `-1`, or an option whose value follows `=`. A root beneath which git writes the files it checks out or patches,
  `checkout-index --prefix` and the `--directory` of `am` and `apply`, is judged as a file it writes, so a root in
  the hub's tracked surface is refused from another checkout too. A `git config` is understood in its reading
  forms, with an action that reads (`--get`, `--get-all`, `--get-regexp`, `--get-urlmatch`, `--get-color`,
  `--get-colorbool`, `-l`, or `--list`), its subcommand `get` or `list`, or one name holding a dot and no action,
  and only options that change what it reads or how it prints. Any other form may write, an abbreviated option
  among them, and one that writes only the file `--file` or `-f` names, with no other scope, is judged as a file
  git writes. The options that start a command or a program their value names, `GIT_RUNNING_OPTIONS`, which takes
  ejryan89-skill-core's `git_client.CARRIER_OPTIONS` and adds the subcommands its plain client never runs, are
  refused while a hub is guarded, in every spelling and wherever the word stands:
  `archive --exec`, `clone --config`, `clone --template`, `clone --upload-pack`, `clone -c`, `clone -u`,
  `difftool --extcmd`, `difftool -x`, `fetch --upload-pack`, `fetch-pack --exec`, `fetch-pack --upload-pack`,
  `filter-branch --commit-filter`, `filter-branch --env-filter`, `filter-branch --index-filter`,
  `filter-branch --msg-filter`, `filter-branch --parent-filter`, `filter-branch --setup`,
  `filter-branch --tag-name-filter`, `filter-branch --tree-filter`, `grep --open-files-in-pager`, `grep -O`,
  `init --template`, `init-db --template`, `ls-remote --exec`, `ls-remote --upload-pack`, `pull --upload-pack`,
  `push --exec`, `push --receive-pack`, `rebase --exec`, `rebase -x`, `send-pack --exec`, and
  `send-pack --receive-pack`. So is a `bisect` or `submodule` action outside the ones `git_client.PLAIN_ACTIONS`
  lists, as `bisect run` and `submodule foreach` are. A word the text does not settle may be any of these options,
  so one given to a subcommand these tables name, or to one `GIT_OUTPUT_SUBCOMMANDS` names as taking `--output`, is
  refused while a hub is guarded; a word after `--` is not. `tests/tools/test_hub_guard_git_options.py` holds to
  these tables every option the installed git's `git <subcommand> -h` lists with a path for its value or names as a
  root, unless `GIT_PATH_OPTIONS_READ` holds it; every operand its usage names with a path, a mail, or a patch,
  unless `GIT_OPERANDS_READ` does; every boolean option its `-h` says writes, overwrites, edits in place, or reaches
  outside the work tree, unless `GIT_MODES_READ` does; and each row of `GIT_OPERAND_WRITERS` to every option with a
  value that `-h` lists. Each read table says why its entry writes nothing outside the checkout a call runs in, so
  an option or operand a later git adds fails there until a row decides it.
- **Bash, by its grammar.** `tools/bash_syntax.py` reads Bash as Bash parses it: single and double quotes, `$'...'`,
  escapes and line continuations, comments, and here-documents, and the command tree of lists, `&&` and `||`,
  pipelines, `( )` subshells, `{ }` groups, `if`, `case`, `while`, `until`, `for`, `select`, `!`, and `time` with
  its `-p` and `--`. The guard reads each simple command whose name is literal once quotes are removed, wherever the
  tree holds it: git as a git call, `gh`'s checkouts as one, and `cd`, `pushd`, and `popd` as moves of the shell. A
  wrapper ejryan89-skill-core's `command_wrappers.COMMAND_WRAPPERS` names, as `env`, `timeout`, `command`, `exec`,
  `nohup`, `sudo`, or `xargs`, is read as the command it runs, past its options by `command_wrappers.unwrapped`, its
  assignments judged as ones before that command, and a literal `bash -c` script, or a `pwsh` or `powershell`
  start-up's script, its parameters read by ejryan89-skill-core's `powershell_startup.startup_command`, is read by its
  shell's reader, so git either runs is judged by the same rows as a bare git call; an `-EncodedCommand` runs a script
  the text does not settle, so it is judged as a git call whose subcommand the text does not settle; a wrapper whose
  words do not settle its program, and any other script the text does not settle, are not read. Any other command is a program the guard does not
  read. A word is read as literal text or `~`; an expansion, a glob, or
  a brace expansion leaves its word one the text does not settle. Such a word may stand among a git call's words
  after its subcommand, where a refused subcommand given one is refused in the hub, since it may be any option. Each
  command or process substitution is read as the commands it runs, to a depth of four, and so is one in an unquoted
  here-document's body; what is nested deeper is not read. A quoted here-document's body, as `<<'EOF'` gives it, and a
  here-string's quoted word do not expand: each is data handed to the command's input, read as nothing it runs.
  Bash its grammar cannot read is not read either; the lines before a syntax error do run, unlike PowerShell's.
- **Directories.** `cd`, `pushd`, and `popd` to a literal directory move the shell, and each git call after them is
  placed there: along `&&` where the change succeeded, along `||` where it failed, and after `;` where the target is
  a directory now, or else nowhere the text settles, since an earlier command may make it one. A pipeline's commands
  and a background job run in subshells of their own, so a change there moves nothing after them. A target the text
  does not settle, `cd -`, a `CDPATH` search, an option of `cd`, and `pushd`'s rotations leave the directory unknown,
  and a compound command that holds a change leaves every git call in it and after it in a directory the text does not
  settle. So do the commands that may move the shell in a way the guard does not follow: a builtin
  `BUILTINS_NOT_FOLLOWED` names (`.`, `source`, `eval`, `builtin`, `command` but with `-v` or `-V`, `trap`, `fc`,
  `alias`, `unalias`, `enable`, and `hash`), a function definition, a function the environment hands the shell as
  `BASH_FUNC_<name>%%`, and a command whose name the text does not settle. A git call whose place is unknown is judged
  as the bullet on where a git call acts says.
- **Assignments before git.** An assignment before git is understood to a name git does not read to choose a file or
  a program, that is, no `GIT_` name and none `GIT_READ_NAMES` names (`HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`,
  `USERPROFILE`, `HOMEDRIVE`, `HOMEPATH`, `TMPDIR`, `TEMP`, `TMP`, `PAGER`, `EDITOR`, `VISUAL`, `SSH_ASKPASS`, and
  `SSLKEYLOGFILE`, the file libcurl writes each TLS session's keys to), and to a name `GIT_PREFIXES` lists, with a
  value it allows, such as `GIT_EDITOR=true`. Any other is a part of the git call the guard does not settle. An
  assignment anywhere else, alone or before another program, is not read.
- **gh.** `gh pr checkout`, `gh pr co`, and `gh repo sync` (`GH_CHECKOUTS`) check a branch out in the checkout gh runs
  in, and are judged as a checkout git makes there, from Bash and PowerShell alike, read past `-R`, `--repo`, and
  `--hostname` with their values. Any other gh call, a subcommand the text does not settle among them, is a program the
  guard does not read, and so is one with another flag before its subcommand, since gh takes the word after a flag it
  does not know as its value, so `gh pr -b x checkout 12` checks a branch out.
- **Redirections.** A redirection that writes a file at a path the text settles is refused when the file lies in a
  guarded hub's tracked files or anywhere in its git directory, as a file tool's edit is; a duplication such as
  `2>&1` and a device such as `/dev/null` write none. In PowerShell every command's file redirection is read so, an
  expression standing as a command, as `'x' > <file>`, included. A target the text does not settle to a literal path,
  or a relative one in a directory it does not settle, is not read, but for one under the system's temporary folder
  that `TMP`, `TEMP`, or `TMPDIR` names from the environment, as `> "$TMP/out.txt"` does. A pattern's target is judged
  by the folder its match lies in. Each target is judged with every link on the way to it followed, so a link that
  already leads into a hub leads the write there.
- **PowerShell.** A PowerShell command is read by PowerShell's own parser, not by a second hand-written grammar,
  statement by statement as Bash's commands are, when every node type in its tree is one `POWERSHELL_UNDERSTOOD`
  names, in the forms that invoke nothing: pipelines of commands joined by `&&` and `||`, with redirections, whose
  elements are constants, strings, lists, parameters, and the values variables, value expressions, and properties
  compute, which leave their word one the text does not settle, as Bash's expansions do; `if` and `foreach`, whose
  conditions and bodies are read; a cast to a type `POWERSHELL_CASTS` names, such as `[int]` or `[string]`; and a
  script block handed to `ForEach-Object`, `Where-Object`, `Sort-Object`, `Select-Object`, or `Group-Object`. Each
  command inside an expression or a body is read as a command that runs within the statement holding it. A command
  holding any other node type or form, a method call, a static member such as `[IO.File]::Delete`, a member the text
  does not name, a cast to another type, a script block handed to any other command, or an assignment to the
  environment or to a variable PowerShell sets or reads itself (`POWERSHELL_VARIABLES`) among them, has only its git
  calls and its redirections read, each placed where the text does not settle once any command in it may move the
  location. A command is read as git, as `gh`, as a location cmdlet, followed as Bash's `cd`, `pushd`, and `popd` are
  to a literal file-system path given by position, `-Path`, or `-LiteralPath`, a leading `~` read as home, or as one
  of these cmdlets, by its name or an alias PowerShell gives it: `Set-Location`, `Push-Location`, `Pop-Location`,
  `Get-ChildItem`, `Get-Content`, `Select-String`, `Select-Object`, `Sort-Object`, `Measure-Object`, `Write-Output`,
  `ForEach-Object`, `Where-Object`, `Group-Object`,
  `Write-Host`, `Out-String`, `Out-Null`, `Out-File`, `Get-Location`, `Get-Item`, `Get-Command`, `Get-Process`,
  `Format-Table`, `Format-List`, `Start-Sleep`, `Remove-Item`, `Copy-Item`, `Move-Item`, `New-Item`, `Set-Content`,
  `Add-Content`, `Resolve-Path`, `Test-Path`, `Join-Path`, `Split-Path`, `Get-Date`, `Get-FileHash`, `ConvertFrom-Json`,
  `ConvertTo-Json`, and the function `mkdir`, which move nothing. Every other node type and command is a program the
  guard does not read; a command dot-sourced into the session, one whose name the text does not settle,
  `ForEach-Object` given a member's name, and a name PowerShell keeps for its own commands, an alias or a function of
  its own (held to `Get-Alias` and `Get-Command`), or any name holding a hyphen and no directory, whatever its verb,
  since a module PowerShell loads on demand may define a function by it, as Windows' own modules define
  `Delete-DeliveryOptimizationCache` (a program by such a name is written with `.exe` or as a path), may move the
  location, which is unknown after it, and so is one a pipeline of several commands sets, since each of them runs in
  the session, and a path on another provider, as `env:` or `alias:`. `tools/hub_guard-powershell.ps1` only reports
  what the parser found, and `tools/hub_guard.py` makes every decision. A command that does not parse is allowed:
  PowerShell parses a whole script before it runs any of it, so none of it runs. A command pwsh cannot read, as when it
  cannot be found or started, is allowed with a note on stderr.

#### What it refuses

A guarded hub is in view when it is the hub of the session's own directory, of its project, or of a place a git call
the command makes names, or the one whose git directory holds that directory or place. While one is, the guard
refuses:

- a git call that acts on a guarded hub in any form but the read-only ones, and one whose place the text does not
  settle in any form but those, since it may act on a hub; a git call whose subcommand the text does not settle,
  wherever it runs, since it may be an alias that writes a hub; a git call after an option of git's own the guard does
  not read; a refused subcommand given a word the text does not settle; and an assignment before git the rule above
  does not understand;
- a git call whose words run a command, write a file at a path the text does not settle, or include a word the text
  does not settle where it may be an option that does either; a setting `-c` or `--config-env` hands git that the
  guard does not know to be safe; and a `git config` that may write, but for one that writes only a file `--file`
  names outside every hub. A file a git call writes into a guarded hub is refused wherever the call runs;
- a git call, or a `gh` checkout, given from the environment an index, a store, or a view of objects that the bullets on
  the index a git call is given and on what the environment has git write refuse, and, while a hub is guarded, one given
  any of them that the guard cannot place; every command while the environment or the configuration has git write a
  trace or its output, the keys of a TLS session, a cookie jar, or a file-system monitor's socket into a guarded hub;
  and, while a hub is guarded, every command while either has git write one into a file the guard cannot place, or
  while git cannot read that configuration; and, while a hub is guarded, a command or a file tool's edit that writes a
  file git reads its system or global configuration from, by a redirection or a git call's words, or that writes any
  file while git cannot name those files; a command whose git call reads, where it runs, a configuration that names a
  cookie jar or a socket folder in a guarded hub, and, while a hub is guarded, one whose such target the guard cannot
  place, or that writes a file an include of that configuration names, or a file named `config` or `config.worktree`
  or in a folder named `.git` while it runs git outside every guarded hub;
- a file a redirection writes into a guarded hub, a link on the way to it followed;
- an alias that cannot be read or nests more than four deep;
- a command the guard does not finish reading and judging within 15 seconds, and the lookups of the hubs the places
  its git calls name reach, each git call of theirs given no more than the time left; and a command either reader
  fails on with an error of its own.

The refusal names what it could not settle or place, and asks for the git call to be written with literal words, or
for the directory to be written out, as `git -C <dir>` or `cd <dir> &&`. With no guarded hub in view, the same is
allowed with a note on stderr. A place whose lookup the reading time cuts short, between git calls or inside one, or
whose lookup git answers with an error for, may be a guarded hub's, so the guard then refuses the command as if one
were in view; it looks up every other place first, so the hubs they reach stay in view. A defect in the guard itself,
once it has found a guarded hub in view, is refused the same way, since a failure says nothing about where a call
acts; a defect before that, such as an event it cannot decode, an event that names no command or file path, or a hub
of the session's own directory or project it cannot look up, an opt-in git cannot read among them, is allowed with a
note on stderr, so that it cannot block every tool and is never passed unseen. A reading the time cuts short before
the lookups of the places it names is such a failure: refused while a hub of the session's own directory or project
is in view, and otherwise allowed with a note. A directory of the session's below a checkout, which a `.git` in it or
above it marks, where git answers with an error is one such hub: it is noted and passed over, never read as a place
outside every repository, and the other hubs in view still count; a git call that starts there is a place git answers
with an error for, refused as above. A successful answer that is not the lines the guard asked for is read as such an
error. A file an edit names in such a checkout, or in a git directory git answers with an error for, may lie in a
guarded hub, so the edit is refused; one where git did not run or finish is allowed with a note.

The hook declares `onFailure: "block"`, so Claude Code blocks a command when the hook times out, cannot start, or
exits with an error, rather than letting it run unseen. The hook command reads the project's own opt-in before it
starts the guard and exits 0 on every path where the clone has not opted in, git's answer that the setting is unset
or false and a setting git cannot read among them, so a block can fall only where the clone is guarded, and there it
is the refusal a failure is owed. One case blocks a clone that has not opted in: a `git config` that does not answer
within the hook's own timeout, which leaves the hook timed out before it can exit. In an opted-in clone where neither
`python` nor `python3` is on the `PATH` Claude Code runs the hook with, or where the first of them the shell finds
cannot run the guard, as a Python 2 `python`, one older than the floor, or a version manager's shim set to a Python it
cannot find, the hook fails, so every tool call it matches is blocked, the edit that would fix it included. Install
Python or put a current one first on `PATH`, or turn the guard off, from a terminal outside the session, then restart
Claude Code so the hook runs with the new `PATH`.

#### What it does not see

The hook sees the command text, and the guard reads git's words in it: it does not see any program other than git,
run by its own words, which it allows whatever those words are, so what a script file, `eval`, a function, a program
that is no wrapper, or `gh` but for its checkouts writes or runs, the git it starts included, and a variable a command
sets for a later git call, by `export` or a PowerShell assignment, reach a hub unseen, and the check names what they
changed there.

Within what it reads, it judges only what the text shows, so these too are left to the check:

- an assignment to `HOME`, `CDPATH`, `PWD`, or `OLDPWD` a command makes, alone or before `cd`, which moves a later
  `cd`, and the git call after it, where the guard does not follow;
- a git call before a part of the command Bash refuses, or that `tools/bash_syntax.py` does not cover, such as
  `coproc`: the whole command is not read, though Bash runs the lines before a syntax error;
- a PowerShell file redirection the reader's statements do not reach, which is allowed with a note;
- the words `xargs` adds from its input, which the guard takes as one word after the command it runs, and the string
  its `-I`, `-i`, or `--replace` replaces, which it reads as the literal it is;
- an assignment or a `-C` inside the line `env -S` or `--split-string` splits;
- an assignment before a shell that runs a script, as `GIT_DIR=<dir> bash -c 'git ...'` or
  `env GIT_DIR=<dir> bash -c '...'`, which the script's git inherits;
- in PowerShell, an assignment a wrapper hands git through another wrapper, as `env GIT_DIR=<dir> timeout 5 git ...`;
- a script a shell reads from its standard input, from a here-document or a pipe, as `bash <<'EOF'` or
  `echo '...' | bash`;

- a program git starts that the environment or the configuration names rather than the command's words: the program
  an inherited `GIT_EXTERNAL_DIFF`, `GIT_PAGER`, `PAGER`, `GIT_SSH_COMMAND`, `GIT_SSH`, `GIT_ASKPASS`, `SSH_ASKPASS`,
  `GIT_EDITOR`, `GIT_SEQUENCE_EDITOR`, `VISUAL`, `EDITOR`, `GIT_PROXY_COMMAND`, or `GIT_EXEC_PATH` names, or a setting
  of the system, global, or repository configuration does, as `core.fsmonitor`, `core.hooksPath` and the hooks it
  holds, `core.pager`, `pager.<command>`, `diff.external`, a diff driver's `textconv` or `command`, a filter's `clean`,
  `smudge`, or `process`, `core.sshCommand`, `core.askPass`, `credential.helper`, `core.gitProxy`, `gpg.program`,
  `remote.<name>.vcs`, or `core.alternateRefsCommand` does, runs in an allowed call, one of the hub's read-only forms
  among them, and writes what it writes, as any program does. The guard refuses each when `-c`, `--config-env`, or an
  assignment before git hands it to git, and refuses a command's writes to the system or global configuration files
  by a redirection or a git call's words, but it does not read them from the session's environment or from a
  configuration file, since one the session started with is the user's own: Claude Code sets `GIT_EDITOR`, Apple's,
  Homebrew's, and Git for Windows' git set `credential.helper` in the system configuration, and `git lfs install` sets
  the `lfs` filter in the global one, so refusing every call that may start one would refuse the hub's own
  fast-forward on an ordinary machine;
- a write target a repository's own configuration names for a git call whose place the text does not settle, and a
  configuration a command writes into a repository it makes by any means but a redirection or a git call's words;
- a shell function the environment the hook starts in supplies, as Bash's exported `BASH_FUNC_<name>%%` variables
  do: the hook command's own words, `test`, `env`, `awk`, `git`, `command`, and `exec` among them, run such a function
  where Bash runs it before the builtin or program of that name, so whoever sets the hook's environment can make the
  hook allow a call, as a function named `exec` would under `/bin/bash`, where a special builtin gives way to a
  function, and not under `/bin/sh`, where it does not; no syntax PowerShell also parses shuts every such name out;
- what the shell's own startup files set: an alias, a function, or an option they define, or a variable the Bash
  tool's shell holds that the hook's environment does not, or holds with another value;
- a work tree set in a repository's own configuration: the guard judges such a call by the checkout it runs in;
- a ref moved from another checkout to an object the hub's store holds, such as `git update-ref refs/heads/main` run in
  a worktree: refs live in the store every worktree shares, and the guard judges a call by the checkout whose `HEAD`,
  index, and tree it writes; a worktree call given another store or view of objects is refused, as the bullet on what
  the environment has git write says.

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
