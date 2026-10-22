"""The audit-skill cues and status-line check, held to the v0.9.0 pull requests that fixed what they name.

Each folder under tests/fixtures/audit-skill/deterministic-prose holds a skill's SKILL.md as it stood before
the pull request that moved its deterministic prose into a script, and as that pull request left it; corpus.json names
the pull request, its merge commit, and the cues the text before it shows. Exactly those of the deterministic-prose
cues fire on the text before, and none fires on the text after.

status-lines holds a SKILL.md before and after a documentation fix that explained lines its script printed, with that
script as the fix left it, which the fix did not change. Each line the fix explained is UNEXPLAINED before it and not
after it.

runtime-prompt-shape holds a runtime prompt sample as it stood before the pull request that put its untrusted data
first and dropped its capitalised emphasis; corpus.json names the cues and lines the sample shows before it, and the
sample the source tree keeps today shows none.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("audit-skill")))

import skill_inventory

REPOSITORY = Path(__file__).resolve().parents[2]
FIXTURES = REPOSITORY / "tests" / "fixtures" / "audit-skill"
CORPUS = json.loads((FIXTURES / "corpus.json").read_text(encoding="utf-8"))
PROSE_CUES = {name for name, _ in skill_inventory.PROSE_CUES}


def scan(*paths: Path) -> list[str]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = skill_inventory.main(["scan", *(str(path) for path in paths)])
    assert code == 0, output.getvalue()
    return output.getvalue().splitlines()


def prose_cues(path: Path) -> set[str]:
    return {line.split()[3] for line in scan(path) if line.startswith("CUE ") and line.split()[3] in PROSE_CUES}


@pytest.mark.parametrize("skill", sorted(CORPUS["deterministic-prose"]))
def test_each_cue_fires_on_the_prose_its_pull_request_moved_into_a_script(skill: str) -> None:
    folder = FIXTURES / "deterministic-prose" / skill
    assert set(CORPUS["deterministic-prose"][skill]["cues"]) == prose_cues(folder / "before.md")


@pytest.mark.parametrize("skill", sorted(CORPUS["deterministic-prose"]))
def test_no_cue_fires_once_the_pull_request_moved_the_prose(skill: str) -> None:
    assert set() == prose_cues(FIXTURES / "deterministic-prose" / skill / "after.md")


def test_the_corpus_holds_every_cue_and_a_text_on_each_side_of_each_fix() -> None:
    named = {cue for entry in CORPUS["deterministic-prose"].values() for cue in entry["cues"]}
    assert named == PROSE_CUES
    folders = {path.name for path in (FIXTURES / "deterministic-prose").iterdir()}
    assert set(CORPUS["deterministic-prose"]) == folders
    for folder in (FIXTURES / "deterministic-prose").iterdir():
        assert sorted(path.name for path in folder.iterdir()) == ["after.md", "before.md"]


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
        assert {line.split()[1] for line in lines if line.startswith("UNEXPLAINED ")} <= {
            (root / script).resolve().as_posix()
        }
    explained = set(entry["unexplained"])
    assert explained <= found["before"]
    assert set() == explained & found["after"]


def shape_cues(lines: list[str], sample: Path) -> list[list[object]]:
    prefix = f"CUE {sample.as_posix()} "
    return [[int(line.split()[2]), line.split()[3]] for line in lines if line.startswith(prefix)]


@pytest.mark.parametrize("role", sorted(CORPUS["runtime-prompt-shape"]))
def test_a_prompt_sample_shows_its_shape_cues_before_its_fix_and_none_after(role: str, tmp_path: Path) -> None:
    entry = CORPUS["runtime-prompt-shape"][role]
    (tmp_path / "deploy-meta").mkdir()
    (tmp_path / "deploy-meta" / f"{entry['skill']}.json").write_text("{}", encoding="utf-8")
    skill = tmp_path / "skills" / entry["skill"] / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Start one subagent with this prompt: `Read <prompt file> and follow it.`\n", encoding="utf-8")
    before = tmp_path / entry["sample"]
    before.parent.mkdir(parents=True)
    before.write_bytes((FIXTURES / "runtime-prompt-shape" / role / "before.txt").read_bytes())
    assert entry["cues"] == shape_cues(scan(skill.parent), before)
    after = REPOSITORY / entry["sample"]
    lines = scan(REPOSITORY / "skills" / entry["skill"])
    assert f"REPLY_BOUNDED {after.as_posix()} yes" in lines
    assert shape_cues(lines, after) == []
