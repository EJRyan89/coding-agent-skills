---
name: runtime-canary
description: >-
  Check that Claude Code, Codex, and Copilot CLI find and run deployed skills, by deploying this checkout into a
  throwaway home and starting each runtime there. Use it before a pull request that changes skill paths,
  allowed-tools, runtime adapters, or agents. Each run calls a model.
allowed-tools: ["Bash(python -B tools/runtime_canary.py*)", "PowerShell(python -B tools/runtime_canary.py*)"]
---

# Runtime canary

`tools/runtime_canary.py` deploys this checkout and the fixture skill `runtime-canary-probe` into a fresh home under the temporary directory, which `deploy.py --canary-home` allows from a worktree. Nothing is deployed anywhere else. It then starts each installed runtime with that home as its working directory, so the runtime finds the deployed skills as project skills, gives it a fixed prompt naming one skill, and prints one fact per line. The script does every deterministic step; your part is choosing what to check and saying what a failure means. Its docstring lists every output line and what the run isolates: configuration, not credentials, so the runtimes use the user's own sign-ins, and each `RUNTIME` line's `KEPT` names what still loaded.

It is a manual gate, never part of `tests/run_validation.py`: each run calls a model, and a run can fail because the model chose not to run the command.

## 1. Choose the skills

Name the shipped skills the change touched: a changed `SKILL.md`, script, frontmatter, `deploy-meta` entry, or `agent_deps`. For a change to how adapters are rendered (`deployer/render.py`), name one model-invocable skill and one user-only skill, such as `repo-cleanup`; Copilot reports the user-only one as `UNSUPPORTED` without a run. The fixture always runs first. Never name every skill: the cost is one model run per runtime per skill. If you would name more than three, ask the user which to keep.

## 2. Run it

From the worktree, with the chosen skills:

```bash
python -B tools/runtime_canary.py "<skill>" "<skill>"
```

Add `--runtime <name>` (repeatable) to check fewer runtimes, `--discovery-only` to list skills without running any model, or `--codex-sandbox unelevated` to rerun Codex in the other Windows sandbox mode after an elevated run was `BLOCKED`. The first line is `HOME "<dir>"`.

## 3. Say what each line means

One success is evidence; one failure is a question. Explain each failure from its line and, when needed, the `TRANSCRIPT` it names (its `.stderr.txt` sits beside it), before anyone changes code:

- `RAN "<script>" "<cwd>"`: the runtime ran the script from the canary's copy of the skill, or of a skill it declares in `skill_deps`. This is the pass. The installed copy never counts. The canary records Python and Bash scripts however the runtime started them, so a Bash script stopped at its usage check by `--help` still counts.
- `NOT_ATTEMPTED "<reason>"`: the skill declares `none` for that runtime in its `deploy-meta` `runtime_support`, so the canary started nothing.
- `UNSUPPORTED "copilot -p cannot start a user-only skill; ..."`: the skill's adapter lets only the user start it, and Copilot's headless mode cannot, so the canary spent no run on it. This is a known runtime limit, not a skill defect; "Headless sessions" in `docs/copilot-support.md` describes it and when to recheck it.
- `BLOCKED "<policy>"`: the runtime's own execution policy refused a command before any script ran, so the skill was never tested. A Codex policy that refuses every shell command, even reading the skill, shows up this way. The line names the Windows sandbox mode Codex ran with and points at "Windows sandbox mode" in `docs/codex-support.md`, which shows how to check the mode without a model. Report the line, not a skill defect.
- `FAILED "... denied: <tool>: <command>"`: Claude Code refused the command because the skill's `allowed-tools` does not grant it. Name the tool: on Windows the model may run the command through `PowerShell` rather than `Bash`.
- `FAILED "ran the installed copy <path>, not the canary copy"`: the runtime followed the copy under the real home. The change was not exercised; say so instead of blaming it.
- `FAILED "ran <path> instead of a script under <dir>"`: it ran a script from the wrong place, which is a path defect.
- `FAILED "ran <script> but it wrote no marker; ..."`: only the fixture says this. The path was right, but a sandbox stopped the write.
- `FAILED "cannot read what the run recorded in <file>: ..."`: the canary could not read its own marker or recorder file afterwards, so the run proves nothing either way. Check the file's permissions before rerunning.
- `FAILED "never ran a script from the skill"`: the transcript shows whether the runtime loaded the skill or the model declined, or whether the first script is in a language the canary does not record, which is anything but Python and Bash. Rerun once before treating it as a defect.
- `MATRIX <runtime> <skill> AGREES <level>` or `DISAGREES <level> "<what ran instead>"`: the run compared with the skill's declared runtime support, the table in "Runtime support" in `docs/skills.md`. `full` expects `RAN`; `partial` expects `UNSUPPORTED` where the runtime lacks `user-only-start` and `RAN` otherwise; `none` expects `NOT_ATTEMPTED`. Any `DISAGREES`, or a skill that declares nothing, makes the canary exit 1. A disagreement on a rerun-able failure is the failure above; one that persists means the declaration or the skill is wrong, so say which.
- `DISCOVERED`, `UNDISCOVERED`, and `SHADOWED`: whether the runtime lists the canary's copy, and which other copies of the same name it lists. Codex and Copilot list user-only skills too. Claude Code is listed only by a run, so `--discovery-only` prints no Claude lines.

## 4. Record the result

Copy the `RUNTIME`, `MATRIX`, `DISCOVERED`, `UNDISCOVERED`, `SHADOWED`, `SUPPLIED`, and `SKIPPED` lines into the pull request's Validation section, with each runtime's version and your reading of any failure. Then delete the `HOME` directory, unless the user wants its transcripts. The script deletes only a home it made, and prints `REMOVED` or `FAILED` with the reason:

```bash
python -B tools/runtime_canary.py --remove-home "<dir>"
```
