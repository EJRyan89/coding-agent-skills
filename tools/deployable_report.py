"""Judge runtime discovery for the deployable workflow, and write its step summary.

Usage:
  python tools/deployable_report.py verify --label NAME --deploy-verify-exit CODE --results FILE
  python tools/deployable_report.py layout --label NAME --results FILE
  python tools/deployable_report.py summary --results FILE [--note TEXT ...] [--summary FILE]

`verify` asks Codex CLI and Copilot CLI which skills they find, the way `python deploy.py verify` does, and judges
each runtime: PASSED when it finds every adapter, SKIPPED when it refuses to list skills until someone signs in,
FAILED otherwise, a runtime that is not installed included, because the workflow installed both. It exits 1 when any
runtime FAILED, when every runtime was SKIPPED (nothing was checked), or when `deploy.py verify` failed for a reason
no skip explains. It appends the pass to the results file. `summary` renders the tool versions and every recorded
pass as Markdown, appended to the step summary file (GITHUB_STEP_SUMMARY by default) and printed.

`layout` is the Claude Code check. Claude Code reads ~/.claude/skills directly, with no adapter, and has no command
that lists skills without a session, so this checks the files instead: Claude Code is installed, every skill the
manifest owns has a non-empty SKILL.md, and every agent it owns is in place. That is a file-layout check, not a
discovery check, and the result says so. It joins the pass with the same label in the results file.

Nothing here starts a model.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from deployer import discovery, manifest, platform_support, tools, verify  # noqa: E402
from deployer.errors import DeployError, print_error  # noqa: E402
from deployer.paths import Paths  # noqa: E402

PASSED = "PASSED"
SKIPPED = "SKIPPED"
FAILED = "FAILED"
CLAUDE = "claude"
# What a runtime says when it will not list skills for want of a sign-in. Anything else is a failure.
SIGN_IN = re.compile(r"\b(?:log(?:ged)?[ -]?in|sign(?:ed)?[ -]?in|authenticat\w*|unauthori[sz]ed|401)\b", re.IGNORECASE)
# The programs whose versions the summary records, by the name a person knows them by.
VERSIONED = (
    ("Git", "git"),
    ("ShellCheck", "shellcheck"),
    ("PowerShell 7", "pwsh"),
    ("GitHub CLI", "gh"),
    ("Node.js", "node"),
    ("npm", "npm"),
    ("Claude Code", "claude"),
    ("Codex CLI", "codex"),
    ("Copilot CLI", "copilot"),
)
Find = Callable[[str], "str | None"]
RunVersion = Callable[[list[str]], "tuple[int, str]"]


@dataclass(frozen=True)
class RuntimeResult:
    runtime: str
    status: str
    detail: str = ""


def sign_in_problem(message: str) -> bool:
    return SIGN_IN.search(message) is not None


def check_runtimes(
    paths: Paths, environment: Mapping[str, str], find: Find | None = None, talk: discovery.Converse | None = None
) -> list[RuntimeResult]:
    """Judge every runtime against the adapters the manifest records."""
    find = find or platform_support.find_executable
    names = verify.adapter_names(manifest.load(paths.manifest_file))
    if not names:
        where = platform_support.normalize(paths.adapter_dest_dir)
        raise DeployError(
            f"ERROR: No runtime adapters are deployed in {where}.", "Deploy first with 'python deploy.py'."
        )
    results: list[RuntimeResult] = []
    # An empty working directory, so no project skill stands in for an adapter.
    workdir = Path(tempfile.mkdtemp(prefix="deployable-report-"))
    try:
        for runtime in discovery.RUNTIMES:
            executable = find(runtime)
            if executable is None:
                results.append(RuntimeResult(runtime, FAILED, "not installed"))
                continue
            try:
                listing = discovery.list_skills(runtime, executable, workdir, environment, talk=talk)
            except discovery.ListingError as exc:
                status = SKIPPED if sign_in_problem(str(exc)) else FAILED
                results.append(RuntimeResult(runtime, status, f"cannot list its skills: {exc}"))
                continue
            problems = [
                line for line in verify.runtime_lines(names, listing, paths.adapter_dest_dir) if line.action != "FOUND"
            ]
            if problems:
                shown = "; ".join(
                    f"{line.name} {line.action}{f' ({line.detail})' if line.detail else ''}" for line in problems
                )
                results.append(RuntimeResult(runtime, FAILED, shown))
            else:
                results.append(RuntimeResult(runtime, PASSED, f"{len(names)} adapters found"))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return results


def check_layout(paths: Paths, find: Find | None = None) -> RuntimeResult:
    """Claude Code reads the deployed files directly, so check that it is installed and the files are in place."""
    find = find or platform_support.find_executable
    owned = manifest.load(paths.manifest_file)
    entries = [entry for entry in owned.sources.values() if isinstance(entry, dict)]
    skills = sorted({name for entry in entries for name in (entry.get("skills") or {})})
    agents = sorted({name for entry in entries for name in (entry.get("agents") or {})})
    if not skills:
        where = platform_support.normalize(paths.dest_dir)
        raise DeployError(f"ERROR: No skills are deployed in {where}.", "Deploy first with 'python deploy.py'.")
    problems = [] if find(CLAUDE) else ["not installed"]
    for name in skills:
        file = paths.dest_dir / name / "SKILL.md"
        if not file.is_file() or file.stat().st_size == 0:
            problems.append(f"{name}/SKILL.md missing or empty")
    problems += [f"agent {name} missing" for name in agents if not (paths.agent_dest_dir / name).is_file()]
    if problems:
        return RuntimeResult(CLAUDE, FAILED, "; ".join(problems))
    plural = "agent" if len(agents) == 1 else "agents"
    return RuntimeResult(
        CLAUDE,
        PASSED,
        f"{len(skills)} skills and {len(agents)} {plural} in place (file layout only, no session started)",
    )


def exit_code(results: list[RuntimeResult], deploy_verify_exit: int) -> int:
    statuses = [result.status for result in results]
    if FAILED in statuses or all(status == SKIPPED for status in statuses):
        return 1
    # deploy.py verify also fails for a runtime that cannot list, which a skip already explains.
    return 1 if deploy_verify_exit != 0 and SKIPPED not in statuses else 0


def _run_version(arguments: list[str]) -> tuple[int, str]:
    result = platform_support.run_tool(arguments)
    return result.returncode, result.output


def tool_versions(find: Find | None = None, run: RunVersion = _run_version) -> list[tuple[str, str]]:
    find = find or platform_support.find_executable
    versions = [("Python", tools.format_version(tools.python_version()))]
    for label, name in VERSIONED:
        path = find(name)
        if path is None:
            versions.append((label, "not installed"))
            continue
        code, output = run([path, "--version"])
        version = tools.parse_version(output) if code == 0 else None
        versions.append((label, tools.format_version(version) if version else "version could not be read"))
    return versions


def render_summary(passes: list[dict], versions: list[tuple[str, str]], notes: list[str]) -> str:
    lines = [
        "## Deployability on a fresh Windows runner",
        "",
        "### Tool versions",
        "",
        "| Tool | Version |",
        "|---|---|",
        *(f"| {label} | {version} |" for label, version in versions),
    ]
    lines += ["", "### Runtime discovery", "", "| Pass | Runtime | Result | Detail |", "|---|---|---|---|"]
    for item in passes:
        for result in item.get("results", []):
            detail = str(result.get("detail", "")).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {item.get('label', '')} | {result['runtime']} | {result['status']} | {detail} |")
    if notes:
        lines += ["", "### Notes", "", *(f"- {note}" for note in notes)]
    return "\n".join(lines) + "\n"


def _read_results(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(data, list):
        raise DeployError(f"ERROR: Results file is not a JSON array: {path}")
    return data


def _record(results_file: Path, label: str, results: list[RuntimeResult]) -> None:
    """Add results to the pass with this label, or start the pass."""
    passes = _read_results(results_file)
    items = [{"runtime": r.runtime, "status": r.status, "detail": r.detail} for r in results]
    for item in passes:
        if item.get("label") == label:
            item.setdefault("results", []).extend(items)
            break
    else:
        passes.append({"label": label, "results": items})
    results_file.write_text(json.dumps(passes, indent=2) + "\n", encoding="utf-8")


def _verify(arguments: argparse.Namespace) -> int:
    paths = Paths(Path(arguments.source), Path(arguments.home))
    try:
        results = check_runtimes(paths, dict(os.environ))
    except DeployError as exc:
        print_error(exc)
        return exc.exit_code
    for result in results:
        print(f"{result.status} {result.runtime} {result.detail}".rstrip())
    _record(Path(arguments.results), arguments.label, results)
    return exit_code(results, arguments.deploy_verify_exit)


def _layout(arguments: argparse.Namespace) -> int:
    paths = Paths(Path(arguments.source), Path(arguments.home))
    try:
        result = check_layout(paths)
    except DeployError as exc:
        print_error(exc)
        return exc.exit_code
    print(f"{result.status} {result.runtime} {result.detail}".rstrip())
    _record(Path(arguments.results), arguments.label, [result])
    return 0 if result.status == PASSED else 1


def _summary(arguments: argparse.Namespace) -> int:
    try:
        passes = _read_results(Path(arguments.results))
    except DeployError as exc:
        print_error(exc)
        return exc.exit_code
    versions = [] if arguments.no_probe else tool_versions()
    text = render_summary(passes, versions, arguments.note)
    print(text, end="")
    target = arguments.summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("verify", "layout", "summary"):
        command = commands.add_parser(name)
        command.add_argument("--results", required=True, help="the JSON file that collects each pass")
        command.add_argument("--home", default=str(platform_support.home_directory()), help=argparse.SUPPRESS)
        command.add_argument("--source", default=str(REPOSITORY_ROOT), help=argparse.SUPPRESS)
    for name in ("verify", "layout"):
        commands.choices[name].add_argument("--label", required=True)
    commands.choices["verify"].add_argument("--deploy-verify-exit", type=int, required=True)
    commands.choices["summary"].add_argument("--note", action="append", default=[])
    commands.choices["summary"].add_argument("--summary", default="")
    commands.choices["summary"].add_argument("--no-probe", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    return {"verify": _verify, "layout": _layout, "summary": _summary}[arguments.command](arguments)


if __name__ == "__main__":
    sys.exit(main())
