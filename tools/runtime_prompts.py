"""Keep a rendered sample of each prompt a skill's script writes for a subagent at runtime, for audit-skill.

Usage:
  python tools/runtime_prompts.py           report every missing, stale, or extra sample; exit 1 if there is one
  python tools/runtime_prompts.py --write   render every sample again and remove the extra ones

Either exits 2, with a FAILED line naming the skill or the path, when a skill's prompts cannot be rendered, whatever
its renderer raised, or a sample cannot be read or written; that is never reported as a stale sample.

A skill whose subagent reads a prompt file a script writes at runtime shows the audit nothing of that prompt in its
own files. This tool renders one such prompt per role from the repository's fixtures, the way a fixture canary
does, with no model and no network, in a throwaway folder under the system temporary directory, and keeps it as
docs/runtime-prompts/<skill>/<role>.txt, where `skill_inventory.py scan` measures it. The throwaway folder reads as
<temp> and this checkout as <source>, so a sample names no machine's paths and renders the same everywhere; its
paths are therefore shorter than a real run's, and its estimate slightly low.
The entrypoint sample replaces the fixture's generated commit IDs with <base-commit> and <head-commit>.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))
sys.path.insert(0, str(sibling_scripts("review-document")))
sys.path.insert(0, str(sibling_scripts("review-insights")))

import review_dispatch
import review_document
import review_hosts
import review_pipeline
import review_run
import review_synthesis
from console import run_main
from executable_locator import PYTHON
from git_client import GitClient

from tools import skill_evals_runs
from tools.checkout import checkout_root

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = Path("docs") / "runtime-prompts"
SCENARIOS = REPOSITORY_ROOT / "tests" / "fixtures" / "skill-evals" / "review-prs"
# The pull request review-prs samples, which its generic reviewer reviews.
PULL_FIXTURE = SCENARIOS / "clean-change"
# The design document review-document samples, reviewed whole by the design-review specialist.
DESIGN_DOCUMENT = SCENARIOS / "design-clean" / "head" / "docs" / "design" / "export-download-links.md"
WRITE_HINT = "run python tools/runtime_prompts.py --write"
# A placeholder for the throwaway folder or the checkout and the rest of the path that follows it.
PLACEHOLDER_PATH = re.compile(r"(<temp>|<source>)([\\/][^\s`'\"<>]*)")


class RenderError(RuntimeError):
    pass


def _prepared_prompts(fixture: Path, directory: Path) -> dict[str, str]:
    """Each role's prompt that `prepare --canary --fixture` writes, under a configuration of its own. The run goes
    through the pipeline's own removal, since it holds the fixture repository whose object files git leaves
    read-only."""
    config = directory / "config.json"
    config.write_text(json.dumps(skill_evals_runs.review_config(directory), indent=2), encoding="utf-8")
    run = directory / "run"
    try:
        prepared = review_pipeline.prepare_fixture(fixture, host="claude-code", config_path=config, run_directory=run)
        return {role["id"]: Path(role["prompt_file"]).read_text(encoding="utf-8") for role in prepared["roles"]}
    finally:
        if run.exists():
            review_run.remove_run(run)


def review_prs(directory: Path) -> dict[str, str]:
    return {
        **_prepared_prompts(PULL_FIXTURE, directory),
        "copilot-entrypoint": copilot_entrypoint(directory / "copilot"),
    }


def copilot_fixture(directory: Path) -> Path:
    """The pull fixture with a nondelegating trusted entrypoint and an open human comment, for the bounded host."""
    fixture = directory / "fixture"
    shutil.copytree(PULL_FIXTURE, fixture)
    manifest = {
        "schema_version": 1,
        "id": "copilot-entrypoint",
        "protocol_version": 1,
        "supports": ["initial", "re-review"],
        "required_capabilities": ["isolated-added-root", "read-diff", "write-result"],
        "entrypoint": "review/SKILL.md",
        "resources": [],
        "agent_profiles": [],
    }
    for tree in ("base", "head"):
        reviewer = fixture / tree / "review"
        reviewer.mkdir()
        (reviewer / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (reviewer / "SKILL.md").write_text("Review the change for correctness and report defects.\n", encoding="utf-8")
    pull_path = fixture / "pull.json"
    pull = json.loads(pull_path.read_text(encoding="utf-8"))
    pull["manifest_path"] = "review/manifest.json"
    pull["threads"] = [
        {
            "author": "reviewer",
            "path": "shortfalls.go",
            "line": 1,
            "body": "Please check the changed behavior.",
            "url": "https://example.invalid/review/1",
            "outdated": False,
        }
    ]
    pull_path.write_text(json.dumps(pull), encoding="utf-8")
    return fixture


def copilot_entrypoint(directory: Path) -> str:
    """Render the real bounded host's prompt through prepare and run_copilot, with a recording CLI and no model."""
    fixture = copilot_fixture(directory)
    config = directory / "config.json"
    configuration = skill_evals_runs.review_config(directory)
    configuration["runtime"] = "copilot-cli"
    config.write_text(json.dumps(configuration), encoding="utf-8")
    run = directory / "run"
    prompts: list[str] = []

    def runner(
        arguments: Sequence[str], cwd: Path, environment: Mapping[str, str], stdin: str
    ) -> review_hosts.ProcessResult:
        if "--version" in arguments:
            return review_hosts.ProcessResult(0, "GitHub Copilot CLI 1.0.88", "")
        prompts.append(stdin)
        review_hosts.staging_path(run, 1).write_text('{"status": "complete"}', encoding="utf-8")
        return review_hosts.ProcessResult(0, "", "")

    try:
        prepared = review_pipeline.prepare_fixture(fixture, host="copilot-cli", config_path=config, run_directory=run)
        services = review_run.Services(copilot_runner=runner, copilot_executable="copilot")
        with review_hosts.host_lock(run):
            claim = review_hosts.new_claim(run, generation=0, now=services.clock())
        review_dispatch.run_host(run, claim["token"], services)
        host = review_hosts.host_state(run, probe=services.probe, now=services.clock())
        if host.outcome != "dispatched":
            raise RenderError(f"Copilot prompt fixture host {host.status}/{host.outcome}: {host.reason}")
        request = json.loads(Path(prepared["request_path"]).read_text(encoding="utf-8"))
        pull = request["pull_request"]
        return prompts[0].replace(pull["base_sha"], "<base-commit>").replace(pull["head_sha"], "<head-commit>")
    finally:
        if run.exists():
            review_run.remove_run(run)


def review_document_prompts(directory: Path) -> dict[str, str]:
    # A copy outside any checkout, so the fixture is named the same wherever this checkout is.
    document = directory / DESIGN_DOCUMENT.name
    shutil.copyfile(DESIGN_DOCUMENT, document)
    built = review_document.build(document, "none", directory / "document", GitClient())
    fixture = next(line.removeprefix("FIXTURE ") for line in built if line.startswith("FIXTURE "))
    return _prepared_prompts(Path(fixture), directory)


def review_insights(directory: Path) -> dict[str, str]:
    # The prompt Staged.stage_run writes into a synthesis run, with the throwaway folder as the run.
    prompt = review_synthesis.prompt_text(
        directory,
        script=REPOSITORY_ROOT / "skills" / "review-insights" / "scripts" / "review_insights.py",
        report_path=directory / "insights.json",
        repositories=["example/inventory"],
        span="2026-01-01 to 2026-03-31",
    )
    return {"synthesis": prompt}


# Each skill whose subagent reads a prompt a script writes at runtime, and how to render its prompts: from a throwaway
# folder, each role's prompt.
Renderers = dict[str, Callable[[Path], dict[str, str]]]
RENDERERS: Renderers = {
    "review-document": review_document_prompts,
    "review-insights": review_insights,
    "review-prs": review_prs,
}


def normalized(text: str, directory: Path) -> str:
    """The prompt with the throwaway folder as <temp> and this checkout as <source>; fails on any path left."""
    for path, placeholder in ((directory, "<temp>"), (REPOSITORY_ROOT, "<source>")):
        text = text.replace(str(path), placeholder).replace(path.as_posix(), placeholder)
    # The rest of each such path with forward slashes, so a sample reads the same whatever separator renders it.
    text = PLACEHOLDER_PATH.sub(lambda match: match.group(1) + match.group(2).replace("\\", "/"), text)
    # A command a prompt hands the reviewer names Python by the platform's command; a sample names it as Windows does.
    text = text.replace(f'{PYTHON} -B "', 'python -B "')
    temporary = Path(tempfile.gettempdir()).resolve()
    for left in (str(temporary), temporary.as_posix()):
        if left.casefold() in text.casefold():
            raise RenderError(f"a rendered prompt still names {left}")
    return text


def rendered(renderers: Renderers) -> dict[Path, str]:
    """Every sample, by its path relative to the repository root."""
    samples: dict[Path, str] = {}
    for skill, render in sorted(renderers.items()):
        directory = Path(tempfile.mkdtemp(prefix="runtime-prompt-")).resolve()
        try:
            try:
                prompts = render(directory)
            except RenderError:
                raise
            # A renderer runs a skill's own pipeline, whose errors are its own; any of them is a render failure, never
            # a stale sample.
            except Exception as exc:
                raise RenderError(f"{skill}'s prompts cannot be rendered: {type(exc).__name__}: {exc}") from exc
            samples.update(
                {SAMPLES / skill / f"{role}.txt": normalized(text, directory) for role, text in prompts.items()}
            )
        finally:
            shutil.rmtree(directory)
    return samples


def _sample(root: Path, relative: Path) -> str | None:
    """A sample's text, or None when it is not UTF-8 and so differs from every rendered prompt."""
    try:
        return (root / relative).read_text(encoding="utf-8")
    except UnicodeError:
        return None
    except OSError as exc:
        raise RenderError(f"cannot read {relative.as_posix()}: {exc}") from exc


def problems(root: Path, renderers: Renderers | None = None) -> list[str]:
    """Every sample that is missing, differs from what renders now, or renders from nothing; empty when current."""
    expected = rendered(RENDERERS if renderers is None else renderers)
    found: list[str] = []
    for relative, text in sorted(expected.items()):
        path = root / relative
        if not path.is_file():
            found.append(f"{relative.as_posix()} does not exist; {WRITE_HINT}")
        elif _sample(root, relative) != text:
            found.append(f"{relative.as_posix()} is stale; {WRITE_HINT}")
    found.extend(
        f"{path.relative_to(root).as_posix()} is no rendered prompt; {WRITE_HINT}"
        for path in (sorted((root / SAMPLES).rglob("*")) if (root / SAMPLES).is_dir() else [])
        if path.is_file() and path.relative_to(root) not in expected
    )
    return found


def write(root: Path, renderers: Renderers | None = None) -> None:
    expected = rendered(RENDERERS if renderers is None else renderers)
    for relative, text in expected.items():
        path = root / relative
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        except OSError as exc:
            raise RenderError(f"cannot write {relative.as_posix()}: {exc}") from exc
    for path in sorted((root / SAMPLES).rglob("*"), reverse=True) if (root / SAMPLES).is_dir() else []:
        try:
            if path.is_file() and path.relative_to(root) not in expected:
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except OSError as exc:
            raise RenderError(f"cannot remove {path.relative_to(root).as_posix()}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--write", action="store_true", help="render every sample again and remove the extra ones")
    parser.add_argument("--root", type=checkout_root, default=REPOSITORY_ROOT, help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    try:
        if arguments.write:
            write(arguments.root)
        found = problems(arguments.root)
    except (OSError, RenderError) as exc:
        print(f"FAILED {exc}", file=sys.stderr)
        return 2
    for problem in found:
        print(problem)
    return 1 if found else 0


if __name__ == "__main__":
    run_main(main, errors="backslashreplace")
