"""Record what each validation job took on CI's runners, from a completed validate run's logs.

Usage:
  python tools/record_job_seconds.py [RUN_ID]

tests/run_validation.py deals its jobs across the validate workflow's legs by tests/validation/job_seconds.json, so
each leg gets about the same seconds of work. This script rewrites that table from one run: it reads the logs of
every leg through `gh run view --log`, takes each `ok <seconds>s <job>` line the runner printed, and sums a suite's
shards under the suite's path. A job a run reports more than once, as a weekly run's second interpreter and second
validation step do, counts at its mean. RUN_ID defaults to the latest successful push run of validate.yml on main, and
a run that is not a completed, successful run of the Validate workflow is refused, since a failed or cancelled run
leaves jobs out. The release procedure in docs/releasing.md runs it before tagging, so the legs are rebalanced once a
release and never by a pull request's live timings. It exits 0 when it wrote the table and 2 when it could not read
the run.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import sys
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "skill-core" / "scripts"))

from console import use_utf8_output
from github_client import GitHubClient, GitHubError, subprocess_runner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TABLE = REPOSITORY_ROOT / "tests" / "validation" / "job_seconds.json"
WORKFLOW_FILE = "validate.yml"
WORKFLOW_NAME = "Validate"
# What job_pool.py prints for each job that passed, after the timestamp gh puts before every log line.
OK_LINE = re.compile(r"^\S+ ok +(\d+(?:\.\d+)?)s (.+?)\s*$")
SHARD_SUFFIX = re.compile(r" \[shard \d+/\d+\]$")


def job_seconds_from_log(log: str) -> dict[str, float]:
    """Each job's seconds by name from `gh run view --log` output, whose lines are `<leg>\\t<step>\\t<text>`."""
    reported: defaultdict[str, list[float]] = defaultdict(list)
    for line in log.splitlines():
        fields = line.split("\t", 2)
        found = OK_LINE.match(fields[-1]) if len(fields) == 3 else None
        if found:
            reported[found.group(2)].append(float(found.group(1)))
    totals: defaultdict[str, float] = defaultdict(float)
    for label, seconds in reported.items():
        totals[SHARD_SUFFIX.sub("", label)] += sum(seconds) / len(seconds)
    return {name: round(seconds, 1) for name, seconds in totals.items() if round(seconds, 1) > 0}


def render_table(run: int, seconds: Mapping[str, float]) -> str:
    """The table's text: the run it came from and the seconds by job name, in name order."""
    return json.dumps({"run": run, "seconds": dict(sorted(seconds.items()))}, indent=2) + "\n"


def latest_run(client: GitHubClient) -> int:
    runs = client.json(
        [
            "run",
            "list",
            "--workflow",
            WORKFLOW_FILE,
            "--branch",
            "main",
            "--event",
            "push",
            "--status",
            "success",
            "--limit",
            "1",
            "--json",
            "databaseId",
        ]
    )
    if not runs:
        raise GitHubError(f"no successful push run of {WORKFLOW_FILE} on main was found", kind="not_found")
    return int(runs[0]["databaseId"])


def run_log(client: GitHubClient, run: int) -> str:
    """The run's logs, once it is known to be a completed, successful run of the Validate workflow."""
    state = client.json(["run", "view", str(run), "--json", "workflowName,status,conclusion"])
    found = (state.get("workflowName"), state.get("status"), state.get("conclusion"))
    if found != (WORKFLOW_NAME, "completed", "success"):
        raise GitHubError(
            f"run {run} is {found[0]!r}, {found[1]}, {found[2] or 'no conclusion'}; record from a completed, "
            f"successful {WORKFLOW_NAME} run",
            kind="refused",
        )
    return client.run(["run", "view", str(run), "--log"]).stdout


def main(arguments: list[str] | None = None, client: GitHubClient | None = None, table: Path = TABLE) -> int:
    parser = argparse.ArgumentParser(description="Record each validation job's seconds from a validate run's logs.")
    parser.add_argument("run", nargs="?", type=int, help="the run's ID; the latest successful main push run if omitted")
    options = parser.parse_args(arguments)
    client = client or GitHubClient(runner=functools.partial(subprocess_runner, cwd=REPOSITORY_ROOT))
    try:
        run = options.run if options.run is not None else latest_run(client)
        seconds = job_seconds_from_log(run_log(client, run))
    except GitHubError as error:
        print(f"Cannot read the validate run: {error}", file=sys.stderr)
        return 2
    if not seconds:
        print(
            f"Run {run} reported no passing jobs; run validation with -v, as the validate workflow does",
            file=sys.stderr,
        )
        return 2
    table.write_text(render_table(run, seconds), encoding="utf-8", newline="\n")
    print(f"Recorded {len(seconds)} jobs, {sum(seconds.values()):.1f} job-seconds, from run {run} in {table.name}.")
    return 0


if __name__ == "__main__":
    use_utf8_output()
    sys.exit(main())
