# Skills

Every skill this repository deploys: how to start it, what its arguments mean, and what it needs installed.

## Starting a skill

In Claude Code, type `/<skill>` followed by its arguments, or ask for the skill by name. A skill started by "you or the agent" also runs when a request matches its description. One started only by "you" changes things you should decide on, such as deleting branches or redeploying, so it runs only when you type it.

Codex and GitHub Copilot CLI load each skill through a generated adapter under `~/.agents/skills` that carries the skill's description, so a skill started by "you or the agent" also runs there when a request matches it. Start one explicitly with `$<skill>` in Codex or `/<skill>` in Copilot, followed by its arguments. A skill started only by "you" runs only when you start it that way.

When a required argument is missing, a skill asks for it rather than guessing.

## Reading the argument syntax

| Notation | Meaning |
|---|---|
| `[--force]` | Optional |
| `a \| b` | One of the alternatives |
| `owner/repo ...` | Repeatable |
| `ORG`, `<SkillName>` | A value you supply |
| `owner/repo#number` | One pull request, such as `octo-org/widgets#42` |

## Summary

**Installed** says how `python deploy.py` selects the skill: `--all` installs every skill marked "by default"; an opt-in skill needs `--include <name>` or a menu choice; a bundle installs its skills together. **Needs** lists what the skill runs beyond the deployment prerequisites in [Installation](installation.md#requirements), and any setting it reads from `python deploy.py configure`. `python deploy.py check` reports which of those tools are installed.

<!-- generated:summary -->
| Skill | Started by | Installed | Needs |
|---|---|---|---|
| [`analyze-skill-cost`](#analyze-skill-cost) | You or the agent | By default | Nothing extra |
| [`audit-ai-config`](#audit-ai-config) | You or the agent | By default | Nothing extra |
| [`curate-agent-memory`](#curate-agent-memory) | You or the agent | By default | Nothing extra |
| [`dotnet-dependencies`](#dotnet-dependencies) | You or the agent | Opt-in, `dotnet` bundle | `dotnet` (optional) |
| [`dotnet-format`](#dotnet-format) | You or the agent | Opt-in, `dotnet` bundle | `dotnet` (optional), `dotnet-format`, `gh` (optional) |
| [`flag-review-finding`](#flag-review-finding) | You or the agent | `code-review-operations` bundle | Nothing extra |
| [`github-activity-report`](#github-activity-report) | You or the agent | By default | `gh` |
| [`repo-cleanup`](#repo-cleanup) | You | By default | `gh`, the `REPOS_ROOT` setting |
| [`review-document`](#review-document) | You or the agent | `code-review-operations` bundle | `copilot` (optional), `gh` (optional), `pdftotext` (optional) |
| [`review-insights`](#review-insights) | You or the agent | `code-review-operations` bundle | Nothing extra |
| [`review-prs`](#review-prs) | You or the agent | `code-review-operations` bundle | `copilot` (optional), `gh`, `pdftotext` (optional) |
| [`update-coding-agent-skills`](#update-coding-agent-skills) | You | By default | Nothing extra |
| [`update-pr-dashboard`](#update-pr-dashboard) | You or the agent | `code-review-operations` bundle | `gh` |
<!-- /generated:summary -->

The summary and the first block of each section below are generated from each skill's `SKILL.md` frontmatter and `deploy-meta/<name>.json`. Change those, then run `python tools/skill_reference.py --write`; validation fails while this file is stale. The explanation after each generated block is written by hand.

## Runtime support

Which runtimes run each skill. **Full** means every step runs there as it does in Claude Code. **Partial** means the skill runs but loses what the note under the table names, because the runtime lacks a capability the skill uses: `agent-delegation` (starting a subagent), `workflow` (Claude Code's Workflow tool), or `user-only-start` (starting a user-only skill from a headless `copilot -p` session as well as an interactive one). **None** means the runtime does not run it. Each skill declares its support in `deploy-meta/<name>.json`; validation holds the declaration to the capabilities its frontmatter shows it uses, and the [runtime canary](../tools/runtime_canary.py) fails when a runtime ran a skill otherwise than the table says.

<!-- generated:runtime-support -->
| Skill | Claude Code | Codex CLI | Copilot CLI |
|---|---|---|---|
| [`analyze-skill-cost`](#analyze-skill-cost) | Full | Full | Full |
| [`audit-ai-config`](#audit-ai-config) | Full | Full | Full |
| [`curate-agent-memory`](#curate-agent-memory) | Full | Full | Full |
| [`dotnet-dependencies`](#dotnet-dependencies) | Full | Full | Full |
| [`dotnet-format`](#dotnet-format) | Full | Full | Full |
| [`flag-review-finding`](#flag-review-finding) | Full | Full | Full |
| [`github-activity-report`](#github-activity-report) | Full | Full | Full |
| [`repo-cleanup`](#repo-cleanup) | Full | Full | Partial |
| [`review-document`](#review-document) | Full | Full | Partial |
| [`review-insights`](#review-insights) | Full | Full | Partial |
| [`review-prs`](#review-prs) | Full | Partial | Partial |
| [`update-coding-agent-skills`](#update-coding-agent-skills) | Full | Full | Partial |
| [`update-pr-dashboard`](#update-pr-dashboard) | Full | Partial | Partial |

- `repo-cleanup` on Copilot CLI: partial, lacking `user-only-start`. A headless copilot -p session cannot start it; start it from an interactive session.
- `review-document` on Copilot CLI: partial, lacking `agent-delegation`. The reviewer role runs inline in the session, without the reviewer agent's guard.
- `review-insights` on Copilot CLI: partial, lacking `agent-delegation`. The synthesis runs inline in the session instead of a subagent, so its whole input loads into the session's context.
- `review-prs` on Codex CLI: partial, lacking `workflow`. Reviewers start as native subagents on the session's model, so a reviewer model or effort setting has no effect.
- `review-prs` on Copilot CLI: partial, lacking `agent-delegation` and `workflow`. The generic reviewer and delegation-free specialists run inline; a specialists manifest that keeps agent-delegation fails.
- `update-coding-agent-skills` on Copilot CLI: partial, lacking `user-only-start`. A headless copilot -p session cannot start it; start it from an interactive session.
- `update-pr-dashboard` on Codex CLI: partial, lacking `workflow`. The reviews it starts run through review-prs, which loses reviewer effort settings here.
- `update-pr-dashboard` on Copilot CLI: partial, lacking `agent-delegation` and `workflow`. The reviews it starts run through review-prs, which here runs the generic reviewer and delegation-free specialists inline and fails a specialists manifest that keeps agent-delegation.
<!-- /generated:runtime-support -->

## `analyze-skill-cost`

<!-- generated:analyze-skill-cost -->
Audit an agent skill for cost and efficiency: token footprint, tool calls, deterministic work left to prose, subagent overhead, runtime adapters, model, and allowed-tools. Read-only findings report. Use it when asked what a skill costs, why it is slow or expensive, or to check one before shipping it.

```text
/analyze-skill-cost <SkillName>
```

Started by you or the agent. Installed by default.
<!-- /generated:analyze-skill-cost -->

`<SkillName>` is a skill's name, not a path. The skill looks for it under `~/.claude/skills/` and, when you start it inside a Git repository, in that repository's `.agents/skills/` and `.claude/skills/`. In a checkout of this repository it also looks in `skills/` for a skill with a `deploy-meta/<name>.json`, and audits that source rather than the deployed copy under `~/.claude/skills/`, so a change is audited before it is deployed. If no skill or more than one has that name, it lists what it found and stops. It takes no flags.

It judges a skill's text by content, not size: duplicated code blocks, prose that restates another step, and documentation the agent never acts on are findings at any length. Sizes and token estimates are reported as data, never as a threshold. A Markdown file the skill loads, its own or one it reads from outside its folder, that is not UTF-8 text stops the audit with the file's path rather than counting as free. The only structure limits it checks are the ones in Anthropic's [skill authoring best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices): a body under 500 lines, a table of contents in a reference file over 100 lines, and references linked directly from `SKILL.md`. Each is a suggestion. When a skill's subagent reads a prompt that a script writes at runtime, as `review-prs`, `review-document`, and `review-insights` do, the audit measures the rendered sample this repository keeps under `docs/runtime-prompts/<skill>/`: its size, the skill files it has the subagent read, and whether it bounds the reply. `python tools/runtime_prompts.py --write` renders the samples from fixtures with no model, and validation fails while one is stale; a skill audited outside a source tree, or with no sample, gets a suggestion to audit that prompt by hand. It never suggests a cheaper `model` for a skill usually followed in the same turn by work that starts subagents, because those subagents inherit the skill's model: a code review started in the same turn as a Haiku-pinned skill ran every reviewer on Haiku, and they missed a must-fix finding.

```text
/analyze-skill-cost repo-cleanup
```

## `audit-ai-config`

<!-- generated:audit-ai-config -->
Read-only assessment of a repository's AI agent configuration for Claude Code, Codex, and Copilot that reports findings and never writes files. Use it when asked to audit or check a repository's agent instructions or setup, not to create or change them.

```text
/audit-ai-config
```

Started by you or the agent. Installed by default. Takes no arguments.
<!-- /generated:audit-ai-config -->

It audits the Git repository you start it in, and asks which repository to audit when you are not in one. It reports the repository's authority and scope, every error and warning, and every documented limitation that applies, and summarizes the informational inventory as a count per check; ask for the full inventory when you want each file it found. Ask for JSON when you want to process the findings rather than read them. It parses files statically and writes nothing. It runs nothing from the repository and makes no network requests unless you authorize its optional operational validation of a trusted repository, which runs the repository's generator in check mode and starts each of its configured stdio MCP servers for a handshake. The skill pre-approves neither command, so Claude Code asks you before each one runs. It stays inside the repository: a symbolic link or junction that leads out of it is reported as a warning and never followed, and a server whose working directory or relative command lies outside it is refused, not started.

It never fixes what it finds; findings are fixed by hand. This source used to ship `init-ai-config`, which generated a cross-runtime layout from `CLAUDE.md`. That skill was retired in `v0.2.0`, because every supported runtime reads `CLAUDE.md` directly or through a thin `AGENTS.md` redirect. The audit still checks the generated layout in repositories the generator configured before, and its `references/generated-layout.md` describes that layout.

## `curate-agent-memory`

<!-- generated:curate-agent-memory -->
Audit and clean up Claude Code auto-memory for a project: find stale, duplicated, or misplaced memories, move durable rules to their proper home, and apply only the changes the user approves. Use it when asked to review, tidy, or prune a project's memories.

```text
/curate-agent-memory [repository path] [--memory-dir DIR]
```

Started by you or the agent. Installed by default.
<!-- /generated:curate-agent-memory -->

- `repository path`: the repository whose memory to curate. It defaults to the current Git repository's root. Every worktree of a repository shares one memory store, so any of them gives the same result.
- `--memory-dir DIR`: the memory directory to use. Without it, the skill finds the directory Claude Code uses, from settings first, and lists candidates for you to choose from when it finds none.

Either way it shows the directory and asks you to confirm before reading further, and it changes nothing you have not approved.

The audit and index rebuild compare link targets by their resolved paths relative to the memory store: `./rule.md` and `rule.md`, or `RULE.md` on Windows, identify the same memory. Missing-file and duplicate reports keep the original target text, and rebuilding keeps the first entry's target spelling.

Both read balanced parentheses and Markdown backslash escapes in link targets: `decision(cache).md` and `decision\(cache\).md` identify the same file. Rebuilding keeps an existing entry's target spelling and title, and its hook when the memory has no description; newly added targets escape parentheses and backslashes so later audits and rebuilds resolve the same file. An incomplete link stays as a non-entry line rather than being read as a truncated target.

Both exclude the index from memory enumeration by the same resolved identity: on Windows, a store's `memory.md` or `Memory.md` is the `MEMORY.md` index, not a memory to audit or add to itself.

It looks for the directory in Claude Code's documented order: `autoMemoryDirectory` from managed, local, project, then user settings (a project's own settings count once the folder is trusted, a directory a repository's settings choose does not count while `permissions.blockReadsOutsideWorkingDirectories` is on in any scope, because Claude Code then loads no memory from it, and a `--settings` file is beyond what it can see); then `CLAUDE_CODE_PROJECT_DIR_NAME` under the configuration directory; then a directory derived from the top level of the repository's main worktree, which a linked worktree shares and which a submodule or a repository with a separate Git directory has of its own. A repository path names any folder in the repository, and when git cannot read the repository the skill stops with git's reason. It compares each memory with the repository's `CLAUDE.md`, `CLAUDE.local.md`, `.claude/rules/`, `AGENTS.md`, README, contributor and `docs/` guidance, Copilot instructions, and skill files, plus your own `CLAUDE.md` and `rules/`. When it rebuilds `MEMORY.md`, it writes one `- [Title](file.md) — hook` line per memory file and never deletes or changes a memory: entries keep their order, title, and surrounding headings, entries for missing files or repeated links are dropped, and unindexed memories are appended. The hook is the memory's `description`, all of a `>` or `|` block scalar's lines, else the entry's existing hook, else the body's first line; a description or the body's line is collapsed to one line and cut to 150 characters, because Claude Code loads `MEMORY.md` into every session and stops at its load limit. It applies the approved changes in order: destination files first, then it deletes the memories they cover through its own script, then it rebuilds `MEMORY.md`. The script audits, deletes from, and rebuilds the index of only a memory store: a directory holding `MEMORY.md`, outside every skills directory; when the directory it finds is not one, it says so before anything changes and lists the others. A directory, symbolic link, or junction named like a memory is reported as unreadable and is never read, indexed, or deleted, and the audit writes its report to a new file, never over an existing one. It deletes only `.md` files named directly inside that directory; it refuses `MEMORY.md`, any path with a separator, drive, or `..`, a directory, and a symbolic link or junction, and when it refuses any name it deletes nothing. Every value it prints from a settings file, a memory's file name, or a memory stays on one line: a path holding a line break, which no line can name, is refused rather than printed, so the script does not delete or rebuild the index of a memory whose file name holds one. A rule moved into a skill goes into the skill's source, never its deployed copy, which the deployer would then stop updating: the script names the source, the `skills/<name>/SKILL.md` of the checkout the deployer's manifest records for that skill or a project skill in the repository, and refuses any file inside a skills directory; with no source, the memory stays unless you name the source file. The skill edits destination files and memories with `Edit` and is granted no `Write`, so creating a new destination file asks you first.

```text
/curate-agent-memory
/curate-agent-memory C:\GitHub\widgets
```

## `dotnet-dependencies`

<!-- generated:dotnet-dependencies -->
Answer why a package or assembly is in a legacy or SDK-style C# solution, which projects reference an assembly, and where versions and binding redirects conflict, from a read-only dependency graph of the solution's project files. Use it when asked why a package is referenced, what pulls a dependency in, which projects use an assembly, or whether versions or binding redirects disagree, and not to restore, build, or change references.

```text
/dotnet-dependencies <why <name>...|where <assembly>|conflicts|graph> [--project <path>]
```

Started by you or the agent. Opt-in with the `dotnet` bundle: deploy it with `--include dotnet`. Needs `dotnet` (optional).
<!-- /generated:dotnet-dependencies -->

Start it in the directory holding the solution (`.sln` or `.slnx`), or in a directory of `.csproj` files when there is no solution; `--root <path>` names another solution or directory, and `--project <path>` narrows any query to one project. It reads project files only: `.csproj` and the files a project imports, such as `Directory.Build.props` (old-style `Reference` elements with their `HintPath` and version, `ProjectReference`, and SDK-style `PackageReference` with a `Version` or a central `Directory.Packages.props` version, and a central `GlobalPackageReference`, where an `Update` item changes the version of a reference and never adds one), `packages.config`, each package's `.nuspec` in the `packages` folder or the NuGet cache, a restore's `obj/project.assets.json`, and the binding redirects in `web.config` and `app.config`. It runs no restore or build and writes nothing but the file `graph` names. C# projects only; other project types are not read.

The first word is the query:

- `why <name> [<name> ...]` prints every chain from each project to each name, a package id or an assembly name (one a package ships, under any name, or one a `Reference` names), with the version at each hop. For a project that is SDK-style, uses `PackageReference`, and has a restore no older than its project file, the package chains come from `dotnet nuget why` when `dotnet` is installed, marked `sdk`, run with `DOTNET_CLI_UI_LANGUAGE` set to English so its answer reads alike whatever the machine's display language; the files are walked for every project too, marked `graph`, so the walk answers every project without `dotnet` and adds the chains the SDK does not see: one that ends in an assembly or a referenced project, or one through a `packages.config` project, whose packages do not flow into a restore. A route both find is printed once, as `sdk`, at the version the project resolves; where the walk took another version from a referenced project's own restore, the hop names it too, as in `Lantern.Json (v13.0.1, graph v12.0.3)`, and every other version it took after it, joined by `;`, and `conflicts` reports the split. Whether `dotnet` is installed changes only which source a route names and which version leads, and a failed `dotnet nuget why`, even one that printed part of an answer first, prints an `SDK_FAILED` line, which names the first line it printed when the script could read none of it. `why --all --project <path>` lists everything one project reaches, direct and transitive, with the first reference that brings each in.
- `where <assembly>` names each project whose references resolve to the assembly and how: an old-style `Reference` with a `HintPath`, a framework or GAC reference without one (marked `framework`, so a redirect that targets one is still checked), a package whose restore or unpacked folder ships the assembly, or a project reference whose assembly name matches.
- `conflicts` names each package or assembly wanted at two or more versions across the solution, one line per project on each side with what brings the version in, and each binding redirect whose `newVersion` is not a version the project references, or whose assembly the project no longer references. Two target frameworks of one project build apart, so versions that differ only between them are not a conflict. It exits 1 when it printed one.
- `graph` writes the whole graph as a Mermaid diagram, or as Graphviz with `--format dot`, to a file in a new temporary directory, or to `--output <file>` outside the skills directories, and prints the path. With `--project <path>` the diagram holds that project and the projects it reaches.

A restore that targets several frameworks answers for each: a line from a project with a restore names the target frameworks it holds for, joined by `;`, and a line from a project with no restore names `-`. A framework is named as the project names it, whether its restore writes the short name or, as older SDKs do, the long one, such as `.NETFramework,Version=v4.7.2`, and whether it writes out the platform version the project leaves to the default, so a project that names both `net8.0-windows` and `net8.0-windows10.0.19041.0` answers for each apart. Each project is evaluated as MSBuild evaluates it, once for each build. The properties are read in the order MSBuild imports the files: an SDK-style project starts with the nearest `Directory.Build.props` at or above it and then the nearest `Directory.Packages.props` at or above it, wherever the solution is, both before its body, reads every other `Import` where it sits, and ends with the nearest `Directory.Build.targets`, where each `GlobalPackageReference` becomes a `PackageReference`; a project that is not SDK-style meets the same points where it imports `Microsoft.Common.props` and its language's targets. Within the files, each condition is evaluated against the values at that point, top to bottom, and a `Choose` applies its first `When` that holds when it is reached, else its `Otherwise`. The items are then read in the same order with the final properties: an `Include` adds one, an `Update` changes those before it, and a `Remove` drops them, with item and metadata names matched without regard to case and a metadata value taken from its attribute or its last child whose condition holds. A project with `TargetFrameworks` builds each framework apart, with `$(TargetFramework)` fixed to it from the start; one with only `TargetFramework` builds that framework; and one that is not SDK-style builds the .NET Framework its `TargetFrameworkVersion` names, such as `net472`, with `$(TargetFramework)` unset. A framework's direct packages are the ones its restore recorded, and its project references are the project file's `ProjectReference` items its restore also recorded; for a framework the restore did not record them for, and for its assembly references, which a restore never records, they are the items that framework's build leaves, a package without a version of its own taking the central `PackageVersion` that build leaves. A project with no restore counts every `Include` and `Update` that may apply in one of its builds, but a `Remove` only where it applies in every build, so a reference one framework removes is kept, and its project read, for the others; a `Remove` that cannot be settled in a build and fails in none is kept and reported. A metadata value such as a `Version` whose property reference cannot be settled is reported and not applied, never compared as its text. A property reference is expanded when it names a property, or calls `StartsWith`, `EndsWith`, or `Contains` on one, or `GetPathOfFileAbove` or `GetDirectoryNameOfFileAbove`; a condition is read when it compares with `==`, `!=`, or, between numbers, `<`, `>`, `<=`, and `>=`, joins with `and`, `or`, `!`, and parentheses, reads a lone operand as a boolean, or calls `Exists` or `HasTrailingSlash`; one operand decides an `or` that holds or an `and` that fails whatever the others are. A property no file sets but `TargetFramework`, `TargetFrameworks`, `TargetFrameworkVersion`, and `AssemblyName`, which start unset, may come from the environment or the command line, so a condition reading it is not settled; `$(MSBuildProjectDirectory)`, `$(MSBuildThisFileDirectory)`, and their siblings are known, and so are `$(SolutionDir)` and its siblings when the root is a solution. An import from the toolset's folders or of an SDK sets nothing the script reads, so it is not read; any other `Import` the script does not read, because its condition or path cannot be settled or its file is missing, unreadable, or outside the examined tree, is reported, and every property after it is unsettled. Anything else, or a malformed condition, is reported as an `UNREADABLE` line naming what was not applied, never guessed. An item inside a `Target` is added only when the target runs, so it is left out and reported, and a project whose `When` has no `Condition`, which MSBuild refuses, is unreadable. A file the script cannot read, such as one that is not well-formed XML or declares a `DOCTYPE` or an entity, costs its own facts and is reported as one `UNREADABLE` line; every other file still answers, and a solution file that cannot be read is reported so, never answered as an empty solution. It reads only inside the examined tree, the checkout holding the solution or the directory searched (the nearest folder at or above it holding `.git`, else that directory): a path a file names, a solution's entry or a `ProjectReference`, that lies outside it, or names a network share (`\\host\share` or `//host`) or a device (`\\?\` or `\\.\`), is reported as an `UNREADABLE` line naming the path and why, and never opened or looked up, so no other host is contacted. The searches for `Directory.Build.props`, `Directory.Build.targets`, `Directory.Packages.props`, and the `packages` folder start only from a project it read, and a file one of them finds is read only inside the tree; one outside it is reported as an `Import` not read, naming its path. The search of a directory for `.csproj` files skips version-control, dependency, build-output, and Visual Studio folders (`.git`, `.hg`, `.svn`, `node_modules`, `.venv`, `venv`, `__pycache__`, `.tox`, `.nox`, `bin`, `obj`, `.vs`) and NuGet's `packages` folder, and does not enter a folder that is a junction or a symbolic link, which may lead outside the directory or back into it, a nested checkout (a folder holding a `.git` folder or file: a clone, submodule, or worktree), or one it cannot list: each of these is reported as an `UNREADABLE` line, and the projects outside it still answer. A file that is not a regular file, such as a device or a pipe, or is larger than `READ_LIMIT_BYTES` in skill-core's `scripts/dotnet_solution.py`, is refused unread and reported the same way. Each `PROJECT` line says where the project's package-to-package edges came from: `assets`, `nuspec`, or `direct` when only the references the project file states are known. Example:

[.NET skills](dotnet.md) states the rule `dotnet-dependencies` shares with `dotnet-format` for which projects a solution's entries name.

```text
/dotnet-dependencies why Newtonsoft.Json --project src/Shop.Web/Shop.Web.csproj
```

## `dotnet-format`

<!-- generated:dotnet-format -->
Run dotnet format (whitespace + style + analyzers) and region layout checks on C# files changed on the current branch and optionally auto-fix violations. Use it when asked to format or style-check C# changes before a commit or pull request.

```text
/dotnet-format
```

Started by you or the agent. Opt-in with the `dotnet` bundle: deploy it with `--include dotnet`. Needs `dotnet` (optional), `dotnet-format`, and `gh` (optional). Takes no arguments.
<!-- /generated:dotnet-format -->

Start it inside the repository, on the branch to check. It first runs `git fetch origin` to bring the remote's branches up to date; when the fetch fails, such as offline, it says so and compares with origin as last fetched. It compares with the pull request's base branch, found with `gh`, else the remote's default branch (`origin/HEAD`), else `origin/main`, else `origin/master`, and checks every committed, uncommitted, and untracked `.cs` file that differs. It uses the nearest solution (`.sln` or `.slnx`) to the current directory that owns a changed file, else the repository's solution whose projects own the most, and stops rather than pick one that owns none. Its search of the repository for solutions skips version-control, dependency, build-output, and Visual Studio folders (`.git`, `.hg`, `.svn`, `node_modules`, `.venv`, `venv`, `__pycache__`, `.tox`, `.nox`, `bin`, `obj`, `.vs`), NuGet's `packages` folder, and nested checkouts, a clone, submodule, or worktree inside the repository, and does not follow a junction or symbolic link; a folder it cannot list ends the run with the folder and the reason, since skipping it could hide the solution to choose. A solution it cannot read, such as a `.slnx` that is not well-formed XML or declares a `DOCTYPE` or an entity, ends the run with the file and the reason, never read as a solution that owns no changed file. It reads a solution, project, or `.editorconfig` file only when it is a regular file no larger than `READ_LIMIT_BYTES` in skill-core's `scripts/dotnet_solution.py`, the bound `dotnet-dependencies` reads within; any other, such as a device, ends the run the same way, unread. It acts only inside the repository. It reads or evaluates a project the solution lists only when the entry lies inside the repository root, judged from its text by the rule `dotnet-dependencies` applies (`refusal` in skill-core's `scripts/dotnet_solution.py`); an entry outside the root, or written as a network share or a device path, is reported as a `REFUSED <entry> <reason>` line and left unread, so the package check sees only the repository's projects. Whether the solution references `Roslynator.Formatting.Analyzers` is read from MSBuild's evaluation of each of its projects that owns a changed file (`dotnet msbuild -getItem`), so a comment, a false condition, or a props file MSBuild does not import never counts; when `dotnet` is missing or a project cannot be evaluated, it reports the package as unknown with the reason. The `.editorconfig` settings are read as the compiler reads them: `root = true` ends the chain only in a file's preamble, before its first section, and a severity set in the nearest `.globalconfig` at or above a project's folder, or in an `.editorconfig` that declares itself global, counts below every `.editorconfig` section, so a value the repository sets there is reported as its own and never overridden. A global config a project names in `GlobalAnalyzerConfigFiles` is not read. It never writes a file that is a symbolic link or a Windows junction, or that lies below one inside the repository, since the write would land wherever the link points: a fix or a setting it would add there is reported as a `REFUSED <path> <reason>` line and left undone, and the file is still checked. A changed file whose name starts with `@` or `-` is passed to the formatter as `./<name>`, because both formatters read a bare `@<name>` as a file of further arguments and a bare `-<name>` as an option. Between solutions that own as many changed files it takes the shallowest, then a `.sln` over a `.slnx`. Files outside that solution, such as scripts or `.cs` files no project compiles, get only the layout checks, and so does a file whose byte order mark names an encoding its bytes are not in, which the layout checker reports as unreadable while it checks the rest. It reports violations and asks before fixing them, and asks before adding any missing `.editorconfig` settings. It runs the SDK's built-in `dotnet format` for a `.slnx` and for a `.sln` whose C# and Visual Basic projects are all SDK-style, which needs the .NET SDK 9.0.200 or newer, and stops when `dotnet` is not installed rather than fall back to the global tool. For any other `.sln`, one holding a project in the format before SDK-style ones, it runs the `dotnet-format` global tool (`dotnet tool install -g dotnet-format`), because the SDK's command can fail with `TypeInitializationException` against .NET Framework solutions on newer SDKs. That global tool is deprecated: its NuGet package, last released as 5.1.250801, is marked legacy and unmaintained because `dotnet format` ships in the .NET SDK from .NET 6 on. It cannot open a `.slnx`, and it bundles a Roslyn older than the analyzers current SDKs give an SDK-style project, so its analyzer pass ends with a `ReflectionTypeLoadException` on such a project; the skill names that failure and its remedy, converting the remaining projects to SDK-style. The global tool is not run on an ASP.NET project's files, which the skill has skipped since its first version as crashing the formatter; the SDK's `dotnet format` formats them, so with it they are formatted like any other, and with the global tool they get only the layout checks. When a fix that runs in several runs fails part way, it names the files the finished runs saved fixes to and the files whose run did not finish. A changed-file list too long for one Windows command line is formatted in several runs, and all of a check's or a fix's runs stop after 570 seconds together, even when the formatter leaves a build host running.

It checks these layout rules:

| Rule | Enforced by |
|---|---|
| `else if` on one line | Roslynator `RCS0041` |
| Exactly one blank line between members | `RCS0010` and `RCS0012` (at least one), `RCS0063` (no more than one) |
| Exactly one blank line after `#region` and before `#endregion` | `RCS0002`, `RCS0005`, and `RCS0063` |
| No blank line between a type's `{` and its first `#region` | `RCS0063` |
| Newline at end of file | `insert_final_newline = true`, applied by the formatter's whitespace pass |
| `#endregion` in the brace scope of its `#region`; no `#endregion` description; exactly one blank line between `#endregion` and a following `#region`; no blank line between `#endregion` and a following `}` | `csharp_layout.py check` |

The Roslynator rules need the `.editorconfig` settings the skill proposes and the `Roslynator.Formatting.Analyzers` package, which it never adds itself.

[.NET skills](dotnet.md) states the rule `dotnet-format` shares with `dotnet-dependencies` for which projects a solution's entries name.

## `flag-review-finding`

<!-- generated:flag-review-finding -->
Add, list, edit, or resolve a structured code-review improvement flag. Use it when the user says a review finding was wrong, noisy, or missed something and wants that recorded.

```text
/flag-review-finding add CATEGORY BODY [--repository owner/repo --pull N [--review-version V --finding ID]] | list | findings --repository owner/repo --pull N | edit ID [--category CATEGORY] [--body BODY] | resolve ID RESOLUTION
```

Started by you or the agent. Installed with the `code-review-operations` bundle.
<!-- /generated:flag-review-finding -->

The first word chooses what to do:

- `add CATEGORY BODY` records a flag. `CATEGORY` is a short label of your choosing, up to 120 characters, and `BODY` is the rationale, up to 4000; quote either when it has spaces. The agent puts the options first and `--` before the two values, so a value may begin with `-`. To tie the flag to a review finding, add `--repository owner/repo --pull N`, then `--review-version V --finding ID`. The review version and finding come from the finding's label in the review report: `v2 F003` is `--review-version 2 --finding F003`. A re-review report shows a finding carried from an earlier review under that review's label, so the report's **Mode** row is not the version to use. Finding IDs restart at `F001` in every review, so `--finding` needs `--review-version`. `--pull` without `--repository`, or `--review-version` without `--finding`, names nothing and is refused. The flag is refused unless `--finding` has that form and that review of the pull request is in the configured archive with that finding, because a flag on a finding that does not exist would never reach a reviewer. `review-insights` links a flag that names a finding to the recommendations covering that finding, and its synthesis can name any open flag, one that names no finding included; accepting a recommendation resolves the flags it links. A flag on a finding doesn't close it or change the verdict: the next re-review of that pull request gives the flag's category and rationale to the reviewer, which weighs it and marks the finding `superseded`, citing the flag, when the flag holds. "Finding ledger" in [code-review-operations.md](code-review-operations.md#finding-ledger) has the details.
- `list` shows the open flags.
- `findings --repository owner/repo --pull N` shows the pull request's open and unverified findings, each under the label its review report shows, so the agent can find the one you mean without reading the report.
- `edit ID [--category CATEGORY] [--body BODY]` corrects the category, the rationale, or both of the open flag `ID`, leaving out the option for a value that stays. The agent joins each value to its option, as in `--body="..."`, so a value may begin with `-`. The repository, pull request, review version, finding, and status stay as they were, so fixing a rationale leaves no superseded flag behind. A resolved flag is refused rather than edited, and so is an ID the store does not have, a blank value, a value past its length limit, or neither option. Any change that would take the flag store past the size it is read at is refused, leaving the store as it was. The status is checked under the flag store's lock.
- `resolve ID RESOLUTION` closes the flag `ID`, such as `RF-000004`, with a non-empty explanation. It prints `RESOLVED ID` when it closes the flag, or `ALREADY_RESOLVED ID` when another call resolved it first, preserving that call's resolution and timestamp. The status is checked under the flag store's lock.

```text
/flag-review-finding add noise "Asked for a null check the caller already guarantees" --repository octo-org/widgets --pull 42 --review-version 2 --finding F003
/flag-review-finding list
/flag-review-finding findings --repository octo-org/widgets --pull 42
/flag-review-finding edit RF-000004 --body "Asked for a null check the constructor already guarantees"
/flag-review-finding resolve RF-000004 "The reviewer prompt now checks the caller's contract"
```

## `github-activity-report`

<!-- generated:github-activity-report -->
Report one user's GitHub contributions in one organization month by month: pull requests authored and merged, commits, pull requests reviewed, and reviews submitted. Use it when asked what someone contributed or reviewed over recent months.

```text
/github-activity-report ORG USER [--months N]
```

Started by you or the agent. Installed by default. Needs `gh`.
<!-- /generated:github-activity-report -->

- `ORG`: the GitHub organization. Required.
- `USER`: the GitHub login to report on. Required.
- `--months N`: how many calendar months to cover, ending with the current, partial month. The default is 12 and the maximum 36.

A 12-month report takes a few minutes, because searches are spaced to stay under GitHub's rate limits. `gh` must be signed in with a token that can read the organization's repositories, including SSO authorization where the organization enforces it.

```text
/github-activity-report octo-org octocat --months 6
```

## `repo-cleanup`

<!-- generated:repo-cleanup -->
Clean up GitHub repos: switch to default branch, prune gone branches, remove stale worktrees, and fast-forward remaining branches. Sweeps all repos under `REPOS_ROOT` or targets a single repo by name/path.

```text
/repo-cleanup [<repo-name-or-path>]
```

Started by you. Installed by default. Needs `gh` and the `REPOS_ROOT` setting.
<!-- /generated:repo-cleanup -->

- With no argument, it sweeps every repository under the `REPOS_ROOT` you set with `python deploy.py configure`.
- `<repo-name-or-path>` cleans one repository: a name under `REPOS_ROOT`, or an absolute path to a directory that contains `.git`.

It performs only the actions it can prove safe, such as removing a branch whose pull request merged, and asks you about anything else, such as local branches with no remote and the clean worktrees they are checked out in, a branch whose pull request closed without merging, unmerged work, a clean detached worktree, or a directory left in the worktree area that Git no longer tracks. Its plan files go in a new temporary directory, never in a skill directory.

For each repository it runs `git fetch --all --prune`, switches to the default branch, prunes worktree records, fast-forwards the default branch, classifies branches and worktrees, and performs the safe actions, re-checking every recorded branch tip first. The default branch is the one `origin/HEAD` names when origin has that branch, else `main` or `master` when origin has one. An `origin/HEAD` naming a branch origin no longer has, as after the remote renamed its default branch or in a clone of a checkout that was on a topic branch, is never followed: it is refreshed from origin as `git remote set-head origin --auto` does, and when that fails `main` or `master` stands in. When neither gives a branch origin has, the repository is reported as an error and left on its branch. Its summary of a repository covers the default branch, whether the run fast-forwarded it or switched to it, and how it was found when `origin/HEAD` named a branch origin lacks, deleted branches, removed worktrees, fast-forwarded and diverged branches, branches not fast-forwarded because their worktree has changes, skipped dirty worktrees, branches kept because their pull request status was unverified or unmatched, protected release worktrees, and any unmerged, local-only, closed-pull-request, preserved, or open-pull-request branches, detached worktrees kept and why, directories Git no longer tracks that were kept or deleted, and removed empty directories. A repository is reported as quiet, with its summary folded, only when nothing changed and nothing needs you. When a step fails partway, the summary still lists everything done before it, under a heading that names the failure. That heading describes the command that failed: `summary` repeats it, and a later `delete-local`, `force-delete`, or `remove-directory` on the same plan that completes reports a complete cleanup again.

The commands enforce these rules, whatever the agent is asked:

- `release/*` branches are never deleted. A worktree is never removed when its branch matches `release/*` or its path contains a `/release/` segment. Both match in any letter case, since Windows treats `Release` and `release` as one directory.
- A branch not checked out in a worktree is deleted automatically only when its remote branch is gone and its pull request status is `MERGED`, `CLOSED`, or `NONE`, or when it was pushed under its own name, its upstream exactly `origin/<its name>`, has no pull request, and the default branch, local or fetched, contains both its tip and its remote branch's, so nothing on it is its own; either way only when the default branch contains its tip. A branch whose upstream is another branch, as one started from `origin/main` tracks `origin/main` and `foo` started from `origin/feature/foo` tracks that, never counts as merged this way. `git branch -d` alone would judge it against `HEAD`, which a skipped or failed switch leaves elsewhere, so such a branch is deleted with `git branch -d`, or with `git branch -D` when `-d` refuses only because `HEAD` lacks it. A squash- or rebase-merged branch the default branch does not contain is deleted with `git branch -D` when its status is `MERGED` and its tip has not moved. A merged or closed pull request counts only when its head is in the same repository, there are no newer upstream commits outside the pull request's base branch, and it ended at the branch's exact local tip. Each pull request is judged against its own fetched base branch, the default branch or another such as a release branch, and a base the fetch did not bring proves nothing. A merged pull request also counts when the local tip is one of its commits and every later commit is a merge whose non-first parents are reachable from that base, as GitHub's "Update branch" makes. A merged pull request also counts when its merge commit made the branch's whole change since the branch left that base, as a tip amended or rebased after the pull request opened keeps it. The comparison is file by file: each file the branch changed must hold the same bytes before and after as it did before and after the merge commit, and the merge commit must change no other file, so whitespace, line endings, and where in a file a change sits all count, and no Git setting or environment variable of yours, such as diff color, an external diff program, or `GIT_DIFF_OPTS`, changes it. A later commit with any other changes, whitespace alone included, the same edit made at another place in a file, a file of the branch's that the base changed after the branch left it and before the merge, or a merge commit the fetch did not bring, leaves the branch `UNMATCHED`. Branches whose status is `OPEN`, `UNMATCHED`, or `UNKNOWN` (any failed or possibly truncated `gh` query) are kept and reported, never deleted or offered for deletion.
- A clean linked worktree whose branch's pull request is `MERGED`, whose remote branch is gone with no pull request, or whose branch the default branch holds as the rule above describes, is removed, and its branch is deleted as the rule above deletes a branch whose remote is gone, even when its remote branch still exists. A clean linked worktree whose pull request is `CLOSED` without a merge is removed, but its branch is kept and you are asked about it, as for a local branch with no remote, because work set aside rather than landed is yours to drop. A clean linked worktree whose branch has no remote and no pull request is only offered, since it may hold a task just started: when you delete the branch, its worktree is removed first under the rules below.
- A detached worktree, one with no branch checked out, is offered for removal only when it is clean, holds none of the state below that `git worktree remove` discards, and its commit is on the default branch or a remote branch, so removing it loses nothing; otherwise it is kept, and the summary says why. A branch being rebased is detached while the rebase runs, so its worktree is judged this way. When you confirm, `remove-directory` removes it only if it has not moved and its commit is still on such a branch.
- After the sweep, each directory under `REPOS_ROOT/Worktrees/<repository>` or the repository's `.claude/worktrees` that is not a registered worktree and holds no `.git` entry or worktree, as one a removal on Windows could not finish leaves behind, is reported as an `ORPHAN`, unless Git tracks a file in it. A directory in which `git ls-files`, run in each repository holding it, from the repository root or `REPOS_ROOT` down, lists a file, matched literally in any letter case, is kept and listed under "Preserved" with the files, the first few and then how many more, as is one for which Git cannot answer, with Git's error. A `Worktrees` folder under `REPOS_ROOT` that is itself a repository is one of the repositories asked, so the folders it tracks are kept. A directory that holds a worktree is searched instead, and a link is neither followed nor reported. An area is searched only when every component below the repository root, or below the folder holding `Worktrees`, is a plain directory, so an area reached through a link or junction there, such as a `.claude` that is one, is not searched at all; a repository root or `REPOS_ROOT` that is itself reached through a link is taken as you placed it. A confirmed deletion refuses a directory that, resolved through links, no longer lies inside its area. Its contents are not in Git, so it is deleted only when you confirm, and only if it is still such a directory and Git, asked again, still tracks no file in it; a link inside it is removed, never followed.
- A branch, worktree, or directory whose name holds a character a fact line cannot carry, a control character, a line or paragraph separator, a byte that is not UTF-8, or trailing Unicode space, is never offered, since a confirmation could name it only by its flattened form, which names nothing: it is listed under "Preserved" by that flattened form with the reason, and stays.
- Otherwise `git branch -D` runs only after you confirm, and only for a branch reported `UNMERGED` whose tip has not moved. A `CLOSED` or `NONE` branch the default branch does not contain is always reported `UNMERGED`, never forced.
- Worktrees with uncommitted or untracked changes are never removed, only reported, even when `status.showUntrackedFiles` hides untracked files from `git status`. Nor is a worktree whose clean status hides state `git worktree remove` discards without asking: an operation in progress (a bisect, rebase, `git am`, merge, cherry-pick, or revert, read from the markers in the worktree's own Git directory), a ref only it sees (under `refs/worktree`, `refs/bisect`, or `refs/rewritten`), or an edit to a file whose skip-worktree or assume-unchanged bit hides it from `git status`, found by comparing each such file with the index; a file a sparse checkout leaves out is absent, not edited, so a sparse worktree is removed as any other. The sweep reports a branch's worktree with such state among the dirty worktrees, and every removal, confirmed or not, asks again just before it runs. Worktrees are removed only by `git worktree remove`; when Git refuses, the worktree and its branch are preserved and reported. When Git deletes the worktree's record and files but not its folder, as on Windows when a process works in it, the branch is preserved with that reason, and a sweep offers what is left as a directory Git no longer tracks. A worktree holding ignored files, such as a `.env` or a `bin` folder of scripts, is preserved and reported too, because `git worktree remove` would delete them; the report names the paths, the first few and then how many more. The exception is regenerable output, which a tool rebuilds from what the worktree holds: `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `node_modules/.cache`, and `.vs` anywhere, `node_modules` beside a `package.json`, and `bin` and `obj` beside a `.csproj`, `.vbproj`, or `.fsproj` project file. Each counts only as a plain directory reached from the worktree through plain directories, never through a link or junction, so nothing outside the worktree is deleted with it. A worktree whose ignored paths are all regenerable, or inside such a directory, is removed with them under the same checks as any other, and the summary names the regenerable paths deleted. Any other ignored path still preserves the worktree, and its report also names the regenerable paths beside it.
- Empty directories are removed only with `rmdir`, only for parents of worktrees removed and directories deleted on the same plan, and never at or above `REPOS_ROOT/Worktrees`, the repository's `.claude/worktrees`, or the repository root.
- Fast-forwards are fast-forward only, and never move a branch checked out in a worktree with uncommitted or untracked changes, the main worktree included; the summary lists each such branch under "Fast-forward skipped". Diverged branches are reported, never rebased, merged, or reset. The main worktree's files are never stashed, reset, or cleaned.
- An unexpected failure in one repository is reported as that repository's `ERROR`, after the summary of what was done before it, and the sweep still reports every other repository. The sweep, `delete-local`, `force-delete`, and `remove-directory` exit 1 when Git refuses a deletion or removal, a branch Git refuses to delete after its worktree was removed included. `delete-local` and `remove-directory` also exit 1 when a confirmed worktree removal did not happen because its ignored files or the state above kept the worktree, which the sweep reports without failing, and `remove-directory` also when deleting a directory fails, it no longer resolves inside its area, which it checks before anything else, or Git now tracks a file in it. A confirmed branch, worktree, or directory that moved since the plan, or a detached worktree whose commit left the default and remote branches, is preserved with the reason and exit 0.
- When a repository's fetch fails, nothing after the fetch runs for it, so its checkout is not switched. A multi-remote fetch may have updated some remote-tracking refs before failing.

In Copilot CLI, start it from an interactive session: a headless `copilot -p` session cannot start a user-only skill ([Headless sessions](copilot-support.md#headless-sessions)). It runs fully in Claude Code and Codex.

```text
/repo-cleanup
/repo-cleanup widgets
```

## `review-document`

<!-- generated:review-document -->
Review one document from its file path, such as a design draft on disk or a document with uncommitted edits, or the documents a pull request changes, through the code-review pipeline's design reviewer, and write a validated report and record. Use it when asked to review a design document, a Word or PDF draft, another text file, or a pull request's documents rather than its code.

```text
/review-document <path> | owner/repo#number [--document PATH] [--base none|committed] [--stage draft|review|final] [--output DIR]
```

Started by you or the agent. Installed with the `code-review-operations` bundle. Needs `copilot` (optional), `gh` (optional), and `pdftotext` (optional).
<!-- /generated:review-document -->

- `<path>`: the document to review, a text file, a Word document (`.docx`), or a PDF (`.pdf`). A PDF needs `pdftotext` meeting its [provider-specific support floor](dependency-updates.md#runtime-tool-floors); without a supported, identifiable parser, the review is refused before it starts, with a reason that names the tool.
- `owner/repo#number`: in place of a path, a pull request, or its URL, `https://github.com/<owner>/<repo>/pull/<number>`. Each Markdown, plain-text, Word, or PDF document it changes is read through `gh` at the pull request's base and head commits, with no checkout, and reviewed on its own, with its own report and record. A changed document that cannot be reviewed, such as one the pull request removes, is skipped with the reason, and a pull request that changes no such document is refused. The pull request's title, description, and comments are never read into the review, and nothing is posted to it.
- `--document PATH`: with a pull request, the one changed document to review, by its path in the repository. Without it, every changed document is reviewed.
- `--base none|committed`: what the document is compared with. Without it, a file committed in a git checkout is reviewed against its committed version, so only the uncommitted change is judged, and any other file is reviewed whole; a pull request's document is reviewed against its version at the pull request's base, and one the pull request adds is reviewed whole. `none` reviews the whole document anyway; `committed` insists on the change and fails when nothing is committed, or when the pull request adds a document.
- `--stage draft|review|final`: the stage the document is at, which sets how severely the design-review specialist judges what it has not written yet, not what it asks. `draft`, for an early iteration, makes a section it has not written, an open question without an owner, and a requirement it says it will state later suggestions, while a contradiction in what it has written stays a must-fix; `final`, for a document about to be built or already approved, makes a placeholder such as `TBD` and an unowned question that decides what is built must-fixes. Without it, `review`, a document meant to be built from as written. Only a document the design-review specialist reviews takes a stage.
- `--output DIR`: where the fixture and the copy of the report go, a new or empty directory outside every skills directory. Without it, a new `review-document-*` temporary directory.

A Markdown, plain-text, Word, or PDF document (`.md`, `.markdown`, `.txt`, `.rst`, `.adoc`, `.docx`, or `.pdf`) is reviewed by the suite's design-review specialist, which looks for requirement gaps, unstated alternatives, risks, open questions, inconsistencies, and rollout gaps; any other text file gets the generic code reviewer. The review runs on the session's model, and design review needs Sonnet or stronger. The skill builds a one-file fixture for each document and reviews it as a [fixture canary](code-review-operations.md#fixture-canaries), so the reviewer guard, result validation, and record are those of a pull request review. It reads from GitHub only a pull request it is given, posts nothing, and changes nothing in the checkout, but it needs the [code-review configuration](code-review-operations.md#first-configuration). The record stays in a new canary directory, and the report is copied beside the fixture. [Document reviews](code-review-operations.md#document-reviews) describes the fixture and the record.

A Word document is reviewed as the text the pipeline extracts from it, one line per paragraph or table row, so a finding's line counts that text, not the document's pages. The report gives each such finding's place in the document beside its line, such as `paragraph 12, under the heading "Scope"`, which is how to find the passage in Word; [Word documents](code-review-operations.md#word-documents) describes the extraction. A Word document whose text cannot be extracted, or whose edit changes only formatting, layout such as an inserted empty paragraph, images, or parts that are not extracted, is refused before anything is reviewed, with the reason.

A PDF is reviewed as the text a supported Xpdf or Poppler `pdftotext` extracts from it, one line per line of text in the order the PDF draws it, and the report gives each finding's place as `page 3, line 12`, counting that page's lines of text; [Word documents](code-review-operations.md#word-documents) ends with the PDF extraction. Every extraction probes the provider and version of each `pdftotext` it finds, in order, before giving document bytes to the first it accepts, then runs that checked path. When it accepts none, the refusal names each parser it passed over with what its probe found and where it was found. A PDF the tool cannot read, one it has not read within 60 seconds, and one whose text is over 1 MiB are refused too. The probe is a support-policy check, not sandboxing or an exploit-proof guarantee. A PDF generated from a source document is better reviewed from that source, whose text keeps its structure.

In Copilot CLI, which cannot start subagents, the session works the reviewer role itself, without the reviewer agent's guard.

```text
/review-document docs/design/export-retention.md
/review-document D:\Drafts\retention.md --base none --output D:\Reviews\retention
/review-document "D:\Drafts\Queue design.docx"
/review-document D:\Drafts\retention.pdf
/review-document octo-org/widgets#42
/review-document https://github.com/octo-org/widgets/pull/42 --document docs/design/retention.md --base none
/review-document docs/design/export-sharing.md --stage draft
```

## `review-insights`

<!-- generated:review-insights -->
Analyze structured code-review findings and open flags for an explicit date range and repository set, and recommend guidance, reviewer, and analyzer changes. Use it when asked what reviews keep flagging, which findings were accepted or rejected, or what to change in review guidance.

```text
/review-insights START_DATE END_DATE [owner/repo ... | --repository-set NAME]
```

Started by you or the agent. Installed with the `code-review-operations` bundle.
<!-- /generated:review-insights -->

- `START_DATE END_DATE`: the inclusive range, as `YYYY-MM-DD`. Both are required; the skill never guesses a range.
- `owner/repo ...` or `--repository-set NAME`: whose reviews to analyze. With neither, started inside a configured repository's checkout or one of its worktrees, it asks whether to analyze that repository or the configured `review-insights` set; started anywhere else, it uses that set. Repository sets are defined in the [code-review configuration](code-review-operations.md#configuration).

It counts the findings by category and analyzer rule, and finds where reviews disagreed with themselves: the same finding at different severities, a verdict that changed while the code did not, and reviewers rating one place differently. It reads the guidance the repository's reviewers were given and finds rules that contradict or repeat each other across files. Then one subagent reads all of it, how later reviews judged the findings, and every open flag, and recommends specific changes, cheapest first: an analyzer rule the repository already has to enforce, a check to move from guidance into a script, an analyzer to adopt or a custom rule to write, then a guidance change: a rule to remove, merge, strengthen, or add, or something reviewers should stop or start flagging. With a report for the period just before, it also compares the two. It then asks you to accept, reject, or defer each recommendation. Accepting one resolves the flags linked to it, including flags that name no finding.

```text
/review-insights 2026-09-01 2026-09-30 --repository-set team
```

## `review-prs`

<!-- generated:review-prs -->
Review eligible pull requests in explicitly configured repositories, review or re-review explicit pull requests, or run isolated canaries of pull requests or local fixtures, and produce validated structured reports. Use it when asked to review or re-review pull requests.

```text
/review-prs [owner/repo ... | --repository-set NAME | --pull owner/repo#number ... --re-review owner/repo#number ... --scope auto|full|incremental] [--force] | --canary owner/repo#number ... | --canary --fixture DIR [--re-review --prior RECORD]
```

Started by you or the agent. Installed with the `code-review-operations` bundle. Needs `copilot` (optional), `gh`, and `pdftotext` (optional).
<!-- /generated:review-prs -->

It runs in one of four modes, chosen by its arguments:

- **Batch**: `owner/repo ...`, `--repository-set NAME`, or nothing for the configured set. It reviews each repository's open, non-draft pull requests and those merged since its last batch, skipping heads already reviewed.
- **Explicit**: `--pull owner/repo#number` for a first review and `--re-review owner/repo#number` for one whose head changed. Repeat either, or both, in one run, naming each pull request once. They cannot be combined with batch selectors. A re-review needs an earlier review, gives each of its findings a disposition, and records the next review version without overwriting the last.
- **Canary**: `--canary owner/repo#number` runs a first review in isolation. Its pull request must be in a configured repository, and it reads the configuration and, when the repository has a `checkout_path`, snapshots the head from that checkout, fetching the pull request's head there when the commit is not already local. It writes the result under a new temporary directory and never reads or writes the configured archive, mirrors, state, dashboards, flags, watermarks, or GitHub review state. Repeat it to check a configuration change against several pull requests in one run, such as one of each kind a reviewer manifest routes differently; each gets its own temporary directory. It takes no other selector and no `--force`.
- **Fixture canary**: `--canary --fixture DIR` runs a canary of a local fixture directory instead of a pull request, reading nothing from GitHub; skill evaluations use it. `--re-review --prior RECORD` re-reviews the fixture against an earlier review record of it, such as one a fixture canary wrote, and records the dispositions and ledger a real re-review would. It takes one fixture and nothing else. [Fixture canaries](code-review-operations.md#fixture-canaries) describes the directory.

`--scope` sets how much every `--re-review` in the run reviews again: `full` reviews the whole pull request; `incremental` reviews in full only the files whose changes differ from the last review and those it left unreviewed, and only records dispositions for the earlier findings in the rest; `auto` chooses between them from how much changed. Without it, the skill asks once; it never picks one itself.

Each reviewer role runs as a subagent. Where the session cannot start one (Copilot CLI, or a review started from inside a subagent), or when you ask for an inline review, the session works each role itself, one at a time; [Inline reviews](code-review-operations.md#inline-reviews) says what that costs in isolation.

It runs fully only in Claude Code. In Codex, reviewers start as native subagents rather than through the Workflow tool, so a reviewer `effort` setting, in the configuration or a specialists manifest, has no effect. In Copilot CLI, the generic reviewer and the specialists of a manifest that leaves `agent-delegation` out of `required_capabilities` run inline, and a repository entrypoint reviewer runs on the bounded Copilot host; a specialists manifest that lists `agent-delegation` fails there.

`--force` reviews heads that already have a review. The skill never posts to GitHub, and keeps its working files, such as a batch's list of pull requests, in new temporary directories, never in a skill directory. [Code-review operations](code-review-operations.md) covers the configuration and the records it writes.

```text
/review-prs --repository-set team
/review-prs --pull octo-org/widgets#42 --re-review octo-org/widgets#37 --scope auto
/review-prs --canary octo-org/widgets#42 --canary octo-org/widgets#51
/review-prs --canary --fixture tests/fixtures/skill-evals/review-prs/re-review --re-review --prior tests/fixtures/skill-evals/review-prs/re-review/prior.json
```

## `update-coding-agent-skills`

<!-- generated:update-coding-agent-skills -->
Fast-forward the coding-agent-skills clone these skills were deployed from to origin/main, then redeploy every skill with deploy.py --all. Stops before a release that raises the major version until the user passes --cross-major, and over a deployment pinned with deploy.py --at until the user passes --unpin.

```text
/update-coding-agent-skills [--cross-major] [--unpin]
```

Started by you. Installed by default.
<!-- /generated:update-coding-agent-skills -->

It updates the clone these skills were deployed from: it fast-forwards `main` to `origin/main`, then runs `python deploy.py --all`. A clone found on another branch is switched to `main` for the update and left there. It stops without changing anything, leaving the clone on the branch it was found on, when tracked files have uncommitted changes, the fetch fails, or local `main` has commits that `origin/main` lacks. It also stops before fetching, reporting `TAG_SHADOWS_BRANCH` and each such tag, when a tag named `main` or `origin/main`, on `origin` or in the clone, would stand in for that branch in Git's short names. It never stashes, resets, or forces a deployment; those decisions stay yours. It also stops before fetching, reporting `PINNED <ref> <commit>`, when `python deploy.py check` names the deployment as pinned with `deploy.py --at <ref>` ([Deploying an earlier commit](recovery.md#deploying-an-earlier-commit)), since updating would replace the pinned commit with `main`; rerun it with `--unpin` when you are ready to leave the pin. A check that fails, or names no pin, does not stop the update here, but the deployer itself refuses to replace a pinned deployment without `--unpin`, so the update then ends with `DEPLOY_FAILED` and the deployer's refusal, and the pin stays.

Without arguments it also stops, reporting `MAJOR_UPDATE <current>..<target>` and the commits it would deploy, when the highest release tag on `origin/main` raises the major version above the installed one (or the minor version, while the major is 0), because such a release may ask you to act, as [Versioning](releasing.md#versioning) explains; read the release notes for `<target>`, then rerun it with `--cross-major` to apply the update. The installed version is the highest release tag reaching the commit `python deploy.py check` names as deployed, not local `main`, so a clone pulled by hand past a release and not redeployed still stops there. A release tag is `vMAJOR.MINOR.PATCH`, optionally with a pre-release suffix such as `-rc.1` that counts as its version; other tags are ignored. When no release tag reaches the deployed commit, it counts as version 0.0.0 and `<current>` reads `untagged`; when `check` names no deployed commit, as before the first deployment that records one, `<current>` reads `unknown`; either way any release on `origin/main` stops the update. When no release tag reaches `origin/main` there is no release to cross and the update proceeds without stopping. A release tag whose major or minor version has more digits than the update compares, or a deployed commit the clone does not hold, ends it with `FAILED` and the reason, after the fetch and before anything is merged or deployed.

- `--cross-major`: apply an update across such a release boundary. The output names the crossing as `CROSSED <current>..<target>`.
- `--unpin`: update and deploy over a pinned deployment, passing `--unpin` to the deployer. The deployer's report then prints `Replaced the deployment pinned with --at <ref> ...` before its Run ID and Manifest lines, which the skill reports.

In Copilot CLI, start it from an interactive session: a headless `copilot -p` session cannot start a user-only skill ([Headless sessions](copilot-support.md#headless-sessions)). It runs fully in Claude Code and Codex.

```text
/update-coding-agent-skills --cross-major
/update-coding-agent-skills --unpin
```

## `update-pr-dashboard`

<!-- generated:update-pr-dashboard -->
Update the owned dashboard section for pull requests the configured user authors, reviews, or participates in. Use it when asked to refresh the pull request dashboard or tracker, or to see which pull requests need attention.

```text
/update-pr-dashboard [owner/repo ... | --repository-set NAME] [--no-review] [--remove owner/repo#number ...]
```

Started by you or the agent. Installed with the `code-review-operations` bundle. Needs `gh`.
<!-- /generated:update-pr-dashboard -->

- `owner/repo ...` or `--repository-set NAME`: which repositories' pull requests to track. With neither, it uses the configured `update-pr-dashboard` set. Use the same scope on every run, or the rows for the other repositories disappear.
- `--no-review`: update the dashboard without offering reviews for pull requests whose AI review is missing or out of date.
- `--remove owner/repo#number ...`: leave those pull requests out of this run's dashboard, for example one you consider approved. It removes only the row and never acts on GitHub. A number that matches none of your open pull requests is refused, naming it, rather than ignored.

Without `--no-review`, it lists the pull requests that need a review, except any pinned under a status of your own (below), asks whether to review them, and, once you confirm, asks once for a re-review scope when any of them is out of date. A review is out of date when the pull request changed since it or GitHub's answers can't show that it didn't, never because a GitHub call failed: then the dashboard keeps its previous rows, no review is offered, and the skill names each pull request it couldn't compare, with the error. When the refresh after the reviews finds a review already out of date, because the pull request received commits while the run was reviewing, the skill says so; that pull request is offered again next run. The pull requests it collects are kept in a new temporary directory, never in a skill directory. A dashboard or configuration reached through a symbolic link is updated where the link leads, and the link and the file's permissions stay as they were.

To keep a pull request under a status of your own, such as `on hold` or `delegated`, ask for it: the skill records it in `dashboard.status_overrides` in the code-review configuration through a validated write, which keeps every other value but rewrites the file with sorted keys and two-space indentation, and from the next update the row sits in a section named after that status. The pull request must be one the tracker held at its last update, so a mistyped number is refused rather than saved, and a new pull request needs an update first. The status can't be a state the tracker computed itself (`to review`, `awaiting response`, `my prs`, `my pull requests`, `drafts`, `missing`, `current`, `stale`, or `incomplete`). Unlike `--remove`, it lasts until you ask to clear it, which also works once the pull request has closed; listing the overrides marks each one whose pull request the tracker no longer holds. A pinned row still shows its AI review state, but the skill never offers it for a review or says its review went out of date; to have it reviewed, clear the override or run `review-prs` on it directly.

The reviews it offers run through `review-prs`, so in Codex and Copilot CLI they have the limits that skill's section states. The dashboard itself works the same in every runtime.

Each row's Findings cell first shows the work that remains, then the progress. The remaining work is the latest AI review's open findings by severity, including those carried from earlier reviews, and how many of them you have flagged with `flag-review-finding`, for example `1M 1S open (1 flagged)` or `none open`. A legend line above the first section spells out the letters: `M` must fix, `H` should fix, `S` suggestion. The progress is what moved since your own last review of that pull request, for example `· 1 new, 1 addressed since your review`, or `· unchanged since your review`. "New" counts findings first raised after the AI review of the commit you reviewed and still open; "addressed" counts findings raised by then that a later review found fixed. Before you have reviewed, or when GitHub can't place your review's commit, it counts every finding the reviews found fixed instead, such as `1H 3S open · 2 addressed`, and shows the open findings alone when none has been fixed yet. A review recorded before the finding ledger reads the same way, and a migrated legacy review shows only its open findings, because it can't say what was addressed. The AI Review link opens the latest report, whose file name carries the review version, and which lists every open finding in full.

```text
/update-pr-dashboard
/update-pr-dashboard --remove octo-org/widgets#42
```
