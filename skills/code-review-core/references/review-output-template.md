# Code Review — {owner}/{repository}#{pull-number}

The canonical Markdown projection is rendered by `scripts/review_records.py`. This file documents the stable human-facing sections without duplicating executable rendering logic.

- Header table: title, base branch, URL, review time (UTC), and calculated verdict (`APPROVED`, `CHANGES REQUESTED`, or `INCOMPLETE` with the changed files that were not reviewed in full)
- Summary, one labelled paragraph per specialist when several ran
- Findings in collapsible `MUST FIX`, `SHOULD FIX`, and `SUGGESTIONS` sections, each finding keyed by its stable ID
- Prior Findings Status table for re-reviews
- Review Details table: mode, adapter, reviewer, immutable SHAs, and the record payload hash
- Final hidden `<!-- reviewed_head_sha: ... -->` compatibility marker

The adjacent JSON record is the machine source of truth. Both files carry hashes that allow `validate_record_pair` to reject mismatched or partially copied artifacts.
