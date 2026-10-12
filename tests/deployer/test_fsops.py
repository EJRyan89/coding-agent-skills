"""The filesystem operations every deployer change goes through, on their own."""

from __future__ import annotations

import io
import os
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

from git_test_base import GitTestCase

from deployer import fsops, platform_support


class FilesystemOperationTests(GitTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="deploy-fsops.")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def read_only(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(stat.S_IREAD)
        self.addCleanup(self.writable_again, path)
        return path

    def writable_again(self, path: Path) -> None:
        """Restore write access if a test fails, so the temporary directory can still be cleaned up."""
        if path.exists():
            path.chmod(stat.S_IREAD | stat.S_IWRITE)

    def tree(self, path: Path) -> dict[str, str]:
        """Each file under path, or path itself, with its text."""
        files = [path] if path.is_file() else sorted(path.rglob("*"))
        return {item.relative_to(self.root).as_posix(): item.read_text(encoding="utf-8") for item in files}

    def held(self, *paths: Path) -> dict[str, dict[str, str]]:
        return {path.name: self.tree(path) for path in paths}

    def move_targets(self) -> list[tuple[str, Path, Path]]:
        """A source and an existing destination for each shape a move meets: file onto file, directory onto a
        directory with contents, directory onto an empty one, and file onto a directory."""
        (self.root / "new.txt").write_text("new\n", encoding="utf-8")
        (self.root / "old.txt").write_text("old\n", encoding="utf-8")
        for name in ("new-skill", "old-skill"):
            (self.root / name).mkdir()
            (self.root / name / "SKILL.md").write_text(f"{name}\n", encoding="utf-8")
        (self.root / "empty").mkdir()
        return [
            ("file onto file", self.root / "new.txt", self.root / "old.txt"),
            ("directory onto directory", self.root / "new-skill", self.root / "old-skill"),
            ("directory onto empty directory", self.root / "new-skill", self.root / "empty"),
            ("file onto directory", self.root / "new.txt", self.root / "old-skill"),
        ]

    def test_move_renames_onto_a_free_destination(self) -> None:
        (self.root / "new.txt").write_text("new\n", encoding="utf-8")
        fsops.move(self.root / "new.txt", self.root / "moved.txt")
        self.assertFalse((self.root / "new.txt").exists())
        self.assertEqual("new\n", (self.root / "moved.txt").read_text(encoding="utf-8"))

    def test_move_never_replaces_an_existing_destination(self) -> None:
        for shape, source, destination in self.move_targets():
            with self.subTest(shape):
                before = self.held(source, destination)
                with self.assertRaises(FileExistsError):
                    fsops.move(source, destination)
                self.assertEqual(before, self.held(source, destination))

    def test_move_refuses_an_existing_destination_where_a_rename_would_replace_it(self) -> None:
        # A POSIX rename replaces a file or an empty directory; Path.replace does the same here.
        def replacing_rename(path: Path, target: Path) -> None:
            path.replace(target)

        with mock.patch.object(Path, "rename", replacing_rename):
            _, source, destination = self.move_targets()[0]
            before = self.held(source, destination)
            with mock.patch.object(platform_support, "RENAME_REPLACES", True), self.assertRaises(FileExistsError):
                fsops.move(source, destination)
            self.assertEqual(before, self.held(source, destination))
            # Without the check, the same move replaces the destination.
            with mock.patch.object(platform_support, "RENAME_REPLACES", False):
                fsops.move(source, destination)
            self.assertFalse(source.exists())
            self.assertEqual("new\n", destination.read_text(encoding="utf-8"))

    def test_remove_deletes_a_read_only_file(self) -> None:
        path = self.read_only(self.root / "held.txt", "read-only\n")
        fsops.remove(path)
        self.assertFalse(path.exists())

    def test_remove_deletes_a_tree_holding_a_read_only_file(self) -> None:
        tree = self.root / "skill"
        self.read_only(tree / "scripts" / "pack.idx", "read-only\n")
        (tree / "SKILL.md").write_text("writable\n", encoding="utf-8")
        fsops.remove(tree)
        self.assertFalse(tree.exists())

    def test_remove_still_raises_what_clearing_read_only_cannot_fix(self) -> None:
        tree = self.root / "skill"
        (tree / "scripts").mkdir(parents=True)
        (tree / "scripts" / "run.py").write_text("print()\n", encoding="utf-8")
        failure = OSError(16, "Device or resource busy", str(tree / "scripts" / "run.py"))
        with mock.patch("os.unlink", side_effect=failure), self.assertRaises(OSError) as raised:
            fsops.remove(tree)
        self.assertIs(failure, raised.exception)

    def test_write_atomic_removes_its_temporary_file_when_the_replace_fails(self) -> None:
        target = self.root / "info.json"
        target.write_text("previous\n", encoding="utf-8")
        with (
            mock.patch.object(Path, "replace", side_effect=PermissionError(5, "Access is denied")),
            self.assertRaises(PermissionError),
        ):
            fsops.write_atomic(target, b"next\n")
        self.assertEqual(["info.json"], sorted(path.name for path in self.root.iterdir()))
        self.assertEqual("previous\n", target.read_text(encoding="utf-8"))

    def test_write_atomic_removes_its_temporary_file_when_the_write_fails(self) -> None:
        target = self.root / "token"
        with mock.patch("os.fsync", side_effect=OSError(28, "No space left on device")), self.assertRaises(OSError):
            fsops.write_atomic(target, b"token")
        self.assertEqual([], list(self.root.iterdir()))

    def test_write_atomic_replaces_the_target(self) -> None:
        target = self.root / "info.json"
        target.write_text("previous\n", encoding="utf-8")
        fsops.write_atomic(target, b"next\n")
        self.assertEqual("next\n", target.read_text(encoding="utf-8"))
        self.assertEqual(["info.json"], sorted(path.name for path in self.root.iterdir()))

    def test_write_atomic_never_writes_through_a_file_already_at_a_temporary_name(self) -> None:
        """A file left or planted beside the target is never opened as the copy, so nothing is written through it."""
        target = self.root / "info.json"
        target.write_text("previous\n", encoding="utf-8")
        leftover = self.root / f".info.json.tmp.{os.getpid()}"
        leftover.write_text("leftover\n", encoding="utf-8")
        fsops.write_atomic(target, b"next\n")
        self.assertEqual("next\n", target.read_text(encoding="utf-8"))
        self.assertEqual("leftover\n", leftover.read_text(encoding="utf-8"))
        self.assertEqual(sorted([leftover.name, "info.json"]), sorted(path.name for path in self.root.iterdir()))

    def test_write_atomic_syncs_the_copy_before_it_replaces_the_target(self) -> None:
        target = self.root / "deploy.config"
        target.write_text("previous\n", encoding="utf-8")
        events: list[str] = []
        real_replace = Path.replace

        def replace(path: Path, destination: Path) -> Path:
            events.append("replace")
            return real_replace(path, destination)

        with (
            mock.patch("os.fsync", side_effect=lambda descriptor: events.append("fsync")),
            mock.patch.object(Path, "replace", replace),
        ):
            fsops.write_atomic(target, b"next\n")
        self.assertEqual(["fsync", "replace"], events)
        self.assertEqual("next\n", target.read_text(encoding="utf-8"))
        self.assertEqual(["deploy.config"], sorted(path.name for path in self.root.iterdir()))

    def test_write_atomic_keeps_the_target_and_removes_its_copy_when_the_sync_fails(self) -> None:
        target = self.root / "deploy.config"
        target.write_text("previous\n", encoding="utf-8")
        with mock.patch("os.fsync", side_effect=OSError(28, "No space left on device")), self.assertRaises(OSError):
            fsops.write_atomic(target, b"next\n")
        self.assertEqual("previous\n", target.read_text(encoding="utf-8"))
        self.assertEqual(["deploy.config"], sorted(path.name for path in self.root.iterdir()))

    def archive(self, *names: str) -> Path:
        """A tar archive holding a file under each name, its content the name itself."""
        archive = self.root / "tree.tar"
        with tarfile.open(archive, "w") as handle:
            for name in names:
                info = tarfile.TarInfo(name)
                info.size = len(name.encode("utf-8"))
                handle.addfile(info, io.BytesIO(name.encode("utf-8")))
        return archive

    def test_extract_archive_strips_leading_slashes_and_extracts_inside_the_destination(self) -> None:
        destination = self.root / "tree"
        fsops.extract_archive(self.archive("/skills/alpha/SKILL.md", "//server/share/notes.md"), destination)
        extracted = {
            path.relative_to(destination).as_posix(): path.read_text(encoding="utf-8")
            for path in destination.rglob("*")
            if path.is_file()
        }
        self.assertEqual(
            {"server/share/notes.md": "//server/share/notes.md", "skills/alpha/SKILL.md": "/skills/alpha/SKILL.md"},
            extracted,
        )

    def test_extract_archive_refuses_a_drive_qualified_name_and_one_leading_outside_the_destination(self) -> None:
        for name, refusal in (
            ("C:/Windows/notes.md", tarfile.AbsolutePathError),
            ("../notes.md", tarfile.OutsideDestinationError),
            ("/../notes.md", tarfile.OutsideDestinationError),
            ("skills/../../notes.md", tarfile.OutsideDestinationError),
        ):
            with self.subTest(name=name):
                destination = self.root / "tree"
                with self.assertRaises(refusal):
                    fsops.extract_archive(self.archive(name), destination)
                self.assertFalse((self.root / "notes.md").exists())


if __name__ == "__main__":
    unittest.main()
