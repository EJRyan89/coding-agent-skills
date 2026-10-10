# Upgrade-notes reviewer

Your files include a contract: a file "Versioning" in TRUSTED_ROOT/docs/releasing.md lists as one, or a contract that has no file (a skill's arguments or the status lines an agent parses, in its `SKILL.md`; a deployer flag in `deployer/arguments.py`). Read that section only. TRUSTED_ROOT/docs/design.md "The structured record is the contract" says why these are what a version number answers for. Validation already fails when a contract file's value changed and no new entry names it, so judge what it cannot.

Ask of each change:

1. **An entry for each contract change.** `docs/upgrade-notes.md` gains an entry under `## Unreleased` for each contract item the change alters, including an argument, status line, or flag, which no check holds. A status line or exit code is a contract only when the skill's `SKILL.md` tells the agent to act on it; a value no step names, or an exit status the skill says not to act on, is behavior. An edit that leaves the contract alone, such as wording in a contract file or a behavior change, needs none: say nothing rather than ask for one.
2. **Its level.** The level is the largest demand the change makes on a user, in Versioning's words; before `1.0.0` a change that would be major is carried in a minor and the entry says so.
3. **The user action.** It names what a user must do after updating (a field to add, a file to rewrite), or `none` when that is true.
4. **It describes the change.** The entry says what the code now does, in the terms a user meets.

Anchor a missing entry on the added line of the contract change it is missing for.

Categories: `Missing upgrade note` for an absent entry or a wrong level or action; `Code-document drift` when the entry misdescribes the change.

Severity: **MUST_FIX** for a change to a durable record or a user-authored file with no migration and no stated manual step; **SHOULD_FIX** for a missing entry, a wrong level, or a wrong user action; **SUGGESTION** for wording.
