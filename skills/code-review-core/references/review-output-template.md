# Code Review — {owner}/{repository}#{pull-number}

The canonical Markdown projection is rendered by `scripts/review_records.py`. This file documents the stable human-facing sections without duplicating executable rendering logic.

- Header table: title, base branch, URL, review time (UTC), and calculated verdict (`APPROVED`, `CHANGES REQUESTED`, or `INCOMPLETE` with the changed files that were not reviewed in full); a re-review adds the ledger's open, flagged, addressed, and unverified counts
- Summary, one labelled paragraph per specialist when several ran
- Findings in collapsible `MUST FIX`, `SHOULD FIX`, and `SUGGESTIONS` sections: every open or unverified ledger entry, labelled with the version and ID that raised it (`v1 F001`), with this review's repeats nested inside; in a re-review each entry says what this review found, and a collapsed `Addressed since v<n>` section lists closed entries with their rationale
- Review Comments table when open review comments were given dispositions
- Reviewers table, showing a configured display name in place of a model identifier
- Review Details table: mode, adapter, reviewer, any mapped model identifiers, immutable SHAs, and the record payload hash
- Final hidden `<!-- reviewed_head_sha: ... -->` compatibility marker

The adjacent JSON record is the machine source of truth. Both files carry hashes that allow `validate_record_pair` to reject mismatched or partially copied artifacts.
