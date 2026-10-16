# .NET skills

The `dotnet` bundle installs two skills for C# solutions together:

- [`dotnet-format`](skills.md#dotnet-format) runs formatting, style, and analyzer checks, and the region layout
  checks, on the C# files changed on the current branch, and fixes them when asked.
- [`dotnet-dependencies`](skills.md#dotnet-dependencies) answers why a package or assembly is in a solution, which
  projects use an assembly, and which versions and binding redirects conflict, from a read-only graph of the
  solution's project files.

Each skill's section in the [skill reference](skills.md) describes its commands and output. This page describes what
the two share.

## Selecting the bundle

The bundle is opt-in: `python deploy.py --all` leaves it out. Add it with `--include dotnet`, or choose it from the
menu `python deploy.py` shows:

```bash
python deploy.py --all --include dotnet
```

Choosing either skill installs both, and once chosen the bundle stays installed on later `--all` runs.
[Deploying](installation.md#deploying) explains how an opt-in bundle stays chosen.

## Which projects the skills see

Both skills read a solution's project list through one reader, `dotnet_solution.py` in skill-core, so they list a
solution the same way and judge its entries by one rule:

- A solution is a `.sln`, whose `Project(...)` lines name the projects, or a `.slnx`, whose `Project` elements do,
  read whatever its encoding. A `.slnx` that declares a `DOCTYPE` or an entity is refused before it is parsed.
- A path a solution's entry or a project's `ProjectReference` names is made absolute from its text alone, without
  reaching the file system.
- An entry that lies outside the tree the skill works in, or that names a network share (`\\host\share` or `//host`)
  or a device (`\\?\` or `\\.\`), is refused and never opened or looked up, so a path a repository writes cannot make
  Windows contact another host as you. `dotnet-format` reports it as a `REFUSED` line; `dotnet-dependencies` as an
  `UNREADABLE` line naming the path and the reason.
- A solution, project, or settings file is read only when it is a regular file no larger than `READ_LIMIT_BYTES` in
  `dotnet_solution.py`; a larger file or a device is refused unread.
- A search of the repository for solutions or projects skips version-control, dependency, build-output, and Visual
  Studio folders, NuGet's `packages` folder, and nested checkouts, and does not follow a junction or symbolic link.

The tree differs by skill. `dotnet-format` works in the repository it formats, so the tree is the repository root
git names, and every file it formats lies under it. `dotnet-dependencies` examines the checkout holding the solution
or the directory searched, the nearest folder at or above it holding `.git`, else that directory, since a solution
in a subfolder routinely names projects in sibling folders of the same checkout. Its searches for
`Directory.Build.props`, `Directory.Build.targets`, `Directory.Packages.props`, and the `packages` folder start only
from a project it accepted, and a file one of them finds is read only inside that tree.

## Requirements

`dotnet-format` needs the .NET SDK and the `dotnet-format` global tool. It runs the SDK's `dotnet format` on a `.slnx`
solution, which the global tool cannot open, and on a `.sln` whose projects are all SDK-style, and the global tool on
any other `.sln`; a `.slnx` therefore needs an SDK at the floor that reads it. `dotnet-dependencies` reads project
files on its own and uses the SDK, when present, for `dotnet nuget why`. `python deploy.py check` reports both tools
and their floors, and [Requirements](installation.md#requirements) lists them with their install commands.
