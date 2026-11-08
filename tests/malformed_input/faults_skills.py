"""The fault drivers of the shipped skills' entry points under skills/; see fault_table.py."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from fault_table import (
    DIRECTORY,
    MISSING,
    OPENED_ONLY,
    REPOSITORY,
    STAT_RAISES,
    UNREADABLE,
    Driver,
    Fixture,
    git_environment,
)

sys.path.insert(0, str(REPOSITORY / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))
sys.path.insert(0, str(sibling_scripts("repo-cleanup")))
sys.path.insert(0, str(sibling_scripts("github-activity-report")))

import github_activity_report
import repo_cleanup
import test_review_fixture
from review_config import write_config

AUDIT_AI_CONFIG = "skills/audit-ai-config/scripts/audit_ai_config.py"
MCP_HANDSHAKE = "skills/audit-ai-config/scripts/mcp_handshake.py"
SKILL_INVENTORY = "skills/audit-skill/scripts/skill_inventory.py"
SKILL_TEXT = """---
name: demo
description: Demonstrate a skill. Use it when asked for a demo.
allowed-tools: ["Read", "Bash(python scripts/demo.py *)"]
---

# Demo

Read `references/guide.md`, then run:

```bash
python scripts/demo.py --check
```
"""
GUIDE_TEXT = "# Guide\n\nThe guide the skill reads.\n"
SCRIPT_TEXT = 'print("DEMO_OK")\n'
# Why a fault is not run where it cannot apply, or where the command's contract makes it the empty case.
ABSENT_CONFIGURATION = (
    "each input is a configuration file a repository may lack, and the audit reports which it found: a removed one "
    "is a repository without it, as the docstring's RESULT INCONCLUSIVE (unconfigured) says"
)
NO_MCP_CONFIGURATION = "a repository with no MCP configuration declares no server: the docstring's NO_SERVERS, exit 0"
LISTED = (
    "the command measures the files the skill folder's listing holds: a file removed, or a folder in its place, is a "
    "skill without that file, which it measures as such (NO_MAIN for SKILL.md), not a file it failed to read"
)
NOT_LOCATED = (
    "a skill folder whose SKILL.md is removed, or is a folder, holds no skill, so locate's NOT_FOUND is its answer"
)
NOT_READ = "locate finds a skill's SKILL.md by its stat and never reads it, so a file it cannot read is still found"
MCP_TEXT = '{"mcpServers": {"remote": {"type": "http", "url": "https://example.invalid/mcp"}}}\n'


def configured_repository(fixture: Fixture) -> None:
    """A repository with agent instructions, settings, an MCP configuration naming a remote server, and a skill."""
    fixture.repository(
        {
            "CLAUDE.md": "# Instructions\n\nRun the tests before a pull request.\n",
            "AGENTS.md": "Read CLAUDE.md.\n",
            ".mcp.json": MCP_TEXT,
            ".claude/settings.json": '{"permissions": {"allow": []}}\n',
            ".claude/skills/demo/SKILL.md": SKILL_TEXT,
            ".claude/skills/demo/references/guide.md": GUIDE_TEXT,
            ".claude/skills/demo/scripts/demo.py": SCRIPT_TEXT,
        }
    )


SKILL = "{work}/.claude/skills/demo"
MEMORY_AUDIT = "skills/curate-agent-memory/scripts/memory_audit.py"
DEPENDENCY_GRAPH = "skills/dotnet-dependencies/scripts/dependency_graph.py"
CSHARP_LAYOUT = "skills/dotnet-format/scripts/csharp_layout.py"
FORMAT_TARGETS = "skills/dotnet-format/scripts/dotnet_format_targets.py"
RUN_FORMAT = "skills/dotnet-format/scripts/run_dotnet_format.py"
MEMORY_STORE = "{home}/.claude/projects/fixture-project/memory"
INDEX = f"{MEMORY_STORE}/MEMORY.md"
MEMORY = f"{MEMORY_STORE}/rule.md"
USER_SETTINGS = "{home}/.claude/settings.json"
INSTRUCTIONS = "{work}/CLAUDE.md"
MANIFEST = "{home}/.claude/skills/.deploy-manifest.json"
SOURCE_SKILL = "{root}/source/skills/demo/SKILL.md"
# The index as reindex writes it, line endings included, so a run with no fault leaves it unchanged.
INDEX_TEXT = b"# Memory\n\n- [Rule](rule.md) \xe2\x80\x94 run the tests first\n"
MEMORY_TEXT = b"---\nname: Rule\ndescription: run the tests first\ntype: feedback\n---\n\nRun the tests first.\n"
CSHARP_GUID = "{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}"
SOLUTION_TEXT = (
    "Microsoft Visual Studio Solution File, Format Version 12.00\n"
    f'Project("{CSHARP_GUID}") = "App", "src\\App\\App.csproj", "{{11111111-1111-1111-1111-111111111111}}"\n'
    "EndProject\n"
)
PROJECT_TEXT = (
    '<Project ToolsVersion="15.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">\n'
    "  <PropertyGroup><TargetFrameworkVersion>v4.8</TargetFrameworkVersion></PropertyGroup>\n"
    '  <ItemGroup><Reference Include="Newtonsoft.Json, Version=12.0.0.0"><HintPath>..\\..\\lib\\Newtonsoft.Json.dll'
    "</HintPath></Reference></ItemGroup>\n"
    '  <ItemGroup><Compile Include="Program.cs" /></ItemGroup>\n'
    "</Project>\n"
)
APP_CONFIG_TEXT = (
    '<?xml version="1.0" encoding="utf-8"?>\n<configuration><runtime>'
    '<assemblyBinding xmlns="urn:schemas-microsoft-com:asm.v1"><dependentAssembly>'
    '<assemblyIdentity name="Newtonsoft.Json" publicKeyToken="30ad4fe6b2a6aeed" culture="neutral" />'
    '<bindingRedirect oldVersion="0.0.0.0-12.0.0.0" newVersion="12.0.0.0" />'
    "</dependentAssembly></assemblyBinding></runtime></configuration>\n"
)
SOURCE_TEXT = "namespace App\n{\n    #region Main\n    class Program { }\n    #endregion\n}\n"
SOLUTION = "{work}/App.sln"
PROJECT = "{work}/src/App/App.csproj"
APP_CONFIG = "{work}/src/App/App.config"
SOURCE_FILE = "{work}/src/App/Program.cs"
EDITORCONFIG = "{work}/.editorconfig"
FILE_LIST = "{root}/file-list.txt"
TOOLS = "{root}/bin"
# The repository resolve reads, outside the work folder: `git fetch` writes its FETCH_HEAD even when it fails.
TARGETS_REPOSITORY = "{root}/repository"


def memory_store(fixture: Fixture) -> None:
    """A git repository as the working folder, and a memory store under the home with its index and one memory that
    the user settings name as autoMemoryDirectory."""
    fixture.repository({"README.md": "# Fixture\n", "CLAUDE.md": "# Instructions\n\nRun the tests.\n"})
    store = Path(MEMORY_STORE.format(**fixture.values()))
    store.mkdir(parents=True)
    (store / "MEMORY.md").write_bytes(INDEX_TEXT)
    (store / "rule.md").write_bytes(MEMORY_TEXT)
    settings = fixture.home / ".claude" / "settings.json"
    settings.write_text(json.dumps({"autoMemoryDirectory": "~/.claude/projects/fixture-project/memory"}), "utf-8")


def deployed_skill(fixture: Fixture) -> None:
    """A git repository as the working folder, and a deployer manifest recording a source checkout that deploys demo."""
    fixture.repository()
    source = fixture.root / "source"
    skill = source / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\ndescription: A demo.\n---\n\n# Demo\n", encoding="utf-8")
    manifest = fixture.home / ".claude" / "skills" / ".deploy-manifest.json"
    manifest.parent.mkdir(parents=True)
    entry = {"source_dir": str(source), "skills": {"demo": {"hash": "0" * 64}}}
    manifest.write_text(json.dumps({"sources": {"main": entry}}), encoding="utf-8")


def project_skill(fixture: Fixture) -> None:
    """A git repository as the working folder holding a project skill demo, and no deployer manifest."""
    fixture.repository({".claude/skills/demo/SKILL.md": "---\nname: demo\ndescription: A demo.\n---\n\n# Demo\n"})


def solution(fixture: Fixture, folder: Path | None = None) -> None:
    """A git repository, the work folder by default, holding a solution of one legacy project with a binding redirect,
    an .editorconfig, and a C# file, and the FILE_LIST naming that file."""
    fixture.repository(
        {
            "App.sln": SOLUTION_TEXT,
            "src/App/App.csproj": PROJECT_TEXT,
            "src/App/App.config": APP_CONFIG_TEXT,
            "src/App/Program.cs": SOURCE_TEXT,
            ".editorconfig": "root = true\n\n[*.cs]\nindent_style = space\n",
        },
        folder,
    )
    (fixture.root / "file-list.txt").write_bytes(b"src/App/Program.cs\0")


def empty_solution(fixture: Fixture) -> None:
    """As `solution`, with a solution listing no project, so `config` evaluates none and starts no dotnet."""
    solution(fixture)
    (fixture.work / "App.sln").write_text(
        "Microsoft Visual Studio Solution File, Format Version 12.00\nGlobal\nEndGlobal\n", encoding="utf-8"
    )


def changed_branch(fixture: Fixture) -> None:
    """As `solution` at TARGETS_REPOSITORY, with origin/main at the first commit and a C# file changed on the branch
    since, and a tools folder holding git alone, so no gh or dotnet is found."""
    repository = Path(TARGETS_REPOSITORY.format(**fixture.values()))
    solution(fixture, repository)
    fixture.git("update-ref", "refs/remotes/origin/main", "HEAD", folder=repository)
    (repository / "src" / "App" / "Program.cs").write_text(SOURCE_TEXT + "// changed\n", encoding="utf-8")
    fixture.git("commit", "-q", "-am", "change", folder=repository)
    tools = fixture.root / "bin"
    tools.mkdir()
    git = shutil.which("git")
    assert git is not None, "git is on PATH"
    if sys.platform != "win32":
        (tools / "git").symlink_to(git)


def formatter_inputs(fixture: Fixture) -> None:
    """As `solution`, with a tools folder holding a dotnet-format that is never started: every fault ends the run
    before the formatter starts."""
    solution(fixture)
    tools = fixture.root / "bin"
    tools.mkdir()
    for name in ("dotnet-format", "dotnet-format.exe"):
        (tools / name).write_bytes(b"")
        (tools / name).chmod(0o755)


# git's own folder on Windows, which holds no gh or dotnet; elsewhere a folder holding a link to git alone, since
# git's folder there may hold gh.
GIT_ONLY_PATH = str(Path(shutil.which("git") or "git").parent) if sys.platform == "win32" else TOOLS
CORE = "skills/ejryan89-code-review-core/scripts"
CONFIG_SCRIPT = f"{CORE}/review_config.py"
PIPELINE = f"{CORE}/review_pipeline.py"
SOURCE = f"{CORE}/review_source.py"
DOCUMENT = "skills/review-document/scripts/review_document.py"
CONFIG = "{home}/.coding-agent-skills/code-review/config.json"
CONFIG_INPUT = "{root}/config-input.json"
# The run a fixture canary prepared, under the fixture root and named as prepare names one, so the guard and
# review_source take it for a run.
RUN = "{root}/code-review-run-fixture"
RUN_STATE = f"{RUN}/run.json"
REQUEST = f"{RUN}/request.json"
RUN_PLAN = f"{RUN}/work/plan.json"
CANARY_PULL = "{root}/pull"
BATCH = "{root}/batch.json"
ROLE = "generic-review"
REPOSITORY_NAME = "example/inventory"
CHECKOUT = "{work}"
DESIGN = "{work}/docs/design.md"
RECORDED = "{root}/archive/design.md"
OUTPUT = "{root}/out"
# The builder of a prepared run: the fixture canary prepare writes, at RUN, read through the pipeline's own function.
RUN_BUILDER = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import review_pipeline
inline = sys.argv[5] == "inline"
review_pipeline.prepare_fixture(
    Path(sys.argv[2]), host="claude-code", inline=inline, config_path=Path(sys.argv[3]), run_directory=Path(sys.argv[4])
)
if not inline:
    review_pipeline.mark_dispatched(Path(sys.argv[4]))
"""
GENERIC = {
    "reviewer": {
        "id": "generic",
        "protocol_version": 1,
        "trusted_ref": None,
        "scope": "generic",
        "manifest_path": None,
    },
    "checkout_path": None,
}


def review_core_config(fixture: Fixture, repositories: dict[str, object] | None = None) -> dict[str, object]:
    """The code-review configuration at its standard path under the fixture's home."""
    config: dict[str, object] = {
        "schema_version": 1,
        "default_repository_set": "primary",
        "repository_sets": {"primary": [REPOSITORY_NAME]},
        "repositories": repositories or {REPOSITORY_NAME: GENERIC},
        "archive_root": str(fixture.root / "archive"),
        "local_mirror_root": None,
        "summary_root": str(fixture.root / "summary"),
        "dashboard_file": str(fixture.root / "dashboard.md"),
        "github_login": "reviewer",
    }
    path = Path(CONFIG.format(**fixture.values()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


def configured(fixture: Fixture) -> None:
    """The configuration, and a copy of it to write elsewhere."""
    config = review_core_config(fixture)
    Path(CONFIG_INPUT.format(**fixture.values())).write_text(json.dumps(config), encoding="utf-8")


def canary_pull(fixture: Fixture) -> None:
    """The configuration, and a fixture directory holding a pull request that adds one line to a.py and leaves b.py,
    which the run's lazy snapshot lists to fetch, as it was."""
    review_core_config(fixture)
    pull = Path(CANARY_PULL.format(**fixture.values()))
    for folder, text in (("base", "x = 1\n"), ("head", "x = 1\ny = 2\n")):
        (pull / folder).mkdir(parents=True)
        (pull / folder / "a.py").write_text(text, encoding="utf-8")
        (pull / folder / "b.py").write_text("z = 3\n", encoding="utf-8")
    pull_file = {
        "schema_version": 1,
        "repository": REPOSITORY_NAME,
        "number": 1,
        "title": "Add a line",
        "base_ref": "main",
        "head_ref": "change",
        "threads": [],
    }
    (pull / "pull.json").write_text(json.dumps(pull_file), encoding="utf-8")


def prepared(inline: bool) -> Callable[[Fixture], None]:
    def prepare(fixture: Fixture) -> None:
        """A fixture canary prepared at RUN, its roles worked inline or by subagents, as prepare writes one."""
        canary_pull(fixture)
        values = fixture.values()
        temporary = fixture.root / "temporary"
        temporary.mkdir()
        environment = {**git_environment(fixture), "TMP": str(temporary), "TEMP": str(temporary)}
        subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                RUN_BUILDER,
                str(REPOSITORY / CORE),
                CANARY_PULL.format(**values),
                CONFIG.format(**values),
                RUN.format(**values),
                "inline" if inline else "subagents",
            ],
            cwd=fixture.work,
            env=environment,
            check=True,
            capture_output=True,
        )

    return prepare


INLINE_RUN = prepared(inline=True)
SUBAGENT_RUN = prepared(inline=False)


def batch(fixture: Fixture) -> None:
    """The configuration, and a batch file enumerate wrote for the repository, listing no pull request."""
    review_core_config(fixture)
    value = {
        "schema_version": 2,
        "today": "2026-01-02",
        "repositories": {
            REPOSITORY_NAME: {
                "complete": True,
                "error": None,
                "previous_watermark": "2026-01-01",
                "eligible": [],
                "deferred": [],
                "listing": {"scan": "merged", "pages": 1, "pulls": 0, "read": 0},
            }
        },
    }
    Path(BATCH.format(**fixture.values())).write_text(json.dumps(value), encoding="utf-8")


def repository_reviewer(fixture: Fixture) -> None:
    """The configuration of a repository reviewer whose checkout is the work folder, a repository whose origin is the
    configured repository and whose main branch holds its review skill."""
    fixture.repository({".claude/skills/review/SKILL.md": "---\nname: review\ndescription: Review.\n---\n\n# Review\n"})
    fixture.git("remote", "add", "origin", f"https://github.com/{REPOSITORY_NAME}.git")
    fixture.git("update-ref", "refs/remotes/origin/main", "HEAD")
    reviewer = {
        "id": "repository-review",
        "protocol_version": 1,
        "trusted_ref": "refs/remotes/origin/main",
        "scope": "repository",
        "manifest_path": None,
        "skill": ".claude/skills/review",
    }
    review_core_config(fixture, {REPOSITORY_NAME: {"reviewer": reviewer, "checkout_path": str(fixture.work)}})


def document_checkout(fixture: Fixture) -> None:
    """A repository holding a committed design document, changed on disk since."""
    fixture.repository({"docs/design.md": "# Design\n\nThe first draft.\n"})
    Path(DESIGN.format(**fixture.values())).write_text("# Design\n\nThe second draft.\n", encoding="utf-8")


def recorded_report(fixture: Fixture) -> None:
    """A fixture review-document built, and the report and record finalize would write of its review."""
    document_checkout(fixture)
    values = fixture.values()
    subprocess.run(
        [
            sys.executable,
            "-B",
            str(REPOSITORY / DOCUMENT),
            "build",
            DESIGN.format(**values),
            "--output",
            OUTPUT.format(**values),
        ],
        cwd=fixture.work,
        env=git_environment(fixture),
        check=True,
        capture_output=True,
    )
    pull = json.loads(Path(OUTPUT.format(**values), "fixture", "pull.json").read_text(encoding="utf-8"))
    record = {
        "repository": pull["repository"],
        "pull_request": {field: pull[field] for field in ("number", "title", "base_ref", "head_ref")},
    }
    recorded = Path(RECORDED.format(**values))
    recorded.parent.mkdir(parents=True)
    recorded.write_text("# Review\n", encoding="utf-8")
    recorded.with_suffix(".json").write_text(json.dumps(record), encoding="utf-8")


def run_driver(
    command: str, arguments: tuple[str, ...], prepare: Callable[[Fixture], None], inputs: tuple[str, ...]
) -> Driver:
    return Driver(
        PIPELINE,
        command,
        (command, *arguments),
        prepare=prepare,
        inputs=inputs,
        outputs=(RUN,),
    )


FLAG_SCRIPT = "skills/flag-review-finding/scripts/flag_review_finding.py"
ACTIVITY = "skills/github-activity-report/scripts/github_activity_report.py"
CLEANUP = "skills/repo-cleanup/scripts/repo_cleanup.py"
CODE_REVIEW = "{home}/.coding-agent-skills/code-review"
FLAG_STORE = f"{CODE_REVIEW}/flags.json"
ARCHIVED_PULL = "{root}/archive/example/one/pulls/12"
CLEANUP_POLICY = "{checkout}/skills/repo-cleanup/references/cleanup-policy.json"
CLEANUP_PLAN = "{root}/plans/repo.json"
SWEEP_RECORD_FILE = f"{{root}}/plans/{repo_cleanup.SWEEP_RECORD}"
# The flag store's absent case, which review_flags.load_store documents: no store is an empty one.
EMPTY_STORE = "no flag store is an empty store, as review_flags.load_store documents"


def tagged_repository(fixture: Fixture) -> None:
    fixture.repository()
    fixture.git("tag", "v1.0.0")


def repository_with_origin(fixture: Fixture) -> None:
    """A repository whose origin is a bare copy of it, as update-coding-agent-skills' clone is."""
    fixture.repository()
    origin = fixture.root / "origin.git"
    fixture.git("clone", "-q", "--bare", str(fixture.work), str(origin))
    fixture.git("remote", "add", "origin", str(origin))


def flag_store(fixture: Fixture) -> None:
    """A code-review configuration naming an archive that holds three reviews of example/one#12, and a flag store
    holding one open flag on the first review's first finding."""
    folder = fixture.home / ".coding-agent-skills" / "code-review"
    archive = fixture.root / "archive"
    write_config(
        {
            "schema_version": 1,
            "default_repository_set": "primary",
            "repository_sets": {"primary": ["example/one"]},
            "repositories": {
                "example/one": {
                    "reviewer": {
                        "id": "generic",
                        "protocol_version": 1,
                        "trusted_ref": None,
                        "scope": "generic",
                        "manifest_path": None,
                    },
                    "checkout_path": None,
                }
            },
            "operation_repository_sets": {},
            "archive_root": str(archive),
            "local_mirror_root": None,
            "summary_root": str(fixture.root / "summary"),
            "dashboard_file": str(fixture.root / "dashboard.md"),
            "github_login": "reviewer",
        },
        folder / "config.json",
    )
    test_review_fixture.commit_fixture(archive)
    flag = {
        "id": "RF-000001",
        "status": "open",
        "created_at": "2026-01-01T00:00:00+00:00",
        "resolved_at": None,
        "repository": "example/one",
        "pull_number": 12,
        "review_version": 1,
        "finding_id": "F001",
        "category": "guideline",
        "body": "Clarify the boundary.",
        "resolution": None,
    }
    store = {"schema_version": 2, "next_id": 2, "flags": [flag]}
    (folder / "flags.json").write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")


def history(fixture: Fixture) -> None:
    """A history file the report wrote, with its header alone."""
    header = ",".join(github_activity_report.HISTORY_COLUMNS)
    (fixture.work / "history.csv").write_text(header + "\n", encoding="utf-8")


def cleanup_plan(fixture: Fixture, events: list[list[str]]) -> dict[str, object]:
    """An applied plan of a repository under the work folder, as a sweep saves it, with `events` recorded."""
    root = fixture.work / "repo"
    area = fixture.work / repo_cleanup.WORKTREE_AREA
    return {
        "schema_version": repo_cleanup.PLAN_SCHEMA_VERSION,
        "repo_root": str(root),
        "repo_name": root.name,
        "worktree_area": str(area),
        "orphan_areas": [str(area / root.name), str(root.joinpath(*repo_cleanup.REPOSITORY_AREA))],
        "default": {"name": "main", "status": "current"},
        "branches": [],
        "worktrees": [],
        "fastforward": [],
        "fastforward_skipped": [],
        "diverged": [],
        "policy": dict.fromkeys(repo_cleanup.POLICY_FIELDS, ""),
        "applied": True,
        "events": events,
    }


def cleanup_repository(fixture: Fixture) -> Path:
    """A repository under the work folder with a feature branch, and an untracked folder in its worktree area."""
    root = fixture.repository(folder=fixture.work / "repo")
    fixture.git("branch", "feature", folder=root)
    stale = root.joinpath(*repo_cleanup.REPOSITORY_AREA, "stale")
    stale.mkdir(parents=True)
    (stale / "notes.txt").write_text("left behind\n", encoding="utf-8")
    (fixture.root / "plans").mkdir()
    return root


def repositories(fixture: Fixture) -> None:
    """The work folder as the root of the repositories a sweep cleans, holding one."""
    cleanup_repository(fixture)


def offered_branch(fixture: Fixture) -> None:
    """An applied plan that offered the feature branch for deletion and for a forced one."""
    root = cleanup_repository(fixture)
    sha = (root / ".git" / "refs" / "heads" / "feature").read_text(encoding="utf-8").strip()
    plan = cleanup_plan(fixture, [["CONFIRM_LOCAL", "feature"], ["UNMERGED", "feature", sha]])
    plan["branches"] = [
        {
            "name": "feature",
            "category": "local",
            "pr": "NONE",
            "sha": sha,
            "action": "ask-delete",
            "detail": "",
            "worktree": None,
        }
    ]
    repo_cleanup.save_plan(fixture.root / "plans" / "repo.json", plan)


def offered_orphan(fixture: Fixture) -> None:
    """An applied plan that offered the untracked folder in the repository's worktree area for deletion."""
    root = cleanup_repository(fixture)
    stale = root.joinpath(*repo_cleanup.REPOSITORY_AREA, "stale")
    repo_cleanup.save_plan(fixture.root / "plans" / "repo.json", cleanup_plan(fixture, [["ORPHAN", str(stale)]]))


def swept(fixture: Fixture) -> None:
    """A sweep record naming one cleaned repository and its plan."""
    offered_branch(fixture)
    plan = (fixture.root / "plans" / "repo.json").as_posix()
    entry = {
        "root": (fixture.work / "repo").as_posix(),
        "state": "cleaned",
        "plan": plan,
        "lines": [],
        "refused": False,
    }
    repo_cleanup.save_plan(
        fixture.root / "plans" / repo_cleanup.SWEEP_RECORD,
        {"schema_version": repo_cleanup.RECORD_SCHEMA_VERSION, "repositories": [entry], "policy": []},
    )


def confirmation(command: str) -> Driver:
    return Driver(
        CLEANUP,
        command,
        (command, "--plan", CLEANUP_PLAN, "--branch", "feature"),
        prepare=offered_branch,
        inputs=(CLEANUP_PLAN,),
        git=True,
    )


INSIGHTS = "skills/review-insights/scripts/review_insights.py"
DASHBOARD = "skills/update-pr-dashboard/scripts/dashboard_pipeline.py"
TRACKER = f"{CODE_REVIEW}/tracker.json"
REVIEW_RECORD = "{root}/archive/owner/repo/pulls/7/review.json"
INSIGHTS_REPORT = "{root}/summary/both/2026-01-01--2026-01-31/insights.json"
SYNTHESIS_RESULT = "{root}/synthesis-result.json"
DASHBOARD_FILE = "{root}/dashboard.md"
TRACKER_INPUT = "{root}/tracker-input.json"
REPORT_ARGUMENTS = ("report", "--start", "2026-01-01", "--end", "2026-01-31")
REPORT_OUTPUTS = ("{root}/summary",)
# Builds the review-insights fixture with the suite's own helpers: a record of owner/repo#7 with a Correctness finding
# and one a custom-candidate analyzer rule could catch, an open flag on the second, and the report of January 2026.
INSIGHTS_BUILDER = """
import sys
from pathlib import Path
scripts = Path(sys.argv[1]) / "skills" / "review-insights" / "scripts"
sys.path.insert(0, str(scripts))
import test_review_insights as fixtures
from review_archive import commit_record
from review_flags import add_flag
import review_insights
archive = Path(sys.argv[2])
commit_record(
    archive,
    "owner/repo",
    7,
    fixtures.record("owner/repo", 7, ["Correctness", fixtures.covered("custom-candidate", "fixture-tool", "no-rule")]),
    expected_latest_version=None,
)
add_flag(Path(sys.argv[3]), category="Correctness", body="Observation.", repository="owner/repo", pull_number=7,
         review_version=1, finding_id="F002")
sys.exit(review_insights.main(["report", "--start", "2026-01-01", "--end", "2026-01-31"]))
"""


def code_review_config(fixture: Fixture, **values: object) -> dict[str, object]:
    """The code-review configuration under the fixture's home, at its standard path, with `values` beside it."""
    config: dict[str, object] = {
        "schema_version": 1,
        "default_repository_set": "primary",
        "repository_sets": {"primary": ["owner/repo"], "both": ["owner/repo", "owner/other"]},
        "repositories": {"owner/repo": GENERIC, "owner/other": GENERIC},
        "operation_repository_sets": {"review-insights": "both"},
        "archive_root": str(fixture.root / "archive"),
        "local_mirror_root": None,
        "summary_root": str(fixture.root / "summary"),
        "dashboard_file": str(fixture.root / "dashboard.md"),
        "github_login": "reviewer",
        **values,
    }
    path = fixture.home / ".coding-agent-skills" / "code-review" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


def insights_fixture(fixture: Fixture) -> None:
    """The configuration, a git repository as the working folder, and the record, flag, and report INSIGHTS_BUILDER
    makes, with the report's synthesis naming SYNTHESIS_RESULT as its result, which holds an empty object."""
    fixture.repository()
    code_review_config(fixture)
    flags = fixture.home / ".coding-agent-skills" / "code-review" / "flags.json"
    temporary = fixture.root / "temporary"
    temporary.mkdir()
    environment = {**git_environment(fixture), "TMP": str(temporary), "TEMP": str(temporary), "TMPDIR": str(temporary)}
    subprocess.run(
        [sys.executable, "-B", "-c", INSIGHTS_BUILDER, str(REPOSITORY), str(fixture.root / "archive"), str(flags)],
        cwd=fixture.work,
        env=environment,
        check=True,
        capture_output=True,
    )
    values = fixture.values()
    report = Path(INSIGHTS_REPORT.format(**values))
    result = Path(SYNTHESIS_RESULT.format(**values))
    document = json.loads(report.read_text(encoding="utf-8"))
    document["synthesis"]["result"] = str(result)
    report.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result.write_text("{}\n", encoding="utf-8")


def dashboard_fixture(fixture: Fixture) -> None:
    """The configuration, an empty archive and flag store, a dashboard with its markers, a tracker record holding
    owner/repo#1 with a missing review, and a collected input holding no pull request."""
    code_review_config(fixture)
    code_review = fixture.home / ".coding-agent-skills" / "code-review"
    (code_review / "flags.json").write_text(
        json.dumps({"schema_version": 2, "next_id": 1, "flags": []}), encoding="utf-8"
    )
    (code_review / "tracker.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "pulls": ["owner/repo#1"],
                "reviews": {"owner/repo#1": {"state": "missing", "reviewed_head_sha": None}},
            }
        ),
        encoding="utf-8",
    )
    (fixture.root / "archive").mkdir()
    (fixture.root / "dashboard.md").write_text(
        "# Dashboard\n\n<!-- code-review-pr-tracker:start -->\n<!-- code-review-pr-tracker:end -->\n", encoding="utf-8"
    )
    (fixture.root / "tracker-input.json").write_text("[]\n", encoding="utf-8")


DRIVERS: list[Driver] = [
    # audit-ai-config and audit-skill
    Driver(
        AUDIT_AI_CONFIG,
        "",
        ("--json", "--root", "{work}"),
        prepare=configured_repository,
        inputs=(
            "{work}/CLAUDE.md",
            "{work}/AGENTS.md",
            "{work}/.mcp.json",
            "{work}/.claude/settings.json",
        ),
        not_run={MISSING: ABSENT_CONFIGURATION},
    ),
    Driver(
        AUDIT_AI_CONFIG,
        "",
        ("--provenance", "--root", "{work}"),
        prepare=configured_repository,
        inputs=("{checkout}/skills/audit-ai-config/references/provenance.json",),
        from_checkout=True,
        not_run={STAT_RAISES: OPENED_ONLY},
    ),
    Driver(
        MCP_HANDSHAKE,
        "",
        ("--root", "{work}"),
        prepare=configured_repository,
        inputs=("{work}/.mcp.json",),
        not_run={MISSING: NO_MCP_CONFIGURATION},
    ),
    Driver(
        SKILL_INVENTORY,
        "locate",
        ("locate", "demo", "--home", "{home}", "--repo", "{work}"),
        prepare=configured_repository,
        inputs=(f"{SKILL}/SKILL.md",),
        not_run={MISSING: NOT_LOCATED, DIRECTORY: NOT_LOCATED, UNREADABLE: NOT_READ},
    ),
    Driver(
        SKILL_INVENTORY,
        "locate",
        ("locate", "demo", "--home", "{home}"),
        prepare=configured_repository,
        git=True,
    ),
    Driver(
        SKILL_INVENTORY,
        "inventory",
        ("inventory", SKILL),
        prepare=configured_repository,
        inputs=(f"{SKILL}/SKILL.md", f"{SKILL}/references/guide.md"),
        not_run={MISSING: LISTED, DIRECTORY: LISTED},
    ),
    Driver(
        SKILL_INVENTORY,
        "tools",
        ("tools", f"{SKILL}/SKILL.md"),
        prepare=configured_repository,
        inputs=(f"{SKILL}/SKILL.md",),
    ),
    Driver(
        SKILL_INVENTORY,
        "scan",
        ("scan", f"{SKILL}/SKILL.md"),
        prepare=configured_repository,
        inputs=(f"{SKILL}/SKILL.md",),
    ),
    Driver(
        SKILL_INVENTORY,
        "scan",
        ("scan", SKILL),
        prepare=configured_repository,
        inputs=(f"{SKILL}/references/guide.md", f"{SKILL}/scripts/demo.py"),
        not_run={MISSING: LISTED, DIRECTORY: LISTED},
    ),
    Driver(
        SKILL_INVENTORY,
        "provenance",
        ("provenance", "UNGRANTED"),
        inputs=("{checkout}/skills/audit-skill/references/provenance.json",),
        from_checkout=True,
        not_run={STAT_RAISES: OPENED_ONLY},
    ),
    # curate-agent-memory, dotnet-dependencies, and dotnet-format
    Driver(
        MEMORY_AUDIT,
        "audit",
        ("audit", "--memory-dir", MEMORY_STORE, "--repo", "{work}", "--output", "{root}/report.json"),
        prepare=memory_store,
        inputs=(INDEX,),
        outputs=("{root}/report.json",),
    ),
    Driver(
        MEMORY_AUDIT,
        "audit",
        ("audit", "--memory-dir", MEMORY_STORE, "--repo", "{work}", "--output", "{root}/report.json"),
        prepare=memory_store,
        inputs=(INSTRUCTIONS,),
        outputs=("{root}/report.json",),
        not_run={
            MISSING: "a repository without a CLAUDE.md has no such destination (destination_files)",
            DIRECTORY: "a folder among the destinations is passed over, as destination_files says",
        },
    ),
    Driver(
        MEMORY_AUDIT,
        "reindex",
        ("reindex", "--memory-dir", MEMORY_STORE, "--write"),
        prepare=memory_store,
        inputs=(INDEX,),
    ),
    Driver(
        MEMORY_AUDIT,
        "reindex",
        ("reindex", "--memory-dir", MEMORY_STORE, "--write"),
        prepare=memory_store,
        inputs=(MEMORY,),
        not_run={
            MISSING: "an entry whose file is gone is dropped, as the docstring's reindex rules say",
            DIRECTORY: "an entry that is not a regular file stays as written with an UNREADABLE line (docstring)",
        },
    ),
    Driver(
        MEMORY_AUDIT,
        "delete",
        ("delete", "--memory-dir", MEMORY_STORE, "rule.md"),
        prepare=memory_store,
        inputs=(INDEX,),
        not_run={UNREADABLE: "delete requires the index to be there as a file and never reads it"},
    ),
    Driver(
        MEMORY_AUDIT,
        "delete",
        ("delete", "--memory-dir", MEMORY_STORE, "rule.md"),
        prepare=memory_store,
        inputs=(MEMORY,),
        not_run={UNREADABLE: "delete removes the named file and never reads it"},
    ),
    Driver(
        MEMORY_AUDIT,
        "resolve",
        ("resolve", "--repo", "{work}"),
        prepare=memory_store,
        inputs=(USER_SETTINGS,),
        git=True,
        not_run={MISSING: "a settings file that is not there sets nothing, as the docstring's settings order says"},
    ),
    Driver(
        MEMORY_AUDIT,
        "skill-source",
        ("skill-source", "demo", "--repo", "{work}", "--home", "{home}"),
        prepare=deployed_skill,
        inputs=(MANIFEST,),
        not_run={MISSING: "no deployer manifest is no deployment, which records no source (manifest_skill_files)"},
    ),
    Driver(
        MEMORY_AUDIT,
        "skill-source",
        ("skill-source", "demo", "--repo", "{work}", "--home", "{home}"),
        prepare=deployed_skill,
        inputs=(SOURCE_SKILL,),
        not_run={UNREADABLE: "skill-source names the source file and never reads it"},
    ),
    Driver(
        MEMORY_AUDIT,
        "skill-source",
        ("skill-source", "demo", "--home", "{home}"),
        prepare=project_skill,
        git=True,
    ),
    *(
        Driver(
            DEPENDENCY_GRAPH,
            command,
            (command, *arguments, "--root", SOLUTION),
            prepare=solution,
            inputs=(SOLUTION, PROJECT),
        )
        for command, arguments in (
            ("why", ("Newtonsoft.Json",)),
            ("where", ("Newtonsoft.Json",)),
            ("conflicts", ()),
            # The graph is written to a new temporary folder: a file the readers refuse is a finding, an UNREADABLE
            # line beside the graph of the rest, never a refusal that leaves a requested --output unwritten.
            ("graph", ()),
        )
    ),
    Driver(
        DEPENDENCY_GRAPH,
        "conflicts",
        ("conflicts", "--root", SOLUTION),
        prepare=solution,
        inputs=(APP_CONFIG,),
        not_run={
            MISSING: "a project with no App.config has no binding redirect (read_configs)",
            DIRECTORY: "read_configs takes only a regular file named as a config; a folder so named is none",
        },
    ),
    Driver(
        CSHARP_LAYOUT,
        "check",
        ("check", "--repo-root", "{work}", "--file-list", FILE_LIST, "--fix"),
        prepare=solution,
        inputs=(FILE_LIST,),
        not_run={STAT_RAISES: OPENED_ONLY},
    ),
    Driver(
        CSHARP_LAYOUT,
        "check",
        ("check", "--repo-root", "{work}", "--file-list", FILE_LIST, "--fix"),
        prepare=solution,
        inputs=(SOURCE_FILE,),
    ),
    Driver(
        CSHARP_LAYOUT,
        "config",
        ("config", "--repo-root", "{work}", "--solution", "App.sln", "--file-list", FILE_LIST, "--apply"),
        prepare=empty_solution,
        inputs=(FILE_LIST,),
        not_run={STAT_RAISES: OPENED_ONLY},
    ),
    Driver(
        CSHARP_LAYOUT,
        "config",
        ("config", "--repo-root", "{work}", "--solution", "App.sln", "--file-list", FILE_LIST, "--apply"),
        prepare=empty_solution,
        inputs=(SOLUTION, EDITORCONFIG),
        not_run={MISSING: "with no .editorconfig, --apply adds one at the repository root, as the docstring says"},
    ),
    Driver(
        FORMAT_TARGETS,
        "resolve",
        ("resolve", "--cwd", TARGETS_REPOSITORY),
        prepare=changed_branch,
        inputs=(f"{TARGETS_REPOSITORY}/App.sln", f"{TARGETS_REPOSITORY}/src/App/App.csproj"),
        git=True,
        environment={"PATH": GIT_ONLY_PATH},
        not_run={
            MISSING: "no solution, or no project owning the changed file, is STOP: nothing to format (docstring)",
            DIRECTORY: "solutions and projects are found by listing files; a folder named like one is none, so STOP",
        },
    ),
    Driver(
        RUN_FORMAT,
        "",
        ("check", "--repo-root", "{work}", "--solution", "App.sln", "--include-file", FILE_LIST),
        prepare=formatter_inputs,
        inputs=(FILE_LIST,),
        environment={"PATH": TOOLS},
        not_run={STAT_RAISES: OPENED_ONLY},
    ),
    # ejryan89-code-review-core and review-document
    Driver(CONFIG_SCRIPT, "validate", ("validate",), prepare=configured, inputs=(CONFIG,)),
    Driver(
        CONFIG_SCRIPT,
        "write",
        ("write", CONFIG_INPUT, "--output", "{root}/written/config.json"),
        prepare=configured,
        inputs=(CONFIG_INPUT,),
        not_run={STAT_RAISES: OPENED_ONLY},
        outputs=("{root}/written",),
    ),
    Driver(
        PIPELINE,
        "enumerate",
        ("enumerate", "--output", "{root}/batch/batch.json"),
        prepare=configured,
        inputs=(CONFIG,),
        gh=True,
        # No outputs: a repository that fails to list is that batch item's failure, which the batch file records for
        # advance, as the docstring says, so the file is written under the gh fault too.
    ),
    Driver(
        PIPELINE,
        "prepare",
        ("prepare", "--canary", "--fixture", CANARY_PULL, "--inline", "--host", "claude-code"),
        prepare=canary_pull,
        inputs=(CONFIG, f"{CANARY_PULL}/pull.json"),
        git=True,
    ),
    run_driver("dispatch", ("--run", RUN), SUBAGENT_RUN, (RUN_STATE,)),
    run_driver("next-role", ("--run", RUN), INLINE_RUN, (RUN_STATE, REQUEST)),
    run_driver("wait", ("--run", RUN, "--timeout", "1"), SUBAGENT_RUN, (RUN_STATE,)),
    run_driver("host", ("--run", RUN, "--token", "token"), SUBAGENT_RUN, (RUN_STATE,)),
    run_driver("workflow", ("--run", RUN), SUBAGENT_RUN, (RUN_STATE, CONFIG)),
    run_driver("wait-reviewers", ("--run", RUN, "--timeout", "1"), SUBAGENT_RUN, (RUN_STATE, RUN_PLAN)),
    run_driver("validate-result", ("--run", RUN, "--role", ROLE), SUBAGENT_RUN, (RUN_STATE, RUN_PLAN)),
    run_driver("check", ("--run", RUN), SUBAGENT_RUN, (RUN_STATE, RUN_PLAN)),
    run_driver("finalize", ("--run", RUN), SUBAGENT_RUN, (RUN_STATE,)),
    run_driver("unfinalized", ("--run", RUN), SUBAGENT_RUN, (RUN_STATE,)),
    Driver(PIPELINE, "advance", ("advance", "--batch", BATCH), prepare=batch, inputs=(BATCH, CONFIG)),
    Driver(
        PIPELINE,
        "inspect-reviewer",
        ("inspect-reviewer", "--repository", REPOSITORY_NAME),
        prepare=repository_reviewer,
        inputs=(CONFIG,),
        git=True,
    ),
    Driver(
        PIPELINE,
        "validate-reviewer",
        ("validate-reviewer", "--repository", REPOSITORY_NAME),
        prepare=repository_reviewer,
        inputs=(CONFIG,),
        git=True,
    ),
    Driver(
        SOURCE,
        "source-file",
        ("source-file", "--run", RUN, "--role", ROLE, "--path=b.py"),
        prepare=SUBAGENT_RUN,
        inputs=(RUN_STATE, REQUEST),
        git=True,
        outputs=(RUN,),
    ),
    Driver(
        SOURCE,
        "source-search",
        ("source-search", "--run", RUN, "--role", ROLE, "--pattern=def"),
        prepare=SUBAGENT_RUN,
        inputs=(RUN_STATE, REQUEST),
        git=True,
        outputs=(RUN,),
    ),
    Driver(
        DOCUMENT,
        "build",
        ("build", DESIGN, "--output", OUTPUT),
        prepare=document_checkout,
        inputs=(DESIGN,),
        git=True,
        outputs=(OUTPUT,),
    ),
    Driver(
        DOCUMENT,
        "report",
        ("report", "--fixture", f"{OUTPUT}/fixture", "--report", RECORDED),
        prepare=recorded_report,
        inputs=(f"{OUTPUT}/fixture/pull.json", RECORDED, RECORDED.replace(".md", ".json")),
        outputs=(OUTPUT,),
    ),
    # update-coding-agent-skills' calls to origin
    Driver(
        "skills/update-coding-agent-skills/scripts/remote_refs.py",
        "shadows",
        ("shadows", "{work}"),
        prepare=repository_with_origin,
        git=True,
    ),
    Driver(
        "skills/update-coding-agent-skills/scripts/remote_refs.py",
        "fetch",
        ("fetch", "{work}"),
        prepare=repository_with_origin,
        git=True,
    ),
    # release tags, flag-review-finding, github-activity-report, and repo-cleanup
    Driver(
        "skills/ejryan89-skill-core/scripts/release_tags.py",
        "",
        ("{work}", "HEAD"),
        prepare=tagged_repository,
        git=True,
    ),
    Driver(
        FLAG_SCRIPT,
        "add",
        ("add", "guideline", "Clarify boundary", "--repository", "example/one", "--pull", "12", "--finding", "v1 F001"),
        prepare=flag_store,
        inputs=(CONFIG, f"{ARCHIVED_PULL}/review.json", f"{ARCHIVED_PULL}/review.md", FLAG_STORE),
        not_run={
            f"{MISSING} {FLAG_STORE}": EMPTY_STORE + ", which add starts with its first flag",
            f"{STAT_RAISES} {ARCHIVED_PULL}/review.md": OPENED_ONLY,
        },
        outputs=(FLAG_STORE,),
    ),
    Driver(
        FLAG_SCRIPT,
        "edit",
        ("edit", "RF-000001", "--body", "A clearer boundary."),
        prepare=flag_store,
        inputs=(FLAG_STORE,),
        not_run={"missing": EMPTY_STORE + ", which holds no flag to edit"},
    ),
    Driver(
        FLAG_SCRIPT,
        "resolve",
        ("resolve", "RF-000001", "fixed"),
        prepare=flag_store,
        inputs=(FLAG_STORE,),
        not_run={"missing": EMPTY_STORE + ", which holds no flag to resolve"},
    ),
    Driver(
        FLAG_SCRIPT,
        "list",
        ("list",),
        prepare=flag_store,
        inputs=(FLAG_STORE,),
        not_run={"missing": EMPTY_STORE + ", which list prints as COUNT 0"},
    ),
    Driver(
        FLAG_SCRIPT,
        "findings",
        ("findings", "--repository", "example/one", "--pull", "12"),
        prepare=flag_store,
        inputs=(CONFIG, f"{ARCHIVED_PULL}/review.json", f"{ARCHIVED_PULL}/review.md"),
        not_run={f"{STAT_RAISES} {ARCHIVED_PULL}/review.md": OPENED_ONLY},
    ),
    Driver(
        ACTIVITY,
        "",
        ("--org", "example", "--user", "octocat", "--history", "{work}/history.csv"),
        prepare=history,
        inputs=("{work}/history.csv",),
        gh=True,
        not_run={
            "missing": "a history file that does not exist yet holds no rows, as read_history documents",
            STAT_RAISES: OPENED_ONLY,
        },
    ),
    Driver(
        CLEANUP,
        "sweep",
        ("sweep", "--repos-root", "{work}"),
        prepare=repositories,
        inputs=(CLEANUP_POLICY,),
        gh=True,
        not_run={STAT_RAISES: OPENED_ONLY},
        from_checkout=True,
    ),
    Driver(
        CLEANUP,
        "report",
        ("report", "--record", "{root}/plans"),
        prepare=swept,
        inputs=(SWEEP_RECORD_FILE, CLEANUP_PLAN),
    ),
    confirmation("delete-local"),
    confirmation("force-delete"),
    Driver(
        CLEANUP,
        "remove-directory",
        ("remove-directory", "--plan", CLEANUP_PLAN, "--path", "{work}/repo/.claude/worktrees/stale"),
        prepare=offered_orphan,
        inputs=(CLEANUP_PLAN,),
        git=True,
    ),
    # review-insights and update-pr-dashboard
    Driver(INSIGHTS, "scope", ("scope",), prepare=insights_fixture, inputs=(CONFIG,), git=True),
    Driver(INSIGHTS, "report", REPORT_ARGUMENTS, prepare=insights_fixture, inputs=(CONFIG,), outputs=REPORT_OUTPUTS),
    Driver(
        INSIGHTS,
        "report",
        REPORT_ARGUMENTS,
        prepare=insights_fixture,
        inputs=(FLAG_STORE, REVIEW_RECORD, INSIGHTS_REPORT),
        not_run={
            MISSING: f"each is the empty case the code documents: {EMPTY_STORE}; no review.json is no review record "
            "pair, as review_archive.record_files lists them; and no insights.json is a first report, which "
            "create_report writes without an earlier one"
        },
        outputs=REPORT_OUTPUTS,
    ),
    Driver(
        INSIGHTS,
        "report",
        REPORT_ARGUMENTS,
        prepare=insights_fixture,
        inputs=(REVIEW_RECORD.replace(".json", ".md"),),
        not_run={STAT_RAISES: OPENED_ONLY},
        outputs=REPORT_OUTPUTS,
    ),
    Driver(
        INSIGHTS,
        "decide",
        (
            "decide",
            "--report",
            INSIGHTS_REPORT,
            "REC-001",
            "--category",
            "Correctness",
            "--flags",
            "RF-000001",
            "accepted",
        ),
        prepare=insights_fixture,
        inputs=(INSIGHTS_REPORT, FLAG_STORE),
        outputs=("{root}/summary",),
    ),
    Driver(
        INSIGHTS,
        "decide-custom",
        ("decide-custom", "--report", INSIGHTS_REPORT, "--flags", "RF-000001", "accepted"),
        prepare=insights_fixture,
        inputs=(INSIGHTS_REPORT, FLAG_STORE),
        outputs=("{root}/summary",),
    ),
    Driver(
        INSIGHTS,
        "synthesize",
        ("synthesize", "--report", INSIGHTS_REPORT, "--result", SYNTHESIS_RESULT, "--host", "codex"),
        prepare=insights_fixture,
        inputs=(INSIGHTS_REPORT, SYNTHESIS_RESULT, INSIGHTS_REPORT.replace("insights.json", "synthesis-context.json")),
        outputs=("{root}/summary",),
    ),
    Driver(
        INSIGHTS,
        "synthesize",
        ("synthesize", "--report", INSIGHTS_REPORT, "--result", SYNTHESIS_RESULT, "--host", "codex"),
        prepare=insights_fixture,
        inputs=(INSIGHTS_REPORT.replace("insights.json", "synthesis-input.jsonl"),),
        not_run={STAT_RAISES: OPENED_ONLY},
        outputs=("{root}/summary",),
    ),
    Driver(
        DASHBOARD,
        "collect",
        ("collect", "--output", "{root}/collected/input.json"),
        prepare=dashboard_fixture,
        inputs=(CONFIG,),
        gh=True,
        outputs=("{root}/collected",),
    ),
    Driver(
        DASHBOARD,
        "collect",
        ("collect", "--output", "{root}/collected/input.json"),
        prepare=dashboard_fixture,
        inputs=(FLAG_STORE,),
        not_run={MISSING: EMPTY_STORE},
        outputs=("{root}/collected",),
    ),
    Driver(
        DASHBOARD,
        "update",
        ("update", "--input", TRACKER_INPUT),
        prepare=dashboard_fixture,
        inputs=(TRACKER_INPUT, CONFIG, DASHBOARD_FILE),
        outputs=(DASHBOARD_FILE,),
    ),
    Driver(
        DASHBOARD,
        "update",
        ("update", "--input", TRACKER_INPUT),
        prepare=dashboard_fixture,
        inputs=(TRACKER,),
        not_run={
            MISSING: "no tracker record is the one no update has written yet, which read_tracker_record documents as "
            "None and update writes"
        },
        outputs=(DASHBOARD_FILE,),
    ),
    Driver(
        DASHBOARD,
        "override",
        ("override", "--set", "owner/repo#1=waiting on author"),
        prepare=dashboard_fixture,
        inputs=(CONFIG, TRACKER),
    ),
    Driver(
        DASHBOARD,
        "repository",
        ("repository", "--add", "owner/added"),
        prepare=dashboard_fixture,
        inputs=(CONFIG,),
    ),
    Driver(
        DASHBOARD,
        "review-arguments",
        ("review-arguments", "--confirm", "owner/repo#1"),
        prepare=dashboard_fixture,
        inputs=(TRACKER, CONFIG),
    ),
]
NOT_RUN: dict[str, str] = {
    f"{CORE}/review_guard.py": "a PreToolUse hook that reads its event only from stdin, which the harness closes, and "
    "whose contract (EXIT_CONTRACT_EXEMPT) answers every call it cannot evaluate with a deny decision and exit 0, "
    "failing closed; test_review_guard.py holds each unreadable event to that deny",
}
