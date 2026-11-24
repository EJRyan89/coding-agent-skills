"""Regression tests for lessons_patterns.py, which lists the lessons catalogue's patterns the files a change touches
meet. Every test builds its own repository under a temporary folder, so it holds on every platform."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))

import lessons_patterns
from path_containment import written_refusal

# A catalogue whose line numbers the expected lines name: the Use field at 9 and its continuation at 10, the first
# pattern's Search for at 12, the second's at 19, and the third's at 26.
CATALOGUE = """# Lessons

## Patterns

### Reads through the one reader

- **Rule:** A file is read through its reader.
- **Why:** A second parser drifts.
- **Use:** `json_reader:read_json`, and
  `deployer.config:load_config`.
- **No guard:** none yet.
- **Search for:** `json.loads(` outside `json_reader.py`.

### Walks leave out sidecars

- **Rule:** A walk leaves out sidecars.
- **Why:** A sidecar is not the user's file.
- **No guard:** none yet.
- **Search for:** `os.walk(` over a folder a user names; `never`; `tools/`; `if not <input>:` before a return.

### Dumps refuse NaN

- **Rule:** A dump refuses NaN.
- **Why:** NaN is not JSON.
- **No guard:** none yet.
- **Search for:** `rg -n "json\\.dumps?\\(" -g '*.py' | rg -v allow_nan=False`, and
  `rg -nU "except OSError:\\n\\s+return None" tools`.
"""

# The line the third pattern's form across lines gives for tools/probe.py, whose `except` stands at its line 3.
PROBE_LINE = (
    'SEARCH\tdocs/lessons.md:27\tDumps refuse NaN\ttools/probe.py:3\trg -nU "except OSError:\\n\\s+return None" tools'
)


def repository(tmp_path: Path, files: dict[str, str], catalogue: str | None = CATALOGUE) -> Path:
    """A repository holding the catalogue, a skill's scripts folder, and `files`, each written as UTF-8 bytes."""
    root = tmp_path / "repository"
    (root / "skills" / "core" / "scripts").mkdir(parents=True)
    if catalogue is not None:
        files = {"docs/lessons.md": catalogue, **files}
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(text.encode("utf-8"))
    return root


def run(root: Path, *paths: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, list[str]]:
    code = lessons_patterns.main(list(paths), root=root)
    return code, capsys.readouterr().out.split("\n")[:-1]


def test_a_pattern_is_found_through_a_module_its_use_names(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = repository(
        tmp_path,
        {
            "tools/reader.py": "import json_reader\n",
            "tools/loader.py": "from deployer import config\n",
            "deployer/config.py": "VALUE = 1\n",
            "skills/core/scripts/json_reader.py": "VALUE = 1\n",
        },
    )
    names = ("tools/reader.py", "tools/loader.py", "deployer/config.py", "skills/core/scripts/json_reader.py")
    assert run(root, *names, capsys=capsys) == (
        0,
        [
            "USE\tdocs/lessons.md:9\tReads through the one reader\ttools/reader.py\tjson_reader",
            "USE\tdocs/lessons.md:9\tReads through the one reader\tskills/core/scripts/json_reader.py\tjson_reader",
            "USE\tdocs/lessons.md:10\tReads through the one reader\ttools/loader.py\tdeployer.config",
            "USE\tdocs/lessons.md:10\tReads through the one reader\tdeployer/config.py\tdeployer.config",
        ],
    )


def test_a_pattern_is_found_through_a_search_for_form(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    walk = "".join(
        line + "\n"
        for line in (
            "import os",
            "",
            "# never mind tools",
            "for top in os.walk(folder):",
            "    if not top:",
            "        return",
        )
    )
    root = repository(tmp_path, {"tools/walk.py": walk})
    assert run(root, "tools/walk.py", capsys=capsys) == (
        0,
        [
            "SEARCH\tdocs/lessons.md:19\tWalks leave out sidecars\ttools/walk.py:4\tos.walk(",
            "SEARCH\tdocs/lessons.md:19\tWalks leave out sidecars\ttools/walk.py:5\tif not <input>:",
        ],
    )


def test_an_rg_form_is_searched_as_rg_searches_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = {
        "tools/dump.py": "json.dumps(a, allow_nan=False)\njson.dump(b, out)\n",
        "docs/dump.md": "json.dumps(a)\n",
        "tools/probe.py": "try:\n    probe()\nexcept OSError:\n    return None\n",
        "deployer/probe.py": "try:\n    probe()\nexcept OSError:\n    return None\n",
    }
    root = repository(tmp_path, files)
    assert run(root, *files, capsys=capsys) == (
        0,
        [
            "SEARCH\tdocs/lessons.md:26\tDumps refuse NaN\ttools/dump.py:2\t"
            "rg -n \"json\\.dumps?\\(\" -g '*.py' | rg -v allow_nan=False",
            PROBE_LINE,
        ],
    )


def test_a_match_across_lines_is_numbered_at_each_line_end_text_lines_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path, {"tools/probe.py": "try:\r    probe()\rexcept OSError:\r\n    return None\n"})
    assert run(root, "tools/probe.py", capsys=capsys) == (
        0,
        [PROBE_LINE],
    )


def test_a_file_no_pattern_meets_prints_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = repository(tmp_path, {"tools/plain.py": "import os\n\nVALUE = os.sep\n", "notes.txt": "nothing here\n"})
    assert run(root, "tools/plain.py", "notes.txt", capsys=capsys) == (0, [])


def test_a_file_not_there_yet_is_met_through_its_module_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path, {})
    assert run(root, "skills/core/scripts/json_reader.py", capsys=capsys) == (
        0,
        [
            "NEW\tskills/core/scripts/json_reader.py",
            "USE\tdocs/lessons.md:9\tReads through the one reader\tskills/core/scripts/json_reader.py\tjson_reader",
        ],
    )


def test_python_that_does_not_parse_is_named_and_its_text_still_searched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path, {"tools/broken.py": "import json_reader\ndef (:\n    os.walk(top)\n"})
    assert run(root, "tools/broken.py", capsys=capsys) == (
        0,
        [
            "UNREAD\ttools/broken.py\tinvalid syntax at line 2",
            "SEARCH\tdocs/lessons.md:19\tWalks leave out sidecars\ttools/broken.py:3\tos.walk(",
        ],
    )


def test_a_form_the_lister_cannot_search_is_named_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    catalogue = CATALOGUE + "\n### Typed\n\n- **Rule:** r.\n- **Why:** w.\n- **No guard:** none.\n"
    catalogue += '- **Search for:** `rg -n "x" -t py`.\n'
    root = repository(tmp_path, {"tools/a.py": "x\n", "tools/b.py": "x\n"}, catalogue)
    assert run(root, "tools/a.py", "tools/b.py", capsys=capsys) == (
        0,
        ['UNSEARCHED\tdocs/lessons.md:34\tTyped\trg -n "x" -t py\tthe rg option -t is not read'],
    )


@pytest.mark.parametrize(
    ("catalogue", "reason"),
    [
        (None, "docs/lessons.md does not exist"),
        ("# Lessons\n", "docs/lessons.md has no '## Patterns' section"),
        ("## Patterns\n\nNothing yet.\n", "docs/lessons.md has no pattern under '## Patterns'"),
    ],
)
def test_a_catalogue_that_cannot_be_read_ends_with_a_failed_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], catalogue: str | None, reason: str
) -> None:
    root = repository(tmp_path, {"tools/walk.py": "os.walk(top)\n"}, catalogue)
    assert run(root, "tools/walk.py", capsys=capsys) == (1, [f"FAILED\t{reason}"])


def test_a_catalogue_that_is_not_utf8_ends_with_a_failed_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path, {"tools/walk.py": "os.walk(top)\n"}, None)
    (root / "docs").mkdir()
    (root / "docs" / "lessons.md").write_bytes(b"\xff## Patterns\n")
    assert run(root, "tools/walk.py", capsys=capsys) == (
        1,
        [
            "FAILED\tdocs/lessons.md cannot be read: 'utf-8' codec can't decode byte 0xff in position 0: "
            "invalid start byte"
        ],
    )


def test_a_path_outside_the_repository_is_refused_unread(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = repository(tmp_path, {})
    (tmp_path / "outside.py").write_bytes(b"os.walk(top)\n")
    assert run(root, "../outside.py", capsys=capsys) == (
        1,
        [f"FAILED\t../outside.py is refused: {written_refusal('../outside.py')}"],
    )


def test_a_folder_is_refused_as_not_a_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = repository(tmp_path, {"tools/walk.py": "os.walk(top)\n"})
    assert run(root, "tools", capsys=capsys) == (1, ["FAILED\ttools is not a file"])


def test_the_repositorys_catalogue_is_read(capsys: pytest.CaptureFixture[str]) -> None:
    """The repository's own catalogue reads, and names a mechanism of folder_walk, which the sidecar pattern uses."""
    code, lines = run(lessons_patterns.REPOSITORY, "skills/ejryan89-skill-core/scripts/folder_walk.py", capsys=capsys)
    assert code == 0
    assert any(line.startswith("USE\t") and line.endswith("\tfolder_walk") for line in lines)
