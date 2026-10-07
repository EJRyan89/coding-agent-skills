"""The filesystem operations every deployer change goes through, on their own."""

from __future__ import annotations

import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from deployer import fsops


class FilesystemOperationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
