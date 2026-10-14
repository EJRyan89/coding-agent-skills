# Security policy

## Supported versions

`main` is the supported line. Releases are checkpoints: tags on `main` that are not patched separately, so a security fix lands on `main` and is carried into the next release. `update-coding-agent-skills` keeps an installation on `main` and stops at a release that raises the major version until the user accepts it. It also stops at an installation pinned with `deploy.py --at`, which stays at its pinned commit, security fixes included, until the user starts the update skill with `--unpin` or, after updating the clone, runs `python deploy.py --all --unpin`, because `deploy.py` refuses to replace the pin without `--unpin`. Update, or unpin and update, before reporting an issue that may already be resolved.

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

When a tagged release carried the defect, its advisory is published once the fix is on `main` and the next tag carries it, and names that tag as the first fixed release. A defect that entered and left `main` between two tags is fixed on `main` and its draft advisory is closed without publication: no release carried it, so an advisory would name nothing a user can act on, and the fix's commit on `main` is the public record. Reporters who want attribution are credited in either case, in the published advisory or in the fix's commit and pull request.

## Security-sensitive areas

Reports are especially useful when they involve:

- path traversal, reparse points, symlinks, or writes outside an intended deployment root;
- configuration parsing or template rendering that permits command execution or injection;
- ownership, collision, rollback, locking, or recovery behavior that can overwrite or lose user files;
- trust-boundary failures between pull-request content and trusted reviewer instructions;
- reviewer isolation that exposes ambient configuration, credentials, or unintended write access; or
- secret disclosure through logs, generated files, archives, or diagnostics.

Non-sensitive defects and feature requests go through normal GitHub issues.
