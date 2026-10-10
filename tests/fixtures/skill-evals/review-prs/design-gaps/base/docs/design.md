# Design

How the reports service is built, and the rules every change keeps.

## Architecture

- The web service serves the report pages and the API.
- The export worker builds each export on the job runner, writes its file to object storage, and records it in the `exports` table.
- Background jobs run on the job runner.

## Invariants

- Every deletion of customer data is written to the audit log before the data is removed, naming the account, the item, and the reason.
- A background job is idempotent: running it twice leaves the same state as running it once.
