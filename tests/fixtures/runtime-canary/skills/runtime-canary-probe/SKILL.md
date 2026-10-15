---
name: runtime-canary-probe
description: "Test fixture for this repository's runtime canary. Use it only when a prompt names it."
allowed-tools: ["Bash({{PYTHON}} -B \"${CLAUDE_SKILL_DIR}/scripts/canary_probe.py\" *)", "PowerShell({{PYTHON}} -B \"${CLAUDE_SKILL_DIR}/scripts/canary_probe.py\" *)"]
---

# Runtime canary probe

Run this command with the arguments you were given, then report the line it prints:

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/canary_probe.py"
```

Do nothing else.
