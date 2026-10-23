"""Record what each validation job took on CI's runners, from several completed validate runs' logs.

Usage:
  python tools/record_job_seconds.py [--count N] [RUN_ID ...]

tests/run_validation.py deals its jobs across the validate workflow's legs on each platform by that platform's table in
tests/validation/job_seconds.json, so each leg gets about the same seconds of work; a job's cost differs between
Windows and Linux by up to an order of magnitude, so each platform's legs are dealt by their own platform's seconds.
One run's job times differ a lot from the next run's, so this script rewrites that table from several runs and takes
each job's median on each platform: it reads the logs of every leg of each run through `gh run view --log`, takes each
`ok <seconds>s <job>` line the runner printed on a leg, with the pool's note on a job it retried left off the job's
name, and sums a suite's shards under the suite's path. A Windows leg is read under its name or the one it had before
the Linux job joined the required check, and a Linux leg under its name or as the one leg of the job that ran every
job unsharded before Linux was dealt, so the median spans each rename. A job a run reports more than once on a
platform, as a weekly run's second interpreter and second validation step do, counts at its mean within that run.
RUN_ID names the runs to read; without any, the latest successful push runs of validate.yml on main are read,
DEFAULT_RUN_COUNT of them or --count. The newest run decides which jobs each platform's table names: a job it does not
report, such as a suite since renamed or removed, is left out, and a job it reports that older runs lack takes the
median of the runs that have it. A run that is not a completed, successful run of the Validation workflow is refused,
since a failed or cancelled run leaves jobs out; so is a successful run whose log names no job as a leg under any
name, one whose log lacks a platform or a leg, as a rerun of one failed leg can leave, and a run whose log gh cannot
read, such as one past the retention period. One refused run refuses the record and the table is left alone. The
release procedure in docs/releasing.md runs it before tagging, so the legs are rebalanced once a release and never by
a pull request's live timings. It exits 0 when it wrote the table and 2 when it could not read the runs.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import statistics
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

from console import run_main
from github_client import GitHubClient, GitHubError, subprocess_runner

from deployer import platform_support

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TABLE = REPOSITORY_ROOT / "tests" / "validation" / "job_seconds.json"
WORKFLOW_FILE = "validate.yml"
WORKFLOW_NAME = "Validation"
# The runs the table is taken from when none are named: five, so the median ignores the two slowest and the two fastest
# readings of a job (a contended runner, a cold cache) while the runs stay recent enough that their logs are still held
# and their jobs are the ones the next release runs.
DEFAULT_RUN_COUNT = 5
# What job_pool.py prints for each job that passed, after the timestamp gh puts before every log line.
OK_LINE = re.compile(r"^\S+ ok +(\d+(?:\.\d+)?)s (.+?)\s*$")
# The note job_pool.py puts after the label of a job it started once more after a termination, as in
# `ok 12.0s tests/a.py [shard 1/3] (retried after status 0x80000004)`: no part of the job's name.
RETRY_NOTE = re.compile(r" \(retried after status [^()]+\)$")
SHARD_SUFFIX = re.compile(r" \[shard \d+/\d+\]$")
# A leg's name in the log, from the workflow's matrix job names, by platform: on Windows `Windows shard <shard>, Python
# <version>`, and before the Linux job joined the required check, `Shard <shard> on Python <version>`; on Linux
# `Validation on Linux, shard <shard>`. So the median of the latest runs spans each rename.
LEG_NAMES = (
    ("Windows", re.compile(r"^Windows shard (\d+)/(\d+), Python ")),
    ("Windows", re.compile(r"^Shard (\d+)/(\d+) on Python ")),
    ("Linux", re.compile(r"^Validation on Linux, shard (\d+)/(\d+)$")),
)
# Before Linux was dealt, one job of this name ran every job there unsharded: one leg of one.
UNSHARDED_LEGS = {"Validation on Linux": "Linux"}
LEG_SHAPES = (
    "`Windows shard <n>/<count>, Python <version>`, `Shard <n>/<count> on Python <version>`, "
    "`Validation on Linux, shard <n>/<count>`, or `Validation on Linux`"
)
# The platforms the table records, one table each: those the deployer supports, by the names the deal reads.
PLATFORMS = tuple(sorted(platform_support.SUPPORTED_PLATFORMS.values()))


def leg_of(job: str) -> tuple[str, int, int] | None:
    """The leg's platform, its index, and the matrix's count when the job is named as a leg, else None."""
    if job in UNSHARDED_LEGS:
        return UNSHARDED_LEGS[job], 1, 1
    for platform, pattern in LEG_NAMES:
        found = pattern.match(job)
        if found:
            return platform, int(found.group(1)), int(found.group(2))
    return None


def job_name(label: str) -> str:
    """The job's name from the label an ok line reports: without the pool's retry note or a suite's shard."""
    return SHARD_SUFFIX.sub("", RETRY_NOTE.sub("", label))


def job_seconds_from_log(log: str) -> dict[str, dict[str, float]]:
    """Each platform's job seconds by name from the legs' lines of `gh run view --log` output, whose lines are
    `<job>\\t<step>\\t<text>`."""
    reported: defaultdict[tuple[str, str], list[float]] = defaultdict(list)
    for line in log.splitlines():
        fields = line.split("\t", 2)
        leg = leg_of(fields[0]) if len(fields) == 3 else None
        found = OK_LINE.match(fields[-1]) if leg else None
        if leg and found:
            reported[leg[0], found.group(2)].append(float(found.group(1)))
    totals: defaultdict[str, defaultdict[str, float]] = defaultdict(lambda: defaultdict(float))
    for (platform, label), seconds in reported.items():
        totals[platform][job_name(label)] += sum(seconds) / len(seconds)
    rounded = {
        platform: {name: round(seconds, 1) for name, seconds in jobs.items() if round(seconds, 1) > 0}
        for platform, jobs in totals.items()
    }
    return {platform: jobs for platform, jobs in rounded.items() if jobs}


def legs_seen(log: str) -> set[tuple[str, int, int]]:
    """Each leg a log has a line from, as its platform, its index, and the matrix's count."""
    return {leg for leg in (leg_of(line.split("\t", 1)[0]) for line in log.splitlines()) if leg}


def leg_problem(log: str) -> str:
    """What a log lacks of each platform's legs, or an empty string when it reports every leg of every platform."""
    seen = legs_seen(log)
    problems = []
    for platform in PLATFORMS:
        counts = {count for found, _, count in seen if found == platform}
        if len(counts) != 1:
            problems.append(f"names no one set of {platform} legs")
            continue
        (count,) = counts
        missing = [f"{index}/{count}" for index in range(1, count + 1) if (platform, index, count) not in seen]
        if missing:
            problems.append(f"lacks {platform} leg {', '.join(missing)}")
    return "; ".join(problems)


def combine(per_run: Mapping[int, Mapping[str, Mapping[str, float]]]) -> dict[str, dict[str, float]]:
    """Each platform's job medians across the runs that report the job there, for the jobs the newest run (highest ID)
    reports on that platform."""
    newest = per_run[max(per_run)]
    return {
        platform: {
            name: round(
                statistics.median(
                    seconds[platform][name] for seconds in per_run.values() if name in seconds.get(platform, {})
                ),
                1,
            )
            for name in jobs
        }
        for platform, jobs in newest.items()
    }


def render_table(runs: Sequence[int], seconds: Mapping[str, Mapping[str, float]]) -> str:
    """The table's text: the runs it came from, oldest first, and each platform's seconds by job name, the platforms
    and the jobs in name order."""
    platforms = {platform: dict(sorted(jobs.items())) for platform, jobs in sorted(seconds.items())}
    return json.dumps({"runs": sorted(runs), "seconds": platforms}, indent=2) + "\n"


def latest_runs(client: GitHubClient, count: int) -> list[int]:
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
            str(count),
            "--json",
            "databaseId",
        ]
    )
    if not runs:
        raise GitHubError(f"no successful push run of {WORKFLOW_FILE} on main was found", kind="not_found")
    return [int(run["databaseId"]) for run in runs]


def run_seconds(client: GitHubClient, run: int) -> dict[str, dict[str, float]]:
    """The run's job seconds, once it is known to be a completed, successful run of the Validation workflow whose log
    reports every leg."""
    state = client.json(["run", "view", str(run), "--json", "workflowName,status,conclusion"])
    found = (state.get("workflowName"), state.get("status"), state.get("conclusion"))
    if found != (WORKFLOW_NAME, "completed", "success"):
        raise GitHubError(
            f"run {run} is {found[0]!r}, {found[1]}, {found[2] or 'no conclusion'}; record from completed, "
            f"successful {WORKFLOW_NAME} runs",
            kind="refused",
        )
    log = client.run(["run", "view", str(run), "--log"]).stdout
    if not legs_seen(log):
        raise GitHubError(
            f"run {run}'s log names no job as a leg, {LEG_SHAPES}; record from runs of the validate workflow's legs",
            kind="refused",
        )
    problem = leg_problem(log)
    if problem:
        raise GitHubError(
            f"run {run}'s log {problem}, as a rerun of a failed leg leaves; record from runs whose every leg on each "
            "platform ran in one attempt",
            kind="refused",
        )
    seconds = job_seconds_from_log(log)
    silent = [platform for platform in PLATFORMS if platform not in seconds]
    if silent:
        raise GitHubError(
            f"run {run}'s {' and '.join(silent)} legs reported no passing jobs; run validation with -v, as the "
            "validate workflow does",
            kind="refused",
        )
    return seconds


def main(arguments: list[str] | None = None, client: GitHubClient | None = None, table: Path = TABLE) -> int:
    parser = argparse.ArgumentParser(
        description="Record each validation job's median seconds on each platform from validate runs."
    )
    parser.add_argument("runs", nargs="*", type=int, help="the runs' IDs; the latest successful main push runs if none")
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_RUN_COUNT,
        help=f"how many of the latest runs to read when none are named (default {DEFAULT_RUN_COUNT})",
    )
    options = parser.parse_args(arguments)
    if options.count < 1:
        parser.error("--count must be at least 1")
    client = client or GitHubClient(runner=functools.partial(subprocess_runner, cwd=REPOSITORY_ROOT))
    try:
        runs = sorted(set(options.runs)) if options.runs else latest_runs(client, options.count)
        per_run = {run: run_seconds(client, run) for run in runs}
    except GitHubError as error:
        print(f"Cannot read the validate runs: {error}", file=sys.stderr)
        return 2
    seconds = combine(per_run)
    table.write_text(render_table(runs, seconds), encoding="utf-8", newline="\n")
    named = ", ".join(str(run) for run in sorted(runs))
    platforms = "; ".join(
        f"{platform} {len(jobs)} jobs, {sum(jobs.values()):.1f} job-seconds"
        for platform, jobs in sorted(seconds.items())
    )
    print(
        f"Recorded {platforms}, as the median of {len(runs)} run{'s' if len(runs) != 1 else ''} ({named}) in "
        f"{table.name}."
    )
    return 0


if __name__ == "__main__":
    run_main(main)
