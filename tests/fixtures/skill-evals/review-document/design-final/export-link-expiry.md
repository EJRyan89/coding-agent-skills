# Export link expiry

## Context

Customers share exports through links the export service creates. Each link is a random 32-byte token stored beside the export's record, and opening it streams the export read-only.

## Requirements

- A shared link expires 24 hours after it is created.
- An expired link returns HTTP 410 and never streams the export.
- The export's owner can revoke a link at any time, and a revoked link behaves as an expired one.

## Design

The link record holds its token, its export, its creation time, and a revoked flag. Opening a link loads the record by its token and refuses it when it was created more than 24 hours ago or its revoked flag is set.

A nightly job deletes expired and revoked link records once they are TBD days old.

## Alternatives

Signed URLs from the storage service were rejected because they cannot be revoked before they expire.

## Rollout

The revoked flag is a new column, added with a default of false before the code that reads it ships; rolling the code back leaves the column unused.
