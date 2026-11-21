# Code Review — example/inventory#107

| | |
|---|---|
| **Title** | Inventory change 107 |
| **Base** | `main` |
| **URL** | https://github.com/example/inventory/pull/107 |
| **Reviewed** | 02-Feb-2026 12:00 UTC |
| **Verdict** | APPROVED |

---

## Summary

Review of inventory change 107&#46;

## Findings

<details open>
<summary><strong>SHOULD FIX (1)</strong></summary>

<details open>
<summary>v1 F001. [Performance] Database call inside a loop</summary>

> **File:** `src/Reservations/ReservationSweeper.cs`  
> **Line:** 40 | **Source:** inventory&#45;review
>
> Each reservation is loaded with its own query inside the loop&#59; load them in one query&#46;
>
> **Evidence:** `src/Reservations/ReservationSweeper.cs line 40.`

</details>

</details>

## Reviewers

| Reviewer | Focus | Model | Files | Findings | Retries | Time | Files read | Searches |
|----------|-------|-------|-------|----------|---------|------|------------|----------|
| `inventory-review` | Repository | claude&#45;sonnet&#45;5&#45;5 | 3 | 1 | 0 | - | - | - |

---

<details>
<summary><strong>Review Details</strong></summary>

| | |
|---|---|
| **Mode** | initial v1 |
| **Adapter** | `inventory-review` (repository) |
| **Reviewer source** | the configured trusted ref, at `111111111111` |
| **Reviewer** | inventory&#45;review (complete) |
| **Base SHA** | `bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb` |
| **Reviewed HEAD** | `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa6b` |
| **Record payload SHA-256** | `9d5940e2b2de92dd41114ae3078e6d217d8f02bcd6a0e7598bea17ea39cae733` |

</details>

<!-- reviewed_head_sha: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa6b -->
