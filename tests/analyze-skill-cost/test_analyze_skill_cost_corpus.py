"""The analyze-skill-cost cues and status-line check, held to the v0.9.0 pull requests that fixed what they name.

Each folder under tests/fixtures/analyze-skill-cost/deterministic-prose holds a skill's SKILL.md as it stood before
the pull request that moved its deterministic prose into a script, and as that pull request left it; corpus.json names
the pull request, its merge commit, and the cues the text before it shows. Exactly those of the deterministic-prose
cues fire on the text before, and none fires on the text after.

status-lines holds a SKILL.md before and after a documentation fix that explained lines its script printed, with that
script as the fix left it, which the fix did not change. Each line the fix explained is UNEXPLAINED before it and not
after it.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("analyze-skill-cost")))

import skill_inventory

# Runs no test of its own; the test functions below check through its assertions.
CHECK = unittest.TestCase()

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "analyze-skill-cost"
CORPUS = json.loads((FIXTURES / "corpus.json").read_text(encoding="utf-8"))
PROSE_CUES = {name for name, _ in skill_inventory.PROSE_CUES}


def scan(*paths: Path) -> list[str]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = skill_inventory.main(["scan", *(str(path) for path in paths)])
    CHECK.assertEqual(0, code, output.getvalue())
    return output.getvalue().splitlines()


def prose_cues(path: Path) -> set[str]:
    return {line.split()[3] for line in scan(path) if line.startswith("CUE ") and line.split()[3] in PROSE_CUES}


@pytest.mark.parametrize("skill", sorted(CORPUS["deterministic-prose"]))
def test_each_cue_fires_on_the_prose_its_pull_request_moved_into_a_script(skill: str) -> None:
    folder = FIXTURES / "deterministic-prose" / skill
    CHECK.assertEqual(set(CORPUS["deterministic-prose"][skill]["cues"]), prose_cues(folder / "before.md"))


@pytest.mark.parametrize("skill", sorted(CORPUS["deterministic-prose"]))
def test_no_cue_fires_once_the_pull_request_moved_the_prose(skill: str) -> None:
    CHECK.assertEqual(set(), prose_cues(FIXTURES / "deterministic-prose" / skill / "after.md"))


def test_the_corpus_holds_every_cue_and_a_text_on_each_side_of_each_fix() -> None:
    named = {cue for entry in CORPUS["deterministic-prose"].values() for cue in entry["cues"]}
    CHECK.assertEqual(PROSE_CUES, named)
    folders = {path.name for path in (FIXTURES / "deterministic-prose").iterdir()}
    CHECK.assertEqual(set(CORPUS["deterministic-prose"]), folders)
    for folder in (FIXTURES / "deterministic-prose").iterdir():
        CHECK.assertEqual(["after.md", "before.md"], sorted(path.name for path in folder.iterdir()))


@pytest.mark.parametrize("skill", sorted(CORPUS["status-lines"]))
def test_the_lines_a_documentation_fix_explained_are_unexplained_before_it_only(skill: str, tmp_path: Path) -> None:
    entry = CORPUS["status-lines"][skill]
    fixture = FIXTURES / "status-lines" / skill
    script = Path(entry["script"])
    found: dict[str, set[str]] = {}
    for side in ("before", "after"):
        root = tmp_path / side
        (root / ".git").mkdir(parents=True)
        skill_md = root / entry["path"]
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text((fixture / f"{side}.md").read_text(encoding="utf-8"), encoding="utf-8")
        (root / script).parent.mkdir(parents=True)
        (root / script).write_text((fixture / f"{script.name}.txt").read_text(encoding="utf-8"), encoding="utf-8")
        lines = scan(skill_md.parent)
        found[side] = {line.split()[3] for line in lines if line.startswith("UNEXPLAINED ")}
        CHECK.assertLessEqual(
            {line.split()[1] for line in lines if line.startswith("UNEXPLAINED ")},
            {(root / script).resolve().as_posix()},
        )
    explained = set(entry["unexplained"])
    CHECK.assertLessEqual(explained, found["before"])
    CHECK.assertEqual(set(), explained & found["after"])
