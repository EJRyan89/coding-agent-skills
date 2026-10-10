"""Keep a rendered sample of each prompt a skill's script writes for a subagent at runtime, for analyze-skill-cost.

Usage:
  python tools/runtime_prompts.py           report every missing, stale, or extra sample; exit 1 if there is one
  python tools/runtime_prompts.py --write   render every sample again and remove the extra ones

A skill whose subagent reads a prompt file a script writes at runtime shows the audit nothing of that prompt in its
own files. This tool renders one such prompt per role from the repository's fixtures, the way a fixture canary
does, with no model and no network, in a throwaway folder under the system temporary directory, and keeps it as
docs/runtime-prompts/<skill>/<role>.txt, where `skill_inventory.py scan` measures it. The throwaway folder reads as
<temp> and this checkout as <source>, so a sample names no machine's paths and renders the same everywhere; its
paths are therefore shorter than a real run's, and its estimate slightly low.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "skill-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "code-review-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "review-document" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "review-insights" / "scripts"))

import review_document
import review_pipeline
import review_synthesis
from console import use_utf8_output
from git_client import GitClient

from tools import skill_evals

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = Path("docs") / "runtime-prompts"
SCENARIOS = REPOSITORY_ROOT / "tests" / "fixtures" / "skill-evals" / "review-prs"
# The pull request review-prs samples, which its generic reviewer reviews.
PULL_FIXTURE = SCENARIOS / "clean-change"
# The design document review-document samples, reviewed whole by the design-review specialist.
DESIGN_DOCUMENT = SCENARIOS / "design-clean" / "head" / "docs" / "design" / "export-download-links.md"
WRITE_HINT = "run python tools/runtime_prompts.py --write"


class RenderError(RuntimeError):
    pass


def _prepared_prompts(fixture: Path, directory: Path) -> dict[str, str]:
    """Each role's prompt that `prepare --canary --fixture` writes, under a configuration of its own. The run goes
    through the pipeline's own removal, since it holds the fixture repository whose object files git leaves
    read-only."""
    config = directory / "config.json"
    config.write_text(json.dumps(skill_evals.review_config(directory), indent=2), encoding="utf-8")
    run = directory / "run"
    try:
        prepared = review_pipeline.prepare_fixture(fixture, host="claude-code", config_path=config, run_directory=run)
        return {role["id"]: Path(role["prompt_file"]).read_text(encoding="utf-8") for role in prepared["roles"]}
    finally:
        if run.exists():
            review_pipeline.remove_run(run)


def review_prs(directory: Path) -> dict[str, str]:
    return _prepared_prompts(PULL_FIXTURE, directory)


def review_document_prompts(directory: Path) -> dict[str, str]:
    # A copy outside any checkout, so the fixture is named the same wherever this checkout is.
    document = directory / DESIGN_DOCUMENT.name
    shutil.copyfile(DESIGN_DOCUMENT, document)
    built = review_document.build(document, "none", directory / "document", GitClient())
    fixture = next(line.removeprefix("FIXTURE ") for line in built if line.startswith("FIXTURE "))
    return _prepared_prompts(Path(fixture), directory)


def review_insights(directory: Path) -> dict[str, str]:
    prompt = review_synthesis.write_prompt(
        directory,
        script=REPOSITORY_ROOT / "skills" / "review-insights" / "scripts" / "review_insights.py",
        report_path=directory / "insights.json",
        repositories=["example/inventory"],
        span="2026-01-01 to 2026-03-31",
    )
    return {"synthesis": prompt.read_text(encoding="utf-8")}


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
            prompts = render(directory)
            samples.update(
                {SAMPLES / skill / f"{role}.txt": normalized(text, directory) for role, text in prompts.items()}
            )
        finally:
            shutil.rmtree(directory)
    return samples


def problems(root: Path, renderers: Renderers | None = None) -> list[str]:
    """Every sample that is missing, differs from what renders now, or renders from nothing; empty when current."""
    expected = rendered(RENDERERS if renderers is None else renderers)
    found: list[str] = []
    for relative, text in sorted(expected.items()):
        path = root / relative
        if not path.is_file():
            found.append(f"{relative.as_posix()} does not exist; {WRITE_HINT}")
        elif path.read_text(encoding="utf-8") != text:
            found.append(f"{relative.as_posix()} is stale; {WRITE_HINT}")
    for path in sorted((root / SAMPLES).rglob("*")) if (root / SAMPLES).is_dir() else []:
        if path.is_file() and path.relative_to(root) not in expected:
            found.append(f"{path.relative_to(root).as_posix()} is no rendered prompt; {WRITE_HINT}")
    return found


def write(root: Path, renderers: Renderers | None = None) -> None:
    expected = rendered(RENDERERS if renderers is None else renderers)
    for relative, text in expected.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    for path in sorted((root / SAMPLES).rglob("*"), reverse=True) if (root / SAMPLES).is_dir() else []:
        if path.is_file() and path.relative_to(root) not in expected:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--write", action="store_true", help="render every sample again and remove the extra ones")
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT, help=argparse.SUPPRESS)
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
    use_utf8_output(errors="backslashreplace")
    sys.exit(main())
