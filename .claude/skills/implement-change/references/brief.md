## Rules every fix session meets

Each rule below is held by the document it names; read that section when a rule is unclear.

- **Where you write.** Edit only by paths inside your own worktree, never in the main checkout, and deploy only into
  a throwaway home with `--canary-home` (*Worktree* in `docs/implementing-changes.md`, "Parallel sessions" in
  `CLAUDE.md`).
- **Tests first.** Show each new check failing on the code before the change, then passing with it, and run each new
  regression test once more with the fix reverted.
- **Local checks**, from *Validation* in `docs/implementing-changes.md`. Never run the full validation locally:
  continuous integration is the gate.
  - each suite the change adds, changes, or reaches, one at a time:
    `python -B tests/run_validation.py -k '<suite path>'`
  - the policy checks: `python -B tests/run_validation.py -k Policies`
  - format and lint: `python -B tests/run_validation.py -k ruff`
  - the type checks: the `RUN` line that
    `python -B .claude/skills/implement-change/scripts/pull_request.py type-checks --base origin/main` prints, run as
    printed
  - the title and the body, given alone:
    `python -B tests/run_validation.py --pr-title '<title file>' --pr-body '<body file>'`
- **Platforms.** A test whose result depends on the platform, such as path separators, links, junctions, file modes,
  case folding, or line endings, says which platforms it holds on, since continuous integration runs Windows, Linux,
  and macOS; a POSIX symbolic link test has its Windows counterpart or a skip that reads the platform alone. A test
  that compares bytes writes its fixture with `write_bytes`.
- **Contract changes.** A new or changed refusal, `FAILED` line, argument, status line, or exit code is a contract
  change: state it under `## Upgrade note` (*Contract files* and *Pull requests* in `docs/implementing-changes.md`).
- **Stated limits.** A check's miss that no repository code writes becomes a stated-limit line in the same pull
  request, not an issue; file any other finding outside the scope as an issue from its template.
- **Prompts.** A change that writes a prompt, such as a `SKILL.md` step, a reviewer's instructions, or a brief, meets
  the `## Prompt patterns` of `docs/lessons.md`: each check it asks for names its command, and each rule its step
  must meet is stated or pointed at.
- **The pull request.** A title of the type the change fits, with a scope; one `Closes #<number>` line of its own for
  each issue it resolves; a `## Forms considered` section, or a `Siblings:` line, naming each lessons pattern and
  sibling this brief lists and what became of it. Fetch and rebase onto `origin/main` before opening it; once it is
  open, merge `origin/main` into it, never rebase or force-push it, and report a failed check instead of rerunning it.
