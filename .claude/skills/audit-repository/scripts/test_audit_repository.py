from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "skill-core" / "scripts"))

import audit_repository as audit
from git_client import GitClient, GitError, GitResult

SCRIPT = Path(__file__).resolve().parent / "audit_repository.py"
REPOSITORY = Path(__file__).resolve().parents[4]
RECORD = ".claude/skills/audit-repository/references/rotation.json"
# One file in each area, and the files whose area depends on the classification order.
FIRST_RELEASE = {
    "deploy.py": "deploy\n",
    "deployer/plan.py": "plan\n",
    "agents/code-review-reviewer.md": "reviewer\n",
    "skills/review-prs/SKILL.md": "review\n",
    "tools/skill_evals.py": "evals\n",
    "tools/worktrees.py": "worktrees\n",
    "docs/code-review-operations.md": "operations\n",
    "skills/repo-cleanup/SKILL.md": "cleanup\n",
    "docs/skills.md": "reference\n",
    "docs/old guide.md": "old\n",
    "README.md": "readme\n",
}


def setUpModule() -> None:
    """Isolate Git from the user's configuration for this process and every Git it starts."""
    directory = Path(tempfile.mkdtemp(prefix="audit-repository-config-"))
    config = directory / "gitconfig"
    config.write_text(
        "[user]\n\tname = Fixture\n\temail = fixture@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n"
        "[core]\n\tautocrlf = false\n",
        encoding="utf-8",
    )
    patch = {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    saved = {name: os.environ.get(name) for name in patch}
    os.environ.update(patch)

    def restore() -> None:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        remove_tree(directory)

    unittest.addModuleCleanup(restore)


def remove_tree(path: Path) -> None:
    def writable(function, target, *_):
        Path(target).chmod(stat.S_IWRITE)
        function(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable)
    else:
        shutil.rmtree(path, onerror=writable)


def git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(directory), *arguments], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} failed: {result.stderr}")
    return result.stdout.strip()


def write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


def record_text(audits: list[dict[str, str]]) -> str:
    return json.dumps({"audits": audits}, indent=2) + "\n"


class Fixture(unittest.TestCase):
    """A repository with a space in its path, tagged v0.1.0 (lightweight) and v0.2.0 (annotated), with one commit
    after v0.2.0."""

    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp(prefix="audit-repository-test-")).resolve()
        self.addCleanup(remove_tree, self.temporary)
        self.root = self.temporary / "audited repo"
        self.root.mkdir()
        git(self.root, "init", "--quiet")
        record = record_text([{"date": "2026-01-02", "tag": "v0.1.0", "area": "deployer"}])
        write(self.root, {**FIRST_RELEASE, RECORD: record})
        self.commit("first release")
        git(self.root, "tag", "v0.1.0")
        write(
            self.root,
            {
                "deployer/plan.py": "plan, changed\n",
                "skills/review-prs/scripts/new tool.py": "tool\n",
                "tools/skill_evals.py": "evals, changed\n",
                "README.md": "readme, changed\n",
            },
        )
        (self.root / "docs" / "old guide.md").unlink()
        self.commit("second release")
        git(self.root, "tag", "-a", "v0.2.0", "-m", "v0.2.0")
        write(self.root, {"skills/repo-cleanup/SKILL.md": "cleanup, changed\n"})
        self.commit("after the second release")

    def commit(self, message: str) -> None:
        git(self.root, "add", "--all")
        git(self.root, "commit", "--quiet", "-m", message)

    def run_main(self, *arguments: str, today: date = date(2026, 3, 4)) -> tuple[int, list[str]]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = audit.main([*arguments, "--repository", str(self.root)], today=lambda: today)
        lines = output.getvalue().splitlines()
        for line in lines:
            if line.startswith("BRIEFS "):
                self.addCleanup(remove_tree, Path(line.split(" ", 1)[1].strip('"')))
        return code, lines

    def briefs(self, lines: list[str]) -> dict[str, str]:
        found = {}
        for line in lines:
            if line.startswith("BRIEF "):
                _, area, path = line.split(" ", 2)
                found[area] = Path(path.strip('"')).read_text(encoding="utf-8")
        return found


class ClassificationTests(unittest.TestCase):
    def test_each_path_belongs_to_the_area_the_issue_names(self) -> None:
        expected = {
            "deploy.py": "deployer",
            "deployer/plan.py": "deployer",
            "deploy-meta/review-prs.json": "deployer",
            "agents/code-review-reviewer.md": "deployer",
            "source.json": "deployer",
            "skills/code-review-core/scripts/review_pipeline.py": "code-review",
            "skills/review-prs/SKILL.md": "code-review",
            "skills/flag-review-finding/SKILL.md": "code-review",
            "tools/skill_evals.py": "code-review",
            "docs/code-review-operations.md": "code-review",
            "docs/code-review-operations-contract.md": "code-review",
            "skills/repo-cleanup/scripts/repo_cleanup.py": "other-skills",
            "skills/runtime-compatibility.md": "other-skills",
            "docs/skills.md": "other-skills",
            "tests/deployer/test_plan.py": "infrastructure",
            "tools/worktrees.py": "infrastructure",
            ".github/workflows/validate.yml": "infrastructure",
            ".claude/skills/audit-repository/SKILL.md": "infrastructure",
            ".agents/skills/audit-repository/SKILL.md": "infrastructure",
            "pyproject.toml": "infrastructure",
            "requirements-dev.txt": "infrastructure",
            "README.md": "infrastructure",
            "docs/releasing.md": "infrastructure",
            "deployer.md": "infrastructure",
            "skills-old/x.md": "infrastructure",
        }
        self.assertEqual(expected, {path: audit.area_of(path).name for path in expected})

    def test_the_rotation_order_is_fixed(self) -> None:
        self.assertEqual(("deployer", "code-review", "other-skills", "infrastructure"), audit.AREA_NAMES)

    def test_every_path_an_area_names_exists_in_this_repository(self) -> None:
        # A renamed document or directory would leave a brief pointing readers at nothing.
        missing = [
            f"{area.name}: {path}"
            for area in audit.AREAS
            for path in (*area.paths, *area.consult)
            if not (REPOSITORY / path.rstrip("/")).exists()
        ]
        self.assertEqual([], missing)


class RecordTests(unittest.TestCase):
    def entries(self, *areas: str) -> list[audit.Entry]:
        return [audit.Entry("2026-01-01", f"v0.{index}.0", area) for index, area in enumerate(areas)]

    def test_the_next_area_is_never_read_first_in_the_fixed_order(self) -> None:
        self.assertEqual("deployer", audit.next_area([]).name)
        self.assertEqual("code-review", audit.next_area(self.entries("deployer")).name)
        self.assertEqual("deployer", audit.next_area(self.entries("code-review", "other-skills")).name)

    def test_the_next_area_is_the_one_read_least_recently(self) -> None:
        entries = self.entries("deployer", "code-review", "other-skills", "infrastructure", "deployer")
        self.assertEqual("code-review", audit.next_area(entries).name)
        entries = self.entries("infrastructure", "other-skills", "code-review", "deployer")
        self.assertEqual("infrastructure", audit.next_area(entries).name)

    def test_this_repository_record_starts_with_the_first_rotation_audit(self) -> None:
        entries = audit.read_record(REPOSITORY / RECORD)
        self.assertEqual(audit.Entry("2026-10-09", "v0.4.0", "deployer"), entries[0])

    def test_a_malformed_record_fails_with_the_reason(self) -> None:
        directory = Path(tempfile.mkdtemp(prefix="audit-repository-record-"))
        self.addCleanup(remove_tree, directory)
        path = directory / "rotation.json"
        cases = {
            "not json": "cannot read the rotation record",
            "[]": "must be an object whose audits is a list",
            '{"audits": [{"date": "2026-01-01", "tag": "v1"}]}': "exactly a date, a tag, and an area",
            '{"audits": [{"date": "1 Jan", "tag": "v1", "area": "deployer"}]}': "not YYYY-MM-DD",
            '{"audits": [{"date": "2026-01-01", "tag": " ", "area": "deployer"}]}': "has no tag",
            '{"audits": [{"date": "2026-01-01", "tag": "v1", "area": "docs"}]}': "names area 'docs'",
            '{"audits": [{"date": "2026-01-01", "tag": "v1", "area": "deployer"}, '
            '{"date": "2026-02-01", "tag": "v1", "area": "code-review"}]}': "records v1 more than once",
        }
        for text, reason in cases.items():
            with self.subTest(text=text):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(audit.AuditError) as caught:
                    audit.read_record(path)
                self.assertIn(reason, str(caught.exception))
        with self.assertRaises(audit.AuditError) as caught:
            audit.read_record(directory / "missing.json")
        self.assertIn("no rotation record at", str(caught.exception))


class ReleaseTests(Fixture):
    def test_without_since_the_base_is_the_latest_tag_reachable_from_head(self) -> None:
        code, lines = self.run_main("release")
        self.assertEqual(0, code)
        base = git(self.root, "rev-parse", "v0.2.0^{commit}")
        self.assertEqual(f"BASE v0.2.0 {base}", lines[0])
        self.assertEqual(f"HEAD {git(self.root, 'rev-parse', 'HEAD')}", lines[1])
        self.assertIn("CHANGED 1", lines)
        self.assertEqual(
            ["AREA deployer 0", "AREA code-review 0", "AREA other-skills 1", "AREA infrastructure 0"],
            [line for line in lines if line.startswith("AREA ")],
        )
        briefs = self.briefs(lines)
        self.assertEqual(["other-skills"], list(briefs))
        self.assertIn("- M `skills/repo-cleanup/SKILL.md`", briefs["other-skills"])

    def test_since_a_ref_briefs_each_area_with_its_changes(self) -> None:
        code, lines = self.run_main("release", "--since", "v0.1.0")
        self.assertEqual(0, code)
        self.assertEqual(f"BASE v0.1.0 {git(self.root, 'rev-parse', 'v0.1.0')}", lines[0])
        self.assertIn("CHANGED 6", lines)
        briefs = self.briefs(lines)
        self.assertEqual(["deployer", "code-review", "other-skills", "infrastructure"], list(briefs))
        self.assertIn("- M `deployer/plan.py`", briefs["deployer"])
        self.assertIn("- A `skills/review-prs/scripts/new tool.py`", briefs["code-review"])
        self.assertIn("- M `tools/skill_evals.py`", briefs["code-review"])
        self.assertIn("- D `docs/old guide.md`", briefs["infrastructure"])
        self.assertIn("- M `README.md`", briefs["infrastructure"])
        self.assertNotIn("skill_evals", briefs["infrastructure"])
        self.assertNotIn("deploy.py", briefs["deployer"])
        self.assertIn("# Release audit: the deployer", briefs["deployer"])
        self.assertIn("- `tests/deployer/`", briefs["deployer"])

    def test_every_brief_carries_the_kinds_of_drift_and_the_report_template(self) -> None:
        _, lines = self.run_main("release", "--since", "v0.1.0")
        drift = [line.split(" ")[1] for line in lines if line.startswith("DRIFT ")]
        self.assertEqual(
            [
                "contract",
                "documents",
                "untested-claim",
                "grant",
                "dead-code",
                "untested-change",
                "error-path",
                "trust-boundary",
            ],
            drift,
        )
        fields = [line.split(" ")[1] for line in lines if line.startswith("TEMPLATE ")]
        self.assertEqual(["location", "kind", "evidence", "severity", "title", "template"], fields)
        sections = [line.split(" ")[1] for line in lines if line.startswith("SECTION ")]
        self.assertEqual(["findings", "unconfirmed", "coverage"], sections)
        for area, text in self.briefs(lines).items():
            with self.subTest(area=area):
                self.assertIn("read-only", text)
                self.assertIn("Confirm it against both sides", text)
                for kind in drift:
                    self.assertIn(f"- **{kind}**:", text)
                for field in fields:
                    self.assertIn(f"- **{field}**:", text)

    def test_since_head_finds_no_changes_and_writes_no_brief(self) -> None:
        code, lines = self.run_main("release", "--since", "HEAD")
        self.assertEqual(0, code)
        self.assertIn("CHANGED 0", lines)
        self.assertFalse([line for line in lines if line.startswith(("BRIEF", "BRIEFS"))])

    def test_a_repository_without_a_tag_fails_with_the_way_out(self) -> None:
        git(self.root, "tag", "-d", "v0.1.0", "v0.2.0")
        code, lines = self.run_main("release")
        self.assertEqual(1, code)
        self.assertEqual(["FAILED no tag is reachable from HEAD; pass --since <ref>"], lines)

    def test_an_unknown_or_option_like_since_fails(self) -> None:
        for ref in ("v9.9.9", "--output=x"):
            with self.subTest(ref=ref):
                code, lines = self.run_main("release", f"--since={ref}")
                self.assertEqual(1, code)
                self.assertEqual(1, len(lines))
                self.assertTrue(lines[0].startswith("FAILED "), lines)

    def test_a_directory_that_is_not_a_repository_fails(self) -> None:
        outside = Path(tempfile.mkdtemp(prefix="audit-repository-outside-"))
        self.addCleanup(remove_tree, outside)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = audit.main(["release", "--repository", str(outside)])
        self.assertEqual(1, code)
        self.assertEqual(f"FAILED {outside.as_posix()} is not a Git repository\n", output.getvalue())

    def test_a_git_that_cannot_run_fails_without_a_traceback(self) -> None:
        def missing(command: object, timeout: float) -> GitResult:
            raise GitError("Git executable 'git' was not found; install Git first", kind="prerequisite")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = audit.main(["release", "--repository", str(self.root)], git=GitClient(missing))
        self.assertEqual(1, code)
        self.assertEqual(
            "FAILED git rev-parse: Git executable 'git' was not found; install Git first\n", output.getvalue()
        )


class RotationTests(Fixture):
    def test_names_the_next_area_and_briefs_it_whole(self) -> None:
        write(self.root, {"tools/skill_evals_draft.py": "uncommitted\n"})
        code, lines = self.run_main("rotation")
        self.assertEqual(0, code)
        self.assertEqual(
            [
                f'RECORD "{RECORD}"',
                f"HEAD {git(self.root, 'rev-parse', 'HEAD')}",
                "READ deployer v0.1.0 2026-01-02",
                "UNREAD code-review",
                "UNREAD other-skills",
                "UNREAD infrastructure",
                "NEXT code-review",
                "FILES 4",
            ],
            lines[:8],
        )
        briefs = self.briefs(lines)
        self.assertEqual(["code-review"], list(briefs))
        listed = [line for line in briefs["code-review"].splitlines() if line.startswith("- `")]
        self.assertEqual(
            [
                "- `docs/code-review-operations.md`",
                "- `skills/review-prs/SKILL.md`",
                "- `skills/review-prs/scripts/new tool.py`",
                "- `tools/skill_evals.py`",
            ],
            listed[:4],
        )
        self.assertIn("# Rotation audit: the code review skills", briefs["code-review"])
        self.assertIn("- `agents/code-review-reviewer.md`", briefs["code-review"])
        self.assertNotIn("skill_evals_draft", briefs["code-review"])

    def test_record_appends_the_next_area_for_a_tag_and_writes_no_brief(self) -> None:
        code, lines = self.run_main("rotation", "--record", "v0.2.0", today=date(2026, 3, 4))
        self.assertEqual(0, code)
        self.assertEqual(["NEXT code-review", "RECORDED v0.2.0 code-review 2026-03-04"], lines[-2:])
        self.assertFalse([line for line in lines if line.startswith(("BRIEF", "DRIFT"))])
        written = (self.root / RECORD).read_bytes().decode("utf-8")
        self.assertEqual(
            record_text(
                [
                    {"date": "2026-01-02", "tag": "v0.1.0", "area": "deployer"},
                    {"date": "2026-03-04", "tag": "v0.2.0", "area": "code-review"},
                ]
            ),
            written,
        )
        _, lines = self.run_main("rotation")
        self.assertIn("READ code-review v0.2.0 2026-03-04", lines)
        self.assertIn("NEXT other-skills", lines)

    def test_record_refuses_a_recorded_or_unknown_tag_and_leaves_the_record(self) -> None:
        before = (self.root / RECORD).read_bytes()
        for tag, reason in (("v0.1.0", "v0.1.0 is already in the rotation record"), ("v9.9.9", "names no commit")):
            with self.subTest(tag=tag):
                code, lines = self.run_main("rotation", "--record", tag)
                self.assertEqual(1, code)
                self.assertTrue(lines[-1].startswith("FAILED ") and reason in lines[-1], lines)
                self.assertEqual(before, (self.root / RECORD).read_bytes())

    def test_a_missing_record_fails(self) -> None:
        (self.root / RECORD).unlink()
        code, lines = self.run_main("rotation")
        self.assertEqual(1, code)
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0].startswith("FAILED no rotation record at "), lines)


class CommandLineTests(Fixture):
    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        """Run the script as a program on a console whose code page cannot encode every path."""
        environment = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        return subprocess.run([sys.executable, "-B", str(SCRIPT), *arguments], capture_output=True, env=environment)

    def test_output_is_utf8_whatever_the_console_code_page(self) -> None:
        outside = Path(tempfile.mkdtemp(prefix="audit-repository-outside-")) / "not a repo π"
        outside.mkdir()
        self.addCleanup(remove_tree, outside.parent)
        result = self.run_script("release", "--repository", str(outside))
        self.assertEqual(1, result.returncode, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(f"FAILED {outside.as_posix()} is not a Git repository\n".encode(), result.stdout)

    def test_a_brief_names_a_path_the_console_code_page_cannot_encode(self) -> None:
        write(self.root, {"docs/π guide.md": "pi\n"})
        self.commit("a guide")
        result = self.run_script("release", "--repository", str(self.root))
        self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace"))
        lines = result.stdout.decode("utf-8").splitlines()
        briefs = next(line for line in lines if line.startswith("BRIEFS "))
        self.addCleanup(remove_tree, Path(briefs.split(" ", 1)[1].strip('"')))
        self.assertIn("- A `docs/π guide.md`", self.briefs(lines)["infrastructure"])

    def test_a_usage_error_exits_2(self) -> None:
        for arguments in ([], ["audit"], ["release", "--record", "v1"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, "-B", str(SCRIPT), *arguments], capture_output=True)
                self.assertEqual(2, result.returncode)
                self.assertEqual(b"", result.stdout)


if __name__ == "__main__":
    unittest.main()
