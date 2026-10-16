# Export retention

`tools/export_sweep.sh` deletes the report exports in a folder that are older than 30 days.

Run `bash tools/export_sweep.sh <folder> --dry-run` first: it deletes nothing and prints
`WOULD_DELETE <path>` for each export it would delete. Without `--dry-run`, it prints
`DELETED <path>` for each export it deletes.

When no folder is given, or the folder does not exist, it prints its usage and exits 1, so a scheduled run can tell
a missing folder from a sweep that found nothing to delete, which exits 0.
