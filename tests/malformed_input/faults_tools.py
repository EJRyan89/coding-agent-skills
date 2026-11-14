"""The fault drivers of the tools/ entry points; see fault_table.py.

A tool that reads the repository it lies in runs from the fixture's checkout copy (`from_checkout`), and a tool that
writes a build, a generated file, or a scaffold names what it writes in `outputs`, so a refusal is held to leaving it
as it found it. A canary (runtime_canary, plugin_canary, skill_evals) is driven only as far as a refusal before any
runtime starts: the checkout's branch, which git reads first, or the fixture files a judging run reads.
"""

from __future__ import annotations

import json
import os
import re

from fault_table import MISSING, OPENED_ONLY, STAT_RAISES, Driver, Fixture

from deployer.manifest import MANIFEST_VERSION

# The deployer's manifest under a fixture home, which deployable_report reads.
MANIFEST = "{home}/.claude/skills/.deploy-manifest.json"
# The results file deployable_report collects each pass in; an absent one starts an empty record, as its docstring says.
RESULTS = "{work}/results.json"
RESULTS_MISSING = "an absent results file starts an empty record, as deployable_report.py's docstring says"
MANIFEST_MISSING = (
    "an absent manifest is a home with nothing deployed, which the command refuses as such ('No skills are deployed', "
    "'No runtime adapters are deployed'), the manifest's own empty case in deployer/manifest.py's load"
)
# A search path that holds no program, so deployable_report finds no runtime and starts none.
NO_PROGRAMS = {"PATH": "{root}/no-programs"}
# The release tag plugin_release builds from, made in the fixture's checkout copy.
TAG = "v0.0.1"
EVALUATED = "tests/fixtures/skill-evals/review-prs/clean-change"
# What a fixture's search path starts with so that skill_evals finds a Claude Code, which nothing starts: the search
# path the suite runs with follows it, so git is still found, braces escaped for the arguments' formatting.
FAKE_CLAUDE_PATH = "{home}/bin" + os.pathsep + os.environ.get("PATH", "").replace("{", "{{").replace("}", "}}")


def deployed_home(fixture: Fixture) -> None:
    """A home one skill and its adapter are deployed into, as the deployer records them, and an empty results file."""
    digest = "sha256:" + "0" * 64
    entry = {"wrappers": {"alpha": {"hash": digest}}, "skills": {"alpha": {"hash": digest, "skill_deps": []}}}
    skills = fixture.home / ".claude" / "skills"
    (skills / "alpha").mkdir(parents=True)
    (skills / "alpha" / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
    (skills / ".deploy-manifest.json").write_text(
        json.dumps({"manifest_version": MANIFEST_VERSION, "sources": {"fixture/source": entry}}), encoding="utf-8"
    )
    (fixture.root / "no-programs").mkdir()
    (fixture.work / "results.json").write_text("[]\n", encoding="utf-8")
    (fixture.work / "verify.log").write_text("", encoding="utf-8")


def tagged_checkout(fixture: Fixture) -> None:
    """The checkout copy with the release tag plugin_release builds from and each tag its marketplace pins, all at
    the copy's one commit."""
    marketplace = (fixture.checkout / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    for tag in {TAG, *re.findall(r"/releases/download/(v\d+\.\d+\.\d+)/", marketplace)}:
        fixture.git("tag", "-f", tag, folder=fixture.checkout)


def gate_folder(fixture: Fixture) -> None:
    """A mutation gate folder whose select step chose no module, which the join reports as such."""
    folder = fixture.work / "gate"
    folder.mkdir()
    record: dict[str, object] = {
        "base": "origin/main",
        "mutmut": "3.0.0",
        "head": "",
        "suites": [],
        "carried": None,
        "modules": [],
    }
    (folder / "mutants.json").write_text(json.dumps(record), encoding="utf-8")


def evaluation_records(fixture: Fixture) -> None:
    """A records folder holding one review record for the scenario judged."""
    folder = fixture.work / "records" / "clean-change"
    folder.mkdir(parents=True)
    (folder / "haiku.json").write_text("{}\n", encoding="utf-8")


def fake_claude(fixture: Fixture) -> None:
    """A Claude Code on the search path that is never started: skill_evals refuses the checkout before it starts it."""
    folder = fixture.home / "bin"
    folder.mkdir()
    for name in ("claude", "claude.cmd"):
        (folder / name).write_text("exit 1\n", encoding="utf-8")
        (folder / name).chmod(0o755)


def hub(fixture: Fixture) -> None:
    """The work folder as a hub checkout of its own, which ignores the worktrees folder as this repository does."""
    fixture.repository({".gitignore": ".claude/worktrees/\n"})


DRIVERS: list[Driver] = [
    Driver("tools/branch_protection.py", "", (), gh=True),
    Driver(
        "tools/deployable_report.py",
        "layout",
        ("layout", "--results", RESULTS, "--label", "fault", "--home", "{home}", "--source", "{checkout}"),
        prepare=deployed_home,
        inputs=(MANIFEST, RESULTS),
        not_run={MISSING: f"{MANIFEST_MISSING}; {RESULTS_MISSING}"},
        environment=NO_PROGRAMS,
    ),
    Driver(
        "tools/deployable_report.py",
        "verify",
        (
            "verify",
            "--results",
            RESULTS,
            "--label",
            "fault",
            "--deploy-verify-exit",
            "0",
            "--deploy-verify-log",
            "{work}/verify.log",
            "--home",
            "{home}",
            "--source",
            "{checkout}",
        ),
        prepare=deployed_home,
        inputs=(MANIFEST, RESULTS),
        not_run={MISSING: f"{MANIFEST_MISSING}; {RESULTS_MISSING}"},
        environment=NO_PROGRAMS,
    ),
    Driver(
        "tools/deployable_report.py",
        "leftovers",
        ("leftovers", "--results", RESULTS, "--home", "{home}", "--source", "{checkout}"),
        prepare=deployed_home,
        inputs=(MANIFEST, RESULTS),
        not_run={MISSING: f"{MANIFEST_MISSING}; {RESULTS_MISSING}"},
        environment=NO_PROGRAMS,
    ),
    Driver(
        "tools/deployable_report.py",
        "summary",
        ("summary", "--results", RESULTS, "--no-probe", "--summary", "{work}/summary.md"),
        prepare=deployed_home,
        inputs=(RESULTS,),
        not_run={MISSING: RESULTS_MISSING},
        environment=NO_PROGRAMS,
    ),
    Driver(
        "tools/mutation_gate.py",
        "join",
        ("join", "{work}/gate"),
        prepare=gate_folder,
        inputs=("{work}/gate/mutants.json",),
    ),
    # A leg reads the select step's record first; one that holds no module of this platform's leg asks git nothing,
    # and tests/tools/test_mutation_gate.py holds its check of HEAD.
    Driver(
        "tools/mutation_gate.py",
        "leg",
        ("leg", "{work}/gate"),
        prepare=gate_folder,
        inputs=("{work}/gate/mutants.json",),
        not_run={STAT_RAISES: OPENED_ONLY},
        from_checkout=True,
    ),
    # The select step reads the changed lines through git before it makes any mutant.
    Driver(
        "tools/mutation_gate.py", "select", ("--base", "HEAD", "select", "{work}/gate"), git=True, from_checkout=True
    ),
    Driver(
        "tools/mutation_gate.py",
        "unrun",
        ("unrun", "{work}/gate"),
        prepare=gate_folder,
        inputs=("{work}/gate/mutants.json",),
    ),
    # A whole run in one process, which reads the changed lines through git before it makes any mutant.
    Driver("tools/mutation_gate.py", "", ("--base", "HEAD"), git=True, from_checkout=True),
    Driver(
        "tools/new_skill.py",
        "",
        ("fault-sample", "--description", "Sample a fault. Use it when a fault is sampled.", "--root", "{checkout}"),
        inputs=("{checkout}/source.json", "{checkout}/docs/skills.md"),
        from_checkout=True,
        outputs=(
            "{checkout}/skills/fault-sample",
            "{checkout}/deploy-meta/fault-sample.json",
            "{checkout}/docs/skills.md",
        ),
    ),
    Driver(
        "tools/plugin_build.py",
        "",
        ("--source", "{checkout}", "--output", "{work}/plugins"),
        inputs=("{checkout}/source.json", "{checkout}/.claude-plugin/marketplace.json"),
        not_run={f"{STAT_RAISES} {{checkout}}/.claude-plugin/marketplace.json": OPENED_ONLY},
        from_checkout=True,
    ),
    Driver(
        "tools/plugin_canary.py",
        "",
        ("--discovery-only",),
        git=True,
        from_checkout=True,
    ),
    Driver(
        "tools/plugin_release.py",
        "build",
        ("build", TAG, "--root", "{checkout}", "--output", "{work}/release"),
        prepare=tagged_checkout,
        inputs=("{checkout}/.claude-plugin/marketplace.json",),
        git=True,
        from_checkout=True,
        outputs=("{checkout}/.claude-plugin/marketplace.json",),
    ),
    Driver(
        "tools/plugin_release.py",
        "check",
        ("check", "--root", "{checkout}"),
        prepare=tagged_checkout,
        inputs=("{checkout}/.claude-plugin/marketplace.json",),
        not_run={STAT_RAISES: OPENED_ONLY},
        git=True,
        from_checkout=True,
    ),
    Driver("tools/project_release_sync.py", "", ("--check",), gh=True),
    Driver(
        "tools/record_job_seconds.py",
        "",
        ("1",),
        gh=True,
        from_checkout=True,
        outputs=("{checkout}/tests/validation/job_seconds.json",),
    ),
    Driver(
        "tools/release_notes.py",
        "",
        ("v99.0.0", "--from", "HEAD", "--write"),
        inputs=("{checkout}/docs/releasing.md", "{checkout}/docs/upgrade-notes.md"),
        not_run={STAT_RAISES: OPENED_ONLY},
        git=True,
        from_checkout=True,
        outputs=("{checkout}/docs/upgrade-notes.md",),
    ),
    Driver(
        "tools/runtime_canary.py",
        "",
        ("--discovery-only",),
        git=True,
        from_checkout=True,
    ),
    Driver(
        "tools/runtime_changelogs.py",
        "",
        ("--summary", "{work}/summary.md"),
        inputs=("{checkout}/.claude/skills/audit-repository/references/guidance.json",),
        not_run={STAT_RAISES: OPENED_ONLY},
        from_checkout=True,
    ),
    Driver(
        "tools/runtime_command_lines.py",
        "",
        ("--runtime", "codex", "--summary", "{work}/summary.md"),
        inputs=("{checkout}/tests/fixtures/captured-output/provenance.json",),
        not_run={STAT_RAISES: OPENED_ONLY},
        from_checkout=True,
    ),
    Driver(
        "tools/runtime_prompts.py",
        "",
        ("--write",),
        inputs=(f"{{checkout}}/{EVALUATED}/pull.json",),
        git=True,
        from_checkout=True,
        outputs=("{checkout}/docs/runtime-prompts",),
    ),
    Driver(
        "tools/skill_evals.py",
        "",
        ("review-prs", "--scenario", "clean-change", "--model", "haiku", "--records", "{work}/records"),
        prepare=evaluation_records,
        inputs=(f"{{checkout}}/{EVALUATED}/scenario.json", "{work}/records/clean-change/haiku.json"),
        not_run={f"{STAT_RAISES} {{work}}/records/clean-change/haiku.json": OPENED_ONLY},
        from_checkout=True,
    ),
    Driver(
        "tools/skill_evals.py",
        "",
        ("review-prs", "--scenario", "clean-change", "--model", "haiku"),
        prepare=fake_claude,
        git=True,
        environment={"PATH": FAKE_CLAUDE_PATH},
        from_checkout=True,
    ),
    Driver(
        "tools/skill_reference.py",
        "",
        ("--write", "--root", "{checkout}"),
        inputs=(
            "{checkout}/docs/skills.md",
            "{checkout}/README.md",
            "{checkout}/source.json",
            "{checkout}/skills/repo-cleanup/SKILL.md",
            "{checkout}/deploy-meta/repo-cleanup.json",
        ),
        from_checkout=True,
        outputs=("{checkout}/docs/skills.md",),
    ),
    Driver(
        "tools/skill_shims.py",
        "",
        ("--write",),
        inputs=("{checkout}/.claude/skills/change-skill/SKILL.md",),
        from_checkout=True,
        outputs=("{checkout}/.agents",),
    ),
    Driver("tools/worktrees.py", "list", ("list",), prepare=hub, git=True),
    Driver(
        "tools/worktrees.py",
        "new",
        ("new", "fix", "fault-sample"),
        prepare=hub,
        git=True,
    ),
]
NOT_RUN: dict[str, str] = {
    "tools/hub_guard.py": (
        "its one input is the PreToolUse event it reads on stdin, which the harness closes: an empty event names no "
        "path or command, so it reads no file and runs no git; tests/tools/test_hub_guard.py drives its git and file "
        "reads with events"
    ),
    "tools/worktrees.py guard": (
        "runs tools/hub_guard.py as its own program on this process's stdin, which the harness closes; hub_guard.py "
        "is named here for the same reason"
    ),
    "tools/deployable_report.py pin": (
        "reads no file and runs no git or gh through the clients: it starts the program --program names for its "
        "--version through platform_support.run_tool, which the harness stops for any program but git"
    ),
    "tools/deployable_report.py bash": (
        "reads no file and runs no git or gh through the clients: it starts the Bash platform_support.find_bash finds "
        "for its --version through platform_support.run_tool, which the harness stops for any program but git"
    ),
}
