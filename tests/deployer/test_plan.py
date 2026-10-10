"""One plan per item: the dry run prints it and the deployment carries it out, so the two cannot disagree."""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import shutil
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

from harness import DeployerTestCase

from deployer import hashing, manifest, migrate, plan, report, selection
from deployer.context import Context, Options, Selection
from deployer.errors import DeployError
from deployer.paths import Paths
from deployer.source import Skill, Source


@dataclass(frozen=True)
class Kind:
    label: str  # the kind as the report names it
    name: str  # the item's name in the report and for --force-item
    manifest_key: str
    root: Callable[[DeployerTestCase], Path]
    directory: bool


SKILL = Kind("", "alpha", "skills", lambda case: case.skills_dir, True)
SHARED = Kind("shared asset", "shared.md", "shared", lambda case: case.skills_dir, False)
ADAPTER = Kind("runtime adapter", "alpha", "wrappers", lambda case: case.agents_dir, True)
AGENT = Kind("agent", "reviewer.md", "agents", lambda case: case.claude_agents_dir, False)


@dataclass(frozen=True)
class Row:
    """One state of one kind, and what both runs must report for it."""

    state: str
    dry: str
    apply: str
    reason: str
    arguments: tuple[str, ...] = ("--all",)
    stdin: str = ""
    shown: bool = True  # False when the report folds a runtime adapter into its skill's line


DIFFERS_FROM_SKILL = "unmanaged and differs from the rendered skill"


def removal(state: str, dry: str, apply: str, reason: str, shown: bool = True) -> Row:
    """A row whose run selects nothing, so every owned item is up for removal."""
    return Row(state, dry, apply, reason, arguments=(), stdin="none\n", shown=shown)


# Selected items: the deployment installs, updates, adopts, replaces, or skips each one.
INSTALL_ROWS: dict[Kind, list[Row]] = {
    SKILL: [
        Row("fresh", "FRESH INSTALL", "INSTALLED", ""),
        Row("deleted by hand", "FRESH INSTALL", "INSTALLED", ""),
        Row("wrong type", "CONFLICT", "SKIPPED", "destination is not a directory"),
        Row("unchanged", "UNCHANGED", "UNCHANGED", ""),
        Row("source updated", "UPDATE", "UPDATED", ""),
        Row("modified", "CONFLICT", "SKIPPED", "modified since last deploy"),
        Row(
            "modified, forced item",
            "REPLACE",
            "REPLACED",
            "forced, previous copy backed up",
            ("--all", "--force-item", "alpha"),
        ),
        Row("modified, forced", "REPLACE", "REPLACED", "forced, previous copy backed up", ("--all", "--force")),
        Row("modified to the rendered copy", "CONFLICT", "SKIPPED", "modified since last deploy"),
        Row("unmanaged identical", "ADOPT", "ADOPTED", "byte-identical"),
        Row("unmanaged differs", "CONFLICT", "SKIPPED", DIFFERS_FROM_SKILL),
        Row(
            "unmanaged differs, forced item",
            "REPLACE",
            "REPLACED",
            "forced, was unmanaged, previous copy backed up",
            ("--all", "--force-item", "alpha"),
        ),
    ],
    SHARED: [
        Row("fresh", "FRESH INSTALL", "INSTALLED", ""),
        Row("deleted by hand", "FRESH INSTALL", "INSTALLED", ""),
        Row("wrong type", "CONFLICT", "SKIPPED", "destination is not a file"),
        Row("unchanged", "UNCHANGED", "UNCHANGED", ""),
        Row("source updated", "UPDATE", "UPDATED", ""),
        Row("modified", "CONFLICT", "SKIPPED", "modified since last deploy"),
        Row(
            "modified, forced item",
            "REPLACE",
            "REPLACED",
            "forced, previous copy backed up",
            ("--all", "--force-item", "shared.md"),
        ),
        Row("modified, forced", "REPLACE", "REPLACED", "forced, previous copy backed up", ("--all", "--force")),
        Row("modified to the rendered copy", "CONFLICT", "SKIPPED", "modified since last deploy"),
        Row("unmanaged identical", "ADOPT", "ADOPTED", "byte-identical"),
        Row("unmanaged differs", "CONFLICT", "SKIPPED", "unmanaged and differs"),
        Row(
            "unmanaged differs, forced item",
            "REPLACE",
            "REPLACED",
            "forced, was unmanaged, previous copy backed up",
            ("--all", "--force-item", "shared.md"),
        ),
    ],
    ADAPTER: [
        Row("fresh", "FRESH INSTALL", "INSTALLED", "", shown=False),
        Row("deleted by hand", "FRESH INSTALL", "INSTALLED", ""),
        Row("wrong type", "CONFLICT", "SKIPPED", "destination is not a directory"),
        Row("unchanged", "UNCHANGED", "UNCHANGED", "", shown=False),
        Row("source updated", "UPDATE", "UPDATED", "", shown=False),
        Row("modified", "CONFLICT", "SKIPPED", "modified since last deploy"),
        Row(
            "modified, forced item",
            "REPLACE",
            "REPLACED",
            "forced, previous copy backed up",
            ("--all", "--force-item", "alpha"),
        ),
        Row("modified, forced", "REPLACE", "REPLACED", "forced, previous copy backed up", ("--all", "--force")),
        Row("modified to the rendered copy", "CONFLICT", "SKIPPED", "modified since last deploy"),
        Row("unmanaged identical", "ADOPT", "ADOPTED", "byte-identical", shown=False),
        Row("unmanaged differs", "CONFLICT", "SKIPPED", "unmanaged and differs"),
        Row(
            "unmanaged differs, forced item",
            "REPLACE",
            "REPLACED",
            "forced, was unmanaged, previous copy backed up",
            ("--all", "--force-item", "alpha"),
        ),
        Row("skill skipped", "CONFLICT", "SKIPPED", "its skill was skipped", shown=False),
    ],
    AGENT: [
        Row("fresh", "FRESH INSTALL", "INSTALLED", ""),
        Row("deleted by hand", "FRESH INSTALL", "INSTALLED", ""),
        Row("wrong type", "CONFLICT", "SKIPPED", "destination is not a file"),
        Row("unchanged", "UNCHANGED", "UNCHANGED", ""),
        Row("source updated", "UPDATE", "UPDATED", ""),
        Row("modified", "CONFLICT", "SKIPPED", "modified since last deploy"),
        Row(
            "modified, forced item",
            "REPLACE",
            "REPLACED",
            "forced, previous copy backed up",
            ("--all", "--force-item", "reviewer.md"),
        ),
        Row("modified, forced", "REPLACE", "REPLACED", "forced, previous copy backed up", ("--all", "--force")),
        Row("modified to the rendered copy", "CONFLICT", "SKIPPED", "modified since last deploy"),
        Row("unmanaged identical", "ADOPT", "ADOPTED", "byte-identical"),
        Row("unmanaged differs", "CONFLICT", "SKIPPED", "unmanaged and differs"),
        Row(
            "unmanaged differs, forced item",
            "REPLACE",
            "REPLACED",
            "forced, was unmanaged, previous copy backed up",
            ("--all", "--force-item", "reviewer.md"),
        ),
    ],
}

# Owned items nothing selects any more: the deployment removes, preserves, keeps, or drops ownership of each one.
REMOVAL_ROWS: dict[Kind, list[Row]] = {
    SKILL: [
        removal("unchanged", "REMOVE", "REMOVED", "deselected or absent from source"),
        removal("modified", "PRESERVE", "PRESERVED", "modified since last deploy"),
        removal("wrong type", "PRESERVE", "PRESERVED", "destination is not a directory"),
        removal("deleted by hand", "DROP OWNERSHIP", "DROPPED OWNERSHIP", "already absent"),
    ],
    SHARED: [
        removal("unchanged", "REMOVE", "REMOVED", "obsolete"),
        removal("modified", "PRESERVE", "PRESERVED", "modified since last deploy"),
        removal("wrong type", "PRESERVE", "PRESERVED", "destination is not a file"),
        removal("deleted by hand", "DROP OWNERSHIP", "DROPPED OWNERSHIP", "already absent"),
        removal("needed by a preserved skill", "KEEP", "KEPT", "needed by alpha"),
    ],
    ADAPTER: [
        removal("unchanged", "REMOVE", "REMOVED", "obsolete", shown=False),
        removal("modified", "PRESERVE", "PRESERVED", "modified since last deploy"),
        removal("wrong type", "PRESERVE", "PRESERVED", "destination is not a directory"),
        removal("deleted by hand", "DROP OWNERSHIP", "DROPPED OWNERSHIP", "already absent"),
    ],
    AGENT: [
        removal("unchanged", "REMOVE", "REMOVED", "no selected skill needs it"),
        removal("modified", "PRESERVE", "PRESERVED", "modified since last deploy"),
        removal("wrong type", "PRESERVE", "PRESERVED", "destination is not a file"),
        removal("deleted by hand", "DROP OWNERSHIP", "DROPPED OWNERSHIP", "already absent"),
    ],
}

INSTALLS = frozenset({"INSTALL", "UPDATE", "UNCHANGED", "ADOPT", "REPLACE"})
LEAVES = frozenset({"SKIP", "PRESERVE", "KEEP"})
RELEASES = frozenset({"REMOVE", "DROP"})


class PlanTests(DeployerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_source_json(shared_assets={"shared.md": "owner"})
        self.make_shared_asset("shared.md")
        self.make_agent("reviewer")
        self.make_alpha()
        self.make_config()

    def make_alpha(self, content: str = "Alpha", description: str | None = None) -> None:
        self.make_skill("alpha", content, shared_deps=["shared.md"], agent_deps=["reviewer"], description=description)

    def path(self, kind: Kind) -> Path:
        return kind.root(self) / kind.name

    def modify(self, kind: Kind) -> None:
        self.append(self.path(kind) / "SKILL.md" if kind.directory else self.path(kind), "local edit\n")

    def delete(self, kind: Kind) -> None:
        path = self.path(kind)
        if kind.directory:
            shutil.rmtree(path)
        else:
            path.unlink()

    def make_wrong_type(self, kind: Kind) -> None:
        self.delete(kind)
        self.write(self.path(kind) / "stray.txt" if not kind.directory else self.path(kind), "not the item\n")

    def update_source(self, kind: Kind) -> None:
        if kind is SKILL:
            self.make_alpha("Alpha, revised")
        elif kind is ADAPTER:
            self.make_alpha(description='"Test skill alpha, revised"')
        elif kind is SHARED:
            self.make_shared_asset("shared.md", "# Revised shared asset")
        else:
            self.make_agent("reviewer", "Review the change, revised.")

    def install_with_old_copy(self, kind: Kind) -> None:
        """Own a revised copy of the item while the home holds the copy this source renders now."""
        self.deploy_ok("--all")
        old = self.root / "old-copy"
        kept_source = self.root / "kept-source"
        (shutil.copytree if kind.directory else shutil.copy2)(self.path(kind), old)
        shutil.copytree(self.source, kept_source)
        self.update_source(kind)
        self.deploy_ok("--all")
        shutil.rmtree(self.source)
        shutil.move(kept_source, self.source)
        self.delete(kind)
        shutil.move(old, self.path(kind))

    def arrange(self, kind: Kind, state: str) -> None:
        """Build the home for one state of the item; any other item may be in any state."""
        if state == "fresh":
            return
        if state in ("needed by a preserved skill", "skill skipped"):
            self.deploy_ok("--all")
            self.modify(SKILL)
            return
        if state == "modified to the rendered copy":
            self.install_with_old_copy(kind)
            return
        self.deploy_ok("--all")
        if state.startswith("unmanaged"):
            self.manifest_file.unlink()
        if state == "deleted by hand":
            self.delete(kind)
        elif state == "wrong type":
            self.make_wrong_type(kind)
        elif state == "source updated":
            self.update_source(kind)
        elif state.startswith(("modified", "unmanaged differs")):
            self.modify(kind)

    def owned_entry(self, kind: Kind) -> Any:
        if not self.manifest_file.exists():
            return None
        return self.owned(kind.manifest_key).get(kind.name)

    def fingerprint(self, kind: Kind) -> str | None:
        path = self.path(kind)
        return hashing.hash_path(path) if os.path.lexists(path) else None

    def planned_run(self, row: Row, *extra: str) -> tuple[list[plan.PlanEntry], str]:
        captured: list[list[plan.PlanEntry]] = []
        real = plan.build

        def spy(*arguments: Any, **keywords: Any) -> list[plan.PlanEntry]:
            entries = real(*arguments, **keywords)
            captured.append(entries)
            return entries

        with mock.patch.object(plan, "build", side_effect=spy):
            output = self.deploy_ok(*row.arguments, *extra, stdin=row.stdin).output
        self.assertEqual(1, len(captured), output)
        return captured[0], output

    def entry(self, entries: list[plan.PlanEntry], kind: Kind) -> plan.PlanEntry:
        matches = [entry for entry in entries if entry.name == kind.name and entry.kind == kind.label]
        self.assertEqual(1, len(matches), entries)
        return matches[0]

    def assert_reported(self, output: str, title: str, label: str, kind: Kind, row: Row) -> None:
        detail = ", ".join(part for part in (kind.label, row.reason) if part)
        line = f"{kind.name} ({detail})" if detail else kind.name
        groups = self.report_groups(output, title)
        if row.shown:
            self.assertIn(line, groups.get(label, []), output)
        else:
            self.assertFalse(any(line in group for group in groups.values()), output)

    def assert_filesystem(self, kind: Kind, entry: plan.PlanEntry, before: str | None, owned: Any) -> None:
        path = self.path(kind)
        if entry.action in INSTALLS:
            self.assertEqual(entry.staged, hashing.hash_path(path))
            self.assertEqual(entry.staged, self.owned_entry(kind)["hash"])
            backups = list((kind.root(self) / ".backups").glob(f"*/{kind.name}"))
            if entry.action == "REPLACE":
                self.assertEqual([before], [hashing.hash_path(backup) for backup in backups])
            else:
                self.assertEqual([], backups)
        elif entry.action in LEAVES:
            self.assertEqual(before, self.fingerprint(kind))
            self.assertEqual(owned, self.owned_entry(kind))
        else:
            self.assertIn(entry.action, RELEASES)
            self.assertFalse(os.path.lexists(path))
            self.assertIsNone(self.owned_entry(kind))

    def check(self, kind: Kind, row: Row) -> None:
        self.arrange(kind, row.state)
        dry_entries, dry_output = self.planned_run(row, "--dry-run")
        planned = self.entry(dry_entries, kind)
        self.assertEqual((row.dry, row.apply), report.ACTION_LABELS[planned.action])
        self.assertEqual(row.reason, planned.reason)
        self.assert_reported(dry_output, "DRY RUN", row.dry, kind, row)
        before, owned = self.fingerprint(kind), self.owned_entry(kind)
        applied_entries, applied_output = self.planned_run(row)
        self.assertEqual(planned, self.entry(applied_entries, kind), "the deployment planned what the dry run showed")
        self.assert_reported(applied_output, "DEPLOYED", row.apply, kind, row)
        self.assert_filesystem(kind, planned, before, owned)

    def check_rows(self, rows: dict[Kind, list[Row]], kind: Kind) -> None:
        for row in rows[kind]:
            with self.subTest(kind=kind.label or "skill", state=row.state):
                self.tearDown()
                self.setUp()
                self.check(kind, row)

    def test_skill_states(self) -> None:
        self.check_rows(INSTALL_ROWS, SKILL)

    def test_shared_asset_states(self) -> None:
        self.check_rows(INSTALL_ROWS, SHARED)

    def test_runtime_adapter_states(self) -> None:
        self.check_rows(INSTALL_ROWS, ADAPTER)

    def test_agent_states(self) -> None:
        self.check_rows(INSTALL_ROWS, AGENT)

    def test_removal_states(self) -> None:
        for kind in (SKILL, SHARED, ADAPTER, AGENT):
            self.check_rows(REMOVAL_ROWS, kind)

    def test_every_action_has_one_label_in_each_report(self) -> None:
        self.assertEqual(
            {
                "INSTALL": ("FRESH INSTALL", "INSTALLED"),
                "UPDATE": ("UPDATE", "UPDATED"),
                "UNCHANGED": ("UNCHANGED", "UNCHANGED"),
                "ADOPT": ("ADOPT", "ADOPTED"),
                "REPLACE": ("REPLACE", "REPLACED"),
                "SKIP": ("CONFLICT", "SKIPPED"),
                "REMOVE": ("REMOVE", "REMOVED"),
                "PRESERVE": ("PRESERVE", "PRESERVED"),
                "DROP": ("DROP OWNERSHIP", "DROPPED OWNERSHIP"),
                "KEEP": ("KEEP", "KEPT"),
            },
            report.ACTION_LABELS,
        )


OWN_SOURCE = "test/skills"
OTHER_SOURCE = "other/skills"
DEPENDENCY_BYTES = b"shared by another source\n"
DEPENDENCY_HASH = "sha256:" + hashlib.sha256(DEPENDENCY_BYTES).hexdigest()


class ValidateSharedAssetsTests(unittest.TestCase):
    """plan.validate_shared_assets called directly: the mapping it returns, and each refusal's exact message in the
    order its checks run."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="shared-assets.")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = Paths(self.root / "source", self.root / "home")
        self.paths.skills_src.mkdir(parents=True)
        self.paths.dest_dir.mkdir(parents=True)

    def shown(self, path: Path) -> str:
        return os.fspath(path).replace("\\", "/")

    def context(
        self,
        assets: dict[str, str],
        skills: dict[str, list[str]],
        owners: dict[str, str] | None = None,
        sources: dict[str, Any] | None = None,
    ) -> Context:
        src = Source(OWN_SOURCE, shared_assets=assets)
        for name, shared in skills.items():
            src.skills[name] = Skill(name, self.paths.skills_src / name, [], shared, [], True)
        data = manifest.Manifest(self.paths.manifest_file, {"sources": sources or {}})
        data.owners["shared"].update(owners or {})
        return Context(self.paths, Options(), src, {}, data, data.ownership(OWN_SOURCE), io.StringIO())

    def owned_by_other(self, entry: Any) -> dict[str, Any]:
        """A manifest in which the other source records d.md with entry."""
        return {"owners": {"d.md": OTHER_SOURCE}, "sources": {OTHER_SOURCE: {"shared": {"d.md": entry}}}}

    def installed_dependency(self) -> None:
        (self.paths.dest_dir / "d.md").write_bytes(DEPENDENCY_BYTES)

    def refusal(self, context: Context, selected: list[str]) -> str:
        with self.assertRaises(DeployError) as caught:
            plan.validate_shared_assets(context, selected)
        return str(caught.exception)

    def test_valid_assets_are_returned_in_source_order(self) -> None:
        (self.paths.skills_src / "o.md").write_text("owned\n", encoding="utf-8")
        self.installed_dependency()
        context = self.context(
            {"o.md": "owner", "d.md": "dependency", "unused.md": "dependency"},
            {"alpha": ["d.md", "o.md"], "beta": ["unused.md"]},
            **self.owned_by_other({"role": "owner", "hash": DEPENDENCY_HASH}),
        )
        result = plan.validate_shared_assets(context, ["alpha"])
        self.assertEqual([("o.md", "owner"), ("d.md", "dependency"), ("unused.md", "dependency")], list(result.items()))
        self.assertEqual({}, plan.validate_shared_assets(self.context({}, {"alpha": []}), ["alpha"]))

    def test_an_unsafe_asset_name_is_refused_in_source_order(self) -> None:
        context = self.context({"ok.md": "owner", "bad/x.md": "bogus", ".hidden": "owner"}, {})
        self.assertEqual("ERROR: shared asset name 'bad/x.md' contains path separator", self.refusal(context, []))

    def test_a_collision_with_a_skill_is_refused_before_any_role_check(self) -> None:
        context = self.context({"zeta": "owner", "a.md": "bogus", "beta": "owner"}, {"beta": [], "zeta": []})
        self.assertEqual("ERROR: Shared asset 'beta' collides with a skill of the same name", self.refusal(context, []))

    def test_roles_are_checked_in_name_order(self) -> None:
        context = self.context({"b.md": "owned", "a.md": "Owner"}, {})
        self.assertEqual(
            "ERROR: Shared asset 'a.md' has invalid role 'Owner' (must be 'owner' or 'dependency')",
            self.refusal(context, []),
        )

    def test_an_owned_asset_must_be_a_file_in_the_source(self) -> None:
        missing = self.paths.skills_src / "o.md"
        message = f"ERROR: source.json declares 'o.md' as owned but file not found at {self.shown(missing)}"
        self.assertEqual(message, self.refusal(self.context({"o.md": "owner"}, {}), []))
        missing.mkdir()
        self.assertEqual(message, self.refusal(self.context({"o.md": "owner"}, {}), []))

    def test_a_dependency_no_selected_skill_needs_is_not_checked(self) -> None:
        context = self.context({"d.md": "dependency"}, {"alpha": [], "beta": ["d.md"]})
        self.assertEqual({"d.md": "dependency"}, plan.validate_shared_assets(context, ["alpha"]))

    def test_a_needed_dependency_must_be_a_file_at_the_destination(self) -> None:
        destination = self.paths.dest_dir / "d.md"
        message = f"ERROR: Dependency 'd.md' not found at destination ({self.shown(destination)})"
        context = self.context({"d.md": "dependency"}, {"alpha": ["d.md"]})
        self.assertEqual(message, self.refusal(context, ["alpha"]))
        destination.mkdir()
        self.assertEqual(message, self.refusal(context, ["alpha"]))

    def test_a_needed_dependency_needs_another_source_that_owns_it_with_a_matching_hash(self) -> None:
        self.installed_dependency()
        good = {"role": "owner", "hash": DEPENDENCY_HASH}
        cases: list[tuple[dict[str, Any], str]] = [
            ({}, "ERROR: Dependency 'd.md' has no owner in the manifest"),
            (
                {"owners": {"d.md": OWN_SOURCE}, "sources": {OWN_SOURCE: {"shared": {"d.md": good}}}},
                "ERROR: Dependency 'd.md' is declared as both dependency and owned by this source",
            ),
            (
                {"owners": {"d.md": OTHER_SOURCE}, "sources": {}},
                f"ERROR: Dependency 'd.md' owner '{OTHER_SOURCE}' does not have role 'owner' (has '')",
            ),
            (
                self.owned_by_other("sha256:" + "0" * 64),
                f"ERROR: Dependency 'd.md' owner '{OTHER_SOURCE}' does not have role 'owner' (has '')",
            ),
            (
                self.owned_by_other({"hash": DEPENDENCY_HASH}),
                f"ERROR: Dependency 'd.md' owner '{OTHER_SOURCE}' does not have role 'owner' (has '')",
            ),
            (
                self.owned_by_other({"role": "dependency", "hash": DEPENDENCY_HASH}),
                f"ERROR: Dependency 'd.md' owner '{OTHER_SOURCE}' does not have role 'owner' (has 'dependency')",
            ),
            *(
                (
                    self.owned_by_other({"role": "owner", **hashed}),
                    f"ERROR: Dependency 'd.md' owner '{OTHER_SOURCE}' has no valid SHA-256 hash in manifest",
                )
                for hashed in ({}, {"hash": 7}, {"hash": "0" * 64}, {"hash": "sha256:" + "A" * 64})
            ),
            (
                self.owned_by_other({"role": "owner", "hash": "sha256:" + "0" * 64}),
                "ERROR: Dependency 'd.md' at destination does not match owner's manifest hash",
            ),
        ]
        for recorded, message in cases:
            with self.subTest(message=message, recorded=recorded):
                context = self.context({"d.md": "dependency"}, {"alpha": ["d.md"]}, **recorded)
                self.assertEqual(message, self.refusal(context, ["alpha"]))
        context = self.context({"d.md": "dependency"}, {"alpha": ["d.md"]}, **self.owned_by_other(good))
        self.assertEqual({"d.md": "dependency"}, plan.validate_shared_assets(context, ["alpha"]))

    def test_assets_are_checked_one_at_a_time_in_name_order(self) -> None:
        context = self.context({"b.md": "owner", "a.md": "dependency"}, {"alpha": ["a.md"]})
        self.assertEqual(
            f"ERROR: Dependency 'a.md' not found at destination ({self.shown(self.paths.dest_dir / 'a.md')})",
            self.refusal(context, ["alpha"]),
        )
        context = self.context({"b.md": "dependency", "a.md": "owner"}, {"alpha": ["b.md"]})
        self.assertEqual(
            "ERROR: source.json declares 'a.md' as owned but file not found at "
            f"{self.shown(self.paths.skills_src / 'a.md')}",
            self.refusal(context, ["alpha"]),
        )

    def test_a_shared_dependency_the_source_does_not_declare_is_refused_last_in_selection_order(self) -> None:
        context = self.context({}, {"alpha": ["x.md"], "beta": ["y.md"], "gamma": ["z.md"]})
        self.assertEqual(
            "ERROR: Skill 'beta' declares shared_dep 'y.md' but it is not in source.json shared_assets",
            self.refusal(context, ["beta", "alpha"]),
        )
        context = self.context({"o.md": "owner"}, {"alpha": ["x.md"]})
        self.assertEqual(
            "ERROR: source.json declares 'o.md' as owned but file not found at "
            f"{self.shown(self.paths.skills_src / 'o.md')}",
            self.refusal(context, ["alpha"]),
        )


class ContextStepTests(unittest.TestCase):
    """The selection and migration steps pipeline.py runs, called directly on a Context."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="context-steps.")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = Paths(self.root / "source", self.root / "home")

    def context(self, skills: dict[str, list[str]], config: dict[str, str] | None = None) -> Context:
        src = Source(OWN_SOURCE)
        for name, required in skills.items():
            src.skills[name] = Skill(name, self.paths.skills_src / name, required, [], [], True)
        data = manifest.Manifest(self.paths.manifest_file, {"sources": {}})
        return Context(self.paths, Options(), src, config or {}, data, data.ownership(OWN_SOURCE), io.StringIO())

    def test_a_selection_needing_unset_variables_names_each_one_and_the_skills_that_need_it(self) -> None:
        context = self.context({"alpha": ["REPOS_ROOT"], "beta": ["REPOS_ROOT", "HOME"], "gamma": []})
        with self.assertRaises(DeployError) as raised:
            selection.require_variables(context, ["alpha", "beta", "gamma"])
        self.assertEqual(
            [
                "ERROR: Selected skills require variables not set in config: REPOS_ROOT",
                "Skills requiring these:",
                "  alpha -> REPOS_ROOT",
                "  beta -> REPOS_ROOT",
                "Run 'python deploy.py configure' to set them.",
            ],
            list(raised.exception.lines),
        )
        # HOME is derived, never configured, and a set variable needs nothing.
        selection.require_variables(self.context({"beta": ["REPOS_ROOT", "HOME"]}, {"REPOS_ROOT": "C:/r"}), ["beta"])

    def test_selecting_nothing_says_what_will_be_removed(self) -> None:
        context = self.context({"alpha": []})
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            selection.print_selection(context.source, Selection(deselect_all=True), [])
        self.assertEqual(
            "\nSelected nothing; unmodified items this source deployed will be removed.\n", printed.getvalue()
        )

    def test_migration_needs_a_manifest_that_records_the_old_source(self) -> None:
        context = self.context({"alpha": []})
        with self.assertRaisesRegex(DeployError, "Cannot migrate without an existing manifest"):
            migrate.migrate(context, "old/source")
        self.paths.manifest_file.parent.mkdir(parents=True)
        context.manifest.save()
        with self.assertRaisesRegex(DeployError, "Migration source 'old/source' is not present in the manifest"):
            migrate.migrate(context, "old/source")


if __name__ == "__main__":
    unittest.main()
