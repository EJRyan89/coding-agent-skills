# Export retention

`tools/export_sweep.sh` deletes the report exports in a folder that are older than 30 days.

Run `bash tools/export_sweep.sh <folder> --dry-run` first: it deletes nothing and prints
`WOULD_DELETE <path>` for each export it would delete. Without `--dry-run`, it prints
`DELETED <path>` for each export it deletes.
