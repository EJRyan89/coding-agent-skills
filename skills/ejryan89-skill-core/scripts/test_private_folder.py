"""Regression tests for the folder a script keeps the user's own state in, in the runtime or temporary directory."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import private_folder
from private_folder import UnsafeFolderError, ancestry_refusal, base, ensure, location, parent_refusal, refusal
from test_link_probe import link_folder

# The modes and the id the rules are held to, written out rather than read from the module's constants.
OWNER_ONLY = 0o700
DIRECTORY = stat.S_IFDIR
OTHER_USER = 4242
# The variable naming the user's runtime directory, as the XDG Base Directory specification names it.
RUNTIME = "XDG_RUNTIME_DIR"


def metadata(mode: int, owner: int) -> os.stat_result:
    """A folder's own `lstat` result, as far as the check reads it."""
    return cast(os.stat_result, SimpleNamespace(st_mode=mode, st_uid=owner, st_file_attributes=0))


class LocationTests(unittest.TestCase):
    def test_the_folder_carries_the_user_s_id_where_the_platform_has_one(self) -> None:
        base = Path("base")
        with mock.patch.object(private_folder, "user_id", return_value=1000):
            self.assertEqual(base / "state-1000", location("state", base))
        with mock.patch.object(private_folder, "user_id", return_value=None):
            self.assertEqual(base / "state", location("state", base))

    @unittest.skipUnless(sys.platform == "win32", "only Windows has no user ids")
    def test_on_windows_there_is_no_user_id_and_the_folder_carries_none(self) -> None:
        self.assertIsNone(private_folder.user_id())
        with mock.patch.dict(os.environ, {RUNTIME: ""}):
            self.assertEqual(Path(tempfile.gettempdir()) / "state", location("state"))

    def test_the_folder_lies_in_the_temporary_directory_where_no_runtime_directory_is_set(self) -> None:
        with mock.patch.dict(os.environ, {RUNTIME: ""}):
            self.assertEqual(Path(tempfile.gettempdir()), base())
            self.assertEqual(Path(tempfile.gettempdir()), location("state").parent)
        with mock.patch.dict(os.environ):
            os.environ.pop(RUNTIME, None)
            self.assertEqual(Path(tempfile.gettempdir()), base())

    def test_where_the_platform_has_no_user_ids_a_runtime_directory_is_never_followed(self) -> None:
        # Windows has none, and ensure judges only a folder's kind there, so a runtime directory another account can
        # write, such as one under C:\ProgramData, would let it make the state folder first.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with (
            mock.patch.object(private_folder, "user_id", return_value=None),
            mock.patch.dict(os.environ, {RUNTIME: str(Path(temporary.name).absolute())}),
        ):
            self.assertIsNone(private_folder.runtime_directory())
            self.assertEqual(Path(tempfile.gettempdir()), base())
            self.assertEqual(Path(tempfile.gettempdir()) / "state", location("state"))


@unittest.skipIf(sys.platform == "win32", "Windows has no user ids, so its runtime directory is never read")
class RuntimeDirectoryTests(unittest.TestCase):
    """A runtime directory is followed only as the XDG Base Directory specification requires one to be: a directory,
    no link, the user owns with mode 0700, in a parent no other account can rename it out of; anything else leaves the
    state folder in the temporary directory."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=OWNER_ONLY)

    def base_for(self, value: Path | str) -> Path:
        with mock.patch.dict(os.environ, {RUNTIME: str(value)}):
            return base()

    def assert_not_followed(self, value: Path | str) -> None:
        self.assertEqual(Path(tempfile.gettempdir()), self.base_for(value))

    def test_a_directory_the_user_owns_with_mode_0700_is_followed(self) -> None:
        self.assertEqual(self.runtime, self.base_for(self.runtime))
        with mock.patch.dict(os.environ, {RUNTIME: str(self.runtime)}):
            self.assertEqual(self.runtime, location("state").parent)
            self.assertTrue(location("state").name.startswith("state"))

    def test_a_link_to_a_directory_of_the_user_s_own_is_not_followed(self) -> None:
        link = self.root / "link"
        link.symlink_to(self.runtime, target_is_directory=True)
        self.assert_not_followed(link)

    def test_a_directory_another_user_owns_is_not_followed(self) -> None:
        # The parent is this user's, which the parent check would judge another's under the id the test pretends.
        with (
            mock.patch.object(private_folder, "user_id", return_value=OTHER_USER),
            mock.patch.object(private_folder, "parent_refusal", return_value=None),
        ):
            self.assert_not_followed(self.runtime)

    def test_a_directory_of_any_mode_but_0700_is_not_followed(self) -> None:
        for mode in (0o755, 0o750, 0o711, 0o500, 0o770, 0o707, 0o777, 0o1700):
            with self.subTest(mode=oct(mode)):
                self.runtime.chmod(mode)
                self.assert_not_followed(self.runtime)
        self.runtime.chmod(OWNER_ONLY)
        self.assertEqual(self.runtime, self.base_for(self.runtime))

    def test_a_directory_in_a_parent_others_can_write_is_followed_only_when_the_parent_is_sticky(self) -> None:
        parent = self.root / "open"
        parent.mkdir()
        runtime = parent / "runtime"
        runtime.mkdir(mode=OWNER_ONLY)
        parent.chmod(0o777)
        self.assert_not_followed(runtime)
        parent.chmod(0o1777)
        self.assertEqual(runtime, self.base_for(runtime))

    def test_a_file_or_nothing_in_its_place_is_not_followed(self) -> None:
        file = self.root / "file"
        file.write_text("x", encoding="utf-8")
        file.chmod(OWNER_ONLY)
        self.assert_not_followed(file)
        self.assert_not_followed(self.root / "missing")

    def test_a_relative_one_is_never_resolved_against_the_working_directory(self) -> None:
        # The relative path leads to the followed directory from here, so following it would show.
        self.assert_not_followed(os.path.relpath(self.runtime))
        self.assert_not_followed(str(Path("run", "user")))

    def test_a_directory_below_a_shared_folder_further_up_than_its_parent_is_not_followed(self) -> None:
        # Whoever can rename the shared folder's entry can put a tree of their own, runtime directory and all, in its
        # place, so a parent of the user's own proves nothing alone.
        shared = self.root / "shared"
        shared.mkdir()
        user = shared / "u"
        user.mkdir()
        user.chmod(0o755)
        runtime = user / "rt"
        runtime.mkdir(mode=OWNER_ONLY)
        shared.chmod(0o777)
        self.assert_not_followed(runtime)
        shared.chmod(0o1777)
        self.assertEqual(runtime, self.base_for(runtime))

    def test_a_trailing_parent_reference_is_resolved_before_the_parent_is_judged(self) -> None:
        # `rt/inner/..` names rt, whose parent is the shared folder, not `rt/inner`, which is the user's own.
        shared = self.root / "shared"
        shared.mkdir()
        runtime = shared / "rt"
        runtime.mkdir(mode=OWNER_ONLY)
        (runtime / "inner").mkdir(mode=OWNER_ONLY)
        shared.chmod(0o777)
        self.assert_not_followed(f"{runtime}/inner/..")
        shared.chmod(0o1777)
        self.assertEqual(runtime, self.base_for(f"{runtime}/inner/.."), "the resolved folder is the one followed")

    def test_a_directory_named_through_a_linked_ancestor_is_followed_by_its_resolved_path(self) -> None:
        # The link lies in a folder of the user's own, so no other account can change where it leads, and every later
        # use names the resolved directory, so none goes through the link again.
        via = self.root / "via"
        via.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.runtime, self.base_for(via / "runtime"))


class RefusalTests(unittest.TestCase):
    """The rule, judged from a folder's metadata, so an owner no test can make is judged as a real one would be."""

    folder = Path("seals")

    def test_a_directory_the_user_owns_that_only_they_can_write_is_accepted(self) -> None:
        for mode in (OWNER_ONLY, 0o755, 0o500):
            with self.subTest(mode=oct(mode)):
                self.assertIsNone(refusal(self.folder, metadata(DIRECTORY | mode, 1000), 1000))

    def test_a_directory_another_user_owns_is_refused_naming_both_ids(self) -> None:
        self.assertEqual(
            f"{self.folder} is owned by user id {OTHER_USER}, not by this user (id 1000)",
            refusal(self.folder, metadata(DIRECTORY | OWNER_ONLY, OTHER_USER), 1000),
        )

    def test_a_directory_its_group_or_anyone_can_write_is_refused_naming_its_mode(self) -> None:
        for mode in (0o720, 0o702, 0o777, 0o1777):
            with self.subTest(mode=oct(mode)):
                self.assertEqual(
                    f"{self.folder} can be written by users other than its owner (mode {mode:o})",
                    refusal(self.folder, metadata(DIRECTORY | mode, 1000), 1000),
                )

    def test_a_link_or_a_file_is_refused_even_where_the_platform_has_no_user_ids(self) -> None:
        for user in (1000, None):
            with self.subTest(user=user):
                self.assertEqual(
                    f"{self.folder} is a link or junction, not a folder of this user's own",
                    refusal(self.folder, metadata(stat.S_IFLNK | 0o777, 1000), user),
                )
                self.assertEqual(
                    f"{self.folder} is not a directory",
                    refusal(self.folder, metadata(stat.S_IFREG | 0o600, 1000), user),
                )

    def test_with_no_user_ids_a_directory_s_owner_and_mode_are_not_judged(self) -> None:
        self.assertIsNone(refusal(self.folder, metadata(DIRECTORY | 0o777, OTHER_USER), None))


class ParentRefusalTests(unittest.TestCase):
    """A parent another account could remove or rename the folder out of, judged from its metadata."""

    parent = Path("tmp")

    def test_a_parent_root_or_the_user_owns_that_others_cannot_write_or_that_is_sticky_is_accepted(self) -> None:
        for owner, mode in ((0, 0o1777), (0, 0o755), (1000, 0o700), (1000, 0o1777), (0, 0o1770)):
            with self.subTest(owner=owner, mode=oct(mode)):
                self.assertIsNone(parent_refusal(self.parent, metadata(DIRECTORY | mode, owner), 1000))

    def test_a_parent_others_can_write_that_is_not_sticky_is_refused_naming_its_mode(self) -> None:
        for mode in (0o777, 0o770, 0o702):
            with self.subTest(mode=oct(mode)):
                self.assertEqual(
                    f"its parent {self.parent} can be written by users other than its owner and is not sticky "
                    f"(mode {mode:o})",
                    parent_refusal(self.parent, metadata(DIRECTORY | mode, 0), 1000),
                )

    def test_a_parent_another_user_owns_is_refused_even_when_only_they_can_write_it(self) -> None:
        self.assertEqual(
            f"its parent {self.parent} is owned by user id {OTHER_USER}, not by root or this user (id 1000)",
            parent_refusal(self.parent, metadata(DIRECTORY | 0o1755, OTHER_USER), 1000),
        )

    def test_with_no_user_ids_a_parent_is_not_judged(self) -> None:
        self.assertIsNone(parent_refusal(self.parent, metadata(DIRECTORY | 0o777, OTHER_USER), None))

    def test_a_folder_further_up_is_named_as_an_ancestor(self) -> None:
        self.assertEqual(
            f"its ancestor {self.parent} can be written by users other than its owner and is not sticky (mode 777)",
            parent_refusal(self.parent, metadata(DIRECTORY | 0o777, 0), 1000, "ancestor"),
        )


@unittest.skipIf(sys.platform == "win32", "a symbolic link needs a privilege there, and no ancestry is judged there")
class AncestryRefusalTests(unittest.TestCase):
    """Every folder from a resolved parent up to the root, of which a link is one that was put there since."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        user = private_folder.user_id()
        assert user is not None, "the class runs only where the platform has user ids"
        self.user = user

    def test_a_resolved_parent_below_folders_no_other_account_can_rename_is_accepted(self) -> None:
        self.assertIsNone(ancestry_refusal(self.root, self.user))

    def test_a_link_as_the_parent_or_further_up_is_refused(self) -> None:
        inner = self.root / "inner"
        inner.mkdir()
        via = self.root / "via"
        via.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(f"its parent {via} is a link or junction", ancestry_refusal(via, self.user))
        self.assertEqual(f"its ancestor {via} is a link or junction", ancestry_refusal(via / "inner", self.user))


class EnsureTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Resolved, as ensure hands back a folder whose parent is resolved, and macOS's temporary directory is a link.
        self.root = Path(temporary.name).resolve()

    def test_a_missing_folder_is_made_owner_only_and_returned(self) -> None:
        folder = self.root / "state"
        self.assertEqual(folder, ensure(folder))
        self.assertTrue(folder.is_dir())
        if private_folder.user_id() is not None:
            self.assertEqual(OWNER_ONLY, stat.S_IMODE(folder.stat().st_mode))
        self.assertEqual(folder, ensure(folder), "a folder already there that passes is used as it is")

    @unittest.skipIf(sys.platform == "win32", "Windows mode bits say nothing about who may write a folder")
    def test_a_folder_others_can_write_is_refused_and_left_as_it_is(self) -> None:
        folder = self.root / "state"
        folder.mkdir()
        folder.chmod(0o777)
        with self.assertRaises(UnsafeFolderError) as raised:
            ensure(folder)
        self.assertEqual(f"{folder} can be written by users other than its owner (mode 777)", str(raised.exception))
        self.assertEqual(0o777, stat.S_IMODE(folder.stat().st_mode))

    @unittest.skipIf(sys.platform == "win32", "Windows has no user ids; WindowsTests cover what holds there")
    def test_a_folder_another_user_owns_is_refused(self) -> None:
        folder = self.root / "state"
        folder.mkdir()
        # The parent is this user's, which the parent check would judge another's under the id the test pretends.
        with (
            mock.patch.object(private_folder, "user_id", return_value=OTHER_USER),
            mock.patch.object(private_folder, "parent_refusal", return_value=None),
            self.assertRaises(UnsafeFolderError) as raised,
        ):
            ensure(folder)
        self.assertTrue(str(raised.exception).startswith(f"{folder} is owned by user id "), raised.exception)

    @unittest.skipIf(sys.platform == "win32", "Windows has no user ids; WindowsTests cover what holds there")
    def test_a_folder_in_a_parent_another_user_owns_is_refused_before_anything_is_made(self) -> None:
        folder = self.root / "state"
        with (
            mock.patch.object(private_folder, "user_id", return_value=OTHER_USER),
            self.assertRaises(UnsafeFolderError) as raised,
        ):
            ensure(folder)
        self.assertTrue(
            str(raised.exception).startswith(f"{folder}: its parent {self.root} is owned by user id "),
            raised.exception,
        )
        self.assertFalse(folder.exists())

    @unittest.skipIf(sys.platform == "win32", "Windows mode bits say nothing about who may write a folder")
    def test_a_folder_in_a_parent_others_can_write_is_refused_unless_the_parent_is_sticky(self) -> None:
        parent = self.root / "open"
        parent.mkdir()
        parent.chmod(0o777)
        folder = parent / "state"
        with self.assertRaises(UnsafeFolderError) as raised:
            ensure(folder)
        self.assertEqual(
            f"{folder}: its parent {parent} can be written by users other than its owner and is not sticky (mode 777)",
            str(raised.exception),
        )
        self.assertFalse(folder.exists(), "nothing was made where another account could rename it away")
        parent.chmod(0o1777)
        self.assertEqual(folder, ensure(folder))

    @unittest.skipIf(sys.platform == "win32", "Windows mode bits say nothing about who may write a folder")
    def test_a_folder_below_a_shared_folder_further_up_than_its_parent_is_refused(self) -> None:
        # Whoever can rename the shared folder's entry can put a tree of their own in its place between the check and
        # a write, so a parent of the user's own proves nothing alone.
        shared = self.root / "shared"
        shared.mkdir()
        user = shared / "u"
        user.mkdir()
        user.chmod(0o755)
        shared.chmod(0o777)
        folder = user / "state"
        with self.assertRaises(UnsafeFolderError) as raised:
            ensure(folder)
        self.assertEqual(
            f"{folder}: its ancestor {shared} can be written by users other than its owner and is not sticky "
            "(mode 777)",
            str(raised.exception),
        )
        self.assertFalse(folder.exists(), "nothing was made where another account could rename it away")
        shared.chmod(0o1777)
        self.assertEqual(folder, ensure(folder))

    @unittest.skipIf(sys.platform == "win32", "Windows mode bits say nothing about who may write a folder")
    def test_a_parent_reference_is_resolved_before_the_parent_is_judged_and_the_resolved_folder_returned(self) -> None:
        # `u/inner/../state` lies in u, whose parent is the shared folder, though `u/inner` is the user's own.
        shared = self.root / "shared"
        shared.mkdir()
        user = shared / "u"
        user.mkdir(mode=OWNER_ONLY)
        (user / "inner").mkdir(mode=OWNER_ONLY)
        shared.chmod(0o777)
        named = user / "inner" / ".." / "state"
        with self.assertRaises(UnsafeFolderError) as raised:
            ensure(named)
        self.assertEqual(
            f"{named}: its ancestor {shared} can be written by users other than its owner and is not sticky (mode 777)",
            str(raised.exception),
        )
        shared.chmod(0o1777)
        self.assertEqual(user / "state", ensure(named))
        self.assertTrue((user / "state").is_dir())

    @unittest.skipIf(sys.platform == "win32", "Windows has no user ids; WindowsTests cover what holds there")
    def test_a_name_ending_in_a_parent_reference_or_at_the_root_is_refused(self) -> None:
        (self.root / "inner").mkdir()
        for named in (self.root / "inner" / "..", Path(self.root.anchor)):
            with self.subTest(named=str(named)), self.assertRaises(UnsafeFolderError) as raised:
                ensure(named)
            self.assertEqual(f"{named} does not end in the name of a folder", str(raised.exception))

    @unittest.skipIf(sys.platform == "win32", "a symbolic link needs a privilege there; refusal tests cover its kind")
    def test_a_link_to_a_folder_of_the_user_s_own_is_refused_and_nothing_is_made_through_it(self) -> None:
        target = self.root / "target"
        target.mkdir(mode=OWNER_ONLY)
        folder = self.root / "state"
        folder.symlink_to(target, target_is_directory=True)
        dangling = self.root / "dangling"
        dangling.symlink_to(self.root / "nowhere", target_is_directory=True)
        for link in (folder, dangling):
            with self.subTest(link=link.name), self.assertRaises(UnsafeFolderError) as raised:
                ensure(link)
            self.assertEqual(f"{link} is a link or junction, not a folder of this user's own", str(raised.exception))
        self.assertFalse((self.root / "nowhere").exists(), "no folder was made where the dangling link points")

    def test_a_file_in_the_folder_s_place_is_refused(self) -> None:
        folder = self.root / "state"
        folder.write_text("x", encoding="utf-8")
        with self.assertRaises(UnsafeFolderError) as raised:
            ensure(folder)
        self.assertEqual(f"{folder} is not a directory", str(raised.exception))

    def test_a_missing_parent_is_raised_rather_than_made(self) -> None:
        with self.assertRaises(FileNotFoundError):
            ensure(self.root / "missing" / "state")


class WindowsTests(unittest.TestCase):
    """What holds where the platform has no user ids, as on Windows, whose temporary directory lies under the user's
    profile, which other accounts cannot write: only the folder's kind is judged, so a link or junction and a file are
    refused, and a folder's mode bits and its parent's, which say nothing there, are not."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = mock.patch.object(private_folder, "user_id", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_folder_is_made_and_used_whatever_its_parent_s_mode_bits_say(self) -> None:
        folder = self.root / "state"
        self.assertEqual(folder, ensure(folder))
        self.assertTrue(folder.is_dir())
        self.assertEqual(folder, ensure(folder))

    def test_a_folder_is_returned_by_the_name_it_was_given(self) -> None:
        (self.root / "inner").mkdir()
        named = self.root / "inner" / ".." / "state"
        self.assertEqual(named, ensure(named))
        self.assertTrue((self.root / "state").is_dir())

    def test_a_link_or_junction_in_the_folder_s_place_is_refused_and_nothing_is_made_through_it(self) -> None:
        target = self.root / "target"
        target.mkdir()
        folder = self.root / "state"
        link_folder(target, folder)
        with self.assertRaises(UnsafeFolderError) as raised:
            ensure(folder)
        self.assertEqual(f"{folder} is a link or junction, not a folder of this user's own", str(raised.exception))
        self.assertEqual([], list(target.iterdir()))

    def test_a_file_in_the_folder_s_place_is_refused(self) -> None:
        folder = self.root / "state"
        folder.write_text("x", encoding="utf-8")
        with self.assertRaises(UnsafeFolderError) as raised:
            ensure(folder)
        self.assertEqual(f"{folder} is not a directory", str(raised.exception))
