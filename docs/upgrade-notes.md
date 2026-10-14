# Upgrade notes

What each release asks of a user who updates, one entry per change a user meets since the previous tag. The GitHub release notes open with the release's entries; [Releasing](releasing.md#versioning) defines the contracts and the levels.

An entry is written in the pull request that makes the change, under `## Upgrade note` in its body, and never in this file. The repository squash-merges with the body as the commit message, so each merged change carries its entry into `main`'s history. Before tagging, `python tools/release_notes.py <version> --write` writes the release's section here from the commits since the previous tag, newest first, and the release pull request commits it ([Before tagging](releasing.md#before-tagging)). Entries for changes merged before entries moved into pull request bodies were written here under `## Unreleased`; the script reads those from the commits that added them, so the next release's section holds them too.

A generated section starts with `Level: <level>.` just below its version heading, before the entries: the largest leading `Level:` word any entry names, in the order of the **Levels** list in [Versioning](releasing.md#versioning). With no entries it states `Level: none (no upgrade-note entries).`, not an inferred patch level. The summary does not replace an entry's explanation or user action: before stability, a minor may carry a change that would be major later, as that entry says. Published sections remain as released; the tool adds the summary only to the new section it generates.

Validation (`tests/run_validation.py`) compares the contract files at the last tag reachable from `origin/main` with the working tree and fails while a changed contract item is named by no entry since that tag. In a pull request it reads the body, which CI passes it, and the commits merged into `origin/main`; a local run reads the branch's commit messages, or the body from a file passed with `--pr-body <file>`. It also fails on a section of this file that is not a version's.

An entry is a `###` heading saying what changed, followed by these fields:

- `Level:` starts with a level word from the Versioning section: `patch`, `minor`, or `major`. Before `1.0.0`, a change that would be major is carried in a minor, and the entry says so.
- `Contract:` the contract items the entry covers, each in backticks as the validation failure names it, or `none` for a demand no contract file records. `none` is case-insensitive and may end with a period.
- `User action:` what the user must do after updating, or `none`.
- `Pull request:` the pull request as `#N`. The script adds it from the squash commit's title, so a body leaves it out.

A pull request whose change no user meets writes `None` under its `## Upgrade note`.

## v0.7.0

Level: minor.

### A plain `--all` keeps a chosen opt-in bundle's skills in a home v0.5.0 or earlier last deployed into

- Level: patch. It fixes a defect in this release's handling of a manifest from v0.5.0 or earlier.
- Contract: none
- User action: none. On a home whose last deployment was v0.5.0 or v0.4.0, the first `--all` used to report `REMOVE` for a chosen opt-in bundle's skills when that release's `--migrate-from` had written the entry or the source had renamed or regrouped the bundle. It now reports them `UNCHANGED`. `--migrate-from` away from such an entry carries the request, and `deploy.py check` counts those skills' tools as required.
- Pull request: #805

### dotnet-dependencies keeps a project's bare and versioned platform frameworks apart

- Level: patch, a defect fix with no contract change: a project declaring both `net8.0-windows` and `net8.0-windows10.0.19041.0` (or any platform with and without a version) now gets `PATH`, `WHERE`, `CONFLICT`, and `REACHES` lines for each framework under its own name, each with its own direct packages, where before one framework was dropped and the other carried both frameworks' direct references.
- Contract: none
- User action: none
- Pull request: #803

### The runtime canary fails a run that checked no runtime, Claude Code alone under --discovery-only included

- Level: patch, a contributor-facing tool fix with no contract change.
- Contract: none
- User action: none
- Pull request: #794

### The Copilot review and the MCP handshake read the repository they name whatever GIT_DIR the shell exported

- Level: patch, a defect fix: a user whose shell sets `GIT_DIR` no longer has the Copilot CLI review, the MCP handshake, or `deploy.py verify` start a program that reads another repository.
- Contract: none
- User action: none
- Pull request: #791

### dotnet-dependencies prints a route the SDK and the walk both find once, applies a Choose's first true When, and keeps an older restore's direct packages

- Level: patch, a fix to a skill new in this release.
- Contract: none
- User action: none
- Pull request: #789

### A user who migrated or deployed with an earlier release keeps the opt-in bundle skills they chose

- Level: patch. A defect fix with no contract change: a plain `--all` no longer removes a chosen opt-in bundle's skills because an earlier release recorded the request without its members, or migrated it away, or because a migration rewrote a bundle's recorded members.
- Contract: none
- User action: none
- Pull request: #787

### review-document skips a pull request document its fixture cannot hold and reviews the rest

- Level: patch. A fix: a document whose path is too long for its fixture's folder, or lies under `source-snapshot-text/`, is skipped before any fixture is written, as the contract promised.
- Contract: none
- User action: none. Such a document is reported as `SKIPPED <path> <reason>` while the others are reviewed; with `--document`, the build fails with that reason.
- Pull request: #773

### A branch whose edit landed at another place, or in a file the base had changed, is kept rather than force-deleted

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none
- Pull request: #774

### A skill reads the repository it names whatever GIT_DIR the shell exported

- Level: patch, a defect fix: a skill, the deployer, and `update-coding-agent-skills` run git without the repository-selecting variables a shell or a git hook exported, so a user whose shell sets `GIT_DIR` or `GIT_INDEX_FILE` no longer has a skill read, clean up, or fast-forward the wrong repository.
- Contract: none
- User action: none
- Pull request: #777

### review-repository's conditions read headers only between hunks, follow a rename's old name, and hold diff paths to the snapshot

- Level: patch, a fix to a repository skill that contributors run, with no contract change.
- Contract: none
- User action: none
- Pull request: #769

### The runtime canary fails a run that could not list a runtime's skills or checked no runtime

- Level: patch, a contributor-facing tool fix with no contract change.
- Contract: none
- User action: none
- Pull request: #776

### An incremental re-review keeps a never-reviewed file in the record's uncovered list

- Level: patch, a defect fix in how a re-review reports and reviews coverage.
- Contract: none
- User action: none
- Pull request: #775

### dotnet-dependencies labels a conditional package only with the frameworks that hold it, keeps the chains the SDK cannot see, and reports a failed dotnet nuget why

- Level: patch, a fix to a skill new in this release.
- Contract: none
- User action: none
- Pull request: #772

### A plain --all keeps the skills an opt-in bundle request installed when the source regroups them

- Level: patch. A defect fix with no contract change: a bundle request stands for the members it installed, so renaming the bundle, moving a skill out of it, or migrating into a source that groups it differently no longer removes them.
- Contract: none
- User action: none
- Pull request: #768

### A validation policy check takes the runner's streams and environment as parameters, never patching them

- Level: patch, a contributor-facing validation fix with no contract change.
- Contract: none
- User action: none
- Pull request: #750

### The runtime canary does not ask a runtime to list a hidden dependency

- Level: patch, a maintainer-facing tool fix with no contract change.
- Contract: none
- User action: none
- Pull request: #749

### The seconds recorder reads validation legs under their old name as well

- Level: patch, a maintainer-facing tool fix with no contract change.
- Contract: none
- User action: none
- Pull request: #744

### A program a tool starts and leaves at once is now ended with the tool

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none
- Pull request: #743

### dotnet-format reports a solution it cannot read instead of passing it over

- Level: patch. A defect fix: a `.slnx` that is not well-formed XML, or declares a `DOCTYPE` or an entity, ends the run with `FAILED <solution> cannot be read: <reason>`, where it used to be read as a solution owning no changed file.
- Contract: none
- User action: none, unless the repository holds such a `.slnx`; fix or remove it.
- Pull request: #751

### A file a sharing specialist reviews is no longer reported as unreviewed

- Level: patch, a defect fix in how a review reports coverage.
- Contract: `docs/code-review-operations-contract.md` (the reviewer manifest's `uncovered` row and the `record.review.coverage.uncovered_files` row)
- User action: none
- Pull request: #745

### A branch whose only unmerged change is whitespace is kept, and the merge proof by content works under any diff setting

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none
- Pull request: #742

### A worktree's untracked files keep it even when status.showUntrackedFiles is no

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none
- Pull request: #742

### dotnet-dependencies answers every target framework and every assembly name, and reports unreadable inputs and SDK failures

- Level: patch. These are fixes to a skill new in this release. Against the unreleased `main`, its lines name the target frameworks they hold for, `why` finds an assembly named unlike its package or named by a `Reference` whether or not `dotnet` is installed, a failed `dotnet nuget why` prints `SDK_FAILED`, `graph` takes `--project`, and a `PackageReference Update` item no longer counts as a reference.
- Contract: none
- User action: none
- Pull request: #740

### The code-review pipeline reads a quoted diff path holding a character above U+00FF

- Level: patch, a defect fix with no contract change: code-review-core's reader of a diff header's path now goes through skill-core's one decoder, so a quoted path holding a character above U+00FF, which the old reader failed on, is read as the file it names.
- Contract: none
- User action: none
- Pull request: #739

### review-document refuses a pull request document whose path a fixture cannot hold

- Level: patch. A fix: a document path the source snapshot's unsafe-path rules refuse is refused before it is written, as the snapshot would have left it out.
- Contract: none.
- User action: none. A pull request document with such a path is now reported as `SKIPPED <path> is not a path a fixture can hold: <reason>`, or, with `--document`, fails with that line.
- Pull request: #737

### A plain --all leaves out an opt-in bundle or skill that is installed only as another skill's dependency

- Level: patch. A defect fix with no contract change: `--all` keeps an opt-in item only when it was requested, by `--include`, the menu, or, for a bundle, a member requested on its own.
- Contract: none
- User action: none; to add an opt-in bundle or skill you never chose, run `python deploy.py --all --include <name>` or choose it from the menu.
- Pull request: #738

### Git fixtures in the regression suites no longer run git's automatic maintenance

- Level: patch, a contributor-only fix to the test base with no user-facing change.
- Contract: none
- User action: none
- Pull request: #698

### review-document takes a document stage

- Level: minor. A new `--stage draft|review|final` option on `review-document`, and a new optional `document_stage` field in a fixture's `pull.json`; without them, reviews are as before.
- Contract: `docs/code-review-operations-contract.md` (the fixture `pull.json`'s `document_stage` row)
- User action: none
- Pull request: #696

### A pull request merges only when validation passes on Linux as well as on Windows

- Level: patch. CI only: the required `Validation` check now needs the `Validation on Linux` job, and the Windows legs are named `Windows shard <index>/<count>, Python <version>`; nothing a deployment or a skill does changes.
- Contract: none
- User action: none
- Pull request: #695

### review-repository routes process identity and the program lookup to its trust-boundary reviewer

- Level: patch. A repository skill's routing, which no deployed skill or contract carries.
- Contract: none
- User action: none
- Pull request: #691

### review-document reviews the documents a pull request changes

- Level: minor. A new form of the skill's argument: a pull request as `owner/repo#number` or its URL, with `--document` to narrow it to one document.
- Contract: none
- User action: none
- Pull request: #693

### repo-cleanup refreshes or ignores a dangling origin/HEAD instead of switching to a stale branch

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none
- Pull request: #690

### A skill under a category folder in the source is refused

- Level: minor. Before `1.0.0` this carries what would later be major for a source that nests skills under `skills/<category>/<name>`. This repository's source has none.
- Contract: none
- User action: none for this repository's source. A fork or other source that nested skills under a category folder moves each one to `skills/<name>` and groups them with a bundle in `source.json`.
- Pull request: #692

### The deployer's hints name python3 on Linux

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none. On Windows nothing changes; on Linux every hint the deployer prints names `python3 deploy.py`.
- Pull request: #689

### A new opt-in skill, dotnet-dependencies, and a dotnet bundle holding it with dotnet-format

- Level: minor. A new skill and a new opt-in bundle in the selection menu; nothing existing changes shape.
- Contract: `skills/dotnet-dependencies`, `deploy-meta/dotnet-dependencies.json` (a new skill directory and its `required_vars`, which name only `PYTHON`)
- User action: none, unless you want the skill: run `python deploy.py --all --include dotnet` or choose the `dotnet` bundle from the menu.
- Pull request: #688

### dotnet-format is selected through the dotnet bundle, and a bundle any member of which is installed is kept by --all

- Level: minor. `--include dotnet-format` is refused once the skill is a bundle member, and an installation holding `dotnet-format` is completed by the next `--all`, which adds `dotnet-dependencies`; before 1.0.0 a renamed selection root is carried in a minor.
- Contract: none
- User action: a script or habit that runs `python deploy.py --all --include dotnet-format` changes it to `--include dotnet`; a plain `--all` on an installation holding `dotnet-format` needs nothing.
- Pull request: #688

### Linux is described, and its fresh install is checked

- Level: minor, a platform the deployer now describes and checks, with Windows unchanged.
- Contract: none
- User action: none on Windows; on Linux, deploy as "On Linux" in docs/installation.md describes, running the commands as python3, while Linux support waits for the maintainer's runtime canary run.
- Pull request: #685

### A worktree whose ignored paths are all regenerable caches is removed with them, and a preserved worktree's line names its ignored paths

- Level: minor. New behavior a user meets: a merged worktree holding only `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, or `node_modules/.cache` paths, which a sweep used to preserve, is now removed with those caches, and the `REMOVED` fact and the summary's "Worktrees removed" row carry the caches deleted; a preserved worktree's reason names the ignored paths instead of only counting them.
- Contract: none
- User action: none; a worktree kept only for such caches is cleaned up by the next sweep.
- Pull request: #684

### A branch merged into a non-default base, or tracking its base, is classified MERGED

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none; a sweep after updating deletes such a branch where it kept it as `PR status unverified` or `PR history unmatched`.
- Pull request: #682

### A branch whose tip was rewritten after its pull request squash-merged is classified MERGED

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none; a sweep after updating deletes such a branch where it kept it as `PR history unmatched`.
- Pull request: #678

### Shipped skills name Python by the platform's command

- Level: minor, the interpreter rendered per platform for the Linux port; on Windows the rendered skills are unchanged.
- Contract: `deploy-meta/analyze-skill-cost.json`, `deploy-meta/audit-ai-config.json`, `deploy-meta/curate-agent-memory.json`, `deploy-meta/dotnet-format.json`, `deploy-meta/flag-review-finding.json`, `deploy-meta/github-activity-report.json`, `deploy-meta/repo-cleanup.json`, `deploy-meta/review-document.json`, `deploy-meta/review-insights.json`, `deploy-meta/review-prs.json`, `deploy-meta/update-pr-tracker.json`
- User action: none; the deployer supplies `PYTHON` itself, and on Linux a redeployment renders `python3` into the skills' commands and grants.
- Pull request: #677

### The deployer core runs on Linux

- Level: minor, a new platform in the deployer core; the README's support claim stays Windows.
- Contract: none
- User action: none on Windows; on Linux you can configure, deploy, and verify with an absolute POSIX `REPOS_ROOT`, but Linux is not yet supported: the code-review skills have not run a review there, since no model has run a skill on Linux through the runtime canary.
- Pull request: #671

### A script reaches a sibling skill's scripts through skill-core's sibling_scripts

- Level: patch. A contributor's script header changes: after the fixed line that puts skill-core on `sys.path`, a script, suite, or tool puts each other sibling there through `sibling_scripts` from skill-core's `skill_roots.py`, and validation fails on a hand-written path to a sibling's scripts and on a lookup the skill does not declare in `skill_deps`.
- Contract: none
- User action: none
- Pull request: #669

### One guarded reader for untrusted XML in skill-core

- Level: patch, a refactor with no change a user meets: the analyzer inventory and dotnet-format refuse and read exactly the documents they did.
- Contract: none
- User action: none
- Pull request: #664

### A repository reviewer's evaluation scenarios run

- Level: patch. A maintainer tool gains a scenario shape; nothing ships or changes for an installation.
- Contract: none
- User action: none
- Pull request: #667

### A git fetch that hits a brief network fault is retried

- Level: patch, a resilience fix with no contract change.
- Contract: none
- User action: none; a skill's `git fetch` or `git ls-remote` that hits a dropped connection, a failed TLS handshake, or a host briefly not found now retries for a few seconds and goes on, and one that still fails says how many retries it gave up after.
- Pull request: #665

### The release step that records the validation legs' seconds takes the median of several runs

- Level: patch. Maintainer-facing: "Before tagging" step 6 in `docs/releasing.md` now reads several runs and `tests/validation/job_seconds.json` records `runs` in place of `run`.
- Contract: none
- User action: none; the maintainer's release step changes, and nothing changes for an installation.
- Pull request: #663

### The snapshot runtime checks each blob and source hash in one place

- Level: patch, a refactor with no behavior change.
- Contract: none
- User action: none; behavior is unchanged.
- Pull request: #662

### Skills find tools where check does, and a supported pdftotext behind Git's bundled one is used

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none; a tool only in WinGet's Links folder now runs in the skills too, and with Git Bash's bundled pdftotext ahead on PATH, a supported pdftotext further down is used and check names the bundled copy as skipped, or the refusal names every copy found when none is supported.
- Pull request: #660

### A validation job ended before its first line is reported as that termination and started once more

- Level: patch, a fix to the contributor validation runner with no contract change; a contributor now reads a `RETRY` line and a `RETRIED` block, or a `FAILED` block that names the termination status and command, where the run said `Command failed with exit code 2147483652`.
- Contract: none
- User action: none
- Pull request: #661

### This repository's reviewer asks for a test for each changed function no test names

- Level: patch, a new route in a repository skill that does not ship.
- Contract: none.
- User action: none.
- Pull request: #659

### This repository's reviewer checks the documents that name a changed script

- Level: patch. A repository skill's reviewer gains a route; nothing that ships changes.
- Contract: none
- User action: none
- Pull request: #657

### A condition script can read the diff and hand its specialists a file, and a specialist can share its files

- Level: minor. Additive: a specialists manifest may set `shares_files`, and a condition script finds the diff in `CODE_REVIEW_DIFF_FILE` and may write `CODE_REVIEW_CONDITION_OUTPUT` for the specialists it opens; existing manifests and scripts run unchanged.
- Contract: `docs/code-review-operations-contract.md` (the reviewer manifest's specialist, `uncovered`, and condition rows)
- User action: none
- Pull request: #656

### review-prs prepare starts fewer git processes

- Level: patch, an efficiency fix with no contract change.
- Contract: none
- User action: none
- Pull request: #649

### A condition script or Copilot CLI run no Windows job object can hold fails the review instead of running without one

- Level: patch, a defect fix that makes the code keep the guarantee the contract already states.
- Contract: none
- User action: none; a review whose condition script or Copilot CLI run Windows will not put in a job object now ends with `FAILED` and `Condition script could not run: <script>: it could not join a Windows job object: ...` or `GitHub Copilot CLI could not run: ...`, naming the call that failed, and should be run from a session whose job allows another inside it.
- Pull request: #651

### The hub guard applies inherited and exported git location variables

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none; with the hub guard on, a git command run while `GIT_DIR`, `GIT_WORK_TREE`, or `GIT_COMMON_DIR` is inherited or exported is now judged by that variable, so one naming another repository is allowed with a note instead of judged by the working directory.
- Pull request: #650

### A tool that hangs stops the run at a deadline

- Level: patch, a new controlled failure with no contract change.
- Contract: none
- User action: none; a validator or git that hangs now ends the run after the deadline `TOOL_DEADLINE_SECONDS` in `deployer/platform_support.py` sets, refusing before any change or rolling back after one, as "A tool that does not finish" in `docs/recovery.md` describes.
- Pull request: #648

### PDF review finds a pdftotext in WinGet's Links folder, and names Xpdf versions as Xpdf does

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none; a supported `pdftotext` only in WinGet's Links folder, which `deploy.py check` already reported, is now used for PDF review too, and the refusal and `check` name Xpdf versions as `4.00` to `4.06`.
- Pull request: #646

### The command policy reports a command whose program it cannot read

- Level: patch, a validation policy that ships nothing; deployed skills behave as before.
- Contract: none
- User action: none. A contributor whose script starts a command whose program validation cannot read names it with a literal or a lookup, or states why beside the code in the module's `COMMANDS_ALLOWED`.
- Pull request: #643

### Repository reviewers load with fewer git processes, and an oversized base document is refused before it is read

- Level: patch, a fix with no contract change: reviewer files are read in batches by blob id, and a base document over the changed-file limit is refused by its size without being read.
- Contract: none
- User action: none; a changed document whose base version is over the changed-file size limit still shows every line as added, with the same note.
- Pull request: #641

### A stopped recovery says whether each interrupted run was committed

- Level: patch, a report fix with no contract change.
- Contract: none
- User action: none; when recovery stops, the report now says for each interrupted run whether it was committed to the manifest (installed) or not (rolled back by the next deployment), instead of that nothing was deployed.
- Pull request: #635

### Closing a stopped streaming command no longer waits up to ten seconds on a program holding its output

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none; a skill whose streaming command is stopped while a program it started holds its output now returns about a second later instead of up to ten.
- Pull request: #637

### Validation finds more platform-specific code and archive writes

- Level: patch. Validation only: the platform and filesystem-write policies find forms they missed; nothing a user deploys or runs changes.
- Contract: none
- User action: none
- Pull request: #638

### A transient GitHub failure on a read is retried, and one on a change is reported as an unknown outcome

- Level: patch, a resilience fix with no contract change.
- Contract: none
- User action: none; a skill's read that meets a brief GitHub 502, 503, 504, or dropped connection now retries a few times before it fails with `(gave up after N retries)`, and a request that may have changed GitHub fails as `unknown_outcome`, which asks you to check GitHub before repeating it.
- Pull request: #636

### The repository reviewer routes host, record, process, and PDF code to the trust-boundary specialist

- Level: patch, a repository skill change that ships to no installation.
- Contract: none
- User action: none
- Pull request: #633

### Remaining whitespace flattening in skill scripts uses the shared helper

- Level: patch. An internal refactor; the printed output is unchanged.
- Contract: none
- User action: none
- Pull request: #634

### A regression suite that runs git inherits the shared test base

- Level: patch, a test-infrastructure change that ships no behavior.
- Contract: none
- User action: none. A contributor whose Python suite runs git, itself or through a module it imports, bases its test classes on `git_test_base.GitTestCase` from skill-core; validation fails otherwise.
- Pull request: #632

### A script that fails unexpectedly ends with a FAILED line and exit 1 instead of a traceback

- Level: patch, a fix that brings every entry point under the script-results contract it already promised.
- Contract: the skill scripts' `FAILED` status line and exit codes ("Script results"), now also printed for an unhandled error and, with exit 130, for Ctrl+C
- User action: none; a script that fails in a way it did not foresee now prints `FAILED <type>: <message>` and exits 1 instead of a traceback, and Ctrl+C prints `FAILED interrupted` and exits 130.
- Pull request: #631

### Malformed files report one error shape

- Level: patch. Validators report a field of the wrong kind in one shape and refuse a few values they accepted by mistake; no format changed.
- Contract: none
- User action: none; a malformed configuration, record, flag store, state file, fixture, or reviewer manifest now prints `FAILED <path> must be <expected>, found <found>`, naming the field from its file's root, such as `config.repositories.owner/repo.reviewer.protocol_version must be an integer, found boolean true`; correct that field.
- Pull request: #630

### The duplication policy also compares module-level constants and compiled patterns

- Level: patch, a validation policy with no change an installation meets.
- Contract: none
- User action: none
- Pull request: #628

### The design document separates what holds today from what the port adds

- Level: patch. Documentation only.
- Contract: none
- User action: none
- Pull request: #626

### Contributor guidance describes what CI runs on a pull request

- Level: patch, contributor guidance only.
- Contract: none
- User action: none
- Pull request: #625

### The contract states how each reviewer prompt reaches the review comments and defines dispositions_only for closed conditions

- Level: patch. Documentation of existing behavior; the record format is unchanged.
- Contract: `docs/code-review-operations-contract.md` (the review-comment threat-model row and the `dispositions_only` field definition)
- User action: none
- Pull request: #627

### SECURITY.md names the stop at an installation pinned with --at

- Level: patch, a documentation correction with no contract change.
- Contract: none
- User action: none, unless the installation was deployed with `deploy.py --at`; it receives no fixes until you start `update-coding-agent-skills` with `--unpin`, or update the clone and run `python deploy.py --all --unpin`, and a vulnerability reporter does one of those first.
- Pull request: #624

## v0.6.0

Level: minor.

### The Copilot CLI host finds the program past an npm shim on PATH

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none
- Pull request: #620

### A deployment without --at refuses to replace a pinned deployment until it passes --unpin

- Level: minor. A new refusal and a new `--unpin` option on `deploy.py`: over a deployment made with `deploy.py --at`, a deployment without `--at` stops before it changes anything unless it passes `--unpin`.
- Contract: none
- User action: none, unless the installation was deployed with `deploy.py --at`; to move it back to the clone's `HEAD`, run `python deploy.py --all --unpin` from the clone, or start `update-coding-agent-skills` with `--unpin` when it stopped with `PINNED`.
- Pull request: #622

### A skill the deployer leaves in place keeps its runtime adapter

- Level: patch. A fix: a preserved skill, and a skill kept because a preserved skill needs it, keep their runtime adapters under `~/.agents/skills` instead of losing them as obsolete.
- Contract: none
- User action: a user whose kept or preserved skill lost its adapter in an earlier deployment redeploys that skill once to restore it.
- Pull request: #621

### The skill evaluation judges reviewers on exact model IDs

- Level: patch, a correction to a manual development gate that ships nothing.
- Contract: none.
- User action: none.
- Pull request: #605

### The design document records the port's platform decisions and the repository's direction

- Level: patch. Documentation only: the design document gains decisions and a direction section; nothing changes for an installation.
- Contract: none
- User action: none
- Pull request: #603

### Condition scripts and Copilot CLI are bounded in stdin, output, and descendants

- Level: patch. A fix: a specialists manifest's condition script reads no stdin and runs with prompts turned off, a condition's failure detail keeps at most 64 KiB of each stream and Copilot's diagnostic at most 1 MiB, each naming what it cut, and every process either starts is ended with it on exit or timeout, so a timeout no longer waits on a process holding its output.
- Contract: none
- User action: none
- Pull request: #601

### Pull request titles carry a type, and validation is CI's job

- Level: patch. A contributor workflow change: validation holds pull request titles to a typed prefix, and local runs are targeted while CI runs the full suite; nothing changes for an installation.
- Contract: none
- User action: none
- Pull request: #602

### The merged-pull watermark is the UTC date

- Level: patch. A fix: the batch date and every merge and update date the watermark is compared with are UTC dates, so a pull request merged later on the UTC day of a batch run that found nothing pending stays eligible.
- Contract: `docs/code-review-operations-contract.md`
- User action: none; east of UTC a pull request merged late in the UTC day after an earlier batch run may have been skipped, and `/review-prs --pull <owner/repo#number>` reviews it.
- Pull request: #596

### update-coding-agent-skills stops at a deployment pinned with deploy.py --at until you pass --unpin

- Level: minor. A new status line, `PINNED <ref> <commit>`, and a new argument, `--unpin`, for `update-coding-agent-skills`.
- Contract: the `update-coding-agent-skills` status lines and arguments
- User action: none, unless the installation was deployed with `deploy.py --at`; then the update stops with `PINNED <ref> <commit>` and changes nothing, and to move back to `main` start it again with `--unpin` or run `python deploy.py --all` from the clone.
- Pull request: #600

### The deployer needs Python 3.11.4 or newer

- Level: minor. A raised tool floor, which would be major after 1.0.0: `deploy.py` refuses a Python older than 3.11.4, the first 3.11 release whose `tarfile` has the extraction filters `deploy.py --at` uses, and `python deploy.py check` reports one as `OUTDATED`.
- Contract: `deployer/tools.py`
- User action: none on Python 3.11.4 or newer; otherwise update Python before the next deployment, as "Installing the tools" in docs/installation.md says.
- Pull request: #598

### deploy.py --at refuses a link or an unsafe member by name

- Level: patch. A fix: a commit whose tree holds a symbolic link, a hard link, or a special file, or a member the extraction filter refuses, is refused with an `ERROR:` line naming the member and why, before anything is deployed, instead of ending in a traceback.
- Contract: none
- User action: none
- Pull request: #598

### The Copilot CLI review host reads its prompt on standard input and refuses an npm shim

- Level: patch. A fix: the Copilot CLI host gives Copilot its prompt on standard input instead of as the `--prompt` argument, and refuses a `copilot.cmd`, `.bat`, or `.ps1` found on `PATH`, falling back to WinGet's `copilot.exe` or failing with an error that names the path and says to install the program from WinGet or the GitHub release.
- Contract: none
- User action: update and redeploy so the host picks up the change; a user whose Copilot CLI came from npm installs it from WinGet (`winget install GitHub.Copilot`) or the GitHub release before the next Copilot CLI review.
- Pull request: #599

### A boolean, decimal, or container schema version is refused in review-insights and repo-cleanup

- Level: patch, because the readers now enforce the documented integer versions; no format or version changes.
- Contract: none.
- User action: none.
- Pull request: #597

### An atomic code-review write closes its descriptor once when it fails

- Level: patch. A fix: a code-review state or record write that fails while writing, syncing, or replacing no longer closes its temporary file's descriptor twice, which could close an unrelated file another thread had just opened.
- Contract: none
- User action: none
- Pull request: #595

### Generated release sections state their overall upgrade level

- Level: minor. Additive output: the release-note tool writes the largest documented entry level below the version heading, or an explicit no-entry summary, and refuses unreadable entry levels before writing.
- Contract: none.
- User action: none. Published sections stay unchanged; entry explanations and migration actions remain authoritative.
- Pull request: #558

### Invalid pending merge dates report the standard review-operation error

- Level: patch. A fix: `advance` reports `FAILED Pull <number> has invalid mergedAt` for malformed pending merge dates instead of an uncaught date-parser error, leaving state unchanged.
- Contract: none.
- User action: none.
- Pull request: #556

### Shared reviewers are told the existing repeat dispositions and permitted read roots accurately

- Level: patch. Correct contradictory guidance without changing accepted results or the enforced boundary.
- Contract: none.
- User action: none.
- Pull request: #545

### PDF review requires a supported, identifiable Xpdf or Poppler parser at runtime

- Level: minor before 1.0.0: raised tool floors and enforced extraction refusals would be major after 1.0.0; `check` also adds the `UNUSABLE` report group.
- Contract: `deployer/tools.py` (the provider-specific tool requirements), `skills/skill-core/scripts/pdf_tool.py` (Xpdf 4.06 and Poppler 26.10.0 maintained-release floors), and `docs/code-review-operations-contract.md` (runtime PDF probe/refusal guarantees; existing snapshot and record formats remain unchanged).
- User action: for PDF review, install or upgrade a trusted Xpdf or Poppler command-line parser to its provider's floor and put that directory before any older Git-bundled parser on PATH. Run `python deploy.py check` in the reviewing environment. No action is needed for reviews without PDFs; refused PDFs are explicit coverage gaps, never silent skips. Version gating is not sandboxing or a guarantee against native parser exploits. No package manager on Windows currently delivers a parser that meets either floor (Poppler from WinGet is 25.07.0 against 26.10.0, and Git's bundled Xpdf is 4.0 against 4.06), so PDF review needs Xpdf installed from its publisher and placed ahead of Git's `bin` directory on PATH until packages catch up.
- Pull request: #542

### Evaluation guidance follows the evaluator's authoritative options

- Level: patch. A guidance fix: the repository evaluation skill uses a generic model-name hint, refers to the evaluator's help for accepted names and defaults, and describes concurrency and per-session timeouts rather than a dated measurement.
- Contract: none.
- User action: none.
- Pull request: #541

### New review reports display supplied prose literally

- Level: patch, because this fixes report rendering without changing record formats or review decisions.
- Contract: none.
- User action: update the deployed skills to use the literal projection for newly written reports. Existing reports are left unchanged; supplied Markdown/HTML now displays as text rather than formatting or active content.
- Pull request: #538

### Copilot entrypoint reviewers share the prepared input and result rules

- Level: patch, because the host now follows the existing reviewer protocol without changing arguments, configuration, or stored formats.
- Contract: none.
- User action: update and redeploy the code-review operations bundle to pick up the host prompt; no configuration migration is needed.
- Pull request: #537

### Nested run-like source directories remain readable within the claimed review

- Level: patch. Fix a legitimate source-read denial by keeping the claimed review root authoritative rather than interpreting source directory names as run metadata.
- Contract: none
- User action: none
- Pull request: #536

### Guarded command values refuse smart double quotes

- Level: patch. Tighten the existing conservative quoted-value rule to reject PowerShell smart double-quote delimiters without changing the Bash-only guarded tool surface or the existing terminal-backslash rejection.
- Contract: none
- User action: none, unless manually supplying a source path or pattern containing U+201C, U+201D, or U+201E; those command values are refused.
- Pull request: #536

### Review trust documentation distinguishes author bytes from trusted infrastructure and compatibility from parser safety

- Level: patch. Correct the documentation of existing fixture, PDF extraction, and release-note approval boundaries without changing behavior or requirements.
- Contract: `docs/code-review-operations-contract.md` (trust-model prose only; no format table changes).
- User action: none. PDF parser policy changes remain deferred pending verified provider and version evidence.
- Pull request: #535

### Malformed review configuration and reviewer manifests fail without tracebacks

- Level: patch, because invalid inputs now follow the existing error contract without changing valid formats.
- Contract: none.
- User action: none; correct malformed fields to the documented types before retrying.
- Pull request: #534

### Legacy reviews with missing or non-string verdicts are offered for an initial review instead of crashing

- Level: minor, because requiring a string narrows a durable-data contract before 1.0.0; valid string legacy indexes remain readable.
- Contract: `docs/code-review-operations-contract.md`.
- User action: none for valid migrated reviews. For an affected pull request, run `review-prs --pull owner/repo#N` to create an initial structured review rather than `--re-review`; no stored index or report is rewritten.
- Pull request: #533

### Repository audit brief-write failures report FAILED instead of tracebacks

- Level: patch. A fix to the documented expected-error behavior for temporary-directory allocation and brief writes.
- Contract: none.
- User action: none. If a brief write fails, its FAILED reason names the path; remove the retained temporary directory after diagnosing the failure.
- Pull request: #532

### Coverage notes name repository-configured snapshot exclusions

- Level: patch. A report-text fix: **Not reviewed in full** now names the repository's `snapshot_exclude` alongside size, safety, and document-extraction causes; coverage and verdict behavior are unchanged.
- Contract: none
- User action: none
- Pull request: #531

### One prepared pull's dispatch-write failure no longer hides the others

- Level: patch, because this fixes failure isolation and cleanup without changing invocation or stored formats.
- Contract: none; restores the documented `FAILED <selector> <reason>` and independent-pull behavior.
- User action: none. When cleanup also fails, inspect or delete the unpublished run directory named in its failure.
- Pull request: #530

### The hub guard accepts attached upstream values without treating them as rename flags

- Level: patch. A fix: `git branch -uorigin/main` is allowed like `-u origin/main`; value boundaries are respected while actual `-m`, `-M`, and `--move` forms remain refused.
- Contract: none.
- User action: none.
- Pull request: #529

### Invalid include names and refused canary-home values exit as usage errors

- Level: patch. A fix to the documented CLI behavior: these invalid values now exit 2 with the standard `--help` diagnostic, before claiming the home or recovering a pending deployment.
- Contract: none.
- User action: none; scripts distinguishing exit statuses should treat these refusals as command-line errors rather than deployment failures.
- Pull request: #528

### The branch-protection check distinguishes missing issue configuration from read failures and accepts YAML comments

- Level: patch. Fixes incorrect exit codes for absent issue-template configuration and false drift reports for a whitespace-separated comment after `blank_issues_enabled: false`.
- Contract: none
- User action: none
- Pull request: #527

### Concurrent flag resolution preserves the first resolution and reports already resolved

- Level: patch. Fixes `flag-review-finding resolve` reporting `FAILED` when another caller resolves the flag first; it now prints `ALREADY_RESOLVED` and preserves the earlier resolution and timestamp. Insight decisions likewise report competing resolutions without claiming them in their histories. A blank resolution is now a validation failure for an already-resolved flag too: where v0.5.0 printed `ALREADY_RESOLVED` and exited 0, it now prints `FAILED Resolution is required` and exits 1, as it already did for an open flag.
- Contract: none.
- User action: none.
- Pull request: #526

### Review artifact hashes must be full SHA-256 digests

- Level: patch, fixing enforcement of the existing documented rule without changing a format.
- Contract: none.
- User action: None for records generated by the pipeline, which already writes full SHA-256 digests. Hand-built records or prior-review fixtures with invalid artifact hashes must supply the correct payload/report SHA-256 digests rather than commit hashes.
- Pull request: #525

### Invalid force-item names are refused before deployment recovery

- Level: patch. Fix the documented refusal behavior: an unknown `--force-item` exits 2 before the lock, startup recovery, canary-home claim, or selection prompt, with accepted names and the help hint, rather than reporting a failed deployment. Known names still have to belong to the selected run.
- Contract: none.
- User action: none.
- Pull request: #524

### Malformed review versions and repository paths are refused

- Level: patch, because validators now enforce the existing documented integer and repository-relative path contracts; no supported format or version changes.
- Contract: none.
- User action: none for valid suite-generated or hand-written files. If a hand-written file used `true` or a decimal version, replace it with its documented integer. If a repository-relative field starts with a Windows drive, replace it with the intended repository-relative POSIX path before retrying. There is no automatic coercion or migration of malformed data.
- Pull request: #523

### Memory indexes preserve entries whose Markdown targets contain parentheses

- Level: patch, because this repairs existing link preservation and makes generated targets round-trip without changing commands or report formats.
- Contract: none.
- User action: none.
- Pull request: #522

### Repository-code declarations use the same process reader as command validation

- Level: patch. A validation fix: `RUNS_REPOSITORY_CODE` is no longer rejected for supported `os`, `asyncio`, or skill-core client calls. Unused process imports no longer keep a stale declaration current, and unresolved callable aliases or unreadable f-string command prefixes fail explicitly.
- Contract: none.
- User action: none for existing shipped skills; contributors should remove stale declarations only when the script starts no process, and retain declarations for scripts that execute target-repository code.
- Pull request: #521

### Document evaluation base commits do not run user Git hooks

- Level: patch. A fix: throwaway document-base setup overrides global and template-installed hooks with an empty temporary hook directory; signing remains disabled.
- Contract: none
- User action: none
- Pull request: #520

### Interrupting recovery of a failed deployment releases the lock and keeps the journal

- Level: patch. A fix to documented behavior: Ctrl+C during immediate rollback or committed-run recovery exits 130 without a traceback unless debug output was requested; unexpected recovery exceptions are reported and exit 1. Both release the lock and retain the journal for a safe retry.
- Contract: none.
- User action: none; rerun the deployment to finish recovery.
- Pull request: #519

### Case-variant memory indexes are no longer audited as memories or added to themselves

- Level: patch. A fix: Windows stores whose index is named `memory.md` or `Memory.md` now exclude that file from memory enumeration consistently in audit and reindex.
- Contract: none
- User action: none
- Pull request: #518

### Contributor validation reads f-string commands instead of ignoring them

- Level: patch. Fixes the declared-tool and shared-client command-policy bypass for Python f-strings.
- Contract: none.
- User action: contributors whose scripts fail the corrected checks must keep the program name literal, declare nonstandard tools, and run Git/GitHub commands through skill-core's shared clients. No action for installed skills.
- Pull request: #517

### Repeated evaluation scenario names count once

- Level: patch. A fix: repeated `--scenario` options now select each scenario once in first-occurrence order, preventing duplicate records output and temporary-directory collisions during model runs.
- Contract: none
- User action: none
- Pull request: #516

### Memory audits recognize alternative link spellings consistently with index rebuilding

- Level: patch. A fix: `./rule.md` and Windows case-only `RULE.md` links identify `rule.md` in both audit and reindex, instead of causing false unindexed findings or missed duplicates. Reports keep the original target spelling.
- Contract: none
- User action: none
- Pull request: #515

### Validation resolves filesystem and command calls through simple assignment aliases

- Level: patch. Fixes validation missing calls such as `writer = open; writer(path, "w")` and `runner = subprocess.run; runner("git status")`, and reports ambiguous callable aliases instead of guessing.
- Contract: none.
- User action: none for installed skills. Contributors whose validation reports a callable alias must use a single resolvable binding and route writes or commands through the existing approved helpers.
- Pull request: #513

### Closed specialist conditions no longer reopen intentionally skipped review scope

- Level: patch. Fixes generic fallback and ignored-file coverage to honor the documented specialist matching behavior.
- Contract: `docs/code-review-operations-contract.md`
- User action: none.
- Pull request: #514

### Preserved skills retain their deployed skill dependencies

- Level: minor. Before 1.0.0 this carries a change that would be major: the ownership manifest advances to schema 8, which older deployers refuse. Supported schema 6/7 manifests migrate automatically at the atomic commit; recorded `skill_deps` retain the transitive closure, shared assets, and agents.
- Contract: `deployer/manifest.py`
- User action: none for an ordinary update. Use the updated deployer after migration. Missing historical edges conservatively keep other same-source skills until an unmodified or forced selected deployment refreshes exact edges. Redeploy the source if an earlier deployment already removed dependency files; migration does not recreate them.
- Pull request: #512

### PowerShell syntax validation includes uppercase and mixed-case extensions

- Level: patch. A fix: deployment now parses rendered `.PS1` and `.Ps1` files as it already parsed `.ps1`, rejecting malformed scripts before installation.
- Contract: none
- User action: none for valid sources; if deployment now reports a syntax error in a previously unchecked file, fix that script in its source and redeploy.
- Pull request: #511

### Community guidance is available locally

- Level: patch, because the change clarifies documentation and makes the existing default policy available in clones.
- Contract: none.
- User action: none.
- Pull request: #509

### `deploy.py --at <ref>` deploys a release or earlier commit without moving the clone

- Level: minor. A new deploy option.
- Contract: none
- User action: none; to step back from a regression, run `python deploy.py --all --at <tag-or-commit>` from the clone on `main`, which deploys that commit's skills with the current deployer and leaves the clone's `HEAD` where it is (`check` names the pin). The pin stays until you lift it: a deployment without `--at` refuses to replace it, and `update-coding-agent-skills` stops with `PINNED` and changes nothing, until either is given `--unpin` (`python deploy.py --all --unpin` from the clone, or the update started again with `--unpin`), which replaces the pinned deployment and reports `Replaced the deployment pinned with --at <ref>`, as the entries for #600 and #622 say.
- Pull request: #507

### The README names the project Coding Agent Skills and states the platform without a version

- Level: patch. Documentation only: the README's title and its platform sentence change wording, and no behavior changes.
- Contract: none
- User action: none
- Pull request: #503

### The code-review contract's failure line, inline seal, manifest rule, and coverage row match the code

- Level: patch. A documentation fix that states what the code already does; no behavior changes.
- Contract: `docs/code-review-operations-contract.md` now says a `check` or `finalize` failure names its run directory (threat model), the inline seal also skips a fixture's throwaway repository and `recorded.json` (inline reviewer row), a reviewer manifest names no file twice in a list and gives no file two roles (reviewer manifests), the suite profile catalogue is named without its members, and `coverage.unavailable_sources` also holds changed documents whose text extraction was refused (request coverage row).
- User action: none
- Pull request: #504

**Deferred.** The v0.6.0 release audit deferred its drift and polish findings to v0.7.0: #609, #610, #611, #612, #613, #614, #615, #616, #617, #618, and #619, and the comments it added to #567, #576, and #579.

## v0.5.0

### A status override of `incomplete` is refused as a computed state

- Level: patch. A fix: `incomplete`, the AI review state of an unchanged head whose review could not read every changed file, is refused as a dashboard status override, in any case and trimmed, as `missing`, `current`, and `stale` already were; a configuration that holds one fails every command that reads it with `FAILED Dashboard status override <owner/repo#N> duplicates a computed tracker state`.
- Contract: `docs/code-review-operations-contract.md`
- User action: none, unless that message names an override; then rename or remove that entry in `dashboard.status_overrides` in the code-review configuration file by hand, since `tracker_pipeline.py override --clear` validates the file first and refuses it too.
- Pull request: #450

### A Word finding's location names the nearest styled heading, never a body paragraph that starts with #

- Level: patch. A fix: a body paragraph starting with `#`, such as `#1 priority is latency.`, is no longer taken for a heading in a later finding's `location`, which names the nearest styled heading above the line; in the extracted text such a paragraph now starts with `\#`, as Markdown escapes it.
- Contract: `docs/code-review-operations-contract.md`
- User action: none
- Pull request: #452

### The code-review contract names review-document's behavior

- Level: patch. The contract gains a Skill behavior row for `review-document` stating what the skill already does, and its opening names the bundle's public skills by that table rather than counting them; no behavior changes.
- Contract: `docs/code-review-operations-contract.md`
- User action: none
- Pull request: #453

### A review-insights synthesis no longer lists a suite profile as a repository's guidance file

- Level: patch. A fix: a reviewer whose manifest routes a specialist to `suite:design-review` no longer makes the synthesis list that reference among the repository's guidance files, and a recommendation targeting it is refused as any other path outside the set is.
- Contract: none
- User action: none
- Pull request: #451

### source-search no longer prints a changed Word document's bytes as matches

- Level: patch. A fix: `source-search` searched the zip of a Word document the pull request changes and printed its compressed bytes as `MATCH` lines; it now leaves out every document the snapshot holds as extracted text, as the contract already said.
- Contract: none
- User action: none
- Pull request: #449

### Several changes around a long run of identical paragraphs diff as those changes alone

- Level: patch: a fix to match the documented behavior; two or more paragraphs changed in a Word document or PDF around a run of identical paragraphs or table rows, such as a form edited at both ends or a table of identical rows with some rows filled in, now diff as those lines alone (before, the whole run showed as removed and added once it passed 200 lines), so the reviewer reads only the changes and a re-review's scope is those changes; this lifts the known limit the previous release's note named, and only a pair whose fewest changed lines take more than two million steps to find, such as thousands of changed rows in one long table, is still matched as before.
- Contract: none
- User action: none
- Pull request: #407

### A repetitive document's diff and re-review scope show only the change made

- Level: patch: a fix to match the documented behavior; one paragraph inserted, deleted, or changed in a Word document or PDF whose other paragraphs repeat, such as a form or a table of identical rows, now diffs as that one line at any length (before, every line on one side of it, all of them above 200 lines), so the reviewer reads only the change and a re-review's scope is that change; a known limit remains: two or more changes around a run of more than 200 identical lines still show the whole run as changed, as before.
- Contract: none
- User action: none
- Pull request: #406

### review-document refuses an --output that is not empty, never replaces a report.md, and refuses an edit that only renumbers the same lines of text

- Level: patch: a fix to match the documented behavior; `build --output DIR` is refused when `DIR` exists and is not an empty directory (before, only one holding `fixture`), `report` refuses to replace a `report.md` already beside the fixture, and a Word document or PDF whose edit changes no line of text but moves its labels, such as an inserted empty paragraph, is refused as unchanged text instead of reviewed on a diff with no hunk.
- Contract: none
- User action: none. Name a new or empty `--output`, or omit it for a new temporary directory; pass `--base none` to review a document whose text did not change.
- Pull request: #403

### A PDF's page and line labels no longer depend on which pdftotext is on PATH

- Level: patch. The snapshot runs `pdftotext` with `-raw`, so Xpdf (bundled with Git for Windows) and Poppler both read a PDF one line per line of text, in the order the PDF draws its text; before, Xpdf joined a paragraph's lines into one, so a finding's `page <p>, line <l>` differed by tool. The contract's snapshot paragraph and threat-model row now say so, including where the two can still differ (a line whose pieces are drawn apart, such as a raised suffix).
- Contract: `docs/code-review-operations-contract.md`
- User action: none
- Pull request: #387

### review-document reviews a PDF from its path, as the text the optional pdftotext extracts

- Level: minor: an addition; `review-document` accepts a `.pdf` it refused before, reviews it through the design-review specialist, and reports each finding's place as `page <p>, line <l>`, as a pull request's review of a PDF already does.
- Contract: none
- User action: none. A PDF needs `pdftotext` on `PATH`, which Git for Windows bundles and Git Bash puts there; without it `review-document` refuses the PDF and names the tool.
- Pull request: #386

### A pull request's changed PDF is reviewed as the text an optional pdftotext extracts, and its review is INCOMPLETE without it

- Level: minor: an optional tool and an extracted format are added, the snapshot manifest and the finding `location` gain values a previous record never held, and a record written before stays valid.
- Contract: `deployer/tools.py` (the new `pdftotext` floor, 4.0) and `docs/code-review-operations-contract.md` (the snapshot's `extracted` format `pdf`, its text held in `source-snapshot-text`, and the `location` form `page <p>, line <l>`).
- User action: none. Git for Windows bundles a `pdftotext` that Git Bash puts on `PATH`; `python deploy.py check` reports it, and without it a review of a changed PDF is `INCOMPLETE` and names the tool.
- Pull request: #379

### review-document reviews a Word document from its path

- Level: minor. Additive: `review-document <path>` accepts a `.docx`, which it refused before, and reviews it as its extracted text through the design-review specialist, reporting each finding's place in the document; a Word document whose text cannot be extracted, has no text, or whose edit leaves its text unchanged is refused by `build` with a `FAILED` line naming why. Text files behave as before.
- Contract: the `review-document` argument `<path>`, which accepts a `.docx`, and its `SKILL.md` statements on routing and on reporting a finding's location.
- User action: none
- Pull request: #378

### analyze-skill-cost reports grants that match no command, and grants in a hidden skill

- Level: minor. A new output line: `skill_inventory.py tools` prints `IDLE_ALLOWED <entry>` for a scoped Bash or PowerShell pattern that matches no command in the skill's shell fences or code spans, and `HIDDEN_ALLOWED` for a hidden skill with any `allowed-tools`. The audit reports both as MUST FIX.
- Contract: none
- User action: none. A skill audited before this release may now show these MUST FIX findings: remove the grant, or show the command it is for.
- Pull request: #375

### A pull request's changed Word document is reviewed as its extracted text

- Level: minor: the snapshot manifest, its exclusion reasons, and the review record gain optional, additive items, and a record written before stays valid.
- Contract: the format tables in `docs/code-review-operations-contract.md`: `record.findings[].location`, the snapshot manifest's `extracted` object, and the `unextractable` exclusion reason, a coverage gap.
- User action: none. A review of a pull request that changes a `.docx` can now report findings in it, and is `INCOMPLETE` when its text cannot be extracted.
- Pull request: #374

### A repository entrypoint reviewer starts from one self-contained prompt

- Level: patch. Prompt wording: the entrypoint reviewer's prompt states the request's facts and the adapter result's contract inline, with the same input rules as a specialist's prompt, and the Copilot CLI host's prompt names the diff and the description untrusted. No record, configuration, or manifest format changes.
- Contract: none
- User action: none
- Pull request: #371

### Reviewers are given the pull request's description, and records keep how much of it they saw

- Level: minor. Additive: a new reviewer input, `PULL_REQUEST_BODY_FILE`, named in every prompt; the adapter request's `pull_request` gains `body_file`, `body_characters`, and `body_given`; the review record's `pull_request` gains the optional `body_characters` and `body_given`, which older records lack and stay valid without; a fixture's `pull.json` takes an optional `body`; and `validate-reviewer` prints a `BODY` line after each `PULL` line.
- Contract: `docs/code-review-operations-contract.md` format tables (adapter request, record, fixture pull request)
- User action: none. A repository entrypoint reviewer may read the request's `body_file` as untrusted data.
- Pull request: #366

### analyze-skill-cost measures the prompt a skill's script writes for a subagent

- Level: minor. Additive: `skill_inventory.py scan` prints new lines after each `RUNTIME_PROMPT`: `RUNTIME_PROMPT_SAMPLE`, `RUNTIME_PROMPT_READ`, and `REPLY_BOUNDED` for a skill whose source tree keeps a rendered sample under `docs/runtime-prompts/<skill>/`, or `RUNTIME_PROMPT_UNSAMPLED` with the reason when it has none. When `analyze-skill-cost` audits `review-prs`, `review-document`, or `review-insights` from this repository, the report shows each prompt's size, its reads, and whether it bounds the reply, and the standing suggestion to audit the prompt by hand is gone. Audited outside a source tree, as with a deployed copy, it still makes that suggestion and says why.
- Contract: none
- User action: none
- Pull request: #361

### The audit-ai-config MCP handshake ends within --timeout and frames each message with a line feed

- Level: patch. Fixes: `mcp_handshake.py` bounds each server from its start to its stop by `--timeout`, plus up to ten seconds for a killed server to be reported gone, so a server whose launcher leaves a program holding its output no longer holds the run until that program ends. Each message it sends ends with `\n` alone, where on Windows it ended `\r\n`. It runs servers through skill-core's bounded process layer, whose `streaming` gains an environment parameter and a whole-session time limit; those are internal additions, and the git client's use of `streaming` is unchanged. The `HANDSHAKE_*`, `CONFIG_*`, `SKIPPED`, `NO_SERVERS`, and `FAILED` lines are unchanged.
- Contract: none
- User action: none
- Pull request: #358

### Correction to the v0.4.0 notes: audit-ai-config changed more frontmatter verdicts than block scalars

- Level: minor, the level v0.4.0 carried this at; this release changes nothing here, and the entry completes that release's notes, which stay as published. The v0.4.0 entry "audit-ai-config reports unreadable files as findings, accepts block scalars, and prints each finding's line" named block scalars only. Since v0.4.0, audit-ai-config also reads quoted scalars, flow and block lists, and nested mappings in skill and agent frontmatter as YAML reads them: a quoted value no longer keeps its quotes, a nested mapping such as `metadata` is no longer an `ERROR` on each indented line, and a block list under an agent's `tools:` is no longer an `ERROR` with "tools must not be empty". Since v0.4.0 it also reports frontmatter it cannot read as YAML as an `ERROR` at its line: a value such as an unclosed quote at the key's line, and a list or mapping line indented out of place at that line, with the message "Frontmatter must use key: value entries, block scalars, block lists, or nested mappings".
- Contract: none
- User action: none, unless CI matches audit-ai-config's frontmatter findings; expect fewer `ERROR`s for the forms above and match the new message for unreadable frontmatter.
- Pull request: #358

### The release procedure refreshes the validate legs' measured seconds

- Level: patch. Maintainer-facing: "Before tagging" in `docs/releasing.md` gains step 6, `python -B tools/record_job_seconds.py`, which rewrites `tests/validation/job_seconds.json` from the latest successful `main` validate run, committed in the release pull request. Nothing changes for an installation.
- Contract: none
- User action: none. A maintainer cutting a release runs the new step.
- Pull request: #357

### A specialist in a reviewer manifest may set `model` and `effort` to null

- Level: minor. Additive: a specialist's `model` and `effort` accept `null`, meaning no setting, as an absent key does: the profile's model, else the session's, and the configuration's `reviewer_effort`, else the session's effort. `validate-reviewer`'s `ROUTE` line now shows the configuration's `reviewer_effort` for a specialist that sets no effort of its own. A previous release refuses a manifest with a null setting.
- Contract: `docs/code-review-operations-contract.md`
- User action: none
- Pull request: #355

### curate-agent-memory states that a `MEMORY.md` hook is cut to 150 characters, and dotnet-format states its `SOLUTION` line

- Level: patch. Documentation only; no behavior changes. This corrects the v0.4.0 entry that said a `>` or `|` block-scalar `description` is "read whole" in the rebuilt `MEMORY.md`. It is read past its header and through all its lines. The hook is then collapsed to one line and cut to 150 characters, as every description has been. dotnet-format's `SKILL.md` now gives the `SOLUTION <solution> <score>` line format that `resolve` already printed.
- Contract: none
- User action: none
- Pull request: #353

### audit-ai-config reports a missing manifest, an ownership marker, and an unreadable directory once, and the handshake resolves relative commands

- Level: minor, because before `1.0.0` the minor level carries changes that would be major later: finding messages a CI filter may match change. A conforming repository with a generator and no valid manifest gets one `scope` `ERROR` ("Manifest is missing, ..." or "Manifest is not a valid ai_config.py manifest, ...") instead of three "Manifest <field> differs from independently derived generator scope" errors. A missing ownership marker is reported only as `ownership` "Generated file missing embedded ownership marker"; parity's "Generated file missing ownership marker" and provenance's "Manifest-owned Copilot ... projection lacks an ownership marker" are no longer emitted. An unlistable agent directory is a `copilot-config` `ERROR` where the audit used to stop with a traceback. Markdown cells escape `|` and line breaks. The handshake resolves a command with a directory part against the server's `cwd`, so such a server may now pass, and a non-string `cwd` now fails.
- Contract: `audit-ai-config` report findings (the `scope`, `parity`, `provenance`, `ownership`, and `copilot-config` messages above) and Markdown findings rows; no status line (`RESULT`, `SUMMARY`, `HANDSHAKE_*`, `CONFIG_*`) changes.
- User action: none, unless CI matches the removed messages; match the `ownership` or new `scope` message instead.
- Pull request: #349

### The review-insights report and its synthesis files are stated in the behavior contract

- Level: patch. Documentation and tests: the contract's Formats section now has tables for `insights.json`, the synthesis input, context, and result, which were already written in this shape. `synthesize` reports a category finding assessment without a text reference as a problem instead of failing with a traceback.
- Contract: `docs/code-review-operations-contract.md`
- User action: none
- Pull request: #347

### A deploy command line whose options cannot run together exits 2 and names the --help to read

- Level: minor. A deployer exit code changes: `--include` without `--all`, `--take-over-source --dry-run`, `--canary-home` with `--dry-run`, `--migrate-from`, or `--take-over-source`, `--migrate-from --dry-run`, an invalid `--migrate-from` source ID or this source's own, and a `--force-item` the run does not install now exit 2 with `Run 'python deploy.py --help' for usage.`, where they exited 1 without it. The messages are otherwise unchanged.
- Contract: none
- User action: none, unless a script checks for exit 1 from one of these refusals; check for 2 instead.
- Pull request: #345

### --migrate-from refuses the options it used to ignore

- Level: minor. A deployer flag changes: `--migrate-from` with `--all`, `--include`, `--force`, or `--force-item` now stops before anything changes with a usage error (exit 2), where it migrated and silently ignored them. `--take-over-source` still combines with it.
- Contract: none
- User action: none, unless a script passes those options with `--migrate-from`; run the migration alone, then deploy with them.
- Pull request: #345

### An unreadable manifest, source.json, or metadata file is reported as unreadable

- Level: patch. A fix: a file the deployer cannot read, such as one another process holds, is reported as `Could not read ...` with its path and the operating system's reason, instead of as malformed content. The exit code stays 1.
- Contract: none
- User action: none.
- Pull request: #345

### review-insights reports are schema 8, and no recommendation ID is given twice

- Level: minor. A durable report format change with an automatic upgrade: `insights.json` gains `next_recommendation_number`, so a subject a regeneration drops never has its `REC-` ID given to another subject or a synthesized recommendation. This release reads reports of versions 1 to 7 and upgrades them when it writes them. A previous release refuses a version 8 report. Before `1.0.0` this is carried in a minor.
- Contract: none
- User action: none
- Pull request: #344

### review-insights records a decision before resolving its flags, and holds a lock while it writes a report

- Level: patch. A fix: an accepted decision is written to the report before its flags are resolved. A flag store failure partway now fails with a reason naming the flags left open, and the decision stays recorded with the flags it did resolve. Running the same `decide` or `decide-custom` again resolves the rest. Two runs on one report no longer lose each other's writes.
- Contract: none
- User action: none
- Pull request: #344

### synthesize --check refuses a recorded synthesis, and a flag outside the range names its finding

- Level: patch. A fix: `review_insights.py synthesize --check` now prints `FAILED` for a report whose synthesis is already recorded, as recording does. In the synthesis input, a `flag` line's `finding` names the finding even when its review is outside the range, and is null only for a flag that names none. A result may cite that finding.
- Contract: none
- User action: none
- Pull request: #344

### review-insights refuses a summary_root inside a skills directory

- Level: minor. A configuration that worked before can now fail: `report` refuses a `summary_root` inside `~/.claude/skills`, `~/.agents/skills`, or the suite's own skills directory, because files left there make the deployer see a skill as modified.
- Contract: none
- User action: if your code-review configuration's `summary_root` is inside one of those directories, move it elsewhere and move its reports with it.
- Pull request: #344

### audit-ai-config verdicts and output lines follow the runtimes' documented frontmatter and its references

- Level: minor, because before 1.0.0 the minor level carries changes that would be major later. Two verdicts turn a passing repository into `RESULT ERRORS`: `allowed-tools: []`, and a Codex `mcp_servers` that is not a table. The JSON `scopeStatus` stops printing internal `generator-scope-*` values, and the handshake prints `CONFIG_WARNING` for a non-table Codex entry.
- Contract: `audit-ai-config` status lines (`scopeStatus`, `CONFIG_ERROR`, `CONFIG_WARNING`).
- User action: none; CI that keyed on a `generator-scope-*` `scopeStatus` reads `manifest-declared-only` and the reason in the `scope` warning instead.
- Pull request: #341

### update-pr-tracker's override documents match what the command does

- Level: patch. Documentation and dashboard wording only; no status line, argument, or format changes. Two statements in the v0.4.0 entry "update-pr-tracker sets and clears status overrides with a validated command" are now known to be false: the command does not change "nothing else in the file" (it keeps every value but rewrites the file with sorted keys and two-space indentation, as `review_config.py write` does), and it does not clear an override whose pull request has closed on its own (an override lasts until you clear it, which works the same once the pull request has closed).
- Contract: none
- User action: none
- Pull request: #339

### Upgrade notes are written in the pull request body and generated into docs/upgrade-notes.md at release

- Level: minor. A contributor-facing change: a pull request states what it asks of a user who updates under `## Upgrade note` in its body instead of under `## Unreleased` in `docs/upgrade-notes.md`, which now holds only release sections that `tools/release_notes.py` writes before tagging. Validation reads the body in CI and the commit messages since the last tag, and fails on a non-version section of the notes file.
- Contract: none
- User action: none. Contributors with an open pull request that adds an entry under `## Unreleased` move it into the body's `## Upgrade note` after merging `main`.
- Pull request: #333

### A specialists manifest that gives a file two roles is refused

- Level: minor. Before `1.0.0` this carries what would later be major: a specialists manifest is refused with `Adapter declares a file more than once` when one repository file has two roles (a top-level resource, a specialist's profile, a specialist's own resource, or a condition script), such as a guideline listed both at the top level, where it is read from the trusted ref, and by a specialist, where it is read from the pull request's base, or when a specialist's `resources` list a file twice. Before, only a file listed twice in the top-level `resources` was refused, and the record silently took one source. Several specialists may still share a profile or a guideline, and several conditions a script. An entrypoint manifest is checked as before.
- Contract: `docs/code-review-operations-contract.md`
- User action: if `validate-reviewer` or a review now fails with `Adapter declares a file more than once`, keep the file in one role: a guideline the specialists apply in their own `resources`, a file every specialist may read in the top-level `resources`.
- Pull request: #324

### files_read no longer counts the snapshot's manifest, and the contract states the category check and adapter.name as they are

- Level: patch. A fix: a reviewer's `files_read` and `bytes_read` no longer count `source-snapshot.json` when the reviewer reads it, since the contract counts files of the snapshot and `review.snapshot.files` leaves the manifest out; earlier records may count it. The contract now says that a specialist finding's `category` must be a string even without `finding_categories`, as it has been checked since the specialist result was stated, and that `review.adapter.name` is `generic` whenever the suite's generic reviewer ran, a fallback included, and otherwise the manifest's `id`. The operations guide no longer says `snapshot_exclude` saves the tarball's download: GitHub's whole tarball is still downloaded and hashed, and exclusion saves the write.
- Contract: `docs/code-review-operations-contract.md`
- User action: none
- Pull request: #324

### repo-cleanup asks before deleting a closed pull request's branch, and a stopped cleanup's heading clears

- Level: patch. Fixes: a clean linked worktree whose pull request closed without merging is still removed, but its branch is now kept and offered on a `CONFIRM_LOCAL` line, as a local branch with no remote is, instead of deleted with the worktree; the summary lists it under `Closed PR (kept)`, and `delete-local` and `force-delete` delete it as before. A worktree whose pull request merged is removed and its branch deleted without asking, as before, and the documents now say so. A summary headed `cleanup stopped partway` now describes only the command that failed: a later `delete-local` or `force-delete` on the same plan that completes is headed `cleanup complete`, and so is `summary` after it.
- Contract: none
- User action: none
- Pull request: #326

### repo-cleanup reports every action it took, including before a failure

- Level: minor. New output lines and fixes: when a step fails partway through cleaning a repository, the sweep now prints the repository's `PLAN` and its summary of what was done before the failure, headed `cleanup stopped partway (<reason>); done before it:`, then its `ERROR`; `delete-local` and `force-delete` print the summary before a `FAILED` line the same way. A repository with no plan summary now shows `CHECKOUT switched` and its `FF_DEFAULT` line, whose values are `forwarded <sha>`, `failed <reason>`, `diverged <ahead> <behind>`, and `skipped <reason>` (`ok` is gone; a default branch already current prints none). The summary's default branch row says when the run fast-forwarded the branch or switched to it, a default branch not fast-forwarded because of changes is listed under `Fast-forward skipped`, and `quiet` now means nothing changed. `Gone with open PR` is now `Open PR (kept)` and lists every branch with an open pull request. `delete-local` and `force-delete` exit 1 when Git refuses a deletion. A sweep that cannot reach GitHub prints only `FAILED` and creates no plans directory. A worktree holding ignored files, such as a `.env`, is now preserved instead of removed with them, `release` protection matches in any letter case, and a branch name that is not UTF-8 is printed with U+FFFD instead of stopping the output.
- Contract: none
- User action: none. A worktree now preserved for its ignored files is removed by hand once they are no longer needed.
- Pull request: #307

### review-document reviews one text document from its file path

- Level: minor. A new skill in the `code-review-operations` bundle: `review-document <path> [--base none|committed] [--output DIR]` reviews a Markdown or other text document that is not in a pull request as a one-file fixture canary, sending a Markdown or plain-text document to the design-review specialist and any other text file to the generic reviewer. A file committed in a git checkout is reviewed against its committed version, so only the uncommitted change is judged; any other file is reviewed whole. It needs the code-review configuration, reads nothing from GitHub, and posts nothing. A `.docx` or PDF is refused until text extraction exists. An installation that has the bundle gets it on its next update. "Document reviews" in `docs/code-review-operations.md` describes it.
- Contract: `skills/review-document`
- User action: none
- Pull request: #321

### Review comments reach a prompt cut to 4,000 characters, and the report escapes a title and a comment as text

- Level: patch. A fix: a reviewer's prompt lists each open review comment's body cut to 4,000 characters, with its full length and `GITHUB_COMMENTS_FILE`, which holds it whole, named after the cut; a body used to reach the prompt whole, up to GitHub's 65,536 characters. The prompt also escapes the line separators U+0085, U+2028, and U+2029 inside a body. The report's **Title** row and each **Review Comments** excerpt escape every character Markdown or HTML reads as markup, so a link, an image, or a tag in a pull request's title or a comment renders as written. The threat model in the operations contract gains rows for comment bodies, the title, and the head branch's name, and every row now names a test in `test_adversarial_inputs.py`.
- Contract: none
- User action: none
- Pull request: #319

### Specialists manifests can name the suite's design-review specialist, and a fixture can carry a manifest

- Level: minor. Additive: a specialist's `profile` in a specialists manifest may now be `suite:design-review`, a profile the suite ships for design documents, read from `code-review-core`'s deployed references rather than the repository. A record whose reviewer used one names it in `review.adapter.source_hashes` under `suite:design-review`, with the SHA-256 of the file that ran; an earlier release reads such a record, but refuses a manifest that names a suite profile. A fixture's `pull.json` may name a `manifest_path` in its `base/` tree, which a fixture canary then reviews with, read from the base commit; without it the generic reviewer reviews the fixture as before. "Design documents" under "Specialist reviewers" in `docs/code-review-operations.md` shows the route and the finding categories.
- Contract: `docs/code-review-operations-contract.md`
- User action: none
- Pull request: #317

### An inline review's self-check and source commands match review-prs's grant

- Level: patch. A fix: each reviewer prompt now gives its self-check, `source-file`, and `source-search` commands with the script path spelled as the session started `prepare`, `${CLAUDE_SKILL_DIR}/../code-review-core/scripts/` filled in, instead of the resolved path. In Claude Code, an inline role's commands now match `review-prs`'s grant rather than asking each time. Writing a role's result still asks, because no skill grant can name the system temporary directory. "Inline reviews" in the operations guide gives the allow rule a headless session needs.
- Contract: none
- User action: none
- Pull request: #316

### source-search judges binary files by their bytes, not by the checkout's attributes

- Level: patch. A fix: on a lazy snapshot, `source-search` let the configured checkout's `.gitattributes`, `.git/info/attributes`, or `core.attributesFile` decide what was binary, so a text file one marked `-diff` or `binary` was never searched and a binary file one marked `diff` was. It now searches every file of the head as text and keeps a match only in a file the snapshot's own test (no NUL byte in the first 8,000 bytes) finds text, testing a file it lists to fetch the first time that file matches. Its output is unchanged, and no Git floor is added.
- Contract: none
- User action: none
- Pull request: #315

### validate-reviewer measures the snapshot route prepare takes and reads the reviewer where prepare does

- Level: minor. New output fields, a new flag, and fixes: `validate-reviewer` now measures the source snapshot on the route `prepare` would take, which its `SNAPSHOT` line names as `source=checkout-lazy` or `source=checkout` beside a new `fetchable=<n>` count. On the lazy route only the files `prepare` writes (the changed files, the analyzer settings, and the paths declared `reads` match) count against the 256 MiB size limit, and every file it lists counts against the file-count limit, so a repository whose unchanged files pass 256 MiB is no longer refused while `prepare` accepts it. For each `--pull` it now reads the reviewer through `prepare`'s fallback, from the default branch's tip or as the suite's generic reviewer when the base predates the review skill, instead of failing, and its `PULL` line ends with `reviewer=<source>`, the `review.adapter.source` the record would name, followed by `prepare`'s `NOTE` on the fallback. `--host`, as on `prepare`, names the runtime that decides an `auto` runtime, since an entrypoint reviewer on the Copilot CLI host gets a whole snapshot.
- Contract: `docs/code-review-operations-contract.md`
- User action: none
- Pull request: #313

### On Codex, review-prs notes a reviewer's model instead of asking for a Claude alias

- Level: patch. A fix: Codex's native delegation takes only its own model identifiers, so where a specialist's manifest or profile names `sonnet`, `opus`, `haiku`, or `fable`, `prepare` and `check` on Codex now print a `NOTE` naming the model in place of the `MODEL` line, as they do for an inline role, and the reviewer runs on the session's model. The record's `model` is the one the reviewer reports, as before. Claude Code still gets the `MODEL` line. An inline retry no longer prints a `MODEL` line either.
- Contract: none
- User action: none
- Pull request: #305

### dotnet-format stops at its time limit even when the formatter leaves a build host running

- Level: patch. A fix: the formatter now runs through skill-core's bounded runner, writing its output to a file rather than a pipe, so a process it starts and leaves holding that output, such as a build host, no longer keeps a check or a fix waiting past `--timeout` (570 seconds by default) on Windows. It reads no stdin and runs with the git, Git Credential Manager, and gh prompts turned off, as every other bounded command does. The output, the `LOG` file, and the exit codes are unchanged.
- Contract: none
- User action: none
- Pull request: #304

### A skill left in place keeps the agents it was deployed with

- Level: minor. A fix with an addition to the ownership manifest that the previous release still reads: each skill's manifest entry gains `agent_deps`, the agent files it was deployed with, and a deployment that leaves a locally modified skill in place, deselected, skipped, or preserved by an uninstall, keeps those agents and reports them as `KEEP`, as it already kept the skill's shared assets. Before, the agent was removed as `no selected skill needs it` while the preserved skill still started it. `MANIFEST_VERSION` stays 7; a previous release ignores `agent_deps` and drops it when it rewrites the entry.
- Contract: `deployer/manifest.py`
- User action: none. An installation updated across this change records a skill's agents at the next deployment that selects the skill and finds it unmodified; until then a preserved skill keeps no agent.
- Pull request: #303

### update-pr-tracker reports a GitHub call that failed instead of marking reviews stale

- Level: minor. A new status line and a fix: when a GitHub call a comparison needs fails for any reason but a missing commit, `tracker_pipeline.py update` no longer reads it as unknown evidence, which marked every affected review `stale`, offered it for re-review, and exited 0. Network and timeout failures now stop the run at once with `FAILED <reason>`, as authentication and rate-limit failures already did. Any other failure, such as HTTP 403, an SSO-withheld result, or a server error, prints the new line `PULL_FAILED <owner/repo#N> <error>` for each pull request whose comparison failed, then `FAILED <n> of <m> pull requests could not be compared; the dashboard keeps its previous rows`, and exits 1 without writing the dashboard or printing a `CANDIDATE`. `collect` treats the same failures alike while placing the user's review commit: a network or timeout failure stops the collection, and any other but a missing commit fails its repository with `REPOSITORY_FAILED`, where before it showed the Findings cell without what moved since the user's review. A missing commit or branch is still unknown, as before.
- Contract: none
- User action: none. An update that now fails names each pull request GitHub could not compare; rerun it once GitHub answers, or leave out a pull request that fails on every run with `--remove`.
- Pull request: #300

### update-coding-agent-skills leaves a clone on the branch it was found on when it stops

- Level: patch. A fix: when local `main` has commits that `origin/main` lacks, `update-coding-agent-skills` now stops with `NOT_FAST_FORWARD` before switching branches, so a clone found on another branch stays on it, and the lines after the status list those local commits instead of Git's error. When Git refuses the fast-forward itself after the switch, for an untracked file in its way, the clone is switched back, or a `still on main:` line says why it could not be. An update still switches the clone to `main` and leaves it there, as before.
- Contract: none
- User action: none
- Pull request: #294

### audit-ai-config asks before its MCP handshake starts a repository's servers

- Level: minor. A fix with a new output line: in Claude Code, `audit-ai-config` pre-approves only its audit engine, so the opt-in handshake, which starts each MCP server the repository configures, now asks for approval before it runs. `analyze-skill-cost`'s `skill_inventory.py tools` prints `REPOSITORY_CODE` for a command that runs a script declaring `RUNS_REPOSITORY_CODE` and is left to prompt, and `GRANTED_REPOSITORY_CODE` for one a grant covers, in place of `UNGRANTED` for such a command.
- Contract: none
- User action: none. Approve the handshake command when you have authorized the operational validation.
- Pull request: #299

### review-insights prints categories and analyzer names screened, and decides them as printed

- Level: patch. A fix: a `RECOMMENDATION` line's category and an `ANALYZER` line's tool and rule are printed on one line, with whitespace flattened and `?` for a double quote, backtick, `$`, backslash, or control character, so a value from a record never breaks the one-fact-per-line output or reaches a shell unquoted. `decide` takes the subject as printed or as recorded, and the skill passes every value in double quotes. A synthesized title is now refused for a control character too. Records and reports are unchanged.
- Contract: none
- User action: none
- Pull request: #293

### The review state refuses a merged_since that is not a date, and enumerate fails only that repository

- Level: patch. A fix: `validate_state` now refuses a repository's `merged_since` whose first ten characters are not a `YYYY-MM-DD` calendar date, as the review state table already said, so such a `state.json` fails with one `FAILED` line naming the repository instead of a traceback from `enumerate`. A watermark that still cannot be read when `enumerate` lists its repository ends in `REPOSITORY_FAILED` for that repository, and the command exits 1 after the batch, as for any other failed repository. Every watermark `advance` writes is such a date, so a state the scripts wrote is read as before.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. A `merged_since` edited by hand to something else is now refused; correct it to a `YYYY-MM-DD` date, or remove it to start that repository again from today.
- Pull request: #295

### A status override may not name a section the tracker renders, such as `My PRs`

- Level: minor. A fix that would be major from `1.0.0`: `dashboard.status_overrides` now refuses every section name `update-pr-tracker` renders, trimmed and in any case, which adds `my prs` to the refused states; `to review`, `awaiting response`, `drafts`, `my pull requests`, `missing`, `current`, and `stale` stay refused. An override named `My PRs` was accepted before, and the dashboard then showed that section twice and dropped every authored pull request not overridden; a configuration holding one now fails to load.
- Contract: `docs/code-review-operations-contract.md`
- User action: none, unless an override's status is `My PRs` in any case. Then rename or remove it in the code-review configuration file by hand, since `tracker_pipeline.py override` validates the file before changing it.
- Pull request: #292

### The reviewer guard refuses a self-check whose script or run ends in a backslash

- Level: patch. A fix: in Claude Code, the reviewer guard now holds a `code-review-reviewer`'s self-check command to the rule its source commands already followed, so no quoted script or run may end in a backslash, which would escape its closing quote. The self-check the pipeline writes is allowed as before.
- Contract: `docs/code-review-operations-contract.md`
- User action: none
- Pull request: #262

## v0.4.0

### A specialist condition may declare the snapshot paths it reads, so its review keeps the lazy snapshot

- Level: minor. Additive: a condition in a specialists manifest may list `reads`, glob patterns of the snapshot paths its script opens besides the changed files, matched as `snapshot_exclude` is. When every condition a review runs declares them, `prepare` keeps the checkout route's snapshot lazy, recorded as `checkout-lazy`, and writes the paths they match beside the changed files and the analyzer settings; the script sees no other path. A condition without `reads` keeps the whole snapshot, recorded as `checkout`, as before. A path `snapshot_exclude` matches is never written, declared or not. `validate-reviewer` runs each condition on the snapshot a review gives it, so its `CONDITION` lines match the review's. A previous release refuses a manifest that has `reads`.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. To use it, add `reads` to each condition of a repository's manifest and check the conditions with `validate-reviewer`; the "Specialist reviewers" section of `docs/code-review-operations.md` gives an example.
- Pull request: #251

### A review skill the base predates is read from the default branch's tip, or the generic reviewer runs, and the record names the reviewer's source

- Level: minor. Additive: a pull request whose base commit predates the repository's configured review `skill`, with no `trusted_ref` configured, no longer fails `prepare` with `does not exist at`. The skill is read from the default branch's tip as origin reports it; when the tip lacks it too, or is the pull request's head, the suite's generic reviewer reviews that pull request. `prepare` prints a `NOTE` for either fallback. Each record's `review.adapter` gains `source`: `generic`, `trusted-ref`, `base`, `default-branch`, or `generic-fallback`, which the report shows as **Reviewer source**. A reviewer with a `trusted_ref` or a `manifest_path`, and a pull request whose base has the skill, behave as before. A previous release refuses a record that has `source`.
- Contract: `docs/code-review-operations-contract.md`
- User action: none
- Pull request: #252

### update-pr-tracker sets and clears status overrides with a validated command

- Level: minor. An addition: `tracker_pipeline.py override` lists the configuration's `dashboard.status_overrides` (`OVERRIDE <owner/repo#N> <status>`, then `OVERRIDES <n>`), and with `--set owner/repo#N=STATUS` or `--clear owner/repo#N` changes them in one validated write, printing `SET`, `CLEARED`, and `WROTE <configuration>`. It changes nothing else in the file and fills in no default. It refuses a computed state, a malformed key, or clearing an override that is not set, and clears an override whose pull request has closed. `update-pr-tracker` now tells the agent to use it when you ask for a pull request to be shown under a status of your own.
- Contract: none
- User action: none
- Pull request: #250

### A repository's snapshot_exclude leaves files out of its review snapshot, and a changed one still makes the review INCOMPLETE

- Level: minor. Additive: a repository's entry in the code-review configuration may list `snapshot_exclude`, glob patterns of files to leave out of its source snapshot, such as resources, designer files, and generated reports; without it nothing changes. A matching file is recorded in the snapshot's manifest under `excluded_paths` with the new reason `configured`, on the lazy, whole, and tarball routes alike: it is never written, `source-file` prints `EXCLUDED <path> configured` for it, and `source-search` leaves it out. A changed file it matches is an unavailable source, so that review is `INCOMPLETE`. `validate-reviewer` fails on a pattern that matches a file the reviewer declares, and its `SNAPSHOT` line counts `configured` exclusions. Each record's `review.snapshot` gains `excluded`, the head's paths left out by reason, which the report's Snapshot row and a canary's `STATS` snapshot line show. A previous release refuses a record that has it.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. To use it, add `snapshot_exclude` to a repository's entry and run `validate-reviewer` for a repository reviewer; "Configuration" in `docs/code-review-operations.md` gives an example.
- Pull request: #249

### The Copilot CLI host re-reads the source snapshot only as far as it changed since prepare

- Level: patch. Faster with the same guarantee: for a run the Copilot CLI host reviews, `prepare` stamps the source snapshot in `run.json` (one SHA-256 over the manifest's bytes and each written file's path, size, and modification time), and the host, before it starts Copilot, checks the structure and the stamp and re-hashes only the files a time cannot vouch for and any fetched file. A stamp that no longer matches, or a run prepared before this release, has every file re-hashed as before. At 25,000 files the host's check went from minutes to about a second, and `prepare` spends about a second taking the stamp.
- Contract: none
- User action: none
- Pull request: #241

### The code-review contract states the specialist result and the review state, and enumerate exits 1 after a failed repository

- Level: minor. Before `1.0.0` this carries what would later be major. The contract's Formats section gains tables for the specialist result each reviewer writes and for the review state `review-prs` keeps (`state.json`), and its opening paragraph names its one exception, `review-insights`' own report. A specialist result now fails its check, and its reviewer is asked to fix it, when it holds a field its prompt's output contract does not list, when it gives its findings as `comments` instead of `findings`, or when a finding's `category` is not a string; before, these were accepted, and the extra fields were dropped. `review_pipeline.py enumerate` exits 1 after printing any `REPOSITORY_FAILED` line, as the tracker's `collect` does; it still lists the other repositories and writes the batch. The generic reviewer's instructions now follow its prompt's output contract and no longer name the adapter schema. The contract now says that the configuration file takes no lock, and why. The unused `references/review-output-template.md` is removed.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. Nothing in `review-prs` acts on `enumerate`'s exit code. A repository specialist profile that tells its reviewer to add fields of its own needs no change: the prompt's output contract replaces any format a profile gives, and the reviewer's self-check names a field that does not belong.
- Pull request: #239

### update-coding-agent-skills stops at a release even when local main has no release tag

- Level: patch. A fix: the `MAJOR_UPDATE` stop now applies when no release tag reaches local `main`, which counts as version 0.0.0 and prints as `MAJOR_UPDATE untagged..<target>` (and `CROSSED untagged..<target>` with `--cross-major`); before, such an installation updated across any release unchecked. A release tag is `vMAJOR.MINOR.PATCH` with an optional pre-release suffix, the highest one reachable counts, and a tag of another shape, such as `v2x.0.0`, is ignored instead of disabling the check. With no release tag on `origin/main` the update proceeds as before.
- Contract: none
- User action: none. An installation whose clone has no release tag on local `main` stops once at the next release and asks for `--cross-major`, like any installation behind a breaking release.
- Pull request: #234

### analyze-skill-cost reads each plain-scalar grant on its own and fails on a file that is not UTF-8

- Level: patch. Fixes: a plain-scalar `allowed-tools` such as `Bash(p), PowerShell(p)` is read as two grants, where it was one Bash grant that printed false `UNPAIRED_ALLOWED` and `UNGRANTED` lines. `skill_inventory.py tools` and `scan` print `FAILED cannot read <path>: not UTF-8 text` and exit 1 for a file that is binary or not UTF-8, instead of an empty result. `scan` also marks a prose line naming a subagent as an `agent` cue. The delegation and model rules moved to a reference the skill reads only when they apply.
- Contract: none
- User action: none
- Pull request: #201

### audit-ai-config reports unreadable files as findings, accepts block scalars, and prints each finding's line

- Level: minor. Before `1.0.0` this carries what would later be major: the JSON report's findings no longer carry `detail`, which was always empty, and the Markdown findings table gains a `Line` column between `Path` and `Message`. An undecodable skill, agent, or `.codex/config.toml` is now an `ERROR` finding where the audit used to stop with a traceback, a `|` or `>` block scalar in skill or agent frontmatter is read as YAML reads it instead of being an `ERROR` per line, `--root .` names the directory, and nested-file checks skip version-control and dependency directories.
- Contract: none
- User action: none, unless a script reads the JSON `detail` field or the Markdown table by column position; drop the field and count the new column.
- Pull request: #230

### dotnet-format formats .slnx solutions with the .NET SDK, reports a failed fetch, and splits a long file list

- Level: minor. An addition: `dotnet-format` finds `.slnx` solutions as well as `.sln` and formats a `.slnx` with the .NET SDK's `dotnet format`, because the deprecated `dotnet-format` global tool, still used for a `.sln`, cannot open one; `dotnet` is a new optional tool with a floor of 9.0.200, the first SDK that reads `.slnx`, which `python deploy.py check` reports. `dotnet_format_targets.py resolve` stops with a `STOP` naming the SDK when the chosen solution is a `.slnx` and `dotnet` is not installed, prefers a `.sln` over a `.slnx` that owns as many changed files, prints `FETCH_FAILED <reason>` after `REPO_ROOT` when `git fetch origin` fails or does not finish and goes on with origin as last fetched, and reports a missing or stalled git, or a repository git refuses, with git's own reason instead of "not inside a Git repository". `run_dotnet_format.py` formats a file list too long for one Windows command line in several runs within its one time limit, where it used to fail with a raw operating-system error.
- Contract: `deployer/tools.py`
- User action: none for a `.sln`. To format a `.slnx` solution, install the .NET SDK 9.0.200 or newer.
- Pull request: #202

### curate-agent-memory changes only a memory store and is granted no Write

- Level: patch. Fixes: `memory_audit.py delete` and `reindex` refuse a directory that holds no `MEMORY.md` or lies inside a skills directory, printing one `FAILED <dir> ...` line and changing nothing, where they used to act on any directory. A `>` or `|` block-scalar `description` is read whole in the audit report and the rebuilt `MEMORY.md`, instead of as the `>-` header. The skill no longer grants `Write`; it edits with `Edit`, so creating a new destination file asks first.
- Contract: none
- User action: none. A memory directory Claude Code writes holds `MEMORY.md`; one that has lost it needs an empty `MEMORY.md` before `reindex` rebuilds it.
- Pull request: #200

### A review's snapshot from a checkout holds only the changed files, and reviewers fetch the rest

- Level: minor. An addition: on the checkout route `prepare` writes only the changed files and the analyzer settings into the source snapshot and lists every other file of the head with its blob id; each reviewer prompt names two commands of `code-review-core/scripts/review_source.py`, `source-file`, which writes a file from the commit by its blob id, and `source-search`, which runs `git grep` at the commit, and the reviewer guard allows both for the reviewer's own role. `review.snapshot.source` gains the value `checkout-lazy`, which an earlier release refuses to read, and `files_read` counts fetched files. The Copilot CLI host, and a specialists manifest whose routed specialist has a `when` condition, still get the whole snapshot, recorded as `checkout`. A fixture canary's repository now lives in its run until `finalize`.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. The pipeline, the commands, and the reviewer guard deploy together with `code-review-core`.
- Pull request: #227

### deploy.py check also reports Git and Claude Code with their versions

- Level: minor. An addition: `check` lists `Git` (used by every skill; `MISSING` when it is not on `PATH`, which leaves deploying possible) and `Claude Code` (optional) with their versions, so its output is all the bug template asks for besides the model. Neither has a floor, and no other line changes.
- Contract: `deployer/tools.py`
- User action: none.
- Pull request: #231

### --force-item names an agent without its .md and refuses a name the run does not install

- Level: minor. A deployer flag changes: `--force-item` now accepts an agent's own name, such as `code-review-reviewer`, as well as its file name, and a run given a `--force-item` that matches no item it installs stops before anything changes, listing the names it accepts, where it used to ignore the name and deploy without replacing anything.
- Contract: none
- User action: none, unless a script passes `--force-item` a name that the run does not install; correct the name or drop it.
- Pull request: #224

### A batch review lists only the pull requests its watermark can still select

- Level: minor. An addition: `enumerate` prints `LISTED <repository> scan=<full|watermark> pages=<n> pulls=<n> read=<n> candidates=<n>` before each listed repository's `PULL` lines. A repository with a watermark now lists its open pull requests and its closed ones from the most recently updated back to the watermark, instead of its whole history, and only the pull requests that can still be selected are looked up in the archive. Which pull requests are selected does not change: a merged pull request is judged by its merge date, as the contract now states.
- Contract: none
- User action: none. A repository without a watermark lists its whole history once, as before, and `advance` then records one.
- Pull request: #216

### update-pr-tracker compares commits four at a time and prints how many GitHub calls it made

- Level: minor. An addition: `tracker_pipeline.py update` prints `GITHUB_CALLS <n>` after `UPDATED`. It now compares commits four at a time and reads a commit's file tree only when the two comparisons list the same files, so a pull request whose files differ is a change even where GitHub cannot list the repository's tree in full, which was unknown before.
- Contract: none
- User action: none.
- Pull request: #215

### Review records measure the source snapshot and what each reviewer read

- Level: minor. An addition: every new record carries the optional `review.snapshot` (its source, `checkout` or `tarball`, its files and bytes, and the seconds `prepare` spent fetching, materializing, and writing prompts) and, per reviewer, `files_read` and `bytes_read`, null when no guard counted its reads, as for an inline reviewer. An earlier release refuses to read a record that has them. The report gains a **Snapshot** row and a **Files read** column, `run.json` gains `snapshot` and `reads`, and a canary's `finalize` prints `STATS` lines. The reviewer guard now writes each role's read log in the run's `work` folder.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. Records written before this release have neither field and read as before.
- Pull request: #206

## v0.3.0

### review-insights synthesizes findings and flags into targeted recommendations

- Level: minor. Additions: `report` writes a sealed synthesis input, context, and prompt beside `insights.json` and prints `SYNTHESIS_PROMPT`, `SYNTHESIS_RESULT`, `SYNTHESIS recorded`, or `SYNTHESIS skipped` before its other lines; the new `synthesize` command and its `VALID`, `PROBLEM`, `SYNTHESIZED`, `TITLE`, `TARGET`, `CHANGE`, `RATIONALE`, and `SYNTHESIZED_COUNT` lines; the `TOPIC`, `ASSESSMENT`, `ADDRESSED_BY`, and `FINDING` lines after each category recommendation once a synthesis is recorded; `decide-custom`; and, replacing the custom-candidate `ANALYZER` and `EXAMPLE` lines, which `report` no longer prints, one `CUSTOM_CANDIDATES` line with its `PATTERN`, `PATTERN_ASSESSMENT`, and `PATTERN_ADDRESSED_BY` lines; `decide --synthesized TITLE`; and report schema version 7, whose `synthesis` field and `synthesized` recommendations an earlier release refuses to read. The skill now starts one subagent per report to write the synthesis. The new `scope` command prints `CURRENT_REPOSITORY` or `NO_CURRENT_REPOSITORY` and `DEFAULT_SET`, and the skill, given no repository or set inside a configured checkout or its worktree, asks whether to analyze that repository or the default set.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. Run `review-insights` again for a range to get its synthesis; a report written before this release has none until it is regenerated.
- Pull request: #190

### A specialists manifest can have findings name the kind of problem instead of the reviewer

- Level: minor. An addition: the optional top-level `finding_categories` in a specialists manifest, and `fallback_finding_category`, the one of them reviewers use when no other fits. With it, every reviewer gives each finding a `category` from the list, `check` refuses a finding without one, and records keep it; without it, nothing changes. An earlier release refuses a manifest that sets it.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. To group findings by kind in records and in `review-insights`, add `finding_categories` to the manifest; reviews before then keep their reviewer-named categories.
- Pull request: #189

### Each skill declares which runtimes run it, and the runtime canary checks the declaration

- Level: minor. An addition: every `deploy-meta` file declares `runtime_support` (`full`, `partial`, or `none` for Claude Code, Codex, and Copilot CLI), `docs/skills.md` prints it as "Runtime support", and the runtime canary prints `NOT_ATTEMPTED` and `MATRIX` lines and exits 1 when a run disagrees with it. The deployer now refuses a source whose `runtime_support` is malformed; a source without one deploys as before.
- Contract: none
- User action: none. Before relying on a skill from Codex or Copilot CLI, check its row: the code reviews and the user-only skills run there in part.
- Pull request: #184

### review-prs runs canaries of local fixtures, as initial reviews and re-reviews

- Level: minor. Additions: `review-prs --canary --fixture DIR [--re-review --prior RECORD]` and the pipeline's `prepare --canary --fixture DIR [--re-review --prior RECORD]`, whose failure prints `FAILED <directory> <reason>`; the fixture's `pull.json` format; and the `fixture` field of `run.json`. A canary's `finalize` no longer reads the flag store, as a canary was documented never to.
- Contract: `docs/code-review-operations-contract.md`
- User action: none.
- Pull request: #179

### Reviews run inline where subagents cannot start, and specialists manifests no longer need agent-delegation

- Level: minor. Additions: `prepare --inline`, the `next-role` command and its `INLINE`, `INLINE_ROLE`, and `INLINE_DONE` lines, and the optional `review.dispatch` field every new record carries (`subagents`, `copilot-host`, or `inline`), which an earlier release refuses to read. A relaxed rule: a specialists manifest may leave `agent-delegation` out of `required_capabilities`, and then its specialists also run inline. On Copilot CLI, a review by the generic reviewer or by a specialists manifest without `agent-delegation` now runs inline instead of failing.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. To let a specialists manifest run on Copilot CLI, remove `agent-delegation` from its `required_capabilities`; a manifest that keeps it runs only as subagents, as before.
- Pull request: #3

### flag-review-finding refuses a finding the archive does not have and lists a pull request's findings

- Level: minor. An addition and a refusal, carried in the pre-1.0 minor: the new `findings` subcommand prints `FINDING` lines; `add` now fails on a finding ID that is not of the form `F001` or that the named review does not have, which it used to save as a flag that never linked; and a flag store whose `finding_id` is not a string or null fails to load instead of crashing later. `review-insights` counts a finding and its repeats once and reports an outcome only when a later review judged it, so a regenerated report can show smaller counts.
- Contract: `docs/code-review-operations-contract.md`
- User action: none, unless a hand-edited flag store holds a `finding_id` that is neither a string nor null. Then every command that reads it prints `FAILED Flag finding ID is invalid`; set that value to null or the finding ID.
- Pull request: #165

### A review without checkout_path fails when GitHub's tarball is not the commit's exact tree

- Level: patch. A fix: the snapshot could leave out a changed file or hold bytes the commit does not hold.
- Contract: none
- User action: none, unless `prepare` prints `FAILED` saying GitHub's tarball is not the commit's exact tree. Then set `checkout_path` for that repository.
- Pull request: #141

### Item names the file system treats as one are one item, and the deployer's own suffixes are reserved

- Level: minor. A deployment that was accepted can now be refused, carried in the pre-1.0 minor. A source whose shared asset is a case variant of another source's item, such as `guide.md` beside `Guide.md`, is refused as a collision, and a name ending in `.deploying-bak` or containing `.tmp.` is refused for every kind of item.
- Contract: none
- User action: none for the skills this repository ships. Before updating, rename in its source any shared asset of your own whose name differs only in case from an item another source deploys, ends in `.deploying-bak`, or contains `.tmp.`, and deploy; the manifest reader refuses an entry with a reserved name.
- Pull request: #157

### A changed path no reviewer prompt can carry safely is an unavailable source

- Level: patch. The field's format is unchanged; it now also lists a changed path with a control character, a backslash, or an absolute, empty, `.`, or `..` segment, which no reviewer is given, so the review is `INCOMPLETE` instead of failing or reaching a prompt.
- Contract: `docs/code-review-operations-contract.md`
- User action: none.
- Pull request: #142

## v0.2.0

### init-ai-config is removed on update

- Level: minor. Major-level, a removed skill, carried in the pre-1.0 minor.
- Contract: `skills/init-ai-config`
- User action: none. Updating removes it; `audit-ai-config` stays.
- Pull request: #83

### update-coding-agent-skills stops at MAJOR_UPDATE on every v0.1.x installation

- Level: minor. While the major version is 0, a raised minor is the breaking component, so `v0.2.0` stops every `v0.1.x` installation.
- Contract: none
- User action: start `update-coding-agent-skills` again once with `--cross-major` after reading these notes.
- Pull request: #32

### deploy.py verify requires Codex CLI 0.88.0 or newer

- Level: minor. Major-level, a raised tool floor, carried in the pre-1.0 minor. Deployment is unaffected; `verify` reports an older Codex CLI as `OUTDATED` and fails.
- Contract: `deployer/tools.py`
- User action: update Codex CLI to 0.88.0 or newer before running `python deploy.py verify`.
- Pull request: #72

### The hidden skill-core skill is added

- Level: minor. A new skill, deployed as a dependency of the skills whose scripts share its modules; it is not offered for selection.
- Contract: `skills/skill-core`
- User action: none.
- Pull request: #104

### The code-review formats are stated as tables, and the unreferenced schemas are removed

- Level: patch. No script read the removed schemas, and the tables state the formats the code already enforced.
- Contract: `docs/code-review-operations-contract.md`, `skills/code-review-core/references/flag-record.schema.json`, `skills/code-review-core/references/legacy-review-index.schema.json`, `skills/code-review-core/references/review-config.schema.json`, `skills/code-review-core/references/review-record.schema.json`, `skills/code-review-core/references/review-request.schema.json`, `skills/code-review-core/references/reviewer-manifest.schema.json`
- User action: none.
- Pull request: #60

### The code-review formats gain optional fields

- Level: minor. Additive: a review record's `schema_version` stays 1, and records without a finding ledger stay valid and read as having no history. A reviewer's output no longer has to carry `prior_dispositions` when the request has no prior findings and may mark a finding as a `repeats` of another; the configuration gains the optional `model_names`; a re-review request's carried findings gain `flags`; trusted reviewer files are hashed as their exact committed bytes.
- Contract: `skills/code-review-core/references/review-adapter.schema.json`, `docs/code-review-operations-contract.md`
- User action: none. A `review-insights` report written before shows `-` in its new columns until it is regenerated.
- Pull request: #47, #67, #80, #82
