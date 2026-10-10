from __future__ import annotations

import ast
import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "skill-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "code-review-core" / "scripts"))

import upgrade_notes_kept
from review_runtime import declared_reviewer_files, glob_matcher, validate_adapter_manifest
from review_specialists import route, uncovered

REPOSITORY = Path(__file__).resolve().parents[4]
SKILL = ".claude/skills/review-repository"
MANIFEST_PATH = f"{SKILL}/references/specialists.json"
CONDITION = f"{SKILL}/scripts/upgrade_notes_kept.py"
CORE_SCRIPTS = "skills/skill-core/scripts"
MANIFEST = validate_adapter_manifest(json.loads((REPOSITORY / MANIFEST_PATH).read_text(encoding="utf-8")))


def tracked_files() -> list[str]:
    """The repository's files, untracked ones included, as a commit of this tree would hold them."""
    listed = subprocess.run(
        ["git", "-C", str(REPOSITORY), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
    )
    return [path for path in listed.stdout.split("\0") if path]


class UpgradeNotesConditionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def notes(self, text: str) -> None:
        path = self.root / "docs" / "upgrade-notes.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def test_notes_with_an_unreleased_section_open_the_reviewer(self) -> None:
        self.notes("# Upgrade notes\r\n\r\n## Unreleased\r\n\r\n## v0.4.0\r\n")
        self.assertEqual(
            (True, "docs/upgrade-notes.md keeps an Unreleased section"), upgrade_notes_kept.keeps_notes(self.root)
        )

    def test_notes_without_one_or_no_notes_close_it(self) -> None:
        self.assertEqual((False, "docs/upgrade-notes.md is not in the head"), upgrade_notes_kept.keeps_notes(self.root))
        self.notes("# Upgrade notes\n\n## v0.4.0\n\nSee ## Unreleased in the text.\n")
        self.assertEqual(
            (False, "docs/upgrade-notes.md has no Unreleased section"), upgrade_notes_kept.keeps_notes(self.root)
        )

    def test_unreadable_notes_leave_the_judgment_to_the_reviewer(self) -> None:
        self.notes("")
        (self.root / "docs" / "upgrade-notes.md").write_bytes(b"\xff\xfe## Unreleased\n")
        kept, reason = upgrade_notes_kept.keeps_notes(self.root)
        self.assertTrue(kept)
        self.assertTrue(reason.startswith("docs/upgrade-notes.md cannot be read: "), reason)

    def test_it_runs_from_the_files_the_manifest_declares_alone(self) -> None:
        # review-prs materializes only the declared files, at their repository paths, and runs the condition there,
        # so every module it imports must be declared too.
        reviewer = self.root / "reviewer"
        for path in declared_reviewer_files(MANIFEST):
            target = reviewer / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPOSITORY / path, target)
        source = self.root / "source"
        for text, code, line in (
            ("## Unreleased\n", 0, "OPEN docs/upgrade-notes.md keeps an Unreleased section"),
            ("## v0.4.0\n", 1, "CLOSED docs/upgrade-notes.md has no Unreleased section"),
        ):
            with self.subTest(code=code):
                (source / "docs").mkdir(parents=True, exist_ok=True)
                (source / "docs" / "upgrade-notes.md").write_text(text, encoding="utf-8")
                ran = subprocess.run(
                    [sys.executable, "-B", str(reviewer / CONDITION), "--source-root", str(source)],
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
                self.assertEqual((code, f"{line}\n", ""), (ran.returncode, ran.stdout, ran.stderr))

    def test_a_missing_source_root_is_a_usage_error_which_fails_the_review(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            upgrade_notes_kept.main(["--source-root", str(self.root / "missing")])
        self.assertEqual(2, raised.exception.code)


class ManifestTests(unittest.TestCase):
    def test_every_file_the_manifest_declares_is_in_the_repository(self) -> None:
        files = set(tracked_files())
        self.assertEqual([], [path for path in declared_reviewer_files(MANIFEST) if path not in files])

    def test_the_condition_imports_only_skill_core_modules_the_manifest_declares(self) -> None:
        tree = ast.parse((REPOSITORY / CONDITION).read_text(encoding="utf-8"))
        imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        core = {path.stem for path in (REPOSITORY / CORE_SCRIPTS).glob("*.py")}
        declared = {f"{CORE_SCRIPTS}/{module}.py" for module in imported & core}
        self.assertEqual({f"{CORE_SCRIPTS}/console.py"}, declared)
        self.assertLessEqual(declared, set(MANIFEST["resources"]))

    def test_the_condition_declares_the_one_file_it_reads_so_the_snapshot_stays_lazy(self) -> None:
        reads = MANIFEST["conditions"]["upgrade-notes-kept"]["reads"]
        self.assertEqual(["docs/upgrade-notes.md"], reads)
        self.assertTrue(glob_matcher(reads[0])(upgrade_notes_kept.NOTES))
        self.assertEqual(CONDITION, MANIFEST["conditions"]["upgrade-notes-kept"]["script"])

    def test_each_file_reaches_the_reviewers_its_route_names(self) -> None:
        expected = {
            "deployer/plan.py": {"deployer"},
            "deploy.py": {"deployer"},
            "deployer/manifest.py": {"deployer", "upgrade-notes"},
            "deployer/arguments.py": {"deployer", "upgrade-notes"},
            "skills/repo-cleanup/SKILL.md": {"skill-contract", "upgrade-notes"},
            "skills/category/example/SKILL.md": {"skill-contract", "upgrade-notes"},
            "deploy-meta/review-prs.json": {"skill-contract", "upgrade-notes"},
            "agents/code-review-reviewer.md": {"skill-contract"},
            "source.json": {"skill-contract"},
            ".claude/skills/audit-repository/SKILL.md": {"skill-contract"},
            ".agents/skills/audit-repository/SKILL.md": {"skill-contract"},
            "tests/validation/skill_scripts.py": {"validation-policy"},
            "tests/run_validation.py": {"validation-policy"},
            "pyproject.toml": {"validation-policy"},
            "docs/code-review-operations-contract.md": {"trust-boundary", "documentation", "upgrade-notes"},
            "skills/code-review-core/scripts/review_guard.py": {"trust-boundary"},
            "skills/code-review-core/scripts/review_source.py": {"trust-boundary"},
            "skills/code-review-core/scripts/test_adversarial_inputs.py": {"trust-boundary"},
            "skills/code-review-core/references/review-adapter.schema.json": {"upgrade-notes"},
            "docs/upgrade-notes.md": {"documentation", "upgrade-notes"},
            "docs/design.md": {"documentation"},
            "README.md": {"documentation"},
            "CLAUDE.md": {"documentation"},
            ".github/pull_request_template.md": {"documentation"},
            ".github/ISSUE_TEMPLATE/bug.md": {"documentation"},
        }
        routes = route(MANIFEST, list(expected), lambda name: True)
        reached: dict[str, set[str]] = {path: set() for path in expected}
        for specialist, paths in routes.items():
            for path in paths:
                reached[path].add(specialist)
        self.assertEqual(expected, reached)

    def test_the_generic_reviewer_takes_tools_skill_scripts_suites_ci_and_the_profiles(self) -> None:
        others = [
            "tools/worktrees.py",
            "skills/repo-cleanup/scripts/repo_cleanup.py",
            "skills/code-review-core/scripts/review_pipeline.py",
            "skills/code-review-core/scripts/test_review_pipeline.py",
            "tests/deployer/test_plan.py",
            "tests/tools/test_skill_evals.py",
            ".github/workflows/validate.yml",
            f"{SKILL}/references/deployer.md",
            CONDITION,
        ]
        self.assertEqual(others, uncovered(MANIFEST, others))

    def test_a_closed_condition_skips_only_the_upgrade_notes_reviewer(self) -> None:
        routes = route(MANIFEST, ["deployer/manifest.py"], lambda name: False)
        self.assertEqual({"deployer": ["deployer/manifest.py"]}, routes)

    def test_each_profile_names_design_sections_that_exist(self) -> None:
        design = (REPOSITORY / "docs" / "design.md").read_text(encoding="utf-8")
        headings = set(re.findall(r"^#{2,3} (.+)$", design, re.MULTILINE))
        named = {
            "deployer": ["One write point", "One platform seam", "Refusals before mutation, and journaled rollback"],
            "skill-contract": ["Tested scripts over prose", "Trust model"],
            "trust-boundary": ["Trust model"],
            "upgrade-notes": ["The structured record is the contract"],
        }
        for specialist in MANIFEST["specialists"]:
            profile = (REPOSITORY / specialist["profile"]).read_text(encoding="utf-8")
            self.assertIn("docs/design.md", profile, specialist["id"])
            for heading in named.get(specialist["id"], []):
                with self.subTest(specialist=specialist["id"], heading=heading):
                    self.assertIn(heading, headings)
                    self.assertIn(f'"{heading}"', profile)

    def test_each_profile_names_only_categories_the_manifest_lists(self) -> None:
        listed = set(MANIFEST["finding_categories"])
        for specialist in MANIFEST["specialists"]:
            profile = (REPOSITORY / specialist["profile"]).read_text(encoding="utf-8")
            line = next(line for line in profile.splitlines() if line.startswith("Categories:"))
            with self.subTest(specialist=specialist["id"]):
                self.assertLessEqual(set(re.findall(r"`([^`]+)`", line)), listed)


class RecommendedConfigurationTests(unittest.TestCase):
    def entry(self) -> dict[str, Any]:
        """The one repository entry SKILL.md's JSON fence shows, read as the configuration would hold it."""
        text = (REPOSITORY / SKILL / "SKILL.md").read_text(encoding="utf-8")
        fences = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
        self.assertEqual(1, len(fences))
        value = json.loads("{" + fences[0] + "}")
        self.assertEqual(1, len(value))
        entry: dict[str, Any] = next(iter(value.values()))
        return entry

    def test_the_entry_names_this_manifest(self) -> None:
        reviewer = self.entry()["reviewer"]
        self.assertEqual(MANIFEST_PATH, reviewer["manifest_path"])
        self.assertEqual(MANIFEST["id"], reviewer["id"])
        self.assertEqual("repository", reviewer["scope"])

    def test_the_exclusion_leaves_out_the_fixtures_and_no_file_the_reviewer_declares(self) -> None:
        patterns = self.entry()["snapshot_exclude"]
        self.assertEqual(["tests/fixtures/**"], patterns)
        matches = glob_matcher("tests/fixtures/**")
        files = tracked_files()
        fixtures = [path for path in files if path.startswith("tests/fixtures/")]
        self.assertTrue(fixtures)
        self.assertEqual(fixtures, [path for path in files if matches(path)])
        self.assertEqual([], [path for path in declared_reviewer_files(MANIFEST) if matches(path)])


if __name__ == "__main__":
    unittest.main()
