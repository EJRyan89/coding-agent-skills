# Export sharing

## Context

Customers download exports today and forward the files by email. This design lets a customer share an export through a link instead, so the file never leaves the export store.

## Requirements

- A shared link opens one export, read-only, for anyone who holds the link.
- A shared link expires 24 hours after it is created.
- Requirements for audit logging of link use will be stated in a later revision.

## Design

The export service creates a link as a random 32-byte token stored beside the export's record. Opening the link looks up the token, checks that it has not expired, and streams the export.

A shared link stays valid for 7 days, so a recipient who is away for a few days can still open it.

## Alternatives

To be written.

## Rollout

To be written.

## Open questions

- Should the owner be able to revoke a link before it expires?
