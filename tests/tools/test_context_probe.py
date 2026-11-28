"""Regression suite for tools/context_probe.py: a fixture repository, review archive, and flag store that plant a
distinctive marker in every field the probe reads, none of which may reach its output."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))

import atomic_file
import file_presence
import json_reader
import json_writer
from git_client import GitError
from review_archive import commit_record
from review_config import write_config
from review_flags import add_flag
from review_records import ReviewRecord, build_record, validate_adapter_result

from tools import context_probe

CHECKOUT = Path(__file__).resolve().parents[2]
SCRIPT = CHECKOUT / "tools" / "context_probe.py"
# Every marker starts with this, so one search finds any of them in an output; each is letters only, so a whole-word
# scan would see it as a word of its own.
MARKER = "qzv"
REPOSITORY = "qzvowner/qzvrepo"
COMMIT = "0123456789abcdef0123456789abcdef01234567"

# Each file the fixture repository holds, by its path, with content that names a marker of its own. The names and the
# folders are markers too, so a name or a path echoed into the profile is caught as surely as content is.
FILES: dict[str, str] = {
    "CLAUDE.md": "# qzvclaudeheading\n\nqzvclaudetext\n\n```bash\nqzvclaudefence\n```\n",
    "qzvpackage/CLAUDE.md": "# qzvnestedclaude\n",
    "AGENTS.md": "# qzvagentsheading\nqzvagentstext\n",
    ".github/copilot-instructions.md": "qzvcopilottext\n",
    ".github/instructions/qzvpathrule.instructions.md": "---\napplyTo: qzvglob/**\n---\nqzvinstructiontext\n",
    ".github/agents/qzvgithubagent.agent.md": "---\nname: qzvgithubagent\n---\nqzvgithubagenttext\n",
    ".github/skills/qzvgithubskill/SKILL.md": "---\nname: qzvgithubskill\ndescription: qzvdescription\n---\n",
    ".github/workflows/qzvworkflow.yml": "name: qzvworkflowname\n",
    ".claude/rules/qzvrule.md": "qzvruletext\n",
    ".claude/commands/qzvcommand.md": "qzvcommandtext\n",
    ".claude/agents/qzvagent.md": "---\nname: qzvagent\n---\nqzvagenttext\n",
    ".claude/skills/qzvskill/SKILL.md": "---\nname: qzvskill\ndescription: qzvskilltext\n---\n# qzvskillheading\n",
    ".agents/skills/qzvshim/SKILL.md": "---\nname: qzvshim\n---\n",
    ".mcp.json": json.dumps({"mcpServers": {"qzvserver": {"command": "qzvservercommand"}}}),
    ".claude/settings.json": json.dumps(
        {
            "qzvsetting": "qzvsettingvalue",
            "hooks": {
                "PreToolUse": [
                    {"matcher": "qzvmatcher", "hooks": [{"type": "command", "command": "qzvhookcommand"}]},
                ],
                "QzvUnknownEvent": [{"hooks": [{"type": "command", "command": "qzvothercommand"}]}],
            },
        }
    ),
    "package.json": json.dumps({"name": "qzvpackagename", "scripts": {"qzvscript": "qzvrun"}}),
    "qzvproject/qzvproject.csproj": "<Project><PropertyGroup><qzvproperty/></PropertyGroup></Project>\n",
    "qzvsource/qzvmodule.py": "QZVCONSTANT = 'qzvliteral'\n",
    "qzvsource/qzvdata.qzvextension": "qzvdatacontent\n",
    "qzvbarename": "qzvbarecontent\n",
}


def git(directory: Path, *arguments: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(directory), *arguments], capture_output=True, text=True, encoding="utf-8", check=False
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} failed: {result.stderr}")


def repository(root: Path) -> Path:
    """A checkout on a marker branch, with a marker remote, author, and commit message, the FILES, a folder link and a
    file link, and a nested checkout, below a folder whose own name is a marker."""
    top = root / "qzvworkspace" / "qzvcheckout"
    for relative, content in FILES.items():
        path = top / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (top / "qzvnested" / ".git").mkdir(parents=True)
    (top / "qzvnested" / "qzvinside.py").write_text("qzvinsidecontent\n", encoding="utf-8")
    outside = root / "qzvoutside"
    outside.mkdir()
    (outside / "qzvsecret.md").write_text("qzvsecretcontent\n", encoding="utf-8")
    (top / "qzvfolderlink").symlink_to(outside, target_is_directory=True)
    (top / ".claude" / "rules" / "qzvfilelink.md").symlink_to(outside / "qzvsecret.md")
    git(top, "init", "--quiet", "--initial-branch", "qzvbranch")
    git(top, "remote", "add", "origin", f"https://github.com/{REPOSITORY}.git")
    git(top, "add", "--all")
    git(
        top,
        "-c",
        "user.name=Qzvauthor",
        "-c",
        "user.email=qzvauthor@qzvdomain.example",
        "commit",
        "--quiet",
        "--message",
        "qzvcommitmessage",
    )
    return top


def review_record(number: int) -> ReviewRecord:
    """A record of the marker repository whose every free-text field is a marker."""
    head = f"{number:x}" * 40
    request: dict[str, Any] = {
        "repository": REPOSITORY,
        "pull_number": number,
        "pull_url": f"https://github.com/{REPOSITORY}/pull/{number}",
        "title": "qzvpulltitle",
        "base_ref": "qzvbaseref",
        "head_ref": "qzvheadref",
        "base_sha": "a" * 40,
        "head_sha": head,
        "mode": "initial",
        "dispatch": "subagents",
        "adapter": {"name": "qzvadapter", "scope": "generic", "source_commit": None, "source_hashes": {}},
        "reviewers": [
            {
                "id": "qzvreviewer",
                "category": "QzvReviewerCategory",
                "files": 3,
                "findings": 1,
                "retries": 1,
                "dispositions_only": False,
                "model": "qzvmodel",
                "summary": "qzvreviewersummary",
            }
        ],
        "unavailable_sources": ["qzvunavailable/qzvsource.cs"],
        "unreviewed_files": {"qzvbinary/qzvasset.qzvsuffix": ".qzvsuffix"},
    }
    finding = {
        "candidate_key": "qzvkey",
        "severity": "MUST_FIX",
        "category": "QzvFindingCategory",
        "path": "qzvsource/qzvmodule.py",
        "line": 1,
        "title": "qzvfindingtitle",
        "body": "qzvfindingbody",
        "evidence": "qzvfindingevidence",
        "source": "qzvfindingsource",
        "analyzer": {"coverage": "custom-candidate", "tool": "qzvtool", "rule": "qzvanalyzerrule"},
    }
    result = validate_adapter_result(
        {
            "protocol_version": 1,
            "repository": REPOSITORY,
            "pull_number": number,
            "head_sha": head,
            "summary": "qzvreviewsummary",
            "reviewer": "qzvresultreviewer",
            "status": "complete",
            "findings": [finding],
            "prior_dispositions": [],
            "usage": None,
        },
        expected_repository=REPOSITORY,
        expected_number=number,
        expected_head_sha=head,
    )
    return build_record(
        request,
        result,
        version=1,
        policy={"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
        reviewed_at="2026-01-15T12:00:00+00:00",
    )


def review_home(root: Path, checkout: Path) -> tuple[Path, Path]:
    """A code-review configuration naming the marker repository's checkout, an archive under a marker folder with a
    record of it, and a flag store with a flag on it; their paths."""
    home = root / "qzvhome"
    archive = home / "qzvarchive"
    config_path = home / "qzvconfig.json"
    flags = home / "qzvflags.json"
    entry = {
        "reviewer": {
            "id": "generic",
            "protocol_version": 1,
            "trusted_ref": None,
            "scope": "generic",
            "manifest_path": None,
        },
        "checkout_path": str(checkout),
    }
    write_config(
        {
            "schema_version": 1,
            "default_repository_set": "qzvset",
            "repository_sets": {"qzvset": [REPOSITORY]},
            "repositories": {REPOSITORY: entry},
            "archive_root": str(archive),
            "local_mirror_root": None,
            "summary_root": str(home / "qzvsummary"),
            "dashboard_file": str(home / "qzvdashboard.md"),
            "github_login": "qzvlogin",
        },
        config_path,
    )
    commit_record(archive, REPOSITORY, 7, review_record(7), expected_latest_version=None)
    add_flag(
        flags,
        category="QzvFlagCategory",
        body="qzvflagbody",
        repository=REPOSITORY,
        pull_number=7,
        review_version=1,
        finding_id="F001",
    )
    return config_path, flags


@pytest.fixture
def planted(tmp_path: Path) -> dict[str, Path]:
    checkout = repository(tmp_path)
    config_path, flags = review_home(tmp_path, checkout)
    terms = tmp_path / "qzvterms" / "qzvterms.txt"
    terms.parent.mkdir()
    # A term the profile does not hold, so the scan runs and passes.
    terms.write_text("qzvterm\n", encoding="utf-8")
    return {"checkout": checkout, "config": config_path, "flags": flags, "terms": terms, "root": tmp_path}


def services(places: dict[str, Path], commit: str | None = COMMIT) -> context_probe.ProbeServices:
    return context_probe.ProbeServices(
        commit=lambda: commit, config_path=lambda: places["config"], flags_path=lambda: places["flags"]
    )


def run(arguments: list[str], probe: context_probe.ProbeServices) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = context_probe.main(arguments, probe)
        except SystemExit as error:
            code = int(error.code or 0)
    return code, stdout.getvalue(), stderr.getvalue()


def preview(places: dict[str, Path], probe: context_probe.ProbeServices | None = None) -> tuple[int, str, str]:
    arguments = ["preview", str(places["checkout"]), "--terms", str(places["terms"])]
    return run(arguments, probe or services(places))


def printed_profile(stdout: str) -> dict[str, Any]:
    body, _, last = stdout.rstrip("\n").rpartition("\n")
    assert last.startswith("DIGEST ")
    profile: dict[str, Any] = json.loads(body)
    return profile


def test_no_planted_marker_reaches_the_preview_and_every_field_was_read(planted: dict[str, Path]) -> None:
    code, stdout, stderr = preview(planted)

    assert code == 0, stdout
    assert MARKER not in stdout.casefold()
    assert MARKER not in stderr.casefold()
    profile = printed_profile(stdout)
    # The fields the markers sit in were read, so their absence is the probe's doing, not an unread field.
    assert set(profile["rule_sources"]) == set(context_probe.RULE_SOURCES)
    assert profile["rule_sources"]["CLAUDE.md"]["files"] == 2
    assert profile["rule_sources"]["claude-rules"]["unread"] == 1
    assert profile["hooks"]["hook_events"] == {"PreToolUse": 1, "other": 1}
    assert profile["languages"]["other"] >= 1
    assert profile["build_systems"] == {"*.csproj": 1, "github-workflows": 1, "package.json": 1}
    assert profile["walk"] == {"linked": 1, "nested-checkout": 1}
    reviews = profile["reviews"]
    assert reviews["totals"]["records"] == 1
    assert reviews["totals"]["finding_categories"] == 1
    assert reviews["totals"]["unavailable_sources"] == 1
    assert reviews["totals"]["unreviewed_files"] == 1
    assert reviews["unreviewed_languages"] == {"other": 1}
    assert reviews["severities"] == {"MUST_FIX": 1}
    assert reviews["flags"] == {"open": 1}
    assert profile["probe"] == {"schema": context_probe.SCHEMA, "commit": COMMIT}


def test_the_written_profile_is_the_previewed_one_and_holds_no_marker(planted: dict[str, Path]) -> None:
    _, stdout, _ = preview(planted)
    found = stdout.rstrip("\n").rpartition("DIGEST ")[2]
    out = planted["root"] / "profile.json"

    code, written, stderr = run(
        ["write", str(planted["checkout"]), "--digest", found, "--out", str(out), "--terms", str(planted["terms"])],
        services(planted),
    )

    assert code == 0
    assert written == f"WROTE {out}\n"
    assert stderr == ""
    text = out.read_text(encoding="utf-8")
    assert MARKER not in text.casefold()
    assert json.loads(text) == printed_profile(stdout)


def test_the_script_run_end_to_end_prints_no_marker_on_either_stream(planted: dict[str, Path]) -> None:
    environment = {
        **os.environ,
        "CODE_REVIEW_CONFIG": str(planted["config"]),
        "CODE_REVIEW_FLAGS": str(planted["flags"]),
    }
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "preview", str(planted["checkout"]), "--terms", str(planted["terms"])],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert MARKER not in result.stdout.casefold()
    assert MARKER not in result.stderr.casefold()
    profile = printed_profile(result.stdout)
    assert profile["reviews"]["totals"]["records"] == 1
    assert context_probe.COMMIT.fullmatch(profile["probe"]["commit"]) or profile["probe"]["commit"] == "unknown"


def test_a_private_term_in_a_key_stops_the_run_naming_only_the_field(planted: dict[str, Path]) -> None:
    planted["terms"].write_text("qzvother\nPYTHON\n", encoding="utf-8")
    out = planted["root"] / "profile.json"

    code, stdout, stderr = preview(planted)
    write_code, written, _ = run(
        ["write", str(planted["checkout"]), "--digest", "0" * 64, "--out", str(out), "--terms", str(planted["terms"])],
        services(planted),
    )

    assert code == 1
    assert stdout == "PRIVATE_TERM profile.languages key\nSTOPPED private term\n"
    assert stderr == ""
    assert write_code == 1
    assert written == stdout
    assert not out.exists()


def test_a_private_term_in_a_value_stops_the_run(planted: dict[str, Path]) -> None:
    planted["terms"].write_text("no_configuration\n", encoding="utf-8")
    probe = context_probe.ProbeServices(
        commit=lambda: COMMIT,
        config_path=lambda: planted["root"] / "absent.json",
        flags_path=lambda: planted["flags"],
    )

    code, stdout, _ = preview(planted, probe)

    assert code == 1
    assert stdout == "PRIVATE_TERM profile.reviews_absent value\nSTOPPED private term\n"


def test_a_term_inside_a_longer_word_does_not_match() -> None:
    assert context_probe.private_hits({"languages": {"Python": 1}}, ["pyth", "thon"]) == []
    assert context_probe.private_hits({"languages": {"Python": 1}}, ["python"]) == ["profile.languages key"]
    assert context_probe.private_hits({"walk": {"nested-checkout": 1}}, ["nested"]) == ["profile.walk key"]


def test_a_missing_term_list_stops_the_run_before_any_profile(planted: dict[str, Path]) -> None:
    planted["terms"].unlink()

    code, stdout, _ = preview(planted)

    assert code == 1
    assert stdout.startswith("FAILED no private-term list at ")
    assert "{" not in stdout


def test_a_term_list_that_is_a_folder_stops_the_run_as_not_a_file(planted: dict[str, Path]) -> None:
    planted["terms"].unlink()
    planted["terms"].mkdir()

    code, stdout, _ = preview(planted)

    assert (code, stdout) == (1, f"FAILED the private-term list at {planted['terms']} is not a file\n")


@contextlib.contextmanager
def released(fifo: Path) -> Iterator[list[Path]]:
    """While the block runs, open and close the FIFO's writing end every RELEASE_INTERVAL seconds, so a reader that
    opens it meets its end of file at once rather than blocking: code that opens it fails the test's assertions, never
    hangs it. A nonblocking open of the writing end succeeds only while a reader holds the FIFO open, so the list it
    gives names the FIFO once the block's code opened it, and stays empty while it never did."""
    done = threading.Event()
    opened: list[Path] = []

    def release() -> None:
        while not done.wait(RELEASE_INTERVAL):
            with contextlib.suppress(OSError):
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))  # type: ignore[attr-defined, unused-ignore]  # POSIX only
                opened.append(fifo)

    releaser = threading.Thread(target=release, daemon=True)
    releaser.start()
    try:
        yield opened
    finally:
        done.set()
        releaser.join()


# How often `released` opens the FIFO's writing end, which ends a read blocked opening it.
RELEASE_INTERVAL = 0.2


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no FIFO a folder can hold")
def test_a_term_list_that_is_a_fifo_stops_the_run_before_anything_opens_it(planted: dict[str, Path]) -> None:
    # read_text() on a FIFO with no writer blocked forever; the list's kind is judged before anything opens it.
    planted["terms"].unlink()
    os.mkfifo(planted["terms"])  # type: ignore[attr-defined, unused-ignore]  # POSIX only: Windows has no os.mkfifo

    with released(planted["terms"]) as opened:
        code, stdout, _ = preview(planted)

    assert (code, stdout) == (1, f"FAILED the private-term list at {planted['terms']} is not a file\n")
    assert opened == []


def test_a_profile_that_changed_since_its_preview_is_not_written(planted: dict[str, Path]) -> None:
    out = planted["root"] / "profile.json"

    code, stdout, _ = run(
        ["write", str(planted["checkout"]), "--digest", "f" * 64, "--out", str(out), "--terms", str(planted["terms"])],
        services(planted),
    )

    assert code == 1
    assert stdout.startswith("CHANGED ")
    assert not out.exists()


def test_the_allowlist_refuses_any_string_it_does_not_name() -> None:
    allowed = {"probe": {"schema": 1, "commit": COMMIT}, "languages": {"Python": 4}}
    context_probe.allowlisted(allowed)
    for profile, place in (
        ({"languages": {"qzvlanguage": 1}}, "profile.languages key"),
        ({"reviews_absent": "qzvreason"}, "profile.reviews_absent"),
        ({"probe": {"commit": "qzvcommit"}}, "profile.probe.commit"),
        ({"files": True}, "profile.files"),
        ({"files": 1.5}, "profile.files"),
        ({"walk": ["linked"]}, "profile.walk"),
    ):
        with pytest.raises(context_probe.ProbeError) as raised:
            context_probe.allowlisted(profile)
        assert str(raised.value).endswith(f"at {place}")
        assert MARKER not in str(raised.value)


def test_the_profile_records_the_probes_commit_or_unknown_and_a_failing_git_stops_it(
    planted: dict[str, Path],
) -> None:
    def failing() -> str | None:
        raise GitError(f"fatal: detected dubious ownership in repository at '{planted['checkout']}/qzvvendor'")

    assert printed_profile(preview(planted, services(planted, None))[1])["probe"]["commit"] == "unknown"
    probe = context_probe.ProbeServices(
        commit=failing, config_path=lambda: planted["config"], flags_path=lambda: planted["flags"]
    )
    code, stdout, stderr = preview(planted, probe)
    assert (code, stdout, stderr) == (1, "FAILED git could not read the probe's own commit: GitError git\n", "")


def test_a_checkout_no_configured_repository_holds_has_no_review_counts(planted: dict[str, Path]) -> None:
    other = planted["root"] / "elsewhere"
    other.mkdir()
    (other / "main.go").write_text("package main\n", encoding="utf-8")

    code, stdout, _ = run(["preview", str(other), "--terms", str(planted["terms"])], services(planted))

    assert code == 0
    profile = printed_profile(stdout)
    assert profile["reviews_absent"] == "not_configured"
    assert profile["languages"] == {"Go": 1}
    assert "reviews" not in profile


def test_counts_are_rounded_down_to_a_power_of_two() -> None:
    assert [context_probe.bucket(count) for count in (0, 1, 2, 3, 4, 7, 8, 1000)] == [0, 1, 2, 2, 4, 4, 8, 512]


def test_a_repository_that_is_no_folder_is_a_usage_error(tmp_path: Path) -> None:
    code, _, stderr = run(["preview", str(tmp_path / "absent")], context_probe.ProbeServices())

    assert code == 2
    assert "the repository must be a folder" in stderr


# Each reader the probe calls on the examined repository or the review records, by the attribute of context_probe it
# is reached through, and the part a FAILED line names when it raises something no handler expects.
READERS = (
    ("folder_walk.walk_files", "repository's files"),
    ("document_headings", "repository's files"),
    ("json_reader.read_file", "repository's hook settings"),
    ("current_repository", "review records"),
    ("record_files", "review records"),
    ("validate_record_pair", "review records"),
    ("load_store", "review records"),
)


@pytest.mark.parametrize(("reader", "part"), READERS)
def test_a_reader_failing_with_a_private_path_in_its_text_names_only_the_part(
    planted: dict[str, Path], monkeypatch: pytest.MonkeyPatch, reader: str, part: str
) -> None:
    def failing(*_arguments: object, **_options: object) -> object:
        raise RuntimeError(f"cannot read {planted['checkout']}/qzvprivate/qzvfile.md at refs/heads/qzvbranch")

    owner, _, name = reader.rpartition(".")
    monkeypatch.setattr(getattr(context_probe, owner) if owner else context_probe, name, failing)

    code, stdout, stderr = preview(planted)

    assert code == 1
    assert stdout == f"FAILED the {part} could not be read: RuntimeError\n"
    assert MARKER not in (stdout + stderr).casefold()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows gives a file no permission that refuses its owner a read")
def test_a_rule_source_it_cannot_read_under_a_private_name_counts_as_unread(planted: dict[str, Path]) -> None:
    unreadable = planted["checkout"] / ".claude" / "rules" / "qzvunreadable.md"
    unreadable.write_text("qzvunreadablecontent\n", encoding="utf-8")
    unreadable.chmod(0)
    try:
        code, stdout, stderr = preview(planted)
    finally:
        unreadable.chmod(0o600)

    assert code == 0
    assert MARKER not in (stdout + stderr).casefold()
    assert printed_profile(stdout)["rule_sources"]["claude-rules"]["unread"] == 2


def test_a_checkout_whose_git_points_at_a_private_path_names_no_path(planted: dict[str, Path]) -> None:
    broken = planted["root"] / "qzvbroken"
    broken.mkdir()
    (broken / ".git").write_text(f"gitdir: {planted['root']}/qzvmissinggitdir\n", encoding="utf-8")
    (broken / "CLAUDE.md").write_text("qzvbrokenrules\n", encoding="utf-8")
    config_path, _ = review_home(planted["root"] / "qzvbrokenhome", broken)

    code, stdout, stderr = run(
        ["preview", str(broken), "--terms", str(planted["terms"])],
        context_probe.ProbeServices(
            commit=lambda: COMMIT, config_path=lambda: config_path, flags_path=lambda: planted["flags"]
        ),
    )

    assert code == 0, stdout
    assert MARKER not in (stdout + stderr).casefold()
    profile = printed_profile(stdout)
    # git refuses a .git file naming no folder, so the review counts are absent as unreadable, never matched by path.
    assert profile["reviews_absent"] == "checkout_unreadable"
    assert "reviews" not in profile


def test_a_repository_argument_it_cannot_examine_names_no_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(path: Path, **_options: object) -> bool:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(file_presence, "is_directory", failing)

    code, stdout, stderr = run(["preview", "/qzvwork/qzvprivate"], context_probe.ProbeServices())

    assert code == 2
    assert "the repository cannot be examined: PermissionError" in stderr
    assert MARKER not in (stdout + stderr).casefold()


def test_a_failure_of_a_kind_no_reader_raises_prints_only_its_kind_end_to_end(planted: dict[str, Path]) -> None:
    # The script runs as its own __main__ in a child, with the walk replaced by one raising an AssertionError, a kind
    # the readers' guard does not catch, whose text holds a marker path; only run_main can end it.
    program = "\n".join(
        [
            "import runpy, sys",
            f"sys.path.insert(0, {str(CHECKOUT / 'skills' / 'ejryan89-skill-core' / 'scripts')!r})",
            "import folder_walk",
            "def failing(*arguments, **options):",
            # Every path goes into the source as a literal repr writes, so a Windows backslash is no escape.
            f"    raise AssertionError({f'walked into {planted["checkout"]}/qzvprivate/qzvfile.md'!r})",
            "folder_walk.walk_files = failing",
            f"sys.argv = [{str(SCRIPT)!r}, 'preview', {str(planted['checkout'])!r},",
            f"    '--terms', {str(planted['terms'])!r}]",
            f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')",
        ]
    )
    environment = {
        **os.environ,
        "CODE_REVIEW_CONFIG": str(planted["config"]),
        "CODE_REVIEW_FLAGS": str(planted["flags"]),
    }

    result = subprocess.run(
        [sys.executable, "-B", "-c", program],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=120,
        check=False,
    )

    assert (result.returncode, result.stdout, result.stderr) == (1, "FAILED AssertionError\n", "")


def commit_repository(top: Path) -> str:
    """A repository at `top` with one commit; its commit."""
    top.mkdir(parents=True, exist_ok=True)
    (top / "qzvrepofile.txt").write_text("qzvrepocontent\n", encoding="utf-8")
    git(top, "init", "--quiet")
    git(top, "add", "--all")
    git(top, "-c", "user.name=Qzvauthor", "-c", "user.email=q@example.invalid", "commit", "--quiet", "-m", "qzvmsg")
    head = subprocess.run(
        ["git", "-C", str(top), "rev-parse", "HEAD"], capture_output=True, text=True, encoding="utf-8", check=True
    )
    return head.stdout.strip()


def test_a_copy_of_the_probe_inside_another_repository_records_no_commit(tmp_path: Path) -> None:
    outer = tmp_path / "qzvprivaterepo"
    commit_repository(outer)
    copy = outer / "vendor" / "cas"
    copy.mkdir(parents=True)

    assert context_probe.checkout_commit(copy, SCRIPT) is None
    # The top of a checkout of its own, but its commit holds no tools/context_probe.py.
    assert context_probe.checkout_commit(outer, SCRIPT) is None


@pytest.mark.parametrize(
    ("committed", "recorded"),
    [
        pytest.param(lambda text: text, True, id="the running file"),
        pytest.param(lambda text: text.replace(b"\n", b"\r\n"), True, id="the running file with CRLF endings"),
        pytest.param(lambda text: text + b"# qzvedited\n", False, id="another file"),
    ],
)
def test_the_commit_is_recorded_only_when_it_holds_the_running_probe(
    tmp_path: Path, committed: Callable[[bytes], bytes], recorded: bool
) -> None:
    top = tmp_path / "qzvprivaterepo"
    (top / "tools").mkdir(parents=True)
    (top / "tools" / "context_probe.py").write_bytes(committed(SCRIPT.read_bytes()))
    head = commit_repository(top)

    assert context_probe.checkout_commit(top, SCRIPT) == (head if recorded else None)


def test_a_probe_file_that_cannot_be_read_stops_the_run_naming_the_class_alone(planted: dict[str, Path]) -> None:
    def failing() -> str | None:
        raise PermissionError(13, "Permission denied", "/qzvprivate/tools/context_probe.py")

    probe = context_probe.ProbeServices(
        commit=failing, config_path=lambda: planted["config"], flags_path=lambda: planted["flags"]
    )

    assert preview(planted, probe) == (1, "FAILED the probe's own file could not be read: PermissionError\n", "")


def run_copy(planted: dict[str, Path], copy: Path) -> subprocess.CompletedProcess[str]:
    """The probe run from a copy of its tree at `copy`, with no .git of its own, on the planted checkout."""
    (copy / "tools").mkdir(parents=True)
    shutil.copy2(SCRIPT, copy / "tools" / "context_probe.py")
    for skill in ("ejryan89-skill-core", "ejryan89-code-review-core", "review-insights", "audit-ai-config"):
        shutil.copytree(
            CHECKOUT / "skills" / skill / "scripts",
            copy / "skills" / skill / "scripts",
            ignore=shutil.ignore_patterns("test_*", "__pycache__"),
        )
    environment = {
        **os.environ,
        "CODE_REVIEW_CONFIG": str(planted["config"]),
        "CODE_REVIEW_FLAGS": str(planted["flags"]),
    }
    return subprocess.run(
        [
            sys.executable,
            "-B",
            str(copy / "tools" / "context_probe.py"),
            "preview",
            str(planted["checkout"]),
            "--terms",
            str(planted["terms"]),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("place", ["vendor/cas", ""], ids=["in a folder of it", "at its top"])
def test_a_copy_without_git_in_a_repository_prints_unknown_end_to_end(planted: dict[str, Path], place: str) -> None:
    # The tool tree, with no .git of its own, unpacked inside the examined repository, in a folder of it or at its top
    # as extracting `git archive` with no prefix there does: git would answer with that repository's commit, so the
    # profile must say unknown and hold no part of it.
    private_head = commit_repository(planted["checkout"])

    result = run_copy(planted, planted["checkout"] / place if place else planted["checkout"])

    assert result.returncode == 0, result.stdout + result.stderr
    assert printed_profile(result.stdout)["probe"]["commit"] == "unknown"
    assert private_head not in result.stdout + result.stderr
    assert MARKER not in (result.stdout + result.stderr).casefold()


# Each FAILED line the probe prints itself, by what raises: the error's text holds a marker path, and the line names
# only the place the probe gives and the error's class.
def test_a_term_list_that_cannot_be_read_names_its_path_and_the_class_alone(
    planted: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(self: Path, *_arguments: object, **_options: object) -> str:
        raise PermissionError(13, "Permission denied", "/qzvprivate/qzvlock")

    monkeypatch.setattr(Path, "read_text", failing)

    code, stdout, stderr = preview(planted)

    assert code == 1
    assert stdout == f"FAILED the private-term list at {planted['terms']} cannot be read: PermissionError\n"
    assert "qzvprivate" not in stdout + stderr


def test_a_term_list_whose_stat_fails_names_its_path_and_the_class_alone(
    planted: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(path: Path) -> bool:
        raise PermissionError(13, "Permission denied", "/qzvprivate/qzvparent")

    monkeypatch.setattr(file_presence, "is_present", failing)

    code, stdout, stderr = preview(planted)

    assert code == 1
    assert stdout == f"FAILED the private-term list at {planted['terms']} cannot be read: PermissionError\n"
    assert "qzvprivate" not in stdout + stderr


def test_a_profile_that_cannot_be_written_names_out_and_the_class_alone(
    planted: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(path: Path, content: str, **_options: object) -> None:
        raise OSError(28, "No space left on device", "/qzvprivate/qzvtemporary")

    _, shown_profile, _ = preview(planted)
    found = shown_profile.rstrip("\n").rpartition("DIGEST ")[2]
    out = planted["root"] / "profile.json"
    monkeypatch.setattr(atomic_file, "replace_text", failing)

    code, stdout, stderr = run(
        ["write", str(planted["checkout"]), "--digest", found, "--out", str(out), "--terms", str(planted["terms"])],
        services(planted),
    )

    assert code == 1
    assert stdout == f"FAILED the profile cannot be written to {out}: OSError\n"
    assert "qzvprivate" not in stdout + stderr


@pytest.mark.parametrize(
    "error",
    [
        OSError(5, "Input/output error", "/qzvprivate/qzvfile"),
        json_writer.JsonWriteRefused("a key twice: qzvprivatekey"),
    ],
    ids=["OSError", "JsonWriteRefused"],
)
def test_any_other_failure_main_catches_names_its_class_alone(
    planted: dict[str, Path], monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def failing(*_arguments: object, **_options: object) -> object:
        raise error

    monkeypatch.setattr(context_probe, "checked_profile", failing)

    code, stdout, stderr = preview(planted)

    assert (code, stdout) == (1, f"FAILED {type(error).__name__}\n")
    assert MARKER not in (stdout + stderr).casefold()


# hook_counts reads .claude/settings.json and counts it as unread, never as absent and never by its content, when it is
# a link, cannot be read, or holds no JSON.
def test_settings_that_are_a_link_are_unread(tmp_path: Path) -> None:
    outside = tmp_path / "qzvoutside.json"
    outside.write_text(json.dumps({"hooks": {"PreToolUse": []}}), encoding="utf-8")
    (tmp_path / "repository" / ".claude").mkdir(parents=True)
    (tmp_path / "repository" / ".claude" / "settings.json").symlink_to(outside)

    assert context_probe.hook_counts(tmp_path / "repository") == {"unread": 1}


# A linked .claude folder is unread wherever it leads, as the walk leaves it out of rule_sources wherever it leads, so
# the hook counts never describe a settings file the walk did not reach, outside the tree least of all.
@pytest.mark.parametrize("place", ["outside", "inside"])
def test_settings_in_a_linked_claude_folder_are_unread_and_not_a_rule_source(tmp_path: Path, place: str) -> None:
    repository = tmp_path / "repository"
    target = tmp_path / "qzvoutside" if place == "outside" else repository / "qzvinside"
    repository.mkdir()
    target.mkdir()
    (target / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": []}}), encoding="utf-8")
    (repository / ".claude").symlink_to(target, target_is_directory=True)

    assert context_probe.hook_counts(repository) == {"unread": 1}
    assert "claude-settings" not in context_probe.walk_shape(repository).rule_sources


def test_a_claude_folder_whose_metadata_cannot_be_read_is_unread_never_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / ".claude"
    folder.mkdir()
    (folder / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": []}}), encoding="utf-8")

    def failing_on_folder(real: Callable[..., os.stat_result]) -> Callable[..., os.stat_result]:
        def stat(path: os.PathLike[str] | str, *arguments: object, **options: object) -> os.stat_result:
            if Path(path) == folder:
                raise PermissionError(13, "Permission denied", str(path))
            return real(path, *arguments, **options)

        return stat

    monkeypatch.setattr(os, "lstat", failing_on_folder(os.lstat))
    monkeypatch.setattr(os, "stat", failing_on_folder(os.stat))

    assert context_probe.hook_counts(tmp_path) == {"unread": 1}


def test_a_claude_entry_that_is_a_file_holds_no_hook_counts(tmp_path: Path) -> None:
    (tmp_path / ".claude").write_text("qzvnotafolder", encoding="utf-8")

    assert context_probe.hook_counts(tmp_path) == {}


def test_settings_that_cannot_be_read_are_unread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(path: Path, **_options: object) -> object:
        raise PermissionError(13, "Permission denied", str(path))

    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(json_reader, "read_file", failing)

    assert context_probe.hook_counts(tmp_path) == {"unread": 1}


@pytest.mark.parametrize(
    "text",
    [
        "{qzvnotjson",
        "\ufeff{}",
        '{"a": 1, "a": 2}',
        "[]",
        '"qzvtext"',
        "null",
        '{"hooks": ["qzvevent"]}',
        '{"hooks": null}',
    ],
    ids=["malformed", "bom", "twice", "a list", "a string", "null", "hooks no object", "hooks null"],
)
def test_settings_that_hold_no_json_are_unread(tmp_path: Path, text: str) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(text, encoding="utf-8")

    assert context_probe.hook_counts(tmp_path) == {"unread": 1}


def test_absent_settings_or_settings_without_hooks_have_no_hook_counts(tmp_path: Path) -> None:
    assert context_probe.hook_counts(tmp_path) == {}
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text('{"model": "qzvmodel"}', encoding="utf-8")
    assert context_probe.hook_counts(tmp_path) == {}


def test_reviewer_runs_snapshots_diff_fallbacks_and_scopes_are_counted() -> None:
    review: dict[str, Any] = {
        "verdict": "APPROVED",
        "mode": "re-review",
        "dispatch": "subagents",
        "reviewers": [
            {"retries": 2, "dispositions_only": True},
            {"retries": 1, "dispositions_only": False},
        ],
        "coverage": {
            "unavailable_sources": ["a"],
            "uncovered_files": ["b", "c"],
            "unreviewed_files": {"d.png": ".png"},
        },
        "snapshot": {
            "source": "checkout",
            "excluded": {"binary": 3, "generated": 2},
            "diff": {"source": "files-api", "limit": "300 files"},
        },
        "scope": {"requested": "auto", "used": "incremental"},
    }
    record = cast(ReviewRecord, {"review": review, "findings": [], "prior_dispositions": []})
    tally = context_probe.ReviewTally()

    tally.add(record)
    tally.add(cast(ReviewRecord, {**record, "review": {**review, "snapshot": {"source": "tarball"}, "scope": None}}))
    section = tally.profile(pulls=1)

    assert section["totals"] == {
        "records": 2,
        "pulls": 1,
        "reviewers": 4,
        "reviewer_retries": 4,
        "dispositions_only": 2,
        "unavailable_sources": 2,
        "uncovered_files": 4,
        "unreviewed_files": 2,
        "excluded_files": 4,
    }
    assert section["snapshot_sources"] == {"checkout": 1, "tarball": 1}
    assert section["diff_fallbacks"] == {"files-api": 1}
    assert section["scope_requested"] == {"auto": 1}
    assert section["scope_used"] == {"incremental": 1}
    assert section["unreviewed_languages"] == {"Image": 2}


def test_a_configuration_that_cannot_be_read_has_no_review_counts(planted: dict[str, Path]) -> None:
    planted["config"].write_text('{"qzvsetting": ', encoding="utf-8")

    profile = printed_profile(preview(planted)[1])

    assert profile["reviews_absent"] == "configuration_unreadable"
    assert "reviews" not in profile


def test_a_record_that_cannot_be_read_is_counted_as_unreadable(planted: dict[str, Path]) -> None:
    pull = next((planted["root"] / "qzvhome" / "qzvarchive").glob("*/*/pulls/7"))
    (pull / "review-v9.json").write_text('{"qzvbroken": ', encoding="utf-8")

    code, stdout, stderr = preview(planted)

    assert code == 0, stdout
    assert MARKER not in (stdout + stderr).casefold()
    totals = printed_profile(stdout)["reviews"]["totals"]
    assert (totals["records"], totals["unreadable"]) == (1, 1)


def test_an_archive_that_cannot_be_listed_is_counted_as_unreadable(
    planted: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(root: Path, repository: str) -> list[Path]:
        raise PermissionError(13, "Permission denied", f"{root}/qzvowner/qzvrepo/pulls")

    monkeypatch.setattr(context_probe, "record_files", failing)

    code, stdout, stderr = preview(planted)

    assert code == 0, stdout
    assert MARKER not in (stdout + stderr).casefold()
    totals = printed_profile(stdout)["reviews"]["totals"]
    assert totals == {"unreadable": 1}


def test_a_flag_store_that_cannot_be_read_is_counted_as_unread(planted: dict[str, Path]) -> None:
    planted["flags"].write_text('{"qzvflags": ', encoding="utf-8")

    code, stdout, stderr = preview(planted)

    assert code == 0, stdout
    assert MARKER not in (stdout + stderr).casefold()
    assert printed_profile(stdout)["reviews"]["flags"] == {"unread": 1}


def test_an_empty_term_list_has_no_pattern_and_no_hits() -> None:
    assert context_probe._term_pattern([]) is None
    assert context_probe.private_hits({"languages": {"Python": 1}}, []) == []


def test_a_review_value_outside_its_closed_vocabulary_counts_as_other() -> None:
    tally = context_probe.ReviewTally()

    tally.count("verdicts", "APPROVED")
    tally.count("verdicts", "qzvverdict")
    tally.count("verdicts", None)
    tally.count("diff_fallbacks", "qzvsource")

    assert tally.vocabularies == {"verdicts": {"APPROVED": 1, "other": 2}, "diff_fallbacks": {"other": 1}}
    assert MARKER not in json.dumps(tally.profile(pulls=0)).casefold()


def test_flags_are_counted_for_the_probed_repository_alone(tmp_path: Path) -> None:
    flags = tmp_path / "qzvflags.json"
    for repository in (REPOSITORY, REPOSITORY.upper(), "qzvother/qzvrepo", None):
        add_flag(
            flags,
            category="QzvFlagCategory",
            body="qzvflagbody",
            repository=repository,
            pull_number=None,
            review_version=None,
            finding_id=None,
        )

    assert context_probe.flag_counts(flags, REPOSITORY) == {"open": 2}
