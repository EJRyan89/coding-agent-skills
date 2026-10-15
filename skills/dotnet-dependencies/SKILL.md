---
name: dotnet-dependencies
description: "Answer why a package or assembly is in a legacy or SDK-style C# solution, which projects reference an assembly, and where versions and binding redirects conflict, from a read-only dependency graph of the solution's project files. Use it when asked why a package is referenced, what pulls a dependency in, which projects use an assembly, or whether versions or binding redirects disagree, and not to restore, build, or change references."
argument-hint: "<why <name>...|where <assembly>|conflicts|graph> [--project <path>]"
allowed-tools: ["Bash({{PYTHON}} -B \"${CLAUDE_SKILL_DIR}/scripts/dependency_graph.py\" *)", "PowerShell({{PYTHON}} -B \"${CLAUDE_SKILL_DIR}/scripts/dependency_graph.py\" *)"]
---

Answer a dependency question about the .NET solution in the current directory from its project files alone. One script builds the graph and answers every query; it reads `.sln`, `.slnx`, `.csproj` and the files a project imports, `Directory.Build.props` and `Directory.Packages.props` among them, `packages.config`, each package's `.nuspec`, `obj/project.assets.json`, `web.config`, and `app.config`, runs no restore or build, and changes nothing but the file `graph` writes. Each line it prints is one tab-separated fact; its docstring names every line. Act on the printed lines, not the exit code: a query that reports a conflict or an unreadable file also exits 1. A last line `FAILED <reason>` means stop and report the reason.

## 1. Choose the query from the question

| The user asks | Run |
|---|---|
| Why is this package or assembly here, what pulls it in | `why <name> [<name> ...]` |
| Everything one project depends on, direct and transitive | `why --all --project "<project>"` |
| Which projects use this assembly, and through what | `where <assembly>` |
| Do versions or binding redirects disagree, is an upgrade safe | `conflicts` |
| A diagram of the solution's dependencies | `graph` |

Run the chosen query from the solution's directory, adding `--root "<solution or directory>"` when the solution is elsewhere or the directory holds more than one, and `--project "<project>"` to narrow any query to one project:

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/dependency_graph.py" why "<name>"
```

Pass the names as the user wrote them; matching ignores case. A name may be a package id or an assembly name.

## 2. Read the answer

- `PATH <project> <frameworks> <name> <source> <chain>` is one chain from the project to the name. `<frameworks>` names the restore's target frameworks that hold the line, joined by `;`, or `-` for a project with no restore; a name reached under one framework only, through a package, assembly, or project reference that holds for it alone, is labelled with it. `source` says who answered: `sdk` is `dotnet nuget why` on a restored SDK-style project, and `graph` is the script's own walk, which answers every project without the SDK and adds the chains the SDK cannot see, such as one ending in an assembly or a referenced project, or passing through a packages.config project. A route both find is one `sdk` line at the version the project resolves, and a hop `<name> (v<version>, graph v<other>)` adds the version a referenced project's own restore resolves, several joined by `;`. `conflicts` reports that split; whether `dotnet` is installed changes only which source a route names and which version leads. `SDK_FAILED <project> <name> <reason>` means the SDK was asked and failed, even after printing part of an answer, so the walk answered that project. `MISSING <name>` means no project reaches the name: check the spelling against a `PROJECT` line's project before concluding it is unused.
- `CONFLICT <name> <kind> <version> <project> <frameworks> <via>` lines come in groups of two or more versions of one name. A split between projects means the build picks one assembly at run time and the others' callers may fail with a missing-method or load error; a split inside one project, direct against transitive, is the same risk on one project. Two frameworks of one project build apart, so a version split between them alone is not a conflict. `REDIRECT_STALE` names a binding redirect whose `newVersion` is not an assembly version the project references, so the redirect sends callers to a version that is not there; `REDIRECT_UNREFERENCED` names a redirect for an assembly the project no longer references, which is clutter rather than a fault.
- `UNREADABLE <file> <reason>` means that file's facts are missing from every line after it, never that the answer is wrong elsewhere. Report the file and the reason with the answer, and say the answer is partial for the projects that file belongs to; an unreadable solution file lists no project, so the answer is empty because of it. A reason naming a condition, or a name the script cannot settle, says what it did instead for the framework it names: an item left out, an `Update`, a `Remove`, or a metadata value such as a `Version` not applied. A reason naming an `Import` means the file it names was not read, so the properties after it are unsettled. A reason naming an item inside a `Target` means that item, which MSBuild adds only when the target runs, is missing from every framework. A file refused for a `DOCTYPE` or `ENTITY` declaration is one the script will not parse by design; the user fixes the file, not the script.
- Each `PROJECT` line's third field says where a project's package-to-package edges came from: `assets` (a restore), `nuspec` (the packages folder or the NuGet cache), or `direct` (neither was found, so only the references the project file states are known). When a question turns on a transitive chain and the project reads `direct`, say so and suggest a restore.

## 3. Report

Answer the question in the user's terms from the lines, naming the projects and the versions. Quote a chain as the `PATH` line shows it. Do not edit a project file, `packages.config`, or a config file to resolve what you found: say what would resolve it and let the user decide.
