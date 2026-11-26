"""Regression suite for tools/context_probe.py: a fixture repository, review archive, and flag store that plant a
distinctive marker in every field the probe reads, none of which may reach its output."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))

import file_presence
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
        raise GitError("fatal: bad config line 1")

    assert printed_profile(preview(planted, services(planted, None))[1])["probe"]["commit"] == "unknown"
    probe = context_probe.ProbeServices(
        commit=failing, config_path=lambda: planted["config"], flags_path=lambda: planted["flags"]
    )
    assert preview(planted, probe)[:2] == (
        1,
        "FAILED git could not read this checkout's commit: fatal: bad config line 1\n",
    )


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
    assert profile.get("reviews_absent") == "checkout_unreadable" or profile["reviews"]["totals"]["records"] == 1


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
            f"    raise AssertionError('walked into {planted['checkout']}/qzvprivate/qzvfile.md')",
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
