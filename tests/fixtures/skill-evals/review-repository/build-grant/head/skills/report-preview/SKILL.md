---
name: report-preview
description: >-
  List the reports a repository's site would publish, and build the site to preview them. Use it when asked
  which reports the site shows or how they look.
allowed-tools: ["Bash(python -B scripts/*)", "PowerShell(python -B scripts/*)"]
---

# Report preview

1. Run `python -B scripts/collect.py '<repository>'`. It prints one `REPORT <path>` line per report under the
   repository's `reports/` folder.
2. When asked how a report looks, run `python -B scripts/render_preview.py '<repository>'`. It builds the site with
   the build script the repository's `package.json` names and prints `PREVIEW <folder>` for the built site, or
   `NO_BUILD` when the package names none.
3. Answer with those paths.
