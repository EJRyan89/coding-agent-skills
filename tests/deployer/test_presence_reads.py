"""Regression tests for the deployer's readers that ask whether a path is there: a stat that fails is refused or
raised, never read as nothing being there."""

from __future__ import annotations

import contextlib
import functools
import io
import os
import shutil
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import file_presence
from harness import SOURCE_ID, DeployerTestCase, backup_entry, forward, install_entry, journal_line

from deployer import (
    fsops,
    hashing,
    journal,
    paths,
    pipeline,
    platform_support,
    report,
    source,
    source_commit,
    source_ref,
    verify,
)
from deployer.errors import DeployError
from deployer.kinds import AGENT_KIND, SKILL
from deployer.names import BACKUP_SUFFIX, transient_name
from deployer.render import CODEX_METADATA

COMMIT = "0123456789abcdef0123456789abcdef01234567"


def denied(path: Path) -> PermissionError:
    return PermissionError(13, "Access is denied", str(path))


@contextlib.contextmanager
def stat_failing_at(*failing: Path) -> Iterator[None]:
    """A stat of each given path that fails, as a denied one does: Path.stat raises PermissionError, through links or
    not, and each presence predicate answers False for it, as every one does for a failed stat from Python 3.14 on, so
    a reader that still asks one reads the path as absent. Every other path is answered as it is."""
    real_stat = Path.stat

    def stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self in failing:
            raise denied(self)
        return real_stat(self, follow_symlinks=follow_symlinks)

    def method(real: Callable[..., bool]) -> Callable[..., bool]:
        return lambda self, *rest, **options: False if self in failing else real(self, *rest, **options)

    def function(real: Callable[..., bool]) -> Callable[..., bool]:
        return lambda name, *rest: False if Path(name) in failing else real(name, *rest)

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(Path, "stat", autospec=True, side_effect=stat))
        for name in ("exists", "is_file", "is_dir"):
            real = getattr(Path, name)
            stack.enter_context(mock.patch.object(Path, name, autospec=True, side_effect=method(real)))
        for name in ("exists", "lexists", "isfile", "isdir"):
            real = getattr(os.path, name)
            stack.enter_context(mock.patch.object(os.path, name, side_effect=function(real)))
        yield


class PresenceReadTests(DeployerTestCase):
    def git_file(self, git_dir: Path) -> Path:
        """A source whose .git file names git_dir, as a linked worktree's does."""
        git_dir.mkdir(parents=True, exist_ok=True)
        marker = self.source / ".git"
        marker.write_text(f"gitdir: {forward(git_dir)}\n", encoding="utf-8")
        return marker

    def bash_beside_git(self, root: Path) -> mock._patch[mock.MagicMock | mock.AsyncMock]:
        """Patch the locator and the git run so git reports its exec path under root, as Git for Windows does."""
        exec_path = root / "mingw64" / "libexec" / "git-core"
        result = SimpleNamespace(returncode=0, output=f"{exec_path}\n")
        return mock.patch.multiple(
            platform_support,
            find_executable=mock.DEFAULT,
            run_tool=mock.Mock(return_value=result),
        )

    def readers(self) -> list[tuple[str, Path, Callable[[], object]]]:
        """Each reader, the path whose stat the row fails, and the call that reads it, after making what it reads."""
        tree = self.root / "tree"
        tree.mkdir()
        target = self.root / "target"
        target.write_text("content", encoding="utf-8")
        self.paths.staging_root.mkdir(parents=True)
        self.paths.agents_src.mkdir(parents=True, exist_ok=True)
        bash_root = self.root / "Git"
        return [
            ("hashing.find_link", tree, lambda: hashing.find_link(tree)),
            ("journal._hash_existing", target, lambda: journal._hash_existing(target)),
            ("journal.pending_runs", self.paths.staging_root, lambda: journal.pending_runs(self.paths)),
            ("source._discover_agents", self.paths.agents_src, lambda: source._discover_agents(self.paths)),
            ("source.is_linked_worktree", self.source / ".git", lambda: source.is_linked_worktree(self.source)),
            (
                "source_ref.is_release_pin",
                self.source / ".git",
                lambda: source_ref.is_release_pin(self.source, "refs/tags/v1.0.0", COMMIT),
            ),
            (
                "platform_support._bash_beside_git",
                bash_root / "bin" / "bash.exe",
                lambda: self.with_bash_beside_git(bash_root),
            ),
        ]

    def with_bash_beside_git(self, root: Path) -> str | None:
        with self.bash_beside_git(root):
            return platform_support._bash_beside_git()

    def test_a_path_whose_stat_fails_is_never_read_as_nothing_there(self) -> None:
        for name, path, read in self.readers():
            with self.subTest(reader=name):
                with stat_failing_at(path), self.assertRaises(Exception) as raised:
                    read()
                failure = raised.exception
                if isinstance(failure, DeployError):
                    self.assertIn(f"{forward(path)}: Access is denied", "\n".join(failure.lines))
                elif isinstance(failure, source_ref.TagsUnreadable):
                    self.assertEqual(f"{path}: Access is denied", str(failure))
                elif isinstance(failure, PermissionError):
                    self.assertEqual(str(path), failure.filename)
                else:
                    self.fail(f"{name} raised {failure!r}, not the stat's PermissionError")

    def test_the_refusals_name_what_could_not_be_read(self) -> None:
        self.paths.agents_src.mkdir(parents=True, exist_ok=True)
        cases: list[tuple[Path, Callable[[], object], str]] = [
            (self.paths.agents_src, lambda: source._discover_agents(self.paths), "read the agents folder"),
            (self.source / ".git", lambda: source.is_linked_worktree(self.source), "read the source's .git file"),
        ]
        for path, read, action in cases:
            with self.subTest(action=action):
                with stat_failing_at(path), self.assertRaises(DeployError) as raised:
                    read()
                self.assertEqual(
                    f"ERROR: Could not {action}: {forward(path)}: Access is denied", raised.exception.lines[0]
                )

    def test_a_staging_root_that_cannot_be_stat_ed_reports_no_runs_rather_than_none_committed(self) -> None:
        self.paths.staging_root.mkdir(parents=True)
        with (
            mock.patch.object(journal, "_committed_run_id", return_value="20260101-000000-done"),
            stat_failing_at(self.paths.staging_root),
        ):
            self.assertIsNone(journal.journaled_runs(self.paths))
        with mock.patch.object(journal, "_committed_run_id", return_value="20260101-000000-done"):
            self.assertEqual([], journal.journaled_runs(self.paths))

    def test_a_run_whose_journal_cannot_be_stat_ed_is_pending_for_the_user(self) -> None:
        run = self.staging_run("20260101-000000-held")
        run.mkdir(parents=True, exist_ok=True)
        empty = self.staging_run("20260101-000000-none")
        empty.mkdir(parents=True, exist_ok=True)
        # A later run is still visited after the held one.
        later = self.staging_run("20260101-000000-torn")
        later.mkdir(parents=True, exist_ok=True)
        (later / "journal.jsonl").write_text("not a journal\n", encoding="utf-8")
        with stat_failing_at(run / "journal.jsonl"):
            self.assertEqual(
                [("20260101-000000-held", False), ("20260101-000000-torn", False)], journal.pending_runs(self.paths)
            )

    def test_a_pin_that_is_not_a_release_tag_or_has_no_commit_is_the_users_before_the_checkout_is_read(self) -> None:
        # Neither the .git nor the tags are read for a pin whose form already names its owner.
        with stat_failing_at(self.source / ".git"):
            self.assertFalse(source_ref.is_release_pin(self.source, "v1.0.0", COMMIT))
            self.assertFalse(source_ref.is_release_pin(self.source, "refs/tags/v1.0.0", None))
            self.assertFalse(source_ref.is_release_pin(self.source, None, COMMIT))

    def test_a_missing_staging_root_or_agents_folder_is_nothing_there(self) -> None:
        self.assertEqual([], journal.pending_runs(self.paths))
        self.assertEqual({}, source._discover_agents(self.paths))
        self.assertIsNone(journal._hash_existing(self.root / "missing"))
        self.assertIsNone(hashing.find_link(self.root / "missing"))

    def test_a_file_where_a_folder_is_read_is_refused_or_holds_no_link(self) -> None:
        self.paths.agents_src.parent.mkdir(parents=True, exist_ok=True)
        self.paths.agents_src.write_text("", encoding="utf-8")
        with self.assertRaises(DeployError) as raised:
            source._discover_agents(self.paths)
        self.assertEqual(
            f"ERROR: agents is not a directory: {forward(self.paths.agents_src)}", raised.exception.lines[0]
        )
        self.assertIsNone(hashing.find_link(self.paths.agents_src))

    def test_a_git_file_that_cannot_be_read_is_refused_not_read_as_no_worktree(self) -> None:
        marker = self.git_file(self.root / "git" / "worktrees" / "task")
        with self.unreadable(marker), self.assertRaises(DeployError) as raised:
            source.is_linked_worktree(self.source)
        self.assertIn("Could not read the source's .git file", raised.exception.lines[0])
        marker.write_bytes(b"gitdir: \xff\n")
        with self.assertRaises(DeployError) as raised:
            source.is_linked_worktree(self.source)
        self.assertEqual(
            f"ERROR: Could not read the source's .git file: {forward(marker)}: it is not UTF-8 text",
            raised.exception.lines[0],
        )

    def test_a_common_directory_that_cannot_be_stat_ed_is_refused_and_a_git_folder_is_no_worktree(self) -> None:
        git_dir = self.root / "git" / "worktrees" / "task"
        self.git_file(git_dir)
        (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
        self.assertTrue(source.is_linked_worktree(self.source))
        with stat_failing_at(git_dir / "commondir"), self.assertRaises(DeployError) as raised:
            source.is_linked_worktree(self.source)
        self.assertEqual(
            f"ERROR: Could not read the git directory the source's .git file names: {forward(git_dir / 'commondir')}: "
            "Access is denied",
            raised.exception.lines[0],
        )
        (self.source / ".git").unlink()
        (self.source / ".git").mkdir()
        self.assertFalse(source.is_linked_worktree(self.source))

    def test_a_folder_where_the_bash_beside_git_stands_is_no_bash(self) -> None:
        root = self.root / "Git"
        bash = root / "bin" / "bash.exe"
        self.assertIsNone(self.with_bash_beside_git(root))
        bash.mkdir(parents=True)
        self.assertIsNone(self.with_bash_beside_git(root))
        bash.rmdir()
        bash.write_text("", encoding="utf-8")
        self.assertEqual(str(bash), self.with_bash_beside_git(root))


class RecoveryPresenceTests(DeployerTestCase):
    """Recovery and the backup checks, each with the stat of the path it decides by failing: the run or step fails,
    and nothing is deleted or moved on a reading of absence."""

    RUN = "20260101-000000-held"

    def run_folder(self, *entries: dict[str, Any]) -> Path:
        run = self.staging_run(self.RUN)
        run.mkdir(parents=True, exist_ok=True)
        (run / "journal.jsonl").write_text("".join(journal_line(entry) for entry in entries), encoding="utf-8")
        return run

    def recovered(self) -> bool:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return journal.recover_incomplete(self.paths)

    def moved_aside(self, item: str) -> tuple[Path, dict[str, Any]]:
        """An item moved aside by an uncommitted run, as its backup entry records it."""
        transient = self.skills_dir / f"{item}{BACKUP_SUFFIX}"
        transient.mkdir(parents=True)
        (transient / "SKILL.md").write_text("previous", encoding="utf-8")
        return transient, backup_entry(item, hashing.hash_path(transient))

    def test_a_staging_root_or_run_folder_it_cannot_stat_fails_recovery_rather_than_finding_nothing(self) -> None:
        run = self.run_folder(install_entry("alpha", "0" * 64))
        for path in (self.paths.staging_root, run):
            with self.subTest(path=path.name), stat_failing_at(path):
                self.assertFalse(self.recovered())
            self.assertTrue(run.is_dir())

    def test_a_run_whose_journal_it_cannot_stat_is_kept_not_deleted(self) -> None:
        run = self.run_folder(install_entry("alpha", "0" * 64))
        with stat_failing_at(run / "journal.jsonl"):
            self.assertFalse(self.recovered())
            self.assertEqual([(self.RUN, False)], journal.pending_runs(self.paths))
        self.assertTrue((run / "journal.jsonl").is_file())

    def test_journaled_runs_cannot_say_which_runs_remain_when_a_journal_cannot_be_stat_ed(self) -> None:
        run = self.run_folder(install_entry("alpha", "0" * 64))
        with (
            mock.patch.object(journal, "_committed_run_id", return_value=self.RUN),
            stat_failing_at(run / "journal.jsonl"),
        ):
            self.assertIsNone(journal.journaled_runs(self.paths))

    def test_a_rollback_step_whose_target_it_cannot_stat_fails_and_moves_nothing(self) -> None:
        def restore(item: str, entry: dict[str, Any]) -> bool:
            return journal._restore_backup(self.paths, cast(Any, entry))

        def undo_install(item: str, entry: dict[str, Any]) -> bool:
            installed = install_entry(item, "0" * 64)
            return journal._undo_install(self.paths, cast(Any, installed), entry["backup_hash"])

        for item, step in (("alpha", restore), ("beta", undo_install)):
            transient, entry = self.moved_aside(item)
            target = self.skills_dir / item
            with self.subTest(step=step.__name__), stat_failing_at(target), contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(journal._guarded(self.RUN, target, functools.partial(step, item, entry)))
            self.assertTrue(transient.is_dir())
            self.assertFalse(target.exists())

    def test_undoing_a_preserve_whose_kept_copy_it_cannot_stat_fails_the_step(self) -> None:
        preserve = {"op": "preserve", "item": "alpha"}
        destination = self.skills_dir / ".backups" / self.RUN / "alpha"
        self.skills_dir.mkdir(parents=True, exist_ok=True)

        def step() -> bool:
            return journal._undo_preserve(self.paths, self.RUN, cast(Any, preserve))

        with stat_failing_at(destination), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(journal._guarded(self.RUN, destination, step))

    def test_keeping_a_backup_over_a_place_it_cannot_stat_keeps_the_copy_beside_its_item(self) -> None:
        transient, entry = self.moved_aside("alpha")
        destination = self.skills_dir / ".backups" / self.RUN / "alpha"
        with stat_failing_at(destination), contextlib.redirect_stderr(io.StringIO()) as warned:
            self.assertIsNone(journal.keep_backup(self.paths, self.RUN, cast(Any, entry)))
        self.assertTrue(transient.is_dir())
        self.assertIn("Access is denied", warned.getvalue())

    def test_a_backup_folder_it_cannot_stat_is_refused_before_the_first_change(self) -> None:
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        backups = self.skills_dir / ".backups"
        for path in (backups, backups / self.RUN):
            with self.subTest(path=path.name), stat_failing_at(path), self.assertRaises(PermissionError):
                journal.check_backup_root(self.paths, "claude", self.RUN)

    def test_a_source_whose_git_it_cannot_stat_is_refused_rather_than_recording_no_commit(self) -> None:
        with stat_failing_at(self.source / ".git"), self.assertRaises(DeployError) as raised:
            source_commit.is_checkout(self.source)
        self.assertEqual(
            f"ERROR: Could not tell whether the source is a Git checkout: {forward(self.source / '.git')}: "
            "Access is denied",
            raised.exception.lines[0],
        )


class PlanPresenceTests(DeployerTestCase):
    """The plan, with the stat of an item's destination failing: the deployment is refused before any change, and
    the manifest keeps what it owns, where the plan read the item as absent."""

    def deployed(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_skill("beta", "Beta")
        self.make_config()
        self.deploy_ok("--all")

    def test_an_owned_item_it_cannot_stat_is_refused_not_dropped_as_already_absent(self) -> None:
        self.deployed()
        self.remove_skill("alpha")
        # The link check before the plan is stubbed, so the plan is what meets the failed stat.
        with mock.patch.object(hashing, "find_link", return_value=None), stat_failing_at(self.skills_dir / "alpha"):
            self.deploy_fails("--all", pattern="Access is denied")
        self.assertIn("alpha", self.owned("skills"))
        self.assertTrue((self.skills_dir / "alpha").is_dir())

    def test_a_destination_it_cannot_stat_is_refused_not_planned_as_absent(self) -> None:
        self.deployed()
        self.make_skill("gamma", "Gamma")
        # The link check before the plan is stubbed, so the plan is what meets the failed stat.
        with mock.patch.object(hashing, "find_link", return_value=None), stat_failing_at(self.skills_dir / "gamma"):
            self.deploy_fails("--all", pattern="Access is denied")
        self.assertNotIn("gamma", self.owned("skills"))

    def test_has_expected_type_raises_for_a_path_it_cannot_stat_and_answers_otherwise(self) -> None:
        folder = self.root / "folder"
        folder.mkdir()
        file = self.root / "file"
        file.write_text("", encoding="utf-8")
        for kind in (SKILL, AGENT_KIND):
            with self.subTest(kind=kind.key):
                for path in (folder, file):
                    with stat_failing_at(path), self.assertRaises(PermissionError):
                        kind.has_expected_type(path)
                self.assertEqual(kind.directory, kind.has_expected_type(folder))
                self.assertEqual(not kind.directory, kind.has_expected_type(file))
                self.assertFalse(kind.has_expected_type(self.root / "missing"))

    def test_a_file_at_the_staging_root_fails_recovery_as_unexpected_state(self) -> None:
        self.paths.staging_root.parent.mkdir(parents=True, exist_ok=True)
        self.paths.staging_root.write_text("", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as printed, contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(journal.recover_incomplete(self.paths))
        self.assertIn("Cannot list the staging runs", printed.getvalue())


class DeployerPresenceTests(DeployerTestCase):
    """The deployer's other readers of whether a path is there: the source's skills, fsops' deletion and moves, the
    canary home's marker, verify's user-only markers, the Copilot shadow warning, the checkout a source records, and
    the backups a committed run keeps. A stat that fails is refused or named, and nothing is deleted, moved over,
    written over, or left unrecorded on a reading of absence."""

    def readers(self) -> list[tuple[str, Path, Callable[[], object]]]:
        """Each reader, the path whose stat the row fails, and the call that reads it, after making what it reads."""
        doomed = self.root / "doomed"
        taken = self.root / "taken"
        marker = self.root / "adapters" / "alpha" / CODEX_METADATA
        canary = self.root / "canary"
        marker.parent.mkdir(parents=True)
        canary.mkdir()
        for path in (doomed, taken, marker, canary / paths.CANARY_MARKER):
            path.write_text("kept", encoding="utf-8")

        def refuse_existing() -> None:
            with mock.patch.object(platform_support, "RENAME_REPLACES", True):
                platform_support.refuse_existing_destination(taken)

        return [
            ("fsops.remove", doomed, lambda: fsops.remove(doomed)),
            ("platform_support.refuse_existing_destination", taken, refuse_existing),
            ("verify.user_only", marker, lambda: verify.user_only(["alpha"], self.root / "adapters")),
            ("paths.claim_canary_home", canary / paths.CANARY_MARKER, lambda: paths.claim_canary_home(canary)),
        ]

    def test_a_path_whose_stat_fails_is_never_deleted_moved_over_or_written_over(self) -> None:
        for name, path, read in self.readers():
            with self.subTest(reader=name):
                with stat_failing_at(path), self.assertRaises(Exception) as raised:
                    read()
                failure = raised.exception
                if isinstance(failure, DeployError):
                    self.assertEqual(
                        f"ERROR: Could not read the --canary-home marker: {forward(path)}: Access is denied",
                        failure.lines[0],
                    )
                else:
                    self.assertIsInstance(failure, PermissionError)
                    self.assertEqual(str(path), cast(PermissionError, failure).filename)
                self.assertEqual("kept", path.read_text(encoding="utf-8"))

    def test_a_missing_path_is_still_nothing_there(self) -> None:
        missing = self.root / "missing"
        fsops.remove(missing)
        with mock.patch.object(platform_support, "RENAME_REPLACES", True):
            platform_support.refuse_existing_destination(missing)
        self.assertEqual([], verify.user_only(["alpha"], self.root / "adapters"))
        canary = self.root / "canary"
        canary.mkdir()
        paths.claim_canary_home(canary)
        self.assertEqual(paths.CANARY_MARKER_TEXT, (canary / paths.CANARY_MARKER).read_bytes())

    def test_something_other_than_a_file_where_a_file_is_read_is_passed_over(self) -> None:
        # A folder at alpha's marker makes no user-only adapter, and the next adapter is still read.
        adapters = self.root / "adapters"
        (adapters / "alpha" / CODEX_METADATA).mkdir(parents=True)
        (adapters / "beta" / CODEX_METADATA).parent.mkdir(parents=True)
        (adapters / "beta" / CODEX_METADATA).write_text("", encoding="utf-8")
        self.assertEqual(["beta"], verify.user_only(["alpha", "beta"], adapters))
        # A folder named SKILL.md is no skill.
        self.make_skill("alpha", "Alpha")
        (self.source / "skills" / "gamma" / "SKILL.md").mkdir(parents=True)
        self.assertEqual(["alpha"], sorted(source._discover_directories(self.paths)))

    def deployed(self) -> None:
        self.make_source_json()
        for name in ("alpha", "beta"):
            self.make_skill(name, name.title())
        self.make_config()
        self.deploy_ok("--all")

    def test_a_skill_whose_files_cannot_be_stat_ed_is_refused_never_dropped_from_the_source(self) -> None:
        # Were both passed over, beta would leave the source unnoticed, and the deployment would remove it. Which of
        # the two is refused first depends on how a Python's glob matches a literal name, so neither is named.
        self.deployed()
        skill_md = self.source / "skills" / "beta" / "SKILL.md"
        metadata = self.source / "deploy-meta" / "beta.json"
        with stat_failing_at(skill_md, metadata):
            result = self.deploy_fails("--all", pattern="Could not read the skill source: .*: Access is denied")
        self.assertNotIn("REMOVED", result.output)
        self.assertTrue((self.skills_dir / "beta" / "SKILL.md").is_file())

    def test_a_skill_md_whose_stat_fails_is_refused_not_passed_over(self) -> None:
        skill_md = self.source / "skills" / "beta" / "SKILL.md"
        self.deployed()
        with stat_failing_at(skill_md), self.assertRaises(PermissionError) as raised:
            source._discover_directories(self.paths)
        self.assertEqual(str(skill_md), raised.exception.filename)

    def test_a_skills_folder_it_cannot_list_is_refused_never_read_as_no_skill(self) -> None:
        # A glob matches nothing in a folder it cannot list, which would drop that skill from the source.
        self.deployed()
        unlisted = self.source / "skills" / "beta"
        real = os.scandir

        def scandir(path: Any = ".") -> Any:
            if isinstance(path, str | os.PathLike) and Path(path) == unlisted:
                raise denied(unlisted)
            return real(path)

        with mock.patch.object(os, "scandir", scandir), self.assertRaises(PermissionError) as raised:
            source._discover_directories(self.paths)
        self.assertEqual(str(unlisted), raised.exception.filename)

    def test_metadata_whose_stat_fails_is_refused_not_passed_over(self) -> None:
        self.deployed()
        orphan = self.source / "deploy-meta" / "gamma.json"
        orphan.write_text("{}", encoding="utf-8")
        self.deploy_fails("--all", pattern=r"deploy-meta/gamma\.json has no matching skill directory")
        with stat_failing_at(orphan):
            result = self.deploy_fails("--all", pattern="Could not read the skill source")
        self.assertIn(f"ERROR: Could not read the skill source: {forward(orphan)}: Access is denied\n", result.output)

    def test_a_metadata_folder_it_cannot_list_is_refused_never_read_as_no_skills(self) -> None:
        # The listing fails as a denied one does: iterdir raises, and Path.glob answers with no entries, which read as
        # the source's skills would remove every deployed one.
        self.deployed()
        meta = self.source / "deploy-meta"
        real_iterdir, real_glob = Path.iterdir, Path.glob

        def iterdir(self: Path) -> Iterator[Path]:
            if self == meta:
                raise denied(meta)
            return real_iterdir(self)

        def glob(self: Path, *arguments: Any, **options: Any) -> Iterator[Path]:
            return iter([]) if self == meta else real_glob(self, *arguments, **options)

        with mock.patch.object(Path, "iterdir", iterdir), mock.patch.object(Path, "glob", glob):
            result = self.deploy_fails("--all", pattern="Could not read the skill source")
        self.assertIn(f"ERROR: Could not read the skill source: {forward(meta)}: Access is denied\n", result.output)
        self.assertTrue((self.skills_dir / "alpha" / "SKILL.md").is_file())
        # The folder's *.json entries are listed in order, a file where the folder stands is refused, and no folder at
        # all is no metadata.
        (meta / "notes.txt").write_text("", encoding="utf-8")
        self.assertEqual([meta / "alpha.json", meta / "beta.json"], source._metadata_files(meta))
        shutil.rmtree(meta)
        self.assertEqual([], source._metadata_files(meta))
        meta.write_text("", encoding="utf-8")
        with self.assertRaises(file_presence.NotADirectory):
            source._metadata_files(meta)

    def test_a_managed_root_component_it_cannot_stat_is_refused_not_passed(self) -> None:
        component = self.home / ".claude"
        component.mkdir()
        with stat_failing_at(component), self.assertRaises(PermissionError) as raised:
            paths.validate_managed_roots(self.paths)
        self.assertEqual(str(component), raised.exception.filename)
        # A file there is still refused by name, and a missing one passes.
        component.rmdir()
        paths.validate_managed_roots(self.paths)
        component.write_text("", encoding="utf-8")
        with self.assertRaises(DeployError) as refused:
            paths.validate_managed_roots(self.paths)
        self.assertEqual(
            f"ERROR: Deployment path component is not a directory: {forward(component)}", refused.exception.lines[0]
        )

    def test_a_copilot_personal_skill_whose_stat_fails_is_warned_of_not_read_as_no_shadow(self) -> None:
        personal = self.paths.copilot_skills_dir
        for name in ("alpha", "beta", "delta", "epsilon"):
            (personal / name).mkdir(parents=True, exist_ok=True)
        unreadable = (personal / "alpha", personal / "delta")
        with stat_failing_at(*unreadable), contextlib.redirect_stderr(io.StringIO()) as warned:
            report.warn_copilot(self.paths, ["alpha", "beta", "gamma", "delta", "epsilon"])
        self.assertEqual(
            [
                "",
                "WARNING: Higher-priority GitHub Copilot personal skills shadow generated runtime adapters:",
                f"  - beta ({forward(personal / 'beta')})",
                f"  - epsilon ({forward(personal / 'epsilon')})",
                "WARNING: Could not tell whether GitHub Copilot personal skills shadow generated runtime adapters:",
                f"  - alpha ({forward(personal / 'alpha')}: Access is denied)",
                f"  - delta ({forward(personal / 'delta')}: Access is denied)",
            ],
            warned.getvalue().splitlines()[:7],
        )

    def test_the_checkout_a_source_was_deployed_from_is_named_unreadable_not_gone(self) -> None:
        self.deployed()
        other = self.root / "other checkout"
        other.mkdir()
        data = self.manifest()
        data["sources"][SOURCE_ID]["source_dir"] = forward(other)
        self.write_manifest(data)
        with stat_failing_at(other):
            result = self.deploy_fails("--all", pattern="not from this checkout")
        self.assertIn(
            f"ERROR: Source '{SOURCE_ID}' is deployed from {forward(other)}, which could not be read (Access is "
            f"denied), not from this checkout, {forward(self.source)}.\n",
            result.output,
        )
        other.rmdir()
        self.deploy_fails("--all", pattern="which no longer exists, not from this checkout")
        # A file where the checkout was is no checkout either.
        other.write_text("", encoding="utf-8")
        self.deploy_fails("--all", pattern="which no longer exists, not from this checkout")


class RecoveryEdgeTests(DeployerTestCase):
    """The answers the presence reads give when nothing failed, held so a narrowed or widened test is caught."""

    RUN = "20260101-000000-held"

    def test_a_file_where_a_root_or_a_run_folder_stands_is_no_folder_rather_than_an_error(self) -> None:
        self.skills_dir.parent.mkdir(parents=True, exist_ok=True)
        self.skills_dir.write_text("", encoding="utf-8")
        # No probe is made in a root that is no folder, and the destination is refused by name.
        journal.check_backup_root(self.paths, "claude", self.RUN)
        with self.assertRaises(DeployError) as raised:
            journal.prepare_backup_destination(self.paths, self.RUN, "alpha", "claude")
        self.assertEqual(
            f"ERROR: Deployment destination is missing: {forward(self.skills_dir)}", raised.exception.lines[0]
        )
        # A stray file among the runs is no run.
        self.paths.staging_root.mkdir(parents=True)
        (self.paths.staging_root / "stray.txt").write_text("", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(journal.recover_incomplete(self.paths))
        self.assertEqual([], journal.pending_runs(self.paths))

    def test_undoing_an_install_reads_an_earlier_restore_only_when_the_backup_is_gone(self) -> None:
        self.skills_dir.mkdir(parents=True)
        target = self.skills_dir / "alpha"
        target.mkdir()
        (target / "SKILL.md").write_text("restored", encoding="utf-8")
        restored = hashing.hash_path(target)
        installed = cast(Any, install_entry("alpha", "0" * 64))
        with contextlib.redirect_stderr(io.StringIO()):
            # The target holds neither the staged copy nor the replaced one, and no backup is beside it.
            self.assertFalse(journal._undo_install(self.paths, installed, "1" * 64))
            self.assertTrue(journal._undo_install(self.paths, installed, restored))
            # The replaced copy is back, but its backup is still beside it: not a finished restore.
            (self.skills_dir / f"alpha{BACKUP_SUFFIX}").mkdir()
            self.assertFalse(journal._undo_install(self.paths, installed, restored))

    def test_undoing_a_preserve_with_no_kept_copy_moves_nothing(self) -> None:
        self.skills_dir.mkdir(parents=True)
        preserve = cast(Any, {"op": "preserve", "item": "alpha"})
        self.assertTrue(journal._undo_preserve(self.paths, self.RUN, preserve))
        self.assertFalse((self.skills_dir / f"alpha{BACKUP_SUFFIX}").exists())


class TransientPresenceTests(DeployerTestCase):
    """The transient backup beside an item, with its stat failing: before the plan, the deployment is refused; after
    the commit, the copy is kept with a warning, never deleted or left unreported."""

    def updated(self) -> Path:
        """Deploy alpha, then change it in the source, so the next deployment replaces it and backs it up."""
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        self.deploy_ok("--all")
        self.make_skill("alpha", "Alpha, changed")
        return self.skills_dir / transient_name("alpha")

    def test_a_stale_backup_it_cannot_stat_refuses_the_plan(self) -> None:
        transient = self.updated()
        with stat_failing_at(transient):
            self.deploy_fails("--all", pattern="Access is denied")

    def test_a_backup_it_cannot_stat_after_the_commit_is_kept_with_a_warning(self) -> None:
        transient = self.updated()
        real = pipeline._finalize

        def finalize(*arguments: Any) -> list[tuple[str, str]]:
            with stat_failing_at(transient):
                return real(*arguments)

        with mock.patch.object(pipeline, "_finalize", side_effect=finalize):
            result = self.deploy_ok("--all")
        kept = list((self.skills_dir / ".backups").glob("*/alpha"))
        self.assertEqual(1, len(kept), result.output)
        self.assertNotIn("changed", (kept[0] / "SKILL.md").read_text(encoding="utf-8"))
        kept_at = kept[0].relative_to(self.skills_dir).as_posix()
        self.assertIn(
            f"WARNING: Could not tell whether alpha's previous copy changed after run {kept[0].parent.name} planned "
            f"it (Access is denied), so it was kept at {kept_at}.",
            result.stderr,
        )

    def test_a_committed_backup_is_deleted_kept_or_gone_with_the_reason_a_stat_failed(self) -> None:
        self.skills_dir.mkdir(parents=True)
        transient = self.skills_dir / transient_name("alpha")
        kept = cast(Any, backup_entry("alpha", "0" * 64, retain=True, run_id="20260101-000000-done"))
        unkept = cast(Any, backup_entry("alpha", "0" * 64))
        self.assertEqual(("gone", ""), journal.backup_disposition(self.paths, kept))
        self.assertEqual(("delete", ""), journal.backup_disposition(self.paths, unkept))
        transient.mkdir()
        self.assertEqual(("keep", ""), journal.backup_disposition(self.paths, kept))
        # An error with no reason of its own is named by its text.
        with mock.patch.object(journal, "backup_unchanged", side_effect=OSError("held")):
            self.assertEqual(("keep", "held"), journal.backup_disposition(self.paths, unkept))
