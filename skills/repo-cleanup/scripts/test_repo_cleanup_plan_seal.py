"""The plan file the confirmation commands take: they delete only what a sweep offered, so a plan this script did not
save, or one changed since it saved it, is refused before anything is deleted, and a missing or malformed field ends
in the documented refusal, never a traceback."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import repo_cleanup as rc
from test_repo_cleanup import (
    GIT_CONFIG,
    UNNAMEABLE,
    CleanupTestCase,
    Fixture,
    build_template,
    facts,
    git,
    remove_tree,
    written_plan,
)

# The settings conftest.py adds to the global configuration of every home this suite's git runs in.
git_config = GIT_CONFIG

# The refusals, by what they name after the plan's path; independent of the script's own wording constants.
NOT_SAVED = "is not a plan repo-cleanup saved at this path, so it may name what no sweep offered; run the sweep again"
CHANGED = (
    "changed since repo-cleanup last saved this plan there, so it may name what no sweep offered; run the sweep again"
)
OTHER_AREAS = "names worktree areas other than the ones a sweep derives for"
NOT_A_PLAN = "is not a repo-cleanup plan: "


def setUpModule() -> None:
    """Build the repositories every Fixture test starts from, with Git isolated as each test isolates it."""
    build_template()


class PlanSealTests(Fixture):
    """A swept repository offering a local-only branch and a leftover directory, beside a branch with an open pull
    request and a folder outside every worktree area, which the sweep offers neither of."""

    def setUp(self) -> None:
        super().setUp()
        git(self.clone, "branch", "scratch", "main")
        self.open_tip = self.push_branch("open-work")
        self.github.pull("open-work", "OPEN", self.open_tip)
        self.leftover = self.area / "leftover"
        self.leftover.mkdir(parents=True)
        (self.leftover / "notes.txt").write_text("left behind\n", encoding="utf-8")
        self.outside = self.root / "outside" / "kept"
        self.outside.mkdir(parents=True)
        (self.outside / "precious.txt").write_text("the user's\n", encoding="utf-8")
        code, lines = self.invoke("sweep", "--repos-root", str(self.repos), "my repo")
        self.assertEqual(0, code, lines)
        self.assertEqual([[self.plan_file.as_posix()]], facts(lines, "PLAN"))
        self.assertEqual([["scratch"]], facts(lines, "CONFIRM_LOCAL"))
        self.assertEqual([[self.leftover.as_posix()]], facts(lines, "ORPHAN"))
        self.swept = self.plan_file.read_bytes()

    def rewrite(self, change: Callable[[dict[str, Any]], None], path: Path | None = None) -> Path:
        """Write the swept plan, changed by `change`, to `path` (the plan file itself by default) without its seal, in
        JSON's compact form."""
        plan = json.loads(self.swept)
        change(plan)
        target = path or self.plan_file
        target.write_bytes(json.dumps(plan).encode("utf-8"))
        return target

    def confirmations(self, plan: Path) -> list[tuple[int, list[str]]]:
        """Each confirmation command on `plan`, naming what the sweep offered, what it kept, and what lies outside."""
        return [
            self.invoke("delete-local", "--plan", str(plan), "--branch", "scratch", "--branch", "open-work"),
            self.invoke("force-delete", "--plan", str(plan), "--branch", "open-work", "--branch", "scratch"),
            self.invoke(
                "remove-directory", "--plan", str(plan), "--path", str(self.leftover), "--path", str(self.outside)
            ),
        ]

    def assert_refused(self, plan: Path, refusal: str, *, printing: bool = False) -> None:
        """Each confirmation command, and with `printing` the report of the sweep whose record names the plan, ends in
        `refusal` alone, deleting nothing."""
        runs = [(str(plan), run) for run in self.confirmations(plan)]
        if printing:
            self.assertEqual(self.plan_file, plan, "the record names only the plan the sweep saved")
            # The report names the plan as the sweep printed it.
            runs.append((plan.as_posix(), self.invoke("report", "--record", str(plan.parent))))
        for named, (code, lines) in runs:
            self.assertEqual(1, code, lines)
            self.assertEqual(1, len(lines), lines)
            self.assertTrue(lines[0].startswith(f"FAILED\t{named} {refusal}"), lines)
        self.assert_untouched()

    def assert_untouched(self) -> None:
        self.assertEqual(self.open_tip, self.tip("open-work"))
        self.assertIsNotNone(self.tip("scratch"))
        self.assertTrue((self.leftover / "notes.txt").is_file())
        self.assertTrue((self.outside / "precious.txt").is_file())

    def test_an_untouched_plan_deletes_what_the_sweep_offered_and_each_command_reseals_it(self) -> None:
        code, lines = self.invoke("delete-local", "--plan", str(self.plan_file), "--branch", "scratch")
        self.assertEqual((0, [["scratch"]]), (code, facts(lines, "DELETED")), lines)
        self.assertNotEqual(self.swept, self.plan_file.read_bytes(), "the command recorded what it did")
        code, lines = self.invoke("remove-directory", "--plan", str(self.plan_file), "--path", str(self.leftover))
        self.assertEqual((0, [[self.leftover.as_posix()]]), (code, facts(lines, "DELETED_DIR")), lines)
        self.assertFalse(self.leftover.exists())
        code, lines = self.invoke("force-delete", "--plan", str(self.plan_file), "--branch", "open-work")
        self.assertEqual(
            (1, [["open-work", "not reported UNMERGED by this plan"]]), (code, facts(lines, "PRESERVED")), lines
        )
        self.assertEqual(self.open_tip, self.tip("open-work"))

    def test_a_plan_changed_after_the_sweep_saved_it_is_refused_before_anything_is_deleted(self) -> None:
        other = self.repos / "other repo"
        git(self.root, "clone", "--quiet", str(self.remote), str(other))
        outside_area = str(self.outside.parent)

        def area_outside(plan: dict[str, Any]) -> None:
            plan["orphan_areas"] = [outside_area]
            plan["events"].append(["ORPHAN", self.outside.as_posix()])

        def other_root(plan: dict[str, Any]) -> None:
            plan["repo_root"] = other.as_posix()

        def unoffered_anchor(plan: dict[str, Any]) -> None:
            plan["worktree_area"] = str(self.root / "outside" / "Worktrees")
            plan["events"].append(["ORPHAN", self.outside.as_posix()])

        def unoffered_branch(plan: dict[str, Any]) -> None:
            plan["events"].append(["UNMERGED", "open-work", self.open_tip])
            plan["events"].append(["CONFIRM_LOCAL", "open-work"])

        def reformatted(plan: dict[str, Any]) -> None:
            del plan  # only the layout changes: the seal holds the plan's bytes, not its meaning

        tampers = {
            "an area outside the repository": area_outside,
            "another repository's root": other_root,
            "an anchor the sweep never offered": unoffered_anchor,
            "a branch the sweep never offered": unoffered_branch,
            "the same plan reformatted": reformatted,
        }
        for name, change in tampers.items():
            with self.subTest(name):
                self.assert_refused(self.rewrite(change), CHANGED, printing=True)
        self.plan_file.write_bytes(self.swept)
        code, lines = self.invoke("delete-local", "--plan", str(self.plan_file), "--branch", "scratch")
        self.assertEqual((0, [["scratch"]]), (code, facts(lines, "DELETED")), "the plan as saved still works")

    def test_a_plan_file_the_script_never_saved_there_is_refused(self) -> None:
        # Unchanged but elsewhere, as a copy inside a repository or a file the caller wrote would be.
        for copy in (self.clone / "plan.json", self.root / "plans" / "copy.json"):
            with self.subTest(copy=copy):
                shutil.copyfile(self.plan_file, copy)
                self.assert_refused(copy, NOT_SAVED)

    def test_a_resealed_plan_whose_areas_or_root_are_not_derived_from_its_repository_is_refused(self) -> None:
        # Saved through the script's own save, so the seal matches: the derived areas and the live repository are
        # checked as well.
        not_a_repository = self.root / "outside"

        def save(change: Callable[[dict[str, Any]], None]) -> None:
            plan = json.loads(self.swept)
            change(plan)
            rc.save_plan(self.plan_file, plan)

        def area_outside(plan: dict[str, Any]) -> None:
            plan["orphan_areas"] = [str(self.outside.parent), plan["orphan_areas"][1]]

        def unoffered_anchor(plan: dict[str, Any]) -> None:
            plan["worktree_area"] = str(not_a_repository / "Worktrees")

        def other_name(plan: dict[str, Any]) -> None:
            plan["repo_name"] = "other repo"

        def relative_root(plan: dict[str, Any]) -> None:
            plan["repo_root"] = "my repo"

        for name, change in {
            "an area outside the repository": area_outside,
            "an anchor the sweep never offered": unoffered_anchor,
            "another repository's name": other_name,
            "a relative root": relative_root,
        }.items():
            with self.subTest(name):
                save(change)
                self.assert_refused(self.plan_file, OTHER_AREAS)

        def moved_root(plan: dict[str, Any]) -> None:
            root = not_a_repository / "my repo"
            plan["repo_root"] = root.as_posix()
            plan["orphan_areas"][1] = str(root / ".claude" / "worktrees")

        save(moved_root)
        root = (not_a_repository / "my repo").as_posix()
        for code, lines in self.confirmations(self.plan_file):
            self.assertEqual((1, [f"FAILED\t{root} is not a git repository"]), (code, lines))
        self.assert_untouched()

    def test_a_plan_with_a_missing_or_malformed_field_ends_in_the_refusal_not_a_traceback(self) -> None:
        def without(field: str) -> Callable[[dict[str, Any]], None]:
            return lambda plan: plan.pop(field)

        def setting(path: list[Any], value: object) -> Callable[[dict[str, Any]], None]:
            def change(plan: dict[str, Any]) -> None:
                holder: Any = plan
                for key in path[:-1]:
                    holder = holder[key]
                holder[path[-1]] = value

            return change

        swept = json.loads(self.swept)
        self.assertTrue(swept["branches"] and swept["events"], "the sweep planned branches and recorded events")
        fields = [
            "schema_version",
            "repo_root",
            "repo_name",
            "worktree_area",
            "orphan_areas",
            "default",
            "branches",
            "worktrees",
            "fastforward",
            "fastforward_skipped",
            "diverged",
            "policy",
            "applied",
            "events",
        ]
        changes: dict[str, tuple[Callable[[dict[str, Any]], None], str]] = {
            **{f"no {field}": (without(field), f"plan must be an object with field {field}") for field in fields},
            "no branch sha": (
                lambda plan: plan["branches"][0].pop("sha"),
                "branches[0] must be an object with field sha",
            ),
            "a null repository root": (setting(["repo_root"], None), "repo_root must be a non-empty string"),
            "an area that is not a string": (setting(["orphan_areas", 0], 7), "orphan_areas[0] must be a non-empty"),
            "an event with too few fields": (
                setting(["events", 0], ["UNMERGED", "scratch"]),
                "events[0] must be a UNMERGED event with 2 fields",
            ),
            "an event of an unknown kind": (setting(["events", 0], ["DELETE_ALL", "x"]), "events[0][0] must be one"),
            "an events list that is an object": (setting(["events"], {}), "events must be an array"),
            "an unknown field": (
                setting(["extra"], 1),
                'plan must be an object of known fields, found unknown field "',
            ),
        }
        for name, (change, detail) in changes.items():
            with self.subTest(name):
                plan = json.loads(self.swept)
                change(plan)
                rc.save_plan(self.plan_file, plan)
                runs = [(str(self.plan_file), run) for run in self.confirmations(self.plan_file)]
                report = self.invoke("report", "--record", str(self.plan_file.parent))
                runs.append((self.plan_file.as_posix(), report))
                for named, (code, lines) in runs:
                    self.assertEqual(1, code, lines)
                    self.assertEqual(1, len(lines), lines)
                    self.assertTrue(lines[0].startswith(f"FAILED\t{named} {NOT_A_PLAN}{detail}"), lines)
                self.assert_untouched()

    def test_a_sweep_record_changed_after_the_sweep_saved_it_is_refused_by_the_report_and_a_later_sweep(self) -> None:
        plans = self.root / "plans"
        record = plans / rc.SWEEP_RECORD
        saved = record.read_bytes()
        changed = json.loads(saved)
        changed["repositories"][0]["lines"] = ["SUMMARY\tmy repo cleanup complete:"]
        refusal = (
            f"FAILED\t{record} changed since repo-cleanup last saved this sweep record there, so it may name what no "
            "sweep offered; run the sweep again"
        )
        for name, data in {
            "changed": json.dumps(changed).encode("utf-8"),
            "reformatted": json.dumps(json.loads(saved)).encode("utf-8"),
        }.items():
            with self.subTest(name):
                record.write_bytes(data)
                for arguments in (
                    ["report", "--record", str(plans)],
                    ["sweep", "--repos-root", str(self.repos), "--record", str(plans), "--skip-checkout", "my repo"],
                ):
                    code, lines = self.invoke(*arguments)
                    self.assertEqual(1, code, lines)
                    self.assertEqual(1, len(lines), lines)
                    self.assertTrue(lines[0].startswith(refusal), lines)
                    self.assertEqual(data, record.read_bytes(), "the refused sweep wrote nothing")
        record.write_bytes(saved)
        code, lines = self.invoke("report", "--record", str(plans))
        self.assertEqual(0, code, lines)

    def test_the_report_refuses_a_changed_plan_its_sealed_record_names(self) -> None:
        self.rewrite(lambda plan: plan["events"].append(["DELETED", "scratch"]))
        code, lines = self.invoke("report", "--record", str(self.root / "plans"))
        self.assertEqual(1, code, lines)
        self.assertEqual(1, len(lines), lines)
        self.assertTrue(lines[0].startswith(f"FAILED\t{self.plan_file.as_posix()} {CHANGED}"), lines)

    def test_a_branch_named_only_by_unicode_whitespace_is_preserved_and_the_rest_cleaned(self) -> None:
        # Git accepts a name of a no-break space; its flattened form names nothing, so it is never offered.
        blank = "\u00a0"
        git(self.clone, "branch", blank, "main")
        code, lines = self.invoke("sweep", "--repos-root", str(self.repos), "my repo")
        self.assertEqual([[f"{self.repos.as_posix()}/my repo", "cleaned"]], facts(lines, "REPO"), lines)
        self.assertEqual([], facts(lines, "ERROR"), lines)
        plan = rc.load_plan(self.plan_file)
        self.assertIn(blank, [branch["name"] for branch in plan["branches"]])
        self.assertIn(["PRESERVED", "", UNNAMEABLE.format("")], plan["events"], "listed by its flattened form")
        code, lines = self.invoke("delete-local", "--plan", str(self.plan_file), "--branch", "scratch")
        self.assertEqual((0, [["scratch"]]), (code, facts(lines, "DELETED")), lines)
        self.assertIsNotNone(self.tip(blank))

    def test_a_record_directory_inside_a_skills_directory_is_refused_before_the_sweep_writes_there(self) -> None:
        home = self.root / "home"
        record = home / ".claude" / "skills" / "repo-cleanup" / "plans"
        record.mkdir(parents=True)
        shutil.copyfile(self.root / "plans" / rc.SWEEP_RECORD, record / rc.SWEEP_RECORD)
        before = sorted(path.name for path in record.iterdir())
        with mock.patch.dict(os.environ, {"USERPROFILE": str(home), "HOME": str(home)}):
            code, lines = self.invoke(
                "sweep", "--repos-root", str(self.repos), "--record", str(record), "--skip-checkout", "my repo"
            )
        self.assertEqual(1, code, lines)
        self.assertEqual(1, len(lines), lines)
        self.assertTrue(lines[0].startswith(f"FAILED\t{record} is inside the skills directory "), lines)
        self.assertEqual(before, sorted(path.name for path in record.iterdir()))


class PlanFieldTests(CleanupTestCase):
    """What the plan reader accepts and refuses, field by field, on plans saved with the script's own writer."""

    def setUp(self) -> None:
        super().setUp()
        root = Path(tempfile.mkdtemp(prefix="repo cleanup fields ")).resolve()
        self.addCleanup(remove_tree, root)
        self.plan = root / "plan.json"

    def read(self, plan: dict[str, Any]) -> dict[str, Any]:
        rc.save_plan(self.plan, plan)
        return rc.load_plan(self.plan)

    def refused(self, plan: dict[str, Any], detail: str) -> None:
        rc.save_plan(self.plan, plan)
        with self.assertRaises(rc.CleanupError) as raised:
            rc.load_plan(self.plan)
        self.assertTrue(str(raised.exception).startswith(f"{self.plan} {NOT_A_PLAN}{detail}"), raised.exception)

    def test_a_branch_name_of_unicode_whitespace_is_read_wherever_a_plan_names_a_branch(self) -> None:
        for blank in ("\u00a0", "\u3000", " "):
            with self.subTest(name=repr(blank)):
                plan = written_plan(
                    default={"name": blank, "status": "up to date"},
                    branches=[
                        {
                            "name": blank,
                            "category": "local",
                            "pr": "NONE",
                            "sha": "a" * 40,
                            "action": "ask-delete",
                            "detail": "",
                            "worktree": None,
                        }
                    ],
                    worktrees=[
                        {"path": "/repos/Worktrees/my repo/wt", "branch": blank, "action": "keep", "detail": ""}
                    ],
                    fastforward=[{"branch": blank, "sha": "a" * 40, "target": "b" * 40, "worktree": None}],
                    fastforward_skipped=[{"branch": blank, "behind": 1, "reason": "changes"}],
                    diverged=[{"branch": blank, "ahead": 1, "behind": 2}],
                    sync={
                        "default": {"name": blank, "note": ""},
                        "checkout": "current",
                        "fast_forward": ["current"],
                        "behind": 0,
                    },
                )
                self.assertEqual(plan, self.read(plan))

    def test_the_sync_record_is_read_and_a_failed_outcome_needs_its_reason(self) -> None:
        synced = {
            "default": {"name": "main", "note": ""},
            "checkout": "switched",
            "fast_forward": ["failed", "not a fast-forward"],
            "behind": 2,
        }
        self.assertEqual(synced, self.read(written_plan(sync=synced))["sync"])
        cases: dict[str, tuple[dict[str, Any], str]] = {
            "a failed outcome without its reason": (
                {**synced, "fast_forward": ["failed"]},
                "sync.fast_forward must be a failed outcome with its reason",
            ),
            "no outcome": ({**synced, "fast_forward": []}, "sync.fast_forward must be a non-empty array"),
            "no checkout": ({key: synced[key] for key in ("default", "fast_forward", "behind")}, "sync must be"),
            "a negative count behind": ({**synced, "behind": -1}, "sync.behind must be an integer of at least 0"),
            "a note that is not a string": (
                {**synced, "default": {"name": "main", "note": 3}},
                "sync.default.note must be a string",
            ),
            "a default with no note": (
                {**synced, "default": {"name": "main"}},
                "sync.default must be an object with field note",
            ),
            "an unknown default field": (
                {**synced, "default": {"name": "main", "note": "", "other": ""}},
                "sync.default must be an object of known fields",
            ),
        }
        for name, (value, detail) in cases.items():
            with self.subTest(name):
                self.refused(written_plan(sync=value), detail)

    def test_stopped_and_the_default_branch_are_read_by_their_kinds(self) -> None:
        self.assertEqual("git vanished", self.read(written_plan(stopped="git vanished"))["stopped"])
        cases: dict[str, tuple[dict[str, Any], str]] = {
            "a stopped marker that is not a string": (written_plan(stopped=1), "stopped must be a non-empty string"),
            "an empty stopped marker": (written_plan(stopped=""), "stopped must be a non-empty string"),
            "no default status": (
                written_plan(default={"name": "main"}),
                "default must be an object with field status",
            ),
            "a null default name": (
                written_plan(default={"name": None, "status": "up to date"}),
                "default.name must be a string",
            ),
            "an empty default status": (
                written_plan(default={"name": "main", "status": ""}),
                "default.status must be a non-empty string",
            ),
        }
        for name, (plan, detail) in cases.items():
            with self.subTest(name):
                self.refused(plan, detail)

    def test_each_nested_entry_is_read_field_by_field(self) -> None:
        branch = {
            "name": "topic",
            "category": "gone",
            "pr": "MERGED",
            "sha": "a" * 40,
            "action": "delete",
            "detail": "",
            "worktree": None,
        }
        tree = {"path": "/repos/Worktrees/my repo/wt", "branch": None, "action": "ask", "detail": "clean", "sha": ""}
        move = {"branch": "topic", "sha": "a" * 40, "target": "b" * 40, "worktree": "/repos/Worktrees/my repo/wt"}
        plan = written_plan(
            branches=[branch],
            worktrees=[tree],
            fastforward=[move],
            fastforward_skipped=[{"branch": "topic", "behind": 3, "reason": "changes"}],
            diverged=[{"branch": "topic", "ahead": 1, "behind": 2}],
        )
        self.assertEqual(plan, self.read(plan))
        cases: dict[str, tuple[dict[str, Any], str]] = {
            "a branch of an unknown category": (
                written_plan(branches=[{**branch, "category": "remote"}]),
                "branches[0].category must be one of",
            ),
            "a branch with an empty sha": (
                written_plan(branches=[{**branch, "sha": ""}]),
                "branches[0].sha must be a non-empty string",
            ),
            "a branch whose worktree is a number": (
                written_plan(branches=[{**branch, "worktree": 1}]),
                "branches[0].worktree must be a non-empty string or null",
            ),
            "a worktree with no detail": (
                written_plan(worktrees=[{key: tree[key] for key in ("path", "branch", "action")}]),
                "worktrees[0] must be an object with field detail",
            ),
            "a worktree whose sha is null": (
                written_plan(worktrees=[{**tree, "sha": None}]),
                "worktrees[0].sha must be a string",
            ),
            "a worktree with an empty path": (
                written_plan(worktrees=[{**tree, "path": ""}]),
                "worktrees[0].path must be a non-empty string",
            ),
            "a fast-forward with no target": (
                written_plan(fastforward=[{key: move[key] for key in ("branch", "sha", "worktree")}]),
                "fastforward[0] must be an object with field target",
            ),
            "a skipped fast-forward behind by a boolean": (
                written_plan(fastforward_skipped=[{"branch": "topic", "behind": True, "reason": "changes"}]),
                "fastforward_skipped[0].behind must be an integer of at least 0",
            ),
            "a diverged branch ahead by a string": (
                written_plan(diverged=[{"branch": "topic", "ahead": "1", "behind": 2}]),
                "diverged[0].ahead must be an integer of at least 0",
            ),
            "an applied flag that is a number": (written_plan(applied=1), "applied must be a boolean"),
        }
        for name, (value, detail) in cases.items():
            with self.subTest(name):
                self.refused(value, detail)

    def test_a_path_with_no_seal_is_refused_as_never_saved_there_and_a_changed_one_as_changed(self) -> None:
        rc.save_plan(self.plan, written_plan())
        rc.seal_file(self.plan).unlink()
        with self.assertRaises(rc.CleanupError) as raised:
            rc.load_plan(self.plan)
        self.assertEqual(
            f"{self.plan} is not a plan repo-cleanup saved at this path, so it may name what no sweep offered; "
            "run the sweep again for a new plan",
            str(raised.exception),
        )
        rc.save_plan(self.plan, written_plan())
        self.plan.write_bytes(self.plan.read_bytes() + b" ")
        with self.assertRaises(rc.CleanupError) as raised:
            rc.load_plan(self.plan)
        self.assertEqual(
            f"{self.plan} changed since repo-cleanup last saved this plan there, so it may name what no sweep "
            "offered; run the sweep again for a new plan",
            str(raised.exception),
        )

    def test_a_plan_path_holding_a_lone_surrogate_is_sealed_and_read_back(self) -> None:
        # A name of bytes that are not UTF-8 reaches Python as a lone surrogate, which the seal's name must hash.
        plan = self.plan.with_name("plan \udce9.json")
        rc.save_plan(plan, written_plan())
        self.assertEqual(written_plan(), rc.load_plan(plan))
        self.assertNotEqual(rc.seal_file(plan), rc.seal_file(self.plan))

    def test_each_refusal_names_the_field_it_refuses_and_what_it_found(self) -> None:
        branch = {
            "name": "topic",
            "category": "gone",
            "pr": "MERGED",
            "sha": "a" * 40,
            "action": "delete",
            "detail": "",
            "worktree": None,
        }
        tree = {"path": "/repos/Worktrees/my repo/wt", "branch": "topic", "action": "keep", "detail": ""}
        move = {"branch": "topic", "sha": "a" * 40, "target": "b" * 40, "worktree": None}
        synced = {
            "default": {"name": "main", "note": ""},
            "checkout": "current",
            "fast_forward": ["current"],
            "behind": 0,
        }
        cases: list[tuple[dict[str, Any], str]] = [
            (written_plan(branches=[{**branch, "name": 1}]), "branches[0].name must be a string, found integer 1"),
            (
                written_plan(branches=[{**branch, "detail": 1}]),
                "branches[0].detail must be a string, found integer 1",
            ),
            (written_plan(worktrees=[{**tree, "branch": 1}]), "worktrees[0].branch must be a string, found integer 1"),
            (
                written_plan(worktrees=[{**tree, "action": 1}]),
                "worktrees[0].action must be a non-empty string, found integer 1",
            ),
            (written_plan(worktrees=[{**tree, "detail": 1}]), "worktrees[0].detail must be a string, found integer 1"),
            (
                written_plan(fastforward=[{**move, "branch": 1}]),
                "fastforward[0].branch must be a string, found integer 1",
            ),
            (
                written_plan(fastforward=[{**move, "target": 1}]),
                "fastforward[0].target must be a non-empty string, found integer 1",
            ),
            (
                written_plan(fastforward=[{**move, "worktree": 1}]),
                "fastforward[0].worktree must be a non-empty string or null, found integer 1",
            ),
            (
                written_plan(diverged=[{"ahead": 1, "behind": 2}]),
                "diverged[0] must be an object with field branch, found an object without it",
            ),
            (
                written_plan(diverged=[{"branch": 1, "ahead": 1, "behind": 2}]),
                "diverged[0].branch must be a string, found integer 1",
            ),
            (
                written_plan(fastforward_skipped=[{"branch": "topic", "behind": 1, "reason": 1}]),
                "fastforward_skipped[0].reason must be a non-empty string, found integer 1",
            ),
            (
                written_plan(sync={**synced, "default": {"name": 1, "note": ""}}),
                "sync.default.name must be a string, found integer 1",
            ),
            (written_plan(sync={**synced, "checkout": 1}), "sync.checkout must be a non-empty string, found integer 1"),
            (
                written_plan(sync={**synced, "fast_forward": [1]}),
                "sync.fast_forward[0] must be a non-empty string, found integer 1",
            ),
            (
                written_plan(sync={**synced, "fast_forward": ["failed"]}),
                "sync.fast_forward must be a failed outcome with its reason, found no reason",
            ),
        ]
        for plan, detail in cases:
            with self.subTest(detail):
                rc.save_plan(self.plan, plan)
                with self.assertRaises(rc.CleanupError) as raised:
                    rc.load_plan(self.plan)
                self.assertEqual(f"{self.plan} {NOT_A_PLAN}{detail}", str(raised.exception))

    def test_an_event_is_refused_when_empty_or_of_the_wrong_count_or_kind_of_field(self) -> None:
        cases: list[tuple[list[Any], str]] = [
            ([], "events[0] must be a non-empty array, found an empty array"),
            (["UNMERGED", "topic"], "events[0] must be a UNMERGED event with 2 fields, found 1 field"),
            (["DELETED"], "events[0] must be a DELETED event with 1 fields, found 0 fields"),
            (
                ["CONFIRM_LOCAL", "topic", "/repos/Worktrees/my repo/wt", "extra"],
                "events[0] must be a CONFIRM_LOCAL event with 1 to 2 fields, found 3 fields",
            ),
            (["PRESERVED", 1, "why"], "events[0][1] must be a string, found integer 1"),
            (["PRESERVED", "topic", None], "events[0][2] must be a string, found null"),
        ]
        for event, detail in cases:
            with self.subTest(detail):
                rc.save_plan(self.plan, written_plan(events=[event]))
                with self.assertRaises(rc.CleanupError) as raised:
                    rc.load_plan(self.plan)
                self.assertEqual(f"{self.plan} {NOT_A_PLAN}{detail}", str(raised.exception))


class SealReadingTests(CleanupTestCase):
    """A seal repo-cleanup cannot read is refused as that, never as a plan changed since its save, and no plan or
    record is saved where the seals lie."""

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="repo cleanup seal reading ")).resolve()
        self.addCleanup(remove_tree, self.root)

    def test_a_seal_that_is_not_utf_8_or_not_a_file_is_refused_as_unreadable(self) -> None:
        plan = self.root / "plan.json"
        record = self.root / rc.SWEEP_RECORD
        rc.save_plan(plan, written_plan())
        rc.save_plan(record, {"schema_version": rc.RECORD_SCHEMA_VERSION, "repositories": [], "policy": []})

        def not_utf_8(seal: Path) -> None:
            seal.write_bytes(b"\xff\xfe" + seal.read_bytes())

        def a_directory(seal: Path) -> None:
            seal.unlink()
            seal.mkdir()

        readers: list[tuple[Path, Callable[[], object], str]] = [
            (plan, lambda: rc.load_plan(plan), "plan"),
            (record, lambda: rc.read_record(self.root), "sweep record"),
        ]
        spoilers: dict[str, Callable[[Path], None]] = {"not UTF-8": not_utf_8, "a directory": a_directory}
        for name, spoil in spoilers.items():
            for path, read, what in readers:
                with self.subTest(name, what=what):
                    seal = rc.seal_file(path)
                    spoil(seal)
                    with self.assertRaises(rc.CleanupError) as raised:
                        read()
                    self.assertTrue(
                        str(raised.exception).startswith(f"cannot read the seal of {what} {path}: "), raised.exception
                    )
                    if seal.is_dir():
                        seal.rmdir()
                    rc.save_plan(path, json.loads(path.read_bytes()))
                    read()

    def test_a_missing_seal_is_still_refused_as_a_file_the_script_never_saved(self) -> None:
        plan = self.root / "plan.json"
        rc.save_plan(plan, written_plan())
        rc.seal_file(plan).unlink()
        with self.assertRaises(rc.CleanupError) as raised:
            rc.load_plan(plan)
        self.assertTrue(str(raised.exception).startswith(f"{plan} {NOT_SAVED}"), raised.exception)

    def test_no_plan_or_record_is_saved_inside_the_seals_folder(self) -> None:
        for path in (rc.SEALS / "plan.json", rc.SEALS / "plans" / rc.SWEEP_RECORD):
            with self.subTest(path=path):
                with self.assertRaises(rc.CleanupError) as raised:
                    rc.save_plan(path, written_plan())
                self.assertEqual(
                    f"{path} is inside the seals folder {rc.SEALS}; run the sweep again for a new plan",
                    str(raised.exception),
                )
                self.assertFalse(path.exists())
        self.assertEqual([], list(rc.SEALS.iterdir()), "no seal was written either")
        rc.save_plan(self.root / "plan.json", written_plan())
        rc.load_plan(self.root / "plan.json")

    def test_a_record_naming_the_seals_folder_is_refused_before_a_sweep_writes_there(self) -> None:
        with self.assertRaises(rc.CleanupError) as raised:
            rc.recorded_location(str(rc.SEALS))
        self.assertEqual(
            f"{rc.SEALS} holds no sweep record; name the directory a sweep printed as PLANS", str(raised.exception)
        )


class PlanOutsideASweepTests(Fixture):
    def test_a_plan_built_outside_a_sweep_leaves_out_only_sync_and_stopped_and_is_read(self) -> None:
        git(self.clone, "branch", "scratch", "main")
        plan = rc.build_plan(str(self.clone), str(self.repos), rc.Services(gh=self.github))
        self.assertTrue({"sync", "stopped"}.isdisjoint(plan), plan)
        path = self.root / "plan.json"
        rc.save_plan(path, plan)
        self.assertEqual(plan, rc.load_plan(path))
        self.assertEqual("up to date", rc.default_notes(plan), "no sync, so nothing a sync did is noted")


class SweepSealTests(Fixture):
    def test_a_seal_that_is_not_utf_8_ends_one_repositorys_run_and_the_sweep_goes_on(self) -> None:
        git(self.root, "clone", "--quiet", str(self.remote), str(self.repos / "other repo"))
        saving = rc.save_plan

        def spoiled(path: str | Path, document: dict[str, Any]) -> None:
            saving(path, document)
            if Path(path).name == "my repo.json":
                rc.seal_file(path).write_bytes(b"\xff\xfe")

        with mock.patch.object(rc, "save_plan", spoiled):
            code, lines = self.invoke("sweep", "--repos-root", str(self.repos))
        repos = self.repos.as_posix()
        self.assertEqual(1, code, lines)
        self.assertEqual([[f"{repos}/my repo", "error"], [f"{repos}/other repo", "quiet"]], facts(lines, "REPO"))
        (error,) = facts(lines, "ERROR")
        self.assertTrue(error[0].startswith(f"cannot read the seal of plan {self.plan_file}: "), error)
        self.assertEqual([], facts(lines, "FAILED"), "the sweep finished and recorded both repositories")
        self.assertEqual(2, len(rc.read_record(self.plan_file.parent)[0]))
