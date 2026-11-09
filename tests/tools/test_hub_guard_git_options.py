"""The hub guard's tables of git's options that write a file or run a command, held to git's own catalogue: every
option `git <subcommand> -h` lists with a path for its value is one the guard judges as a write, refuses as running a
command, or names in GIT_PATH_OPTIONS_READ with a reason, so an option a later git adds fails here until a row decides
it. The readers of those tables are held to each spelling git's option parser takes."""

from __future__ import annotations

import functools
import re
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import astuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from tools import hub_guard

# An option line of parse-options' help: an optional short letter, then a long option (perhaps with [no-]) or a short
# one alone, then its value's placeholder.
OPTION_LINE = re.compile(r"^\s{2,}(?:-(\w),\s+)?(?:--(?:\[no-\])?([\w-]+)|-(\w))\s*(?:\[?=)?<([^>]+)>")
# The same line for an option that takes the next word or a value after =, not one whose value is optional.
VALUED_LINE = re.compile(r"^\s{2,}(?:-(\w),\s+)?(?:--(?:\[no-\])?([\w-]+)|-(\w))(?:=| )<([^>]+)>")
# A boolean option's line, with its description on the same line.
FLAG_LINE = re.compile(r"^\s{2,}(?:-(\w),\s+)?(?:--(?:\[no-\])?([\w-]+)|-(\w))\s{2,}(\S.*)$")
# The placeholders that name a file or a directory, and the option names that name a root git writes beneath.
PATH_PLACEHOLDER = re.compile(r"file|path|dir|root", re.IGNORECASE)
ROOT_OPTION = re.compile(r"(?:prefix|directory|root)")
# The operand placeholders of a usage line that name a file, a directory, a mail, or a patch.
PATH_OPERAND = re.compile(r"file|path|dir|root|msg|patch|mbox|dest|base-name", re.IGNORECASE)
# A placeholder in a usage line, with the word it is attached to.
PLACEHOLDER = re.compile(r"(\S*?)<([^>]+)>")
# What starts a usage line before its words: `usage:` or `or:`, a marker, and `git <subcommand>`.
USAGE_START = re.compile(r"^\s*(?:usage:|or:)?\s*(?:\(EXPERIMENTAL!\)\s+)?(?:git\s+\S+)?")
# What a boolean option's description says when it makes git write a file.
WRITING_FLAG = re.compile(r"\b(?:over)?write\b|in[ -]place|outside the work", re.IGNORECASE)
# The builtins whose -h starts a server or waits for input rather than printing its options.
NOT_ASKED = frozenset({"daemon", "credential-cache--daemon", "fsmonitor--daemon", "upload-pack", "receive-pack"})


def help_text(words: Sequence[str], cwd: str) -> str:
    result = subprocess.run(
        ["git", *words, "-h"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    return result.stdout + result.stderr


@functools.cache
def help_texts() -> dict[str, str]:
    """The `-h` text of each builtin the installed git lists, asked outside every repository."""
    builtins = subprocess.run(
        ["git", "--list-cmds=builtins"], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True
    ).stdout
    with tempfile.TemporaryDirectory() as empty:
        return {name: help_text([name], empty) for name in builtins.split() if name not in NOT_ASKED}


def spellings(match: re.Match[str]) -> list[str]:
    letter, name, alone = match.group(1), match.group(2), match.group(3)
    return [spelling for spelling in (letter and f"-{letter}", name and f"--{name}", alone and f"-{alone}") if spelling]


def path_options() -> set[str]:
    """Each `<subcommand> <option>` the installed git's `-h` lists with a path for its value, or with a name that
    names a root, both spellings of an option that has a letter and a name."""
    found: set[str] = set()
    for subcommand, text in help_texts().items():
        for line in text.splitlines():
            match = OPTION_LINE.match(line)
            if match is None:
                continue
            if PATH_PLACEHOLDER.search(match.group(4)) or ROOT_OPTION.fullmatch(match.group(2) or ""):
                found.update(f"{subcommand} {spelling}" for spelling in spellings(match))
    return found


def usage_operands() -> set[str]:
    """Each `<subcommand> <placeholder>` the installed git's usage names as an operand that is a path, a mail, or a
    patch: a placeholder no option word stands before or is attached to."""
    found: set[str] = set()
    for subcommand, text in help_texts().items():
        for line in text.split("\n\n", 1)[0].splitlines():
            rest = USAGE_START.sub("", line, count=1)
            for placeholder in PLACEHOLDER.finditer(rest):
                attached = placeholder.group(1).lstrip("[(|")
                before = rest[: placeholder.start()].split()
                previous = before[-1].strip("[]()|") if before else ""
                valued = previous.startswith("-") and previous != "--"
                if attached.startswith("-") or attached.endswith("=") or (not attached and valued):
                    continue
                if PATH_OPERAND.search(placeholder.group(2)):
                    found.add(f"{subcommand} <{placeholder.group(2)}>")
    return found


def writing_flags() -> set[str]:
    """Each `<subcommand> <option>` the installed git's `-h` lists with no value and a description that says it
    writes, overwrites, edits in place, or reaches outside the work tree."""
    found: set[str] = set()
    for subcommand, text in help_texts().items():
        for line in text.splitlines():
            match = FLAG_LINE.match(line)
            if match is not None and WRITING_FLAG.search(match.group(4)):
                found.update(f"{subcommand} {spelling}" for spelling in spellings(match))
    return found


def valued_options(words: Sequence[str]) -> set[str]:
    """Each option `git <words> -h` lists that takes the next word as its value, asked in a new repository."""
    with tempfile.TemporaryDirectory() as directory:
        subprocess.run(["git", "init", "-q", directory], capture_output=True, timeout=30, check=True)
        text = help_text(words, directory)
    found: set[str] = set()
    for line in text.splitlines():
        match = VALUED_LINE.match(line)
        if match is not None:
            found.update(spellings(match))
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

    def test_every_operand_git_s_usage_names_with_a_path_is_decided(self) -> None:
        listed = usage_operands()
        self.assertIn("mailinfo <msg>", listed, "the usage reader finds git's operands")
        self.assertIn("pack-objects <base-name>", listed, "the usage reader reads a usage's later lines")
        self.assertNotIn("mailsplit <directory>", listed, "a placeholder attached to an option is its value")
        undecided = [
            entry
            for entry in listed
            if entry.split(" ")[0] not in hub_guard.GIT_OPERAND_SUBCOMMANDS and entry not in hub_guard.GIT_OPERANDS_READ
        ]
        self.assertEqual([], sorted(undecided))

    def test_every_boolean_option_git_says_writes_is_decided(self) -> None:
        listed = writing_flags()
        self.assertIn("interpret-trailers --in-place", listed, "the flag reader finds git's in-place modes")
        self.assertIn("apply --unsafe-paths", listed, "the flag reader finds an option reaching outside the work tree")

        def decided_flag(entry: str) -> bool:
            subcommand, option = entry.split(" ", 1)
            rows = [row for key, row in hub_guard.GIT_OPERAND_WRITERS.items() if key.split(" ")[0] == subcommand]
            return (
                any(option in row.modes | row.stdout for row in rows)
                or (subcommand in hub_guard.GIT_UNREAD_MODES and option in hub_guard.GIT_UNREAD_MODES[subcommand].modes)
                or entry in hub_guard.GIT_MODES_READ
            )

        self.assertEqual([], sorted(entry for entry in listed if not decided_flag(entry)))

    def test_each_operand_writer_and_unread_mode_skips_the_value_of_every_option_git_lists_with_one(self) -> None:
        for key, row in (*hub_guard.GIT_OPERAND_WRITERS.items(), *hub_guard.GIT_UNREAD_MODES.items()):
            with self.subTest(key=key):
                subcommand = key.split(" ")[0]
                skipped = row.valued | hub_guard.GIT_WRITING_OPTIONS.get(subcommand, frozenset())
                self.assertEqual([], sorted(valued_options(key.split(" ")) - skipped))

    def test_every_read_operand_and_mode_names_a_reason_and_none_is_also_a_writer(self) -> None:
        for entry, reason in (*hub_guard.GIT_OPERANDS_READ.items(), *hub_guard.GIT_MODES_READ.items()):
            with self.subTest(entry=entry):
                self.assertGreater(len(reason.split()), 2)
                self.assertNotIn(entry.split(" ")[0], hub_guard.GIT_OPERAND_SUBCOMMANDS)

    def test_the_writers_an_outside_review_found_allowed_are_in_the_tables(self) -> None:
        # Pinned against literals, not the tables: an in-place mode, output operands, and roots that wrote a hub.
        self.assertEqual(frozenset({"--in-place"}), hub_guard.GIT_OPERAND_WRITERS["interpret-trailers"].modes)
        self.assertEqual((0, None), astuple(hub_guard.GIT_OPERAND_WRITERS["mailinfo"])[:2])
        self.assertEqual((0, 1), astuple(hub_guard.GIT_OPERAND_WRITERS["merge-file"])[:2])
        self.assertEqual((1, 1), astuple(hub_guard.GIT_OPERAND_WRITERS["worktree move"])[:2])
        self.assertIn("--prefix", hub_guard.GIT_WRITING_OPTIONS["checkout-index"])
        self.assertIn("--directory", hub_guard.GIT_WRITING_OPTIONS["apply"])
        self.assertIn("--directory", hub_guard.GIT_WRITING_OPTIONS["am"])
        self.assertEqual(frozenset({"--unsafe-paths"}), hub_guard.GIT_UNREAD_MODES["apply"].modes)

    def test_each_subcommand_with_an_option_that_writes_or_runs_refuses_a_word_the_text_does_not_settle(self) -> None:
        # Pinned against literals, not the tables: the subcommands a probe found writing --output.
        for subcommand in ("diff", "log", "show", "rev-list", "shortlog", "reflog", "blame", "range-diff", "revert"):
            with self.subTest(subcommand=subcommand):
                self.assertIn(subcommand, hub_guard.GIT_OUTPUT_SUBCOMMANDS)
        self.assertEqual("--output", hub_guard.GIT_OUTPUT)


@functools.cache
def row_help(key: str) -> str:
    """`git <key> -h` for a READ_ONLY_FORMS row, asked in a new repository."""
    with tempfile.TemporaryDirectory() as directory:
        subprocess.run(["git", "init", "-q", directory], capture_output=True, timeout=30, check=True)
        return help_text(key.split(" "), directory)


# The head of an option line of parse-options' help, before the two spaces its description starts after: an optional
# letter, then a long option (perhaps with [no-]) or a letter alone, then what follows it.
HELP_HEAD = re.compile(r"(?:-(\w),\s+)?(?:--(\[no-\])?([\w-]+)|-(\w))(.*)")


def help_options(text: str) -> dict[str, tuple[str, str]]:
    """Each option spelling a help text lists, with its kind, flag, valued, or attached, and its description; the
    `--no-` form of a `--[no-]` option is a flag."""
    found: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not line.startswith("  ") or not stripped.startswith("-"):
            continue
        head, *described = re.split(r"\s{2,}", stripped, maxsplit=1)
        description = described[0] if described else ""
        match = HELP_HEAD.fullmatch(head)
        if match is None:
            continue
        letter, negatable, name, alone, after = match.groups()
        if after.startswith("["):
            kind = "attached"
        elif re.match(r"[ =]<", after):
            kind = "valued"
        elif after == "" or after.startswith(" "):
            # A long head leaves one space before its description.
            kind, description = "flag", f"{after} {description}".strip()
        else:
            continue
        for spelling in (letter and f"-{letter}", name and f"--{name}", alone and f"-{alone}"):
            if spelling:
                found[spelling] = (kind, description)
        if negatable:
            found[f"--no-{name}"] = ("flag", description)
    return found


def declared(form: hub_guard.ReadOnlyForm) -> dict[str, str]:
    """Each option a row declares, with its kind."""
    return {
        **dict.fromkeys(form.flags, "flag"),
        **dict.fromkeys(form.attached, "attached"),
        **dict.fromkeys(form.valued, "valued"),
    }


class ReadOnlyCatalogueTests(unittest.TestCase):
    """READ_ONLY_FORMS, the forms a guarded hub allows, held to the installed git's own catalogue."""

    def test_the_help_reader_reads_each_kind_of_option_line(self) -> None:
        listed = help_options(row_help("branch"))
        self.assertEqual("valued", listed["--set-upstream-to"][0])
        self.assertEqual("valued", listed["-u"][0])
        self.assertEqual("attached", listed["--color"][0])
        self.assertEqual("flag", listed["--no-color"][0])
        self.assertEqual("flag", listed["-D"][0])
        self.assertEqual("attached", help_options(row_help("tag"))["-n"][0])
        self.assertIn("delete branch", listed["-D"][1])
        self.assertEqual("flag", help_options(row_help("apply"))["--inaccurate-eof"][0])

    def test_each_row_declares_only_options_git_lists_for_it_with_the_kind_git_reads(self) -> None:
        # An option git does not list may be an abbreviation it expands, and a valued option git reads as a flag would
        # skip a word git reads as an operand or an option.
        groups = declared(hub_guard.REVISION_OPTIONS) | declared(hub_guard.DIFF_OPTIONS)
        for key, form in hub_guard.READ_ONLY_FORMS.items():
            if not form.listed:
                continue
            listed = help_options(row_help(key))
            for option, kind in declared(form).items():
                if option in groups and groups[option] == kind:
                    continue
                with self.subTest(key=key, option=option):
                    self.assertIn(option, listed)
                    allowed = {"valued": {"valued"}, "attached": {"attached"}, "flag": {"flag", "attached"}}[kind]
                    self.assertIn(listed[option][0], allowed)

    def test_no_row_declares_an_option_git_says_writes_or_runs(self) -> None:
        for key, form in hub_guard.READ_ONLY_FORMS.items():
            subcommand = key.split(" ")[0]
            listed = help_options(row_help(key))
            writing = {
                hub_guard.GIT_OUTPUT,
                *hub_guard.GIT_WRITING_OPTIONS.get(subcommand, ()),
                *running(subcommand),
                *(option for option, (_, description) in listed.items() if WRITING_FLAG.search(description)),
            }
            with self.subTest(key=key):
                self.assertEqual([], sorted(set(declared(form)) & writing))

    def test_no_row_declares_an_option_known_to_write_the_hub_or_follow_what_does(self) -> None:
        # Pinned against literals, not the tables: the options the outside reviews and git's documentation name as
        # moving HEAD, writing the index or tree, setting an upstream, or running a command.
        known = {
            "fetch": {"-u", "--update-head-ok", "--set-upstream", "--refmap", "--upload-pack", "--prefetch", "-f"},
            "pull": {"--rebase", "-r", "--ff", "--no-ff", "--set-upstream", "--autostash", "--squash", "--commit"},
            "merge": {"--ff", "--no-ff", "--squash", "--commit", "--autostash", "--abort", "--continue", "-m"},
            "branch": {"-u", "--set-upstream-to", "--unset-upstream", "-t", "--track", "-m", "-M", "--move"},
            "apply": {"--apply", "--no-check", "--index", "--cached", "-3", "--3way", "--reject", "--unsafe-paths"},
            "reflog": {"--updateref", "--rewrite", "--expire", "--expire-unreachable", "--stale-fix"},
            "worktree add": {"-f", "--force", "-B", "--orphan", "--track"},
            "worktree remove": {"-f", "--force"},
            "switch": {"-c", "-C", "-f", "--force", "-m", "--merge", "--detach", "--discard-changes"},
            "checkout": {"-b", "-B", "-f", "--force", "-m", "--merge", "--detach", "-p", "--patch"},
            "tag": {"-d", "--delete", "-a", "-s", "-f", "-m", "-F"},
            "log": {"--output", "--ext-diff", "--textconv", "--show-signature", "--notes"},
            "diff": {"--output", "--ext-diff", "--textconv", "--no-index"},
            "grep": {"-O", "--open-files-in-pager", "--textconv", "--no-index"},
            "cat-file": {"--textconv", "--filters"},
        }
        for key, options in known.items():
            with self.subTest(key=key):
                self.assertEqual([], sorted(set(declared(hub_guard.READ_ONLY_FORMS[key])) & options))
        self.assertNotIn("reflog delete", hub_guard.READ_ONLY_FORMS)
        self.assertNotIn("reflog expire", hub_guard.READ_ONLY_FORMS)

    def test_every_builtin_no_row_names_is_refused_in_a_hub(self) -> None:
        builtins = subprocess.run(
            ["git", "--list-cmds=builtins"], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True
        ).stdout.split()
        self.assertIn("commit", builtins, "git lists its builtins")
        for name in builtins:
            if name in hub_guard.READ_ONLY_SUBCOMMANDS:
                continue
            with self.subTest(subcommand=name):
                self.assertIsNotNone(hub_guard.read_only_refusal(name, []))
        # Pinned against literals: subcommands that write, and actions of a row's subcommand no row names.
        for words in (
            ["commit"],
            ["reset", "--hard"],
            ["update-ref", "refs/heads/main", "HEAD~1"],
            ["reflog", "delete", "main@{0}"],
            ["reflog", "expire", "--all"],
            ["stash"],
            ["stash", "pop"],
            ["worktree", "move", "a", "b"],
            ["remote", "set-url", "origin", "x"],
            ["submodule", "status"],
            ["notes", "add"],
            ["replace", "HEAD", "HEAD~1"],
        ):
            with self.subTest(words=words):
                self.assertIsNotNone(hub_guard.read_only_refusal(words[0], words[1:]))


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
            ("pack-objects", ["--window", "5", "out"], ["out"]),
            ("pack-objects", ["--win", "5", "out"], ["out"]),
            ("pack-objects", ["--window=5", "out"], ["out"]),
            ("interpret-trailers", ["--in-place", "--trailer", "Reviewed-by: x", "a"], ["a"]),
            ("interpret-trailers", ["--in", "--trailer=Reviewed-by: x", "a", "b"], ["a", "b"]),
            ("interpret-trailers", ["--in-place", "--", "a"], ["a"]),
            ("interpret-trailers", ["--in-place", "--no-in-place", "a"], []),
            ("interpret-trailers", ["--no-in-place", "--in-place", "a"], ["a"]),
            ("interpret-trailers", ["--trailer", "--in-place", "a"], []),
            ("interpret-trailers", ["a"], []),
            ("interpret-trailers", ["--", "--in-place", "a"], []),
            ("mailinfo", ["msg", "patch"], ["msg", "patch"]),
            ("mailinfo", ["-k", "--encoding", "utf-8", "msg", "patch"], ["msg", "patch"]),
            ("merge-file", ["current", "base", "other"], ["current"]),
            ("merge-file", ["-L", "a", "-L", "b", "current", "base", "other"], ["current"]),
            ("merge-file", ["-La", "--marker-size", "9", "current", "base", "other"], ["current"]),
            ("merge-file", ["-qL", "a", "current", "base", "other"], ["current"]),
            ("merge-file", ["-Lp", "current", "base", "other"], ["current"]),
            ("merge-file", ["--", "-p", "base", "other"], ["-p"]),
            ("merge-file", ["-p", "current", "base", "other"], []),
            ("merge-file", ["-qp", "current", "base", "other"], []),
            ("merge-file", ["--stdo", "current", "base", "other"], []),
            ("merge-file", ["--object-id", "current", "base", "other"], []),
            ("merge-file", ["--stdout", "--no-stdout", "current", "base", "other"], ["current"]),
            ("merge-file", [None, "current", "base", "other"], [None]),
            ("checkout-index", ["--prefix=out/", "-a", "-f"], ["out/"]),
            ("checkout-index", ["-a", "--prefix", "out/"], ["out/"]),
            ("checkout-index", ["--pre", "out/", "-a"], ["out/"]),
            ("checkout-index", ["-a", "-f"], []),
            ("apply", ["--directory=out", "p.diff"], ["out"]),
            ("apply", ["--unsafe-paths", "p.diff"], [None]),
            ("apply", ["--unsafe", "p.diff"], [None]),
            ("apply", ["--unsafe-paths", "--no-unsafe-paths", "p.diff"], []),
            ("apply", ["--", "--unsafe-paths"], []),
            ("am", ["--directory", "out", "mail.mbox"], ["out"]),
            ("worktree", ["move", "old", "out"], ["out"]),
            ("worktree", ["move", "-f", "old", "out"], ["out"]),
            ("worktree", ["remove", "old"], []),
            # An option's value spelled as a mode's --no- form, or as a printing option, is that option's value.
            ("apply", ["--unsafe-paths", "--exclude", "--no-unsafe-paths", "p.diff"], [None]),
            ("apply", ["--unsafe-paths", "--exc", "--no-unsafe-paths", "p.diff"], [None]),
            ("apply", ["--unsafe-paths", "-p", "--no-unsafe-paths", "p.diff"], [None]),
            ("apply", ["--unsafe-paths", "-C", "--no-unsafe-paths", "p.diff"], [None]),
            ("apply", ["--unsafe-paths", "--include=--no-unsafe-paths", "p.diff"], [None]),
            ("apply", ["--exclude", "--unsafe-paths", "p.diff"], []),
            ("apply", ["--directory", "--no-unsafe-paths", "--unsafe-paths", "p.diff"], ["--no-unsafe-paths", None]),
            ("interpret-trailers", ["--in-place", "--trailer", "--no-in-place", "a"], ["a"]),
            ("interpret-trailers", ["--in-place", "--where=--no-in-place", "a"], ["a"]),
            ("merge-file", ["-L", "-p", "current", "base", "other"], ["current"]),
            ("merge-file", ["-L", "--stdout", "current", "base", "other"], ["current"]),
            ("merge-file", ["--marker-size", "--stdout", "current", "base", "other"], ["current"]),
            ("merge-file", ["--diff-algorithm=--stdout", "current", "base", "other"], ["current"]),
            ("pack-objects", ["--window", "--stdout", "out"], ["out"]),
            ("format-patch", ["--subject-prefix", "--stdout", "-1"], ["."]),
            ("format-patch", ["--author", "--stdout", "-1"], ["."]),
            ("format-patch", ["--cover-letter", "--stdout"], ["."]),
            ("format-patch", ["-1", "--stdout"], []),
            ("format-patch", ["--stdout", "-1"], []),
            ("format-patch", ["HEAD~2", "--stdout"], []),
            ("format-patch", ["--subject-prefix=x", "--stdout", "-1"], []),
            ("format-patch", ["-1", "--stdout", "--no-stdout"], ["."]),
            ("format-patch", ["-1", "--", "--stdout"], ["."]),
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
