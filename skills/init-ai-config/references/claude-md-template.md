# CLAUDE.md

<!-- Guidance: Replace {placeholders} with repo-specific values.
     Sections marked [Universal] are expected in every CLAUDE.md.
     Sections marked [If applicable] should be included only when relevant.
     Change command-template fences from text to the appropriate executable
     language after every placeholder in the block has been replaced.
     Delete guidance comments before committing. -->

This file provides authoritative guidance to AI coding agents working with code in this repository.

## Overview
<!-- [Universal] One paragraph: what the repo is, who it's for, what it produces. -->

{repo_description}

## Build and Test Commands
<!-- [Universal] Exact shell commands with flags. Run from the repository root. -->

```text
# Build
{build_command}

# Run all tests
{test_command}

# Run a single test
{single_test_command}

# Verify formatting
{format_check_command}

# Fix formatting
{format_fix_command}
```

## Formatting Rules
<!-- [Universal] Reference the canonical config file (.editorconfig, .prettierrc, etc.).
     State zero-tolerance rules prominently. -->

- Line endings: {line_endings}
- Indentation: {indent_style}, size {indent_size}
- Encoding: {encoding}
- See `{formatting_config_file}` for the full set

## Architecture
<!-- [If applicable] Class hierarchies, key abstractions, design patterns.
     Include only when the codebase has non-obvious structure that agents need to navigate. -->

{architecture_description}

## Project Layout
<!-- [Universal] Table of directories/projects and their purpose. -->

| Directory | Purpose |
|---|---|
| {dir_1} | {purpose_1} |
| {dir_2} | {purpose_2} |

## Test Conventions
<!-- [Universal] Framework, directory structure, base classes, assertion patterns. -->

- Test framework: {test_framework}
- Test directory: {test_directory}
- {additional_test_conventions}

## Creating a New {primary_artifact}
<!-- [If applicable] Step-by-step for the primary "new thing" workflow.
     Consider creating a skill instead if the workflow is complex. -->

1. {step_1}
2. {step_2}

## Performance Guidance
<!-- [If applicable] Hot-path rules, what to avoid. -->

{performance_guidance}

## Maintaining AI Agent Config
<!-- [Universal when multi-runtime] How to regenerate derived files.
     Include the exact command and explain what it does. -->

When CLAUDE.md or a canonical skill changes, regenerate the derived files before committing — the CI parity check will fail if generated content drifts:

```bash
python .github/scripts/ai_config.py --write
```

## Local Validation Before Pushing
<!-- [Universal] Pre-push commands that mirror CI checks. -->

```bash
# AI config parity
python .github/scripts/ai_config.py --check

# {additional_local_checks}
```

## MCP Tools
<!-- [If applicable] Table of tools with install command and when to use each.
     Classify each tool by risk: read-only, mutating, or destructive. -->

Install dependencies: `{mcp_install_command}`

| Tool | Purpose | Risk |
|---|---|---|
| {tool_name} | {tool_purpose} | {read-only|mutating|destructive} |

## CI / Quality Gates
<!-- [Universal] Pipeline expectations, coverage thresholds, PR conventions. -->

- CI runs: {ci_checks}
- Coverage threshold: {coverage_threshold}
- PR title convention: {pr_convention}

> **Never disable or suppress** {enforced_tool} rules. If the codebase triggers a rule, fix the code.

> **Never work around a failing check.** Do not add skip or expected-failure markers, weaken or disable a gate, or leave TODO or placeholder comments to land partial work. Finish the change so every check passes, or leave it uncommitted.

## Resource Allocation
<!-- [If applicable] Conflict-avoidance for shared sequential resources
     (IDs, ports, tokens) when parallel agents are expected. -->

{resource_allocation_strategy}
