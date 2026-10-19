"""Run a skill's fixed scenarios, model by model, and judge the review record each run wrote.

Usage:
  python -B tools/skill_evals.py SKILL [--write] [--runs N] [--timeout SECONDS] [--jobs N]
  python -B tools/skill_evals.py SKILL [--scenario NAME ...] [--model NAME ...] [--runs N] [--records DIR]

SKILL is one skill's name, a folder of scenarios under tests/fixtures/skill-evals/; a name that is not a skill's is a
usage error, and a skill with no scenario there fails the run. Repeated --scenario and --model names count once, in
first-occurrence order. Without --scenario, every scenario is selected in name order. --timeout is a positive number
of seconds. --runs, a positive count and 1 unless given, runs each scenario that many times on each model and judges
every run, since one run is one sample of what a model does.

A scenario is a directory tests/fixtures/skill-evals/<skill>/<scenario>/, which never ships, holding a scenario.json.
A fixture scenario is a code-review fixture, which `review_pipeline.py prepare --canary --fixture <scenario>` reviews
in place of a pull request: the change as two trees, base/ and head/, and the pull request in pull.json (see
review_canary.py in code-review-core). A re-review scenario's run adds `--re-review --prior <scenario>/<prior>`, its
prior a plain relative path inside the scenario's folder. A
document scenario names a document instead, for a skill started on one file's path, as review-document is: the run
writes it under its own file name into a new folder in the run's folder and starts the skill with that path alone.
With a base, that folder is a new git checkout whose one commit holds the base under the same name, and the document
is written over it uncommitted, so the skill reviews only the edit; without one, nothing encloses it and the skill
reviews it whole. The checkout's Git commands use a temporary empty hook directory, overriding global and
template-installed hooks, and the base commit disables signing. A finding's path is then the document's file name.
A repository reviewer's scenario judges one of this repository's own reviewers under .claude/skills/, such as
review-repository, which review-prs runs and no session starts: it sits under the reviewer's name, and is a fixture
scenario naming the reviewer and review-prs, whose pull.json manifest_path names the reviewer's manifest. Since
--canary-home ships no repository skill, each run copies the fixture into a throwaway folder outside the home and writes
there, at its path in both trees, each of the reviewer's files from this checkout: its SKILL.md, references/, scripts/
but its regression suites, and every file its manifest declares. A condition's `reads` add none, since they name what
its script reads of the reviewed tree. The copy is removed when the run ends, however it ends, and the fixture itself is
never written. The run starts review-prs on the copy, and its rows are the reviewer's. A reviewer the checkout lacks, a
file it declares that the checkout lacks, or a fixture already holding a file the run places, fails the load. Such a
fixture may hold a file of its trees under its name with `.fixture` appended, which the copy drops: this repository's
reviewers review its Python, which validation would otherwise lint and type-check as this repository's own, each
module under its bare name, shadowing the real one of that name. A held file named `.fixture` alone, one beside the
file it is restored to, or one restored to a file the run places fails the load, and so does one in any other
scenario's fixture, which is reviewed in place.
The scenario.json:

  {"skill": "<skill>", "mode": "initial" | "re-review",
   "prior": "<prior record file in the scenario>"  (re-review only),
   "document": "<path>",  "base": "<path>",  "stage": "<stage>"
      (a document scenario, initial only; base optional; stage optional, one of draft, review, and final, which the
      skill is started with as --stage),
   "reviewer": "<reviewer>"  (a repository reviewer's scenario, whose skill is review-prs),
   "expectations": [<expectation>, ...]}

A document or base path leads from the scenario's folder to a file, which may be another scenario's under the same
scenarios root, so a document serves several skills' scenarios without a copy.

A trigger scenario judges a skill's description rather than its work: whether a session given only a request a user
might type starts the skill, as its description says it should, and whether one given a near miss, such as a request
a neighbouring skill's description names, leaves it alone. Its scenario.json holds no expectations, since each
request is one:

  {"skill": "<skill>", "mode": "trigger", "requests": ["<request>", ...], "near_misses": ["<request>", ...]}

The skill is a shipped one under skills/, which the deployment puts in each home, and the model may start it: one that
sets disable-model-invocation fails the load. Each list holds at least one request; a request is one line of text,
given once in the scenario, that starts with neither `-` nor `/`, which Claude Code would read as an option or a
command. Its expectations are `starts "<request>"` for each request and `does_not_start "<request>"` for each near
miss, in that order. A run gives each request, then each near miss, to a headless session of its own in the model's
home, on that model, since the session's model is the one that decides whether to start a skill, with no grant and
at most TRIGGER_TURNS turns, so a session that starts a skill stops soon after. A session started the skill when one
of its own messages, not a subagent's, calls the Skill tool naming it. The run writes, in its folder, its trigger
record: {"requests": [{"request": "<request>", "started": ["<skill>", ...]}, ...], "models": ["<model ID>", ...]},
each request's session with the skills it started, and the model IDs the sessions' messages named. A session counts
for nothing when Claude Code could not start, timed out, or did not list the skill in its init event, which are `run`
failures, or when its messages named no model or another one, a `record` failure; its exit code is not read, since
the turn limit is the evaluation's own. The first such session makes the whole run count for nothing.

Each expectation is a statement over the record, checked here by script and never by reading the report. Severities
rank SUGGESTION < SHOULD_FIX < MUST_FIX; `lines` lists the head lines a finding may be anchored at, any of them:

  {"kind": "finding", "path": P, "lines": [N, ...], "severity": S, "category": C}
      a finding at P on one of the lines, at least as severe as S, and of category C, matched ignoring case, when
      `category` is given; a scenario whose manifest declares finding_categories names one of them
  {"kind": "no_finding_above", "severity": S, "paths": [P, ...], "lines": [N, ...]}
      no finding more severe than S on the paths, or on any path when `paths` is left out; with `lines`, which takes
      exactly one path, only on those lines of it, so a document may hold a more severe finding elsewhere; S is
      below the highest severity, above which nothing ranks
  {"kind": "only_at", "path": P, "lines": [N, ...]}
      every finding, whatever its severity, is at P on one of the lines, as a review of one edited line should be
  {"kind": "verdict", "verdict": "APPROVED" | "CHANGES_REQUESTED" | "INCOMPLETE"}
  {"kind": "ledger", "addressed": N, "still_present": N}     re-review only
      exactly that many ledger entries this review judged addressed, and still present
  {"kind": "disposition", "entry": "v1:F001", "disposition": D}     re-review only
      this review's disposition of that ledger entry
  {"kind": "repeats", "path": P, "lines": [N, ...], "entry": "v1:F001"}     re-review only
      every finding this review raised at P on those lines at that ledger entry's severity repeats the entry, so the
      problem counts once; raising none there also passes, since the ledger then carries the entry alone, and so does
      a finding there of another severity: a less severe one is about another defect, and a more severe one is a new
      finding, since a repeat may not be more severe than what it repeats

An unknown kind, a field a kind does not take, or a `category` the scenario's manifest does not list, ignoring case,
fails the whole run before anything is judged. The manifest is the one the fixture's pull.json names in its base tree,
the checkout's for a repository reviewer's scenario, or review-document's design manifest for a document scenario.

Records come from a record source, a function of a scenario, a model, and which run, that returns the record that
run wrote. --records DIR reads DIR/<scenario>/<model>.json, or with --runs N above 1, DIR/<scenario>/<model>-<run>.json
for each run from 1 to N: the record JSON a run finalized, or a trigger scenario's trigger record. It holds a review
record to what a run's record is held to: code-review-core's record contract (validate_record), the review version
of the scenario's mode, a guard that held every reviewer, and every reviewer's `model` naming that model's ID. The
model is the one each reviewer's result reports, which is weaker than a run's check of the transcript. A trigger
record must name each of the scenario's requests once, and every model ID in it must be that model's. A missing or
unreadable record, or one any of those refuses, fails every expectation of its scenario, model, and run.

Without --records, it runs the scenarios. For each model it makes a fresh home under one throwaway directory,
deploys this checkout into it with `deploy.py --canary-home`, as tools/runtime_canary.py does, and sets the model's
ID in that home's copy of the reviewer agent, whose `model: inherit` would otherwise run every reviewer on the
session's model: a fixture's reviewers have no MODEL line, since neither the generic reviewer nor a scenario's
specialist names a model, and `inherit` outranks CLAUDE_CODE_SUBAGENT_MODEL.
The session gets that agent with `--agents`, which outranks the same agent loaded from the home's .claude/agents:
Claude Code runs a project agent's hooks only in a folder whose workspace trust was accepted, which a `-p` session
never is, so loaded from the home the reviewer guard would never run. The definition is the home's agent file, its
frontmatter and body, with one change: its hook runs the home's review_guard.py in place of the profile folder's,
so the guard checks the home's prompts against the home's copy of the pipeline that wrote them.
Then, for each run of each scenario on each model, up to --jobs at once, every scenario on every model queued for one
run before any for the next, it starts Claude Code headless with the model's home as
its working directory, so the deployed skills and agent load as project ones, and the prompt
`/<skill> <prepare_arguments>`, on a repository reviewer's scenario's copy, or `/<skill> "<document>"` with the path
of a document scenario's document, staged in the run's folder; a document or a copy that cannot be staged fails the
run before any session starts. The session, which
orchestrates the skill, stays on SESSION_MODEL's ID. It has no Workflow tool, so reviewers start as native
subagents, whose messages --forward-subagent-text puts in the transcript with the model each ran on, and
--include-hook-events puts each guard decision beside them. A run counts only if every one of those messages names
that model's ID, a dated suffix allowed, and every message of the session names the session's; a run with no
subagent, or one on another model or another generation of the same family, fails every expectation, since the model
was not the one judged.
Edits are accepted, standing in for the user who approves each reviewer's result file, which the reviewers write
inside the home, and the reviewers' self-check and source commands are allowed, each by the quoted path of the home's
deployed script as the pipeline's prompts give it, in both shells, with no wildcard in the path; the skill's
allowed-tools grant the rest. Each run's own folder is <home>/.skill-evals/<scenario>, or with --runs above 1
<home>/.skill-evals/<run>/<scenario>, and its temporary directory is a folder there, through TMPDIR, which Python
reads first on every system, so the pipeline's run folders and the canary root that finalize writes land there.
The record judged is the latest record of the pull request recorded there, read and validated by code-review-core's
archive, and it must be version 2 for a re-review, whose canary root first holds the prior record as version 1, and
version 1 otherwise: a run whose latest record is another version, or cannot be read, recorded no review of its own,
and fails with that reason. The code-review configuration is written once per home at
`.skill-evals/config.json`, which all runs in that home read. A record any of whose reviewers has a null `files_read`
counts for nothing either: the guard's claim starts a role's read log, so a reviewer the guard held has a count, if
only 0, and null means the boundary was never exercised.

What still comes from the user's own setup: their sign-in, user CLAUDE.md and auto-memory, as for the runtime
canary.

Output, one fact per line. With --runs above 1, every line about one run, a TRANSCRIPT, GUARDED, NOT_COUNTED, PASS, or
FAIL line, names it as ` run=<run>` after its model, and the lines are otherwise as they are for one run:
  HOME "<dir>"                                    the throwaway directory, one home per model inside it
  DEPLOYED <model> <source id>                    or DEPLOY_FAILED <model> "<reason>", which stops the run, also
                                                  when the home's reviewer agent cannot be passed with --agents, or
                                                  the home's path holds a *, which a grant would read as any text
  RUNTIME claude <version>
  TRANSCRIPT <scenario> <model> "<path>"          a run finished; its stream-json output, in the order runs end, one
                                                  line for each session of a trigger scenario's run
  REVIEWER <model> <model ID>[,<model ID>]        the models the reviewers ran on, and a trigger scenario's sessions,
                                                  or `none`
  GUARDED <scenario> <model> <role> files_read=<n>
                                                  a reviewer of a run that counts, which the guard held, and the
                                                  snapshot files the guard counted it reading
  NOT_COUNTED <scenario> <model> <kind> "<reason>"
                                                  a record that counts for nothing, so each expectation of its
                                                  scenario and model fails with this kind and reason: `run` when the
                                                  run recorded no review of its own, because the session ended
                                                  before finalize, timed out, could not start, did not list the
                                                  skill, had a command denied, or its fixture or document could not
                                                  be staged, a trigger scenario's session as above, or, with
                                                  --records, no record file can be read; `record`
                                                  when a record does not show the model judged: no reviewer subagent
                                                  ran, reviewers or the session ran on another model, a reviewer no
                                                  guard held, a record the record contract or the scenario's
                                                  review version refuses, or a trigger record of another model's
  PASS <skill> <scenario> <model> <expectation>
  FAIL <skill> <scenario> <model> <expectation> <kind> "<reason>"
                                                  <kind> is `judgment` for what the record shows, or the `run` or
                                                  `record` of its NOT_COUNTED line
  RUNS <scenario> <model> <passed>/<runs> all|some|none
                                                  with --runs above 1, after the outcomes, one per scenario and model:
                                                  the runs that passed every expectation of theirs, of the runs, and
                                                  whether that is every run (all, the pass^k of k runs), some, which
                                                  is an inconsistent result, or none
  FAILED "<reason>"                               nothing was run or judged: the scenarios could not be loaded,
                                                  Claude Code is missing, or --write's existing results file
                                                  cannot be read or its table cannot be read
A run then prints, between a `CITATION BEGIN` and a `CITATION END` line, the Markdown a pull request's Validation
section cites it with: the command, a table with one row per scenario and one column per model, each cell the
expectations passed of those checked over every run, followed over several runs by `(<passed>/<runs> runs)`, the
RUNS line's count, and in a text fence the RUNTIME, REVIEWER, and GUARDED lines, each NOT_COUNTED line with its
reason as the results file records it, and each judgment's FAIL line, so the block names no path of the machine. With
--records, the table alone follows the outcomes. Then `WROTE <file>` when --write replaced the skill's
rows in docs/skill-evaluations.md, and `REMOVED "<dir>"` when nothing failed and the home is gone; otherwise the home
stays for its transcripts. A model none of whose reviewers ran is written in the results file as not run with a fixed
phrase for its first reason, such as `timed out after 1800 seconds` or `denied 2 commands`, which names no path of the
machine; the FAIL lines and the transcript keep the whole reason. --write reads the results table strictly before any
run: the header, its separator, then rows of the header's columns, each naming a skill, and nothing else between the
markers. Every other skill's row is written back as it was. A row's Runs cell is the runs of each scenario, and with
more than one, each scenario's counts in By scenario end with the runs that passed every expectation, of the runs
judged, as the citation's cells do; a run none of whose judged part ran, reviewers or a trigger scenario's sessions,
is left out of both counts, as a scenario none of whose runs held a judgment is `not run`.
It exits 1 on any FAIL, FAILED, or DEPLOY_FAILED line, 0 otherwise, and 2 on a usage error.

It records pass or fail, the model identifiers, the runtime version, and the date, and nothing else: no tokens,
cost, or timings. It calls models, so it is never part of validation; see .claude/skills/evaluate-skill/SKILL.md.
"""

from __future__ import annotations

import argparse
import datetime
import math
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "skill-core" / "scripts"))

from bounded_process import without_repository_variables
from console import run_main
from git_client import GitClient

from deployer import fsops, platform_support
from deployer.source import SKILL_NAME_PATTERN
from tools import runtime_canary
from tools.runtime_canary import Runner
from tools.skill_evals_judging import Outcome, judge, not_counted_line, outcome_line, records_in, report, table
from tools.skill_evals_records import JUDGMENT, MODELS, RUN
from tools.skill_evals_results import replace_results, result_rows, results_table
from tools.skill_evals_runs import (
    NO_REVIEW,
    Run,
    RunKey,
    Session,
    claude_version,
    prepare_home,
    run_all,
    run_keys,
    run_lines,
    runs_source,
)
from tools.skill_evals_scenarios import REPOSITORY_ROOT, SCENARIO_ROOT, Scenario, ScenarioError, load_scenarios, quote

RESULTS = REPOSITORY_ROOT / "docs" / "skill-evaluations.md"
DEFAULT_TIMEOUT = 1800
DEFAULT_JOBS = 3


@dataclass(frozen=True, kw_only=True)
class Options:
    """The command line, read once from the parsed arguments."""

    skill: str
    records: Path | None
    # The --scenario and --model names in the order given, repeats included; no --model is every model.
    scenarios: tuple[str, ...]
    models: tuple[str, ...]
    write: bool
    timeout: float
    jobs: int
    runs: int

    @classmethod
    def parsed(cls, namespace: argparse.Namespace) -> Options:
        return cls(
            skill=namespace.skill,
            records=namespace.records,
            scenarios=tuple(namespace.scenario),
            models=tuple(namespace.model or ()),
            write=namespace.write,
            timeout=namespace.timeout,
            jobs=namespace.jobs,
            runs=namespace.runs,
        )

    def judged_models(self) -> list[str]:
        """The models a run judges: each given once, in the order given, or every model."""
        return list(dict.fromkeys(self.models or MODELS))


def command_line(options: Options) -> str:
    """The command that made the run, without --jobs, which changes no result."""
    words = ["python", "-B", "tools/skill_evals.py", options.skill]
    words += [word for name in dict.fromkeys(options.scenarios) for word in ("--scenario", name)]
    words += [word for name in dict.fromkeys(options.models) for word in ("--model", name)]
    if options.runs != 1:
        words += ["--runs", str(options.runs)]
    if options.timeout != DEFAULT_TIMEOUT:
        words += ["--timeout", f"{options.timeout:g}"]
    return " ".join([*words, *(["--write"] if options.write else [])])


def citation(
    skill: str,
    options: Options,
    version: str,
    scenarios: Sequence[Scenario],
    models: Sequence[str],
    runs: Mapping[RunKey, Run],
    outcomes: Sequence[Outcome],
) -> list[str]:
    """The Markdown a pull request's Validation section cites the run with: the command, the table, and in a text
    fence the runtime, the models the judged part ran on, each guarded reviewer, each run that counts for nothing with
    its reason as the results file records it, and each judgment that failed, none of which names a path of the
    machine."""
    facts = [f"RUNTIME claude {version}", *run_lines(scenarios, models, runs)]
    facts += [
        not_counted_line(key[0], key[1], run.kind or RUN, run.recorded or NO_REVIEW, key[2])
        for key in run_keys(scenarios, models, runs)
        if (run := runs[key]).record is None
    ]
    facts += [outcome_line(skill, outcome) for outcome in outcomes if outcome.kind == JUDGMENT]
    fence = "`" * max([3, *(len(run) + 1 for fact in facts for run in re.findall("`+", fact))])
    return [
        f"`{command_line(options)}`",
        "",
        *table(scenarios, models, outcomes),
        "",
        f"{fence}text",
        *facts,
        fence,
    ]


def new_home() -> Path:
    return Path(tempfile.mkdtemp(prefix="skill-evals-")).resolve()


@dataclass(frozen=True)
class Seams:
    """What a run reaches outside this module, so tests can stand in for each."""

    runner: Runner = runtime_canary.run_process
    which: Callable[[str], str | None] = platform_support.find_executable
    deploy: Callable[[Path, Path], tuple[int, str]] = runtime_canary.deploy
    environment: Mapping[str, str] | None = None
    make_home: Callable[[], Path] = new_home
    today: Callable[[], datetime.date] = datetime.date.today
    results: Path = RESULTS
    git: GitClient = field(default_factory=GitClient)


def preflight(write: bool, seams: Seams) -> tuple[str, str | None]:
    """Claude Code's path and, with --write, the results file's text; raise ScenarioError before any run when
    something a run needs is missing."""
    executable = seams.which("claude")
    if executable is None:
        raise ScenarioError("Claude Code (claude) is not installed")
    if not write:
        return executable, None
    try:
        text = seams.results.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScenarioError(f"cannot read {seams.results}: {exc}") from exc
    results_table(text)
    return executable, text


def evaluate(skill: str, scenarios: Sequence[Scenario], options: Options, seams: Seams) -> int:
    """Run every scenario on every model from throwaway homes, judge the records, and print the result."""
    models = options.judged_models()
    # Without git's repository-selecting variables, so neither the version probe nor a session, nor anything the
    # session starts, reads the repository a shell's `GIT_DIR` names rather than the throwaway home.
    base = without_repository_variables(os.environ if seams.environment is None else seams.environment)
    try:
        executable, results_text = preflight(options.write, seams)
    except ScenarioError as exc:
        print(f"FAILED {quote(str(exc))}")
        return 1
    root = seams.make_home()
    print(f"HOME {quote(runtime_canary.forward(root))}", flush=True)
    homes = {model: root / model for model in models}
    for model, home in homes.items():
        reason = prepare_home(home, model, seams.deploy)
        if reason:
            print(f"DEPLOY_FAILED {model} {quote(reason)}")
            return 1
        print(f"DEPLOYED {model} {runtime_canary.source_id(REPOSITORY_ROOT)}", flush=True)
    version = claude_version(executable, seams.runner, base, root)
    print(f"RUNTIME claude {version}", flush=True)
    session = Session(executable, base, seams.runner, options.timeout, seams.git)
    runs = run_all(scenarios, homes, session, options.jobs, options.runs)
    for line in run_lines(scenarios, models, runs):
        print(line)
    outcomes = judge(scenarios, models, runs_source(runs), options.runs)
    failed = report(skill, outcomes)
    print("CITATION BEGIN")
    for line in citation(skill, options, version, scenarios, models, runs, outcomes):
        print(line)
    print("CITATION END")
    if results_text is not None:
        rows = result_rows(skill, scenarios, models, outcomes, runs, version, seams.today())
        seams.results.write_text(replace_results(results_text, skill, rows), encoding="utf-8", newline="\n")
        print(f"WROTE {runtime_canary.forward(seams.results)}")
    if not failed:
        fsops.remove(root)
        print(f"REMOVED {quote(runtime_canary.forward(root))}")
    return 1 if failed else 0


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, not {value}")
    return number


def seconds(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds, not {value}")
    return number


def skill_name(value: str) -> str:
    if not SKILL_NAME_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(f"must be a skill's name, a folder under {SCENARIO_ROOT}, not {value}")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run a skill's scenarios on each model and judge their records.")
    result.add_argument(
        "skill", type=skill_name, help="the skill whose scenarios to judge, a folder under tests/fixtures/skill-evals"
    )
    result.add_argument("--records", type=Path, help="judge a folder of <scenario>/<model>.json records; run nothing")
    result.add_argument("--scenario", action="append", default=[], help="judge only this scenario (repeatable)")
    result.add_argument("--model", action="append", choices=list(MODELS), help="judge only this model (repeatable)")
    result.add_argument("--write", action="store_true", help="replace the skill's rows in docs/skill-evaluations.md")
    result.add_argument(
        "--timeout", type=seconds, default=DEFAULT_TIMEOUT, help=f"seconds for each run (default {DEFAULT_TIMEOUT})"
    )
    result.add_argument("--jobs", type=positive, default=DEFAULT_JOBS, help=f"runs at once (default {DEFAULT_JOBS})")
    result.add_argument(
        "--runs", type=positive, default=1, help="runs of each scenario on each model, each judged (default 1)"
    )
    return result


def main(argv: Sequence[str], root: Path = SCENARIO_ROOT, seams: Seams | None = None) -> int:
    command = parser()
    options = Options.parsed(command.parse_args(argv))
    if options.write and (options.records or options.scenarios or options.models):
        command.error("--write records a full run: every scenario on every model, with no --records")
    try:
        scenarios = load_scenarios(options.skill, list(options.scenarios), root)
    except ScenarioError as exc:
        print(f"FAILED {quote(str(exc))}")
        return 1
    if options.records is None:
        return evaluate(options.skill, scenarios, options, seams or Seams())
    models = options.judged_models()
    outcomes = judge(scenarios, models, records_in(options.records), options.runs)
    failed = report(options.skill, outcomes)
    for line in table(scenarios, models, outcomes):
        print(line)
    return 1 if failed else 0


if __name__ == "__main__":
    run_main(lambda: main(sys.argv[1:]), errors="backslashreplace")
