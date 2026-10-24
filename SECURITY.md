# Security policy

## Supported versions

An installation follows releases: tags on `main`, a release marked as a pre-release included, that are not patched separately, so fixes, security fixes included, land on `main` and reach users at the next release tag. `update-coding-agent-skills` deploys the latest release by default; following `main` itself, where a fix arrives as soon as it merges, is the opt-in, with `UPDATE_CHANNEL=main` in `python deploy.py configure` or the skill's `--channel main`. The update stops at a release that raises the breaking component of the version until the user accepts it: the minor version before `1.0.0`, and the major after it, as [Versioning](docs/releasing.md#versioning) says. It also stops at an installation the user pinned with `deploy.py --at`, other than its own pin at a release, which stays at its pinned commit, security fixes included, until the user starts the update skill with `--unpin` or, after updating the clone, runs `python deploy.py --all --unpin`, because `deploy.py` refuses to replace the pin without `--unpin`. Update, or unpin and update, before reporting an issue that may already be resolved; one fixed on `main` that no release carries yet is resolved for an installation that follows `main`, and for the rest at the next tag.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use [GitHub private vulnerability reporting](https://github.com/EJRyan89/coding-agent-skills/security/advisories/new).

Include enough information to reproduce and assess the issue:

- the output of `python deploy.py check`, which names the deployed commit, or the affected commit;
- relevant operating-system and tool versions;
- minimal reproduction steps;
- expected and observed behavior;
- potential impact and required preconditions; and
- any suggested mitigation, if known.

Avoid including credentials, tokens, personal configuration, private repository contents, or other sensitive data. Use sanitized fixtures whenever possible.

The maintainer will assess the report, coordinate any fix, and disclose it as [Disclosure](#disclosure) says. No response or remediation deadline is guaranteed.

## Disclosure

When a tagged release carried the defect, its advisory is published once the fix is on `main` and the next tag carries it, and names that tag as the first fixed release. A defect that entered and left `main` between two tags is fixed on `main`, and its draft advisory is closed without publication at the release whose tag carries the fix: no release carried it, so an advisory would name nothing a user can act on, and the fix's commit on `main` is the public record. Reporters who want attribution are credited in either case, in the published advisory or in the fix's commit and pull request.

## Security-sensitive areas

Reports are especially useful when they involve a boundary the [trust model](docs/design.md#trust-model) places, each named here in bold as the trust model names it, or one of the areas after them:

- **A pull request's or document's author**: content of a pull request, or of a document given to `review-document` for a standalone review, that reaches trusted reviewer instructions, is followed as an instruction, or escapes the review snapshot's rules. A fixture is trusted input only as the checkout that holds it is.
- **The PDF parser**: a PDF's bytes reaching `pdftotext` without the version check passing, or its output accepted past the size, time, or encoding limits.
- **Authored upgrade notes**: a pull request's body reaching executable script text in continuous integration, or the release notes other than as authored Markdown.
- **What a skill may run**: a skill that runs a command beyond the standard set and the tools it declares, or writes inside a deployed skill directory.
- **A repository a skill examines**: a skill that reads outside the examined tree or past its read limits, or starts a command the repository configures, such as an MCP server for `audit-ai-config`'s handshake, without the user's explicit authorization.
- **A grant**: an `allowed-tools` grant that pre-approves more than the commands its skill runs, such as a bare shell or a pattern that reaches another skill's scripts.
- **The deployer**: path traversal, reparse points, symlinks, or writes outside an intended deployment root; configuration parsing or template rendering that permits command execution or injection; and ownership, collision, rollback, locking, or recovery behavior that can overwrite or lose user files.
- **The reviewer guard**: a reviewer call outside its role that the guard allows, or a result it lets count without having claimed the role.
- **The hub guard**: an edit to a guarded hub, or a git command that moves its HEAD or writes its tree, that the guard lets through.
- **The merge shepherd**: an action of the shepherd on a pull request whose head branch is not in this repository.
- **The same user**: a process running as the user is trusted, so a report that needs one is in scope only when the suite hands it something the user did not, such as a credential written to a file.
- Reviewer isolation, under the guard or in the [bounded Copilot CLI host](docs/copilot-support.md#bounded-code-review-host), that exposes ambient configuration, credentials, or unintended write access beyond what those pages say remains.
- Secret disclosure through logs, generated files, archives, or diagnostics.

Non-sensitive defects and feature requests go through normal GitHub issues.
