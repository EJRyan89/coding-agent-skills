"""code-review-core's file and path helpers: ordered parallel calls, working paths, atomic writes."""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import review_io
import skill_roots
from atomic_file import UnfinishedCopyError
from review_io import PersistenceError, map_in_order, working_path


class ParallelCallTests(unittest.TestCase):
    def test_results_keep_item_order_and_at_most_four_run_at_once(self) -> None:
        active = peak = 0
        guard = threading.Lock()

        def call(item: int) -> int:
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05 * (10 - item))  # later items finish first
            with guard:
                active -= 1
            return item * 10

        self.assertEqual([(item * 10, None) for item in range(10)], map_in_order(call, range(10)))
        self.assertEqual(4, peak)

    def test_caught_errors_stay_with_their_item(self) -> None:
        def call(item: int) -> int:
            if item == 1:
                raise ValueError("item one failed")
            return item

        outcomes = map_in_order(call, [0, 1, 2], catch=(ValueError,))
        self.assertEqual([(0, None), (2, None)], [outcomes[0], outcomes[2]])
        self.assertEqual((None, "item one failed"), (outcomes[1][0], str(outcomes[1][1])))

    def test_fatal_and_uncaught_errors_stop_the_rest(self) -> None:
        for catch, fatal in (((ValueError,), lambda error: True), ((), lambda error: False)):
            started: list[int] = []
            release = threading.Event()

            def call(item: int, *, started: list[int] = started, release: threading.Event = release) -> int:
                started.append(item)
                if item == 1:
                    raise ValueError("stop")
                release.wait(timeout=0.5)
                return item

            with self.subTest(catch=catch), self.assertRaisesRegex(ValueError, "stop"):
                map_in_order(call, range(12), catch=catch, fatal=fatal)
            self.assertLess(len(started), 12, "calls not yet started are cancelled")

    def test_one_item_runs_in_the_calling_thread(self) -> None:
        caller = threading.get_ident()
        self.assertEqual([(caller, None)], map_in_order(lambda item: threading.get_ident(), ["only"]))
        self.assertEqual([(caller, None)] * 2, map_in_order(lambda item: threading.get_ident(), "ab", workers=1))


class WorkingPathTests(unittest.TestCase):
    """working_path refuses a path in the skills directories skill-core names, and keeps any other path."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.own = self.root / "source skills"
        self.deployed = self.root / "home" / ".claude" / "skills"
        self.adapters = self.root / "home" / ".agents" / "skills"
        for patcher in (
            mock.patch.object(skill_roots, "SKILLS_ROOT", self.own),
            mock.patch.object(skill_roots, "deployed_skill_roots", return_value=(self.deployed, self.adapters)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_path_inside_a_skills_directory_is_refused_naming_it(self) -> None:
        for root in (self.own, self.deployed, self.adapters):
            target = root / "review-prs" / "batch.json"
            with self.subTest(root=root), self.assertRaises(PersistenceError) as raised:
                working_path(target, "review-prs-batch-", "batch.json")
            self.assertEqual(
                f"{target} is inside the skills directory {root}; omit the option to write under a new temporary "
                "directory",
                str(raised.exception),
            )
            self.assertFalse(root.exists())

    def test_a_path_beside_the_skills_directories_is_kept(self) -> None:
        target = self.root / "home" / ".claude" / "batch.json"
        self.assertEqual(target, working_path(target, "review-prs-batch-", "batch.json"))

    def test_no_path_gives_the_name_in_a_new_temporary_directory(self) -> None:
        path = working_path(None, "review-prs-batch-", "batch.json")
        self.addCleanup(shutil.rmtree, path.parent)
        self.assertEqual("batch.json", path.name)
        self.assertTrue(path.parent.name.startswith("review-prs-batch-"), path)
        self.assertEqual([], list(path.parent.iterdir()))


class AtomicWriteTests(unittest.TestCase):
    """review_io's atomic writes are skill-core's replace, whose own suite covers the write; here each failure is
    the PersistenceError the review steps report."""

    def test_a_failed_replace_is_a_persistence_error_and_leaves_the_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_bytes(b"previous\n")
            with (
                mock.patch.object(Path, "replace", side_effect=OSError("synthetic replace")),
                self.assertRaisesRegex(
                    PersistenceError, f"^Cannot atomically replace {re.escape(str(path))}: synthetic replace$"
                ) as raised,
            ):
                review_io.atomic_write_text(path, "next\n")
            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertEqual(b"previous\n", path.read_bytes())
            self.assertEqual(["state.json"], sorted(item.name for item in Path(temporary).iterdir()))

    def test_bytes_that_are_not_utf8_replace_the_file_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "condition.txt"
            path.write_bytes(b"previous\n")
            review_io.atomic_write_bytes(path, b"caf\xc3\xa9 \xff\xc3")
            self.assertEqual(b"caf\xc3\xa9 \xff\xc3", path.read_bytes())
            self.assertEqual(["condition.txt"], sorted(item.name for item in Path(temporary).iterdir()))

    def test_a_folder_that_cannot_be_made_or_text_utf8_cannot_encode_fails_before_a_copy_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            blocker = Path(temporary) / "blocker"
            blocker.write_bytes(b"a file where the folder would go\n")
            path = Path(temporary) / "state.json"
            path.write_bytes(b"previous\n")
            for name, target, content, cause in (
                ("mkdir", blocker / "inner" / "state.json", "next\n", OSError),
                ("lone surrogate", path, "next \ud800\n", UnicodeEncodeError),
            ):
                with (
                    self.subTest(failure=name),
                    self.assertRaisesRegex(
                        PersistenceError, f"^Cannot atomically replace {re.escape(str(target))}: "
                    ) as raised,
                ):
                    review_io.atomic_write_text(target, content)
                self.assertIsInstance(raised.exception.__cause__, cause)
            self.assertEqual(b"previous\n", path.read_bytes())
            self.assertEqual(["blocker", "state.json"], sorted(item.name for item in Path(temporary).iterdir()))

    def test_a_copy_that_cannot_be_removed_is_named_in_a_persistence_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_bytes(b"previous\n")
            with (
                mock.patch.object(Path, "replace", side_effect=OSError("synthetic replace")),
                mock.patch.object(Path, "unlink", side_effect=OSError("synthetic unlink")),
                self.assertRaises(PersistenceError) as raised,
            ):
                review_io.atomic_write_text(path, "next\n")
            copies = [item for item in Path(temporary).iterdir() if item.name != "state.json"]
            self.assertEqual(1, len(copies), "the copy stays where the error says")
            self.assertEqual(
                f"Cannot remove {copies[0]}, the unfinished copy of {path}: synthetic unlink", str(raised.exception)
            )
            self.assertIsInstance(raised.exception.__cause__, UnfinishedCopyError)
            self.assertEqual(b"previous\n", path.read_bytes())
