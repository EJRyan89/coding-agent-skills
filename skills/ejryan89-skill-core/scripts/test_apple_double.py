"""Regression tests for the one test of whether a file is an AppleDouble sidecar macOS wrote."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from apple_double import is_sidecar

# The header macOS writes at the start of a sidecar, written out so the rule is held to it, not to the constant: the
# magic number 0x00051607, then the version 0x00020000, big-endian.
HEADER = b"\x00\x05\x16\x07\x00\x02\x00\x00"


class SidecarTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def write(self, name: str, content: bytes) -> Path:
        path = self.folder / name
        path.write_bytes(content)
        return path

    def test_a_dot_underscore_file_beside_its_file_that_opens_with_the_header_is_a_sidecar(self) -> None:
        path = self.write("._doc.md", HEADER + b"Mac OS X        \x00\x02")
        self.assertTrue(is_sidecar(path, {"doc.md", "._doc.md"}))
        self.assertTrue(is_sidecar(path, ["doc.md"]), "any collection of names")
        self.assertTrue(is_sidecar(self.write("._exact", HEADER), {"exact"}), "the header alone is enough")

    def test_a_name_alone_never_decides_it(self) -> None:
        cases = {
            "no file beside it": ("._doc.md", HEADER, {"._doc.md", "other.md"}),
            "a file beside it of another case": ("._doc.md", HEADER, {"Doc.md"}),
            "no header": ("._doc.md", b"plain text, long enough\n", {"doc.md"}),
            "cut short of the header": ("._doc.md", HEADER[:7], {"doc.md"}),
            "empty": ("._doc.md", b"", {"doc.md"}),
            "AppleSingle's magic number": ("._doc.md", b"\x00\x05\x16\x00\x00\x02\x00\x00", {"doc.md"}),
            "version 1": ("._doc.md", b"\x00\x05\x16\x07\x00\x01\x00\x00", {"doc.md"}),
            "the header with no dot-underscore": ("doc.md", HEADER, {"doc.md", "oc.md"}),
            "one dot": (".doc.md", HEADER, {"doc.md"}),
            "the prefix alone": ("._", HEADER, {"._"}),
        }
        for case, (name, content, names) in cases.items():
            with self.subTest(case=case):
                self.assertFalse(is_sidecar(self.write(name, content), names))

    def test_only_a_dot_underscore_name_whose_file_is_listed_is_opened(self) -> None:
        sidecar = self.write("._doc.md", HEADER)
        alone = self.write("._alone.md", HEADER)
        plain = self.write("doc.md", HEADER)
        with mock.patch.object(Path, "open", side_effect=AssertionError("opened")):
            self.assertFalse(is_sidecar(alone, {"doc.md"}))
            self.assertFalse(is_sidecar(plain, {"doc.md", "oc.md"}))
        real = Path.open
        opened: list[Path] = []

        def recording(path: Path, *arguments: Any, **keywords: Any) -> Any:
            opened.append(path)
            return real(path, *arguments, **keywords)

        with mock.patch.object(Path, "open", recording):
            self.assertTrue(is_sidecar(sidecar, {"doc.md"}))
        self.assertEqual([sidecar], opened)

    def test_a_file_that_cannot_be_opened_or_read_is_not_a_sidecar(self) -> None:
        path = self.write("._doc.md", HEADER)
        with mock.patch.object(Path, "open", side_effect=PermissionError("synthetic refusal")):
            self.assertFalse(is_sidecar(path, {"doc.md"}))

        class Unreadable:
            def __enter__(self) -> Unreadable:
                return self

            def __exit__(self, *_: Any) -> None:
                return None

            def read(self, _size: int) -> bytes:
                raise OSError("synthetic read failure")

        with mock.patch.object(Path, "open", return_value=Unreadable()):
            self.assertFalse(is_sidecar(path, {"doc.md"}))
        self.assertFalse(is_sidecar(self.folder / "._gone.md", {"gone.md"}), "nor one no longer there")

    def test_a_folder_named_like_a_sidecar_is_not_one(self) -> None:
        (self.folder / "._doc.md").mkdir()
        self.assertFalse(is_sidecar(self.folder / "._doc.md", {"doc.md"}))


@unittest.skipUnless(sys.platform == "darwin", "only macOS makes and mounts a FAT image without privileges")
class RealVolumeTests(unittest.TestCase):
    """The sidecars macOS writes itself on a real FAT and exFAT volume, attached from a disk image."""

    def test_the_sidecar_macos_writes_beside_a_file_and_a_folder_is_one_and_the_file_is_not(self) -> None:
        for file_system in ("MS-DOS", "ExFAT"):
            with self.subTest(file_system=file_system), tempfile.TemporaryDirectory() as temporary:
                image = Path(temporary) / "volume.dmg"
                mount = Path(temporary) / "mount"
                mount.mkdir()
                hdiutil = ["hdiutil", "create", "-quiet", "-size", "8m", "-fs", file_system, "-volname", "PROBE"]
                subprocess.run([*hdiutil, str(image)], check=True, capture_output=True, timeout=120)
                attach = ["hdiutil", "attach", "-quiet", "-nobrowse", "-mountpoint", str(mount), str(image)]
                subprocess.run(attach, check=True, capture_output=True, timeout=120)
                try:
                    self._check_volume(mount)
                finally:
                    detach = ["hdiutil", "detach", "-quiet", "-force", str(mount)]
                    subprocess.run(detach, check=True, capture_output=True, timeout=120)

    def _check_volume(self, volume: Path) -> None:
        (volume / "docs").mkdir()
        (volume / "doc.md").write_bytes(b"text\n")
        # macOS may already mark a new file with an attribute; one set here makes the sidecar whatever it does.
        for written in ("doc.md", "docs"):
            attribute = ["xattr", "-w", "org.example.probe", "1", str(volume / written)]
            subprocess.run(attribute, check=True, capture_output=True, timeout=60)
        names = {entry.name for entry in volume.iterdir()}
        self.assertLessEqual({"doc.md", "._doc.md", "docs", "._docs"}, names, "macOS wrote both sidecars")
        self.assertTrue(is_sidecar(volume / "._doc.md", names))
        self.assertTrue(is_sidecar(volume / "._docs", names))
        self.assertFalse(is_sidecar(volume / "doc.md", names))
