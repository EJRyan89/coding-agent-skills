---
name: report-preview
description: >-
  List the reports a repository's site would publish, by reading its reports folder. Use it when asked which
  reports the site shows.
allowed-tools: ["Bash(python -B scripts/collect.py *)", "PowerShell(python -B scripts/collect.py *)"]
---

# Report preview

1. Run `python -B scripts/collect.py '<repository>'`. It prints one `REPORT <path>` line per report under the
   repository's `reports/` folder.
2. Answer with those paths.
