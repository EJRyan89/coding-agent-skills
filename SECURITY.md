# Security policy

## Supported versions

Only the latest release is supported, and security fixes land on the default branch and in the next release. Upgrade to the latest release before reporting an issue that may already be resolved.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use [GitHub private vulnerability reporting](https://github.com/EJRyan89/coding-agent-skills/security/advisories/new).

Include enough information to reproduce and assess the issue:

- affected version or commit;
- relevant operating-system and tool versions;
- minimal reproduction steps;
- expected and observed behavior;
- potential impact and required preconditions; and
- any suggested mitigation, if known.

Avoid including credentials, tokens, personal configuration, private repository contents, or other sensitive data. Use sanitized fixtures whenever possible.

The maintainer will assess the report, coordinate any fix and disclosure, and credit reporters who want attribution. No response or remediation deadline is guaranteed.

## Security-sensitive areas

Reports are especially useful when they involve:

- path traversal, reparse points, symlinks, or writes outside an intended deployment root;
- configuration parsing or template rendering that permits command execution or injection;
- ownership, collision, rollback, locking, or recovery behavior that can overwrite or lose user files;
- trust-boundary failures between pull-request content and trusted reviewer instructions;
- reviewer isolation that exposes ambient configuration, credentials, or unintended write access; or
- secret disclosure through logs, generated files, archives, or diagnostics.

Non-sensitive defects and feature requests go through normal GitHub issues.
