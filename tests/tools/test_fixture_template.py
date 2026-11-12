"""Regression suite for tests/tools/fixture_template.py: a git fixture built once per process and copied per test."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_template import FixtureTemplate, path_forms, relocate_configs, remove_tree, repository_shape

GIT_CONFIG: Mapping[str, str] = MappingProxyType({"init.defaultBranch": "main"})


def git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(directory), *arguments], capture_output=True, text=True, encoding="utf-8", check=False
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} failed: {result.stderr}")
    return result.stdout.strip()


def build_remote_and_clone(root: Path) -> None:
    """A bare origin with one commit on main, and a clone of it that also reaches it through an insteadOf URL."""
    origin = root / "origin store" / "origin.git"
    git(root, "init", "--quiet", "--bare", str(origin))
    git(root, "clone", "--quiet", str(origin), str(root / "work tree"))
    (root / "work tree" / "README.md").write_text("fixture\n", encoding="utf-8")
    git(root / "work tree", "add", "README.md")
    git(root / "work tree", "commit", "--quiet", "-m", "first")
    git(root / "work tree", "push", "--quiet", "origin", "main")
    git(root / "work tree", "config", f"url.{origin.as_posix()}.insteadOf", "https://github.com/owner/repo")
    git(root / "work tree", "remote", "add", "github", "https://github.com/owner/repo")
    git(root / "work tree", "fetch", "--quiet", "github")


class Temporary(unittest.TestCase):
    git_config = GIT_CONFIG

    def folder(self) -> Path:
        path = Path(os.path.realpath(tempfile.mkdtemp(prefix="fixture template test ")))
        self.addCleanup(remove_tree, path)
        return path


class PathFormTests(unittest.TestCase):
    def test_a_windows_path_has_its_escaped_form_first_then_its_own_then_forward_slashes(self) -> None:
        self.assertEqual(
            ["C:\\\\Temp\\\\a b", "C:\\Temp\\a b", "C:/Temp/a b"], path_forms(PureWindowsPath("C:\\Temp\\a b"))
        )

    def test_a_posix_path_has_one_form(self) -> None:
        self.assertEqual(["/home/fixture/a b"], path_forms(PurePosixPath("/home/fixture/a b")))


class RelocateTests(Temporary):
    def test_each_form_of_the_template_path_in_a_repositorys_config_names_the_copy(self) -> None:
        template, copy = self.folder() / "template", self.folder() / "copy"
        repository = copy / "repo" / ".git"
        repository.mkdir(parents=True)
        (repository / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        forms = path_forms(template / "origin.git")
        text = "".join(f"\tkey{index} = {form}\n" for index, form in enumerate(forms))
        (repository / "config").write_text(f'[url "{template.as_posix()}/origin.git"]\n{text}', encoding="utf-8")
        relocate_configs(copy, template)
        expected = "".join(f"\tkey{index} = {form}\n" for index, form in enumerate(path_forms(copy / "origin.git")))
        self.assertEqual(
            f'[url "{copy.as_posix()}/origin.git"]\n{expected}', (repository / "config").read_text(encoding="utf-8")
        )

    def test_a_file_named_config_outside_a_repository_is_left_as_it_is(self) -> None:
        template, copy = self.folder() / "template", self.folder() / "copy"
        copy.mkdir()
        (copy / "config").write_text(str(template), encoding="utf-8")
        relocate_configs(copy, template)
        self.assertEqual(str(template), (copy / "config").read_text(encoding="utf-8"))


class TemplateTests(Temporary):
    def test_the_template_is_built_once_and_each_copy_is_a_folder_of_its_own(self) -> None:
        builds: list[Path] = []

        def build(root: Path) -> None:
            builds.append(root)
            (root / "file.txt").write_text("template\n", encoding="utf-8")

        template = FixtureTemplate("counted", build)
        first, second = template.copy(self.folder() / "first"), template.copy(self.folder() / "second")
        self.assertEqual([template.root], builds)
        self.assertEqual("counted", template.root.name)
        (first / "file.txt").write_text("changed\n", encoding="utf-8")
        self.assertEqual("template\n", (second / "file.txt").read_text(encoding="utf-8"))
        self.assertEqual("template\n", (template.root / "file.txt").read_text(encoding="utf-8"))

    def test_a_copy_may_go_into_an_existing_empty_folder(self) -> None:
        template = FixtureTemplate("existing", lambda root: (root / "file.txt").write_text("x\n", encoding="utf-8"))
        destination = self.folder()
        self.assertEqual(destination, template.copy(destination))
        self.assertEqual("x\n", (destination / "file.txt").read_text(encoding="utf-8"))

    def test_a_build_that_fails_leaves_no_template_and_the_next_copy_builds_again(self) -> None:
        attempts: list[Path] = []

        def build(root: Path) -> None:
            attempts.append(root)
            if len(attempts) == 1:
                raise OSError("no room")
            (root / "file.txt").write_text("built\n", encoding="utf-8")

        template = FixtureTemplate("retried", build)
        with self.assertRaisesRegex(OSError, "no room"):
            template.copy(self.folder())
        self.assertFalse(attempts[0].exists())
        copied = template.copy(self.folder())
        self.assertEqual("built\n", (copied / "file.txt").read_text(encoding="utf-8"))
        self.assertEqual(2, len(attempts))

    def test_a_copy_equals_a_fresh_build_and_a_push_from_it_leaves_the_templates_origin_untouched(self) -> None:
        template = FixtureTemplate("remote and clone", build_remote_and_clone)
        fresh = self.folder()
        build_remote_and_clone(fresh)
        copied = template.copy(self.folder())
        repositories = ["origin store/origin.git", "work tree"]
        self.assertEqual(repository_shape(fresh, repositories), repository_shape(copied, repositories))

        before = git(template.root / "origin store" / "origin.git", "rev-parse", "main")
        clone = copied / "work tree"
        git(clone, "commit", "--quiet", "--allow-empty", "-m", "only in this copy")
        git(clone, "push", "--quiet", "origin", "main")
        git(clone, "push", "--quiet", "github", "main:refs/heads/through-github")
        pushed = git(clone, "rev-parse", "HEAD")
        self.assertEqual(before, git(template.root / "origin store" / "origin.git", "rev-parse", "main"))
        self.assertEqual(pushed, git(copied / "origin store" / "origin.git", "rev-parse", "main"))
        self.assertEqual(pushed, git(copied / "origin store" / "origin.git", "rev-parse", "through-github"))


class ShapeTests(Temporary):
    def test_the_shape_writes_the_root_as_a_placeholder_and_tells_two_subjects_apart(self) -> None:
        first, second = self.folder(), self.folder()
        build_remote_and_clone(first)
        build_remote_and_clone(second)
        repositories = ["origin store/origin.git", "work tree"]
        shape = repository_shape(first, repositories)
        self.assertIn("<root>/origin store/origin.git", shape["work tree config"])
        self.assertNotIn(first.as_posix(), "".join(shape.values()))
        self.assertNotIn("origin store/origin.git status", shape)
        self.assertEqual("refs/heads/main\n", shape["work tree head"])
        git(second / "work tree", "commit", "--quiet", "--amend", "-m", "another subject")
        self.assertNotEqual(shape, repository_shape(second, repositories))

    def test_a_failing_git_command_names_the_repository(self) -> None:
        missing = self.folder()
        with self.assertRaisesRegex(AssertionError, "for-each-ref"):
            repository_shape(missing, ["absent"])


class RemoveTreeTests(Temporary):
    def test_a_read_only_file_is_removed_with_its_folder(self) -> None:
        folder = Path(tempfile.mkdtemp(prefix="fixture template removal "))
        locked = folder / "nested" / "object"
        locked.parent.mkdir()
        locked.write_text("x", encoding="utf-8")
        locked.chmod(stat.S_IREAD)
        remove_tree(folder)
        self.assertFalse(folder.exists())
