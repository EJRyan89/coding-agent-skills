"""Regression tests for fix_brief.py, which writes the brief a fix is planned from, from its issue. Every test builds
its own repository under a temporary folder with real git and answers gh from a fake, so it holds on every platform."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))

import fix_brief
import lessons_patterns
import private_folder
from git_client import GitClient, GitResult
from github_client import CommandResult, GitHubClient

REPOSITORY = Path(__file__).resolve().parents[4]
# A catalogue of one pattern, whose heading stands at line 5 and whose one form a fixture file holds.
CATALOGUE = """# Lessons

## Patterns

### Walks leave out sidecars

- **Rule:** A walk leaves out sidecars.
- **Why:** A sidecar is not the user's file.
- **No guard:** none yet.
- **Search for:** `os.walk(` over a folder a user names.
"""
WALKS = "`docs/lessons.md:5` Walks leave out sidecars"
# A profile whose Contract files section names a folder, and a file only inside a code block, which names nothing.
PROFILE = """# Implementing changes

## Contract files

- a tool floor under `tools/`;
- the manifest:

```text
`deploy/run.sh`
```

## Pull requests

- `deploy/run.sh` here is no contract entry.
"""
# Files every fixture repository tracks beside the catalogue and the profile.
FILES = {
    "docs/lessons.md": CATALOGUE,
    "docs/implementing-changes.md": PROFILE,
    "tools/reader.py": "import os\n\n\ndef read_config():\n    return os.walk(top)\n",
    "tools/caller.py": "from tools.reader import read_config\n\nread_config()\nread_config()\n",
    "deploy/run.sh": "echo run\n",
    "deploy/run.ps1": "Write-Output run\n",
    "skills/a/SKILL.md": "A skill.\n",
    "skills/b/SKILL.md": "Another skill.\n",
}


def git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


def repository(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    """A git repository tracking FILES and `files`, each written as UTF-8 bytes."""
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "--quiet")
    for name, text in (FILES | (files or {})).items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(text.encode("utf-8"))
    git(root, "add", "--all")
    return root


def issue(body: str, *comments: str, number: int = 7, title: str = "Read the config once") -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/o/r/issues/{number}",
        "body": body,
        "comments": [{"body": comment, "author": {"login": "someone"}} for comment in comments],
    }


class Run:
    """One run of the script: its exit, its lines, the brief it wrote, and the gh commands it ran."""

    def __init__(self, code: int, lines: list[str], calls: list[list[str]]) -> None:
        self.code = code
        self.lines = lines
        self.calls = calls
        found = [line.split("\t", 1)[1] for line in lines if line.startswith("BRIEF\t")]
        self.brief = Path(found[0].strip('"')).read_text(encoding="utf-8") if found else ""

    def section(self, heading: str) -> str:
        """The brief's lines under `## <heading>` up to the next `## ` heading."""
        match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", self.brief, re.MULTILINE | re.DOTALL)
        assert match is not None, heading
        return match.group(1).strip()


def run(
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: object,
    *arguments: str,
    git_client: GitClient | None = None,
    status: int = 0,
) -> Run:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    monkeypatch.setattr(private_folder, "temporary_directory", lambda: out)
    calls: list[list[str]] = []

    def fake_gh(command: Sequence[str]) -> CommandResult:
        calls.append(list(command))
        if status:
            return CommandResult(status, "", "GraphQL: Could not resolve to an issue with the number of 7.")
        return CommandResult(0, json.dumps(answer), "")

    github = GitHubClient(runner=fake_gh, sleeper=lambda _seconds: None)
    code = fix_brief.main([*arguments], root=root, github=github, git=git_client)
    return Run(code, capsys.readouterr().out.split("\n")[:-1], calls)


def test_an_issue_that_names_a_symbol_lists_the_files_holding_it_with_their_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path)
    body = "Calling `read_config()` twice reads the file twice; an `owner` word and `other.ps1` are no symbols."
    result = run(root, tmp_path, monkeypatch, capsys, issue(body), "7")
    assert result.code == 0
    assert result.lines[0] == "ISSUE\t7\tRead the config once"
    assert result.calls == [["gh", "issue", "view", "7", "--json", "number,title,url,body,comments"]]
    assert result.section("Callers") == (
        "### `read_config`\n\n"
        "2 tracked files hold it as a word; `git grep -n -w -F -e 'read_config'` lists them.\n\n"
        "- `tools/caller.py`: 1, 3, 4\n"
        "- `tools/reader.py`: 4"
    )
    assert "owner" not in result.section("Callers")
    assert result.section("Files").startswith("The issue names no tracked file in a code span")


def test_a_symbol_in_a_comment_and_in_each_form_written_as_code_is_searched_by_its_last_identifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path)
    forms = "`reader:read_config`, `tools.reader.read_config`, `GitClient`, `MAX_TWINS`, `a.b`, `Plain`, `run`"
    result = run(root, tmp_path, monkeypatch, capsys, issue("No span here.", f"Also {forms}."), "7")
    searched = re.findall(r"^### `([^`]+)`", result.section("Callers"), re.MULTILINE)
    assert searched == ["read_config", "GitClient", "MAX_TWINS"]


def test_an_issue_that_names_a_path_lists_its_twins_its_lessons_patterns_and_its_contract_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path)
    body = "See `tools/reader.py:4`, `run.sh`, and `docs/implementing-changes.md#contract-files`.\n"
    result = run(root, tmp_path, monkeypatch, capsys, issue(body), "7")
    assert result.code == 0
    assert result.section("Files") == (
        "- `tools/reader.py`: named by the issue\n"
        "- `deploy/run.sh`: named by the issue\n"
        "- `docs/implementing-changes.md`: named by the issue"
    )
    assert result.section("Twins") == "- `deploy/run.sh`: `deploy/run.ps1`"
    assert result.section("Lessons patterns").startswith(f"- {WALKS}: `tools/reader.py:5 holds os.walk(`\n")
    assert result.section("Contract") == (
        "- `docs/implementing-changes.md:5` names `tools/`, which holds `tools/reader.py`"
    )


def test_an_issue_that_names_neither_a_path_nor_a_symbol_still_carries_the_construct_and_the_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path)
    result = run(
        root, tmp_path, monkeypatch, capsys, issue("The config is read twice.\n\n```\n`tools/reader.py`\n```"), "7"
    )
    assert result.code == 0
    assert result.section("Files").startswith("The issue names no tracked file in a code span")
    assert result.section("Callers") == "The issue names no symbol in a code span."
    assert result.section("Twins") == "No file has a twin in the other shell or another tracked file of its name."
    assert result.section("Lessons patterns") == "No pattern of the catalogue meets these files."
    assert (
        result.section("Contract") == "No *Contract files* entry of `docs/implementing-changes.md` names these files."
    )
    assert "## Construct and its spellings (to complete)" in result.brief
    assert result.brief.endswith(fix_brief.RULES.read_text(encoding="utf-8"))


def test_a_name_several_files_hold_is_left_for_the_author_and_a_given_path_not_there_yet_is_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path)
    result = run(root, tmp_path, monkeypatch, capsys, issue("Change `SKILL.md`."), "7", "--path", "tools/new.py")
    assert result.section("Files") == (
        "- `tools/new.py`: given with `--path`, not there yet\n"
        "- `SKILL.md` names 2 tracked files: `skills/a/SKILL.md`, `skills/b/SKILL.md`; give the one meant with `--path`"
    )


def test_the_catalogue_named_by_the_issue_is_not_searched_for_its_own_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path)
    result = run(root, tmp_path, monkeypatch, capsys, issue("Add a pattern to `docs/lessons.md`."), "7")
    assert result.section("Lessons patterns") == (
        "`docs/lessons.md` holds every pattern's forms, so it is not searched for them.\n\n"
        "No pattern of the catalogue meets these files."
    )


def test_each_list_cut_short_says_how_much_it_left_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    holders = {f"many/f{index:02}.py": "use_it()\n" * 7 for index in range(fix_brief.MAX_CALLER_FILES + 2)}
    shims = {f"{folder}/x/SKILL.md": "A shim.\n" for folder in ("c", "d", "e", "f", "g")}
    root = repository(tmp_path, holders | shims)
    symbols = " ".join(f"`name_{index}`" for index in range(fix_brief.MAX_SYMBOLS))
    result = run(root, tmp_path, monkeypatch, capsys, issue(f"`use_it()` {symbols} `skills/a/SKILL.md`"), "7")
    callers = result.section("Callers")
    assert "- `many/f00.py`: 1, 2, 3, 4, 5, and 2 more\n" in callers
    assert "- and 2 more files\n" in callers
    assert callers.endswith("The issue names 1 more symbols past the first 12; search for each by hand.")
    assert result.section("Twins") == (
        "- `skills/a/SKILL.md`: and 6 other tracked files of its name, too common to list"
    )


def test_a_shim_in_a_folder_of_the_files_own_folder_name_is_a_twin_however_common_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    others = {f"skills/{name}/SKILL.md": "A skill.\n" for name in "cdefgh"}
    root = repository(tmp_path, others | {"shims/a/SKILL.md": "A shim.\n"})
    result = run(root, tmp_path, monkeypatch, capsys, issue("`skills/a/SKILL.md`"), "7")
    assert result.section("Twins") == (
        "- `skills/a/SKILL.md`: `shims/a/SKILL.md`, and 7 other tracked files of its name, too common to list"
    )


@pytest.mark.parametrize(
    ("answer", "status", "reason"),
    [
        (None, 1, "FAILED\tcannot read issue #7: "),
        (issue("x", number=8), 0, "FAILED\tgh answered issue #7 with something not that issue"),
        ({**issue("x"), "body": None}, 0, "FAILED\tthe issue's body is not text"),
        ({**issue("x"), "comments": "none"}, 0, "FAILED\tthe issue's comments are not a list of comments"),
    ],
)
def test_an_issue_gh_cannot_read_or_answers_in_another_shape_fails_naming_why(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: object,
    status: int,
    reason: str,
) -> None:
    root = repository(tmp_path)
    result = run(root, tmp_path, monkeypatch, capsys, answer, "7", status=status)
    assert result.code == 1
    assert len(result.lines) == 1
    assert result.lines[0].startswith(reason)
    assert not list((tmp_path / "out").iterdir())


def test_a_tracked_file_the_search_cannot_read_fails_rather_than_reading_as_no_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path)
    reader = lessons_patterns.Corpus.text

    def text(corpus: lessons_patterns.Corpus, name: str) -> str | None:
        if name == "tools/caller.py":
            raise PermissionError(13, "Permission denied", name)
        return reader(corpus, name)

    monkeypatch.setattr(lessons_patterns.Corpus, "text", text)
    result = run(root, tmp_path, monkeypatch, capsys, issue("`read_config`"), "7")
    assert result.code == 1
    assert result.lines == [
        "FAILED\tcannot read a file or write the brief: [Errno 13] Permission denied: 'tools/caller.py'"
    ]
    assert not list((tmp_path / "out").iterdir())


def test_a_tracked_file_that_is_gone_or_not_utf8_holds_no_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path, {"tools/gone.py": "read_config()\n"})
    (root / "tools" / "gone.py").unlink()
    (root / "tools" / "caller.py").write_bytes(b"\xffread_config()\n")
    result = run(root, tmp_path, monkeypatch, capsys, issue("`read_config`"), "7")
    assert result.code == 0
    assert "1 tracked files hold it as a word" in result.section("Callers")
    assert "- `tools/reader.py`: 4" in result.section("Callers")


def test_a_repository_git_cannot_list_fails_naming_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path)

    def runner(command: Sequence[str], timeout: float) -> GitResult:
        return GitResult(128, "", "fatal: not a git repository")

    result = run(root, tmp_path, monkeypatch, capsys, issue("x"), "7", git_client=GitClient(runner=runner))
    assert result.code == 1
    assert result.lines[0].startswith("FAILED\tgit cannot list the repository's files: ")
    assert "fatal: not a git repository" in result.lines[0]


def test_a_path_outside_the_repository_is_refused_before_anything_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path)
    result = run(root, tmp_path, monkeypatch, capsys, issue("x"), "7", "--path", "../outside.py")
    assert result.code == 1
    assert result.lines[0].startswith("FAILED\t../outside.py is refused")
    assert not list((tmp_path / "out").iterdir())


@pytest.mark.parametrize("arguments", [[], ["0"], ["-3"], ["seven"], ["7", "--path"]])
def test_an_issue_that_is_not_a_positive_number_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arguments: list[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        fix_brief.main(arguments, root=tmp_path)
    assert stopped.value.code == 2


def test_each_line_the_docstring_names_is_one_the_script_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path)
    keys = set(re.findall(r"^    ([A-Z]+) [<\"]", fix_brief.__doc__ or "", re.MULTILINE))
    printed = {line.split("\t")[0] for line in run(root, tmp_path, monkeypatch, capsys, issue("x"), "7").lines}
    printed |= {line.split("\t")[0] for line in run(root, tmp_path, monkeypatch, capsys, None, "7", status=1).lines}
    assert keys == printed == {"ISSUE", "BRIEF", "FAILED"}


def test_the_rules_name_the_exact_command_of_each_check_they_ask_for() -> None:
    """A pinned contract: the lines the prompt patterns "A prompt names the exact command for each check it asks for"
    and "A prompt reaches every rule its step must meet" rely on, each an independent literal."""
    rules = fix_brief.RULES.read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", rules)
    for command in (
        "`python -B tests/run_validation.py -k '<suite path>'`",
        "`python -B tests/run_validation.py -k Policies`",
        "`python -B tests/run_validation.py -k ruff`",
        "`python -B .claude/skills/implement-change/scripts/pull_request.py type-checks --base origin/main`",
        "`python -B tests/run_validation.py --pr-title '<title file>' --pr-body '<body file>'`",
    ):
        assert command in flat, command
    assert "Edit only by paths inside your own worktree, never in the main checkout" in flat
    assert "Never run the full validation locally" in flat


def test_each_script_and_document_the_rules_name_is_in_this_repository() -> None:
    rules = fix_brief.RULES.read_text(encoding="utf-8")
    named = set(re.findall(r"python -B ([^ `]+\.py)", rules)) | set(re.findall(r"`([a-z./-]+\.md)`", rules))
    named |= {"CLAUDE.md"}
    assert named >= {"tests/run_validation.py", "docs/implementing-changes.md", "docs/lessons.md"}
    assert [name for name in sorted(named) if not (REPOSITORY / name).is_file()] == []
