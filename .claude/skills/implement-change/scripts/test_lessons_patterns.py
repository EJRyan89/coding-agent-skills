"""Regression tests for lessons_patterns.py, which lists the lessons catalogue's patterns the files a change touches
meet. Every test builds its own repository under a temporary folder, so it holds on every platform."""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))

import lessons_patterns
from git_client import GitClient, GitError
from lessons_patterns import Form, Unsearchable
from path_containment import written_refusal

# A catalogue whose line numbers the expected lines name: its patterns' headings stand at lines 5, 14, 22, and 30.
CATALOGUE = """# Lessons

## Patterns

### Reads through the one reader

- **Rule:** A file is read through its reader.
- **Why:** A second parser drifts.
- **Use:** `json_reader:read_json`, `json_reader:read_text`, and
  `deployer.config:load_config`.
- **No guard:** none yet.
- **Search for:** `json.loads(` outside `json_reader.py`.

### Walks leave out sidecars

- **Rule:** A walk leaves out sidecars.
- **Why:** A sidecar is not the user's file.
- **No guard:** none yet.
- **Search for:** `os.walk(` over a folder a user names; `if not <input>:` before a return; `<path>`;
  and `os.walk(` again.

### Dumps refuse NaN

- **Rule:** A dump refuses NaN.
- **Why:** NaN is not JSON.
- **No guard:** none yet.
- **Search for:** `rg -n "json\\.dumps?\\(" -g '*.py' | rg -v allow_nan=False`, and
  `rg -nU "except OSError:\\n\\s+return None" tools`.

### The package runs

- **Rule:** The package runs through its entry.
- **Why:** A second entry drifts.
- **Use:** `deployer:run`.
- **No guard:** none yet.
"""
READER = "PATTERN\tdocs/lessons.md:5\tReads through the one reader"
WALKS = "PATTERN\tdocs/lessons.md:14\tWalks leave out sidecars"
DUMPS = "PATTERN\tdocs/lessons.md:22\tDumps refuse NaN"
RUNS = "PATTERN\tdocs/lessons.md:30\tThe package runs"
PROBE_FORM = 'rg -nU "except OSError:\\n\\s+return None" tools'
# Enough files holding nothing a pattern names that a use or a form three of them share is not common.
FILLERS = 100
# Each fixture repository's files, by its root, as git's ls-files would list them once they were added.
TRACKED: dict[Path, list[str]] = {}


def repository(tmp_path: Path, files: dict[str, str], catalogue: str | None = CATALOGUE) -> Path:
    """A repository holding the catalogue, a skill's scripts folder, the fillers, and `files`, each written as UTF-8
    bytes and each tracked."""
    root = tmp_path / "repository"
    (root / "skills" / "core" / "scripts").mkdir(parents=True)
    fillers = {f"filler/f{index}.py": "VALUE = 1\n" for index in range(FILLERS)}
    fillers |= {f"filler/n{index}.md": "A note.\n" for index in range(FILLERS)}
    written = fillers | ({"docs/lessons.md": catalogue} if catalogue is not None else {}) | files
    for name, text in written.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(text.encode("utf-8"))
    TRACKED[root] = sorted(written)
    return root


def run(root: Path, *paths: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, list[str]]:
    code = lessons_patterns.main(list(paths), root=root, tracked=lambda: TRACKED[root])
    return code, capsys.readouterr().out.split("\n")[:-1]


def test_a_pattern_is_met_by_a_file_that_is_a_module_its_use_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {"skills/core/scripts/json_reader.py": "VALUE = 1\n", "deployer/config.py": "VALUE = 1\n"}
    root = repository(tmp_path, files)
    assert run(root, *files, capsys=capsys) == (
        0,
        [f"{READER}\tskills/core/scripts/json_reader.py is json_reader\tdeployer/config.py is deployer.config"],
    )


def test_a_package_init_is_the_package_and_a_part_that_is_no_identifier_imports_as_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {"deployer/__init__.py": "VALUE = 1\n", "tools/my-tool/json_reader.py": "VALUE = 1\n"}
    root = repository(tmp_path, files)
    assert run(root, *files, capsys=capsys) == (0, [f"{RUNS}\tdeployer/__init__.py is deployer"])


def test_a_pattern_is_met_by_a_file_that_imports_a_module_and_uses_the_name_its_mechanism_binds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {
        "tools/reader.py": "import json_reader\n\njson_reader.read_json(path)\n",
        "tools/loader.py": "from deployer import config\n\nconfig.load_config()\n",
        "tools/bare.py": "import json_reader\n\nVALUE = 1\n",
        "tools/runner.py": "import deployer.config\n\ndeployer.run()\n",
    }
    root = repository(tmp_path, files)
    assert run(root, *files, capsys=capsys) == (
        0,
        [
            f"{READER}\ttools/reader.py uses json_reader:read_json\ttools/loader.py uses deployer.config:load_config",
            f"{RUNS}\ttools/runner.py uses deployer:run",
        ],
    )


def test_a_relative_import_is_resolved_against_the_files_own_package(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {
        "deployer/cli.py": "from .config import load_config\n\nload_config()\n",
        "deployer/other.py": "from . import config\n\nconfig.load_config()\n",
        "deployer/sub/__init__.py": "from ..config import load_config\n\nload_config()\n",
        "deployer/above.py": "from ..json_reader import read_json\n\nread_json()\n",
    }
    root = repository(tmp_path, files)
    uses = "uses deployer.config:load_config"
    assert run(root, *files, capsys=capsys) == (
        0,
        [f"{READER}\tdeployer/cli.py {uses}\tdeployer/other.py {uses}\tdeployer/sub/__init__.py {uses}"],
    )


def test_a_mechanism_more_than_three_files_in_a_hundred_use_is_too_common_to_meet_a_pattern(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    users = {f"tools/user{index}.py": "import json_reader\n\njson_reader.read_json(path)\n" for index in range(5)}
    root = repository(tmp_path, {**users, "skills/core/scripts/json_reader.py": "VALUE = 1\n"})
    assert run(root, "tools/user0.py", "skills/core/scripts/json_reader.py", capsys=capsys) == (
        0,
        [f"{READER}\tskills/core/scripts/json_reader.py is json_reader"],
    )


def test_a_pattern_is_met_by_a_search_for_form_once_whatever_lines_name_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = ("import os", "", "for top in os.walk(folder):", "    if not top:", "        return")
    root = repository(tmp_path, {"tools/walk.py": "".join(f"{line}\n" for line in lines)})
    assert run(root, "tools/walk.py", capsys=capsys) == (
        0,
        [f"{WALKS}\ttools/walk.py:3 holds os.walk(\ttools/walk.py:4 holds if not <input>:"],
    )


def test_a_form_more_than_three_files_in_a_hundred_of_its_kind_hold_is_too_common_to_meet_a_pattern(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    walkers = {f"tools/walk{index}.py": "os.walk(top)\n" for index in range(5)}
    root = repository(tmp_path, {**walkers, "docs/walk.md": "os.walk(top)\n"})
    assert run(root, "tools/walk0.py", "docs/walk.md", capsys=capsys) == (
        0,
        [f"{WALKS}\tdocs/walk.md:1 holds os.walk("],
    )


def test_an_rg_form_is_searched_as_rg_searches_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = {
        "tools/dump.py": "json.dumps(a, allow_nan=False)\njson.dump(b, out)\n",
        "docs/dump.md": "json.dumps(a)\n",
        "tools/probe.py": "try:\n    probe()\nexcept OSError:\n    return None\n",
        "deployer/probe.py": "try:\n    probe()\nexcept OSError:\n    return None\n",
    }
    root = repository(tmp_path, files)
    form = "rg -n \"json\\.dumps?\\(\" -g '*.py' | rg -v allow_nan=False"
    assert run(root, *files, capsys=capsys) == (
        0,
        [f"{DUMPS}\ttools/dump.py:2 holds {form}\ttools/probe.py:3 holds {PROBE_FORM}"],
    )


def test_a_match_across_lines_is_numbered_at_each_line_end_text_lines_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path, {"tools/probe.py": "try:\r    probe()\rexcept OSError:\r\n    return None\n"})
    assert run(root, "tools/probe.py", capsys=capsys) == (0, [f"{DUMPS}\ttools/probe.py:3 holds {PROBE_FORM}"])


@pytest.mark.parametrize(
    ("form", "name", "text", "line"),
    [
        ("rg -n --glob=*.py x", "a.py", "x\n", 1),
        ("rg -n --glob=*.py x", "a.md", "x\n", None),
        ("rg -n --glob '*.py' x", "a.md", "x\n", None),
        ("rg -n -g 'tools/*.py' x", "tools/a.py", "x\n", 1),
        ("rg -n -g 'tools/*.py' x", "other/tools/a.py", "x\n", None),
        ('rg --multiline "a\\nb"', "a.py", "z\na\nb\n", 2),
        ("rg -n x | rg --invert-match y", "a.py", "x y\nx\n", 2),
        ("rg --regexp x tools", "tools/a.py", "x\n", 1),
        ("rg -e x tools", "docs/a.py", "x\n", None),
        ("rg -n -- -x", "a.py", "a\nb -x\n", 2),
        ("rg -n x .", "deep/a.py", "x\n", 1),
        ("rg -n x", "a.py", "y\n", None),
        ("rg -q", "a.py", "run rg -q here\n", 1),
        ("if not <input>:", "a.py", "if not\nvalue:\n", None),
        ("a ... b", "a.py", "a then b\n", 1),
        ("<path>", "a.py", "anything\n", None),
    ],
)
def test_a_form_is_searched_as_its_command_or_its_text_asks(form: str, name: str, text: str, line: int | None) -> None:
    assert Form.of(form).first_line(name, text) == line


@pytest.mark.parametrize(
    ("form", "why"),
    [
        ("rg -t py x", "the rg option -t is not read"),
        ("rg --type py x", "the rg option --type is not read"),
        ('rg -n "("', "Python's re cannot compile the pattern: missing ), unterminated subpattern at position 0"),
        ("rg -n x | grep y", "a command after the first is not an rg naming a pattern"),
    ],
)
def test_a_form_the_lister_cannot_search_says_why(form: str, why: str) -> None:
    with pytest.raises(Unsearchable, match=f"^{re.escape(why)}$"):
        Form.of(form)


def test_a_form_the_lister_cannot_search_is_named_once_for_its_pattern(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    catalogue = CATALOGUE + "\n### Typed\n\n- **Rule:** r.\n- **Why:** w.\n- **No guard:** none.\n"
    catalogue += '- **Search for:** `rg -n "x" -t py`, and\n  `rg -n "x" -t py` again.\n'
    root = repository(tmp_path, {"tools/a.py": "x\n", "tools/b.py": "x\n"}, catalogue)
    assert run(root, "tools/a.py", "tools/b.py", capsys=capsys) == (
        0,
        ['UNSEARCHED\tdocs/lessons.md:42\tTyped\trg -n "x" -t py\tthe rg option -t is not read'],
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
        ["NEW\tskills/core/scripts/json_reader.py", f"{READER}\tskills/core/scripts/json_reader.py is json_reader"],
    )


def test_python_that_does_not_parse_is_named_and_its_text_still_searched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path, {"tools/broken.py": "import json_reader\ndef (:\n    os.walk(top)\n"})
    assert run(root, "tools/broken.py", capsys=capsys) == (
        0,
        ["UNREAD\ttools/broken.py\tinvalid syntax at line 2", f"{WALKS}\ttools/broken.py:3 holds os.walk("],
    )


def test_a_file_that_is_not_utf8_is_named_and_no_form_is_searched_in_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path, {})
    (root / "tools").mkdir()
    (root / "tools" / "image.py").write_bytes(b"\xffos.walk(top)\n")
    assert run(root, "tools/image.py", capsys=capsys) == (
        0,
        ["UNREAD\ttools/image.py\t'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"],
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


def test_files_git_cannot_list_end_with_a_failed_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = repository(tmp_path, {"tools/walk.py": "os.walk(top)\n"})

    def unlisted() -> Sequence[str]:
        raise GitError("fatal: not a git repository")

    assert lessons_patterns.main(["tools/walk.py"], root=root, tracked=unlisted) == 1
    assert capsys.readouterr().out == "FAILED\tgit cannot list the repository's files: fatal: not a git repository\n"


def test_the_tracked_files_are_the_ones_git_lists(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Five more walkers would make the form common, but they are untracked, and git lists only the tracked files.
    root = repository(tmp_path, {"tools/walk.py": "os.walk(top)\n"})
    git = GitClient()
    git.output(["init", "--quiet"], directory=root)
    git.output(["add", "--all"], directory=root)
    for index in range(5):
        (root / "tools" / f"untracked{index}.py").write_bytes(b"os.walk(top)\n")
    assert lessons_patterns.main(["tools/walk.py"], root=root) == 0
    assert capsys.readouterr().out == f"{WALKS}\ttools/walk.py:1 holds os.walk(\n"


def test_the_repositorys_own_catalogue_and_tracked_files_are_read(capsys: pytest.CaptureFixture[str]) -> None:
    folder_walk = "skills/ejryan89-skill-core/scripts/folder_walk.py"
    assert lessons_patterns.main([folder_walk]) == 0
    lines = capsys.readouterr().out.split("\n")[:-1]
    assert any(line.startswith("PATTERN\t") and f"\t{folder_walk} is folder_walk" in line for line in lines)
