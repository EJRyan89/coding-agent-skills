# Adding a skill

This is the contract a skill must meet. The `change-skill` repository skill (`.claude/skills/change-skill/SKILL.md`) is the procedure for meeting it, and `python tools/new_skill.py <name> --description "<description>"` scaffolds a new skill's frontmatter, metadata, and reference section; run it with `--help` for the other options.

## Files

Add each skill as a pair:

```text
skills/<skill-name>/SKILL.md
deploy-meta/<skill-name>.json
```

The `name` in the `SKILL.md` frontmatter must exactly match the directory and metadata filename. Skill and shared-asset names must be unique.

The deployer and the repository tools read `name`, `description`, `argument-hint`, and the invocation flags with `frontmatter.py` from the hidden `skill-core` skill. Each of those may be a plain, quoted, or block (`|` or `>-`) scalar, but not a list or a nested mapping. Deployment stops with the reason when `name` cannot be read, and validation when any of them cannot. Keys they do not read, such as `hooks`, are never parsed. A skill script that reads frontmatter imports the same reader as `frontmatter`, declaring `skill-core` like its `console.py`.

Claude Code loads every model-invocable skill's description into every session, and Codex and GitHub Copilot CLI read it through the skill's runtime adapter. Keep it within 1,024 characters once rendered: Copilot refuses a longer one, so deployment stops instead. Add `disable-model-invocation: true` to a skill only the user should start, such as one that deletes branches or redeploys, and also `user-invocable: false` to an internal dependency nobody starts directly. Never add either to a skill another skill invokes by name; a contract test checks this.

Skill and bundle names must:

- be at most 64 characters, the limit for a skill `name` in the Agent Skills format;
- use only lowercase letters, digits, and hyphens, without a leading or trailing hyphen, such as `repo-cleanup` or `review-prs`;
- not be a reserved Windows device name such as `con`, `aux`, `nul`, `com1`, or `lpt1`.

The Agent Skills frontmatter rules ("YAML frontmatter requirements" in Anthropic's [skill authoring best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)) add two more:

- a skill name must not contain the reserved words `anthropic` or `claude`, so `claude-helper` is refused;
- neither a skill's name nor its description may contain an XML tag such as `<tag>` or `</tag>`. The naming grammar already keeps angle brackets out of a name; in a description, a bare `<` or `>`, as in `a < b` or `x -> y`, is not a tag and is allowed.

The deployer rejects a name that breaks these rules before rendering anything, and a description with an XML tag once rendered, before changing any file. `tools/new_skill.py` refuses the same values.

When a skill needs executable logic, place both its implementation and tests under:

```text
skills/<skill-name>/scripts/
```

A skill's executable files are Bash, Python, and PowerShell: `.bash`, `.sh`, `.py`, and `.ps1` files, run by `bash`, `python`, and `pwsh`, the commands `deployer/tools.py` lists, and rejected anywhere else in a skill. JavaScript and TypeScript are not supported, because no runtime they need is a deployment prerequisite and the renderer cannot validate them: `.js`, `.mjs`, `.cjs`, and `.ts` files are rejected anywhere in a skill, `scripts/` included. Keep `SKILL.md` and supporting Markdown focused on orchestration and explanation. Executable-language fences are reserved for short command examples of at most five lines; extract longer logic into `scripts/`.

The body keeps what the agent needs to run the skill: the commands, what their output means, and what to ask the user. What a person reads once, such as guarantees the scripts enforce, a glossary of output that labels itself, or a procedure for an action the skill never takes, belongs in the skill's section of [skills.md](skills.md) instead. For structure, follow Anthropic's [skill authoring best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices): keep the body under 500 lines, link every reference file directly from `SKILL.md`, and start a reference file over 100 lines with a table of contents. `analyze-skill-cost` checks all of this, by content rather than by size.

### Description

A model-invocable skill's description is all the model reads when deciding whether to start it, so it says what the skill does, then when to use it: `<What it does>. Use it when <the requests and situations that call for it>.` Name the requests the way a user would make them and, where it helps, a near miss the skill should not take. Keep what it does first, and add trigger cues rather than length, since every session loads the description. A user-only skill's description never starts anything, so it needs no such clause.

`tools/new_skill.py` refuses, and validation fails for, a model-invocable skill whose description has no `Use it when`, `Use when`, `Use it before`, or `Use it after` clause.

### Granting tools

In Claude Code, `allowed-tools` pre-approves the listed tools for the turn that starts the skill, so they run without asking the user. It restricts nothing: a tool it leaves out still runs after the usual prompt. Codex ignores the field, and the adapters do not carry it to Copilot, which cannot scope a shell grant (see the end of this section).

Grant shell commands as patterns, never as a bare `Bash` or `PowerShell`, which would pre-approve every command in that turn:

```yaml
allowed-tools: ["Bash(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "PowerShell(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "Read"]
```

- Grant each pattern to both shells. On Windows the model may run a fence's command through its PowerShell tool rather than Bash. A Bash-only grant then prompts, and in a non-interactive run the command is denied.
- Write the pattern the way the command is written, opening quote included. Claude Code fills in `${CLAUDE_SKILL_DIR}` in the pattern as in the command, then compares the command's text, quotes and all, with `*` standing for any text. The same pattern without the quote never matches a quoted command. A skill that runs a sibling's scripts grants `${CLAUDE_SKILL_DIR}/../<skill>/scripts/*` instead.
- A grant is a prefix match on the command's text, not a sandbox: any command that starts with the pattern's text is approved whatever follows, so `scripts/../../other/program.py` and any argument after the prefix match too. The script itself, together with Claude Code's working-directory and protected-path rules, bounds what the command does, so a script that acts on a path the user supplies validates it.
- Keep shell variables and command substitution, such as `"$PWD"` or `$(...)`, out of fence commands: Claude Code asks before it runs a command that expands one, whatever is granted. Have the script default to the current directory instead.
- Grant any other command exactly as the body runs it, such as `Bash(git rev-parse --show-toplevel)`. Leave a command that runs code from the target repository ungranted, so the user approves it.
- List tools other than the shells by name, such as `Read` or `AskUserQuestion`, and only those a step uses. Each grant widens what runs unasked (a `Read` grant covers files outside the working directory, which otherwise prompt), so name its use in the step that needs it, such as "read the `LOG` file" or "every file the glob `docs/**/*.md` matches".

These rules were checked on Windows with Claude Code 2.1.288. That check predates the runtime canary run behind the README's tested version, which did not recheck each rule. Validation fails when a skill grants a shell every command, grants a pattern to one shell only, grants a tool that no step names or implies (a prohibition such as "do not read", or a prompt quoted for a subagent, implies nothing), runs one of its own or a sibling's scripts that a shell's grants do not cover, or expands a shell variable in a fence. It applies the `analyze-skill-cost` inventory (`skill_inventory.py tools`), which reports these as `UNSCOPED_ALLOWED`, `UNPAIRED_ALLOWED`, `UNUSED_ALLOWED`, `UNGRANTED`, and `EXPANDS`.

The runtime adapters leave `allowed-tools` out because Copilot CLI cannot scope a shell grant to a skill's scripts. This was checked with Copilot CLI 1.0.91 on Windows, running a probe skill from a directory whose path has a space, with no shell grant on the command line:

- Copilot accepts the field and reads `Bash(...)` as its own `shell(...)`, but it matches a shell grant against the command's name, such as `python`, not against the command's text. A grant naming the script path, such as `shell(python -B "<skill directory>/scripts/*)`, approved nothing, quoted or not, and neither did the same pattern passed as `--allow-tool`.
- The only grant that let the script run, `shell(python:*)`, also ran a `python -c` command outside the skill.
- A non-interactive `copilot -p` run applied none of a skill's grants, not even `shell(python:*)`.

Carrying the field would approve either nothing or every Python command, so a Copilot user approves each script a skill runs. Revisit this when Copilot matches a shell grant against the command's arguments.

### Paths to a skill's own files

A skill names its own files, and a sibling skill's, through `${CLAUDE_SKILL_DIR}`, never through a rendered install path such as `{{HOME}}/.claude/skills/<name>/`:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/tool.py" report
python -B "${CLAUDE_SKILL_DIR}/../code-review-core/scripts/review_pipeline.py" check --run "<run>"
```

Claude Code replaces `${CLAUDE_SKILL_DIR}` with the absolute directory of the skill it loads. Codex and Copilot read the skill through its adapter, which states the same directory for them (see the adapter paragraph below). Either way the path is absolute when the command runs, so it does not depend on the working directory, and the skill works under any install layout. Keep the quotes: the directory may contain spaces.

- Name a sibling as `${CLAUDE_SKILL_DIR}/../<skill>/` and declare it in `skill_deps`, so it is installed beside the skill.
- Never write a bare `scripts/...` or `../<skill>/...` path in a shell fence; it resolves against whatever directory the session is in.
- In `SKILL.md` prose, name the skill's own files in a code span as `${CLAUDE_SKILL_DIR}/scripts/...` or `${CLAUDE_SKILL_DIR}/references/...`, never bare. Claude Code resolves a bare path against the working directory, costing a failed read and a search, and Codex and Copilot resolve it against the adapter, which has neither folder. Files under `references/` are exempt, because Claude Code fills in the variable only in `SKILL.md`. Validation fails on a bare path in a `SKILL.md` code span outside a fence.
- Claude Code replaces the variable everywhere in the skill, prose included, so do not use it as literal text, for example to explain it.
- Claude Code replaces it only in a skill it loads, so a skill that needs another skill's procedure invokes that skill by name, as `update-pr-tracker` invokes `review-prs`; it never reads the other skill's `SKILL.md` or cites its step numbers or section titles, which break silently when that skill is renumbered or retitled.
- A script that needs the user's Claude directory derives it, as `curate-agent-memory` does from `CLAUDE_CONFIG_DIR` or `~/.claude`, rather than taking a rendered `{{HOME}}`. Keep `{{HOME}}` for documentation that names another location, such as the user's skills root.

Repository validation fails when a skill or agent names any skill through `{{HOME}}/.claude/skills/<skill>`, when a shell fence runs a script or a `SKILL.md` code span names its own file by a bare relative path, when a skill reaches `../<skill>` without declaring it in `skill_deps`, when a script puts another skill's `scripts` directory on `sys.path` without declaring that skill in `skill_deps`, or when a skill reads another skill's `SKILL.md` or names it beside a step number or section title.

### Script results

Every skill script reports the same way, so a `SKILL.md` reads each one alike:

| Exit | Means | Prints |
|---|---|---|
| 0 | The command did its job, including "nothing to do" and a state the skill polls again, such as `RUNNING`. | Its result lines. |
| 1 | Findings the user must act on, such as violations or invalid results, or a named failure. | Its finding lines, or a last line `FAILED <reason>`. |
| 2 | A usage error: arguments `argparse` rejects, including its `type=` checks, before any work starts. | `argparse`'s usage text on stderr. |

- Every line goes to stdout, one fact per line, as `KEY value…`. A failure is a `FAILED <reason>` line on stdout, never on stderr and never through `parser.error`, which is for usage alone. A batch command names each item's failure on its own line, such as `REPOSITORY_FAILED <repository> <reason>`, and exits 1.
- An expected error, such as a missing file, a failed `git` or `gh` call, or a directory that is not a repository, becomes a `FAILED` line, never a traceback.
- A script runs `gh` only through `GitHubClient` in the hidden `skill-core` skill's `github_client.py`, declared and imported like its `console.py`, and declares `gh` in `tools` (or `optional_tools`). Every gh command reads no stdin, shows no prompt, and fails when it runs longer than the client's `timeout` (300 seconds unless the client names another). The client classifies a failure into `GitHubError.kind` (`prerequisite`, `execution`, `timeout`, `authentication`, `forbidden`, `not_found`, `rate_limit`, `search_cap`, `network`, `sso_partial`, `malformed` for JSON it cannot parse, or `api` for anything else), and a caller's parse raises the same error with its own kind, such as `malformed` for an unexpected shape, `incomplete` for results GitHub cut short, `input` for an argument the caller rejects, or `ineligible` for a pull request it will not take. The client waits out a rate limit with one bounded backoff policy unless the call passes `retry=False`, and replaces each byte of output that is not UTF-8 with U+FFFD, counting them in `CommandResult.replaced`. A regression suite injects a runner that returns `CommandResult`, and a sleeper that records the waits.
- A Python script runs `git` only through `GitClient` in the hidden `skill-core` skill's `git_client.py`, declared and imported like its `console.py`. Every git command reads no stdin but the bytes a call gives in full as `input_bytes`, shows no prompt (`GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`), and fails when it runs longer than the client's `timeout` (300 seconds unless the client or the call names another), so an expired credential fails a command instead of waiting on a prompt. `run` returns a `GitResult` with the exit status, stdout, and stderr whatever git's exit, since an exit status is often the answer; `output` returns stdout and raises on a nonzero exit. stdout keeps every byte (`GitResult.output_bytes()` gives them back), and stderr replaces a byte that is not UTF-8 with U+FFFD. `stream` runs a command that answers requests while it runs, such as `cat-file --batch`; there the timeout bounds each wait for output, so a long answer that keeps arriving never times out. A failure raises `GitError` with its class in `kind`: `prerequisite`, `execution`, `timeout`, `not_repository`, or `git` for anything else. A regression suite injects a runner that takes the command and its timeout and returns `GitResult`. Validation fails on a Python script outside `skill-core`, tests aside, that runs `git` or `gh` itself: a list or tuple that begins with either, or a `subprocess` call given either as a string.
- A script prints no JSON. Data the next step reads goes to a file whose path the script prints on one line (see "Working files"). A report the skill shows the user as it is, such as `github-activity-report`'s table, stays a report.
- No other exit codes. A `SKILL.md` keys on the printed lines, not on which nonzero code came back.
- Output is UTF-8. A Windows pipe defaults to a legacy code page that cannot encode every path or title a script prints, so every entry point starts its `if __name__ == "__main__":` block with `use_utf8_output()` from the hidden `skill-core` skill's `console.py`, declares `skill-core` in `skill_deps`, and imports it as "Validation" below shows. Pass `errors="replace"` for output that may hold surrogate-escaped paths, or `newline="\n"` for line endings that must not follow the platform. To test it, run the script with `PYTHONIOENCODING=cp1252` on output that code page cannot encode and compare the UTF-8 bytes it printed, not only its exit code: a traceback exits 1 too.

A script that must speak another protocol states why beside its code as a module-level `EXIT_CONTRACT_EXEMPT = "<reason>"`: `review_guard.py`, for example, answers Claude Code's hook protocol. Repository validation fails when any other script calls `parser.error` while handling an exception, prints `FAILED` on stderr, prints `json.dumps` output, or exits with a literal code other than 0, 1, or 2. It also fails when an entry point's `__main__` block does not start with `use_utf8_output()`, or when a script outside `skill-core` reconfigures a stream's encoding itself.

### Working files

A script that writes a working file, such as a batch, an input, or a plan, chooses the path itself: by default a new directory from `tempfile.mkdtemp` with a prefix naming the skill, such as `review-prs-batch-`. It prints the path on one line, such as `BATCH <file>`, and the next step reads that line. An option for an explicit path may stay, but the script refuses, with a `FAILED` line and exit code 1 before writing anything, any path inside the skills directory it runs from, `~/.claude/skills`, or `~/.agents/skills`.

Never leave the path to the agent. Given a placeholder such as `--output "<file>"` and a skill directory it already knows, an agent writes beside `SKILL.md`, and the deployer then sees the installed skill as modified and skips it on every later update. Repository validation fails when a command fence in a skill passes a `<...>` placeholder to `--output`, `--output-<name>`, `--out`, `--out-dir`, or `--plans`. A placeholder for a path an earlier command printed, such as `--run "<run directory>"` or `--input "<input file>"`, is fine.

### Metadata

`deploy-meta/<name>.json` declares the variables and shared files needed by the skill:

```json
{
    "required_vars": ["REPOS_ROOT"],
    "shared_deps": ["runtime-compatibility.md"]
}
```

Metadata may also declare same-source skill dependencies and selection visibility:

```json
{
    "required_vars": [],
    "shared_deps": ["runtime-compatibility.md"],
    "skill_deps": ["code-review-core"],
    "selectable": true
}
```

`selectable` defaults to `true`. Set it to `false` only for an internal support skill that is reachable from a selectable skill. Dependencies must exist in this source and form an acyclic graph. The deployer expands the complete transitive dependency closure before rendering or mutation.

Every skill declares `runtime_support`, which runtimes run it, one value per runtime (`claude-code`, `codex`, and `copilot-cli`): `"full"`, or an object whose `level` is `partial` or `none`, whose `needs` names the capabilities the runtime lacks for it, and whose `reason` says in one line what the user loses:

```json
"runtime_support": {"claude-code": "full", "codex": "full", "copilot-cli": {"level": "partial", "needs": ["user-only-start"], "reason": "A headless copilot -p session cannot start it; start it from an interactive session."}}
```

The capabilities, and which runtimes offer them, are catalogued in `deployer/runtime_support.py`: `agent-delegation` (starting a subagent; Claude Code and Codex), `workflow` (Claude Code's Workflow tool), and `user-only-start` (starting a user-only skill from a headless session as well as an interactive one; Claude Code and Codex). The value follows from what the skill needs, and validation derives that from its frontmatter: an `Agent` grant in `allowed-tools` needs `agent-delegation`, a `Workflow` grant needs `workflow`, `disable-model-invocation: true` needs `user-only-start`, and a skill another skill's `SKILL.md` says to invoke passes on what it does, though not how it starts. A runtime that offers every need is `full`; one that lacks any is `partial` or `none` and names exactly what it lacks. A hidden skill is `none` on every runtime, with the reason that no runtime starts it. The deployer refuses a malformed declaration, an unknown runtime or capability, and a `partial` or `none` without a reason. `tools/new_skill.py` writes the value that follows from the options it is given, with a default reason for each partial to sharpen. `tools/skill_reference.py` prints the declarations as "Runtime support" in `docs/skills.md`, and the runtime canary compares each run with them.

Every metadata file has one layout, the one `tools/new_skill.py` writes: one key per line, indented four spaces, with each value on its key's line. Repository validation fails on any other layout.

### Commands skills may run

Skills run on machines where installing extra utilities may be inconvenient or not allowed, so a skill may run only:

- Python and its standard library. Call it as `python`; `python3` is often a Microsoft Store placeholder on Windows.
- Git for Windows: `git`, Bash, and the utilities it bundles, such as `grep`, `sed`, `awk`, `find`, `tr`, `xargs`, and `curl`.
- PowerShell.
- A tool from the catalogue in `deployer/tools.py`, declared in the skill's metadata as described below.

`STANDARD_COMMANDS` in `deployer/tools.py` lists the first three groups exactly; the Windows-specific names among them (`python`, `powershell`, and `cygpath`) come from `deployer/platform_support.py`. Repository validation fails when a skill's scripts or Bash examples run anything else. Reach for these substitutes instead:

| Instead of | Use |
|---|---|
| `jq` | Python's `json` module, or PowerShell's `ConvertFrom-Json`. A script parses `gh --json` and `gh api` output with the `json` module, adding `--paginate --slurp` to a paginated `gh api` call, and validates its shape. `gh --jq` is for a one-line command in prose, never a script; validation fails when a skill script passes `--jq`, `-q`, or `--template` to `gh` |
| `rg` | `grep -E` in Bash, Python's `re` module, or the agent's built-in search tool |
| `yq` | JSON instead of YAML; Python's standard library cannot read YAML |
| `curl` or `wget` against the GitHub API | `gh api`, which handles authentication |
| `xmllint` | Python's `xml.etree.ElementTree` |
| `python3` | `python` |

If a skill truly needs another tool, add it to `SKILL_TOOLS` in `deployer/tools.py` and declare it, so that `check` reports it and deployment warns when it is missing.

Declare the external tools a skill runs, beyond Git and Python, which every deployment already needs:

```json
{
    "tools": ["dotnet-format"],
    "optional_tools": ["gh"]
}
```

`tools` lists the tools the skill cannot work without. `optional_tools` lists those it uses only when they are installed, such as dotnet-format reading a pull request's base branch through `gh` before falling back to the remote's default branch; a tool may not be in both. The known tools are listed in `SKILL_TOOLS` in `deployer/tools.py`: currently `gh`, `copilot` (optional for every skill), and `dotnet-format`. To add one, add it there with how to find it and its minimum version, if any. `python deploy.py check` reports each declared tool and which skills use it, as optional when every skill using it declares it optional, and deploying warns when a selected skill requires a tool that is not installed. [`docs/skills.md`](skills.md) lists each skill's own declared tools.

A skill runs what its own scripts and Bash examples run, and what the scripts of its `skill_deps` that it reaches run: a dependency script is reached when the skill's own files import it or name its path (`${CLAUDE_SKILL_DIR}/../<dependency>/scripts/<script>.py`), and so is every dependency script a reached one imports. A tool a reached script runs is the skill's, and the skill declares it too. A dependency's other tools are not the skill's: `flag-review-finding` depends on `code-review-core` but reaches none of its `gh` calls, so it declares no tools. Repository validation fails when a skill's own scripts or Bash examples run a known tool it does not declare, when a dependency script it reaches runs one it does not declare, when it declares a tool that nothing it runs or reaches runs, or when it runs a command that is neither standard nor declared.

Declare an atomic selection bundle in `source.json`:

```json
{
  "bundles": {
    "code-review-operations": {
      "members": ["review-prs", "update-pr-tracker"]
    }
  }
}
```

A selectable skill may belong to at most one bundle. Bundle members are hidden as individual prompts, and `--all` selects the bundle as one user-intent root. The manifest records requested bundles/root skills separately from the expanded concrete installation.

### Opt-in skills and bundles

Mark a skill or bundle opt-in when only some users need it, such as one for a single programming language. Set `"opt_in": true` in the skill's `deploy-meta/<name>.json`, or in the bundle's definition in `source.json`; for a skill inside a bundle, mark the bundle instead. Only menu items can be opt-in, so a hidden dependency cannot be.

`--all` skips opt-in items unless they are already installed, so an existing installation keeps them. Users add one with `--all --include <name>` or by choosing it from the menu, which labels it `(opt-in)`. `python deploy.py check` reports the tools of an opt-in item that is not installed as optional.

Every selected public skill also receives a generated thin adapter under `~/.agents/skills/<name>`. The adapter points to the authoritative rendered skill under `~/.claude/skills/<name>` and contains no copied workflow implementation. It also states that `${CLAUDE_SKILL_DIR}` in the skill stands for that directory, because those runtimes do not replace the variable and their own base directory is the adapter's, which has no `scripts/`. It carries the skill's rendered description, which Codex and Copilot match requests against, but not its `allowed-tools`. For a skill with `disable-model-invocation: true`, it instead keeps a description that names only the skill and sets each runtime's own explicit-only switch: `disable-model-invocation: true` for Copilot and `policy.allow_implicit_invocation: false` in `agents/openai.yaml` for Codex. When the skill declares a shared Markdown asset in `shared_deps`, such as `runtime-compatibility.md`, the adapter first tells the runtime to read and apply it. Hidden dependencies do not receive adapters. Adapter ownership, conflicts, retained force backups, removal, and recovery are part of the same deployment transaction and manifest. The deployment report lists an adapter as `<name> (runtime adapter, ...)` only when it needs attention or does something its skill does not; the manifest records adapters under the historical key `wrappers`. The `.agents/skills/` copies of this repository's own skills are hand-maintained shims, not adapters.

Supported derived variables are `HOME` and `SOURCE_ROOT`, the directory containing the `deploy.py` that rendered the skill. The only configured variable is `REPOS_ROOT`. Derived values pass an allowlist, as configured ones do: letters of any script, digits, spaces, and `/ : . @ _ ( ) -`. A deployment whose home folder or checkout path has another character stops before any change and names the character.

Every token a skill uses must appear in its `required_vars`, and every entry in `required_vars` must be used. Shared assets may use only derived variables. A configured variable that no skill requires, or a derived variable that no skill or shared asset uses, is rejected by validation, so remove it from `deployer/config.py` together with the last skill that needs it. To add one, declare its character allowlist in `CONFIGURED_VARIABLES` and its prompt in `PROMPTS` in `deployer/config.py`, then require it from the skill that uses it.

## Template values

Use a declared variable as an uppercase token:

```text
{{REPOS_ROOT}}
```

Every token used by a selected skill must be available during deployment. The deployer rejects unresolved tokens after rendering.

Substitution depends on where the token appears. In `.json` and `.toml` files, and in `json` fenced blocks in Markdown, the value is escaped as string content and the rendered file must still parse. In Bash, PowerShell, Python, and YAML files, in the matching fenced blocks, and in a Markdown file's frontmatter, a value containing quote, expansion, or escape characters is rejected rather than substituted. Markdown prose and `.txt` files receive the value unchanged. A fence is read as CommonMark reads it: three or more backticks or tildes open it, and only the same character, at least as many times, closes it, so a fence of four backticks can show an example fenced with three. The first word of its info string sets its context. The deployer, the ShellCheck extraction, and every validation rule that reads Markdown fences use the one detector in `deployer/render.py`. Keep tokens inside quoted strings in executable content, and add an execution fixture whenever a token appears in Bash or PowerShell: a test in `tests/deployer/` whose name contains `with_spaces`. It carries the token in a string literal, binds a value containing spaces, runs the rendered result with that language's shell (`find_bash`, or `find_pwsh`) through `run_tool`, and asserts the run's output against that value. Validation fails when a token in a skill's Bash or PowerShell sits outside quotes, or when no such test covers that token in that language. A token named only in a comment, a run whose output is never compared with the spaced value, or a test that never runs the result covers nothing.

## Shared assets

Place an owned shared file directly under `skills/` and declare it in `source.json`:

```json
{
  "shared_assets": {
    "runtime-compatibility.md": "owner"
  }
}
```

A shared Markdown asset is runtime guidance for Codex and Copilot. Declare it in the skill's `shared_deps`, but do not mention it in `SKILL.md`: Claude runs the skill directly and would spend a read on it every run, and the generated adapter already points other runtimes to it. Validation rejects a `SKILL.md` that names one, so keep anything Claude also needs, such as a rule about posting to GitHub, in the skill itself.

`shared_assets` must be an object, and each role a string, `owner` or `dependency`; source discovery stops with an error naming the asset otherwise. Use the `dependency` role only when another installed source owns the asset. The deployer verifies the owning source and manifest hash before installing a skill that needs the dependency.

A shared asset's name is one file name of letters, digits, dots, hyphens, underscores, and spaces. The deployer compares names as the file system does, so on Windows `Guide.md` and `guide.md` name one file: an asset may not share its name, in any case, with a skill or another asset of its source, or with an item another source has deployed. A name ending in `.deploying-bak` or containing `.tmp.` is reserved for the deployer's own files.

The manifest records each installed skill's `shared_deps`, and those records move with the skill during `--migrate-from`. A shared asset is installed only while a selected skill needs it. When no selected skill needs an owned asset, the deployer removes it unless a skill that stays installed still needs it: a locally modified skill that was preserved or skipped, or a skill owned by another source. In that case the asset is kept and reported as `KEEP` until nothing installed needs it.

## Subagent definitions

A Claude Code subagent a skill starts can ship with it. Put its definition in `agents/<name>.md`, a Markdown file whose frontmatter `name` matches the file name, and list it in the skill's `deploy-meta/<name>.json`:

```json
{
  "agent_deps": ["code-review-reviewer"]
}
```

The deployer installs each agent a selected skill declares to `~/.claude/agents/<name>.md`, byte for byte. Agent files take no template values, because their frontmatter (`tools`, `model`, `effort`) is executable configuration. An agent is owned in the manifest like a skill: an unmanaged file that differs is skipped unless forced, with its backup kept, a locally modified one is preserved, and installs, removals, and recovery share the run's journal. An agent is removed when no selected skill still needs it. `agents/` holds only `<name>.md` files, and executable logic stays in a skill's `scripts/`. A hook that runs a skill's script cannot use `${CLAUDE_SKILL_DIR}` or `$HOME`: Claude Code runs hooks in Git Bash, whose `$HOME` follows `HOME` rather than the profile folder the deployer installs into. Have Python find the script instead, in isolated mode so the session's working directory is not on its import path, as `code-review-reviewer` does: `python -I -B -c "import os, runpy; runpy.run_path(os.path.expanduser('~/.claude/skills/<skill>/scripts/<script>.py'), run_name='__main__')"`. Validation fails when an agent names `$HOME`. Agents are Claude-only: Codex and Copilot follow the skill's instructions with their own subagents, so a skill that names an agent must say what to use when the agent type is not available, as happens in a Claude session started before the deployment.

Manifest version 7 records agents. A version 6 manifest is read and saved as version 7, which an older deployer refuses rather than rewriting a source entry without its agents.

## Reference documentation

Every selectable skill has a section in [Skills](skills.md), the reference users read to start it:

1. If the skill takes arguments, declare them as `argument-hint` in its frontmatter, using the notation that page explains. A skill that reads `$ARGUMENTS` without one fails validation.
2. Run `python tools/skill_reference.py --write`. It regenerates the summary table and adds the skill's section, whose first block it fills from the frontmatter and metadata.
3. Below that block, write what each argument means, what the skill does with none, and an example invocation. For a skill without arguments, say what it acts on instead.
4. Add a row for the skill, or its bundle, to the "Included skills" table in the README, linking its section here.

Run `python tools/skill_reference.py` to see what is still missing. Validation fails while the reference is stale, a section has no hand-written part, or the README table leaves a skill out, keeps a row for a removed one, or links a section that does not exist.

## Validation

Before adding the skill to a release:

1. Add positive rendering coverage to the appropriate `tests/deployer/test_*.py` module, creating a focused module only when necessary. `tests/run_validation.py` discovers and runs every module in parallel.
2. If the skill contains executable files, include an executable regression suite under its `scripts/` directory. Name each test entry point `test_*`, `test-*`, `*_test`, `*-test`, or `*.test.*` and use `.py`, `.sh`, or `.ps1`. `tests/run_validation.py` discovers and runs these suites automatically, under `tests/` and each skill's `scripts/`, by name alone. A skill is `skills/<name>` or `skills/<category>/<name>` holding `SKILL.md`, as the deployer reads them, or a repository skill under `.claude/skills/`: a file whose name misses the patterns never runs. It runs each suite as a program, so end a Python suite with `if __name__ == "__main__":` calling `unittest.main()`; without it the suite runs no tests and still exits 0, and validation fails.
3. Python under `scripts/` passes the same format and lint checks as the rest of the repository, `ruff format --check` and `ruff check` with the rules in `pyproject.toml`, and no file or rule is exempt. A script that imports a sibling's scripts, as `skill_deps` allows, puts `${CLAUDE_SKILL_DIR}/../<dependency>/scripts` on `sys.path` first. Write that as one bare statement directly above the imports it serves, `sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "<dependency>" / "scripts"))`, and define any path constant after those imports: ruff's E402 accepts an import after a `sys.path` change but not after an assignment, so the sibling-import pattern needs no exemption. The rule set includes function complexity (C901) and length in statements (PLR0915), capped at the `max-complexity` and `max-statements` ceilings in `pyproject.toml`. The ceilings are a ratchet (#91): they only go down, `tests/run_validation.py` fails a change that raises one, and neither rule takes a `noqa`, so split a function that exceeds them into helpers of one concern. The bandit security rules (`S`) are selected except S603 and S607, which flag every subprocess call made without a shell and every program started by its PATH name: skills run declared tools as argument lists found on PATH, which the command checks under "Commands skills may run" hold instead, so those two describe the design and are left unselected rather than suppressed on every call (#90). The same Python is type-checked: validation runs mypy, with the `[tool.mypy]` configuration in `pyproject.toml`, on each skill's `scripts/` directory from inside it, and fails on any error; a `# type: ignore[<code>]` that must stay states its reason in a comment after it. mypy resolves a `skill_deps` sibling's modules through `mypy_path`, which lists each `skills/<name>/scripts` directory a module or regression suite puts on `sys.path`. When a skill first imports a sibling's scripts, add `$MYPY_CONFIG_FILE_DIR/skills/<dependency>/scripts` there; validation fails until the list matches the `sys.path` changes. Regression suites are type-checked too, so a fixture that is malformed on purpose is typed as the loose shape it is, never silenced. PowerShell passes `Invoke-ScriptAnalyzer -Severity Warning,Error`: every `.ps1` under `scripts/` and every `powershell`, `pwsh`, or `ps1` fence in the skill's Markdown, as written with its tokens quoted. No rule is suppressed, by `SuppressMessageAttribute` or a settings file; fix the cause.
4. Validation also holds these rules across the repository. Each lives in a policy module under `tests/validation/`, with a fixture suite beside it, `tests/validation/test_<module>.py`, that shows a violating input failing and a conforming one passing. A new rule goes in its family's module with its fixture test.
   - Keep every directory under `skills/` a skill or a category of skills. A directory holds `SKILL.md`, or is a category whose every subdirectory does, and no `SKILL.md` sits deeper than `skills/<category>/<name>`. Anything else is a folder whose files and suites nothing would find.
   - Do not copy a definition between files. Validation fails when a top-level function or class of four or more lines, docstrings aside, is defined identically in two non-test Python files under `skills/`, `deployer/`, or `tools/`, and names every copy; two scripts of one skill count, because one can import the other. Import one copy instead. A copy that must stay, such as a deployer function a deployed skill cannot import, is sanctioned beside the code in every module that holds it, as a module-level `DUPLICATION_ALLOWED = {name: reason}` whose reason names the change that removes it; nothing sanctions a whole module. An entry whose definition is no longer copied fails, so the list cannot go stale.
   - Shared code lives in the hidden `skill-core` skill, and everything in the repository imports it from `skills/skill-core/scripts`: skill scripts through `skill_deps`, the deployer through the one `sys.path` statement in `deployer/__init__.py`, which locates the directory from its own file, never through the configured source path, and each `tools/` module through the same statement written above its skill-core imports. Code that a skill and the deployer or `tools/` both need moves there instead of being copied. Validation fails on a module under `deployer/`, `tools/`, or `skills/` outside skill-core that has a skill-core module's name, on a deployer or `tools/` import of a skill-core module with no such statement before it, on a skill-core module that imports from the deployer or `tools/`, and on a deployer or skill-core module that imports anything but the standard library, the deployer, and skill-core.
   - Keep relative Markdown links working. Every relative link in a `.md` file Git does not ignore, outside `.claude/worktrees/`, must resolve to a file or folder, and a `#fragment` to a heading of its target by GitHub's rule: lower-case, backticks and punctuation other than hyphens and underscores removed, each space turned into a hyphen, and a repeated heading numbered `-1`, `-2`. Links inside code, and links with a scheme such as `https:`, are not checked.
   - Import every module in a test. Each non-test Python module under a shipped or repository skill's `scripts/`, `deployer/`, or `tools/`, other than an `__init__.py`, must be imported by at least one `test_*.py`: by an `import` statement, or through `importlib`, by `import_module` or by `spec_from_file_location` from its path. Mentioning it, running it as a program, or naming a `test_<module>.py` after it does not count.
   - Hold repository skills to the shipped standard. A repository skill under `.claude/skills/`, which loads into every session here as a shipped skill does, passes the same policies, applied to that tree by the same functions. Its `scripts/`, if it has one, is found, laid out, formatted, linted, and type-checked as a shipped skill's is, and its suites run with the rest. Its Markdown is held to these rules: no fence of more than five lines, no output path left to the agent ("Working files"), scoped twin shell grants ("Granting tools") that cover each repository tool or test it runs, such as `python -B tools/runtime_canary.py`, and a description a runtime adapter accepts that says when to use it ("Description"). Its `.agents/skills` shim carries the same frontmatter. A change to one runs `analyze-skill-cost` on it before the pull request, as a shipped skill's does.
5. Add failure cases for any new parsing, path, or execution behavior.
6. Run the repository validation sequence:

   ```powershell
   python -B tests/run_validation.py
   ```

7. If debugging a failure, run the affected Bash, ShellCheck, PSScriptAnalyzer, or Python command directly.
8. Deploy into an isolated temporary home with `deploy.py --canary-home` and verify configuration, manifest entries, backups, and rendered output. Setting `HOME` does not isolate a run: on Windows the deployer always uses the profile folder.
9. When the change touches how the skill names its own files, its `allowed-tools`, its runtime adapter, or its agents, run the `runtime-canary` repository skill. `tools/runtime_canary.py` deploys into a throwaway home with `deploy.py --canary-home`, starts Claude Code, Codex, and Copilot CLI there, and reports whether each found the skill and ran its script from the deployed copy. Each run calls a model, so it never runs inside validation; record its result in the pull request.
