# Retention for report exports

Status: proposed

## Context

Customers export reports as CSV files, which the export worker writes to object storage and records in the `exports` table. Nothing deletes them: storage holds every export since launch, and its cost grows each month.

## Requirements

- R1. An export is deleted when its retention period ends. The period depends on the account's plan, as the table below gives it.
- R2. A customer can download an export until it is deleted.
- R3. Every deletion is written to the audit log before the export is removed, as `docs/design.md` requires.

| Plan | Retention |
| --- | --- |
| Free | 30 days |
| Team | 90 days |
| Enterprise | 365 days |

## Design

A nightly sweep job on the job runner selects the exports whose retention period has ended, writes one audit entry for each, deletes its object from storage, and then deletes its row. A row whose object is already gone is deleted without an error, so a sweep that stops partway and runs again leaves the same state as one that ran once.

Enterprise exports are kept for 90 days, the same as Team, so the sweep needs only two retention periods.

The sweep skips every account with a legal hold.

## Alternatives considered

- Object storage lifecycle rules, which expire objects by age inside the bucket. Rejected.

## Rollout

The sweep ships disabled behind the `export_retention` setting. It is first enabled in report-only mode, which for one week logs what it would delete and deletes nothing, and then enabled for deletion. Turning the setting off stops the sweep.

## Open questions

- Should a deleted export stay recoverable for 7 days, by moving it to a holding bucket instead of deleting it? This decides whether the sweep deletes objects or moves them.
