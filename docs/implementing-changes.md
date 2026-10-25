# Implementing changes

This is the implementation profile for this repository. The `implement-change` repository skill
(`.claude/skills/implement-change/`) carries the generic procedure and reads every repository-specific fact from the
sections below. `tests/tools/test_implementing_changes_profile.py` fails when a section goes missing or one it does
not name appears. The skill that adds or changes a shipped skill, `change-skill`, builds on `implement-change`.

## Size gate

A change skips the plan and is implemented directly only when all of these hold:

- one component changes (one skill, one deployer module, one tool, or one document);
- the fix is known before exploring;
- no design choice remains;
- no contract file is touched.

Anything else is planned first.

## Worktree

A session starts in one of two places.

- **In the hub.** Pull `main` there first (`git pull --ff-only`), since a new worktree branches from the hub's local
  `main`. Then run `python tools/worktrees.py new <kind> <name>` with the change's title type as the kind, such as
  `feat` or `fix` (see [Pull requests](#pull-requests)), and enter the printed path (in Claude Code, `EnterWorktree`
  with `path:`).
- **In a worktree a runtime already made**, such as the `.claude/worktrees/<name>` on a `claude/<name>` branch that
  the Claude desktop app creates before the session starts. Stay in it and create no second tree: the unused one and
  its branch would have no pull request, so `repo-cleanup` keeps them and reports them. Before reading or editing,
  run `git fetch origin main` and `git merge --ff-only origin/main`, which moves a branch with no commits of its own
  to the current base.

If intake or the bug class changes the type after `new` named the branch, rename it with
`git branch -m '<type>/<name>'` before its first push. [Parallel sessions](parallel-sessions.md) explains why and how
a worktree is retired.

## Validation

Validation is continuous integration's job: the `Validation` check runs `python -B tests/run_validation.py`, split
across its Windows legs and across its Linux legs, in full on every push and on every pull request that is not
documentation-only. A documentation-only pull request runs the subset the runner selects (the policy checks, the suites
that name a changed file, and, when a changed file is Markdown, the Markdown fence checks), and a pull request merges
only when the check passes on both platforms.

Locally, before the pull request, run what the change directly touches and leave the rest to continuous integration,
which runs it anyway:

1. Each suite the change adds or changes, and each suite of a module or script it changes, one at a time, as the
   runner starts it: a Python suite with `python -B -m pytest <suite>`, a Bash or PowerShell suite as a program, or
   any of them through the runner with `-k <suite path>`.
2. Each new regression test with the fix reverted, to see it fail without the fix, then again with the fix restored.
3. For a changed Python file, ruff and mypy through the runner's jobs: `-k ruff`, the format and lint checks, which
   take seconds over the whole tree, and the type check of each root a changed file sits in, as in
   `python -B tests/run_validation.py -k ruff -k '*mypy*deployer*'` for a change under `deployer/`.
4. Once the title and the body are written, the checks that read them, and only those. Given without `-k`,
   `--changed`, `--full`, or `--shard`, `--pr-title` and `--pr-body` limit the run to the title, upgrade-note,
   silent-revert, and known-failure closing checks, start no suite, and say so:

```powershell
python -B tests/run_validation.py --pr-title '<title file>' --pr-body '<body file>'
```

`--changed` is optional, never required: from the files changed since `origin/main`, committed, uncommitted, or
untracked, it selects the policy checks, the static checks that read the kinds of file changed (for Python, the format,
lint, and type checks), and the suites the change reaches: a changed suite, a suite that imports a changed module
directly or through others, one that names a changed file or such an importer, one in the `scripts/` of the skill a
changed file sits in, and the suites `CHANGE_REACHES` in `tests/validation/job_selection.py` adds, such as the
deployer's rendering suite for a change under `skills/` and the suites that read the floors for a change to a tool
floor. It earns its run when that reach is narrow; for a change to the deployer, ejryan89-skill-core, or the runner it
selects most suites and comes close to a full run. A reach no file name or import shows goes in that table, never in a
list a contributor keeps. `-k <pattern>` instead runs only the policies and suites whose name or path matches, as when
rerunning one suite, and a pattern that matches nothing, even beside one that matches, is refused.

The runner decides what a documentation-only change needs. When every file changed since `origin/main` (the pull
request's base in continuous integration), uncommitted or untracked, is under `docs/` or `.github/ISSUE_TEMPLATE/`, or
is the top-level `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CLAUDE.md`, `AGENTS.md`, or
`.github/pull_request_template.md`, it runs the policy checks and only the suites that name a changed file, and, when
any changed file is Markdown, the PowerShell analyzer and the ShellCheck check of Markdown Bash fences, since those read
the fences a documentation change can break. The Python format, lint, and type checks and the shell checks of skill
scripts do not run, since no documentation file reaches them. Any other change, or one it cannot determine, runs every
suite, and so does `--full`, which the workflow passes on a push. Markdown under `skills/`, `agents/`, and `.claude/` is
skill and agent behavior, not documentation. `DOCUMENTATION_FILES` and `DOCUMENTATION_DIRECTORIES` in
`tests/run_validation.py` name only files no suite executes.

A guard, gate, verdict, count, or measurement has a test in which its evidence never existed, as item 5 of
[Validation](adding-a-skill.md#validation) in the skill contract states.

A test that holds a document's promise, such as a status line, an error line, or a listed construct, takes its
expected wording from that document, or from the one constant the code and the document both use, never from a copy
restated in the test, so the test cannot pin a change the document does not make. A shared constant's text is pinned
once, in one test that compares the constant with an independent literal, so a change to the text fails until that
pin changes with it; every other test takes the text from the constant. A contract value a decision in
[Design](design.md) fixes, such as what a plugin answers for a template token, is pinned the same way, and the pin
says beside it that it is a pinned contract value and which decision fixes it. No validation check holds this
rule, since a literal a test restates from a document cannot be told from one the test needs for itself; review holds
it.

Run the suite in full locally only when the change's reach warrants it, such as a validation policy or the runner.
When a leg fails in continuous integration, reproduce that leg with
`python -B tests/run_validation.py --shard <index>/<count>`, adding `--full` for a push, schedule, or dispatch leg,
which the workflow runs with it: the runner deals the legs after it selects, so without `--full` a branch whose changes
are all documentation deals a different leg. A pull-request leg needs no `--full`. Bringing an open pull request current
with its base needs no local rerun, since continuous integration runs on the result.

The gates that call a model never run in continuous integration, so they stay with the author: a change to skill
paths, `allowed-tools`, runtime adapters, or agents runs the `runtime-canary` repository skill, a change to a skill's
prompt, description, model guidance, or reviewer instructions runs `evaluate-skill` when the skill has scenarios, and a change to a
skill under `skills/` or `.claude/skills/` runs `audit-skill` on it. A canary run that started the real profile's
installed copy of a skill, which no runtime keeps out of a model's reach, tested nothing: the canary prints it
`UNCOUNTED`, runs the skill again, and judges only the last run, so the record holds every `UNCOUNTED` line beside the
run that counted, and a rerun of the whole canary chosen because it passed is no result. `CLAUDE.md` ("Required
validation") holds
the rules validation enforces.

The `Mutation gate` job of the `Mutation` workflow runs on every pull request and is not a required check. It mutates
the lines the pull request adds or changes in each Python module under `deployer/`, `tools/`, a skill's `scripts/`, or a
repository skill's `scripts/`, suites and `conftest.py` left out, with mutmut's operators, and runs each mutant against
the suites that executed the statement holding its line unmutated, as coverage.py measured them, until one fails, so a
mutant on a continuation line of a multi-line statement is reached when that statement ran. Of each such suite it runs
only the tests that ran the statement, as coverage.py's test-function contexts recorded them, or that started, while
their test ran, the process that ran it, and the whole suite when anything else ran it too: a set-up, a fixture, an
import, or a process started outside a test's call. Before the reach pass it measures a canary suite, and a reach run
that no longer measures the suite's own process and the process its test starts, each line named with that test, fails
the job. A mutant of a module-level assignment, such as a table or a compiled pattern, runs at import, so it runs
instead the tests that ran a line reading a name it binds, when only the module's functions read those names and no
other file names one, and the whole of each reaching suite otherwise. A mutation anchored on an expression that starts
on an unchanged line, such as removing an argument from a call that opens above the change, is left out. Its step
summary gives each module's changed lines and kill ratio, names each changed module whose changed lines yield no mutant,
and names each surviving mutant by its line, the mutation, and the suites it passed, with how many of a suite's tests
ran when not all did; a pull request that changes no such module gets one line saying so. The mutants run from one queue
that takes one from each module in turn, and the run is capped by `DEFAULT_MINUTES` in the gate. A run the cap cuts
short, leaving a mutant not run or a suite's reach unmeasured, is unfinished: its summary's first line says so, naming
each module's mutants not run and how many lie on lines that run at import, and the job fails. An unfinished gate is not
a verdict, whatever its table shows. Split the pull request, or move the module-level tables its changed lines hold into
the functions that use them, until its gate finishes. The summary of a finished run is the record: read it, and for a
surviving mutant on a line the pull request changed that alters behavior a caller relies on, such as a refusal or a line
a document or test treats as a contract, add the test that kills it, or name it in the pull request with why it is
equivalent. A mutant that only changes text no reader depends on needs neither, and no kill ratio is copied anywhere. It
runs on Linux only, because mutmut refuses Windows; the gate's docstring in `tools/mutation_gate.py` gives the details.

## Contract files

A change to any of these is a contract change, raises the release level, and is named under `## Upgrade note` in the
pull request body (the Versioning section of [releasing.md](releasing.md) holds the contracts and levels):

- `MANIFEST_VERSION` and `OLDEST_READABLE_VERSION` in `deployer/manifest.py`;
- a `required_vars` list under `deploy-meta/`;
- a schema under `skills/ejryan89-code-review-core/references/`, or a format table in
  `docs/code-review-operations-contract.md`, among them the code-review configuration file's format and the reviewer
  manifest format;
- a skill directory name under `skills/`, or a shipped skill's arguments, status lines, or exit codes, named as the
  `Contract:` field in [upgrade-notes.md](upgrade-notes.md) says;
- a tool floor in `deployer/tools.py`: `MINIMUM_PYTHON`, or a tool's `minimum`.
- a provider-specific PDF tool floor in `skills/ejryan89-skill-core/scripts/pdf_tool.py`, referenced by the catalogue's
  `provider_minimum`.

The upgrade-notes check in `tests/validation/upgrade_notes.py` compares every item above at the last tag except the
arguments, the status lines, and the exit codes, which no file holds, and fails while this list or the Versioning
section of [releasing.md](releasing.md) leaves out a file or directory it reads.

## Pull requests

A pull request's title becomes its squash commit's title, so `main`'s history reads by the titles. The title opens
with a type, an optional scope in parentheses, a colon, and a space, as in `fix(deployer): ...`:

- `feat` adds or extends a capability;
- `fix` corrects a defect;
- `docs` changes documentation only;
- `test` adds or changes tests only;
- `refactor` changes structure without changing behavior;
- `chore` is upkeep that changes no behavior, such as a rename or a recorded table;
- `ci` changes a workflow;
- `build` changes the development dependencies or the deployment prerequisites.

The type agrees with the worktree's branch kind when the kind is one of them, so the title check holds the type only
on a `<type>/<name>` branch; on a branch a runtime named, such as `claude/<name>`, the author alone holds it. A title carries no `!` and the body no
breaking-change footer: the Level under `## Upgrade note` is the one statement of a release level. Validation's title
check, in `tests/validation/pull_request_title.py`, reads the title from `--pr-title <file>` locally and from the pull
request in continuous integration.

The body names every issue the change resolves, each in its own `Closes #<number>` line. The project board moves an
issue by that line, and an issue a change resolved without one stays open.

A change a user who updates meets, every [contract change](#contract-files) included, states it under
`## Upgrade note` in the body, in the shape [upgrade-notes.md](upgrade-notes.md) gives, and never in that file: the
squash merge carries the body into `main`, and the release's notes are written from it. A shipped skill's arguments,
status lines, and exit codes are the contract items no check compares, so a change to them owes the note with nothing
to catch its absence; the `Contract:` field in [upgrade-notes.md](upgrade-notes.md) says how the entry names them.

A pull request that returns a file to its state before a commit on `main` reverts that commit's change to it, and
validation's silent-revert check, in `tests/validation/silent_reverts.py`, names each such file with the commit and
its pull request. A branch left behind, or a conflict resolved by taking the branch's side of a file it never set out
to change, carries such a revert unseen. The check compares each changed file's content with its versions on `main`'s
first-parent line, from the tip back to `COMMITS_BEFORE_MERGE_BASE` commits before the branch's merge base, and judges
a branch that merged `origin/main` in by what it holds. A file the branch deletes is never a revert. One it adds is
judged like any other, so it is named when it equals the file as it stood before any commit in that span changed or
deleted it, the newest such commit with it: a file brought back as it stood at its deletion names the deletion, and
one equal to an earlier version names the change that replaced that version. A deliberate revert is declared on a
line of the body of its own, optionally as a list item and outside an HTML comment, as
``Reverts `<path>` to its state before <commit>``, with seven or more hexadecimal digits of the commit the check
names. The check reads the body from `--pr-body <file>` locally, otherwise the branch's commit messages, and the pull
request in continuous integration.

A pull request that closes an issue a known failure of the malformed-input suite still names, in
`tests/malformed_input/known_failures.json`, is refused by the known-failure closing check, in
`tests/validation/known_failures.py`: the issue would close while its case still fails. It reads every closing keyword
GitHub reads (`close`, `fix`, or `resolve`, in any of their forms, before `#<number>`) outside an HTML comment, from
`--pr-body <file>` locally and the pull request in continuous integration, and passes when no body is given. The pull
request that fixes the reader removes the entry, which the suite then requires, and closes the issue.

Before `gh pr create`, run `git fetch origin main` and `git rebase origin/main`: onto `origin/main`, not the local
`main`, which is only as fresh as the hub's last pull. The branch is unpublished, so the rebase rewrites nothing anyone
has. Then push it under its own name with `git push -u origin HEAD`. While the pull request is open, merge
`origin/main` into it before pushing more commits, and never rebase or force-push it. `gh pr update-branch` is one way
to make that merge; it moves the remote branch, so pull before the next push. If a push is rejected, fetch and inspect
the remote branch before anything else: the maintainer may have updated it.

## Documentation

Each fact has one owner; update the owner in the same change.

| Document | Owns |
|---|---|
| `CLAUDE.md` | Architecture, safety rules, validation and pull request rules for every agent runtime, each stated once with a pointer to the section that holds how it is enforced. |
| `docs/design.md` | Why the repository is built as it is: the trust model, the invariants, and each decision with its reason, date, and source, and what was declined. |
| `docs/skills.md` | Each skill's reference. Generated blocks come from `python tools/skill_reference.py --write`. |
| `README.md` | The "Included skills" table and the front-door summary. |
| `docs/adding-a-skill.md` | The skill contract. |
| `docs/parallel-sessions.md` | Worktrees and the hub guard. |
| `docs/releasing.md` | Versioning, contracts, and the release procedure. |
| `docs/code-review-operations.md` | Code-review configuration and records. |
| `docs/dotnet.md` | What the `dotnet` bundle's skills share: its selection, the projects they see, and their requirements. |
| `docs/installation.md`, `docs/recovery.md` | Install, update, and recovery. |
| `docs/setting-up-a-machine.md` | The order a new machine's setup follows and the Windows conditions it meets on the way; the install commands stay with their owners. |
| `docs/codex-support.md`, `docs/copilot-support.md` | Per-runtime support. |
| `CONTRIBUTING.md` | The contributor path from a clone to a merged pull request, and the full account of the Python format, lint, and type checks (`CLAUDE.md` keeps the rules a session acts on). |
| `SECURITY.md` | The supported line, how to report a vulnerability, the disclosure policy for advisories, and the security-sensitive areas. |
| `docs/dependency-updates.md` | Every pin and floor: where it is declared, how it is updated and reviewed, and the check that holds it. |
| `docs/upgrade-notes.md` | What each release changes for an installation, one entry per change a user meets, written at release from each pull request body's `## Upgrade note`. |
| `docs/code-review-operations-contract.md` | The behavior the code-review skills promise to keep, and the formats of their records. |
| `docs/skill-evaluations.md` | The latest `evaluate-skill` result per skill and model, written by `python tools/skill_evals.py --write`. |

## Model guidance

The strongest model plans, and implements, a change in any of these areas, because a subtle error there passes tests
and ships:

- rendering and token substitution into Bash, PowerShell, JSON, or YAML, and quoting;
- trust boundaries: the configuration parser, ownership, journaled rollback, locking, and backups;
- the code-review pipeline and its records;
- any contract file above.

A balanced model suffices for a concrete plan that follows an existing pattern, such as a new tool test, a
documentation change, or a skill whose scripts and tests mirror an existing one. The cheapest model suits a fully
specified mechanical edit whose test already exists.

## Shared code

Shared code has one home. A module that more than one of the skills, the deployer, and `tools/` need lives in
`skills/ejryan89-skill-core/scripts`, and each imports it from there; never copy it. [Validation](adding-a-skill.md#validation)
in the skill contract states how each part reaches it and what validation holds.
