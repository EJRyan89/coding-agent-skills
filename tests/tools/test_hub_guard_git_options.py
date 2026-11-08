"""The hub guard's tables of git's options that write a file or run a command, held to git's own catalogue: every
option `git <subcommand> -h` lists with a path for its value is one the guard judges as a write, refuses as running a
command, or names in GIT_PATH_OPTIONS_READ with a reason, so an option a later git adds fails here until a row decides
it. The readers of those tables are held to each spelling git's option parser takes."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from tools import hub_guard

# An option line of parse-options' help: an optional short letter, then a long option (perhaps with [no-]) or a short
# one alone, then its value's placeholder.
OPTION_LINE = re.compile(r"^\s{2,}(?:-(\w),\s+)?(?:--(?:\[no-\])?([\w-]+)|-(\w))\s*(?:\[?=)?<([^>]+)>")
# The placeholders that name a file or a directory.
PATH_PLACEHOLDER = re.compile(r"file|path|dir|root", re.IGNORECASE)
# The builtins whose -h starts a server or waits for input rather than printing its options.
NOT_ASKED = frozenset({"daemon", "credential-cache--daemon", "fsmonitor--daemon", "upload-pack", "receive-pack"})


def path_options() -> set[str]:
    """Each `<subcommand> <option>` the installed git's `-h` lists with a path for its value, both spellings of an
    option that has a letter and a name."""
    builtins = subprocess.run(
        ["git", "--list-cmds=builtins"], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True
    ).stdout
    found: set[str] = set()
    with tempfile.TemporaryDirectory() as empty:
        for subcommand in builtins.split():
            if subcommand in NOT_ASKED:
                continue
            result = subprocess.run(
                ["git", subcommand, "-h"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=empty,
                stdin=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
            for line in (result.stdout + result.stderr).splitlines():
                match = OPTION_LINE.match(line)
                if match is None or not PATH_PLACEHOLDER.search(match.group(4)):
                    continue
                letter, name, alone = match.group(1), match.group(2), match.group(3)
                found.update(
                    f"{subcommand} {spelling}"
                    for spelling in (letter and f"-{letter}", name and f"--{name}", alone and f"-{alone}")
                    if spelling
                )
    return found


def running(subcommand: str) -> set[str]:
    """Each spelling of an option GIT_RUNNING_OPTIONS names for a subcommand, as `-h` lists it."""
    options = hub_guard.GIT_RUNNING_OPTIONS.get(subcommand)
    if options is None:
        return set()
    return {*(f"--{name}" for name in options.long), *(f"-{letter}" for letter in options.short)}


def decided(entry: str) -> bool:
    subcommand, option = entry.split(" ", 1)
    return (
        option == hub_guard.GIT_OUTPUT
        or option in hub_guard.GIT_WRITING_OPTIONS.get(subcommand, ())
        or option in running(subcommand)
        or entry in hub_guard.GIT_PATH_OPTIONS_READ
    )


class CatalogueTests(unittest.TestCase):
    def test_every_option_git_lists_with_a_path_is_decided(self) -> None:
        listed = path_options()
        self.assertIn("archive -o", listed, "the catalogue reader finds git's options")
        self.assertEqual([], sorted(entry for entry in listed if not decided(entry)))

    def test_every_read_entry_names_a_reason_and_no_option_is_both_read_and_written(self) -> None:
        for entry, reason in hub_guard.GIT_PATH_OPTIONS_READ.items():
            with self.subTest(entry=entry):
                subcommand, option = entry.split(" ", 1)
                self.assertGreater(len(reason.split()), 2)
                self.assertNotIn(option, hub_guard.GIT_WRITING_OPTIONS.get(subcommand, ()))
                self.assertNotIn(option, running(subcommand))

    def test_each_subcommand_with_an_option_that_writes_or_runs_refuses_a_word_the_text_does_not_settle(self) -> None:
        # Pinned against literals, not the tables: the subcommands a probe found writing --output.
        for subcommand in ("diff", "log", "show", "rev-list", "shortlog", "reflog", "blame", "range-diff", "revert"):
            with self.subTest(subcommand=subcommand):
                self.assertIn(subcommand, hub_guard.GIT_OUTPUT_SUBCOMMANDS)
        self.assertEqual("--output", hub_guard.GIT_OUTPUT)


class EffectsTests(unittest.TestCase):
    def test_each_spelling_of_a_writing_option_names_the_file_it_writes(self) -> None:
        for subcommand, words, written in (
            ("diff", ["--output=out"], ["out"]),
            ("diff", ["--output", "out"], ["out"]),
            ("log", ["--outp=out"], ["out"]),
            ("archive", ["-o", "out", "HEAD"], ["out"]),
            ("archive", ["-oout", "HEAD"], ["out"]),
            ("archive", ["-vo", "out", "HEAD"], ["out"]),
            ("archive", ["-voout", "HEAD"], ["out"]),
            ("format-patch", ["-1", "--output-dir", "out"], ["out"]),
            ("format-patch", ["-1"], ["."]),
            ("format-patch", ["-1", "--stdout"], []),
            ("bugreport", [], ["."]),
            ("bugreport", ["-o", "out"], ["out"]),
            ("bundle", ["create", "--version", "3", "out", "HEAD"], ["out"]),
            ("bundle", ["create", "--", "out", "HEAD"], ["out"]),
            ("bundle", ["create", None, "HEAD"], [None]),
            ("bundle", ["verify", "out"], []),
            ("pack-objects", ["--window", "5", "out"], ["5", "out"]),
            ("pack-objects", ["--stdout"], []),
            ("fast-export", ["--export-m=out"], ["out"]),
            ("diff", ["--output-indicator-new=+"], []),
            ("diff", ["--", "--output=out"], []),
            ("diff", ["--output=out", "--output", None], ["out", None]),
            ("init", [], ["."]),
            ("init", ["-q", "out"], ["out"]),
            ("init", ["--separate-git-dir=git", "out"], ["git", "out"]),
            ("unpack-file", ["0123abc"], ["."]),
            ("clone", ["https://example.invalid/x.git"], ["."]),
            ("clone", ["https://example.invalid/x.git", "out"], ["out"]),
            ("clone", ["--depth", "1", "https://example.invalid/x.git", "out"], ["out"]),
            ("clone", ["--separate-git-dir", "git", "../x"], ["git", "."]),
            ("worktree", ["add", "-b", "topic", "out", "HEAD"], ["out"]),
            ("worktree", ["add", "--reason=r", "-fbtopic", "out"], ["out"]),
            ("worktree", ["list"], []),
        ):
            with self.subTest(subcommand=subcommand, words=words):
                self.assertEqual(written, hub_guard.git_effects(subcommand, words).written)

    def test_each_spelling_of_a_running_option_or_word_is_found(self) -> None:
        for subcommand, words, runs in (
            ("fetch", ["--upload-pack=x", "."], "--upload-pack=x"),
            ("fetch", ["--upl", "x", "."], "--upl"),
            ("grep", ["-nOvim", "x"], "-nOvim"),
            ("rebase", ["-ix", "true"], "-ix"),
            ("difftool", ["--extc=true"], "--extc=true"),
            ("init-db", ["--templ", "t"], "--templ"),
            ("bisect", ["run", "make"], "run"),
            ("bisect", ["replay", "steps"], "replay"),
            ("submodule", ["--quiet", "foreach", "true"], "foreach"),
            ("submodule", ["status", "foreach"], None),
            ("fetch", ["origin"], None),
            # carried_option reads every word wherever it stands, so a value spelled like one is refused too.
            ("rebase", ["--", "-x"], "-x"),
        ):
            with self.subTest(subcommand=subcommand, words=words):
                self.assertEqual(runs, hub_guard.git_effects(subcommand, words).runs)

    def test_a_git_config_reads_only_in_its_reading_forms(self) -> None:
        for words, expected in (
            (["--get", "a.b"], (True, None)),
            (["a.b"], (True, None)),
            (["get", "a.b"], (True, None)),
            (["list", "--show-origin"], (True, None)),
            (["-l"], (True, None)),
            (["--file", "x", "--get", "a.b"], (True, "x")),
            (["a.b", "c"], (False, None)),
            (["set", "a.b", "c"], (False, None)),
            (["edit"], (False, None)),
            (["-e"], (False, None)),
            (["--unse", "a.b"], (False, None)),
            (["--unset", "a.b"], (False, None)),
            (["-fx", "a.b", "c"], (False, "x")),
            (["--file=x", "a.b", "c"], (False, "x")),
            (["--global", "--file", "x", "a.b", "c"], (False, None)),
            ([None, "c"], (False, None)),
            (["--", "a.b", "c"], (False, None)),
        ):
            with self.subTest(words=words):
                self.assertEqual(expected, hub_guard.config_reads(words))

    def test_a_setting_is_understood_by_its_key_or_section_in_any_case(self) -> None:
        for setting, understood in (
            ("color.ui=never", True),
            ("Color.Diff.Meta=blue", True),
            ("alias.go=status", True),
            ("core.worktree=/x", True),
            ("user.name=A Name", True),
            ("core.pager=less", False),
            ("pager.log=less", False),
            ("diff.external=x", False),
            ("core.fsmonitor=x", False),
            ("include.path=x", False),
            (None, False),
        ):
            with self.subTest(setting=setting):
                reading = hub_guard.Reading({})
                hub_guard.read_setting(setting, reading)
                self.assertEqual(understood, not reading.unread)
