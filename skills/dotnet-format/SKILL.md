---
name: dotnet-format
description: "Run dotnet format (whitespace + style + analyzers) and region layout checks on C# files changed on the current branch and optionally auto-fix violations. Use it when asked to format or style-check C# changes before a commit or pull request."
allowed-tools: ["Bash(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "PowerShell(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "Read", "AskUserQuestion"]
---

Check the C# files changed on the current branch with the `dotnet-format` global tool, or the .NET SDK's `dotnet format` for a `.slnx` solution (whitespace, code-style, and analyzer rules the repository configures) and with this skill's region layout checker, then offer to fix what they find. Every step is one script command, run exactly as shown. Each prints one tab-separated fact per line. Do not read the changed files, count braces, or edit source files yourself: the tools find and fix every violation. Act on the printed lines, not the exit code: a command that reports findings also exits 1. A last line `FAILED <reason>` means stop: read the end of the `LOG` file, when one was printed, and report the reason, the cause the log shows, and its path.

## Steps

1. **Resolve the targets** from the directory the user invoked the skill in:

   ```bash
   python -B "${CLAUDE_SKILL_DIR}/scripts/dotnet_format_targets.py" resolve
   ```

   `STOP <reason>` means there is nothing to format: report the reason and stop. Report a `FETCH_FAILED <reason>` line (the base is origin as last fetched, so it may be stale) and go on. Report each `SKIPPED_ASPNET <file> <project>` line (ASP.NET projects crash the formatter) and each `OUTSIDE_SOLUTION <file>` line (no project of the chosen solution owns it, so the formatter skips it; only the layout checker sees it). Keep `REPO_ROOT`, `SOLUTION`, and `FILE_LIST` for the next steps.

2. **Check the layout analyzer configuration:**

   ```bash
   python -B "${CLAUDE_SKILL_DIR}/scripts/csharp_layout.py" config --repo-root "<REPO_ROOT>" --solution "<SOLUTION>" --file-list "<FILE_LIST>"
   ```

   Each setting is judged as the changed files see it, so path-specific sections count. If any `SETTING` line says `missing`, list those settings and call `AskUserQuestion` with **Add settings** / **Skip**. On **Add settings**, rerun the command with `--apply` and report its `ADDED` lines; it adds the missing settings as the first section of the outermost `.editorconfig`, so every existing section still overrides them. A `SETTING` state of `other:<value>` is the repository's own choice: report it, never change it. If `PACKAGE Roslynator.Formatting.Analyzers missing`, tell the user the `RCS*` layout rules run only once the solution references that NuGet package (for example a `PackageReference` with `PrivateAssets="all"` in `Directory.Build.props`, followed by a restore or build, since the formatter runs with `--no-restore`). Never add the package yourself.

3. **Check.** Run both commands from step 1's values, giving the formatter run a 600000 ms timeout:

   ```bash
   python -B "${CLAUDE_SKILL_DIR}/scripts/run_dotnet_format.py" check --repo-root "<REPO_ROOT>" --solution "<SOLUTION>" --include-file "<FILE_LIST>"
   python -B "${CLAUDE_SKILL_DIR}/scripts/csharp_layout.py" check --repo-root "<REPO_ROOT>" --file-list "<FILE_LIST>"
   ```

   Each prints `DIAGNOSTIC` or `VIOLATION` lines and a `SUMMARY <count> <files> <rules>` line; the formatter also prints `LOG <path>` with its full output.

4. **If both summaries count 0**, report "no formatting issues found on changed files" and stop. Otherwise report the counts, files, and distinct rules, and call `AskUserQuestion` with **Fix** / **Skip**. `region-scope` violations are report-only: tell the user which `#endregion` to move into the scope where its `#region` opened.

5. **If Fix**, run the step 3 formatter command with `fix` in place of `check` (same timeout) and the layout command with `--fix` added, then rerun both step 3 commands once to re-verify. Report what remains, with the `LOG` path if the formatter still reports diagnostics; do not loop.

## Important

- Do **not** stage or commit fixed files; leave them in the working tree for the developer to review.
