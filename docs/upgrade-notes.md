# Upgrade notes

What each release asks of a user who updates, one entry per change a user meets since the previous tag. The GitHub release notes open with the release's entries; [Releasing](releasing.md#versioning) defines the contracts and the levels.

An entry is written in the pull request that makes the change, under `## Upgrade note` in its body, and never in this file. The repository squash-merges with the body as the commit message, so each merged change carries its entry into `main`'s history. Before tagging, `python tools/release_notes.py <version> --write` writes the release's section here from the commits since the previous tag, newest first, and the release pull request commits it ([Before tagging](releasing.md#before-tagging)). Entries for changes merged before entries moved into pull request bodies were written here under `## Unreleased`; the script reads those from the commits that added them, so the next release's section holds them too.

A generated section starts with `Level: <level>.` just below its version heading, before the entries: the largest leading `Level:` word any entry names, in the order of the **Levels** list in [Versioning](releasing.md#versioning). With no entries it states `Level: none (no upgrade-note entries).`, not an inferred patch level. The summary does not replace an entry's explanation or user action: before stability, a minor may carry a change that would be major later, as that entry says. Published sections remain as released; the tool adds the summary only to the new section it generates.

Validation (`tests/run_validation.py`) compares the contract files at the last tag reachable from `origin/main` with the working tree and fails while a changed contract item is named by no entry since that tag. In a pull request it reads the body, which CI passes it, and the commits merged into `origin/main`; a local run reads the branch's commit messages, or the body from a file passed with `--pr-body <file>`. It also fails on a section of this file that is not a version's.

An entry is a `###` heading saying what changed, followed by these fields:

- `Level:` starts with a level word from the Versioning section: `patch`, `minor`, or `major`. Before `1.0.0`, a change that would be major is carried in a minor, and the entry says so.
- `Contract:` the contract items the entry covers, each in backticks, or `none` when the change touches no contract item. `none` is case-insensitive and may end with a period. An item a contract file holds is named as the validation failure names it. A shipped skill's arguments, status lines, and exit codes are contract items too, the skill invocation contract of [Versioning](releasing.md#versioning), though no file holds them: an entry for a change that adds, renames, or removes one, or gives one a new value, field, or meaning, names the skill and each such item, as in `` `repo-cleanup` `report` `` or `` `update-coding-agent-skills` `DEPLOY_PINNED` ``, and no check catches its absence. A repository skill under `.claude/skills/` is not deployed, so its arguments, status lines, and exit codes are no contract item, and an entry for a change to them says `Contract: none`. This is the one statement of how an entry names such a change; the other documents point here.
- `User action:` what the user must do after updating, or `none`.
- `Pull request:` the pull request as `#N`. The script adds it from the squash commit's title, so a body leaves it out.

A pull request whose change no user meets writes `None` under its `## Upgrade note`.

## v0.13.0

Level: minor.

### The hub guard allows only declared read-only git forms in a guarded hub

- Level: patch, a defect fix in a contributor-facing tool with no contract change.
- Contract: none
- User action: none. In a clone that opts into the hub guard, git in the hub runs only in the forms "The guard's contract" in `docs/parallel-sessions.md` lists, with options spelled in full; pull `main` with `git pull --ff-only origin main` rather than a bare `git pull --ff-only`, and run any other git from a worktree or a terminal.
- Pull request: #2472

### The hub guard judges git's operand writers, in-place modes, and roots

- Level: patch, a defect fix in a contributor-facing tool with no contract change.
- Contract: none
- User action: none in a clone that has not opted into the guard. In one that has, while a hub is guarded the guard refuses `git interpret-trailers --in-place`, `git mailinfo`, `git merge-file` without `-p`, or `git worktree move` writing into the hub; `git checkout-index --prefix`, `git am --directory`, or `git apply --directory` pointing into the hub, from any checkout; and every `git apply --unsafe-paths`.
- Pull request: #2466

### audit-ai-config's MCP handshake bounds its writes and caps a paging cursor

- Level: patch, a defect fix that adds no option and changes no exit code.
- Contract: `HANDSHAKE_FAILED` gains the reasons `timed out sending a request` and `tools/list returned a nextCursor over 4096 characters`.
- User action: none.
- Pull request: #2465

### The update no longer asks for a credential when it fetches

- Level: patch, because it fixes a hang. No argument, status word, or exit code changes.
- Contract: none.
- User action: none. If the update stops with `FETCH_FAILED` naming a credential, run `git fetch` in the clone once to sign in, then rerun the update.
- Pull request: #2459

### The hub guard reads what git's options write and run, and refuses a writing git config

- Level: patch, a defect fix in a contributor-facing tool with no contract change.
- Contract: none
- User action: none in a clone that has not opted into the guard. In one that has, while a hub is guarded the guard refuses: a git option that writes a file into the hub, such as `git diff --output=<file>` or `git archive -o <file>`; a `git config` that writes, so turn the guard off from a terminal; a `-c` setting outside the guard's allow-list, such as `core.pager`; an option that runs a command, such as `rebase -x` or `fetch --upload-pack`; and a variable or substitution given to `diff`, `log`, `fetch`, `push`, and the other subcommands with such an option. Write such words literally. `TAR_OPTIONS`, `TAPE`, and `RIPGREP_CONFIG_PATH` are refused before any runner, `bash -c` included.
- Pull request: #2458

### A copy deleted beside a held deletion leftover is never deleted in place

- Level: patch. A rerun after a rollback or finalization stopped partway beside a held leftover now finishes instead of refusing or keeping a fragment as a backup.
- Contract: none
- User action: none. A warning may now name `.<name>.deploying-del.<token>`: delete it once nothing holds it open, as for `.<name>.deploying-del`.
- Pull request: #2455

### A skill's git and gh commands start no askpass helper

- Level: patch, a defect fix that brings the commands back to the documented "shows no prompt".
- Contract: none.
- User action: none. If a skill's push or fetch relied on an IDE's askpass dialog for a credential, configure a credential helper, or sign in to Git Credential Manager or gh, before running the skill.
- Pull request: #2454

### audit-skill reads the git subcommands a PowerShell script runs

- Level: patch, a defect fix: the runtime-support derivation now sees what the scripts it audits run.
- Contract: `audit-skill` `RUNTIME_NEEDS`, `audit-skill` `FAILED`
- User action: none. A `RUNTIME_NEEDS sandbox-network git <subcommand>` line may now name a `.ps1` script or a subcommand no literal names, such as `$sub`, and auditing a skill that has PowerShell scripts needs pwsh.
- Pull request: #2446

### A manifest whose recorded checkout is not text is refused

- Level: patch, since it refuses a malformed manifest no deployer writes, and no contract item changes.
- Contract: none
- User action: none, unless a hand edit left `source_dir` as something other than a path; the refusal names the manifest's remedy.
- Pull request: #2442

### `tools/skill_evals.py --write` keeps a model's measured row when no record of it counted

- Level: patch, a defect fix: a run that measured nothing no longer overwrites a measured result.
- Contract: none. `tools/skill_evals.py` and `evaluate-skill` are repository tools, not deployed.
- User action: none for a user of the deployed skills. A contributor who reads a `KEPT <model>` line fixes what that model's `NOT_COUNTED` lines name and runs the evaluation again to record it.
- Pull request: #2441

### The hub guard reads the git call after the `time` keyword's `--`

- Level: patch, a defect fix in a contributor-facing tool with no contract change.
- Contract: none
- User action: none, in a clone that has not opted into the guard. In one that has, a git call after `time --` or `time -p --`, such as `time -- git checkout main`, is now judged as the bare call: allowed from a worktree, and refused in the hub when it writes the hub, where before it was allowed.
- Pull request: #2440

### Values on the code-review and other skills' status lines stay on one line

- Level: patch, a fix to what the status lines print for a value holding a line break, a tab, or a control character; no line is added, renamed, or removed.
- Contract: `review-prs` `MATCH`, `review-prs` `FAILED`, `review-prs` `REPOSITORY_FAILED`, `review-prs` `NOTE`, `review-prs` `RETRY`, `review-prs` `INVALID`, `review-prs` `SKILL`, `review-prs` `UNMATCHED`, `review-prs` `REVIEWER`, `review-prs` `TOOLS`, `review-document` `DOCUMENT`, `review-document` `FAILED`, `review-insights` `FAILED`, `curate-agent-memory` `SKILL_FILE`, `repo-cleanup` `REFUSED_ENTRY`, `repo-cleanup` `POLICY`, `audit-ai-config` `FAILED`, `update-pr-dashboard` `SET`, `update-pr-dashboard` `WROTE`. Each value these and the same skills' other stdout lines print passes ejryan89-skill-core's `flat_text`, `fact_line`, or `single_line`: a tab, a control character, or a line break in it prints as a space, and a line's trailing whitespace is dropped, so one fact is one line. `repo-cleanup` prints `-` for an empty field of a `POLICY`, `ADDED`, or `REFUSED_ENTRY` line, as its other lines do.
- User action: none.
- Pull request: #2436

### audit-skill reads a git command a wrapper runs

- Level: patch, a defect fix that brings audit-skill's runtime needs to what validation and its documentation state.
- Contract: none
- User action: none. An audit of a skill whose Bash script runs `xargs git push` or a similar wrapped remote git command may now report the network need it missed.
- Pull request: #2435

### audit-ai-config names a JSON report it cannot write

- Level: patch, because the exit code is unchanged and the `FAILED <reason>` line the skill already documents gains one reason.
- Contract: `audit-ai-config` `FAILED <reason>`
- User action: none.
- Pull request: #2407

### update-coding-agent-skills says where it runs in a guarded clone

- Level: patch: a fix to the skill's guidance, with no new configuration or command.
- Contract: none.
- User action: in a clone with `coding-agent-skills.hubGuard` set, start the skill from a session outside the clone, or run its script from a terminal; from PowerShell, through `& "$env:ProgramFiles\Git\bin\bash.exe"`.
- Pull request: #2401

### A relative home folder is refused by name

- Level: patch, a defect fix: a relative `USERPROFILE` or `HOME` was read under the working directory.
- Contract: none.
- User action: none, unless your profile folder variable is a relative path; set it to an absolute one.
- Pull request: #2392

### repo-cleanup names a branch or path this plan already handled

- Level: patch, a defect fix: a branch or path the plan already deleted or removed was reported `PRESERVED`, against SKILL.md's "stays".
- Contract: `repo-cleanup` `ALREADY_DELETED`, `repo-cleanup` `ALREADY_REMOVED`
- User action: none. Naming one again prints `ALREADY_DELETED <branch>` or `ALREADY_REMOVED <path>`, still with exit 0, and the summary no longer lists it as preserved.
- Pull request: #2391

### dotnet-format's fix refuses a read-only or linked file before the formatter writes

- Level: patch, a defect fix bringing the fix to what `docs/skills.md` says it checks.
- Contract: `dotnet-format` `REFUSED`
- User action: none. `fix` prints `REFUSED <file> <reason>` for each read-only or linked file and exits 1, leaving it unwritten; make the file writable, or the path a real file, and run it again.
- Pull request: #2391

### curate-agent-memory counts the overlaps it leaves out

- Level: minor, an added report field the skill reads, which is additive under Versioning in `docs/releasing.md`.
- Contract: `curate-agent-memory` `overlaps_omitted`
- User action: none.
- Pull request: #2391

### A status override key past ten digits fails the configuration

- Level: patch, a defect fix: such a key loaded, then `update` and `override --clear` refused it.
- Contract: none
- User action: remove an override key of more than ten digits from the code-review configuration, which no GitHub pull request number reaches.
- Pull request: #2391

### A repository owner GitHub cannot have is refused

- Level: minor. A configuration naming an owner no GitHub login spells, such as `my.org/repo`, `a--b/repo`, or a 40-character owner, now fails to load, which asks its user to act; no repository GitHub holds is affected.
- Contract: none
- User action: you are affected only if a configured repository's owner holds a dot, a doubled, leading, or trailing hyphen, more than one `_`, or more than 39 characters. Correct it to the owner's login as GitHub shows it.
- Pull request: #2393

### Reviewers link only the same defect as a repeat

- Level: patch, a defect fix: a second defect in the same code no longer closes with the first.
- Contract: none
- User action: none.
- Pull request: #2393

### update-coding-agent-skills names git's reason when git fails, and compares release numbers of any length

- Level: patch. The status words and exit codes are unchanged. Only the reasons a `FAILED` line gives change, a `FAILED` line now always comes before anything is merged, and one case moves from `NOT_FAST_FORWARD` to `FAILED`.
- Contract: `update-coding-agent-skills` `FAILED`, `update-coding-agent-skills` `NOT_FAST_FORWARD`.
- `FAILED not a git repository: <clone>` becomes `FAILED cannot read <clone> as a git repository: <git's reason>`.
  - New reasons:
    - `FAILED cannot look up refs/heads/main: <reason>`;
- `FAILED cannot tell whether refs/heads/main is an ancestor of refs/remotes/origin/main: <reason>`;
    - `FAILED cannot read the branch the clone is on: <reason>`;
    - `FAILED cannot read the commit the clone is on: <reason>`;
    - `FAILED cannot compare release <current> with <target>: <reason>`.
- `FAILED cannot compare release tag <tag>: its <part> version has more than 18 digits` is gone, because such a release now compares exactly.
- The short-name failures of main before and after the fast-forward and of the deployed commit now stop before the checkout, leaving the clone as found.
- A `merge-base` that warns before exiting 1 is a `FAILED` line rather than `NOT_FAST_FORWARD`.
- User action: none.
- Pull request: #2382

### The hub guard refuses any slash word to Windows sort or find that isn't exactly a safe option

- Level: patch, a contributor-facing tool refusing more spellings of an option it already refused.
- Contract: none
- User action: none, in a clone that hasn't opted into the guard. In one that has, on Windows and while a guarded hub is in view, give `sort` and `find` paths that don't start with a slash, such as `./x` or `C:/x`, and write each slash option as a word of its own, in ASCII, with any value in the next word. `sort` there takes only the dash options `-r`, `-R`, and `-M`.
- Pull request: #2386

### dotnet-format reads `.editorconfig` lines as the compiler does

- Level: patch, a defect fix that brings the reading in line with the compiler.
- Contract: none.
- User action: none. A setting written with `:` or followed by a comment is now read, so `config` may stop reporting it as missing.
- Pull request: #2383

### audit-ai-config refuses a non-finite hook timeout and follows `CLAUDE.local.md` for AGENTS.md scope

- Level: patch, a defect fix in what the audit reports.
- Contract: none.
- User action: none. A Codex hook with `timeout = inf` or `nan` is now an `ERROR`, and a repository with only `AGENTS.md` and `CLAUDE.local.md` is no longer audited for Claude Code.
- Pull request: #2383

### The code-review configuration accepts an optional `reviewer_model`

- Level: minor. Additive: an optional key, null by default, so a configuration without it behaves as before; a previous release refuses a configuration that has it.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. To run reviewers on a model by default, set `reviewer_model` to `sonnet`, `opus`, `haiku`, `fable`, or `inherit`; a specialist's own `model` still wins.
- Pull request: #2378

### The hub guard reads runners' and programs' options by an allow-list

- Level: minor, a contributor-facing tool's contract changing what it refuses.
- Contract: none
- User action: none, in a clone that has not opted into the guard. In one that has, while a guarded hub is in view, a command that gives a runner, `find`, `rg`, `sort`, or `tar` an option off its allow-list, or an abbreviated long option, is refused, and so are `tar`'s extraction and `-C`. Spell the option in full, or leave out an option that changes where the command runs or writes a file the command does not name; extract an archive outside a guarded hub's view.
- Pull request: #2371

### A repository Windows cannot keep in a folder of its own is refused, and its archived records are no longer read

- Level: minor. Before `1.0.0` a refusal that asks some users to act is a minor unless it only enforces a promise the documents already made, and none promised this one. It removes access to existing data: on every platform, a review, re-review, or `review-insights` report of such a repository is refused, `update-pr-dashboard`'s collection of a set holding it fails with the reason and, as any repository's failure does, writes no collection, the records an earlier release archived for it are no longer read, and a hand-written `reviewers/<owner>/<name>/manifest.json` for it is no longer found. On Windows those records had shared another repository's folder.
- Contract: none
- User action: you are affected only if a configured repository's owner or name ends in a dot, such as `owner/a.`, or is a Windows reserved device name, alone or before an extension (`con`, `prn`, `aux`, `nul`, `com0` to `com9`, `lpt0` to `lpt9`, `conin$`, `conout$`, such as `owner/con` or `owner/nul.js`); the refusal names the identity and the folder under `archive_root` it no longer reads or writes. No supported route reviews such a repository: remove it from your repository sets, and keep or move aside its folder under `archive_root` and its `reviewers/<owner>/<name>` folder, which nothing reads. On Windows, the records under the trimmed name's folder (`owner/a` for `owner/a.`) are that other repository's and its own mixed, and no record says which is which.
- Pull request: #2374

### A pull request adding a file named by whitespace alone is reviewed

- Level: patch, a defect fix: the review, review-document, and the tracker refused the whole file listing.
- Contract: none
- User action: none.
- Pull request: #2374

### review-insights names a guidance file it would not follow through a link, and a stopped search only when pairs were left

- Level: patch, a defect fix: the lines now say what the search and the read did.
- Contract: `review-insights` `GUIDANCE_UNREADABLE` `GUIDANCE_SEARCH_STOPPED`
- User action: none. `GUIDANCE_UNREADABLE` now also names a file reached through a link in the checkout, such as a `CLAUDE.md` linked to `AGENTS.md`; `GUIDANCE_SEARCH_STOPPED` appears only when the search left pairs uncompared, and its `pairs=` counts the pairs compared.
- Pull request: #2374

### A run's journal beside no manifest is refused, never rolled back

- Level: minor. The fix adds a refusal that takes back an automatic recovery the documents promised: a journal an earlier release left on a first deployment it never committed was rolled back, and is now reconciled by hand, so before 1.0.0 it is a minor rather than a patch.
- Contract: none
- User action: none, unless a deployment or dry run stops with `The manifest <path> is missing, but run <run-id>'s journal remains`. If you moved the manifest aside, put it back and rerun. Otherwise the run is one an earlier release left unfinished on a first deployment, before any manifest existed, and it was not committed: reconcile it by hand as "When recovery fails" in `docs/recovery.md` describes for a run that was not committed, then rerun with `--dry-run`.
- Pull request: #2375

### A day over GitHub's search cap is searched again in parts instead of being reported as capped

- Level: minor. Counts that were capped at 1000 results for a day now include every result whenever a second date can separate them, and the `WARNING` remains only for a part that still exceeds the cap. A run that meets such a day takes several minutes longer for each one.
- Contract: none
- User action: none. A month a history file records as `capped`, including one an earlier version capped, is now searched again on every run instead of being read back. The `WARNING` that said such a month "is read from the history file" is gone. While the month's search is still capped, the run shows the search's own `WARNING`. Once a run counts the month whole, the month is recorded uncapped and closed. The report should run in the background, since each day over the cap adds minutes no one can know in advance.
- Pull request: #2373

### A dry run can preview a source takeover

- Level: minor, a deployer flag pairing that was refused is now accepted.
- Contract: `deploy.py` `--take-over-source`
- User action: none. To preview taking a source over from a moved or re-cloned checkout, run `python deploy.py --all --take-over-source --dry-run` before deploying with the flag.
- Pull request: #2372

### A deletion the deployer stopped partway is finished, not kept as your change

- Level: patch, a defect fix: recovery no longer reports the deployer's own half-done deletion as a change made after the plan.
- Contract: none
- User action: none. A `.<name>.deploying-del` a warning names holds only what the deployer was deleting: delete it once nothing holds it open.
- Pull request: #2372

### JSON a skill, the deployer, or a tool writes is checked before it is written

- Level: patch. A value that could not be read back strictly, such as `NaN` or a lone surrogate, is now refused where the JSON is written rather than wherever it is read later. The hub guard refuses a command it would hand its PowerShell reader holding one. When a reason the hub guard or the code-review reviewer guard gives for a denial holds one, the guard still denies the call, with a fixed reason in its place.
- Contract: none
- User action: none
- Pull request: #2367

### Skills stop with their FAILED line on a file they cannot read or a git that fails

- Level: patch, defect fixes that bring each skill to the failure its documentation already names.
- Contract: `curate-agent-memory` `resolve`, `curate-agent-memory` `reindex`, `curate-agent-memory` `audit`, `flag-review-finding` `findings`
- User action: none. Where a skill now ends with `FAILED <reason>` naming a file or git's reason, such as `curate-agent-memory`'s `resolve` for a settings file it cannot read (once `NO_MEMORY_DIR` and exit 0) or `flag-review-finding`'s `findings` for a review record missing between versions, fix what it names and rerun.
- Pull request: #2366

### deploy.py names a skill folder that holds no SKILL.md

- Level: patch, a clearer refusal for the same refused source.
- Contract: none
- User action: none. `ERROR: Skill '<name>' has no SKILL.md file in skills/<name>` replaces `deploy-meta/<name>.json has no matching skill directory` when the folder is there; restore its `SKILL.md`.
- Pull request: #2366

### Every entry point states its exit codes in an exit table

- Level: patch, a contributor rule validation holds; no command changes.
- Contract: none
- User action: none for a user. A contributor adding an entry point or a subcommand writes its `Exit status:` table ("Script results" in `docs/adding-a-skill.md`) and a fault driver in `tests/malformed_input/faults_*.py`.
- Pull request: #2366

### The hub guard refuses every part of a command it does not understand

- Level: minor, a contributor-facing tool's contract changing what it refuses.
- Contract: none
- User action: none, in a clone that has not opted into the guard. In one that has, while a guarded hub is in view, a command holding a part the guard does not read, such as `export`, `eval`, `bash <script>`, a runner around git such as `timeout 60 git status`, arithmetic, a function definition, or a redirection to `"$out"`, is refused even when it names no git, where before it was allowed when the guard could model it; write it as plain commands with literal words. A hook that fails or times out now blocks the command rather than letting it run.
- Pull request: #2363

### update-coding-agent-skills names a deployed commit the clone lacks and a list git cannot read

- Level: minor, since `NOT_IN_CLONE` and `UNLISTED` are new output lines of a shipped skill, which Versioning places at minor; the rest is a defect fix.
- Contract: `update-coding-agent-skills` `NOT_IN_CLONE`, `update-coding-agent-skills` `UNLISTED`. `NOT_IN_CLONE <commit>` follows `MAJOR_UPDATE` or `UPDATED` when the clone does not hold the deployed commit `deploy.py check` names, and `<current>` or `<old>` is then `unknown`; `UNLISTED <range> <reason>` stands in place of a list of commits git could not read.
- User action: none for an installation whose clone still holds the deployed commit. Where the clone has lost it, the update that installs this change runs the installed copy of `update.sh`, which still ends `FAILED cannot read the release tags reaching the deployed commit`, and `--cross-major` does not get past that: deploy the release once by hand from the clone, with `git fetch --tags origin` and then `python deploy.py --all --at refs/tags/<tag>` (adding `--unpin` if the deployer refuses to replace a pin), as "Deploying an earlier commit" in `docs/recovery.md` describes; that records a commit the clone holds, and later updates report a lost commit with `NOT_IN_CLONE`.
- Pull request: #2344

### flag-review-finding reads a pull request number and a review version in ASCII digits alone

- Level: patch, a defect fix: a value `int()` read as another number, such as `1_0` or a digit of another script, is now refused, as "A number read from text is bounded" promises.
- Contract: `flag-review-finding` `--pull`, `flag-review-finding` `--review-version`
- User action: none. A `--pull` or `--review-version` of `0`, a negative number, or anything but ASCII digits is now a usage error that exits 2, rather than `FAILED Pull number must be positive` with exit 1 or a flag on a different number.
- Pull request: #2340

### review-document refuses a pull request whose file listing GitHub cut short or whose head moved during the build

- Level: patch, a defect fix. The skill promised to review each document a pull request changes, and a capped or mixed listing broke that promise without any sign.
- Contract: `review-document` `FAILED`, `docs/code-review-operations-contract.md`
- User action: none. For a pull request too large for GitHub's files listing, review its documents by path in a checkout of its head. After a `changed while its files were being read` failure, build again.
- Pull request: #2333

### `--at` refuses a commit that sets export-ignore or export-subst

- Level: patch, a fix that refuses a tree `--at` used to deploy other than its commit's, naming the attribute and path to remove.
- Contract: none
- User action: none, unless you deploy with `--at`, or update through `update-coding-agent-skills`, from a source whose attributes set `export-ignore` or `export-subst`; then remove those attributes from the commit or the clone, as the refusal says.
- Pull request: #2339

### `run_bounded` takes an `index_file` keyword

- Level: patch, an addition no existing caller meets.
- Contract: none
- User action: none. A skill author whose script needs git to read another index passes `index_file=<path>` to ejryan89-skill-core's `run_bounded`; a `GIT_INDEX_FILE` in a given `env` is still left out, whatever its value, as every other repository-selecting variable is.
- Pull request: #2339

### Deployment reports say when a diff is cut, and more refusals end with their step

- Level: patch, a defect fix that brings the reports to what `docs/installation.md` and `docs/recovery.md` promise.
- Contract: none.
- User action: none. A deployed file whose name is not valid UTF-8 is now refused by name: rename or remove it.
- Pull request: #2335

### A review record that is not JSON fails the insights report as an invalid pair, and a stray period folder no longer stops later reports

- Level: patch, a defect fix that brings two failures to the lines `review-insights` already documents.
- Contract: none.
- User action: none.
- Pull request: #2328

### A home, COPILOT_HOME, or CLAUDE_CONFIG_DIR that is not an absolute path is refused

- Level: patch, a fix that makes these reads follow the platform seam's rule that nothing is looked up relative to the working directory.
- Contract: none
- User action: none, unless `USERPROFILE` (Windows), `HOME` (elsewhere), `COPILOT_HOME`, or `CLAUDE_CONFIG_DIR` is set to a relative path; set it to an absolute path, or unset `COPILOT_HOME` or `CLAUDE_CONFIG_DIR`, as the refusal says.
- Pull request: #2330

### A repository whose name starts with a dot, such as an organization's `.github`, is accepted

- Level: patch, a defect fix. GitHub allows the name, and the configuration's mute list already took it.
- Contract: `docs/code-review-operations-contract.md`, `skills/ejryan89-code-review-core/references/review-adapter.schema.json`
- User action: none. The adapter schema's `repository` pattern accepts the same names, so an entrypoint reviewer's author who validates against it should take the updated schema. Every `dashboard.status_overrides` key an earlier release wrote still loads.
- Pull request: #2324

### dotnet-dependencies reads condition values as MSBuild does

- Level: patch, a defect fix that brings the condition reader to the behavior its docstring promises.
- Contract: none
- User action: none. An answer may now include or leave out an item whose condition compares numbers or booleans in a spelling MSBuild reads differently from Python, and a comparison MSBuild refuses is reported as `UNREADABLE` rather than settled.
- Pull request: #2338

### A source file or folder spelled in another case is refused

- Level: patch, a refusal of a source the deployer already could not deploy alike on both platforms.
- Contract: none.
- User action: none for this repository's source. A source of your own whose `SKILL.md`, `source.json`, `skills`, `deploy-meta`, or `agents` folder, metadata `.json`, or owned shared asset is spelled in another case is refused with the rename to make.
- Pull request: #2342

### A read-only configuration or manifest is refused rather than replaced

- Level: patch, a defect fix: the deployer now leaves a file the user made read-only as it was, on every platform, as the skills' scripts already do.
- Contract: none
- User action: none. To have `configure` or a deployment rewrite a file you made read-only, make it writable first; the refusal, `ERROR: The manifest is read-only: <path>` for the manifest, names the file, and a dry run gives it too.
- Pull request: #2337

### `curate-agent-memory` refuses to delete a read-only memory, before it deletes any

- Level: patch, a defect fix: a refusal the command promised to make before any deletion now comes first.
- Contract: none; the refusal is a `FAILED <name>: <reason>` line delete already prints.
- User action: none. To delete a memory you marked read-only, make it writable first; on Linux and macOS this is new.
- Pull request: #2332

### A deployment finishes when it cannot delete a leftover after its commit

- Level: patch, recovery and finalization now follow one documented rule, and a cleanup step after the commit no longer fails a finished deployment.
- Contract: none
- User action: none. If a deployment warns that a previous copy `could not be deleted`, delete that `<name>.deploying-bak` once nothing holds it open; the next deployment of that item refuses until it is gone.
- Pull request: #2306

### A dry run from another checkout is refused as the deployment refuses it

- Level: patch, the dry run now gives the answer the deployment gives, which `docs/recovery.md` promises: it never shows a plan or a pending recovery the deployment would refuse.
- Contract: none
- User action: none. If `--dry-run` now stops with `Source '<id>' is deployed from <path>, not from this checkout`, run it from the recorded checkout. If this checkout replaces the recorded one, deploy with `--take-over-source`; a dry run cannot take the flag yet (#2313). A dry run while another deployment runs now stops with `Another deployment is running`, as that deployment's own journal is not a recovery to report; rerun it once the deployment finishes.
- Pull request: #2306

### `verify` names Codex's cap on the skills budget when an adapter is omitted

- Level: patch, a reworded report line with the same status and exit code.
- Contract: none.
- User action: none. An adapter reported `OMITTED` is brought back by offering Codex fewer skills; raising `skills.max_context_tokens` helps only below the cap Codex sets, which "Checking discovery" in `docs/codex-support.md` points at.
- Pull request: #2304

### A path or folder a skill or the deployer cannot examine fails the step instead of reading as missing or empty

- Level: minor, a new value in a report field an agent parses.
- Contract: skill invocation: curate-agent-memory's report field `exists` can now be `null`, with an `unreadable` reason, beside `true` and `false`.
- User action: none. The curate-agent-memory report gives a cited path it cannot examine as `"exists": null` with an `"unreadable"` reason, apart from `"exists": false` for one that is not there. A `FAILED` line, or a deployer `ERROR: Could not ...` line, naming a folder or file now means it is there but could not be examined or listed. Fix its permissions or the share it lives on and run the step again.
- Pull request: #2282

### A deployer path that cannot be stat'ed is refused, not read as absent

- Level: patch, a defect fix: the deployer read a path it could not stat as missing, and could remove a skill whose files it could not stat, or move a pin a user made without `--unpin`.
- Contract: none
- User action: none. If `deploy.py` or `deploy.py verify` now stops naming a path and `Access is denied` or another reason, fix the permissions on the path it names and rerun. If it stops with `git --exec-path failed`, or `check` reports Git Bash `UNUSABLE`, put right what Git reports, or set `GIT_BASH`.
- Pull request: #2272

### A reviewer manifest's specialist can route on the title, choose its pull-request texts, and be given the issues the pull request closes

- Level: minor, a new optional manifest field and a new record field, both backward compatible.
- Contract: `docs/code-review-operations-contract.md` (`specialists-manifest.specialists[].title_types`, `specialists-manifest.specialists[].inputs`, `request.pull_request.closing_issues` and its `issues[]` with `unreadable`, `record.pull_request.closing_issues` and its `issues[]` with `unreadable`, `fixture-pull.closing_issues[]` with `unreadable`), `review-prs` `validate-reviewer` `TITLE`
- User action: none. A manifest that uses neither field reviews as before.
- Pull request: #2271

### A configuration path that cannot be examined or listed is reported, not taken for no file or an empty folder

- Level: patch, a defect fix: the audit, `dotnet-format`, and the tools read a path they could not examine or list as absent or empty.
- Contract: none
- User action: none. An audit or run that now reports such a path names it and the reason; put the kind of file or folder the runtimes read there, or make it readable.
- Pull request: #2270

### A code-review file or folder that cannot be read fails the step instead of reading as missing

- Level: patch, a defect fix.
- Contract: a migrated legacy review index (`legacy-review.json`) the file system will not let be stat'ed or read is no longer treated as unreviewed: `enumerate` and `collect` report it as that repository's `REPOSITORY_FAILED` line, and `prepare` fails naming it, rather than offering the pull request for review again. An index that is not there, or whose content is invalid, still reads as unreviewed.
- User action: none. A `FAILED` or `REPOSITORY_FAILED` line naming a flag store, batch state, legacy index, review record, archive folder, run file, insights report, or tracker record now means it is there but could not be read; fix its permissions or the share it lives on and run the step again.
- Pull request: #2269

### Skills report a file or folder they cannot look at instead of treating it as missing

- Level: patch. A defect fix: an unreadable path read as absent, or a folder that could not be listed read as empty, gave an empty answer or let a change go ahead, such as a skill not found, no metadata, a worktree with nothing to lose, an orphan already gone, or a new output folder. The lines are the ones each skill already prints for an unreadable or refused input, now printed for one whose stat fails too.
- Contract: none
- User action: none.
- Pull request: #2268

### A source, staging, or backup path the deployer cannot stat is refused, not read as absent

- Level: patch, a defect fix: the deployer refused a path it could not read, but read one it could not stat as missing.
- Contract: none
- User action: none. If `deploy.py` now stops with `ERROR: Could not ...` naming the source's `.git` or the `agents` folder, or with `Cannot list the staging runs`, or recovery keeps a run it cannot read, fix the permissions on the path it names and rerun.
- Pull request: #2266

## v0.12.0

Level: minor.

### The deploy-time ShellCheck reads no settings of yours

- Level: patch. The check now judges the rendered content as the repository's rules say; a user's settings no longer narrow it.
- Contract: none
- User action: none. A ShellCheck too old to accept `--norc` fails the deployment's check: update ShellCheck.
- Pull request: #2249

### A stopped cleanup stays reported, and only offered branches are deleted

- Level: patch, a defect fix that brings repo-cleanup back to its documented behavior.
- Contract: none.
- User action: none.
- Pull request: #2250

### A `.backups` that is not a real folder is refused before the deployment changes anything

- Level: patch. The refusal enforces the order `docs/design.md` already promises ("before it changes anything"), and the action it asks is the one it names.
- Contract: none
- User action: none, unless a deployment refuses with `Backup path contains a symlink or junction` or `Backup path is not a directory`: replace that `.backups` with a real directory after checking what it holds, and rerun. On `Backup path is not writable`, give your account write access to that `.backups`, or replace it with a directory you own, and rerun. A recovery that warns it kept a previous copy as `<name>.deploying-bak` leaves that copy for you to check and move out of the root.
- Pull request: #2254

### A file the deployer or a skill cannot stat is unreadable, not absent

- Level: patch, a defect fix: the helper now does what its docstring and `docs/lessons.md` promise.
- Contract: none
- User action: none. If `deploy.py` now stops with `ERROR: Could not tell whether the manifest is there`, or the same for the configuration or `source.json`, fix the permissions on the path it names and rerun.
- Pull request: #2252

### Names on dotnet-format's lines stay on one line

- Level: patch, a fix to what the status lines print for a name holding a line break, a tab, or a control character; no line is added, renamed, or removed.
- Contract: `dotnet-format` `FILE`, `dotnet-format` `REFUSED`, `dotnet-format` `SAVED`, `dotnet-format` `UNFINISHED`, `dotnet-format` `DIAGNOSTIC`, `dotnet-format` `VIOLATION`, `dotnet-format` `FIXED`, `dotnet-format` `FAILED`, `dotnet-dependencies` `FAILED`. Each value these and the skills' other lines print is flattened: a tab, a control character, or a line break in it prints as a space, so one fact is one line. `dotnet-format` now also names as `SAVED` a fixed file whose name holds a line separator.
- User action: none.
- Pull request: #2253

### check names Git as needed to deploy from a Git checkout

- Level: patch, a defect fix: `check` reported ready to deploy where the deployment refuses.
- Contract: none
- User action: none. If `check` now lists `Git` as `MISSING` and is not ready to deploy, install Git as "Installing the tools" in `docs/installation.md` says, or put it on `PATH`, then run `check` again.
- Pull request: #2180

### verify reads a Codex catalog whose skill paths hold a space or a parenthesis

- Level: patch, a defect fix: a catalog verify refused as a shape it does not know is read as Codex wrote it.
- Contract: none
- User action: none. A profile folder whose name holds a space or a parenthesis no longer makes `python deploy.py verify` fail Codex when Codex names its skills by absolute path.
- Pull request: #2179

### dotnet-format's layout fixer refuses a read-only file per file and checks every file before the first write

- Level: patch. A fix: `--fix` and `--apply` now refuse a read-only file with a `REFUSED` line and carry on, where they used to stop with `FAILED` after part of the tree was changed.
- Contract: `dotnet-format` `REFUSED`, `dotnet-format` `FIXED`
- User action: none.
- Pull request: #2158

### A state file an earlier release wrote with a lone surrogate is refused

- Level: patch, a defect fix: this release's JSON reader refuses a lone surrogate (#2061), and the refusal of such a state file now says why and what to do.
- Contract: none
- User action: none, unless a deployment stops with `holds a lone surrogate` for the manifest, or with a malformed journal entry or lock metadata that holds one. Then follow "A state file that does not read" in `docs/recovery.md`. For the manifest, put back a copy you trust, or move it out of `~/.claude/skills` and run `python deploy.py --all --dry-run`. A journal's run is reconciled as "When recovery fails" describes, and a stale lock is removed.
- Pull request: #2121

### A read-only file a skill would replace is refused

- Level: patch, a defect fix. The refusal makes Linux behave as Windows already did, and keeps the protection the user set on the file.
- Contract: none
- User action: none, unless you keep a file a skill writes read-only, such as a dashboard, history, or plan file. Make it writable before running the skill; the failure line names the file. `dotnet-format`'s layout fixer instead skips such a file with a `REFUSED` line and goes on, as the entry for #2158 says.
- Pull request: #2117

### `deploy.py check` names a tag holding a Unicode line separator

- Level: patch, a fix: a valid tag name no longer fails the check.
- Contract: none.
- User action: none.
- Pull request: #2115

### Status overrides that render one heading share one tracker section

- Level: patch, a defect fix: the contract row now states the comparison the tracker applies, which only merges sections that already showed one heading.
- Contract: `update-pr-dashboard` `status_overrides`
- User action: none.
- Pull request: #2069

### dotnet-format refuses an absolute file-list entry or solution, and reads its file list NUL-separated

- Level: patch, a defect fix: the scripts now apply the refusal their contract already stated, and the file list is internal to one run of the skill.
- Contract: `dotnet-format` `REFUSED`, `dotnet-format` `FILE_LIST`
- User action: none. A file list saved from an earlier version is refused; run the skill again to write a new one.
- Pull request: #2059

### A status override spaced like a computed state is refused, and spellings of one status share a section

- Level: patch, a defect fix; the newly refused value already rendered a duplicate computed section.
- Contract: none
- User action: if `update-pr-dashboard` now fails naming a status override that duplicates a computed tracker state, set that pull request's override to another status, or clear it, with `dashboard_pipeline.py override`.
- Pull request: #2014

### Review records count each reviewer's searches, and the report notes a reviewer that read no source of a large change

- Level: minor. An addition: each reviewer the guard held records `searches`, its searches of the source snapshot (a `Grep` of a folder, a `Glob`, or a `source-search`), apart from `files_read`, null when not counted; the report's Reviewers table gains a **Searches** column and a line saying what each read column measures; and a report notes each reviewer that read and searched no source when the pull request changes more lines than the new configuration value `unread_source_lines` (200 by default). The note changes no verdict. An earlier release refuses a record that has `searches`, and a canary's `STATS` reviewer lines end in `searches=<n>`.
- Contract: `docs/code-review-operations-contract.md`
- User action: none; set `unread_source_lines` in the code-review configuration to change when the note appears.
- Pull request: #2001

### A finding linked to one of another category is a new finding

- Level: patch. A fix: a reviewer's link across categories was recorded as a repeat, which hid the new finding.
- Contract: none
- User action: none. A re-review may now show a finding as new, and count it, where it showed a repeat before.
- Pull request: #2000

### A report's severity headings and the dashboard's Findings cell name repeats

- Level: patch, a display fix with no contract change.
- Contract: none
- User action: none. A severity heading with repeats now reads like `SUGGESTIONS (2 issues, 3 repeats)`, and the dashboard's Findings cell like `2S open (3 repeats)`; both still count each problem once. A dashboard collected before updating shows no repeats until its next update.
- Pull request: #1993

### A review that ran several specialists keeps each one's summary

- Level: patch, a defect fix whose record change is an optional field older records omit.
- Contract: `record.review.reviewers[].summary`
- User action: none. A record written by this version holds each specialist's own summary on its reviewer run, and its stored combined summary labels each role as `role: text` without bold markers; a reader of records that relied on the `**role:**` markers reads the per-reviewer `summary` instead. An earlier release refuses a record that has a reviewer's `summary`, as it refuses one with `searches` (#2001).
- Pull request: #1992

### verify and the shepherd refuse an answer of a shape they do not know

- Level: patch, a defect fix: each ends such an answer in the failure it already reports for one it cannot read.
- Contract: none
- User action: none
- Pull request: #1988

### A reader names a section or setting of another kind instead of reading it as empty

- Level: patch, a defect fix: each reader ends such input in the failure or report it documents.
- Contract: none
- User action: none
- Pull request: #1987

### Input nested past the shared depth limit is refused by name

- Level: patch, a defect fix: each parser ends such input in the refusal it documents.
- Contract: none
- User action: none
- Pull request: #1986

### audit-skill refuses a plugin hooks file it cannot read whole

- Level: patch, a defect fix: the scan ends in the refusal it already documents for a hooks file it cannot read.
- Contract: none
- User action: none
- Pull request: #1981

### `check` and `--at` name Git's reason when Git cannot read the checkout

- Level: patch, a fix: a refusal that misnamed its cause now names it, and no command, option, or file format changes.
- Contract: none.
- User action: none. Where `check` or `--at` now stops with Git's reason, put right what Git reports in the checkout, then retry.
- Pull request: #1966

### A JSON or XML file nested past 100 levels is refused by name

- Level: patch, a defect fix: each reader ends such a file in the refusal it documents, and no shipped file nests near the limit.
- Contract: none
- User action: none
- Pull request: #1972

### A guarded reviewer may read a snapshot file whose path no UTF-8 log can hold

- Level: patch, a fix: such a Read was denied as a call the guard could not evaluate, though it lay inside the reviewer's run.
- Contract: none
- User action: none. The read goes uncounted in the reviewer's `files_read` and `bytes_read`.
- Pull request: #1979

### A git failure in curate-agent-memory, the code-review pipeline, or review-document is reported, not taken for absence

- Level: patch, a defect fix: each names git's reason where it used to go on as if something were absent.
- Contract: none.
- User action: none. If one of them now fails naming git, fix what git names, such as a broken ref or an unparsable `.git/config`.
- Pull request: #1959

### A ref git cannot read is reported, not taken for missing

- Level: patch, a defect fix: repo-cleanup and dotnet-format report git's reason where they used to go on as if the ref did not exist.
- Contract: none.
- User action: none. If repo-cleanup now reports `ERROR git ... failed in <repository>` or dotnet-format `git could not resolve`, fix what git names, such as a broken ref file or an unparsable `.git/config`.
- Pull request: #1950

### A guidance version git cannot read fails the report

- Level: patch, a defect fix: a git failure is reported as one instead of as a guidance version that is not there.
- Contract: none.
- User action: none. If `report` now prints `FAILED git could not read <path> at <commit>`, fix what git names, or point the repository's `checkout_path` at a git checkout of it.
- Pull request: #1952

### The release adds a twin sweep before tagging

- Level: patch. Maintainer-facing only: "Before tagging" in `docs/releasing.md` gains step 9, run through `audit-repository twins`; nothing changes for an installation.
- Contract: none.
- User action: none.
- Pull request: #1945

### A checkout git cannot read is reported, not taken for none

- Level: patch, a defect fix: these commands stop with git's reason where they used to go on without the repository.
- Contract: none.
- User action: none. If audit-skill, shepherd-pull-requests, or review-insights now prints `FAILED git could not read the checkout at <dir>`, fix what git names, such as an unparsable `.git/config` or a `safe.directory` entry, and run it again.
- Pull request: #1947

### Codex network reasons point to the guide

- Level: patch, since only the wording of declared reasons changes; no level, need, or behavior changes.
- Contract: `dotnet-format` `runtime_support` `codex`, `github-activity-report` `runtime_support` `codex`, `repo-cleanup` `runtime_support` `codex`, `review-document` `runtime_support` `codex`, `review-prs` `runtime_support` `codex`, `update-coding-agent-skills` `runtime_support` `codex`, `update-pr-dashboard` `runtime_support` `codex`
- User action: none. A configuration that already opens Codex's network keeps working; "Network access" in `docs/codex-support.md` gives both routes, and warns against setting a permission profile together with `[sandbox_workspace_write]`.
- Pull request: #1927

### A number past the digit limit ends in each skill's own failure

- Level: patch, a defect fix: `flag-review-finding` and `dotnet-format` end such input in their documented `FAILED <reason>` line, and `audit-skill` reports it as an `EXIT_MISMATCH` line or skips a shell exit no shell takes.
- Contract: none
- User action: none
- Pull request: #1925

### The deploy menu names an answer that is no whole number

- Level: patch, a defect fix: the menu gives its documented refusal.
- Contract: none
- User action: none
- Pull request: #1920

### A JSON integer past the digit limit is refused by name

- Level: patch, a defect fix: each JSON reader ends such a file in the refusal it documents.
- Contract: none
- User action: none
- Pull request: #1919

### A relative GIT_BASH or PROGRAMFILES is ignored on Windows

- Level: patch, a fix that makes the lookups follow the platform seam's rule that nothing is looked up relative to the working directory.
- Contract: none.
- User action: none, unless `GIT_BASH` is set to a relative path; set it to the absolute path of Git's `bin\bash.exe`.
- Pull request: #1904

### dotnet-format's and audit-ai-config's scripts refuse a `--timeout` that is no limit

- Level: patch, a defect fix: the values now refused raised, ran without a bound, or timed out every command at once.
- Contract: `dotnet-format` `--timeout`, `audit-ai-config` `--timeout`. `run_dotnet_format.py --timeout` and `mcp_handshake.py --timeout` now refuse `nan`, `inf`, zero, a negative number, and text that is not a number with argparse's usage error, exit 2, which names the value: `argument --timeout: must be a positive number of seconds, not <value>`.
- User action: none.
- Pull request: #1914

### The duplication policy refuses a shared fact restated outside its owner

- Level: patch, a contributor-facing validation change with no contract change.
- Contract: none
- User action: none for a user who deploys. A contributor who writes git's value options, a suite-name pattern such as a `test_*.py` glob, or the code-review default configuration name as a literal outside its owner now fails validation. Import the fact or call what the owner offers, such as `is_suite_name`, or sanction it with `DUPLICATION_ALLOWED` as "Validation" in `docs/adding-a-skill.md` describes.
- Pull request: #1878

### JSON a skill reads may no longer repeat a key or hold NaN

- Level: patch, a defect fix: every reader already documented refusing a file it cannot read, and JSON leaves a repeated key and `NaN` undefined.
- Contract: none
- User action: none, unless a configuration or tracker file you edit by hand repeats a key in one object or holds `NaN` or `Infinity`: the skill now refuses it with its usual failure line, naming the key or the constant. Remove the repeat and run again.
- Pull request: #1874

### update-pr-dashboard refuses a pull-request number longer than ten digits

- Level: patch, since no GitHub pull request has such a number.
- Contract: `update-pr-dashboard` `FAILED`. It now also ends `update`, `review-arguments --confirm`, and `override --set` or `--clear` given a pull-request identity whose number has more than ten digits, and `update` given a tracker input file larger than the code-review core's `read_json` reads.
- User action: none.
- Pull request: #1874

### Contributors install the development lock with hash checking and wheels only

- Level: patch. A documented command and a printed hint gain the flags CI already passes.
- Contract: none
- User action: none for a user who deploys. A contributor installs the development dependencies with `python -m pip install --require-hashes --only-binary :all: -r requirements-dev.txt`.
- Pull request: #1869

### audit-skill reads a Python script's git network calls

- Level: patch. A fix: the derivation now matches what the runtimes need, and no shipped skill's declared support changes.
- Contract: `audit-skill` `RUNTIME_NEEDS`
- User action: none
- Pull request: #1868

### dotnet-format refuses a solution outside the repository

- Level: patch, a defect fix that brings the formatter and the layout checker in line with the skill's promise to act only inside the repository.
- Contract: `dotnet-format` `FAILED`. `run_dotnet_format.py check` and `fix` and `csharp_layout.py config` now also end with `FAILED the solution <solution> is refused: <reason>` when `--solution` lies outside the repository root, through `..`, an absolute path, a network share or device form, or a symbolic link or junction.
- User action: none. The solution `resolve` chooses always lies inside the root.
- Pull request: #1870

### Codex's guide gives a permission profile for network access

- Level: patch, a documentation change to the Codex guide.
- Contract: none
- User action: none. A machine that turned the network on with `[sandbox_workspace_write] network_access = true` keeps working. To move to the permission-profile route that "Network access" in `docs/codex-support.md` now gives, remove `sandbox_mode` and `[sandbox_workspace_write]` from `~/.codex/config.toml` first, since the two do not combine.
- Pull request: #1866

### The hub guard refuses a script pwsh reads from its standard input

- Level: patch, a defect fix in a contributor-facing tool with no contract change.
- Contract: none
- User action: none, in a clone that has not opted into the guard. In one that has, while a guarded hub is in view, a command that names git and pipes a script to `pwsh`, or runs `pwsh -Command -` or `pwsh -File -`, is now refused as one the guard cannot read, where before it was allowed. Pass the script with `pwsh -Command`, which the guard reads.
- Pull request: #1865

### The hub guard refuses a script bash or sh reads from its standard input

- Level: patch, a defect fix in a contributor-facing tool with no contract change.
- Contract: none
- User action: none, in a clone that has not opted into the guard. In one that has, while a guarded hub is in view, a command that names git and pipes a script to `bash` or `sh`, or runs `bash -s`, is now refused as one the guard cannot read, where before it was allowed. Pass the script with `bash -c` or a here-string, which the guard reads.
- Pull request: #1851

### review-insights names a repository whose guidance overlap search stopped at its budget

- Level: minor, a new output line.
- Contract: `review-insights` `GUIDANCE_SEARCH_STOPPED`
- User action: none. When a report prints `GUIDANCE_SEARCH_STOPPED <repo> pairs=<n>`, a missing `GUIDANCE_OVERLAP` for that repository does not mean its guidance agrees.
- Pull request: #1864

### review-insights refuses a report with a field missing or of the wrong kind, naming it

- Level: patch. A fix: `decide`, `decide-custom`, `synthesize`, and `report` already promised a `FAILED <reason>` last line.
- Contract: `review-insights` `FAILED`
- User action: none. A hand-edited report that lacks a field now fails with a line naming the field; restore it, or run `report` again for that range.
- Pull request: #1864

### The adapter result schema reads whitespace and line ends as the validator does

- Level: patch. A fix: the schema states only what `check` already accepts and refuses, now for every character.
- Contract: `skills/ejryan89-code-review-core/references/review-adapter.schema.json`
- User action: none. A tool that validated adapter results against the schema now accepts a field holding U+FEFF and refuses one that is blank by Python's whitespace or a title broken by a Python line end, as `check` always did.
- Pull request: #1863

### configure clears a repo-cleanup list with a lone `-`

- Level: patch. A defect fix: the prompt promised an empty answer meant none, and nothing let a user clear a list already set.
- Contract: none
- User action: none. To clear `REPO_CLEANUP_REGENERABLE` or `REPO_CLEANUP_PROTECTED`, answer `-` at its prompt instead of running `configure --reset`.
- Pull request: #1862

### Manifest refusals say what to do, and print forward-slash paths

- Level: patch, a defect fix that brings refusal output in line with what `docs/installation.md` already promises.
- Contract: none
- User action: none. A manifest the deployer refuses now ends with the step to take: put back a copy you trust, or move it out of the skills folder and rerun with `--dry-run`, as "A state file that does not read" in `docs/recovery.md` describes. A manifest a newer checkout wrote asks you to update this checkout with `git pull` or deploy from that checkout. On Windows, the manifest's path and the configuration file's path in a refusal print with forward slashes, as every other deployer path does.
- Pull request: #1861

### The hub guard reads a computed PowerShell provider path as Bash reads a computed export

- Level: patch, a defect fix in a contributor-facing tool with no contract change.
- Contract: none
- User action: none, in a clone that has not opted into the guard. In one that has, a PowerShell cmdlet that writes, removes, or renames an environment variable at a path or name the command computes, such as `Set-Item $p x` or `Set-Content $p x`, now leaves a later git call that writes the checkout unplaced, so it is refused while a guarded hub is in view, as after `export "$v"=x` in Bash. Write the path out, or end it in a literal file name such as `"$dir\out.txt"`.
- Pull request: #1850

### repo-cleanup drops `summary` and `sweep --plans`, and a never-offered confirmation exits 1

- Level: minor. A subcommand and an option are removed and an exit code changes, which before 1.0.0 a minor carries. The skill's own steps never ran either removed command, and they act on the printed lines, not the exit status.
- Contract: `repo-cleanup` `summary`, `repo-cleanup` `--plans`, `repo-cleanup` `delete-local`, `repo-cleanup` `force-delete`, `repo-cleanup` `remove-directory`
- User action: none for the skill. A script of your own that ran `repo_cleanup.py summary --plan <file>` runs `report --record <plans directory>` instead. One that passed `sweep --plans <directory>` drops the option and reads the directory from the `PLANS` line.
- Pull request: #1860

### audit-skill names a missing script, an unreadable trigger scenario, and the needs of a skill it invokes

- Level: minor, since the skill prints new output lines and gives `RUNTIME_NEEDS` a new evidence kind; a user who updates needs to do nothing.
- Contract: `audit-skill` `SCRIPT_MISSING`, `audit-skill` `TRIGGER_SCENARIO_UNREADABLE`, and `audit-skill` `RUNTIME_NEEDS` (the `skill <name>` evidence)
- User action: none
- Pull request: #1853

### The hub guard notes a checkout git cannot read, and worktrees list shows a count it could not take as unknown

- Level: patch, a defect fix in a contributor-facing tool with no contract change.
- Contract: none
- User action: none. In a clone that has opted into the guard, a checkout git answers with an error for is noted on stderr instead of passed over in silence. `python tools/worktrees.py list` shows a count it could not take as `?` with the state `unknown`, where before it showed `-` and `in progress`.
- Pull request: #1846

### A malformed hooks file, file list, or memory index is refused by name

- Level: patch, a defect fix that brings each reader in line with the refusal it already documents.
- Contract: none
- User action: none.
- audit-skill's scan ends with `FAILED <file> is not a JSON object` for a plugin hooks file that is not an object, where it read it as holding no hooks.
- dotnet-format ends with `FAILED <file> line <n> holds a NUL, which no path holds` for a file list with a NUL on a line.
- curate-agent-memory ends with `FAILED <dir> is not a memory directory: its MEMORY.md cannot be read: not a file` when a directory stands in the index's place, where it said the folder holds no `MEMORY.md`.
- Pull request: #1848

### A skill whose sibling skill is missing ends with a FAILED line

- Level: patch, a defect fix that brings the missing-sibling exit in line with the "Script results" contract.
- Contract: none
- User action: none. A skill script run without a sibling skill it needs now prints `FAILED MissingSibling: skill <name> has no scripts under <folder>; <remedy>` on stdout and exits 1, where it printed the reason on stderr. A git stream that has not exited in time is now reported as `had not exited after <n> seconds` instead of `gave no output`.
- Pull request: #1840

### The merge shepherd updates one pull request at a time and can hold one from merging

- Level: patch, a change to a repository skill's behavior and status lines; nothing ships.
- Contract: none
- User action: none. A shepherd session running `shepherd-pull-requests` now meets `HELD #<number> <branch> "<reason>"` for a pull request whose body holds a line `Shepherd-hold: <reason>`, which it does not arm and whose auto-merge it turns off. It also meets the `disarmed` action, and `next-tick` on every behind pull request but the one it updates. A `HUB` line naming a hub the tick runs in and could not fast-forward now exits 1.
- Pull request: #1845

### The release and rotation briefs ask four questions, and a duplicate search says when it was cut short

- Level: patch, a change to a repository skill's briefs and status lines; nothing ships.
- Contract: none
- User action: none. `audit-repository` now prints `QUESTION <name> <what>` lines and a `question` finding field for release and rotation runs, and `MATCHES_CUT <query> <limit>` after a duplicate search that found more issues than it lists.
- Pull request: #1845

### The hub guard reads `env -C<dir>`, and only the last of several `-C`

- Level: patch, a defect fix in a contributor-facing tool with no contract change.
- Contract: none
- User action: none, in a clone that has not opted into the guard. In one that has, a git call that `env -C<dir>` starts, with the directory attached, is now judged where `env` runs it, where before it was refused as an option the guard could not read. Given two `-C`, `env` runs its command in the last one, read from where `env` starts, and the guard now judges the call there rather than at the two joined.
- Pull request: #1843

### update-pr-dashboard can mute repositories it names as untracked

- Level: minor. An addition: `repository --ignore "<owner/repo or pattern>"` records an entry in the code-review configuration's new `dashboard.ignored_repositories`, printing `IGNORED <entry>`, and `--unignore` removes one, printing `UNIGNORED <entry>`. `collect` no longer prints `UNTRACKED_REPOSITORY` for a repository an entry matches, and prints `UNTRACKED_IGNORED <count>` when it mutes any. An entry that a repository of a set matches is refused.
- Contract: `update-pr-dashboard` `repository --ignore`, `update-pr-dashboard` `repository --unignore`, `update-pr-dashboard` `IGNORED`, `update-pr-dashboard` `UNIGNORED`, `update-pr-dashboard` `UNTRACKED_IGNORED`, `config.dashboard.ignored_repositories`
- User action: none. To silence a repository you will never track, ask the skill to mute it.
- Pull request: #1841

### A deeply nested or misshapen JSON file is refused by name, not as a RecursionError

- Level: patch, a defect fix that brings each reader in line with the refusal it already documents.
- Contract: none
- User action: none. repo-cleanup refuses a cleanup policy, plan, or sweep record nested past the recursion limit with its `FAILED cannot read ...` line, and review-insights refuses such a synthesis input, or one whose seal line is not an object, with `FAILED the synthesis input ...`. Both ended in `FAILED RecursionError` or `FAILED AttributeError` before. review-document refuses a recorded file saved with a byte-order mark as not a file it recorded.
- Pull request: #1839

### github-activity-report searches only the months a history lacks, and a deployer source refuses a repeated JSON key

- Level: patch. Fixes: a gap in a `github-activity-report` history no longer re-searches the closed months after it, and a history count too long to convert is a `[history]` failure rather than an unclassified one. The deployer now refuses a `deploy-meta` file or `source.json` that repeats a key or holds `NaN`, which it used to read as one of the values.
- Contract: none
- User action: none, unless you maintain your own skill source whose `deploy-meta` or `source.json` repeats a key; remove the repeat.
- Pull request: #1844

### audit-ai-config reports new findings for Codex hooks, Copilot's reading of .claude/agents and .claude/rules, and long code-review instructions

- Level: patch. These are defect fixes that bring the checks in line with the runtimes' current documentation. No argument, status line, or exit code changes.
- Contract: none
- User action: none. On your next audit, expect these new results:
  - an `ERROR` for a Codex `prompt`, `agent`, or `http` hook, or an `mcp_tool` hook without `server` or `tool`;
  - a `WARNING` for an `async` Codex guard;
  - `copilot-agent` and `collision` findings on `.claude/agents` files;
  - a `layering` `WARNING` for an instruction file over about 1,000 lines that code review reads;
  - an inventory `ERROR` for an undecodable `CLAUDE.local.md`, `GEMINI.md`, or `REVIEW.md`;
  - `.claude/rules` listed among Copilot CLI's sources;
  - Claude Code in the scope of a repository whose only instructions are `AGENTS.md`.
- Pull request: #1842

### The hub guard's hook command notes an opt-in git cannot read

- Level: patch, since it brings the hook command to the behavior the guard's contract already states and changes no contract item.
- Contract: none
- User action: none. A clone that sees the note has a `coding-agent-skills.hubGuard` value that is no boolean or a `.git/config` git cannot parse, and the guard is not running there until it is fixed.
- Pull request: #1838

### A pin whose release tags Git cannot read is refused with Git's reason

- Level: patch, a defect fix that corrects a refusal's stated cause; nothing that ran before stops now.
- Contract: none
- User action: none. If a deployment or the update stops with `ERROR: Refusing to replace the deployment pinned with --at <ref> to <commit>: Git could not read the release tags reaching it`, put right what Git reports in the clone, then retry.
- Pull request: #1835

### A JSON file that repeats a key or holds NaN is refused, not read

- Level: patch, a defect fix that brings each reader in line with the refusal it already documents.
- Contract: none
- User action: none. audit-ai-config reports its "Could not read or parse" `ERROR` for a `.mcp.json`, `.claude/settings.json`, or other configuration file that repeats a key or holds `NaN`; curate-agent-memory refuses such a settings file as unreadable; audit-skill, review-document, and the code-review skills end in their documented refusal or `FAILED` line; and the code-review analyzer inventory lists such a `package.json` as present but unread. Such a file was read with the repeated key's last value before; remove the repeat to have it read.
- Pull request: #1830

### verify reports a Codex adapter its skills budget leaves out, and refuses a catalog it cannot read

- Level: patch. These are defect fixes in a read-only command, and no contract item changes.
- Contract: none
- User action: none. If `python deploy.py verify` reports an adapter as `OMITTED`, Codex's skills budget is leaving that adapter out of the model's catalog. Deploy fewer skills, or raise `skills.max_context_tokens` in Codex's `config.toml`, as "Checking discovery" in `docs/codex-support.md` says.
- Pull request: #1831

### A deployment refuses a deployed folder it cannot list

- Level: patch, a defect fix: a folder that could not be listed was hashed and link-checked as empty.
- Contract: none
- User action: none. If a deployment names a folder it cannot list, make the folder readable or move it out of the deployed skill, then deploy again.
- Pull request: #1824

### audit-skill's scan fails on a script it cannot measure

- Level: patch, a defect fix: a script that is not UTF-8 text or not valid Python was left out of the status-line comparison without a sign.
- Contract: `audit-skill` `FAILED`. `scan` now also ends `FAILED cannot read <path>: not UTF-8 text` or `FAILED cannot read <path>: not valid Python` for a script whose status lines it compares.
- User action: none.
- Pull request: #1824

### A skill listing in a shape verify does not know is a discovery failure

- Level: patch, a defect fix: a listing verify misread is now reported as unread.
- Contract: none
- User action: none. If a runtime release changes its skill listing, `python deploy.py verify` prints `DISCOVERY_FAILED` with the entry's shape rather than a skill missing or turned off; report it.
- Pull request: #1823

### A directory in a configuration file's place is reported, not read as no file

- Level: patch, a defect fix that brings each reader in line with the refusal it already documents.
- Contract: none
- User action: none. audit-ai-config now reports an `ERROR` (or, for the manifest, its malformed-manifest warning) where a directory stands in place of `.mcp.json`, `.github/mcp.json`, `.vscode/mcp.json`, `.codex/config.toml`, `.claude/settings.json`, or `.github/ai-config-manifest.json`, where it reported nothing before; curate-agent-memory, audit-skill, dotnet-format, and review-document end in their documented refusal or `FAILED` line where they read the directory as absent or ended in a traceback.
- Pull request: #1820

### check reports a tool whose version it cannot read as unusable

- Level: patch, a defect fix: `check` stops reporting a tool as meeting its floor without reading its version.
- Contract: none
- User action: none. If `check` now lists `gh`, `copilot`, `codex`, `dotnet`, or Claude Code as `UNUSABLE` with a `version probe` reason, run that tool's `--version` to see why it fails, and repair or reinstall it.
- Pull request: #1804

### A deployment from a Git checkout refuses when Git cannot read its commit

- Level: patch, a defect fix: the deployer kept the promise to record the source commit at each deployment by silently recording none.
- Contract: none
- User action: if a deployment stops with `ERROR: Cannot read the commit checked out in <checkout>`, install Git or put right what Git reports in the checkout, then rerun.
- Pull request: #1801

### A state file the deployer cannot read is refused by name

- Level: patch, a defect fix: these files used to end the run as an unclassified `FAILED` line, or a folder in their place read as no file.
- Contract: none
- User action: if a run stops with `Manifest is malformed` or `<file> at <path> is not a file`, follow "A state file that does not read" in `docs/recovery.md`.
- Pull request: #1801

### curate-agent-memory reads a junctioned `.claude` as a link

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none. On Windows, a repository whose `.claude` folder is a junction now has its `settings.local.json` counted as supplied by the repository, as Claude Code counts it, so a repository-level `autoMemoryDirectory` there gets the same trust and blocking notes as one in `settings.json`.
- Pull request: #1800

### A path or glob pattern holding a C1 control is refused, and the unguarded-reviewer reason names every setting that keeps the guard's hook from running

- Level: patch, two defect fixes: a check now refuses what its contract already promised to refuse, and a refusal reason names its causes completely.
- Contract: `review-prs` `check`, `review-document` `check`, `review-insights` `synthesize`, `docs/code-review-operations-contract.md`. Its format tables refuse a `snapshot_exclude` or `reads` pattern holding a line separator beside a control character. The refusal reason, printed in `RETRY <selector> <id> <prompt file> <reason>` and `FAILED <selector> <id> <reason>`, and by `synthesize` as `FAILED <reason>`, is now `the reviewer ran without its guard: the reviewer guard never claimed this role, as when a setting keeps its hook from running (disableAllHooks; allowManagedHooksOnly, which runs no hook an agent's frontmatter declares and no plugin's hook unless managed settings force-enable the plugin; or strictPluginOnlyCustomization locking hooks, which stops the frontmatter hooks of agents in ~/.claude/agents, or agents, which stops loading those agents), the code-review-reviewer agent or its hook did not load, or another agent type ran the role; let the code-review-reviewer agent load and run its hook, or prepare the review again with --inline`. For `synthesize`, the remedy reads `or follow the prompt yourself and record its result with --inline`.
- User action: on Claude Code with managed settings, the reviewer guard runs on the deployer route only without `allowManagedHooksOnly` and without a `hooks` or `agents` lock in `strictPluginOnlyCustomization`; otherwise review with `--inline`. A `snapshot_exclude` pattern or a condition's `reads` pattern holding a C1 control or a line separator must drop it. Nothing else.
- Pull request: #1793

### audit-ai-config reads no configuration through a link out of the repository

- Level: patch, a defect fix that makes the audit keep the promise its report already makes.
- Contract: none
- User action: none. A repository whose `.claude`, `.codex`, `.vscode`, or another configuration folder or file is a link or junction out of it now gets an `ERROR` for each file the audit could not read there, beside the link's `WARNING`. The optional handshake reports such a file as `CONFIG_ERROR` and starts none of its servers.
- Pull request: #1785

### audit-ai-config holds a skill's name to its folder only where Codex reads it

- Level: patch, a defect fix that brings a check in line with the pages it rests on.
- Contract: none
- User action: none. A skill whose `name` differs from its folder is no longer an `ERROR` under `.github/skills` or `.claude/skills`. Under `.agents/skills`, which Codex reads, it is now a `codex-skill` `ERROR`.
- Pull request: #1785

### SECURITY.md names every trust-model boundary, and the Copilot CLI host's statement names policy hooks

- Level: patch. Documentation of existing behavior: the security-sensitive areas now cover a standalone document, the PDF parser, and the content of a repository a skill examines, and the bounded Copilot CLI reviewer host is described as keeping the machine's policy hooks, which it always did.
- Contract: none
- User action: none
- Pull request: #1784

### dotnet-format refuses a listed file outside the repository root

- Level: patch, a fix: the user does nothing after updating.
- Contract: `dotnet-format` `REFUSED`, `dotnet-format` `FAILED`, and the exit codes of `csharp_layout.py check`, `csharp_layout.py config`, and `run_dotnet_format.py`. `REFUSED <entry> <reason>` now also names a FILE_LIST entry that is absolute, leaves the root through `..`, or is led out of it by a symbolic link or junction. The entry is never read or written, and the command exits 1. `csharp_layout.py config` ends with `FAILED no listed file lies inside the repository root` when it refuses every entry.
- User action: none.
- Pull request: #1782

### reindex and audit no longer follow an index entry outside the memory store

- Level: patch, a defect fix that brings `reindex` and `audit` to the rule their documentation already states.
- Contract: none.
- User action: none. An `UNREADABLE` line for an entry that reaches outside the store means you need to move that memory into the store, or remove the entry.
- Pull request: #1780

### The release step that records the validation legs' seconds records each platform's

- Level: patch. Maintainer-facing: "Before tagging" step 6 in `docs/releasing.md` now records Linux and Windows seconds, `tests/validation/job_seconds.json` keys its seconds by platform, and the Linux validation job runs as two legs.
- Contract: none
- User action: none; the maintainer's release step changes, and nothing changes for an installation.
- Pull request: #1779

### The local step before a pull request is the suites a change touches and the title and body checks; `--changed` is optional

- Level: patch, since only the contributor procedure changes and nothing a deployment reads.
- Contract: none.
- User action: none for a user who deploys. Before a pull request, a contributor runs the suites the change touches one at a time, each new regression test with the fix reverted, ruff and mypy for the changed files, and `python -B tests/run_validation.py --pr-title '<title file>' --pr-body '<body file>'`, which given alone runs only the pull request checks (#1792), as "Validation" in `docs/implementing-changes.md` lists; `--changed` is optional.
- Pull request: #1776

## v0.11.1

Level: patch.

### This release is a patch, though it adds a refusal

- Level: patch. Each change since v0.11.0 is a fix. The one new user action, adding `--unpin` to move a pin you made yourself, follows from the pin promise `SECURITY.md` already makes, so the refusal that asks for it holds an existing promise rather than adding a demand.
- Contract: none
- User action: none beyond the entries below.
- Pull request: #1686

### A pin you made by hand and an installed v0.11.0 update copy

- Level: patch, a one-time step for a release-channel user who pinned by hand.
- Contract: none
- User action: if you pinned with `deploy.py --at` and follow the release channel, the installed v0.11.0 copy of `update-coding-agent-skills` never passes `--unpin` there, so the v0.11.1 deployer refuses to move your pin and the old copy's advice to rerun with `--unpin` loops. Move the pin once from the clone with `python deploy.py --all --at refs/tags/v0.11.1 --unpin`, or switch to `main` for that run by starting `update-coding-agent-skills` with `--channel main --unpin`. From v0.11.1 on, the update skill's own `--unpin` works.
- Pull request: #1686

### A pin you make by hand over the update's own release pin is yours

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none. If you pin with `deploy.py --at <tag>` or `--at <commit>` at the commit the update's release pin already holds, your ref is recorded, and the next release stops at your pin until you pass `--unpin`.
- Pull request: #1719

### repo-cleanup's commands use a plan or sweep record only as the script last saved it

- Level: patch, a defect fix that brings the confirmation commands in line with the skill's documented promise.
- Contract: `repo-cleanup` `FAILED`. It now also ends `delete-local`, `force-delete`, `remove-directory`, `summary`, `report`, `sweep --record <dir>`, and `sweep --plans <dir>` over a directory that holds a sweep record, when the plan or record is not as the script last saved it, when a plan's repository root is no longer a Git repository or its worktree areas are not the ones a sweep derives, when a plan has a field missing or malformed (naming the field, where it ended in `FAILED KeyError` before), and when `--record` names a directory inside a skills directory.
- User action: none. A plan made before this update is refused; run the sweep again for a new one.
- Pull request: #1688

### The Mutation gate mutates only the lines a pull request changes

- Level: patch, since it changes a contributor's advisory check and no contract.
- Contract: none
- User action: none for a user who updates. A contributor's `Mutation gate` summary now counts only the mutants on the lines the pull request adds or changes, with each module's changed lines in its table, and it names each changed module whose changed lines yield no mutant.
- Pull request: #1708

### A user's pin stays theirs when an update deploys at its commit

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none. A pin you made with `deploy.py --at` keeps the ref you gave it when a deployment, the release update's included, lands on the pinned commit. The next release then stops at your pin until you pass `--unpin`. The refusal over a pin no longer suggests deploying another commit with `--at`, which is refused too.
- Pull request: #1707

### `deploy.py --at` and the update's release channel no longer replace a pin you made without `--unpin`

- Level: patch, a defect fix that holds the update to the pin promise `SECURITY.md` makes; the only new demand is `--unpin` where a run would otherwise have replaced your pin.
- Contract: `deploy.py` `--at`, `deploy.py` `--unpin`, `update-coding-agent-skills` `FAILED`, `update-coding-agent-skills` `DEPLOY_PINNED`. `--at` over a pin you made, at another commit, now refuses until `--unpin`, which `--at` now accepts. The update ends with `FAILED cannot read whether the installation is pinned: <reason>` when `deploy.py check` prints no deployed commit and pin it can read. `DEPLOY_PINNED` can now end the release channel too.
- User action: none, unless you move a pin you made with `--at` by hand. Then add `--unpin`, as in `python deploy.py --all --at <ref> --unpin`. If the update stops with `FAILED cannot read whether the installation is pinned`, run `python deploy.py check` in the clone and fix what it reports. To deploy over a pin you made, start the update with `--unpin`.
- Pull request: #1686

## v0.11.0

Level: minor.

### `review-prs` and `review-document` carry on past a reviewer result or run file the parser refuses

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none. A reviewer result nested past the recursion limit or holding an integer past the digit limit is now that role's `RETRY` line, and an unreadable run file that run's `FAILED` line. Before, either stopped `check --runs` for the whole list, on every retry. A result whose text spells a lone surrogate is now retried at `check` instead of failing the review at `finalize`.
- Pull request: #1634

### audit-ai-config reports a file past Python's integer-digit or nesting limit, and audit-skill keeps each value it prints on its line

- Level: patch, a defect fix that brings two shipped skills' output in line with their documented contracts.
- Contract: `audit-skill` `AGENT_HOOK`, `audit-skill` `PLUGIN_HOOK`, `audit-skill` `AGENT_TOOLS`, `audit-skill` `AGENT_MODEL`. A value these lines carry now has its whitespace and control characters removed.
- User action: none.
- Pull request: #1633

### github-activity-report's history records the search cap, and an older history is searched again once

- Level: minor, a new column in a shipped skill's history file format, before 1.0.0.
- Contract: `github-activity-report` `--history`
- User action: none required. The first run on a history file written before this change searches every month in the window again, as long as a first run, and rewrites the file with the new `capped` column. Rows of users that run does not report get `capped` set to `unknown`, and their months are searched again the next time those users are reported. Anything else that reads the file should expect the extra column.
- Pull request: #1632

### repo-cleanup's sweep runs under Windows PowerShell with empty cleanup settings

- Level: patch, a defect fix in a shipped skill's rendered command with no contract change.
- Contract: none
- User action: none beyond deploying the update. Codex sessions on Windows that fall back to Windows PowerShell no longer fail the sweep with "expected one argument" when `REPO_CLEANUP_REGENERABLE` or `REPO_CLEANUP_PROTECTED` is empty, the default.
- Pull request: #1631

### configure and the deployment menu name what an ended input left out

- Level: patch, a fix that adds one line to an existing cancellation message.
- Contract: none.
- User action: none.
- Pull request: #1597

### `github-activity-report` reports a group or a team, and can keep a history file

- Level: minor, new arguments and new failure kinds for a shipped skill, before 1.0.0.
- Contract: `github-activity-report` `--user` (now repeatable), `--team`, `--history`, `--metric`, `[scope]`, `[team]`, `[history]`
- User action: none. One `--user` without `--history` behaves as before. To report a team, the `gh` token needs `read:org` (`gh auth refresh -s read:org`); keep any history file private.
- Pull request: #1589

### Validation fails a skill status line its Markdown never names, and a grant a step only implies

- Level: patch, a contributor-facing validation change with no contract change.
- Contract: none
- User action: none for a user. A contributor who changes a skill names each line its scripts print in its Markdown, or says why the agent need not act on it. A contributor also names each granted tool other than a shell, such as `Read`, in the step that uses it. `skill_inventory.py scan <skill folder>` prints the lines validation fails on.
- Pull request: #1586

### repo-cleanup joins the Claude Code plugin

- Level: minor, a skill added to the plugin and a new install-time option.
- Contract: `repo-cleanup` `sweep`. It now ends with `FAILED --repos-root is empty; set the root directory for your git repositories` or `FAILED --repos-root <value> is not an absolute path` when the root is empty or relative.
- User action: a plugin user who updates `coding-agent-skills@ejryan89` sets the `repos_root` option to the absolute path of the folder holding their git repositories, with `claude plugin configure coding-agent-skills@ejryan89` or from `/plugin`, then starts `/coding-agent-skills:repo-cleanup`. A deployer user does nothing.
- Pull request: #1577

### Copilot CLI no longer refuses every shell call in a trusted checkout of this repository on Windows

- Level: patch, a fix to the contributor-facing hub guard hook with no change to what the guard refuses for Claude Code.
- Contract: `.claude/settings.json` (its `PreToolUse` hook command now opens with `set \` and `exit 0`, which ends it under PowerShell, and reads the opt-in through `test` and `env -u`)
- User action: none. A Claude Code session started before the update keeps the old command, which decides the same way. A Copilot CLI session in a trusted checkout picks up the new command at its next start.
- Pull request: #1576

### Copilot reviews start again on Copilot CLI 1.0.103

- Level: patch, because it restores Copilot reviews and the canary on the Copilot CLI that updated itself to 1.0.103, and changes nothing on earlier versions.
- Contract: none.
- User action: none beyond updating and deploying. On Copilot CLI 1.0.103, a review through `review-prs` or `review-document` that failed at start with "--no-remote was removed" now runs.
- Pull request: #1575

### The review record's `reviewers` field is stated as written for a single reviewer too

- Level: patch, a correction of the format's statement to what records already hold; no record changes.
- Contract: `docs/code-review-operations-contract.md`
- User action: none.
- Pull request: #1570

### audit-skill prints the description's first sentence

- Level: minor, a new output line.
- Contract: `audit-skill` `DESCRIPTION_LEAD`
- User action: none.
- Pull request: #1569

### audit-ai-config documents a second cause of FAILED

- Level: patch, documentation of existing behavior.
- Contract: `audit-ai-config` `FAILED`
- User action: none.
- Pull request: #1569

### The adapter-result schema states what `check` refuses

- Level: patch, a published schema brought to the validator it describes; no result accepted before is refused.
- Contract: `skills/ejryan89-code-review-core/references/review-adapter.schema.json`
- User action: none. An entrypoint reviewer's author who checks results against the schema now sees there what `check` already refused: a head that is not 40 or 64 hexadecimal digits, a blank string, a repository that is not `owner/name`, and a finding path that leaves the repository. A `pull_number` of the wrong kind is now refused as `adapter-result.pull_number must be an integer of at least 1, found ...`, naming the schema's floor.
- Pull request: #1568

### Agent configuration in a nested `.github` folder stays out of the snapshot

- Level: patch, a defect fix that brings the snapshot to its contract.
- Contract: none
- User action: none. Files under a nested `.github/agents`, `.github/instructions`, `.github/prompts`, or `.github/skills` folder, and `*.instructions.md` under any `.github` folder, are now `agent-instruction` exclusions, so reviewers judge them from the diff alone.
- Pull request: #1568

### audit-ai-config judges skills and agents by each runtime that reads them, and Codex's own project files

- Level: minor, new checks that add findings, and a narrowed one: `.claude/agents` files leave Copilot's agent rules for Claude Code's.
- Contract: `audit-ai-config` `agent-skill`, `audit-ai-config` `claude-skill`, `audit-ai-config` `claude-agent`, `audit-ai-config` `codex-skill`, `audit-ai-config` `codex-hook`, `audit-ai-config` `codex-agent`, `audit-ai-config` `codex-rule`, `audit-ai-config` `copilot-agent`, `audit-ai-config` `inventory`
- User action: none. A repository with skills under `.claude/skills` or `.agents/skills`, agents under `.claude/agents`, or hooks, agents, or rules under `.codex` may meet new findings; a `.claude/agents` file that met a `copilot-agent` finding now meets Claude Code's rules instead.
- Pull request: #1565

### Validation runs pytest in strict mode, and CI installs the development locks as wheels only

- Level: patch, a contributor-facing validation change with no contract change.
- Contract: none
- User action: none for a user who updates. A contributor's suites now run under pytest's `strict = true`, so a duplicate parametrization id is an error beside an unregistered marker or an unknown option. CI installs `requirements-dev.txt` and `requirements-mutation.txt` with `--only-binary :all:`, so a pin is raised only to a version with a wheel for every Python and platform CI runs.
- Pull request: #1566

### audit-ai-config judges a repository without a manifest, and Claude Code's own project files

- Level: minor, a new scope status and new checks that add findings without changing an existing one's meaning.
- Contract: `audit-ai-config` `scopeStatus` (`file-derived`), `audit-ai-config` `claude-settings`, `audit-ai-config` `claude-hook`, `audit-ai-config` `claude-memory`, `audit-ai-config` `inventory`, `audit-ai-config` `authority`, `audit-ai-config` `layering`
- User action: none. A repository without `.github/ai-config-manifest.json` now gets layering findings for the runtimes its files show, and one with a `.claude/settings.json`, `.claude/rules`, or a long or importing `CLAUDE.md` gets findings about them; read the new warnings rather than expecting the earlier `COMPLIANT`.
- Pull request: #1563

### audit-skill audits a skill's plugin form and names the plugins that carry a skill

- Level: minor, a new argument form and new lines in a shipped skill's commands, before 1.0.0.
- Contract: `audit-skill` `locate`, `inventory`, `scan`. `locate` takes `[<plugin>:]<SkillName> [<plugin folder>]` and prints `SCOPE plugin` followed by `PLUGIN <name> <root>`, and its empty-name line is `STOP usage: audit-skill [<plugin>:]<SkillName> [<plugin folder>]`; `inventory` prints `CARRIED_BY <plugin>`, `PLUGIN_CARRIES skill|agent <name>`, `LEFT_OUT <item> <reason>`, and `PLUGINS_UNREADABLE <reason>`; `scan` prints `PLUGIN_HOOK <event> <matcher> <program> timeout=<seconds or default> on_failure=<value>` and names a plugin's agents as `<plugin>:<agent>`.
- User action: none. To audit a plugin's copy, start `/audit-skill <plugin>:<skill>`, or give the folder `tools/plugin_build.py` printed after the name.
- Pull request: #1562

### update-pr-dashboard adds a repository to its set and names repositories it does not track

- Level: minor, a new command and new status lines in a shipped skill's invocation contract.
- Contract: `update-pr-dashboard` `repository`, `update-pr-dashboard` `ADDED_REPOSITORY`, `update-pr-dashboard` `ADDED_TO_SET`, `update-pr-dashboard` `UNTRACKED_REPOSITORY`, `update-pr-dashboard` `UNTRACKED_CHECK_FAILED`
- User action: none. Any `UNTRACKED_REPOSITORY` line from the next `collect` of a set names a repository whose pull requests the dashboard has been missing. Ask the skill to track it rather than editing `config.json` by hand.
- Pull request: #1560

### `repo-cleanup` reads its deletion lists from a shipped data file, and you can add to them in `configure`

- Level: minor, new configured variables and new `sweep` arguments and status lines, all additive.
- Contract: `deploy-meta/repo-cleanup.json` (its `required_vars`, which now carry `REPO_CLEANUP_REGENERABLE` and `REPO_CLEANUP_PROTECTED`); the configured variables `REPO_CLEANUP_REGENERABLE` and `REPO_CLEANUP_PROTECTED` (new, empty by default); `repo-cleanup` `--regenerable` and `--protected` (new `sweep` arguments); `repo-cleanup` `POLICY`, `ADDED`, and `REFUSED_ENTRY` (new status lines, printed by `sweep` after `PLANS` and repeated first by `report`); `repo-cleanup` `SUMMARY` (its "Protected release worktrees" row is now "Protected worktrees").
- User action: none to keep the shipped lists. To add regenerable directories or protected branch prefixes, run `python deploy.py configure`, answer the `REPO_CLEANUP_REGENERABLE` prompt with entries such as `.venv .tox=tox.ini,setup.cfg` and the `REPO_CLEANUP_PROTECTED` prompt with prefixes such as `hotfix/`, and deploy again; the next sweep prints each entry it used or refused.
- Pull request: #1559

### audit-skill reports the agents a skill starts, each subagent's model and effort, and the skill's runtime support

- Level: minor, new status lines and findings from a shipped skill's commands, before 1.0.0.
- Contract: `audit-skill` `DECLARED`, `audit-skill` `AGENT`, `audit-skill` `AGENT_TOOLS`, `audit-skill` `AGENT_MODEL`, `audit-skill` `AGENT_CLAUDE_MD`, `audit-skill` `AGENT_HOOK`, `audit-skill` `AGENT_UNREADABLE`, `audit-skill` `AGENT_UNDEFINED`, `audit-skill` `AGENT_UNDECLARED`, `audit-skill` `UNGUARDED_SUBAGENT`, `audit-skill` `SUBAGENT_MODEL`, `audit-skill` `SUBAGENT_EFFORT`, `audit-skill` `PINNED_SUBAGENT`, `audit-skill` `RUNTIME`, `audit-skill` `RUNTIME_NEEDS`, `audit-skill` `RUNTIME_UNDECLARED`, `audit-skill` `RUNTIME_UNREADABLE`, `audit-skill` `RUNTIME_OVERCLAIMED`. `DECLARED` gains the kinds `agent`, `tool`, and `optional-tool`, and `DECLARED agent <name> via <skill>`.
- User action: none. A report's Scope now states Models, Subagents, and Runtime support where it stated the frontmatter model, and an audit can raise the new findings on a skill that passed before.
- Pull request: #1558

### audit-ai-config reports a .mcp.json server Claude Code skips

- Level: patch, a defect fix: the audit now reports servers Claude Code never loads, which it passed before.
- Contract: `audit-ai-config` `SKIPPED` (the handshake prints `transport=unknown`, not `transport=http`, for a JSON server with a `url` and no `type`)
- User action: none; a repository the audit now fails needs `"type": "http"` (or `"sse"` / `"ws"`) on each remote `.mcp.json` server, no `"type": "sdk"` entry there, and no server with a reserved built-in name.
- Pull request: #1553

### `review-prs` and `review-document` carry on past a head settings file the analyzer inventory cannot parse

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none. A pull request whose head holds a `package.json`, `ruff.toml`, or `pyproject.toml` nested past the recursion limit, or holding an integer past the digit limit, is now reviewed with that file listed as unread. Before, it ended `prepare` for its whole group. A broken `pyproject.toml`, `package.json`, `setup.cfg`, or `tox.ini` now shows up in `analyzers.json` as unread instead of being left out.
- Pull request: #1552

### A deployment and `verify` say when COPILOT_HOME hides the adapters from Copilot CLI

- Level: patch, a defect fix that adds a warning and an explanation, with no contract change.
- Contract: none
- User action: none, unless you set `COPILOT_HOME` or start Copilot CLI with `--config-dir`. Copilot then reads none of the deployed skills; the deployment now warns, and to use them in Copilot leave `COPILOT_HOME` unset or set it to the `.copilot` folder of your home as the warning prints it, and start Copilot without `--config-dir`.
- Pull request: #1551

### audit-skill reports a runtime prompt's shape and a skill's trigger scenario

- Level: minor, since the skill prints new output lines and a user who updates needs to do nothing.
- Contract: `audit-skill` `TRIGGER_SCENARIO`, `audit-skill` `NO_TRIGGER_SCENARIO`, and the `audit-skill` `CUE` cues `after-reply`, `untrusted-after-contract`, and `emphasis`
- User action: none
- Pull request: #1548

### deploy.py verify names a failed `codex debug prompt-input` probe as that probe

- Level: patch, a defect fix: a message that named the wrong probe now names the one that failed.
- Contract: `deploy.py verify` (`Listed its skills, but cannot read which ones it offers the model (codex debug prompt-input): <reason>`, in place of `Cannot list its skills: <reason>` when only the catalog probe failed)
- User action: none. When you see the new sentence, run `codex debug prompt-input` by itself to see why it fails. The listing worked.
- Pull request: #1511

### The RECORDED line names its counts: new, open, and addressed findings

- Level: minor, a status line's field renamed and two fields added, before 1.0.0.
- Contract: `review-prs` `RECORDED`, `review-document` `RECORDED`. The line is now `RECORDED <selector> verdict=<verdict> new=<n> open=<n> addressed=<n> <report>`: `new` replaces `findings` and counts the findings the review raised, `open` counts the finding ledger's open entries after it, and `addressed` its entries closed as addressed.
- User action: anything that parses the `RECORDED` line outside the skills reads `new=` where it read `findings=`, and takes the report path after `addressed=<n>`.
- Pull request: #1547

### The update reads releases with the ejryan89-skill-core deployed beside it, not the clone's

- Level: patch, a defect fix in a shipped skill: one new `FAILED` reason and a fuller existing one, with no change to arguments, status words, or exit codes.
- Contract: `update-coding-agent-skills` `FAILED`. The new reason is `FAILED no ejryan89-skill-core release_tags.py deployed beside update.sh: <path>`, and `FAILED cannot read the commit of refs/tags/<tag>: <reason>` now ends with git's reason instead of nothing.
- User action: none from a v0.10.0 installation. An installation deployed at v0.9.0 whose clone already holds v0.10.0 or later stops with `FAILED cannot read the release tags reaching ...: ... can't open file '.../skills/skill-core/scripts/release_tags.py'`. Deploy once by hand from the clone with `python deploy.py --all --at refs/tags/<latest release>`, or `python3` where the deployer runs under that name. The update skill works from then on.
- Pull request: #1510

### The hub guard reads `nice -n5` and `stdbuf -oL`, and env and the prefix programs in PowerShell

- Level: patch, a defect fix in a contributor-facing tool with no contract change.
- Contract: none
- User action: none, in a clone that has not opted into the guard. In one that has, `nice -n5 git ...`, `stdbuf -oL git ...`, and other prefix options with their value attached are judged as the bare git call, allowed from a worktree, where before they were refused everywhere; a git call that `env`, `nohup`, `nice`, `timeout`, `stdbuf`, or the `time` program starts from the PowerShell tool is now refused in the hub where it was allowed; `git gitk` is no longer refused; and the guard's stderr notes begin `hub guard` rather than `worktrees guard`.
- Pull request: #1512

### audit-ai-config warns when Codex would read a root instruction file only in part

- Level: patch, a fix in what the audit reports; the user does nothing after updating.
- Contract: none.
- User action: none.
- Pull request: #1509

## v0.10.0

Level: minor.

### The hub guard reads a redirection before a command's name, and the `time` program's command

- Level: patch, a defect fix in a contributor-facing tool with no contract change.
- Contract: none
- User action: none, in a clone that has not opted into the guard. In one that has, a git call after a redirection that starts its command, such as `2>/dev/null git status` or `>out git commit`, is now judged as the bare call: allowed from a worktree, and in the hub refused only when it writes the hub, where before it was refused everywhere as unreadable. A git call the `time` program starts, as in `\time git commit` or `x=1 time git commit`, is now refused in the hub where it was allowed.
- Pull request: #1452

### On Claude Code, a reviewer result the reviewer guard never claimed is refused

- Level: minor, a new refusal and a new required argument to a shipped skill's command, before 1.0.0.
- Contract: `review-prs` `check`, `review-document` `check`, `review-insights` `synthesize`. The new refusal reason, printed in `RETRY <selector> <id> <prompt file> <reason>` and `FAILED <selector> <id> <reason>`, and by `synthesize` as `FAILED <reason>`, is `the reviewer ran without its guard: the reviewer guard never claimed this role, as when hooks are off (disableAllHooks, or allowManagedHooksOnly for a plugin's hooks), the code-review-reviewer agent or its hook did not load, or another agent type ran the role; enable hooks for the code-review-reviewer agent, or prepare the review again with --inline`. For `synthesize`, the remedy reads `or follow the prompt yourself and record its result with --inline`. `synthesize` now needs `--host RUNTIME` to record a result, and takes `--inline`.
- User action: on Claude Code, keep hooks enabled for the `code-review-reviewer` agent: no `disableAllHooks`, and for the plugin route no organization `allowManagedHooksOnly` that drops its hooks. Otherwise, review with `--inline`. Anything that calls `review_insights.py synthesize` outside the skill passes `--host`.
- Pull request: #1451

### Validation type-checks every root with mypy's strict mode, and a new root must join

- Level: patch, a contributor-facing validation change with no contract change.
- Contract: none
- User action: none for a user who updates. A contributor now meets `mypy --strict` in every file validation type-checks, the tests and `conftest.py` included. A pull request that adds a skill's `scripts/` or another type-check root lists it under `mypy-strict` in `pyproject.toml`'s `[tool.validation]`, and its code meets strict mode from the start; validation fails until it is listed.
- Pull request: #1450

### Validation type-checks ejryan89-code-review-core and review-document with mypy's strict mode

- Level: patch, a contributor-facing validation change with no contract change.
- Contract: none
- User action: none for a user who updates. A contributor changing `skills/ejryan89-code-review-core/scripts` or `skills/review-document/scripts` now meets `mypy --strict` there, in the skill's existing type-check job, as every other skill's `scripts/` already does.
- Pull request: #1449

### Validation type-checks the shipped and repository skills' scripts with mypy's strict mode

- Level: patch, a contributor-facing validation change with no contract change.
- Contract: none
- User action: none for a user who updates. A contributor changing a skill's `scripts/` (every shipped and repository skill but ejryan89-code-review-core and review-document, which follow) now meets `mypy --strict` there, in that skill's existing type-check job. That means every definition fully annotated, no bare generics, no `Any` returned where a type is declared, no implicit re-export (a suite imports a name from the module that defines it, or patches `os`, not `module.os`), and no unused `type: ignore`.
- Pull request: #1447

### Claude Code users can install the skills as a plugin from the repository's marketplace

- Level: minor, a second way to install that leaves the deployer and every deployed install as they were.
- Contract: `claude plugin marketplace add EJRyan89/coding-agent-skills`, `claude plugin install coding-agent-skills@ejryan89`, `claude plugin install coding-agent-skills-dotnet@ejryan89`, and the invocation form `/coding-agent-skills:<skill>` and `/coding-agent-skills-dotnet:<skill>`
- User action: none. A Claude Code user who wants the plugin runs `/plugin marketplace add EJRyan89/coding-agent-skills`, then `/plugin install coding-agent-skills@ejryan89`, once a release attaches the archives, and starts a skill as `/coding-agent-skills:<skill>`. The plugin carries neither `repo-cleanup` nor `update-coding-agent-skills`.
- Pull request: #1446

### Validation type-checks tools/ with mypy's strict mode

- Level: patch, a contributor-facing validation change with no contract change.
- Contract: none
- User action: none for a user who updates. A contributor changing `tools/` now meets `mypy --strict` there, in the job `static type check (mypy --strict tools)`. That means every definition fully annotated, no bare generics, no `Any` returned where a type is declared, no implicit re-export, and no unused `type: ignore`. The ejryan89-code-review-core and review-insights scripts and the `tests/validation` modules that `tools/` imports are held to it through that run too.
- Pull request: #1445

### Validation type-checks the deployer and deploy.py with mypy's strict mode

- Level: patch, a contributor-facing validation change with no contract change.
- Contract: none
- User action: none for a user who updates. A contributor changing `deployer/` or `deploy.py` now meets `mypy --strict` there, in a validation job of its own named `static type check (mypy --strict <root>)`. That means every definition fully annotated, no bare generics, no `Any` returned where a type is declared, no implicit re-export, and no unused `type: ignore`. `deploy.py` keeps `from __future__ import annotations`, so an annotation never runs before the version check.
- Pull request: #1442

### Validation type-checks ejryan89-skill-core with mypy's strict mode

- Level: patch, a contributor-facing validation change with no contract change.
- Contract: none
- User action: none for a user who updates. A contributor changing `skills/ejryan89-skill-core/scripts` now meets `mypy --strict` there. That means every definition fully annotated, no bare generics, no `Any` returned where a type is declared, no implicit re-export (a suite patches `os`, not `atomic_file.os`), and no unused `type: ignore`. A helper for one platform starts with a `sys.platform` check that raises elsewhere; code the suites also run on the other platform names `unused-ignore` in its ignore. An area added to `[tool.validation]` `mypy-strict` in `pyproject.toml` never leaves it.
- Pull request: #1440

### analyze-skill-cost is renamed audit-skill, and the hidden cores are renamed ejryan89-skill-core and ejryan89-code-review-core

- Level: minor. A renamed skill, which is major, carried in a minor before 1.0.0.
- Contract: `skills/analyze-skill-cost`, `skills/audit-skill`, `skills/skill-core`, `skills/ejryan89-skill-core`, `skills/code-review-core`, `skills/ejryan89-code-review-core`, `deploy-meta/analyze-skill-cost.json`, `deploy-meta/audit-skill.json`, `skills/ejryan89-code-review-core/references/review-adapter.schema.json` (moved with its skill, unchanged), `skills/ejryan89-skill-core/scripts/pdf_tool.py` (moved with its skill, its PDF provider floors unchanged)
- User action: run `python deploy.py --all` from the hub. The deployment moves each skill you have under an old name to its new one in one journaled step, which `--dry-run` lists under `RENAME` and the deployment under `RENAMED`:
  - `analyze-skill-cost` to `audit-skill`, with its runtime adapter;
  - `skill-core` to `ejryan89-skill-core`;
  - `code-review-core` to `ejryan89-code-review-core`, with the reviewer agent's guard hook now naming that folder.

A request you made for `analyze-skill-cost` carries to `audit-skill`. If you had modified one of the old skills, the update preserves it under its old name beside the new one, and its report line names the new name. An old core that a preserved skill still runs on stays with it. Carry your changes over, delete the old folder under `~/.claude/skills`, and the next deployment drops it. Start the audit as `/audit-skill <SkillName>`: `/analyze-skill-cost` no longer resolves. Update any permission rule or script you wrote for a path under `analyze-skill-cost/`, `skill-core/`, or `code-review-core/` to the new folder name.
- Pull request: #1439

### The hub guard places a git call after any prefix assignment Bash reads, subscripted ones included

- Level: patch, a defect fix in a contributor-facing tool.
- Contract: none; the guard now reads `NAME[SUBSCRIPT]=value git ...`, `NAME[SUBSCRIPT]+=value git ...`, and `NAME[SUBSCRIPT]=( ) git ...` as it reads `NAME=value git ...`, and `env` takes any `NAME=value` word.
- User action: none, in a clone that has not opted into the guard. In one that has, a git call after a subscripted prefix assignment, such as `a[1]=x git commit -m x` in the hub, is now refused where it was allowed, and so is one after `env 'a[1]=x'`.
- Pull request: #1437

### Every skill the model may start has a trigger scenario, and a changed description owes its run

- Level: patch, a contributor-facing validation policy and repository skill gate; no shipped skill changes.
- Contract: the `trigger_scenario_problems` validation policy, which fails on a model-invocable shipped skill without a trigger scenario of at least three requests and on a trigger scenario its loader refuses, and `change-skill`'s `GATE evaluate-skill <skill> trigger` line, with `GATE evaluate-skill <skill> if-prompt` now printed only for a skill with a review scenario
- User action: none for a user of the deployed skills. A contributor adding a model-invocable shipped skill writes its `tests/fixtures/skill-evals/<skill>/description/scenario.json`, and one changing such a skill's description runs `evaluate-skill` for it before the pull request.
- Pull request: #1438

### Argument counts and blind excepts are ratchets

- Level: patch, a contributor-facing lint change with no contract change.
- Contract: none
- User action: none. A contributor's function takes at most the arguments `max-args` in `pyproject.toml` allows; group the ones that travel together rather than raise it. A blind except, `except Exception` or `except BaseException`, is refused, and validation counts the ones kept with `# noqa: BLE001 - <reason>` and fails past `BLIND_EXCEPT_CEILING` in `tests/validation/python_checks.py`, so catch the exceptions the code expects. A change that lowers either count lowers its ceiling and literal with it.
- Pull request: #1436

### The return, idiom, comprehension, performance, and builtins lint rules hold the Python

- Level: patch, a contributor-facing lint change with no contract change.
- Contract: none
- User action: none. A contributor's Python meets ruff's `RET`, `PIE`, `C4`, `PERF`, and `A` rules: a list built by a loop that only appends is a comprehension or an `extend`, a dictionary whose every key takes one value is `dict.fromkeys`, a value is returned without first being named, and no variable, argument, class, or module takes a builtin's name. `python -m ruff check --fix` rewrites the findings ruff marks safe.
- Pull request: #1435

### code-review-core defines its severities, verdicts, dispositions, ledger states, and dispatch modes once, with types

- Level: patch, a typing refactor with no behavior or contract change.
- Contract: none
- User action: none
- Pull request: #1433

### review-prs reviews a pull request over GitHub's diff limits

- Level: minor, a new optional record field and new `NOTE` and `FAILED` lines, with no change asked of an existing configuration.
- Contract: `record.review.snapshot.diff`, `review-prs` `NOTE`, `review-prs` `FAILED`
- User action: none. A repository whose pull requests can pass the diff endpoint's limits, such as its 300 changed files, should set `checkout_path`. Without a checkout, the diff comes from GitHub's files API, which lists at most 3,000 files and leaves out the patch of a very large file diff, and such a review is `INCOMPLETE`.
- Pull request: #1432

### A damaged or hand-edited `run.json` fails with a line naming its first wrong field

- Level: patch, a defect fix: a run `prepare` wrote reads as before.
- Contract: `review-prs` `FAILED`, `review-document` `FAILED`
- User action: none. A `FAILED <run directory> <reason>` line from `check` or `finalize`, or a `FAILED <reason>` line from `next-role`, `wait`, or `dispatch`, for a run whose `run.json` was damaged or edited, now gives as its reason `run.<field> must be <expected>, found <found>`, such as `run.roles[1].model must be a non-empty string or null, found integer 5`; prepare the review again.
- Pull request: #1431

### The hub guard reads Bash array assignments

- Level: patch, a contributor-facing tool now reads a Bash shape it refused, with no contract change.
- Contract: none
- User action: none. In a clone that has opted into the guard, a Bash command holding `NAME=( )` or `NAME+=( )` that names git is now judged by where each git call acts, where before it was refused as unreadable.
- Pull request: #1430

### skill-core's error kinds and the deployer's plan actions and journal ops are typed vocabularies

- Level: patch, a typing refactor with no behavior or contract change.
- Contract: none
- User action: none
- Pull request: #1428

### A walk-through sets up a new Windows machine in order and names the conditions a fresh install meets

- Level: patch, a documentation addition with no contract change.
- Contract: none
- User action: none
- Pull request: #1427

### The pytest-style lint rules hold the suites

- Level: patch, a contributor-facing lint change with no contract change.
- Contract: none
- User action: none. In a test function or a helper it calls, a contributor writes the forms the `PT` rules hold: one condition per `assert`, `pytest.raises` around one statement, a `parametrize` table in its documented shape, and a mock's `return_value` in place of a lambda. A unittest test case still checks with its own assertion methods, since PT009 and PT027 are not selected.
- Pull request: #1425

### The suites' helpers assert directly, and a module-level TestCase is refused

- Level: patch, a contributor-facing test change with no contract change.
- Contract: none
- User action: none. A contributor checks in a test function, or a helper it calls, with a plain `assert` or `pytest.raises`, never through a `unittest.TestCase` made outside a class, which validation now refuses; a unittest test case keeps its own assertion methods.
- Pull request: #1424

### update-coding-agent-skills reads Partial on Codex, whose default sandbox turns off the network its fetch needs

- Level: minor, a runtime-support declaration that now says what Codex's default sandbox withholds.
- Contract: `update-coding-agent-skills` `runtime_support` `codex`
- User action: to run the update from Codex without approving each git call, or through `codex exec`, add `[sandbox_workspace_write]` with `network_access = true` to `~/.codex/config.toml`, as "Network access" in `docs/codex-support.md` shows.
- Pull request: #1423

### On Windows, check reports a pwsh that is the packaged PowerShell's alias, and the install command names the MSI build

- Level: minor, a new `check` line.
- Contract: `deploy.py check` (the `pwsh command` line)
- User action: if `check` reports `pwsh command` as `UNUSABLE`, run `winget uninstall --id Microsoft.PowerShell`, then `winget install --id Microsoft.PowerShell --installer-type wix`, as "PowerShell app execution alias" in `docs/codex-support.md` shows.
- Pull request: #1423

### audit-ai-config counts CLAUDE.md, GEMINI.md, and REVIEW.md as code review's instructions

- Level: patch, a check corrected to what GitHub's code review reads today.
- Contract: `audit-ai-config` `layering`
- User action: none. A repository that failed only on the code-review `layering` `ERROR` now passes.
- Pull request: #1423

### The regression suites may use a plain assert

- Level: patch, a contributor-facing lint change with no contract change.
- Contract: none
- User action: none. A contributor writing a test under `tests/` or in a skill's `scripts/test_*.py` checks with a plain `assert`, which pytest rewrites to show both sides when it fails, in place of a module-level `CHECK = unittest.TestCase()`; S101 still refuses an `assert` in every other module.
- Pull request: #1422

### tools/skill_evals.py is cut into modules by concern

- Level: patch, an internal refactor of a contributor tool with no change to its command line, output, or files.
- Contract: none
- User action: none
- Pull request: #1421

### code-review-core's pipeline is cut into modules by concern and its run state is typed

- Level: patch, an internal refactor with no change to any command, status line, or file.
- Contract: none
- User action: none
- Pull request: #1419

### code-review-core reads review records and reviewer results into typed shapes

- Level: patch, an internal refactor with no change to the record or result format.
- Contract: none
- User action: none
- Pull request: #1418

### analyze-skill-cost and audit-ai-config name the guidance each check derives from

- Level: minor, a new command, option, and status lines that add to the skills' contract without changing what was there.
- Contract: `analyze-skill-cost` `provenance`, `PAGE`, `DECISION`, `NOT_A_CHECK`; `audit-ai-config` `--provenance`, and the JSON report's `provenance` key.
- User action: none. Ask either skill why a finding holds to see the page and passage, or the decision, its check rests on.
- Pull request: #1415

### The deployer reads its manifest and journal into typed shapes

- Level: patch, an internal refactor with no change to the manifest or journal format.
- Contract: none
- User action: none
- Pull request: #1413

### review-insights runs its synthesis as the guarded code-review-reviewer, in a run under the temporary directory

- Level: minor, a changed meaning of two status lines and of two format fields, and a runtime support declaration narrowed on Codex and Copilot CLI.
- Contract: `review-insights` `SYNTHESIS_PROMPT`, `review-insights` `SYNTHESIS_RESULT`, `docs/code-review-operations-contract.md` (`insights.synthesis`), and the agent `code-review-reviewer`, which `review-insights` now starts in Claude Code
- User action: none. `report` stages the synthesis's run each time it writes a pending synthesis, so a synthesis left pending by an earlier version is staged again by the next `report`, which step 1 always runs. A `synthesis-prompt.md` an earlier version left beside a report is no longer read and can be deleted. A Claude Code session started before the update may not have the new agent description loaded, and starts the agent all the same.
- Pull request: #1410

### The hub guard's hook runs tools/hub_guard.py

- Level: patch, a contributor-facing tool split by concern with no change to what the guard refuses or prints.
- Contract: `.claude/settings.json` (its `PreToolUse` hook command now runs `python -B "$CLAUDE_PROJECT_DIR/tools/hub_guard.py"`, where it ran `tools/worktrees.py guard`)
- User action: none in a clone that has not opted into the guard. In one that has, nothing is required: a Claude Code session started before the update keeps the old hook command, which `tools/worktrees.py guard` still answers by running the moved guard; a session started after it runs `tools/hub_guard.py`. A script that imported the guard's functions from `tools.worktrees` imports them from `tools.hub_guard`.
- Pull request: #1408

### Reviewer effort reaches Codex reviewers and the Copilot CLI host

- Level: minor, a new status line in two shipped skills and a setting that now applies on two more runtimes.
- Contract: `review-prs` `EFFORT`, `review-document` `EFFORT`
- User action: none. A configured `reviewer_effort`, or a specialist manifest's `effort`, now sets the reasoning effort of each reviewer Codex starts, through `spawn_agent`'s `reasoning_effort`, and of the Copilot CLI host, through `--reasoning-effort`. Clear it if you set it only for the Workflow path in Claude Code.
- Pull request: #1409

### The capability the review skills lack on Codex and Copilot CLI is `fail-closed-agent-hooks`

- Level: minor, a renamed capability in the deployer operation contract, carried in a minor before 1.0.0. A declaration that names the old capability is refused.
- Contract: `fail-closed-agent-hooks`, `deploy-meta/review-document.json` (its `runtime_support`), `deploy-meta/review-insights.json` (its `runtime_support`), `deploy-meta/review-prs.json` (its `runtime_support`), `deploy-meta/update-pr-dashboard.json` (its `runtime_support`)
- User action: none for this repository's skills. In a skill source of your own, rename `agent-hooks` to `fail-closed-agent-hooks` in each `runtime_support` declaration. Drop `agent-delegation` from a Copilot CLI declaration, since Copilot CLI now offers it. `review-insights` stays partial on Copilot CLI, which lacks `fail-closed-agent-hooks` (#1410).
- Pull request: #1409

### `CLAUDE.md` states each rule once and points at the section that holds its enforcement

- Level: patch, a contributor-facing documentation change with no contract change.
- Contract: none
- User action: none. A contributor who looked in `CLAUDE.md` for how validation enforces a rule follows its pointer: the deployer's invariants are in `docs/design.md`, the validation runner under "The runner" in `CONTRIBUTING.md`, documentation-only selection under "Validation" in `docs/implementing-changes.md`, and the skill contract's rules in `docs/adding-a-skill.md` as before.
- Pull request: #1407

### The development and mutation dependencies are hash-locked, dependencies included

- Level: patch, a contributor-facing change to how the validation tools are pinned, with no contract change.
- Contract: none
- User action: none for a user who deploys. A contributor installs as before with `python -m pip install -r requirements-dev.txt`, which now checks every hash and installs only the locked versions. To change a pin, edit `requirements-dev.in` or `requirements-mutation.in`, not the `.txt` lock. Install pip-tools into a virtual environment of its own. Then run `python -m piptools compile --generate-hashes --allow-unsafe --strip-extras --no-emit-index-url requirements-dev.in` from the repository root on Windows, and again for `requirements-mutation.in`, as "Compiling a lock" in `docs/dependency-updates.md` says. Commit the input and the locks together.
- Pull request: #1406

### Skill evaluations run each scenario several times on request and judge whether a skill's description starts it

- Level: patch, a change to a contributor-facing tool and a repository skill.
- Contract: `tools/skill_evals.py` `--runs`, its `RUNS` line and the ` run=<run>` token on its per-run lines, the `trigger` scenario mode with `requests` and `near_misses`, and the `Runs` column of `docs/skill-evaluations.md`'s results table
- User action: none. A hand-kept copy of the results table adds the `Runs` column after `Passed`.
- Pull request: #1401

### Two test suites name their failures

- Level: patch, because only test suites change.
- Contract: none
- User action: none.
- Pull request: #1397

### The deployer's internal names and lock release are consolidated

- Level: patch, a refactor with no contract change; no deployment output, file, or journal entry changes.
- Contract: none
- User action: none
- Pull request: #1395

### The gh skills read Partial on Codex, which gives their commands no network by default

- Level: minor, a new runtime-support capability and declarations that now say what Codex's default sandbox withholds.
- Contract: `dotnet-format` `runtime_support` `codex`, `github-activity-report` `runtime_support` `codex`, `repo-cleanup` `runtime_support` `codex`, `review-document` `runtime_support` `codex`, `review-prs` `runtime_support` `codex`, `update-pr-dashboard` `runtime_support` `codex`, `sandbox-network`
- User action: to run these skills in Codex without approving each `gh` call, or through `codex exec`, add `[sandbox_workspace_write]` with `network_access = true` to `~/.codex/config.toml`, as "Network access" in `docs/codex-support.md` shows.
- Pull request: #1403

### On Windows, Codex's sandbox guidance recommends MXC, and check's codex sandbox line reports the configured mode

- Level: patch, documentation and wording with no change to what a setting does.
- Contract: `deploy.py check` (the `codex sandbox` line)
- User action: none needed. A Windows user may switch to MXC with `[features] prefer_mxc = true`, keeping `[windows] sandbox = "elevated"` as the fallback, and confirm with the `codex sandbox` command in "Windows sandbox mode".
- Pull request: #1403

### deploy.py verify names Codex's reason for an adapter it could not load, and fails a user-only adapter Codex offers the model

- Level: minor, a new verify status and a new reason on an existing one.
- Contract: `deploy.py verify` (`NOT FOUND` with `could not load it: <reason>`, and the new `EXPOSED` status)
- User action: none. An `EXPOSED` adapter means its `agents/openai.yaml` is not keeping it out of the model's catalog; redeploy, and report it if it persists.
- Pull request: #1403

### Runtime adapters carry the skill's argument hint and a user-only skill's real description

- Level: patch. A fix to what the generated adapters hold. The next deployment rewrites them, and the user does nothing.
- Contract: none. The runtime adapter `~/.agents/skills/<name>/SKILL.md` now carries the skill's `argument-hint` when it has one, and a user-only skill's adapter carries the skill's own `description` in place of the generic "Runtime adapter for the authoritative <skill> skill" line, beside `disable-model-invocation: true` and Codex's `agents/openai.yaml` as before.
- User action: none. Copilot CLI and Codex show the new frontmatter after the next deployment.
- Pull request: #1398

### Repository tools report their own failures precisely: the mutation gate's coverage steps, release_notes' defects, and a failed repository scaffold

- Level: patch, defect fixes in skill-core and in contributor-facing tools with no contract change.
- Contract: none
- User action: none. A contributor who records a test's cost in `RECORDED_TEST_SECONDS` records only a test that is not parametrized.
- Pull request: #1396

### review-document reads Partial on Codex, and the review skills' runtime support names the reviewer guard

- Level: patch, a correction of what the runtime-support declarations say, with no change to how a skill runs.
- Contract: `review-document` `runtime_support` `codex`, `review-document` `runtime_support` `copilot-cli`, `review-prs` `runtime_support` `codex`, `review-prs` `runtime_support` `copilot-cli`, `update-pr-dashboard` `runtime_support` `codex`, `update-pr-dashboard` `runtime_support` `copilot-cli`, `agent-hooks`
- User action: none. A Codex user of `review-document` should know its reviewer runs without the reviewer agent's guard, so the reviewer's prompt is its only boundary; run reviews of untrusted content from Claude Code.
- Pull request: #1394

### review-document's build prints the prepare arguments that name every fixture

- Level: minor, a new output line and a new refusal of an output directory whose path a quoted argument does not keep.
- Contract: `review-document` `PREPARE_ARGUMENTS`
- User action: none. An `--output`, or a temporary directory, whose path holds `"`, `$`, a backtick, or a backslash is now refused with a `FAILED` line naming it; name another `--output`.
- Pull request: #1360

### repo-cleanup and curate-agent-memory name every line their commands print

- Level: patch, documentation of lines the scripts already printed.
- Contract: `repo-cleanup` `FETCH_FAILED`, `curate-agent-memory` `INDEX_LINE`, `curate-agent-memory` `WOULD_WRITE`
- User action: none. repo-cleanup's sweep no longer records a `DEFAULT` line, which it never printed. The change-skill repository skill names its lines too.
- Pull request: #1360

### analyze-skill-cost names deterministic prose by cue and compares the status lines a skill names with what its scripts print

- Level: minor, new status lines and cue identifiers in a shipped skill's output.
- Contract: `analyze-skill-cost` `UNPRINTED`, `analyze-skill-cost` `EXIT_MISMATCH`, `analyze-skill-cost` `UNEXPLAINED`, `analyze-skill-cost` `assembled-arguments`, `analyze-skill-cost` `parsed-output`, `analyze-skill-cost` `carried-state`, `analyze-skill-cost` `owed-by-rules`, `analyze-skill-cost` `hand-drafted`
- User action: none. An audit may now report prose a script should replace and lines a skill's document and scripts disagree on; anything reading `scan`'s output by line prefix meets the three new lines after the `RUNTIME_PROMPT` lines.
- Pull request: #1353

### A changed installer or other listed binary over the 16 MiB limit is named as unreviewed instead of making the review INCOMPLETE

- Level: patch, a defect fix completing the previous release's handling of binary types the review does not read, with no contract change.
- Contract: none
- User action: none.
- Pull request: #1352

### A review names the binary file types it does not read, and any other changed binary makes it INCOMPLETE

- Level: minor. A changed binary of a type the review does not read is recorded under `review.coverage.unreviewed_files` and named in the report, and does not make the verdict `INCOMPLETE`. A changed binary of any other type now does. A repository's entry may set `unreviewed_suffixes` to add its own types. Records are written at `schema_version` 2, and version 1 records are still read without a migration. A previous release cannot read a version 2 record, which would be major from `1.0.0` on and is carried in a minor before it.
- Contract: `docs/code-review-operations-contract.md` (`config.repositories.<repository>.unreviewed_suffixes`, `request.coverage.unreviewed_files`, `record.review.coverage.unreviewed_files`, `record.schema_version` `2`)
- User action: none. If a repository's pull requests change binaries of a type the shipped list does not name and those reviews now end `INCOMPLETE`, add the suffixes to that repository's entry with `review_config.py write`, as in `"unreviewed_suffixes": [".bin"]`.
- Pull request: #1347

### update-coding-agent-skills follows the latest release by default, and main only when you opt in

- Level: minor, a changed default of the update skill and a new configured variable, which would be major after 1.0.0.
- Contract: `deploy-meta/update-coding-agent-skills.json` (its `required_vars`, which now carry `UPDATE_CHANNEL`); the configured variable `UPDATE_CHANNEL` (`release` or `main`, default `release`); `update-coding-agent-skills` `--channel` (new argument); `update-coding-agent-skills` `UP_TO_DATE` and `UPDATED` (on the release channel they name the release tag, and `UPDATED` lists the commits the release adds to the deployed commit); `update-coding-agent-skills` `NOT_IN_RELEASE` (new detail line); `update-coding-agent-skills` `NO_RELEASE` (new status line); `update-coding-agent-skills` `PINNED` (now only for a pin the user made, and on the release channel only for one at another commit than the release, after the fetch); `update-coding-agent-skills` `MAJOR_UPDATE` (on the release channel it compares the release tag the update would deploy and lists the commits up to it).
- User action: none to keep the new default. Your next update deploys the latest release and pins the installation there with `--at refs/tags/<tag>`. If the installation had followed `main` past that release, it steps back, and the update lists the commits it leaves after `NOT_IN_RELEASE`. To keep following `main`, run `python deploy.py configure` and answer `main` at the `UPDATE_CHANNEL` prompt, or start the skill with `--channel main` for one run. Until a release carries this change, the release it deploys holds the older copy of the skill. That copy stops at the pin with `PINNED refs/tags/<tag>`, and rerunning it with `--unpin` returns you to `main`. After the skill deploys a release, `python deploy.py --all` from the clone needs `--unpin`, as it does over any pin.
- Pull request: #1343

### review-prs can review a repository's pull requests in batches of a configured size

- Level: minor. Additive: a repository's entry may set `batch_size` to review that many pull requests per run, the oldest first, and `enumerate --batch-size N` overrides it for one run. `enumerate`'s `LISTED` line gains `deferred=<n>`, the eligible pull requests left for the next run, which the watermark never passes. The batch file's version is now 2. A previous release refuses a configuration that has `batch_size`.
- Contract: `docs/code-review-operations-contract.md` (`config.repositories.<repository>.batch_size`), `review-prs` `LISTED` (`deferred=<n>`), `review-prs` `enumerate --batch-size`
- User action: none. To review a large repository in chunks, add `"batch_size": 25` (or another size) to its entry with `review_config.py write`, and run `review-prs` again for each later chunk.
- Pull request: #1341

### A pull request runs a mutation gate beside Validation

- Level: patch, since it changes CI and contributor guidance, not anything a deployment installs.
- Contract: none
- User action: none. A contributor now sees a `Mutation gate` check on each pull request. Its step summary gives each changed module's kill ratio and surviving mutants, and is the record. The template's Validation section asks only for the judgment it dictates: kill each survivor on a changed line where the mutation alters behavior a caller relies on, or name it as equivalent. Nothing from the summary is copied into the pull request.
- Pull request: #1339

### deploy.py names the path and reason of each failure the recovery and installation guides describe

- Level: patch, defect fixes that make the deployer's messages match its documents.
- Contract: `ERROR: <tool> did not finish within <seconds> seconds` (now printed by `check` for the Codex sandbox probe, in place of `codex debug prompt-input named no sandbox mode`), `ERROR: Recovery of run <run-id> failed at <path>: <reason>` (in place of `WARNING: Failed to restore backup for <item>`), `ERROR: Could not finish the deployment: <path>: <reason>` (in place of `ERROR: Unexpected OSError: ...`), `Cancelled after the deployment began; its changes are reconciled before exit.` and `Deployment cancelled; reconciling the current journal before exit...` (in place of `ERROR: Unexpected KeyboardInterrupt: `), `ERROR: Could not finish recovery: <path>: <reason>` (in place of `ERROR: Unexpected OSError: ...` when recovery stops)
- User action: none. A script that matched one of the old lines should match the new one.
- Pull request: #1340

### A pull request declares a revert of a file to an older state of main

- Level: patch, since it changes contributor validation and no deployed skill, deployer command, or record.
- Contract: the pull request body's revert declaration, ``Reverts `<path>` to its state before <commit>``, on a line of its own outside an HTML comment, as "Pull requests" in `docs/implementing-changes.md` and the template state.
- User action: none for a user who deploys; a contributor whose pull request reverts a file on purpose adds the line to its body.
- Pull request: #1337

### review-prs and code-review-core declare Copilot CLI optional

- Level: patch, a metadata correction with no contract change.
- Contract: none
- User action: none. The two skills' metadata now matches what `python deploy.py check` reported all along: Copilot CLI is optional, and the review skills run fully without it.
- Pull request: #1335

### code-review-core no longer ships its test archive builder

- Level: minor, a file removed from a deployed hidden skill; nothing a user runs changes.
- Contract: none
- User action: none; the next deployment reports `ejryan89-code-review-core` under `RENAMED` (from `code-review-core`, as the rename entry for #1439 says) and installs it without `scripts/review_fixture.py`.
- Pull request: #1329

### The regression suites run through pytest, and skill-core no longer ships its test base

- Level: minor, a new development prerequisite for contributors and a file removed from a deployed hidden skill; nothing a user runs changes.
- Contract: none
- User action: none for a user who deploys; the next deployment reports `ejryan89-skill-core` under `RENAMED` (from `skill-core`, as the rename entry for #1439 says) and installs it without `scripts/git_test_base.py`. A contributor runs `python -m pip install -r requirements-dev.txt` to install pytest, runs a suite with `python -B -m pytest <suite>` from the repository root instead of `python <suite>`, and isolates a suite's git by nothing at all: the root `conftest.py` does it, and a test class or suite module adds settings as `git_config`.
- Pull request: #1327

### What changed, as a user meets it

- Level: patch; a record entry in a repository skill's reference file, nothing deployed.
- Contract: none
- User action: none
- Pull request: #1325

### Validation requires ruff 0.17.1

- Level: patch, a development-dependency move with no contract change.
- Contract: none
- User action: contributors reinstall the development dependencies with `python -m pip install -r requirements-dev.txt`; validation stops on ruff 0.17.0 and names the floor. Users who only deploy need nothing.
- Pull request: #1326

### Deployed skills no longer carry their regression suites

- Level: minor, a change to what a deployment holds; it asks nothing of the user.
- Contract: none
- User action: none. The next deployment reports each skill that had a suite under `UPDATED` and removes the suite files from `~/.claude/skills/<skill>/scripts/`. A skill you edited locally is preserved as before.
- Pull request: #1315

### Validation requires ruff 0.17.0

- Level: patch, a development-dependency move with no contract change.
- Contract: none
- User action: contributors reinstall the development dependencies with `python -m pip install -r requirements-dev.txt`; validation stops on ruff 0.16.10 and names the floor. Users who only deploy need nothing.
- Pull request: #1314

## v0.9.0

Level: minor.

### The hub guard reads an alias wherever its call runs, and its PowerShell reader matches the Bash one on quotes and `$env:` assignments

- Level: patch, a defect fix in a contributor-facing tool with no contract change.
- Contract: none
- User action: none, in a clone that has not opted into the guard. In one that has, these are now refused where they were allowed:
- from a worktree, an alias the repository defines that writes the hub, such as `git hop` for `!git -C "<hub>" commit`;
  - from a worktree, an alias that cannot be read;
- through the PowerShell tool, a refused subcommand quoted in part, such as `git com'mit'`;
- through the PowerShell tool, a command that sets a variable and then moves into the hub through it, such as `$env:TEMP = '<hub>'; Set-Location $env:TEMP; git commit`;
- through the PowerShell tool, a `pwsh -c` or `Invoke-Expression` string holding an environment variable, in a command that names git.

Through the PowerShell tool, a plain assignment of a literal location variable, such as `$env:GIT_WORK_TREE = '<this worktree>'; git commit` from that worktree, is now allowed where it was refused as unplaced. An alias that cannot be read is now allowed with a note in a repository with no guarded hub in view.
- Pull request: #1310

### An upgrade note names a shipped skill's changed argument, status line, or exit code under Contract

- Level: patch, a documentation change to the upgrade-note rule with no contract change.
- Contract: none
- User action: none. A contributor who changes a shipped skill's argument, status line, or exit code names the skill and each item in backticks under `Contract:`, as the `Contract:` field in `docs/upgrade-notes.md` says. A change to a repository skill's lines says `Contract: none`.
- Pull request: #1309

### review-document's fixes from #1220 are restored after a later merge reverted them

- Level: patch, defect fixes restored with no contract change.
- Contract: none
- User action: none. This change restores what #1220's entry in this release describes, and it changes no contract beyond that entry.
- Pull request: #1308

### dotnet-format requires the .NET SDK and leaves the deprecated global tool optional

- Level: minor, a changed tool requirement in the deployer operation contract: the skill's required tool is now the .NET SDK.
- Contract: `deploy-meta/dotnet-format.json` (its `tools` and `optional_tools`)
- User action: none if the .NET SDK 9.0.200 or newer is installed. With only the `dotnet-format` global tool, `python deploy.py check` now reports `dotnet (used by dotnet-format)` under `MISSING` and the global tool under `OPTIONAL`, and the skill stops on any `.slnx` or SDK-style `.sln`, as it already did; install the .NET SDK to format those. The global tool is still used, and still needed, only for a `.sln` holding a project older than SDK-style.
- Pull request: #1306

### The deployer refuses every Windows device name, COM0 and LPT0 included

- Level: patch, a defect fix: the deployer and the review snapshot now read one list.
- Contract: none
- User action: none, unless a source of yours names a skill, bundle, or item `com0`, `lpt0`, or a device name followed by spaces before an extension, such as `con .md`; rename it, since Windows opens each as a device.
- Pull request: #1306

### The fsops policy reads writes through `sys.modules` and a module's `__dict__`, and `--changed` reaches the deployer suites for a skill's metadata

- Level: patch, stricter validation of this repository's own code and a wider local selection, with no contract change.
- Contract: none
- User action: none. A contributor whose deployer code calls through `sys.modules[...]`, `module.__dict__[...]`, or a literal lookup of a module loader now sees the write counted. A call through any other subscript, or through a module named by anything but a literal, is now reported as unresolved. `python -B tests/run_validation.py --changed` now runs `tests/deployer/test_rendering_locking.py` and `tests/deployer/test_bundles_dependencies.py` for a change under `deploy-meta/`.
- Pull request: #1311

### The merge shepherd names a pull request in conflict with main and flattens GitHub text onto its lines

- Level: patch, a defect fix in a repository skill with one new status line; nothing ships.
- Contract: none
- User action: none. A shepherd session running `shepherd-pull-requests` now meets `CONFLICT #<number> <branch> head <commit> merge origin/main and resolve` for a pull request whose merge state is `DIRTY`, and the tick exits 1 while one is open; message the session that owns the branch once per head, as `SKILL.md` section 2 says. Branch, path, state, and step names in its lines are flattened onto one line.
- Pull request: #1307

### change-skill's gates owe the runtime canary for a flip of a skill's selectable flag

- Level: patch, a defect fix in a repository skill's gate.
- Contract: none
- User action: none. A contributor whose change flips `selectable` in a `deploy-meta` file now sees `GATE runtime-canary <skill>` and runs the `runtime-canary` repository skill before the pull request.
- Pull request: #1305

### audit-repository's remove-briefs deletes only a briefs directory holding its run's marker

- Level: patch, a defect fix in a repository skill.
- Contract: none
- User action: delete by hand any briefs directory left from a run before this change. `remove-briefs` now refuses it because it holds no `.audit-repository-briefs.json`. A run now writes that marker before its first brief.
- Pull request: #1305

### Claude Code needs 2.1.295 or newer for the code-review reviewer's guard to block when it cannot run

- Level: minor. A raised tool floor, which would be major after 1.0.0, and a change at the reviewer trust boundary: the `code-review-reviewer` agent's guard hook declares `onFailure: "block"`, so in Claude Code a reviewer's tool call is denied, no longer allowed, when the guard cannot start, times out, or exits with an unexpected status. Claude Code has its first floor, 2.1.295, the first release that honors the setting, and `python deploy.py check` reports an older one as `OUTDATED` (`needs 2.1.295 or newer`); Claude Code stays optional, so deploying is not blocked.
- Contract: `deployer/tools.py`
- User action: update Claude Code to 2.1.295 or newer (on Windows, 2.1.299 or newer also blocks a reviewer call when Git for Windows is not installed), then redeploy so the updated agent reaches `~/.claude/agents`.
- Pull request: #1276

### The runtime canary's `--changed-since` default and the release audit's default base are the last release, not the nearest tag

- Level: patch, a defect fix in two repository tools with no contract change.
- Contract: none
- User action: none for a release run. A contributor who relied on `python -B tools/runtime_canary.py --changed-since` or `audit_repository.py release` starting at a pre-release or other non-release tag passes that tag explicitly (`--changed-since <tag>`, `--since <tag>`); with no `vX.Y.Z` tag reaching `HEAD` both now fail and ask for one.
- Pull request: #1270

### The worktree survey writes no checkout's index and marks one git cannot read

- Level: patch, defect fixes in the repository's own tool with no contract change.
- Contract: none
- User action: none. `python tools/worktrees.py list` now reads each worktree without refreshing its index, and lists a checkout git cannot read, such as one whose `.git` file names a folder that is gone, as `unreadable` with dirty `-` instead of as a clean one.
- Pull request: #1271

### The filesystem-write and platform policies read more forms

- Level: patch, stricter validation of this repository's own code with no contract change.
- Contract: none
- User action: none for a user. A contributor's deployer change now fails validation when a sanctioned `tempfile` call names a `prefix` or `suffix` that is not a literal free of path separators; when it writes through `getattr(module, "name")`, `importlib.import_module("name")`, `operator.methodcaller` or `attrgetter`, a module passed as a value, `logging.config.dictConfig` or `fileConfig`, `zipapp.create_archive`, `venv.create` or `EnvBuilder`, or `Path.copy` or `Path.move`; or when it calls a callable another call returns, as in `factory()(path)`. Code under `deployer/`, `tools/`, `deploy.py`, or the validation runner now fails when it names a POSIX-only signal, signal function, process-group or child-wait call, or open flag, `os.add_dll_directory`, `sys.winver`, `sys.dllhandle`, the `winsound`, `_overlapped`, or `syslog` module, the `tasklist` command, a further Windows environment name such as `WINDIR` or `ProgramFiles(x86)`, or `pathlib.PureWindowsPath`, `WindowsPath`, or `PosixPath` outside `deployer/platform_support.py`, unless the module states why in `PLATFORM_ALLOWED`.
- Pull request: #1272

### The pool and command policies read more ways to change shared state or start a process

- Level: patch, a contributor-facing validation change with no contract change.
- Contract: none
- User action: none for a user who deploys. Contributors: a policy check now fails when it changes `os.environ` through an aliased `|=`, calls `os.dup2`, changes `os.environb`, reaches `os.environ` through `getattr` or `sys.modules`, or uses a lookup by a computed name that may reach a stream, the environment, or the working directory. A skill script now fails when it hands on a runner held in a dict or tuple indexed where it is written, or takes a runner from a module looked up by a computed name, and has no `COMMANDS_ALLOWED` reason for it. The policy also reads `os.startfile`, `pty.spawn`, an event loop's `subprocess_exec` and `subprocess_shell`, `asyncio.subprocess`'s runners, and a runner reached through `importlib.import_module`, `__import__`, or `sys.modules` as starting a process, and checks the program each one starts.
- Pull request: #1269

### The shepherd tick names a merged worktree pushed under another branch name, with every branch to delete

- Level: patch, a defect fix in a repository skill for working on this repository; nothing deployed changes.
- Contract: none
- User action: none. A shepherd session meets the `MERGED` line in a new shape, `MERGED #<number> worktree "<path>" branch <branch> [branch <branch>]`: one `branch <branch>` for each local branch to delete, the worktree's checked-out branch first and then the pull request's head branch when a local branch of that name exists; a detached worktree with no such branch has none. It now also names a worktree matched by commit, whose branch is not the pull request's.
- Pull request: #1268

### A GitHub login is read by GitHub's rule in github-activity-report and the code-review configuration

- Level: minor, input validation brought to GitHub's own rule, refusing a configuration key and argument values an earlier release accepted. That would be major after 1.0.0.
- Contract: the code-review configuration's `dashboard.author_names` keys (GitHub's login rule in `skills/skill-core/scripts/github_logins.py` replaces the pattern `[A-Za-z0-9][A-Za-z0-9-]{0,38}`), and the `github-activity-report` arguments `--org` and `--user`
- User action: none, unless your code-review configuration's `dashboard.author_names` names a login with two hyphens in a row or a trailing hyphen, which GitHub no longer issues: the configuration is then refused until you correct the key. `github-activity-report` refuses `--org` or `--user` values longer than thirty-nine characters, or with a misplaced hyphen or underscore, which name no GitHub account. Both now accept an Enterprise Managed Users login such as `mona_acme`.
- Pull request: #1261

### Fact lines replace every control character with a space

- Level: patch, a safety fix to how every skill prints text on one line.
- Contract: none
- User action: none. A control character in text a skill prints on one line, such as a terminal escape in a review finding's title or a Git message, is now a space in every skill, as it already was in `repo-cleanup`. In `repo-cleanup`, a branch or worktree whose name holds a C1 control character is now reported `PRESERVED` with the reason instead of offered for removal.
- Pull request: #1261

### The duplication policy reads repository skills and the validation policies

- Level: patch, a contributor-facing validation change with no contract change.
- Contract: none
- User action: none for a user who deploys. A contributor who copies a definition or a long constant between files under `.claude/skills/` or `tests/validation/`, or between one of them and `skills/`, `deployer/`, or `tools/`, now fails validation. Import one copy (a policy helper from `tests/validation/validation_support.py`), or sanction it with `DUPLICATION_ALLOWED` as "Validation" in `docs/adding-a-skill.md` describes.
- Pull request: #1263

### The code-review analyzer inventory counts a project that imports its SDK, and reads a project file with a byte that is not UTF-8

- Level: patch, a defect fix with no contract change. The inventory reviewers check `available` coverage against now lists the SDK's analyzers for a project whose SDK comes through an `Import`. It also reads a project file as `dotnet-dependencies` and `dotnet-format` do, instead of listing one with an invalid byte as unread.
- Contract: none
- User action: none
- Pull request: #1262

### update-coding-agent-skills no longer stops at a pre-release tag

- Level: minor. It changes when the skill's `MAJOR_UPDATE` status line fires: a tag with a suffix such as `-rc.1` is no longer a release, so only a final `vMAJOR.MINOR.PATCH` release that raises the breaking component stops the update.
- Contract: none
- User action: none. The repository publishes no suffixed tags.
- Pull request: #1259

### The shepherd tick names a pull request out of step with its branch, judges the newest run on a head, and updates a behind branch GitHub has not computed

- Level: patch, defect fixes in a repository skill for working on this repository; nothing deployed changes.
- Contract: none
- User action: none. A shepherd session meets a new line, `OUT_OF_STEP #<number> <branch> head <commit> branch <commit>`, which asks it to message the owner of `<branch>` to merge `origin/main` and push, a new `JUDGED ... superseded by run <id>` form, which needs nothing, and a new last line, `FAILED cannot read origin's branches: <reason>`.
- Pull request: #1255

### The deployer holds the Windows device names once

- Level: patch, an internal consolidation with no behavior change.
- Contract: none
- User action: none.
- Pull request: #1256

### update-coding-agent-skills ends with DEPLOY_PINNED when the deployer refuses a pin check did not name

- Level: minor. A new status line, `DEPLOY_PINNED <ref> <commit>`, for `update-coding-agent-skills`.
- Contract: the `update-coding-agent-skills` status lines
- User action: none. When `deploy.py check` does not name a pin but the deployment was made with `deploy.py --at`, the update now ends with `DEPLOY_PINNED <ref> <commit>` instead of `DEPLOY_FAILED 1`. Unlike `PINNED`, which stops before anything is fetched, the clone was fetched and fast-forwarded to `origin/main` (and switched to `main` if it was on another branch); nothing was deployed, and the installation stays at the pinned commit until you start the skill again with `--unpin`.
- Pull request: #1251

### review-prs and review-document carry one run list from `prepare` to every later pipeline command

- Level: minor, new pipeline arguments and status lines; every existing form is still accepted.
- Contract: `prepare --batch`, `prepare --runs`, `prepare --canary owner/repo#N`, a repeatable `prepare --fixture`, `prepare` taking more than four pull requests, `--runs` on `workflow`, `wait-reviewers`, `check`, `finalize`, and `unfinalized`, and the status lines `RUNS <run list> pending=<n>`, `UNPREPARED <selector>`, and `WORKFLOW none`.
- User action: none. A script of your own that reads `prepare`'s output sees a `RUNS` line last.
- Pull request: #1252

### repo-cleanup ends its run with a `report` command

- Level: minor, a new command and status lines in a shipped skill.
- Contract: `skills/repo-cleanup/SKILL.md` (the new `report` command, its `--record` and `--aborted` arguments, and its `REPO <path> <state>` lines with the new `aborted` state, `QUIET <path>...`, `PROCESSED <text>`, and `FAILED <reason>` lines; the sweep's new `--record` argument, which step 3's re-sweep passes; and the `sweep.record` file a sweep writes in its plans directory)
- User action: none.
- Pull request: #1248

### flag-review-finding add takes a finding by its report label

- Level: minor, an additive argument form: `--finding` accepts the report label `v2 F003`, which it refused before, and reads the review version from it. `--review-version 2 --finding F003` still works.
- Contract: the `flag-review-finding` `add` argument `--finding`, and its new refusal `FAILED Report label '<label>' names review version <n>, not <m>` for a `--review-version` that disagrees with the label beside it.
- User action: none.
- Pull request: #1249

### review-insights prints the report and decision arguments it used to leave to the session

- Level: minor, new status lines on existing commands with no existing line or argument changed.
- Contract: the `review-insights` status lines `SCOPE_ARGUMENTS` (printed by `scope`), `DECIDE` (printed by `report` and `synthesize` after each recommendation), and `DECIDE_CUSTOM` (printed by `report` after the custom candidates)
- User action: none. Redeploy to get the skill that passes them; a script that reads these commands' output by line kind sees the new lines.
- Pull request: #1250

### Validation reads the checkout it runs in whatever repository GIT_DIR names

- Level: patch, a defect fix in the validation runner with no contract change.
- Contract: none
- User action: none. A contributor who runs `python -B tests/run_validation.py` from a git hook or a shell that exports `GIT_DIR`, `GIT_WORK_TREE`, or `GIT_INDEX_FILE` now gets the checkout's file list, changed files, and branch, not another repository's.
- Pull request: #1247

### update-pr-dashboard builds the review-prs arguments with a new `review-arguments` command

- Level: patch, a new command the skill runs internally; what the user is asked and what `review-prs` receives are unchanged.
- Contract: none
- User action: none. Redeploy to pick up the skill. `dashboard_pipeline.py review-arguments --confirm owner/repo#N ... [--scope auto|full|incremental]` prints `SCOPE_NEEDED owner/repo#N` or one `REVIEW_ARGUMENTS <arguments>` line, and step 3 of the skill runs it in place of building the arguments itself.
- Pull request: #1246

### A shepherd-pull-requests repository skill runs the merge shepherd's tick

- Level: minor, a new repository skill for working on this repository; nothing deployed changes.
- Contract: none
- User action: none. A session that shepherds pull requests in the hub can run `/shepherd-pull-requests` on its repeating prompt in place of a prose check-in.
- Pull request: #1244

### implement-change opens a pull request through its own script

- Level: patch, a repository skill's procedure moved into a tested script. Nothing a deployment installs changes.
- Contract: none
- User action: none. A contributor following `implement-change` runs `.claude/skills/implement-change/scripts/pull_request.py` with `draft`, `check`, and `open` in step 8, in place of writing the body by hand and pasting the title into `gh pr create`.
- Pull request: #1240

### audit-repository looks up milestones and carried issues, splits large areas, and adds duplicate search and briefs removal

- Level: minor, a repository skill's new commands and status lines.
- Contract: none
- User action: none for users of the deployed skills. A contributor running the audits needs `gh` signed in, records the rotation area from a worktree (the main checkout is refused), runs `duplicates` before filing and `remove-briefs` after, and reads `BRIEF <area>-<part>` names when an area holds more than 60 files.
- Pull request: #1241

### change-skill reads contract sections and names the gates a change owes by script

- Level: patch, a repository skill's new commands for contributors; no shipped skill, deployer, or contract file changes.
- Contract: none
- User action: none. A contributor following `change-skill` now runs `python -B .claude/skills/change-skill/scripts/change_skill.py section "<title>"` to read a cited section, and `python -B .claude/skills/change-skill/scripts/change_skill.py gates` to list the `REGENERATE` and `GATE` lines the change owes.
- Pull request: #1242

### `tools/skill_evals.py` prints each failure's kind and the pull request's citation block

- Level: patch, a repository tool's output for contributors; no shipped skill or contract changes.
- Contract: none
- User action: none. A contributor running `evaluate-skill` now pastes the block between `CITATION BEGIN` and `CITATION END` into the pull request's Validation section. A `FAIL` line has its kind (`judgment`, `run`, or `record`) before its quoted reason, and each run that counts for nothing gets a `NOT_COUNTED` line.
- Pull request: #1245

### The runtime canary names the skills a change reaches, prints its record, and removes a home that passed

- Level: patch, a repository tool for contributors; no shipped skill or deployment contract changes.
- Contract: none
- User action: none for users. A contributor running the `runtime-canary` repository skill names the skills with `python -B tools/runtime_canary.py --changed-since origin/main` (or `--changed-since` alone for a release, from the latest tag), pastes the `RECORD_BEGIN` to `RECORD_END` block into the pull request, and passes `--keep-home` to keep the transcripts of a run that passed, which the canary otherwise deletes.
- Pull request: #1243

### `analyze-skill-cost` prints its own stop line and scans a skill folder

- Level: minor. The skill has a new status line and a new argument form.
- Contract: `skills/analyze-skill-cost/SKILL.md` (the new `STOP <message>` status line `skill_inventory.py locate` prints after `NOT_FOUND` and its `AVAILABLE` lines or after `AMBIGUOUS` lines, and alone for an empty name, where it printed `FAILED invalid skill name ''`; `locate`'s name argument, now optional; and `skill_inventory.py scan`'s skill-folder argument, which it takes beside files)
- User action: none. Deploy to get the new `SKILL.md` and script together.
- Pull request: #1239

### An entrypoint reviewer's finding on a file the pull request does not change is refused and retried

- Level: patch, a defect fix that holds the entrypoint result to the rule its prompt already states.
- Contract: none
- User action: none. A repository's entrypoint reviewer whose result names a file outside the pull request's diff gets a `RETRY` line from `check` naming the finding, as a specialist's off-diff finding does, and `finalize` fails on such a result rather than recording it.
- Pull request: #1232

### A missing `pdftotext` is reported with the versions to install

- Level: patch, a clearer message with no contract change.
- Contract: none
- User action: none. When no `pdftotext` is installed, `python deploy.py check`'s `pdftotext` line and a PDF review's refusal end with `install Xpdf 4.06 or newer or Poppler 26.10.0 or newer`, and on Linux the Xpdf hint after it, as the refusal of an unsupported `pdftotext` already did.
- Pull request: #1232

### The review-insights and repo-cleanup suites hold the behaviors earlier audits found untested

- Level: patch, test coverage with no change to the skills' behavior.
- Contract: none
- User action: none
- Pull request: #1236

### update-pr-dashboard documents that a pinned pull request is never offered for review

- Level: patch, a documentation correction; the skill already behaved this way.
- Contract: none
- User action: none. A pull request you pin with a status override keeps showing its AI review state, but is never offered for a review; clear the override or run `review-prs` on it to have it reviewed.
- Pull request: #1236

### The dotnet bundle has its own page

- Level: patch, documentation only.
- Contract: none
- User action: none. `docs/dotnet.md` describes what the `dotnet` bundle's skills share and how to select it.
- Pull request: #1230

### Deployer tests hold the recovery and migration promises and pin usage errors at exit 2

- Level: patch, test coverage and a documentation clarification; no behavior change.
- Contract: none
- User action: none
- Pull request: #1231

### Validation selects what a change reaches with `--changed`, refuses an empty `-k`, and fails on skipped tests and TODO comments

- Level: patch, contributor tooling with no contract change.
- Contract: none
- User action: none for a user who deploys. A contributor runs `python -B tests/run_validation.py --changed` for the local checks instead of a `-k` list; a reach no file name or import shows goes in `CHANGE_REACHES` in `tests/validation/job_selection.py`. A `-k` that matches no policy check and no suite exits 2. A unittest or pytest skip or expected-failure marker, other than a skip on the platform alone, and a `TODO`, `FIXME`, or `XXX` comment fail validation; a skip that must stay is sanctioned beside the code in a module-level `SKIP_ALLOWED`.
- Pull request: #1235

### Tagging checks the release's upgrade-notes section with `release_notes.py --check`

- Level: patch, a change to the maintainer's release procedure and repository tools with no contract change.
- Contract: none
- User action: none for a user who deploys. A maintainer cutting a release runs `git fetch origin main --tags` and `python tools/release_notes.py vX.Y.Z --check` before tagging and tags once it prints `CURRENT`. When it prints `BEHIND`, carry the entries it lists into the section through a pull request. `tools/deployable_report.py` has no `owned` command, and `leftovers` takes no `--owned`.
- Pull request: #1233

### github-activity-report documents that a refused argument exits 2

- Level: patch, a documentation correction of a stated exit code; the script already behaved this way.
- Contract: none
- User action: none. The skill's Failures section now says an argument the script refuses, such as an organization's display name in place of its login, exits 2 with the reason on stderr and nothing on stdout, rather than the `FAILED` line and exit 1 it promised.
- Pull request: #1229

### Repository tools refuse what they could not handle, and exit 2 for an input they cannot read

- Level: patch, defect fixes in repository tools; no shipped skill or contract changes.
- Contract: none
- User action: none for users of the shipped skills. Contributors meet these changes. `tools/new_skill.py` refuses a description over 1,024 characters and a name a repository skill already has, and checks `docs/skills.md` before it writes anything. It also prints a `REMAINING` line for each unused grant and each unrun `--tool`. `tools/worktrees.py new` takes only `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `ci`, `build`, or `inv` as the kind. `tools/release_notes.py --write`, `tools/deployable_report.py`, and `tools/runtime_prompts.py` exit 2 with an `ERROR:` or `FAILED` line naming the file they cannot read or write, where they ended with exit 1 before. `python tools/skill_shims.py` reports any file in `.agents/skills` other than a shim's own files, and a user-only repository skill's shim gains `agents/openai.yaml`.
- Pull request: #1225

### `deploy.py --uninstall` removes what this source deployed without a prompt

- Level: minor, a new deployer flag.
- Contract: `deploy.py --uninstall`
- User action: none. A script that uninstalls by piping `none` into the selection prompt, such as `'none' | python deploy.py` or `printf 'none\n' | python3 deploy.py`, keeps working. Replace it with `python deploy.py --uninstall`, and `python deploy.py --uninstall --dry-run` for the preview, so that it no longer depends on the prompt's wording or on the menu being shown.
- Pull request: #1223

### `deploy.py check` reports the Codex sandbox setting and the Python app execution aliases on Windows

- Level: minor, new output lines.
- Contract: `deploy.py check` (the `python command` and `codex sandbox` lines)
- User action: none. On Windows, a `python command` or `codex sandbox` line reported `UNUSABLE` names a machine setting Codex CLI needs before it can run a skill's scripts, with the section of `docs/codex-support.md` that says how to put it in place.
- Pull request: #1223

### A token in a fence of another name for Bash or Python is escaped, and one in a language the deployer cannot escape for is refused

- Level: minor, refusing input an earlier release deployed. That would be major after 1.0.0.
- Contract: `deploy.py` (the template sources a deployment renders: a template token in a fence of a language the deployer cannot escape for is refused, and one in a fence of another name for Bash or Python is held to that language's rule)
- User action: none, unless a source you deploy writes a template token in a `fish`, `cmd`, `bat`, `batch`, `javascript`, `js`, `node`, `mjs`, `cjs`, `typescript`, or `ts` fence. That deployment now stops before changing anything and names the file. Move the example into a Bash, PowerShell, or Python fence. A token in a `py`, `python3`, `console`, `zsh`, `ksh`, or `dash` fence is now refused when its value holds a character that is unsafe in that language, as in a `python` or `bash` fence.
- Pull request: #1223

### A redeploy leaves an unchanged item in place

- Level: patch, a behavior fix with no contract change.
- Contract: none
- User action: none. An item reported `UNCHANGED` is no longer moved aside and reinstalled, so the bytecode caches its scripts made stay. A directory a terminal or an editor holds open no longer fails a run that had nothing to change there.
- Pull request: #1223

### update-pr-tracker is renamed update-pr-dashboard

- Level: minor. A skill rename and a configuration key rename, carried in a minor before 1.0.0.
- Contract: `skills/update-pr-tracker`, `skills/update-pr-dashboard`, `deploy-meta/update-pr-tracker.json`, `deploy-meta/update-pr-dashboard.json`, `operation_repository_sets`
- User action: run `python deploy.py --all` from the hub. It removes `update-pr-tracker` and installs `update-pr-dashboard`. If you had modified `update-pr-tracker`, the update preserves it beside the new skill: carry your changes over, delete its folder under `~/.claude/skills`, and the next deployment drops it and its adapter. Rename an `update-pr-tracker` key under `operation_repository_sets` in the code-review configuration to `update-pr-dashboard`; until then every code-review operation refuses the configuration, naming the key. Start the skill as `/update-pr-dashboard`, and update any permission rule you wrote for the old script path, `update-pr-tracker/scripts/tracker_pipeline.py`, which is now `update-pr-dashboard/scripts/dashboard_pipeline.py`.
- Pull request: #1227

### The dashboard names reviews that went stale since the last update, and refuses an unmatched removal

- Level: minor. A new output line and a new refusal.
- Contract: `docs/code-review-operations-contract.md` (under Formats, the Tracker record (`tracker`) rows `schema_version`, now `1` or `2`, and the new `reviews`, and the new Tracker review (`tracker.reviews.<pull>`) table)
- User action: none. The first update after updating reads the version 1 record as holding no reviews, so it names none, and it writes a version 2 record. A `--remove` naming no pull request the dashboard holds now fails, naming it, where it used to be ignored.
- Pull request: #1227

### A configuration not named config.json keeps its tracker record under its own name

- Level: patch. A defect fix: two configurations in one folder shared one record.
- Contract: `docs/code-review-operations-contract.md` (under Formats, the Tracker record (`tracker`) location: `<name>.tracker.json` beside a configuration named `<name>.json`, and `tracker.json` only beside `config.json`)
- User action: none, unless your code-review configuration file is not named `config.json`. Then run one update before your next `override --set`, which is refused until the record `<name>.tracker.json` exists. The old `tracker.json` beside it is no longer read and can be deleted.
- Pull request: #1227

### Linked dashboards and configurations stay linked and keep their mode

- Level: patch. A defect fix.
- Contract: none
- User action: none. A dashboard or configuration reached through a symbolic link is now updated where the link leads, and the replaced file keeps its permissions instead of becoming `0600`. A dashboard or configuration with a second hard link is refused, naming it.
- Pull request: #1227

### review-insights ranks the signals it keeps and prints one GUIDANCE_UNREADABLE line per file

- Level: minor, a new `fallback=partial` value on a printed line and a stated order for the report's signals. The rest is defect fixes.
- Contract: `review-insights` `GUIDANCE_UNREADABLE` (the new `fallback=partial` value), and `docs/code-review-operations-contract.md` (under Formats, the Signals (`insights.signals`) rows `severity_drift`, `verdict_flips`, `reviewer_disagreements`, and `guidance_overlaps`, which now state the order the strongest are kept in, and the Reviewer disagreement row `reports`, which now states which findings are kept and in what order)
- User action: none. A script that reads `GUIDANCE_UNREADABLE` lines should accept `fallback=partial`: the working-tree copy could not be read, and only some of the versions reviewers were given were found at a pinning commit. Regenerate a report to have its verdict flips and reviewer disagreements ranked; a report whose signals were edited by hand into another shape is refused on load until it is regenerated.
- Pull request: #1226

### The repository searches in audit-ai-config, dotnet-format, and dotnet-dependencies skip one stated set of folders and every nested checkout, and never loop through a junction

- Level: patch, defect fixes with no contract change.
- Contract: none
- User action: none. `dotnet-format` no longer fails on a junction or on a malformed solution under `packages/`, `.vs/`, or a nested clone or worktree, and it no longer chooses a solution from those folders. A folder it cannot list now ends the run as `FAILED <folder>: the folder cannot be listed: <reason>`. `audit-ai-config` no longer reports `AGENTS.md`, `AGENTS.override.md`, or nested `.mcp.json` files under `bin/`, `obj/`, `.vs/`, or a nested checkout. It also no longer repeats them through a link back into the repository. `dotnet-dependencies`, searching a directory without a solution, reports each nested checkout as an `UNREADABLE` line and does not read its projects; to answer for a submodule's projects, run it with `--root` on the submodule.
- Pull request: #1222

### repo-cleanup keeps worktrees holding an operation, refs, or hidden edits, removes ones holding only build and IDE output, and refuses offers it could not name

- Level: patch, defect fixes in a shipped skill's judgement with no command or fact kind added.
- Contract: none
- User action: none. Expect a sweep to keep, and report with the reason, a worktree with a bisect, rebase, merge, cherry-pick, or revert in progress, a ref under `refs/worktree`, `refs/bisect`, or `refs/rewritten`, or an edited file whose skip-worktree or assume-unchanged bit hides it from `git status`. It now removes a merged worktree whose only ignored files are `.vs`, `bin` and `obj` beside a .NET project file, or `node_modules` beside a `package.json`, and reports them as "regenerable path" where it said "regenerable cache". It lists a branch, worktree, or directory whose name holds a line separator, a control character, or a byte that is not UTF-8 under "Preserved", not offered. When Windows keeps a worktree's folder after Git deleted its record and files, the branch's reason says so, and the folder is offered as a leftover. `remove-directory` refuses with exit 1 a confirmed orphan that now resolves outside its area. A confirmation on a plan no sweep applied says `run the sweep again for a new plan`.
- Pull request: #1224

### Repository skills report what they could not read or write and hold their names

- Level: patch, defect fixes in repository skills and one validation policy; no shipped skill changes.
- Contract: none
- User action: none for users of the shipped skills. For maintainers: a repository skill under `.claude/skills/` and its `.agents/skills/` shim must be named by the skill naming rules in "Files" of `docs/adding-a-skill.md`, with a frontmatter `name` equal to the folder's name. `audit_repository.py rotation --record` leaves the record unchanged when its write fails.
- Pull request: #1221

### review-document reviews a committed document under git's name, skips a pull request's links, refuses an oversized document, and pairs each report with its fixture

- Level: patch, defect fixes with no contract change.
- Contract: none
- User action: none. `build` now ends with a `FAILED` line naming git's reason when git cannot read a committed document, where it used to review the document whole. It refuses a document over the 16 MiB a review reads before writing a fixture. It prints `SKIPPED <path> is a symbolic link ...` or `... a submodule ...` for a pull request document GitHub answers that way, instead of failing the whole build. `report` refuses a recorded report whose record is not the review of the fixture it is given.
- Pull request: #1220

### The skill evaluation refuses input it cannot judge, holds --records to what a run is held to, and writes no machine path into its results

- Level: patch, defect fixes in a repository tool; no shipped skill changes.
- Contract: none
- User action: none for users of the shipped skills. For the maintainer's `evaluate-skill` runs: a `--records` record must be one code-review-core's record contract accepts, at the scenario's review version, with every reviewer held by the guard and reporting the model its file is named for. A skill name with no scenarios, a non-positive `--timeout`, `no_finding_above MUST_FIX`, a category the scenario's manifest does not list, a drive path, and a results table holding anything but its header, separator, and rows are refused before anything runs.
- Pull request: #1216

### dotnet-format runs the SDK's dotnet format on a .sln of SDK-style projects and reads analyzer settings and the Roslynator package as the build does

- Level: patch, defect fixes with no contract change.
- Contract: none
- User action: none. A `.sln` whose projects are all SDK-style is now formatted by the .NET SDK's `dotnet format`, so it needs the .NET SDK 9.0.200 or newer, already the `dotnet` floor `deploy.py check` reports, and the `dotnet-format` global tool is used only for a `.sln` holding a project in the older format; only that tool skips an ASP.NET project's files. `config` reports a severity set in a `.globalconfig` as the repository's own, honours `root = true` only before a file's first section, and reports the package `present`, `missing`, or `unknown <reason>` from MSBuild's evaluation. `check` reports a file it cannot decode as `UNREADABLE` and checks the rest, and a failed `fix` names its `SAVED` and `UNFINISHED` files.
- Pull request: #1217

### Validation reads more of each rule it states

- Level: patch, stricter repository validation with no contract change.
- Contract: none
- User action: none. A contributor whose change writes one of the forms these policies used to miss is told so by validation, by file and line.
- Pull request: #1219

### flag-review-finding bounds a flag's category and body and the store's size, and refuses a pull request or review version named alone

- Level: patch, defect fixes: new refusals of input the store's readers could not use, with no change to the store's format.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. `add` and `edit` now refuse a category longer than 120 characters and a body longer than 4000, `add` refuses `--pull` without `--repository` and `--review-version` without `--finding`, and a change that would make the flag store larger than 4194304 bytes is refused, each with a `FAILED` line naming why and the store left as it was. A store that already holds such flags still loads.
- Pull request: #1211

### audit-ai-config reports what it could not read and how Codex really loads instructions

- Level: patch, defect fixes in the audit engine's findings and Markdown report, with no contract change.
- Contract: none
- User action: none. A repository the audit called `RESULT COMPLIANT` may now be `RESULT ERRORS` when a file a check reads cannot be decoded, a root `AGENTS.override.md` masks `CLAUDE.md`, or the root file Codex chooses is empty. A Codex setup that relies on `CLAUDE.md` without an `AGENTS.md` now draws a `WARNING`, unless `.codex/config.toml` names it in `project_doc_fallback_filenames`. A script that reads the Markdown findings table sees punctuation as character entities, so it should read the `--json` report instead.
- Pull request: #1215

### The MCP handshake reads .vscode/mcp.json as the audit does and refuses an unstartable server alone

- Level: patch, defect fixes in the handshake's status lines.
- Contract: none
- User action: none. A `.vscode/mcp.json` without its `servers` wrapper is now `CONFIG_ERROR` with exit 1, not `CONFIG_WARNING` and `NO_SERVERS`, and its malformed entries are reported. A server whose command, args, cwd, or env holds a NUL, or whose env name is empty or holds `=`, is now `HANDSHAKE_FAILED` with that reason, and the servers after it still run, where the whole run used to end `FAILED`.
- Pull request: #1215

### dotnet-dependencies reports a version it cannot settle, finds a central versions file above the solution, answers through the SDK in any CLI language, and reports linked folders

- Level: patch, defect fixes with no contract change.
- Contract: none
- User action: none. Answers change where the earlier reading was wrong. A `Version`, `VersionOverride`, `HintPath`, or central `PackageVersion` version whose property reference cannot be settled, such as one from a property no file sets or set after an import not read, is reported as an `UNREADABLE` line and not applied, so the hop reads `v?` and `conflicts` no longer reports its text as a version. A `Directory.Packages.props` above the solution's folder is found as MSBuild finds it: read inside the checkout, and reported as an import not read, by its path, above it. An import report for `Directory.Build.props`, `Directory.Build.targets`, or `Directory.Packages.props` names the found file's path rather than its bare name. `dotnet nuget why` runs with `DOTNET_CLI_UI_LANGUAGE=en`, so a non-English machine no longer prints an `SDK_FAILED` line for every restored project. An `SDK_FAILED` reason for an answer the script cannot read names the first line printed. When the script searches a directory for `.csproj` files, a junction, a symbolic link to a folder, or a folder it cannot list is reported as an `UNREADABLE` line and not entered, where a dangling or looping junction used to end the run `FAILED`. A failed `graph` query no longer leaves an empty temporary folder.
- Pull request: #1213

### A renamed file is routed by its old path and its new one, and a document the extractor fails on is skipped rather than ending prepare

- Level: patch, defect fixes in the code-review pipeline. The rename rule makes routing review what the manifest already names.
- Contract: `docs/code-review-operations-contract.md` (under Formats, the reviewer manifest's Specialist (`specialists-manifest.specialists[]`) row `include`: a renamed file is matched by its old path and its new one)
- User action: none. A specialist now reviews a file renamed out of its `include` pattern, or into its `exclude`, and `validate-reviewer` shows that route. A Word document or PDF whose text cannot be read for any reason is an `unextractable` coverage gap, and the review is `INCOMPLETE` rather than failed.
- Pull request: #1214

### This repository's reviewer routes more files to its specialists

- Level: patch, a fix to the repository's own reviewer manifest with no contract change.
- Contract: none
- User action: none. A machine whose code-review configuration names `review-repository` reads the manifest at each pull request's base, so reviews of pull requests based after this change start the added reviewers on their own.
- Pull request: #1210

### curate-agent-memory checks the memory store at the start and reports odd entries instead of failing on them

- Level: minor. New status lines (`REFUSED` from `resolve`, `UNREADABLE` from `reindex`), a new `unreadable` key in the audit report, and new refusals, each of which stops the run before anything is written.
- Contract: none
- User action: none. You may now see these:
- `resolve` reports a memory directory that holds no `MEMORY.md`, or lies inside a skills directory, as `REFUSED <reason>` and lists the other candidates, and `audit` refuses it with the same `FAILED` line that `reindex` and `delete` print. The skill stops at Step 1 or 2 instead of at Step 5.
- `audit --output` refuses an existing file, or one inside the memory directory, Claude Code's projects folder, or a skills directory.
- `resolve` prints `FAILED git could not read the repository: <reason>` when git is missing, times out, or fails, where it printed "not inside a Git repository", or fell back to the linked worktree's own folder.
- In a submodule or a repository with a separate Git directory, `resolve` derives the store from the working tree, not the Git directory; if you curated such a store before, the directory it names now differs.
  - `--repo <subdirectory>` resolves to the repository's top level.
- A directory or link named `*.md` in the store is reported as unreadable, not read.
- Pull request: #1207

### Skill and agent frontmatter that YAML refuses is refused at deployment

- Level: patch, defect fixes with no contract change. The frontmatter reader refuses shapes a runtime's YAML parser would not load, which it used to read as text.
- Contract: none
- User action: none, unless you deploy skills or agents of your own from this source. If a deployment reports a frontmatter value as `a mapping; quote it`, `not a scalar`, `indented with a tab`, or a value going on after a comment, quote the value or fix its indentation as the message says.
- Pull request: #1206

### analyze-skill-cost no longer requires HOME, reports unreadable Markdown, and judges each command of a joined line

- Level: minor, a new status line with a new exit status. The rest are defect fixes that bring the inventory in line with its documented rules, and a dropped required variable, which asks nothing of an existing configuration.
- Contract: `deploy-meta/analyze-skill-cost.json` (the `required_vars` list no longer names `HOME`), and the `analyze-skill-cost` status line `UNREADABLE <path> <reason>` from `skill_inventory.py`: `inventory` prints it for a Markdown file that is not UTF-8 text, where it counted the file as costing nothing, and exits 1 after it, and `tools` and `scan` fail on one
- User action: none. A configured `HOME` is still accepted, and redeploying picks up the new wording and script.
- Pull request: #1204

### configure reports a failed read of its configuration as a read, and a menu answer requests each item once

- Level: patch, defect fixes with no contract file changed.
- Contract: the `ERROR: Could not read the configuration` and `ERROR: Could not write the configuration` lines `deploy.py configure` now ends a file failure with, followed by the path remedy and the `--debug` hint, replacing `ERROR: Could not write configuration`; the path in every `deploy.py` command's `ERROR: Could not ...` line, now printed with forward slashes on Windows; `requested_bundles` and `requested_skills` in `.deploy-manifest.json`, which now hold each root once.
- User action: none, unless a script of yours matches `Could not write configuration` or a backslash path in a `deploy.py` error line. Match the new lines instead.
- Pull request: #1203

### The runtime canary ends a run at its limit with everything it started, and reports a runtime it cannot start as that skill's failure

- Level: patch, defect fixes in repository tools; no shipped skill changes.
- Contract: none
- User action: none for users of the shipped skills. For the maintainer's canary lines: a runtime the canary cannot start for a skill is now `RUNTIME <runtime> <skill> FAILED "cannot run <program>: <reason>"`, and the run goes on to its other skills, runtimes, and `MATRIX` lines, so no rerun with `--runtime` is needed to judge the rest. `timed out after <n> seconds` now means the session was ended at that limit, along with every process it started.
- Pull request: #1205

### update-coding-agent-skills compares the deployed commit's release, not local main's, and refuses a version it cannot compare

- Level: minor. The `MAJOR_UPDATE <current>..<target>` status line measures `<current>` by the commit `python deploy.py check` names as deployed, gains the value `unknown` when check names none, and lists the commits from that commit; `FAILED` gains a release tag whose major or minor has more than 18 digits and a deployed commit the clone does not hold.
- Contract: the `update-coding-agent-skills` status lines
- User action: none, unless the update now stops with `MAJOR_UPDATE`: a clone you pulled by hand past a release without redeploying, an installation whose `check` reports `Deployed commit: unknown`, or a pin you leave with `--unpin` to an older major. Read the release notes for `<target>` and start the skill again with `--cross-major`.
- Pull request: #1208

### github-activity-report refuses malformed pages and values, and a refused gh api request names GitHub's reason

- Level: patch, defect fixes with no contract change.
- Contract: none
- User action: none. A report for a login or organization GitHub cannot search now fails with GitHub's reason after `Validation Failed (HTTP 422)`. Check both logins' spelling and that the token can read the organization.
- Pull request: #1202

### review-prs waits for its native reviewers before checking them, and reports a run that prepared nothing as it is

- Level: patch, defect fixes in a skill's instructions with no contract change.
- Contract: none
- User action: none. After the next deployment, a review-prs run that starts reviewers as native subagents waits for them before `check`, instead of spending a reviewer's retry while it is still working, and an empty batch or a run where every pull request was skipped is no longer reported as failed.
- Pull request: #1201

### The hub guard refuses a git command it cannot place while a hub is guarded, and reads commands by each shell's grammar

- Level: patch, a contributor-facing tool with no contract change.
- Contract: none
- User action: none, in a clone that has not opted into the guard. In one that has, these are now refused where they were allowed: a git command that would write the checkout it runs in and whose directory, repository, or subcommand the text does not settle (a `$VAR` the command does not set, a `$( )`, a `cd` that only some paths run or whose target is not a directory, anything after `source`), a command naming git that the guard cannot read (Bash it cannot parse, a computed `bash -c` or `eval` script, a PowerShell command when `pwsh` cannot be started), `git fetch --update-head-ok` or `-u` in the hub, `git apply` with `--check` after `--` or with `--apply`, a `--git-dir` or `GIT_DIR` naming the hub's `.git` from anywhere, `git -C <hub> --work-tree=<worktree>` (it writes the hub's index), and an `Edit` or `Write` under the hub's `.git`. The refusal says what it could not settle; write the directory into the command. The notes "could not tell where `git <subcommand>` runs" and "could not find pwsh" now appear only outside every guarded hub; inside one they are refusal reasons.
- Pull request: #1200

### dotnet-dependencies evaluates a project as MSBuild does, its imports in order and each framework's build apart

- Level: patch, defect fixes in the evaluator with no contract change.
- Contract: none
- User action: none. Answers change where the earlier reading disagreed with MSBuild. A `Directory.Build.props`, an explicit `Import`, and a `Directory.Build.targets` are now read, so a `Choose` or condition on a framework they set is decided with it; this replaces the earlier promise that a `Choose` under a `Directory.Build.props` is reported rather than chosen, which now happens only when the file cannot be read. A `Choose` in `Directory.Packages.props` sees the framework as it is before the project body. A framework set from another property (`$(Tfm)`) is that property's value. An SDK-style project's frameworks are its `TargetFrameworks`, else its `TargetFramework`, never a `TargetFrameworkVersion` beside them; a legacy project's build is named by the framework its `TargetFrameworkVersion` names (`net472` for `v4.7.2`) and its `$(TargetFramework)` is unset. Item, property, and metadata names match without case; a `VersionOverride` child, a `HintPath` attribute, and the last of conditioned or repeated `Version` children are read; a `GlobalPackageReference` reaches every project. A project with no restore no longer counts an `Include` whose condition fails in every build it has (earlier, every `Include` counted whatever its condition); one that may apply, such as one under `$(Configuration)`, still counts. New `UNREADABLE` reasons: an `Import` not read (its condition or path cannot be settled, or its file is missing, unreadable, or outside the examined tree), after which every property is unsettled; an `AssemblyName` that cannot be settled; a metadata value whose condition cannot be settled; and an item whose name cannot be settled.
- Pull request: #1199

### An installation last deployed before bundle members were recorded keeps every installed skill on its next --all

- Level: patch. This fixes how the deployer reads a manifest entry written before bundle members were recorded, with no contract change.
- Contract: none
- User action: none. If your last deployment was by v0.6.0 or earlier, the next `--all` no longer removes the skills of an opt-in bundle you chose whose bundle the source has since renamed, given a new member, or that sits beside a skill v0.6.0 installed through a bundle request, so no `--include` is needed. It also no longer installs the rest of an opt-in bundle you never chose, when the installation was a version 7 manifest and only a dependency pulled one member in. One case reads wider: a skill installed only as another skill's dependency, which the source no longer has that skill depend on, now counts as chosen, and the dry run installs the rest of its opt-in bundle. If that bundle is not one you chose, deploy from the menu of `python deploy.py` and leave it out.
- Pull request: #1198

### The skill evaluation tool grants exact script paths and judges only the run's own record

- Level: patch, defect fixes in a repository tool, with no contract change for shipped skills.
- Contract: none
- User action: none for a user of the shipped skills. A contributor whose `tools/` module passes `--allowedTools` to Claude Code writes each grant as a string or an f-string, or returns them from a function of the module, and grants each script by its quoted path; validation now holds those grants to the skill grant rules.
- Pull request: #1195

### The runtime canary counts only a script the interpreter opened, and removes only a home holding its marker

- Level: patch, defect fixes in a repository tool; no shipped skill changes.
- Contract: none
- User action: none for users of the shipped skills. For the maintainer's canary lines: `RAN` now needs a script file the interpreter could open, so a missing script, a folder, or `python -m` is `FAILED "never ran a script from the skill"`, and a script that does not compile shows `exit=1` on Python 3.12 or later. `--remove-home` refuses a folder without the `.runtime-canary/home.json` marker each run now writes, so delete by hand any home an earlier version made.
- Pull request: #1194

### audit-ai-config refuses a handshake outside the repository and reports links that lead out of it

- Level: patch, defect fixes that bring the scripts to their documented bounds.
- Contract: none
- User action: none. The handshake now prints `CONFIG_ERROR <file> Server '<name>': cwd '<cwd>' is outside the repository` (or `command '<command>' is outside the repository`) and `SKIPPED` for a server whose working directory or relative command leads out of the repository, where it used to start it. The audit reports `Link leads out of the repository; the audit does not follow it` as an inventory `WARNING` for each such link instead of reading the files behind it. A line break in a Markdown report cell now appears as a space, not `<br>`.
- Pull request: #1189

### audit-repository briefs write each path and ref as a quoted JSON string

- Level: patch, a defect fix in a repository skill with no contract change.
- Contract: none
- User action: none for a user of the shipped skills. For the maintainer, an `audit-repository` brief now lists each path and ref as a JSON string in code formatting, which its reader unquotes as the brief says.
- Pull request: #1167

### change-skill grants and the repository tools' --root

- Level: patch, a defect fix in a repository skill and contributor-facing tools with no contract change.
- Contract: none
- User action: none. Contributors meet the new forms: `tools/new_skill.py`, `tools/skill_shims.py`, and `tools/skill_reference.py` refuse a `--root` without `source.json` and `.claude/skills`, or one in a skills directory, as a usage error. `change-skill` now pre-approves only `skill_shims.py --write`, `skill_reference.py --write`, and `skill_reference.py`, so any other form of those two prompts.
- Pull request: #1171

### dotnet-format refuses solution entries outside the repository and never writes through a link

- Level: minor, a new output line (`REFUSED`) an agent reads; before 1.0.0 it ships in the next release's minor level.
- Contract: `csharp_layout.py REFUSED`
- User action: none. A solution whose entries name a project outside the repository holding it, or a network-share or device path, now sees `REFUSED <entry> <reason>` for each from the layout configuration check, and those projects no longer count toward `PACKAGE`. A fix or an added `.editorconfig` setting for a file that is, or lies below, a symbolic link or Windows junction is reported as `REFUSED <path> <reason>` and left undone. A changed file whose name starts with `@` or `-` is passed to the formatter as `./<name>`.
- Pull request: #1154

### curate-agent-memory keeps every fact on one line, honors the block on outside reads, and moves rules into a skill's source

- Level: minor, a new `skill-source` command and changed `resolve`, `reindex`, and `delete` results.
- Contract: none
- User action: none. `resolve` reports no memory directory when `permissions.blockReadsOutsideWorkingDirectories` is on and a repository's settings chose the directory, and the skill asks you to choose one. A rule moved into a skill now lands in that skill's source checkout, so deploy again for the deployed skill to carry it.
- Pull request: #1155

### analyze-skill-cost reports a grant that names no script, and reads commands from every fence and from code spans

- Level: minor. A new output line: `skill_inventory.py tools` prints `NO_SCRIPT_ALLOWED <entry>` for a shell pattern whose text before its `*` names a program but no script, such as `python *`, `bash -c *`, or `git *`, which the audit reports as MUST FIX and validation refuses. The audit now reads commands from a fence wherever Markdown allows one (indented, in a list item, in a blockquote, under a label such as `zsh`) and from a code span in which a program runs a script.
- Contract: none
- User action: none. A skill audited before this release may now show `NO_SCRIPT_ALLOWED`, or `UNGRANTED` and `GRANTED_REPOSITORY_CODE` for a command it shows in a code span or a fence the audit did not read; grant each script by name, or the `git` or `gh` subcommand it runs. A contributor whose skill grants such a pattern sees validation fail until it names the script.
- Pull request: #1156

### update-coding-agent-skills stops at TAG_SHADOWS_BRANCH when a tag is named main or origin/main

- Level: minor, a new status line: the update reads and moves `main` and `origin/main` by their full names, so a tag of either name no longer stands in for the branch, and when such a tag is on `origin` or in the clone the update stops before fetching with `TAG_SHADOWS_BRANCH` followed by each tag. A clone that holds such a tag now stops where it used to update.
- Contract: none
- User action: none, unless the update stops at `TAG_SHADOWS_BRANCH`: then remove the listed tag from the clone with `git tag -d <name>`, or have it removed from `origin`, and rerun the update.
- Pull request: #1137

### repo-cleanup keeps a directory Git tracks files in under a worktree area, with a new PRESERVED reason

- Level: patch, a defect fix. The sweep adds a status line, a `PRESERVED <path> <reason>` naming the files Git tracks there or Git's error, in place of an `ORPHAN` offer, and `remove-directory` exits 1 when Git now tracks a file in a confirmed directory. No line kind is added or renamed.
- Contract: none
- User action: none
- Pull request: #1093

### code-review-core refuses an origin on another host, and dotnet-format refuses a file above the read bound

- Level: patch, defect fixes with no contract change.
- Contract: none
- User action: none. A configured repository whose checkout's origin is on a host other than github.com, even one whose path ends in `github.com/<owner>/<repo>`, is now refused when a review prepares, as not a recognizable GitHub remote; point that checkout's `origin` at github.com. `dotnet-format` now refuses a solution, project, or `.editorconfig` file larger than `READ_LIMIT_BYTES` in skill-core's `scripts/dotnet_solution.py`, or one that is not a regular file, with a `FAILED` line naming the file and the reason, as `dotnet-dependencies` already reports such a file `UNREADABLE`.
- Pull request: #1094

### Programs are never found through a relative PATH entry, and git is started at the path the locator finds

- Level: patch, defect fixes with no contract change.
- Contract: none
- User action: none, unless a program the skills or `deploy.py check` use is reachable only through a relative `PATH` entry such as `.` or `bin`. In that case, name its folder by its absolute path in `PATH`.
- Pull request: #1095

### The pull request tracker writes pull request titles and author names as literal text

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none. The next `update` rewrites every row of the owned section with the new escaping, so a title's punctuation is stored as character entities and Markdown in a title shows literally. A dashboard that a title already broke, so that `update` refuses with `Dashboard must contain exactly one marker pair`, needs the stray marker removed by hand once; after that, no title can break it.
- Pull request: #1092

### Skills pre-approve only the scripts they run, by name

- Level: patch, a defect fix that narrows what skills pre-approve.
- Contract: none
- User action: none. If you approved a skill's `scripts/*` commands before, Claude Code may ask once more for the narrower named commands. A command that reaches another skill's scripts through `scripts/../../` now asks for approval in the starting skill's turn. A new skill scaffolded with `python tools/new_skill.py` now grants `scripts/<name with underscores>.py` by name rather than `scripts/*`.
- Pull request: #1078

### review-insights prints record text on one line, and review-document refuses a document path the pipeline gives no reviewer

- Level: patch, defect fixes in two skills' printed lines.
- Contract: none
- User action: none. A `REVIEWER`, `EXAMPLE`, or Markdown value holding a line break now shows it as a space. A document whose path the code-review pipeline would leave out of every prompt, such as one holding U+2028 or starting with a drive letter, is now skipped or refused by review-document with the plain refusal.
- Pull request: #1061

### review-prs and review-document pre-approve only the scripts they run, and advance refuses a batch dated after today

- Level: patch, a defect fix that narrows what two skills pre-approve.
- Contract: none
- User action: none. If you approved `code-review-core`'s scripts for these skills before, Claude Code may ask once more for the narrower named commands. A `code-review-core` command the skills never run, such as `review_config.py`, now asks for approval when run in the review's turn. `review_pipeline.py advance` now refuses, with `FAILED <file> is dated <date>, after today, <today>`, a batch file dated after today in UTC, which `enumerate` never writes.
- Pull request: #1060

### dotnet-dependencies refuses a referenced project outside the checkout, and any file above its read bound

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none. A solution whose entries or `ProjectReference` items name a project outside the checkout holding it (or, outside a checkout, outside the solution's directory), or a network-share or device path, now sees an `UNREADABLE` line naming that project and the reason instead of its facts, and the run exits 1. A file larger than `READ_LIMIT_BYTES` in the skill's `scripts/msbuild_reader.py`, or one that is not a regular file, is reported the same way.
- Pull request: #1015

### PowerShell, git, and mypy are never taken from the working directory

- Level: patch, a defect fix with no contract change.
- Contract: none
- User action: none. `deploy.py check`, a deployment, and validation started from a folder that holds a program named `pwsh`, `powershell`, `git`, or `mypy` now use the one `PATH` names, never the one in that folder.
- Pull request: #1012

### The reviewer guard denies network, device, and out-of-root paths unresolved, and never claims a run nested in another

- Level: patch, a defect fix in the reviewer guard with no contract change.
- Contract: none
- User action: none. Redeploy to pick up the guard. A guarded reviewer that names a path outside the system temporary directory and `code-review-core`'s folder, or a network or device path, is now denied with "give an absolute path inside your review run folder" or "give a local path, not a network or device path" before the guard touches it.
- Pull request: #1014

### Skill documents describe what repo-cleanup, dotnet-dependencies, and runtime-canary already do

- Level: patch, documentation of existing behavior.
- Contract: none.
- User action: none.
- Pull request: #969

### Documentation of the renamed-bundle exception and the runner

- Level: patch, documentation only.
- Contract: none.
- User action: none.
- Pull request: #970

### The code-review documents describe review-insights' output and refusals as the code has them

- Level: patch, documentation corrections with no behavior change: the format tables' descriptions now match what the code already writes and refuses, and no field, value, or line changes.
- Contract: `docs/code-review-operations-contract.md`
- User action: none
- Pull request: #968

## v0.8.0

Level: minor.

### A plain `--all` installs no unchosen opt-in bundle from a home v0.6.0 last deployed into, and keeps a renamed one only while it holds no new skill

- Level: patch, defect fixes with no contract change.
- Contract: none
- User action: none, unless your last deployment was v0.6.0 from a source with an opt-in bundle and it preserved or skipped a locally modified skill. In that case, preview the next `--all` with `python deploy.py --all --dry-run`. It may report `REMOVE` for the skills of an opt-in bundle you chose that the source has since renamed, moved a skill out of, or given a new skill, or of one that deployment installed a skill through only by a bundle request it recorded: one typed as `--include <bundle>`, chosen from the menu, or a bundle a plain `--all` installed. If it does, run `python deploy.py --all --include <bundle>`, naming that bundle by its current name, and name each skill the source moved out of it by its new selection item, its new bundle or its own name, with another `--include`. Otherwise the next `--all` in such a home installs nothing from an opt-in bundle you never chose: it reports `UNCHANGED` where it used to report `FRESH INSTALL` for the rest of a bundle whose member another skill only depends on. It also keeps a renamed opt-in bundle's skills `UNCHANGED` beside a preserved skill that a newly installed skill depends on, and beside a skill v0.6.0 preserved after you deselected it.
- Pull request: #930 and #914

### The hub guard reads a location variable assigned in parentheses or in a multiple assignment

- Level: patch, a defect fix in the repository's hub guard.
- Contract: none
- User action: none. From a worktree, a PowerShell command that assigns `GIT_DIR`, `GIT_WORK_TREE`, or `GIT_COMMON_DIR` in parentheses or as a target of a multiple assignment and then runs a refused git subcommand is now judged as running in an unknown place, as the plain `$env:` assignment is, instead of being allowed as acting on the worktree.
- Pull request: #928

### dotnet-dependencies reports a Choose on an unset framework under a Directory.Build.props instead of choosing it

- Level: patch, a defect fix in the condition reader with no contract change. Under a `Directory.Build.props`, a `Choose` whose `When` tests `$(TargetFramework)` before the project file sets it is no longer chosen as if the framework were unset. Its items are reported as `UNREADABLE` lines, and the run exits 1, so a project its `When` removes is no longer silently `MISSING`.
- Contract: none
- User action: none
- Pull request: #927

### dotnet-dependencies chooses a Choose branch once for its items, and reports an unevaluable Remove without a restore

- Level: patch, defect fixes in the condition reader with no contract change: a `Remove` in a `Choose` branch MSBuild never enters is no longer applied, nor in a project with a restore any other item there (no silent `MISSING` for a project a never-entered `Otherwise` removes), while a project with no restore still counts every `Include` and `Update` whatever its condition, and under a `Directory.Build.props` a `Choose` on a framework the project has not set yet is reported rather than chosen, as #927's entry says; a project with no restore whose conditional `Remove` cannot be evaluated keeps the item and now prints an `UNREADABLE` line naming the `Remove`, exiting 1; and a project whose framework a `Choose` with an unevaluable `When` sets now names every framework its branches may give it.
- Contract: none
- User action: none
- Pull request: #912

### The hub guard reads a git location variable through \Environment::, variable notation on the provider, and a cast, and no longer through Environment::\name

- Level: patch, a defect fix in a contributor-facing tool with no contract change.
- Contract: none
- User action: none. From a worktree, `Set-Item \Environment::GIT_WORK_TREE <hub>; git reset --hard` and `${Environment::GIT_WORK_TREE} = '<hub>'; git reset --hard` are now allowed only with the note that the guard could not tell where git runs, and in the hub `Set-Item Environment::\GIT_DIR x; git commit` is refused, as it was before v0.8.0's audit fixes: the guard refuses more.
- Pull request: #913

### The hub guard reads a git location variable named in any case in Bash and set through PowerShell's Environment:: path

- Level: patch, a defect fix in a contributor-facing tool with no contract change.
- Contract: none
- User action: none. In a clone that opts into the hub guard, a Bash command such as `git_work_tree=<hub> git reset --hard` from a worktree is now refused, and `Set-Item Environment::GIT_WORK_TREE <hub>; git reset --hard` in PowerShell is allowed only with the note that the guard could not tell where git runs.
- Pull request: #884

### A plain `--all` keeps a chosen opt-in bundle's skills when v0.6.0 also installed a new skill while preserving one

- Level: patch. It fixes a defect in this release's handling of a manifest v0.6.0 wrote.
- Contract: none
- User action: none. The affected home was deployed by v0.5.0 or earlier with an opt-in bundle. It was then deployed by v0.6.0 while a skill was locally modified (preserved) or skipped, and that deployment also installed a skill the earlier installation didn't have. On such a home, the first `--all` after the source renamed the bundle or moved a member out of it reported `REMOVE` for the bundle's skills. It now reports them `UNCHANGED`, except where the entry for #930 and #914 above still asks you to name the bundle with `--include`. `--migrate-from` away from such an entry carries the request, and `deploy.py check` counts those skills' tools as required. If the new skill brought a dependency of its own, that dependency counts as chosen only when no bundle of the source holds it or the home holds every member of its bundle, so a later `--all` never installs the rest of an opt-in bundle you never chose for it. If the installation requested no skill on its own (only bundles), the entry reads as before.
- Pull request: #882

### repo-cleanup reports a refused branch deletion, never searches through a link, and matches a branch's upstream by its exact name

- Level: patch, defect fixes bringing the commands in line with their documented rules.
- Contract: none
- User action: none. `delete-local`, and the sweep, now exit 1 when Git refuses to delete a branch, including one whose worktree was just removed with it, where before they exited 0 after printing `PRESERVED`. A worktree area reached through a link or junction, such as a `.claude` that is one, is no longer searched for leftover directories, and `remove-directory` refuses, with exit 1, a directory that resolves outside its area. A branch counts as pushed under its own name, and so as finished once the default branch holds it, only when its upstream is exactly `origin/<its name>`: a branch tracking `origin/feature/<its name>` or another remote is now kept.
- Pull request: #881

### review-insights finds every nearby reviewer disagreement and credits each guidance version only to its own reviewers

- Level: patch. These are defect fixes in a skill this release added. The report schema is unchanged.
- Contract: `docs/code-review-operations-contract.md` (the `synthesis-input:guidance-file` `path` and `reviewers` rows, now one line per pinned version, and the recommendation target's `path` row, which bounds a `move-to-script` path)
- User action: none. A report can show disagreements a nearby same-severity finding used to hide. A guidance file that reviewers were given at different versions now has one line per version, each naming only its own reviewers. A guidance file the checkout cannot read prints a `GUIDANCE_UNREADABLE` line, and the report falls back to the reviewer's commit instead of failing. A synthesis whose `move-to-script` path holds a control character, a quote, a backtick, `$`, or a backslash, or is longer than 260 characters, is refused, and every printed `TARGET` line stays on one line.
- Pull request: #880

### dotnet-dependencies applies a conditional Remove only to its frameworks

- Level: patch, a defect fix in this release's condition reader with no contract change: a `ProjectReference`, `Reference`, or `PackageReference` `Remove` under a condition no longer removes the item for every framework, so a project removed for one framework is read and walked for the others (no silent `MISSING`), and a project with no restore removes an item only where the condition holds for every framework it names.
- Contract: none
- User action: none
- Pull request: #879

### dotnet-dependencies evaluates a Choose once, when it is reached

- Level: patch, a defect fix with no contract change: a project whose target framework comes from a `Choose`, or from a `PropertyGroup` whose condition reads the framework it sets, now names the framework MSBuild picks, where before a `When` that set the framework it tested handed over to the `Otherwise`.
- Contract: none
- User action: none
- Pull request: #879

### The update script ignores git's repository variables in any case on Windows

- Level: patch, a defect fix with no contract change: `update.sh` now unsets git's repository-selecting variables whatever their case, so a lower-case `git_dir` in the shell no longer points the update at another clone.
- Contract: none
- User action: none
- Pull request: #844

### flag-review-finding corrects an open flag's category and rationale

- Level: minor. A new `edit` command; the flag store's schema and every existing command are unchanged.
- Contract: `docs/code-review-operations-contract.md` (the `flag-review-finding` row under Skill behavior)
- User action: none
- Pull request: #842

### audit-ai-config counts path-specific instructions as a Copilot CLI/app source

- Level: patch. A repository with only `.github/instructions/**/*.instructions.md` and a Copilot CLI or Copilot app target now reports `Copilot CLI/app: effective sources: .github/instructions` instead of the `ERROR` "no instruction sources available", and a repository that has them beside other sources lists them too.
- Contract: none
- User action: none
- Pull request: #841

### A condition whose processes could not be ended is no longer reported as one that could not run

- Level: patch, since a failure that already failed the review now says what happened.
- Contract: none.
- User action: none; a review whose condition script started but whose processes could not be ended now ends with `FAILED` and `Condition script started, but its processes could not be ended: <script>: The job's processes may still run: ...` (or `The group's processes may still run: ...` on Linux) in place of `Condition script could not run: ...`. Those processes may still be running, so check for them before rerunning the review.
- Pull request: #840

### analyze-skill-cost reports inline Python written with the interpreter token

- Level: patch. A bug fix: the `generated-code` cue now finds `{{PYTHON}} -c`, and `python -c` or `python3 -c` with interpreter options before `-c`, so a source-tree audit raises the MUST FIX the skill already described.
- Contract: none
- User action: none. An audit of a skill that runs inline Python may now report a MUST FIX it did not before.
- Pull request: #839

### github-activity-report refuses a search response whose count is not a non-negative integer

- Level: patch, a defect fix: a malformed response is refused rather than used.
- Contract: none
- User action: none
- Pull request: #838

### `curate-agent-memory` lists candidate directories when the configured one is missing

- Level: patch, a defect fix: `memory_audit.py resolve` now prints the `CANDIDATE <dir>` lines its documentation promised when a settings-named `autoMemoryDirectory` does not exist or settings name no usable directory, so the skill offers the memory directories it found instead of an empty list.
- Contract: none.
- User action: none.
- Pull request: #837

### review-insights reports consistency signals and reads guidance text

- Level: minor. The insights report (schema 9) gains `signals` and a "Consistency signals" Markdown section, the synthesis input gains `guidance-file`, `severity-drift`, `verdict-flip`, `reviewer-disagreement`, `guidance-overlap`, and `guidance-unsupplied` lines, and synthesized recommendations gain the `consolidate` and `move-to-script` types and a cheapest-fix order within each priority.
- Contract: `docs/code-review-operations-contract.md` (the insights report's `schema_version`, now `9`, and its new `signals` field; the signal tables: `insights.signals` and its `severity_drift`, `verdict_flips`, `reviewer_disagreements`, `guidance_overlaps`, and `unsupplied_references` rows; the recommendation `type` rows, which gain `consolidate` and `move-to-script`, with the new `merge` field and the `target` rows those types change; and the synthesis-input `guidance-file`, `severity-drift`, `verdict-flip`, `reviewer-disagreement`, `guidance-overlap`, and `guidance-unsupplied` lines)
- User action: none. An existing report reads as before and gains its signals when it is regenerated. To give the synthesis your guidance text, set the repository's `checkout_path` in the code-review configuration.
- Pull request: #835

### `deploy.py --debug` shows the traceback of a failure no command handled

- Level: patch, a defect fix with no contract change: the documented `--debug` and `DEPLOYER_DEBUG` behavior now holds for every failure.
- Contract: none.
- User action: none. A run with `--debug` or `DEPLOYER_DEBUG=1` that ends with a `FAILED` line now prints its traceback on stderr after it.
- Pull request: #830

### The deployment lock no longer treats a Windows process it cannot open as gone

- Level: patch, a defect fix: on Windows, a lock holder whose process `OpenProcess` refuses with an error other than an invalid parameter is now reported running with an unreadable start time instead of gone.
- Contract: none
- User action: none. A deployment that meets such a lock now stops with "Lock PID <pid> is alive but its start time cannot be read" instead of reclaiming it; if no deployment is running, remove the lock as the message says.
- Pull request: #828

### dotnet-dependencies labels a conditional assembly, project, or central-version reference with the frameworks that hold it, and leaves a Target's items out

- Level: patch. It is a defect fix with no contract change. A `Reference`, `ProjectReference`, or central `PackageVersion` under a framework condition, and the chains through it, are now labelled only with the frameworks it holds for, where before they carried every framework. An item inside a `Target` is no longer read as a reference; an `UNREADABLE` line names it. An `UNREADABLE` reason for a condition the script cannot evaluate now says what it did instead: an item left out, an `Update` not applied, or a `Remove` not made. A project with a `When` that has no `Condition` is reported unreadable.
- Contract: none
- User action: none
- Pull request: #834

### A plain `--all` keeps a chosen opt-in bundle's skills after v0.6.0 preserved a skill through its manifest upgrade

- Level: patch. It fixes a defect in this release's handling of a manifest v0.6.0 wrote.
- Contract: none
- User action: none. The affected home was deployed by v0.5.0 or earlier with an opt-in bundle, then by v0.6.0 while a skill was locally modified (preserved) or skipped. On such a home, the first `--all` after the source renamed the bundle, or moved a member out of it, used to report `REMOVE` for the bundle's skills. It now reports them `UNCHANGED`, except where the entry for #930 and #914 above still asks you to name the bundle with `--include`. `--migrate-from` away from such an entry carries the request, and `deploy.py check` counts those skills' tools as required. One exception: if that installation requested no skill on its own (only bundles), the entry reads as before.
- Pull request: #832

### update-pr-tracker refuses an override for a pull request it does not track, and marks overrides it no longer tracks

- Level: minor. A command that accepted any well-formed key now refuses one the tracker does not hold, the listing has a new line, and the tracker keeps a new file.
- Contract: `docs/code-review-operations-contract.md`, its new tracker record format (`tracker`)
- User action: run `update-pr-tracker` once after updating, before setting a status override. `override --set` reads the `tracker.json` record that each update now writes beside the code-review configuration, and refuses every key until an update has written it. After that, it refuses a key for a pull request the last update did not track. Existing overrides are kept, and the listing now marks each one whose pull request the tracker no longer holds as `OVERRIDE_UNTRACKED`, so you can clear the ones you no longer need.
- Pull request: #833

### A condition's file stays within its 32 KiB cap and holds the count the prompt states

- Level: patch, a defect fix with no contract change: the file a condition gives its specialists is the script's own bytes, cut before a character the cap splits, and its length is the count the prompt and the plan state, where before a split character or bytes that are not UTF-8 made it larger than both.
- Contract: none
- User action: none
- Pull request: #829

### A review comment's author, path, and url are never blank

- Level: patch, a fix that holds records to what GitHub's reader already writes.
- Contract: `docs/code-review-operations-contract.md`
- User action: none.
- Pull request: #827

### repo-cleanup cleans branches main already holds and asks about local-only worktrees, detached worktrees, and leftover directories

- Level: minor, new behavior, new questions, and a new command.
- Contract: `skills/repo-cleanup/SKILL.md` (the new `remove-directory` command, its `--plan` and `--path` arguments, and its `REMOVED`, `DELETED_DIR`, and `PRESERVED <path> <reason>` status lines; the sweep's new `CONFIRM_DETACHED <path> <sha>` and `ORPHAN <path>` lines and the worktree `CONFIRM_LOCAL` now carries; and the `REMOVED` line `delete-local` prints for a worktree it removes with its branch)
- User action: none. A sweep now deletes a branch pushed under its own name with no pull request when the default branch holds it and its remote branch, removing its clean worktree. It asks before removing a clean worktree whose local-only branch you delete (`CONFIRM_LOCAL` now carries the worktree's path), a clean detached worktree whose commit is on the default branch or a remote branch (`CONFIRM_DETACHED`), and a directory in the worktree area that Git no longer tracks (`ORPHAN`), all through the new `remove-directory` command. The summary gains "Detached (kept)", "Orphans (kept)", and "Orphans deleted".
- Pull request: #822

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
