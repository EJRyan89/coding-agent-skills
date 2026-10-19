"""Judge runtime discovery for the deployable workflow, and write its step summary.

Usage:
  python tools/deployable_report.py verify --label NAME --deploy-verify-exit CODE --deploy-verify-log FILE
                                            --results FILE
  python tools/deployable_report.py layout --label NAME --results FILE
  python tools/deployable_report.py leftovers --results FILE
  python tools/deployable_report.py summary --results FILE [--note TEXT ...] [--summary FILE]

`verify` asks Codex CLI and Copilot CLI which skills they find, the way `python deploy.py verify` does, and judges
each runtime: PASSED when it finds every adapter, SKIPPED when it refuses to list skills until someone signs in,
FAILED otherwise, a runtime that is not installed included, because the workflow installed both. It exits 1 when any
runtime FAILED, when every runtime was SKIPPED (nothing was checked), or when `deploy.py verify` failed for a reason
no skip explains: a skip explains only a failure verify's saved output names for runtimes SKIPPED here. It appends the
pass to the results file. `summary` renders the tool versions and every recorded pass as Markdown, appended to the
step summary file (GITHUB_STEP_SUMMARY by default) and printed.

`layout` is the Claude Code check. Claude Code reads ~/.claude/skills directly, with no adapter, and has no command
that lists skills without a session, so this checks the files instead: Claude Code is installed, every skill the
manifest owns has a non-empty SKILL.md, and every agent it owns is in place. That is a file-layout check, not a
discovery check, and the result says so. It joins the pass with the same label in the results file.

`leftovers` is the uninstall check. After the uninstall, it names every entry, files and directories alike, in the
directories items deploy to, and each item the manifest still records, and records them for the summary; it exits 1
when it names any. It allows what the uninstall leaves by design: the manifest, which keeps the source's entry with no
items, and each directory's retained backups. Every path the deployment owns is an entry of one of those directories,
so the listing after the uninstall is the whole check and nothing is saved before it; the workflow's step before the
uninstall is the layout check, which refuses a profile with nothing deployed.

An absent results file starts an empty record. A results file that cannot be read, is not JSON, or does not hold a
JSON array, and a results file that cannot be written are each an `ERROR:` line naming the file, and the command exits
2, leaving the file as it was.

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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

from console import run_main

from deployer import discovery, journal, manifest, platform_support, tools, verify
from deployer.arguments import PROG, VERIFY_COMMAND_LINE
from deployer.errors import DeployError, print_error
from deployer.kinds import KINDS
from deployer.paths import Paths

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

PASSED = "PASSED"
SKIPPED = "SKIPPED"
FAILED = "FAILED"
# The exit code of a results file the tool cannot read or write, apart from the 1 a check's failure returns.
INPUT_ERROR = 2
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
# What a directory items deploy to holds after the uninstall by design, beside nothing else: the manifest, which keeps
# the source's entry with no items, and the backups a run retained (docs/installation.md, "Uninstalling").
BACKUPS = ".backups"
UNINSTALL = "uninstall"
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
        raise DeployError(f"ERROR: No runtime adapters are deployed in {where}.", f"Deploy first with '{PROG}'.")
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
        raise DeployError(f"ERROR: No skills are deployed in {where}.", f"Deploy first with '{PROG}'.")
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


def _item_roots(paths: Paths) -> list[Path]:
    """The directories items deploy to, once each, in the kinds' order."""
    return list(dict.fromkeys(journal.root_directory(paths, kind.root) for kind in KINDS))


def _root_entries(paths: Paths) -> set[Path]:
    """Every entry in the directories items deploy to, files included, but the manifest and the retained backups."""
    found: set[Path] = set()
    for root in _item_roots(paths):
        if root.is_dir():
            found.update(entry for entry in root.iterdir() if entry != paths.manifest_file and entry.name != BACKUPS)
    return found


def _manifest_items(paths: Paths) -> list[tuple[str, str, Path]]:
    """Each item the manifest records, as its source, its kind's noun, and its deployed path."""
    owned = manifest.load(paths.manifest_file)
    return [
        (source_id, kind.noun, journal.root_directory(paths, kind.root) / name)
        for source_id in sorted(owned.sources)
        for kind in KINDS
        for name in sorted(owned.ownership(source_id).of(kind))
    ]


def leftovers(paths: Paths) -> list[str]:
    """After the uninstall: each entry where items deploy and each item the manifest still records, named by its path
    or by the source that still owns it. Each item deploys to a direct child of its kind's directory, so the listing
    names every path the deployment owned that is still there."""
    left = sorted(platform_support.normalize(path) for path in _root_entries(paths))
    left += [
        f"manifest source '{source_id}' still owns {noun} {platform_support.normalize(path)}"
        for source_id, noun, path in _manifest_items(paths)
    ]
    return left


def verify_failures(output: str) -> set[str]:
    """The runtimes `deploy.py verify` names on its line saying which it failed for, by the names discovery uses; a
    name it does not know stays as printed."""
    runtimes = {label: runtime for runtime, label in discovery.LABELS.items()}
    for line in output.splitlines():
        if line.startswith(verify.FAILED_FOR):
            return {runtimes.get(name, name) for name in line[len(verify.FAILED_FOR) :].rstrip(".").split(" and ")}
    return set()


def exit_code(results: list[RuntimeResult], deploy_verify_exit: int, deploy_verify_output: str) -> int:
    statuses = [result.status for result in results]
    if FAILED in statuses or all(status == SKIPPED for status in statuses):
        return 1
    if deploy_verify_exit == 0:
        return 0
    # deploy.py verify also fails for a runtime that cannot list, which a skip here explains. A failure it names for a
    # runtime that passed here, or one that names no runtime, is a failure of its own.
    failed = verify_failures(deploy_verify_output)
    skipped = {result.runtime for result in results if result.status == SKIPPED}
    return 0 if failed and failed <= skipped else 1


def _verify_log(path: str) -> str:
    """The saved output of deploy.py verify, or "" when there is none to read; PowerShell may start it with a BOM."""
    try:
        return Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


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
        f"## Deployability on a fresh {platform_support.PLATFORM_NAME} runner",
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
    for item in passes:
        if "left" in item:
            lines += ["", "### After the uninstall", ""]
            lines += [f"- Left: `{path}`" for path in item["left"]] or ["Nothing deployed is left."]
    if notes:
        lines += ["", "### Notes", "", *(f"- {note}" for note in notes)]
    return "\n".join(lines) + "\n"


def _read_results(path: Path) -> list[dict]:
    """The recorded passes, none when the file is absent; a file that cannot be read as a JSON array is an input
    error, which leaves the file as it is."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise DeployError(f"ERROR: Cannot read the results file: {path}: {exc}", exit_code=INPUT_ERROR) from exc
    if not isinstance(data, list):
        raise DeployError(f"ERROR: Results file is not a JSON array: {path}", exit_code=INPUT_ERROR)
    return data


def _write_results(path: Path, passes: list[dict]) -> None:
    try:
        path.write_text(json.dumps(passes, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise DeployError(f"ERROR: Cannot write the results file: {path}: {exc}", exit_code=INPUT_ERROR) from exc


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
    _write_results(results_file, passes)


def _record_leftovers(results_file: Path, left: list[str]) -> None:
    """Replace the uninstall's record of what was left, or start it."""
    passes = [item for item in _read_results(results_file) if item.get("label") != UNINSTALL]
    passes.append({"label": UNINSTALL, "left": left})
    _write_results(results_file, passes)


def _verify(arguments: argparse.Namespace) -> int:
    paths = Paths(Path(arguments.source), Path(arguments.home))
    try:
        results = check_runtimes(paths, dict(os.environ))
    except DeployError as exc:
        print_error(exc)
        return exc.exit_code
    for result in results:
        print(f"{result.status} {result.runtime} {result.detail}".rstrip())
    try:
        _record(Path(arguments.results), arguments.label, results)
    except DeployError as exc:
        print_error(exc)
        return exc.exit_code
    return exit_code(results, arguments.deploy_verify_exit, _verify_log(arguments.deploy_verify_log))


def _layout(arguments: argparse.Namespace) -> int:
    paths = Paths(Path(arguments.source), Path(arguments.home))
    try:
        result = check_layout(paths)
    except DeployError as exc:
        print_error(exc)
        return exc.exit_code
    print(f"{result.status} {result.runtime} {result.detail}".rstrip())
    try:
        _record(Path(arguments.results), arguments.label, [result])
    except DeployError as exc:
        print_error(exc)
        return exc.exit_code
    return 0 if result.status == PASSED else 1


def _leftovers(arguments: argparse.Namespace) -> int:
    paths = Paths(Path(arguments.source), Path(arguments.home))
    try:
        left = leftovers(paths)
        _record_leftovers(Path(arguments.results), left)
    except DeployError as exc:
        print_error(exc)
        return exc.exit_code
    for item in left:
        print(f"::error::left after uninstall: {item}")
    if left:
        return 1
    print("Nothing deployed is left where skills, runtime adapters, and agents deploy.")
    return 0


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
        with Path(target).open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("verify", "layout", "leftovers", "summary"):
        command = commands.add_parser(name)
        command.add_argument("--results", required=True, help="the JSON file that collects each pass")
        command.add_argument("--home", default=str(platform_support.home_directory()), help=argparse.SUPPRESS)
        command.add_argument("--source", default=str(REPOSITORY_ROOT), help=argparse.SUPPRESS)
    for name in ("verify", "layout"):
        commands.choices[name].add_argument("--label", required=True)
    commands.choices["verify"].add_argument("--deploy-verify-exit", type=int, required=True)
    commands.choices["verify"].add_argument(
        "--deploy-verify-log", required=True, help=f"the saved output of {VERIFY_COMMAND_LINE}"
    )
    commands.choices["summary"].add_argument("--note", action="append", default=[])
    commands.choices["summary"].add_argument("--summary", default="")
    commands.choices["summary"].add_argument("--no-probe", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    handlers = {"verify": _verify, "layout": _layout, "leftovers": _leftovers, "summary": _summary}
    return handlers[arguments.command](arguments)


if __name__ == "__main__":
    run_main(main, errors="backslashreplace")
