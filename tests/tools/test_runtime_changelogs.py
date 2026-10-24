"""Regression suite for tools/runtime_changelogs.py, with every request answered by a stub.

No test reaches the network: each run is given a fetcher that answers from fixture changelogs, fixture release pages,
and stub page responses. The terms are read from this checkout, as a real run reads them.
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from guidance_record import GUIDANCE_PATH, Changelog

from tools import runtime_changelogs as watch
from tools.runtime_changelogs import Fetched, Term

REPOSITORY = Path(__file__).resolve().parents[2]

CLAUDE_CHANGELOG = """# Changelog

## 2.1.305

- Added `--allowedTools` patterns for PowerShell
- Fixed the status line flickering
- Skills now read `model` from frontmatter
  when the session has none

## 2.1.304-0

- A prerelease entry naming --allowedTools

## 2.1.303

### Fixed

- Fixed `CLAUDE_CONFIG_DIR` being ignored under ~/.claude/skills
- Fixed the model picker

## 2.1.302

- Changed --allowedTools, already read
"""


def release(tag: str, body: str, *, prerelease: bool = False) -> dict[str, object]:
    return {"tag_name": tag, "body": body, "prerelease": prerelease, "draft": False}


CODEX_PAGES = [
    [
        release("rust-v0.165.0-alpha.1", "- `--sandbox` in an alpha", prerelease=True),
        release("rust-v0.164.0", "## Changes\n\n- `codex exec --ephemeral` keeps no session\n- Faster startup"),
    ],
    [
        release("rust-v0.163.1", "- The `--sandbox` flag reads `allow_implicit_invocation`"),
        release("rust-v0.163.0", "- `--sandbox` changed, already read"),
    ],
]

WATCHED = [
    Term("flag", "--allowedTools", "claude-code"),
    Term("flag", "--sandbox", "codex"),
    Term("flag", "--ephemeral", "codex"),
    Term("variable", "CLAUDE_CONFIG_DIR"),
    Term("path", ".claude/skills"),
    Term("frontmatter", "model"),
    Term("frontmatter", "allow_implicit_invocation"),
]

CLAUDE = Changelog("claude-code", "https://example.com/claude.md", "markdown", "2.1.302")
CODEX = Changelog("codex", "https://api.example.com/codex/releases", "github-releases", "0.163.0")


class StubFetcher:
    """Answers each address from a table; an address it does not hold answers 200 where it was asked."""

    def __init__(self, answers: dict[str, Fetched]) -> None:
        self.answers = answers
        self.requested: list[str] = []
        self.timeouts: dict[str, float] = {}

    def __call__(self, url: str, timeout: float) -> Fetched:
        self.requested.append(url)
        self.timeouts[url] = timeout
        return self.answers.get(url, Fetched(200, url, b"<html></html>"))


def answers() -> dict[str, Fetched]:
    found = {CLAUDE.url: Fetched(200, CLAUDE.url, CLAUDE_CHANGELOG.encode("utf-8"))}
    for number, page in enumerate(CODEX_PAGES, start=1):
        url = f"{CODEX.url}?per_page=100&page={number}"
        found[url] = Fetched(200, url, json.dumps(page).encode("utf-8"))
    return found


class ChangelogTests(unittest.TestCase):
    def test_a_markdown_changelog_keeps_each_newer_release_and_skips_prereleases(self) -> None:
        releases = watch.changelog_releases(CLAUDE, StubFetcher(answers()), 1.0)
        self.assertEqual(["2.1.305", "2.1.303"], [found.name for found in releases])
        self.assertEqual(
            (
                "Added `--allowedTools` patterns for PowerShell",
                "Fixed the status line flickering",
                "Skills now read `model` from frontmatter when the session has none",
            ),
            releases[0].entries,
        )

    def test_github_releases_are_read_page_by_page_until_the_recorded_release(self) -> None:
        fetcher = StubFetcher(answers())
        releases = watch.changelog_releases(CODEX, fetcher, 1.0)
        self.assertEqual(["0.164.0", "0.163.1"], [found.name for found in releases])
        self.assertEqual([f"{CODEX.url}?per_page=100&page=1", f"{CODEX.url}?per_page=100&page=2"], fetcher.requested)

    def test_kept_entries_name_the_terms_they_mention_each_in_its_own_changelog(self) -> None:
        claude = watch.changelog_lines(CLAUDE, watch.changelog_releases(CLAUDE, StubFetcher(answers()), 1.0), WATCHED)
        self.assertEqual(
            [
                "CHANGELOG claude-code 2.1.302 2.1.305 3",
                'ENTRY claude-code 2.1.303 "CLAUDE_CONFIG_DIR .claude/skills" '
                '"Fixed `CLAUDE_CONFIG_DIR` being ignored under ~/.claude/skills"',
                'ENTRY claude-code 2.1.305 "--allowedTools" "Added `--allowedTools` patterns for PowerShell"',
                'ENTRY claude-code 2.1.305 "model" '
                '"Skills now read `model` from frontmatter when the session has none"',
            ],
            claude,
        )
        codex = watch.changelog_lines(CODEX, watch.changelog_releases(CODEX, StubFetcher(answers()), 1.0), WATCHED)
        self.assertEqual(
            [
                "CHANGELOG codex 0.163.0 0.164.0 2",
                'ENTRY codex 0.163.1 "--sandbox allow_implicit_invocation" '
                '"The `--sandbox` flag reads `allow_implicit_invocation`"',
                'ENTRY codex 0.164.0 "--ephemeral" "`codex exec --ephemeral` keeps no session"',
            ],
            codex,
        )

    def test_a_changelog_with_no_newer_release_lists_the_recorded_one_as_the_latest(self) -> None:
        recorded = Changelog(CLAUDE.subject, CLAUDE.url, CLAUDE.format, "2.1.305")
        releases = watch.changelog_releases(recorded, StubFetcher(answers()), 1.0)
        self.assertEqual(
            ["CHANGELOG claude-code 2.1.305 2.1.305 0"], watch.changelog_lines(recorded, releases, WATCHED)
        )

    def test_a_changelog_that_cannot_be_fetched_or_read_stops_the_watch(self) -> None:
        cases = (
            (CLAUDE, {CLAUDE.url: Fetched(503, CLAUDE.url, reason="HTTP 503 Unavailable")}, "HTTP 503 Unavailable"),
            (CLAUDE, {CLAUDE.url: Fetched(200, CLAUDE.url, b"# Changelog\n\nNothing here.\n")}, "names no release"),
            (CODEX, {f"{CODEX.url}?per_page=100&page=1": Fetched(403, CODEX.url, reason="rate limit")}, "rate limit"),
            (CODEX, {f"{CODEX.url}?per_page=100&page=1": Fetched(200, CODEX.url, b"{")}, "did not return JSON"),
            (CODEX, {f"{CODEX.url}?per_page=100&page=1": Fetched(200, CODEX.url, b"{}")}, "not return a list"),
        )
        for changelog, table, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(watch.WatchError) as caught:
                watch.changelog_releases(changelog, StubFetcher({**answers(), **table}), 1.0)
            self.assertIn(reason, str(caught.exception))

    def test_github_releases_that_never_reach_the_recorded_release_stop_the_watch(self) -> None:
        page = json.dumps([release("rust-v9.0.0", "- newer")]).encode("utf-8")
        table = {f"{CODEX.url}?per_page=100&page={n}": Fetched(200, CODEX.url, page) for n in range(1, 11)}
        with self.assertRaises(watch.WatchError) as caught:
            watch.changelog_releases(CODEX, StubFetcher(table), 1.0)
        self.assertIn("name no release up to 0.163.0", str(caught.exception))


# Real changelog entries, captured with their provenance, one for each way an entry writes a term.
CAPTURED = REPOSITORY / "tests" / "fixtures" / "captured-output" / "changelogs"
PROVENANCE = Path("tests") / "fixtures" / "captured-output" / "provenance.json"
ENTRIES: dict[str, dict[str, str]] = json.loads((CAPTURED / "entries.json").read_text(encoding="utf-8"))["entries"]
COPILOT_CHANGELOG = (CAPTURED / "copilot-cli-changelog.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("term", "entry", "matched"),
    [
        pytest.param(Term("flag", "--no-remote"), "flag-after-a-slash", True, id="flag-after-a-slash"),
        pytest.param(Term("flag", "-p"), "short-flag-after-a-slash", True, id="short-flag-after-a-slash"),
        pytest.param(Term("flag", "-p"), "flag-in-parentheses-and-flag-with-a-value", True, id="flag-in-parentheses"),
        pytest.param(
            Term("flag", "--output-format"), "flag-in-parentheses-and-flag-with-a-value", True, id="flag-with-a-value"
        ),
        pytest.param(Term("flag", "--mcp-config"), "flag-in-a-code-span", True, id="flag-in-a-code-span"),
        pytest.param(Term("flag", "--resume"), "flag-with-a-value-in-a-code-span", True, id="flag-value-in-code"),
        pytest.param(Term("flag", "-C"), "flag-with-a-value-in-a-code-span", True, id="short-flag-in-code"),
        pytest.param(Term("flag", "--plugin-dir"), "flag-inside-a-longer-flag", True, id="flag"),
        pytest.param(Term("flag", "--plugin"), "flag-inside-a-longer-flag", False, id="flag-prefix"),
        pytest.param(Term("flag", "-p"), "flag-inside-a-longer-flag", False, id="short-flag-inside-a-longer-one"),
        pytest.param(Term("variable", "NO_PROXY"), "variables-as-words", True, id="variable"),
        pytest.param(Term("variable", "ALL_PROXY"), "variables-as-words", True, id="variable-ending-the-entry"),
        pytest.param(Term("variable", "COPILOT_HOME"), "variable-and-path-as-words", True, id="variable-first"),
        pytest.param(Term("variable", "HOME"), "variable-and-path-as-words", False, id="variable-suffix"),
        pytest.param(Term("variable", "TERM"), "one-word-variable-in-a-code-span", True, id="one-word-variable-code"),
        pytest.param(Term("variable", "HOME"), "one-word-variable-after-a-dollar", True, id="one-word-variable-dollar"),
        pytest.param(Term("variable", "VAR"), "one-word-variable-in-braces", True, id="one-word-variable-braces"),
        pytest.param(Term("path", ".claude/skills"), "path-in-a-code-span", True, id="path-in-a-code-span"),
        pytest.param(Term("path", ".agents/skills"), "variable-and-path-as-words", True, id="path-under-the-home"),
        pytest.param(Term("path", ".agents/skills"), "path-split-by-a-parenthesis", False, id="path-split"),
        pytest.param(Term("frontmatter", "allowed-tools"), "hyphenated-key-in-a-code-span", True, id="hyphenated-key"),
        pytest.param(Term("frontmatter", "model"), "one-word-key-in-a-code-span", True, id="plain-key-in-code"),
        pytest.param(Term("frontmatter", "model"), "one-word-key-as-a-word", False, id="plain-key-as-a-word"),
    ],
)
def test_a_term_matches_only_where_a_real_entry_names_it(term: Term, entry: str, matched: bool) -> None:
    captured = ENTRIES[entry]
    assert (watch.matching(captured["entry"], captured["changelog"], [term]) == [term.text]) is matched


CODEX_RELEASES = json.loads((CAPTURED / "codex-releases.json").read_text(encoding="utf-8"))


def test_a_real_codex_releases_page_is_read_by_release_with_its_prerelease_left_out() -> None:
    releases = watch.github_releases(CODEX_RELEASES, "https://api.github.com/repos/openai/codex/releases")
    assert [release.release for release in releases] == [(0, 164, 0), (0, 163, 0)]
    assert releases[0].entries[-1] == (
        "CLI configuration overrides are preserved when `-c` or `--config` appears at multiple subcommand levels. "
        "(#56022)"
    )
    assert watch.matching(releases[0].entries[-1], "codex", [Term("flag", "-c", "codex")]) == ["-c"]
    changelog = Changelog("codex", "https://example.com/releases", "github-releases", "0.163.0")
    url = f"{changelog.url}?per_page=100&page=1"
    fetcher = StubFetcher({url: Fetched(200, url, json.dumps(CODEX_RELEASES).encode("utf-8"))})
    assert [release.name for release in watch.changelog_releases(changelog, fetcher, 1.0)] == ["0.164.0"]


# No captured changelog writes a variable in PowerShell's or cmd's form, so these two entries are written here.
@pytest.mark.parametrize(
    "entry", [pytest.param("reads $env:PATH on Windows", id="powershell"), pytest.param("%PATH% is read", id="cmd")]
)
def test_a_one_word_variable_matches_in_a_windows_shells_form(entry: str) -> None:
    assert watch.matching(entry, "codex", [Term("variable", "PATH")]) == ["PATH"]


def test_the_versions_a_changelog_is_read_across_are_its_recorded_one_and_each_newer_release() -> None:
    changelog = Changelog("copilot-cli", "https://example.com/copilot.md", "markdown", "1.0.100")
    fetcher = StubFetcher({changelog.url: Fetched(200, changelog.url, COPILOT_CHANGELOG.encode("utf-8"))})
    releases = watch.changelog_releases(changelog, fetcher, 1.0)
    assert watch.versions_read(changelog, releases) == [(1, 0, 100), (1, 0, 103), (1, 0, 102), (1, 0, 101)]


def test_a_flag_is_matched_only_in_its_own_runtimes_changelog() -> None:
    flag = Term("flag", "--sandbox", "codex")
    assert watch.matching("the --sandbox flag", "copilot-cli", [flag]) == []
    assert watch.matching("the --sandbox flag", "codex", [flag]) == ["--sandbox"]


@pytest.mark.parametrize(
    ("fetched", "line"),
    [
        pytest.param(Fetched(200, "https://example.com/a"), None, id="where-asked"),
        pytest.param(Fetched(200, "https://example.com/a/#top"), None, id="slash-and-fragment"),
        pytest.param(
            Fetched(200, "https://example.com/b"), "MOVED https://example.com/a https://example.com/b", id="moved"
        ),
        pytest.param(Fetched(404, "https://example.com/a"), "GONE https://example.com/a 404", id="not-found"),
        pytest.param(Fetched(410, "https://example.com/a"), "GONE https://example.com/a 410", id="gone"),
        pytest.param(
            Fetched(403, "https://example.com/a", reason="HTTP 403 Forbidden"),
            'UNREADABLE https://example.com/a "HTTP 403 Forbidden"',
            id="forbidden",
        ),
        pytest.param(
            Fetched(0, "https://example.com/a", reason="timed out"),
            'UNREADABLE https://example.com/a "timed out"',
            id="unreachable",
        ),
    ],
)
def test_a_page_is_reported_only_when_it_moved_is_gone_or_cannot_be_read(fetched: Fetched, line: str | None) -> None:
    assert watch.page_line("https://example.com/a", fetched) == line


VARIABLES_MODULE = """
import os
from os import environ
NAME = "FROM_CONSTANT"
PROXY_VARIABLES = ("HTTPS_PROXY", "lowercase_proxy")
def f(env, environment, variables, settings):
    os.environ["FROM_SUBSCRIPT"]
    env.get("FROM_GET")
    environ.pop(NAME)
    os.getenv("FROM_GETENV")
    "FROM_IN" in environment
    {**environment, "FROM_SPREAD": "1"}
    settings["NOT_AN_ENVIRONMENT"]
    {"NOT_SPREAD": 1}
    variables.get("NOT_A_MAPPING_NAME")
"""


def test_the_variables_a_module_reads_or_sets_are_its_environment_keys_only() -> None:
    tree = ast.parse(VARIABLES_MODULE)
    assert watch.module_variables(tree, watch.module_constants(tree)) == {
        "FROM_CONSTANT",
        "HTTPS_PROXY",
        "FROM_SUBSCRIPT",
        "FROM_GET",
        "FROM_GETENV",
        "FROM_IN",
        "FROM_SPREAD",
    }


class RepositoryTermsTests(unittest.TestCase):
    """The terms read from this checkout hold what its code uses, each pinned here by its literal."""

    terms: ClassVar[list[Term]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.terms = watch.terms(REPOSITORY)

    def texts(self, kind: str, subject: str | None = None) -> set[str]:
        return {term.text for term in self.terms if term.kind == kind and term.subject == subject}

    def test_flags_come_from_each_runtimes_command_line_builders(self) -> None:
        self.assertLessEqual({"-p", "--allowedTools", "--setting-sources"}, self.texts("flag", "claude-code"))
        self.assertLessEqual({"--sandbox", "--skip-git-repo-check"}, self.texts("flag", "codex"))
        self.assertLessEqual({"--add-dir", "--no-custom-instructions"}, self.texts("flag", "copilot-cli"))

    def test_variables_paths_and_keys_come_from_the_code_the_deployer_and_skills_run(self) -> None:
        self.assertLessEqual(
            {"CLAUDE_CONFIG_DIR", "CODEX_HOME", "COPILOT_HOME", "COPILOT_AUTO_UPDATE", "CLAUDE_SKILL_DIR"},
            self.texts("variable"),
        )
        self.assertLessEqual(
            {".claude/skills", ".agents/skills", ".claude/agents", ".copilot/skills", "agents/openai.yaml"},
            self.texts("path"),
        )
        self.assertLessEqual(
            {"allowed-tools", "argument-hint", "disable-model-invocation", "allow_implicit_invocation"},
            self.texts("frontmatter"),
        )

    def test_each_term_is_listed_once_in_kind_order(self) -> None:
        self.assertEqual(len(self.terms), len(set(self.terms)))
        kinds = [term.kind for term in self.terms]
        self.assertEqual(sorted(kinds, key=watch.KINDS.index), kinds)


class MainTests(unittest.TestCase):
    """A run over this checkout's guidance record, with every request answered by a stub."""

    def table(self) -> dict[str, Fetched]:
        record = watch.read_record(REPOSITORY)
        table: dict[str, Fetched] = {}
        for changelog in record.changelogs:
            if changelog.format == "markdown":
                text = f"## 99.0.0\n\n- Changed --allowedTools and `allowed-tools`\n\n## {changelog.read_through}\n"
                table[changelog.url] = Fetched(200, changelog.url, text.encode("utf-8"))
            else:
                url = f"{changelog.url}?per_page=100&page=1"
                page = [release("rust-v99.0.0", "- Nothing watched"), release(f"v{changelog.read_through}", "")]
                table[url] = Fetched(200, url, json.dumps(page).encode("utf-8"))
        moved, gone = record.pages[0].url, record.pages[1].url
        table[moved] = Fetched(200, "https://example.com/moved")
        table[gone] = Fetched(404, gone, reason="HTTP 404 Not Found")
        return table

    def run_main(self, table: dict[str, Fetched], *arguments: str) -> tuple[int, list[str]]:
        with pytest.MonkeyPatch.context() as patch:
            printed: list[str] = []
            patch.setattr("builtins.print", lambda line: printed.append(line))
            code = watch.main(list(arguments), fetcher=StubFetcher(table), top=REPOSITORY)
        return code, printed

    def test_findings_are_listed_and_written_to_the_summary_and_never_fail_the_run(self) -> None:
        record = watch.read_record(REPOSITORY)
        summary = Path(self.enterContext(tempfile.TemporaryDirectory())) / "summary.md"
        code, lines = self.run_main(self.table(), "--summary", str(summary))
        self.assertEqual(0, code, lines)
        self.assertEqual([f"TERMS {kind}" for kind in watch.KINDS], [line.rsplit(" ", 1)[0] for line in lines[:4]])
        codex = next(changelog for changelog in record.changelogs if changelog.subject == "codex")
        self.assertIn(f"CHANGELOG codex {codex.read_through} 99.0.0 0", lines)
        self.assertIn(
            'ENTRY claude-code 99.0.0 "--allowedTools allowed-tools" "Changed --allowedTools and `allowed-tools`"',
            lines,
        )
        self.assertIn(f"MOVED {record.pages[0].url} https://example.com/moved", lines)
        self.assertIn(f"GONE {record.pages[1].url} 404", lines)
        text = summary.read_text(encoding="utf-8")
        self.assertIn("| 99.0.0 | --allowedTools allowed-tools | Changed --allowedTools and `allowed-tools` |", text)
        self.assertIn(f"- MOVED {record.pages[0].url} https://example.com/moved", text)
        self.assertIn("No entry mentions a watched term.", text)
        # Every runtime's latest release is 99.0.0 here, so every captured sample is behind it.
        samples = json.loads((REPOSITORY / PROVENANCE).read_text(encoding="utf-8"))["samples"]
        recorded = samples["changelogs/copilot-cli-changelog.md"]["versions"]["copilot-cli"]
        self.assertIn(f"SAMPLE_BEHIND copilot-cli changelogs/copilot-cli-changelog.md {recorded} 99.0.0", lines)
        self.assertIn(f"| copilot-cli | changelogs/copilot-cli-changelog.md | {recorded} | 99.0.0 |", text)
        behind = {line.split(" ")[2] for line in lines if line.startswith("SAMPLE_BEHIND ")}
        self.assertEqual(set(samples), behind)

    def test_a_sample_at_its_runtimes_latest_release_is_not_behind(self) -> None:
        table = self.table()
        record = watch.read_record(REPOSITORY)
        for changelog in record.changelogs:
            if changelog.format == "markdown":
                table[changelog.url] = Fetched(200, changelog.url, f"## {changelog.read_through}\n".encode())
            else:
                url = f"{changelog.url}?per_page=100&page=1"
                page = [release(f"v{changelog.read_through}", "")]
                table[url] = Fetched(200, url, json.dumps(page).encode("utf-8"))
        samples = json.loads((REPOSITORY / PROVENANCE).read_text(encoding="utf-8"))["samples"]
        latest = {changelog.subject: changelog.read_through for changelog in record.changelogs}
        expected = {
            path
            for path, sample in samples.items()
            if any(
                tuple(map(int, version.split("."))) < tuple(map(int, latest[runtime].split(".")))
                for runtime, version in sample["versions"].items()
            )
        }
        code, lines = self.run_main(table)
        self.assertEqual(0, code, lines)
        self.assertEqual(expected, {line.split(" ")[2] for line in lines if line.startswith("SAMPLE_BEHIND ")})

    def test_a_flag_the_code_passes_only_before_a_release_it_reads_is_watched(self) -> None:
        # Copilot CLI 1.0.103 retired --no-remote, which the code still passes to the releases before it.
        top = Path(self.enterContext(tempfile.TemporaryDirectory()))
        record = json.loads((REPOSITORY / GUIDANCE_PATH).read_text(encoding="utf-8"))
        copilot = next(changelog for changelog in record["changelogs"] if changelog["subject"] == "copilot-cli")
        copilot["read_through"] = "1.0.100"
        (top / GUIDANCE_PATH).parent.mkdir(parents=True)
        (top / GUIDANCE_PATH).write_text(json.dumps(record), encoding="utf-8")
        (top / PROVENANCE).parent.mkdir(parents=True)
        (top / PROVENANCE).write_bytes((REPOSITORY / PROVENANCE).read_bytes())
        table = {**self.table(), copilot["url"]: Fetched(200, copilot["url"], COPILOT_CHANGELOG.encode("utf-8"))}
        with pytest.MonkeyPatch.context() as patch:
            printed: list[str] = []
            patch.setattr("builtins.print", lambda line: printed.append(line))
            code = watch.main([], fetcher=StubFetcher(table), top=top)
        self.assertEqual(0, code, printed)
        retired = ENTRIES["flag-after-a-slash"]["entry"]
        self.assertIn(f'ENTRY copilot-cli 1.0.103 "--no-remote" "{retired}"', printed)

    def test_every_request_is_given_the_timeout_the_run_was_given(self) -> None:
        fetcher = StubFetcher(self.table())
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("builtins.print", lambda line: None)
            self.assertEqual(0, watch.main(["--timeout", "7.5"], fetcher=fetcher, top=REPOSITORY))
        record = watch.read_record(REPOSITORY)
        self.assertTrue({changelog.url for changelog in record.changelogs} & set(fetcher.timeouts))
        self.assertTrue({page.url for page in record.pages} <= set(fetcher.timeouts))
        self.assertEqual({7.5}, set(fetcher.timeouts.values()))

    def test_a_watch_that_cannot_run_prints_one_failed_line_and_exits_1(self) -> None:
        record = watch.read_record(REPOSITORY)
        url = record.changelogs[0].url
        code, lines = self.run_main({**self.table(), url: Fetched(500, url, reason="HTTP 500 Server Error")})
        self.assertEqual(1, code)
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0].startswith("FAILED cannot fetch the "), lines)
        self.assertIn("HTTP 500 Server Error", lines[0])

    def test_a_record_naming_no_changelog_for_a_supported_runtime_cannot_run(self) -> None:
        top = Path(self.enterContext(tempfile.TemporaryDirectory()))
        record = json.loads((REPOSITORY / GUIDANCE_PATH).read_text(encoding="utf-8"))
        record["changelogs"] = [changelog for changelog in record["changelogs"] if changelog["subject"] != "codex"]
        (top / GUIDANCE_PATH).parent.mkdir(parents=True)
        (top / GUIDANCE_PATH).write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(watch.WatchError) as caught:
            watch.read_record(top)
        self.assertIn("names no changelog for codex", str(caught.exception))
        (top / GUIDANCE_PATH).write_text("{", encoding="utf-8")
        with self.assertRaises(watch.WatchError) as caught:
            watch.read_record(top)
        self.assertIn("cannot read the guidance record", str(caught.exception))
