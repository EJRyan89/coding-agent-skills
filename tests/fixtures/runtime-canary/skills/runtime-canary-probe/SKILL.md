---
name: runtime-canary-probe
description: "Test fixture for this repository's runtime canary. Use it only when a prompt names it."
allowed-tools: ["Bash(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "PowerShell(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)"]
---

# Runtime canary probe

Run this command with the arguments you were given, then report the line it prints:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/canary_probe.py"
```

Do nothing else.
