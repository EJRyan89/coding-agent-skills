"""Regression tests for lessons_patterns.py, which lists the lessons catalogue's patterns the files a change touches
meet. Every test builds its own repository under a temporary folder, so it holds on every platform."""

from __future__ import annotations

import re
import subprocess
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

# A catalogue whose line numbers the expected lines name: its patterns' headings stand at lines 5, 14, 22, 30, and 37.
CATALOGUE = """# Lessons

## Patterns

### Reads through the one reader

- **Rule:** A file is read through its reader.
- **Why:** A second parser drifts.
- **Use:** `a helper`, `json_reader:read_json`, `json_reader:read_text`, and
  `deployer.config:load_config`.
- **No guard:** none yet.
- **Search for:** `json.loads(` outside `json_reader.py`.

### Walks leave out sidecars

- **Rule:** A walk leaves out sidecars.
- **Why:** A sidecar is not the user's file.
- **No guard:** none yet.
- **Search for:** `os.walk(` over a folder a user names; `if not <input>:` before a return; `<path>`;
  and `os.walk(` again, and `shutil.rmtree(`.

### Dumps refuse NaN

- **Rule:** A dump refuses NaN.
- **Why:** NaN is not JSON.
- **No guard:** none yet.
- **Search for:** `rg -n "json\\.dumps?\\(" -g '*.py' | rg -v allow_nan=False`, and
  `rg -nU "except OSError:\\n\\s+return None" tools`.

### The package runs

- **Rule:** The package runs through its entry.
- **Why:** A second entry drifts.
- **Use:** `deployer:run` and `deployer.sub.helper:run`.
- **No guard:** none yet.

### Clients list names

- **Rule:** A listing goes through the client.
- **Why:** A second listing drifts.
- **Use:** `git_client:GitClient.names` and `frontmatter:Frontmatter.user_only`.
- **No guard:** none yet.
- **Search for:** `DeployError(` under `deployer/`; `raise` in any module.
"""
READER = "PATTERN\tdocs/lessons.md:5\tReads through the one reader"
WALKS = "PATTERN\tdocs/lessons.md:14\tWalks leave out sidecars"
DUMPS = "PATTERN\tdocs/lessons.md:22\tDumps refuse NaN"
RUNS = "PATTERN\tdocs/lessons.md:30\tThe package runs"
CLIENTS = "PATTERN\tdocs/lessons.md:37\tClients list names"
PROBE_FORM = 'rg -nU "except OSError:\\n\\s+return None" tools'
# Enough files holding nothing a pattern names that a use or a form twelve of them share is common past the floor.
FILLERS = 100
# A count of holders past both the floor of ten and four in a hundred of the files these fixtures track.
COMMON = 12
# Tracked files the corpus reads as holding nothing: one gone from the checkout, one not UTF-8, one a folder has taken
# the place of, and Python that does not parse though it spells a mechanism.
GONE = "gone/missing.py"
BINARY = "filler/binary.py"
FOLDER = "filler/folder.py"
BROKEN = "filler/broken.py"
# Each fixture repository's files, by its root, as git's ls-files would list them once they were added.
TRACKED: dict[Path, list[str]] = {}


def repository(
    tmp_path: Path, files: dict[str, str], catalogue: str | None = CATALOGUE, fillers_py: int = FILLERS
) -> Path:
    """A repository holding the catalogue, a skill's scripts folder, the fillers, the files the corpus reads as
    holding nothing, and `files`, each written as UTF-8 bytes and each tracked."""
    root = tmp_path / "repository"
    (root / "skills" / "core" / "scripts").mkdir(parents=True)
    (root / "filler").mkdir()
    (root / BINARY).write_bytes(b"\xffimport json_reader\n")
    (root / FOLDER).mkdir()
    fillers = {f"filler/f{index}.py": "VALUE = 1\n" for index in range(fillers_py)}
    fillers[BROKEN] = "import json_reader\njson_reader.read_json(\n"
    fillers |= {f"filler/n{index}.md": "A note.\n" for index in range(FILLERS)}
    written = fillers | ({"docs/lessons.md": catalogue} if catalogue is not None else {}) | files
    for name, text in written.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(text.encode("utf-8"))
    TRACKED[root] = sorted([*written, GONE, BINARY, FOLDER])
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
        "tools/reader.py": "import json_reader\nimport os\n\njson_reader.read_json(path)\n",
        "tools/loader.py": "from deployer import config\nfrom os import path\n\nconfig.load_config()\n",
        "tools/bare.py": "import json_reader\n\nVALUE = 1\n",
        "tools/runner.py": "import deployer.config\n\ndeployer.run()\ndeployer.config.load_config()\n",
        "tools/fromer.py": "from json_reader import read_json\nfrom os import path\n",
        "tools/star.py": "from json_reader import *\n\nread_json(path)\n",
    }
    root = repository(tmp_path, files)
    read_json, load_config = "uses json_reader:read_json", "uses deployer.config:load_config"
    assert run(root, *files, capsys=capsys) == (
        0,
        [
            f"{READER}\ttools/reader.py {read_json}\ttools/fromer.py {read_json}\ttools/star.py {read_json}"
            f"\ttools/loader.py {load_config}\ttools/runner.py {load_config}",
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
        "deployer/sub/inner.py": "from .helper import run\n\nrun()\n",
        "top.py": "from .json_reader import read_json\n\nread_json()\n",
    }
    root = repository(tmp_path, files)
    uses = "uses deployer.config:load_config"
    assert run(root, *files, capsys=capsys) == (
        0,
        [
            f"{READER}\tdeployer/cli.py {uses}\tdeployer/other.py {uses}\tdeployer/sub/__init__.py {uses}",
            f"{RUNS}\tdeployer/sub/inner.py uses deployer.sub.helper:run",
        ],
    )


def test_a_mechanism_more_than_ten_other_files_use_is_too_common_to_meet_a_pattern(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    users = {f"tools/user{index}.py": "import json_reader\n\njson_reader.read_json(path)\n" for index in range(COMMON)}
    root = repository(tmp_path, {**users, "skills/core/scripts/json_reader.py": "VALUE = 1\n"})
    assert run(root, "tools/user0.py", "skills/core/scripts/json_reader.py", capsys=capsys) == (
        0,
        [f"{READER}\tskills/core/scripts/json_reader.py is json_reader"],
    )


def test_each_mechanism_is_judged_common_on_its_own_uses(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    users = {f"tools/user{index}.py": "import json_reader\n\njson_reader.read_json(path)\n" for index in range(COMMON)}
    both = "import json_reader\nfrom deployer import config\n\njson_reader.read_json(path)\nconfig.load_config()\n"
    root = repository(tmp_path, {**users, "tools/both.py": both})
    assert run(root, "tools/both.py", capsys=capsys) == (
        0,
        [f"{READER}\ttools/both.py uses deployer.config:load_config"],
    )


def test_a_relative_use_of_a_nested_module_counts_toward_its_commonness(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    users = {f"deployer/sub/user{index}.py": "from .helper import run\n\nrun()\n" for index in range(COMMON)}
    root = repository(tmp_path, {**users, "deployer/sub/helper.py": "def run() -> None: ...\n"})
    assert run(root, "deployer/sub/user0.py", "deployer/sub/helper.py", capsys=capsys) == (
        0,
        [f"{RUNS}\tdeployer/sub/helper.py is deployer.sub.helper"],
    )


def test_a_use_ten_other_files_share_is_not_yet_common_and_the_change_never_counts_toward_its_own(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Eleven users hold it, but the target is one of them, and the broken filler that spells it holds nothing.
    users = {f"tools/user{index}.py": "import json_reader\n\njson_reader.read_json(path)\n" for index in range(11)}
    root = repository(tmp_path, users)
    assert run(root, "tools/user0.py", capsys=capsys) == (0, [f"{READER}\ttools/user0.py uses json_reader:read_json"])


def test_a_use_four_other_files_in_a_hundred_share_is_not_yet_common(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Beside the target, 285 fillers, the broken, binary, folder, and gone files, and twelve users make 300 files.
    users = {f"tools/user{index}.py": "import json_reader\n\njson_reader.read_json(path)\n" for index in range(13)}
    root = repository(tmp_path, users, fillers_py=284)
    assert run(root, "tools/user0.py", capsys=capsys) == (0, [f"{READER}\ttools/user0.py uses json_reader:read_json"])


def test_a_class_member_is_matched_on_the_attribute_a_file_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Twelve files use the class through another member; reading `.names` is still rare, and naming the class is no use.
    others = {
        f"tools/other{index}.py": "from git_client import GitClient\n\nGitClient().output([])\n"
        for index in range(COMMON)
    }
    files = {
        "tools/lister.py": "import git_client\n\ngit.names([])\n",
        "tools/variable.py": "import git_client\n\nnames = []\n",
        "tools/front.py": "from frontmatter import Frontmatter\n\nFrontmatter.parse(text)\n",
        "tools/flagged.py": "import frontmatter\n\nfront.user_only\n",
    }
    root = repository(tmp_path, {**others, **files})
    assert run(root, *files, capsys=capsys) == (
        0,
        [
            f"{CLIENTS}\ttools/lister.py uses git_client:GitClient.names\t"
            "tools/flagged.py uses frontmatter:Frontmatter.user_only"
        ],
    )


def test_a_common_form_still_meets_a_file_in_a_place_its_own_clause_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `DeployError(` shares a clause with `deployer/` and keeps its place; `raise`, in the next clause, does not; and a
    # common use is cut inside the place as anywhere, since a place says where forms matter.
    raisers = {f"tools/raise{index}.py": "raise DeployError(reason)\n" for index in range(COMMON)}
    listers = {f"tools/lister{index}.py": "import git_client\n\ngit.names([])\n" for index in range(COMMON)}
    # A file that only names the place in its text holds no form: the place says where the forms matter.
    mention = {"tools/mention.py": "PLACE = 'deployer/thing.py'\n"}
    thing = "import git_client\n\nraise DeployError(git.names([]))\n"
    root = repository(tmp_path, {**raisers, **listers, **mention, "deployer/thing.py": thing})
    assert run(root, "deployer/thing.py", "tools/raise0.py", "tools/mention.py", capsys=capsys) == (
        0,
        [f"{CLIENTS}\tdeployer/thing.py:3 holds DeployError("],
    )


def test_a_form_common_among_every_kind_is_common_in_a_kind_with_few_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    notes = {f"docs/walk{index}.md": "os.walk(top)\n" for index in range(20)}
    root = repository(tmp_path, {**notes, "tools/clean.sh": "os.walk(top)\nshutil.rmtree(top)\n"})
    assert run(root, "tools/clean.sh", capsys=capsys) == (0, [f"{WALKS}\ttools/clean.sh:2 holds shutil.rmtree("])


def test_a_file_of_a_kind_git_tracks_none_of_is_never_common(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path, {})
    (root / "docs" / "walk.rst").write_bytes(b"os.walk(top)\n")
    assert run(root, "docs/walk.rst", capsys=capsys) == (0, [f"{WALKS}\tdocs/walk.rst:1 holds os.walk("])


def test_a_pattern_is_met_by_a_search_for_form_once_whatever_lines_name_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = ("import os", "", "for top in os.walk(folder):", "    if not top:", "        shutil.rmtree(top)")
    root = repository(tmp_path, {"tools/walk.py": "".join(f"{line}\n" for line in lines)})
    holds = (
        "\ttools/walk.py:3 holds os.walk(\ttools/walk.py:4 holds if not <input>:\ttools/walk.py:5 holds shutil.rmtree("
    )
    assert run(root, "tools/walk.py", capsys=capsys) == (0, [f"{WALKS}{holds}"])
    assert run(root, "tools\\walk.py", capsys=capsys) == (0, [f"{WALKS}{holds}"])


def test_a_file_with_no_extension_is_judged_apart_from_every_kind(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Twelve Python walkers make the form common among every kind, while no other file without an extension holds it.
    walkers = {f"tools/walk{index}.py": "os.walk(top)\n" for index in range(COMMON)}
    root = repository(tmp_path, {**walkers, "tools/clean": "os.walk(top)\nshutil.rmtree(top)\n"})
    assert run(root, "tools/clean", capsys=capsys) == (0, [f"{WALKS}\ttools/clean:2 holds shutil.rmtree("])


def test_a_form_common_among_its_kind_is_cut_there_alone(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Twelve Python walkers make the form common among Python files; 200 more notes keep it rare among every kind.
    walkers = {f"tools/walk{index}.py": "os.walk(top)\n" for index in range(COMMON)}
    notes = {f"docs/note{index}.md": "A note.\n" for index in range(200)}
    root = repository(tmp_path, {**walkers, **notes, "tools/both.py": "os.walk(top)\nshutil.rmtree(top)\n"})
    (root / "docs" / "walk.md").write_bytes(b"os.walk(top)\n")
    assert run(root, "tools/both.py", "docs/walk.md", capsys=capsys) == (
        0,
        [f"{WALKS}\tdocs/walk.md:1 holds os.walk(\ttools/both.py:2 holds shutil.rmtree("],
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


def test_each_rg_form_is_judged_common_on_its_own_matches(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dumpers = {f"tools/dump{index}.py": "json.dump(b, out)\n" for index in range(COMMON)}
    both = "json.dump(b, out)\ntry:\n    probe()\nexcept OSError:\n    return None\n"
    root = repository(tmp_path, {**dumpers, "tools/both.py": both})
    assert run(root, "tools/both.py", capsys=capsys) == (0, [f"{DUMPS}\ttools/both.py:4 holds {PROBE_FORM}"])


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
        ("rg -nA1 x", "a.py", "x\n", 1),
        ("rg -nA 2 x", "a.py", "x\n", 1),
        ("rg --line-number x", "a.py", "x\n", 1),
        ("rg --regexp -x", "a.py", "b -x\n", 1),
        ("rg x -- tools", "tools/a.py", "x\n", 1),
        ("rg x -g", "a.py", "x\n", None),
        ("rg x --glob", "a.py", "x\n", None),
        ("rg x -", "a.py", "x\n", None),
        ('rg -U "a\\n^b$"', "a.py", "a\nb\nc\n", 1),
        ('rg -U "^$"', "a.py", "a\n", None),
        ("rg x tools/", "tools/a.py", "x\n", 1),
        ("rg x ''", "deep/a.py", "x\n", 1),
        ("rg x -g 'a*'", "tools/a.py", "x\n", 1),
        ("rg -e x -e y", "a.py", "a\ny\n", 2),
        ("rg -e x -e y tools", "docs/a.py", "y\n", None),
        ("rg x -g '!*.md'", "a.md", "x\n", None),
        ("rg x -g '!*.md'", "a.py", "x\n", 1),
        ("rg x -g '*.py' -g '!tools/*'", "tools/a.py", "x\n", None),
        ("rg x -g '**/a.py'", "a.py", "x\n", 1),
        ("rg x -g '**/a.py'", "deep/a.py", "x\n", 1),
        ("rg x -g '**/a.py'", "deep/b.py", "x\n", None),
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
    catalogue += '- **Search for:** `rg -n "x" -t py`, then `x = 1`, and\n  `rg -n "x" -t py` again.\n'
    root = repository(tmp_path, {"tools/a.py": "x = 1\n", "tools/b.py": "x\n"}, catalogue)
    assert run(root, "tools/a.py", "tools/b.py", capsys=capsys) == (
        0,
        [
            "PATTERN\tdocs/lessons.md:45\tTyped\ttools/a.py:1 holds x = 1",
            'UNSEARCHED\tdocs/lessons.md:50\tTyped\trg -n "x" -t py\tthe rg option -t is not read',
        ],
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


def test_a_module_that_is_not_utf8_is_named_and_met_through_its_module(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path, {})
    (root / "skills" / "core" / "scripts" / "json_reader.py").write_bytes(b"\xff\n")
    assert run(root, "skills/core/scripts/json_reader.py", capsys=capsys) == (
        0,
        [
            "UNREAD\tskills/core/scripts/json_reader.py\t'utf-8' codec can't decode byte 0xff in position 0: "
            "invalid start byte",
            f"{READER}\tskills/core/scripts/json_reader.py is json_reader",
        ],
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
    # Twelve more walkers would make the form common, but they are untracked, and git lists only the tracked files.
    root = repository(tmp_path, {"tools/walk.py": "os.walk(top)\n"})
    git = GitClient()
    git.output(["init", "--quiet"], directory=root)
    git.output(["add", "--all"], directory=root)
    for index in range(COMMON):
        (root / "tools" / f"untracked{index}.py").write_bytes(b"os.walk(top)\n")
    assert lessons_patterns.main(["tools/walk.py"], root=root) == 0
    assert capsys.readouterr().out == f"{WALKS}\ttools/walk.py:1 holds os.walk(\n"


def test_the_repository_git_lists_is_the_one_at_the_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Twelve tracked walkers make the form common in the fixture, which no listing of another repository would.
    root = repository(tmp_path, {f"tools/walk{index}.py": "os.walk(top)\n" for index in range(COMMON)})
    git = GitClient()
    git.output(["init", "--quiet"], directory=root)
    git.output(["add", "--all"], directory=root)
    assert lessons_patterns.main(["tools/walk0.py"], root=root) == 0
    assert capsys.readouterr().out == ""


def test_the_script_runs_as_a_program_from_any_folder(tmp_path: Path) -> None:
    script = Path(lessons_patterns.__file__)
    finished = subprocess.run(
        [sys.executable, "-B", "-I", str(script), "no/such/file.txt"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        check=False,
    )
    assert (finished.returncode, finished.stdout, finished.stderr) == (0, "NEW\tno/such/file.txt\n", "")


@pytest.mark.skipif(sys.platform == "win32", reason="a symbolic link needs a privilege Windows grants few accounts")
def test_a_tracked_link_out_of_the_repository_is_never_read(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Nine other walkers and the catalogue, which names the form, keep it at ten, the floor; the link out of the
    # repository would make it eleven if it were read.
    walkers = {f"tools/walk{index}.py": "os.walk(top)\n" for index in range(10)}
    root = repository(tmp_path, walkers)
    (tmp_path / "outside.py").write_bytes(b"os.walk(top)\n")
    (root / "tools" / "link.py").symlink_to(tmp_path / "outside.py")
    TRACKED[root].append("tools/link.py")
    assert run(root, "tools/walk0.py", capsys=capsys) == (0, [f"{WALKS}\ttools/walk0.py:1 holds os.walk("])


def test_the_repositorys_own_catalogue_and_tracked_files_are_read(capsys: pytest.CaptureFixture[str]) -> None:
    folder_walk = "skills/ejryan89-skill-core/scripts/folder_walk.py"
    assert lessons_patterns.main([folder_walk]) == 0
    lines = capsys.readouterr().out.split("\n")[:-1]
    assert any(line.startswith("PATTERN\t") and f"\t{folder_walk} is folder_walk" in line for line in lines)
