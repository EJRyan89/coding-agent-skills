"""What repo-cleanup may delete and protect beyond its shipped lists: the regenerable directories and branch prefixes
a user adds, each changing an outcome, and each added entry the sweep refuses, on the repositories and worktrees
test_repo_cleanup.py builds."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import cleanup_policy
import repo_cleanup as rc
from git_client import GitClient, GitResult
from test_repo_cleanup import (
    GIT_CONFIG,
    CleanupTestCase,
    Fixture,
    WorktreeTestCase,
    build_template,
    facts,
    git,
    remove_tree,
    written_plan,
)

# The settings conftest.py adds to the global configuration of every home this suite's git runs in.
git_config = GIT_CONFIG


def setUpModule() -> None:
    """Build the repositories every Fixture test starts from, with Git isolated as each test isolates it."""
    build_template()


class PolicyReadingFailureTests(CleanupTestCase):
    """What a command reads of the lists fails closed: a record whose lists are not lines, and an added entry Git
    cannot be asked about."""

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="repo cleanup policy reading ")).resolve()
        self.addCleanup(remove_tree, self.root)

    def test_a_sweep_record_whose_policy_is_not_a_list_of_lines_is_refused(self) -> None:
        record = self.root / rc.SWEEP_RECORD
        malformed_policies: list[object] = ["POLICY\tdefaults", [1], [None], {"lines": []}, [["POLICY"]]]
        for policy in malformed_policies:
            with self.subTest(policy=policy):
                rc.save_plan(record, {"schema_version": rc.RECORD_SCHEMA_VERSION, "repositories": [], "policy": policy})
                with self.assertRaises(rc.CleanupError) as raised:
                    rc.read_record(self.root)
                self.assertEqual(f"{record} is not a repo-cleanup sweep record", str(raised.exception))
        rc.save_plan(record, {"schema_version": rc.RECORD_SCHEMA_VERSION, "repositories": [], "policy": ["POLICY"]})
        self.assertEqual(([], ["POLICY"]), rc.read_record(self.root))

    def test_an_added_entry_git_cannot_be_asked_about_fails_rather_than_counting_as_regenerable(self) -> None:
        (self.root / ".venv").mkdir()

        def failing(command: Sequence[str], timeout: float) -> GitResult:
            return GitResult(128, "", "fatal: not a git repository")

        services = rc.Services(git=GitClient(failing))
        with self.assertRaises(rc.CleanupError) as raised:
            rc.regenerable_under(services, str(self.root), [".venv/"], cleanup_policy.configured(".venv"))
        self.assertEqual(f"git ls-files failed in {self.root}: fatal: not a git repository", str(raised.exception))
        shipped = cleanup_policy.configured()
        (self.root / "__pycache__").mkdir()
        self.assertEqual(
            ({"__pycache__/"}, {}), rc.regenerable_under(services, str(self.root), ["__pycache__/"], shipped)
        )


class RegenerableHelpTests(CleanupTestCase):
    def test_the_sweep_help_names_the_separator_an_added_entry_is_read_with(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            rc.main(["sweep", "--help"])
        text = " ".join(output.getvalue().split())
        self.assertIn("each <directory> or <directory>=<project file>,<project file>", text)
        (entry,) = cleanup_policy.configured(".tox=tox.ini,setup.cfg").added_regenerable()
        self.assertEqual(".tox=tox.ini,setup.cfg", entry, "the form the help shows is read as one entry with projects")


class AddedRegenerableTests(WorktreeTestCase):
    """An added regenerable directory lets a worktree go that the shipped list keeps, and a refused one never does."""

    def test_an_added_regenerable_directory_lets_a_worktree_holding_only_it_and_caches_go(self) -> None:
        self.ignore(".venv/", "__pycache__/")
        path = self.add("venv")
        self.fill(path, ".venv/lib/site.py", "__pycache__/a.pyc")
        removed, events, _ = self.remove("venv")
        self.assertEqual(rc.Removal(removed=False, refused=False), removed, "the shipped list alone keeps it")
        self.assertEqual(
            f"worktree {path} holds 1 ignored path git worktree remove would delete: .venv/; also 1 regenerable "
            "path: __pycache__/",
            events[0][2],
        )
        self.plan["events"].clear()
        self.plan["policy"] = {"regenerable": ".venv", "protected": ""}
        removed, events, _ = self.remove("venv")
        self.assertEqual(rc.Removal(removed=True, refused=False), removed)
        self.assertEqual(
            [["REMOVED", str(path), "venv", "deleted 2 regenerable paths: .venv/, __pycache__/"], ["DELETED", "venv"]],
            events,
        )
        self.assertFalse(path.exists())

    def test_an_added_directory_with_project_files_counts_only_beside_one_of_them(self) -> None:
        (self.repo / "app").mkdir()
        (self.repo / "app" / "setup.cfg").write_text("[metadata]\n", encoding="utf-8")
        git(self.repo, "add", "app/setup.cfg")
        git(self.repo, "commit", "--quiet", "-m", "project")
        self.ignore(".tox/")
        self.plan["policy"] = {"regenerable": ".tox=tox.ini,setup.cfg", "protected": ""}
        alone = self.add("alone")
        self.fill(alone, "lib/.tox/py311/log")
        removed, events, _ = self.remove("alone")
        self.assertEqual(rc.Removal(removed=False, refused=False), removed, "no project beside it")
        self.assertEqual(
            [
                [
                    "PRESERVED",
                    "alone",
                    f"worktree {alone} holds 1 ignored path git worktree remove would delete: lib/.tox/",
                ]
            ],
            events,
        )
        self.plan["events"].clear()
        beside = self.add("beside")
        self.fill(beside, "app/.tox/py311/log")
        removed, events, _ = self.remove("beside")
        self.assertEqual(rc.Removal(removed=True, refused=False), removed)
        self.assertEqual(["REMOVED", str(beside), "beside", "deleted 1 regenerable path: app/.tox/"], events[0])
        self.assertFalse(beside.exists())
        self.assertTrue((alone / "lib" / ".tox" / "py311" / "log").is_file())

    def test_an_added_directory_holding_tracked_files_is_refused_and_named_and_the_worktree_kept(self) -> None:
        (self.repo / "config").mkdir()
        (self.repo / "config" / "app.json").write_text("{}\n", encoding="utf-8")
        git(self.repo, "add", "config/app.json")
        git(self.repo, "commit", "--quiet", "-m", "configuration")
        self.ignore("config/local.env", "build/")
        self.plan["policy"] = {"regenerable": "config build", "protected": ""}
        path = self.add("configured")
        self.fill(path, "config/local.env", "build/out.bin")
        removed, events, _ = self.remove("configured")
        self.assertEqual(rc.Removal(removed=False, refused=False), removed)
        self.assertEqual(
            [
                [
                    "PRESERVED",
                    "configured",
                    f"worktree {path} holds 1 ignored path git worktree remove would delete: config/local.env; "
                    "refused added regenerable entry config: config holds tracked files; also 1 regenerable path: "
                    "build/",
                ]
            ],
            events,
        )
        self.assertTrue((path / "config" / "local.env").is_file())
        self.assertIsNotNone(self.tip("configured"))

    def test_a_refused_added_entry_is_not_used_and_keeps_the_worktree_it_would_have_let_go(self) -> None:
        self.ignore("cache/")
        path = self.add("cached")
        self.fill(path, "cache/data.bin")
        for entry in ("/cache", "C:/cache", "../cache", "cache/../cache", "./cache", "cache//", "=cache", "*ache"):
            with self.subTest(entry=entry):
                self.plan["events"].clear()
                self.plan["policy"] = {"regenerable": entry, "protected": ""}
                self.assertEqual([entry], [refused.entry for refused in rc.policy_of(self.plan).refused])
                removed, events, _ = self.remove("cached")
                self.assertEqual(rc.Removal(removed=False, refused=False), removed)
                self.assertEqual(
                    [
                        [
                            "PRESERVED",
                            "cached",
                            f"worktree {path} holds 1 ignored path git worktree remove would delete: cache/",
                        ]
                    ],
                    events,
                )
        self.assertTrue((path / "cache" / "data.bin").is_file())


class AddedProtectedTests(Fixture):
    """An added protected prefix keeps the branches and worktrees the shipped list lets go."""

    def test_an_added_protected_prefix_keeps_the_branches_and_worktrees_it_names(self) -> None:
        self.finished_branch("hotfix/login")
        self.finished_branch("topic")
        tree = self.area / "Hotfix" / "topic wt"
        git(self.clone, "worktree", "add", "--quiet", str(tree), "topic")
        finished, lines = self.sync()
        self.assertTrue(finished, lines)
        shipped = rc.build_plan(str(self.clone), str(self.repos), rc.Services(gh=self.github))
        self.assertEqual(
            {"hotfix/login": "delete", "topic": "remove-worktree"},
            {branch["name"]: branch["action"] for branch in shipped["branches"]},
            "the shipped list alone lets both go",
        )
        policy = cleanup_policy.configured(protected="hotfix/")
        plan = rc.build_plan(str(self.clone), str(self.repos), rc.Services(gh=self.github), policy=policy)
        self.assertEqual(
            {"hotfix/login": ("keep", "protected"), "topic": ("keep", "worktree-protected")},
            {branch["name"]: (branch["action"], branch["detail"]) for branch in plan["branches"]},
        )
        self.assertEqual(["protected"], [entry["action"] for entry in plan["worktrees"]])
        self.assertEqual({"regenerable": "", "protected": "hotfix/"}, plan["policy"])
        rc.save_plan(self.plan_file, plan)
        _, applied = self.apply()
        self.assertEqual([], facts(applied, "DELETED") + facts(applied, "REMOVED"))
        self.assertIsNotNone(self.tip("hotfix/login"))
        self.assertIsNotNone(self.tip("topic"))
        self.assertTrue(tree.is_dir())
        self.assertIn(
            "  Protected worktrees:     1 (preserved)", "\n".join(fields[0] for fields in facts(applied, "SUMMARY"))
        )


class PolicyLinesTests(Fixture):
    """The lines a sweep and the report print naming the lists the sweep used."""

    def sweep(self, *extra: str) -> tuple[int, list[str]]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = rc.main(
                ["sweep", "--repos-root", str(self.repos), *extra],
                rc.Services(gh=self.github),
            )
        return code, output.getvalue().splitlines()

    def report(self) -> tuple[int, list[str]]:
        return self.invoke("report", "--record", str(self.root / "plans"))

    def test_the_sweep_names_the_lists_it_used_after_plans_and_the_report_repeats_them(self) -> None:
        policy_file = cleanup_policy.POLICY_FILE.as_posix()
        code, lines = self.sweep()
        self.assertEqual(0, code, lines)
        self.assertEqual(f"POLICY\tdefaults\t{policy_file}", lines[1], "it follows PLANS")
        self.assertEqual([], facts(lines, "ADDED") + facts(lines, "REFUSED_ENTRY"))
        self.assertEqual({"regenerable": "", "protected": ""}, rc.load_plan(self.plan_file)["policy"])

        code, lines = self.sweep("--regenerable", ".venv ../cache bin .tox=tox.ini", "--protected", "hotfix/ Release/")
        self.assertEqual(0, code, lines)
        used = [
            f"POLICY\tdefaults+additions\t{policy_file}",
            "ADDED\tregenerable\t.venv",
            "ADDED\tregenerable\t.tox=tox.ini",
            "ADDED\tprotected\thotfix/",
            "REFUSED_ENTRY\tregenerable\t../cache\tis a parent-relative path, which reaches outside the worktree",
            "REFUSED_ENTRY\tregenerable\tbin\tis a shipped entry; an addition cannot change the project files it needs",
        ]
        self.assertEqual(used, lines[1:7])
        self.assertEqual(
            {"regenerable": ".venv ../cache bin .tox=tox.ini", "protected": "hotfix/ Release/"},
            rc.load_plan(self.plan_file)["policy"],
        )
        code, reported = self.report()
        self.assertEqual(0, code, reported)
        self.assertEqual(used, reported[: len(used)])

    def test_lists_joined_to_their_options_read_as_the_skill_renders_them_empty_or_set(self) -> None:
        # SKILL.md joins each list to its option, so an empty setting arrives as --regenerable= in every shell.
        code, lines = self.sweep("--regenerable=", "--protected=")
        self.assertEqual(0, code, lines)
        self.assertEqual(f"POLICY\tdefaults\t{cleanup_policy.POLICY_FILE.as_posix()}", lines[1])
        self.assertEqual({"regenerable": "", "protected": ""}, rc.load_plan(self.plan_file)["policy"])
        code, lines = self.sweep("--regenerable=.venv .tox=tox.ini", "--protected=hotfix/")
        self.assertEqual(0, code, lines)
        self.assertEqual(
            ["ADDED\tregenerable\t.venv", "ADDED\tregenerable\t.tox=tox.ini", "ADDED\tprotected\thotfix/"], lines[2:5]
        )

    def test_a_broken_shipped_policy_stops_the_sweep_before_anything_runs(self) -> None:
        broken = self.root / "cleanup-policy.json"
        broken.write_text('{"schema_version": 2}\n', encoding="utf-8")
        output = io.StringIO()
        with mock.patch.object(cleanup_policy, "POLICY_FILE", broken), contextlib.redirect_stdout(output):
            code = rc.main(
                ["sweep", "--repos-root", str(self.repos)],
                rc.Services(gh=self.github),
            )
        self.assertEqual(1, code)
        self.assertEqual(["FAILED"], [line.split("\t")[0] for line in output.getvalue().splitlines()])
        self.assertFalse((self.root / "plans").exists())
        self.assertEqual([], self.github.calls)


class RecordedPolicyTests(CleanupTestCase):
    """The policy a plan records, which every later command on the plan rebuilds."""

    def test_a_plan_whose_policy_is_not_the_recorded_configured_values_is_refused(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="repo cleanup plan ")).resolve()
        self.addCleanup(remove_tree, root)
        plan = root / "plan.json"
        recorded = written_plan()
        for policy in ({}, {"regenerable": ".venv"}, {"regenerable": ".venv", "protected": "hotfix/"}):
            with self.subTest(policy=policy):
                rc.save_plan(plan, {**recorded, "policy": policy})
                self.assertEqual(policy, rc.load_plan(plan)["policy"])
        malformed_policies: list[object] = [[], "x", {"regenerable": 1}, {"regenerable": None}, {"other": ""}]
        for malformed in malformed_policies:
            with self.subTest(policy=malformed):
                rc.save_plan(plan, {**recorded, "policy": malformed})
                with self.assertRaisesRegex(rc.CleanupError, "is not a repo-cleanup plan"):
                    rc.load_plan(plan)
        without = {field: value for field, value in recorded.items() if field != "policy"}
        rc.save_plan(plan, without)
        with self.assertRaisesRegex(rc.CleanupError, "plan must be an object with field policy"):
            rc.load_plan(plan)
        self.assertEqual(cleanup_policy.configured(), rc.policy_of({"policy": {}}), "no configured additions")
        self.assertEqual(
            cleanup_policy.configured(".venv", "hotfix/"),
            rc.policy_of({"policy": {"regenerable": ".venv", "protected": "hotfix/"}}),
        )
