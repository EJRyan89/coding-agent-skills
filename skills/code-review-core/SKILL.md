---
name: code-review-core
description: Internal support for the code-review operations bundle. Not intended for direct invocation.
disable-model-invocation: true
user-invocable: false
---

# Code-review core

This is a non-selectable dependency of the public code-review operations skills, which run its scripts as their own steps say. Do not invoke it as a user workflow, and never invoke Copilot directly: the pipeline's `dispatch` starts the bounded host.

Its formats, safety invariants, and design rules are in `docs/code-review-operations-contract.md` in this suite's source repository.
