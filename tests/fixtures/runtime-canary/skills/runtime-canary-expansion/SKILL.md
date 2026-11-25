---
name: runtime-canary-expansion
description: "Test fixture for this repository's runtime canary: whether Claude Code asks before a granted command that expands a shell variable. Use it only when a prompt names it."
allowed-tools: ["Bash({{PYTHON}} -B \"${CLAUDE_SKILL_DIR}/scripts/expansion_probe.py\" *)", "PowerShell({{PYTHON}} -B \"${CLAUDE_SKILL_DIR}/scripts/expansion_probe.py\" *)"]
---

# Runtime canary expansion probe

Run each of these two commands once, in this order, each on its own and exactly as written, then report the line each prints:

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/expansion_probe.py" literal
```

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/expansion_probe.py" "$PWD"
```

If a command is denied, do not run it again or in another form. Do nothing else.
