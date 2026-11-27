"""A FIFO, socket, device, or other special file in a tree the deployer reads is refused by name and kind, before any
read, so the deployment never blocks opening one.

The FIFO tests run on Linux and macOS only: Windows has no FIFO a folder can hold, its named pipes living in their own
namespace, and its counterpart special file, the AF_UNIX socket a folder can hold, is a reparse point, which the
deployer refuses as a link before this check is reached.
"""

from __future__ import annotations

import contextlib
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import override
from unittest import mock

import file_lock
from harness import DeployerTestCase, forward

from deployer import hashing, lock
from deployer.errors import DeployError

POSIX_ONLY = "Windows has no FIFO a folder can hold; its AF_UNIX socket file is a reparse point, refused as a link"
# How often the releaser opens each FIFO's writing end, which ends a read blocked opening it.
RELEASE_INTERVAL = 0.2


@contextlib.contextmanager
def released(*fifos: Path) -> Iterator[set[Path]]:
    """While the block runs, open and close each FIFO's writing end every RELEASE_INTERVAL seconds, so a reader that
    opens one gets its end of file at once rather than blocking: code that opens a FIFO fails the test's assertions,
    never hangs it. A nonblocking open of a writing end succeeds only while a reader holds the FIFO open, so each FIFO
    it opens is added to the set it gives, which the block's code never opened while it stays empty."""
    done = threading.Event()
    opened: set[Path] = set()

    def release() -> None:
        while not done.wait(RELEASE_INTERVAL):
            for fifo in fifos:
                with contextlib.suppress(OSError):
                    os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))  # type: ignore[attr-defined, unused-ignore]  # POSIX only
                    opened.add(fifo)

    releaser = threading.Thread(target=release, daemon=True)
    releaser.start()
    try:
        yield opened
    finally:
        done.set()
        releaser.join()


def make_fifo(path: Path) -> None:
    """Make a FIFO at `path`; only the tests that skip Windows call it."""
    os.mkfifo(path)  # type: ignore[attr-defined, unused-ignore]  # POSIX only: Windows has no os.mkfifo


def mode(kind: int) -> os.stat_result:
    """A stat result whose mode is `kind` with read and write for its owner, and every other field zero."""
    return os.stat_result((kind | 0o600, 0, 0, 0, 0, 0, 0, 0, 0, 0))


class SpecialKindTests(unittest.TestCase):
    """hashing.special_kind names each kind a stat mode gives; this holds on every platform, from modes alone."""

    def test_each_special_kind_is_named_and_a_file_folder_or_link_is_none(self) -> None:
        self.assertEqual("FIFO (named pipe)", hashing.special_kind(mode(stat.S_IFIFO)))
        self.assertEqual("socket", hashing.special_kind(mode(stat.S_IFSOCK)))
        self.assertEqual("character device", hashing.special_kind(mode(stat.S_IFCHR)))
        self.assertEqual("block device", hashing.special_kind(mode(stat.S_IFBLK)))
        self.assertEqual("special file", hashing.special_kind(mode(0o160000)))
        for ordinary in (stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK):
            with self.subTest(mode=oct(ordinary)):
                self.assertIsNone(hashing.special_kind(mode(ordinary)))


@unittest.skipIf(sys.platform == "win32", POSIX_ONLY)
class TreeReadTests(unittest.TestCase):
    """read_tree and hash_path refuse a FIFO by its path and kind, and never open it."""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="special-files."))).resolve()
        self.tree = self.root / "tree with spaces"
        (self.tree / "scripts").mkdir(parents=True)
        (self.tree / "notes.txt").write_bytes(b"x\n")

    def refused(self, fifo: Path, read: Path) -> DeployError:
        """The refusal of reading `read`, hash_path's of the FIFO itself or read_tree's of a tree, once `fifo` is
        made; the FIFO is removed whatever the read does, so a read that opened it leaves no FIFO behind."""
        make_fifo(fifo)
        try:
            with released(fifo) as opened, self.assertRaises(DeployError) as caught:
                hashing.hash_path(read) if read == fifo else hashing.read_tree(read)
        finally:
            fifo.unlink()
        self.assertEqual(set(), opened)
        return caught.exception

    def test_a_fifo_in_a_tree_is_refused_by_path_and_kind(self) -> None:
        for relative in ("plainfifo", "scripts/run.py"):
            with self.subTest(relative=relative):
                fifo = self.tree / relative
                refusal = self.refused(fifo, self.tree)
                self.assertEqual(
                    (
                        f"ERROR: Refusing to hash content containing a FIFO (named pipe): {forward(fifo)}",
                        "Remove it, or move it out of the tree, then rerun with --dry-run.",
                    ),
                    refusal.lines,
                )

    def test_a_fifo_named_as_the_sidecar_of_a_listed_file_is_refused_never_opened_to_read_a_header(self) -> None:
        fifo = self.tree / "._notes.txt"
        refusal = self.refused(fifo, self.tree)
        self.assertEqual(
            f"ERROR: Refusing to hash content containing a FIFO (named pipe): {forward(fifo)}", refusal.lines[0]
        )

    def test_hash_path_of_a_fifo_or_a_tree_holding_one_is_refused(self) -> None:
        fifo = self.root / "alone"
        refusal = self.refused(fifo, fifo)
        self.assertEqual(
            f"ERROR: Refusing to hash content containing a FIFO (named pipe): {forward(fifo)}", refusal.lines[0]
        )
        pipe = self.tree / "pipe"
        make_fifo(pipe)
        try:
            with released(pipe), self.assertRaises(DeployError):
                hashing.hash_path(self.tree)
        finally:
            pipe.unlink()

    def test_a_regular_file_is_still_read_and_hash_path_of_a_missing_file_raises_its_error(self) -> None:
        self.assertEqual({"notes.txt": b"x\n"}, hashing.read_tree(self.tree))
        self.assertEqual(hashing.file_hash(b"x\n"), hashing.hash_path(self.tree / "notes.txt"))
        with self.assertRaises(FileNotFoundError):
            hashing.hash_path(self.tree / "missing")


@unittest.skipIf(sys.platform == "win32", POSIX_ONLY)
class DeploymentTests(DeployerTestCase):
    """A deployment, and its dry run, refuse a FIFO in the source or in an installed copy before any change."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.make_source_json(shared_assets={"shared.md": "owner"})
        self.make_agent("reviewer")
        self.make_skill("alpha", "Alpha", agent_deps=["reviewer"], shared_deps=["shared.md"])
        self.make_shared_asset("shared.md")
        self.make_config()

    def test_a_fifo_anywhere_the_source_is_read_is_refused_by_path_and_kind_before_any_change(self) -> None:
        for relative in (
            "skills/alpha/plainfifo",
            "skills/alpha/._SKILL.md",
            "skills/alpha/scripts/run.py",
            "deploy-meta/notes.json",
            "agents/notes.md",
        ):
            fifo = self.source / relative
            fifo.parent.mkdir(exist_ok=True)
            make_fifo(fifo)
            try:
                for arguments in (("--dry-run",), ()):
                    with self.subTest(relative=relative, arguments=arguments), released(fifo) as opened:
                        result = self.deploy_fails("--all", *arguments, pattern="FIFO")
                        self.assertEqual(set(), opened)
                        self.assertIn(
                            f"ERROR: FIFO (named pipe) found in deployable source tree: {forward(fifo)}\n"
                            "Remove it, or move it out of the checkout, then retry.\n",
                            result.output,
                        )
                        self.assertFalse(self.manifest_file.exists())
            finally:
                fifo.unlink()

    def test_a_shared_asset_that_is_a_fifo_is_refused_before_any_change(self) -> None:
        asset = self.source / "skills" / "shared.md"
        asset.unlink()
        make_fifo(asset)
        with released(asset):
            result = self.deploy_fails("--all", pattern="FIFO")
        self.assertIn(f"ERROR: FIFO (named pipe) found in deployable source tree: {forward(asset)}\n", result.output)

    def test_a_fifo_planted_in_an_installed_copy_is_refused_before_any_change(self) -> None:
        self.deploy_ok("--all")
        fifo = self.skills_dir / "alpha" / "pipe"
        make_fifo(fifo)
        for arguments in (("--dry-run",), ()):
            with self.subTest(arguments=arguments), released(fifo) as opened:
                result = self.deploy_fails("--all", *arguments, pattern="FIFO")
                self.assertEqual(set(), opened)
                self.assertIn(
                    f"ERROR: Refusing to hash content containing a FIFO (named pipe): {forward(fifo)}\n", result.output
                )

    def test_a_fifo_where_a_held_locks_record_belongs_names_no_holder_and_is_never_opened(self) -> None:
        # The record of a lock another process holds is read only to name its holder, so a FIFO there names none.
        self.paths.deployer_dir.mkdir(parents=True, exist_ok=True)
        handle = file_lock.take(self.paths.lock_file)
        assert handle is not None
        self.addCleanup(file_lock.release, handle, self.paths.lock_file)
        self.lock_dir.mkdir()
        fifo = self.lock_dir / "info.json"
        make_fifo(fifo)
        with mock.patch("deployer.lock.LOCK_WAIT_SECONDS", 0), released(fifo) as opened:
            result = self.deploy_fails("--all", pattern="Another deployment is running")
        self.assertEqual(set(), opened)
        self.assertIn("ERROR: Another deployment is running.\nWait for it to finish, then retry.\n", result.output)

    def test_a_fifo_at_the_lock_file_is_refused_by_a_dry_run_and_a_deployment_and_kept(self) -> None:
        # Each asks whether another process holds the file lock before it takes it, which opened the file to try it.
        # A lock the file system refuses on a FIFO, as macOS does, read as a file system with no lock, deleted it.
        self.paths.deployer_dir.mkdir(parents=True, exist_ok=True)
        fifo = self.paths.lock_file
        make_fifo(fifo)
        for arguments in (("--all", "--dry-run"), ("--all",)):
            with self.subTest(arguments=arguments), released(fifo) as opened:
                result = self.deploy_fails(*arguments, pattern="not a regular file")
                self.assertEqual(set(), opened)
                self.assertIn(
                    f"ERROR: Could not open the deployment lock: {forward(fifo)}: "
                    "it is not a regular file, so its lock is not tried\n",
                    result.output,
                )
                self.assertTrue(stat.S_ISFIFO(fifo.stat(follow_symlinks=False).st_mode))
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_a_fifo_at_the_lock_record_is_refused_by_a_dry_run_and_a_deployment_and_kept(self) -> None:
        # Older than the grace a record with no metadata gets, so a judgment that took it for a record would remove it.
        self.paths.deployer_dir.mkdir(parents=True, exist_ok=True)
        fifo = self.lock_dir
        make_fifo(fifo)
        then = time.time() - lock.RECORD_GRACE_SECONDS - 540
        os.utime(fifo, (then, then))
        for arguments in (("--all", "--dry-run"), ("--all",)):
            with self.subTest(arguments=arguments), released(fifo) as opened:
                result = self.deploy_fails(*arguments, pattern="lock's record")
                self.assertEqual(set(), opened)
                self.assertIn(
                    f"ERROR: The deployment lock's record {forward(fifo)} is not a folder.\n"
                    "Move it out of the way once you know what it is, then retry.\n",
                    result.output,
                )
                self.assertTrue(stat.S_ISFIFO(fifo.stat(follow_symlinks=False).st_mode))
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_a_link_to_nothing_at_the_lock_file_is_refused_by_a_dry_run_as_by_the_deployment(self) -> None:
        # The dry run asks whether another process holds the file lock, whose stat and open find nothing through a
        # link to nothing; the deployment's open refuses that link, so the dry run refuses it with the same line.
        self.paths.deployer_dir.mkdir(parents=True, exist_ok=True)
        target = self.paths.deployer_dir / "nowhere"
        self.paths.lock_file.symlink_to(target)
        for arguments in (("--all", "--dry-run"), ("--all",)):
            with self.subTest(arguments=arguments):
                result = self.deploy_fails(*arguments, pattern="symbolic link to nothing")
                self.assertIn(
                    f"ERROR: Could not open the deployment lock: {forward(self.paths.lock_file)}: "
                    "a symbolic link to nothing is in the lock file's place; remove it\n",
                    result.output,
                )
                self.assertTrue(self.paths.lock_file.is_symlink())
                self.assertFalse(os.path.lexists(target))
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_a_fifo_in_the_place_of_a_held_locks_token_is_never_opened_and_its_record_stays(self) -> None:
        # A token that is not the file this run wrote is not this run's, so the release leaves the record.
        held = lock.acquire(self.paths)
        token = self.lock_dir / "token"
        token.unlink()
        make_fifo(token)
        with released(token) as opened:
            held.release()
        self.assertEqual(set(), opened)
        self.assertTrue(self.lock_dir.is_dir())
