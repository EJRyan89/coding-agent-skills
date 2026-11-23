"""Fixture tests for tests/validation/tree_readers.py.

The policy fails on a reader of a commit's tree its catalogue does not name, on one whose git words give other bytes
than its entry promises, and on an entry that names no reader, and passes on a catalogue that names each reader with
the bytes it gives.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from tree_readers import CHECKOUT, COMMITTED, ENTRIES, tree_reader_problems
from validation_support import MODULE_SCOPE, write_fixture_tree

# A module with one reader of each kind of bytes, each spelled as the repository spells one.
READERS = (
    "import subprocess\n"  # 1
    "from git_client import GitClient\n"  # 2
    "\n"  # 3
    "\n"  # 4
    "def _git(source, *arguments):\n"  # 5
    "    return subprocess.run([GIT, '-C', str(source), *arguments], check=False)\n"  # 6
    "\n"  # 7
    "\n"  # 8
    "def export(source, commit):\n"  # 9
    "    return _git(source, 'archive', '--format=tar', commit)\n"  # 10: a call named for git, a checkout's bytes
    "\n"  # 11
    "\n"  # 12
    "def listing(checkout, commit):\n"  # 13
    "    return GitClient().names(['ls-tree', '-r', '--full-tree', commit], directory=checkout)\n"  # 14: entries
    "\n"  # 15
    "\n"  # 16
    "class Snapshot:\n"  # 17
    "    def read(self, blob):\n"  # 18
    "        reading = ('cat-file', 'blob', blob)\n"  # 19: a tuple bound to a name, the committed bytes
    "        sizing = ('cat-file', '-s', blob)\n"  # 20: the size alone
    "        return self.run(*sizing), self.run(*reading)\n"  # 21
)
CATALOGUE = {"tool.py": {"export": CHECKOUT, "listing": ENTRIES, "Snapshot.read": COMMITTED}}


class TreeReaderFixtures(unittest.TestCase):
    def problems(
        self,
        files: dict[str, str],
        readers: Mapping[str, Mapping[str, str]],
        not_readers: Mapping[tuple[str, ...], str] | None = None,
    ) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, files, newline="\n")
            return tree_reader_problems(root, [root / name for name in files], readers, not_readers or {})

    def test_a_catalogue_naming_each_reader_with_the_bytes_it_gives_passes(self) -> None:
        self.assertEqual([], self.problems({"tool.py": READERS}, CATALOGUE))

    def test_a_reader_the_catalogue_does_not_name_fails(self) -> None:
        problems = self.problems({"tool.py": READERS}, {"tool.py": {"export": CHECKOUT, "listing": ENTRIES}})
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("tool.py:19 Snapshot.read reads a commit's tree with git cat-file,"))
        self.assertIn("TREE_READERS in tests/validation/tree_readers.py does not name", problems[0])
        self.assertIn("the row on readers of a commit's tree in docs/design.md's Decisions", problems[0])

    def test_a_reader_whose_words_give_other_bytes_than_its_entry_promises_fails(self) -> None:
        # The snapshot reader moved to git archive would hand its callers a checkout's bytes.
        moved = READERS.replace("('cat-file', 'blob', blob)", "('archive', '--format=tar', blob)")
        problems = self.problems({"tool.py": moved}, CATALOGUE)
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("tool.py:19 Snapshot.read reads checkout bytes of a commit's tree, "))
        self.assertIn("promises committed", problems[0])

    def test_a_cat_file_mode_that_applies_filters_gives_a_checkout_s_bytes(self) -> None:
        converted = READERS.replace("('cat-file', 'blob', blob)", "('cat-file', '--filters', blob)")
        problems = self.problems({"tool.py": converted}, {"tool.py": {**CATALOGUE["tool.py"]}})
        self.assertEqual(1, len(problems), problems)
        self.assertIn("Snapshot.read reads checkout bytes", problems[0])
        self.assertEqual(
            [], self.problems({"tool.py": converted}, {"tool.py": {**CATALOGUE["tool.py"], "Snapshot.read": CHECKOUT}})
        )

    def test_a_scope_that_gives_both_a_checkout_s_and_the_committed_bytes_fails(self) -> None:
        both = READERS.replace("('cat-file', '-s', blob)", "('archive', blob)")
        problems = self.problems({"tool.py": both}, CATALOGUE)
        self.assertEqual(1, len(problems), problems)
        self.assertIn("Snapshot.read reads both a checkout's bytes and the committed bytes", problems[0])

    def test_an_entry_that_names_no_reader_and_a_promise_of_no_kind_fail(self) -> None:
        readers = {"tool.py": {**CATALOGUE["tool.py"], "gone": ENTRIES}, "other.py": {"listing": "bytes"}}
        not_readers: dict[tuple[str, ...], str] = {("archive", "folders"): "names folders", ("ls-tree", "x"): " "}
        problems = self.problems({"tool.py": READERS}, readers, not_readers)
        self.assertEqual(
            [
                "tests/validation/tree_readers.py: TREE_READERS names other.py listing, which reads no commit's tree; "
                "remove the entry",
                "tests/validation/tree_readers.py: TREE_READERS names tool.py gone, which reads no commit's tree; "
                "remove the entry",
                "tests/validation/tree_readers.py: NOT_READERS names the words ('archive', 'folders'), which no "
                "sequence spells; remove the entry",
                "tests/validation/tree_readers.py: NOT_READERS names the words ('ls-tree', 'x'), which no sequence "
                "spells; remove the entry",
                "tests/validation/tree_readers.py: NOT_READERS gives the words ('ls-tree', 'x') no reason; state why "
                "they never reach git",
                "tests/validation/tree_readers.py: TREE_READERS promises 'bytes' for other.py listing, which PROMISES "
                "does not name",
            ],
            problems,
        )

    def test_words_that_never_reach_git_pass_only_when_they_are_exactly_a_not_readers_entry(self) -> None:
        excused: dict[tuple[str, ...], str] = {("archive", "checkout"): "a fixture's folders"}
        files = {"scenarios.py": "FOLDERS = ('archive', 'checkout')\n"}
        self.assertIn("scenarios.py:1 FOLDERS reads a commit's tree with git archive", self.problems(files, {})[0])
        self.assertEqual([], self.problems(files, {}, excused))
        for words in ("('archive', 'HEAD')", "('archive', 'checkout', 'x')"):
            with self.subTest(words=words):
                problems = self.problems({"scenarios.py": f"FOLDERS = {words}\n"}, {}, excused)
                self.assertEqual(2, len(problems), problems)
                self.assertIn("scenarios.py:1 FOLDERS reads a commit's tree with git archive", problems[0])
                self.assertIn("NOT_READERS names the words ('archive', 'checkout'), which no sequence", problems[1])

    def test_an_excused_sequence_is_passed_over_wherever_it_sits_as_its_stated_limit_says(self) -> None:
        excused: dict[tuple[str, ...], str] = {("archive", "checkout"): "a fixture's folders"}
        files = {
            "tool.py": (
                "def export(git):\n    return git.output(['archive', 'checkout']), git_output('archive', 'checkout')\n"
            )
        }
        self.assertEqual([], self.problems(files, {}, excused))

    def test_each_spelling_that_reaches_git_is_read(self) -> None:
        files = {
            "spellings.py": (
                "import os\n"  # 1
                "import subprocess as run_module\n"  # 2
                "\n"  # 3
                "\n"  # 4
                "def program_first(commit):\n"  # 5
                "    return run_module.run(['/usr/bin/git', 'cat-file', 'blob', commit])\n"  # 6
                "\n"  # 7
                "\n"  # 8
                "def options_first(git, checkout, commit):\n"  # 9
                "    return run_module.run([git, '-C', checkout, 'ls-tree', commit])\n"  # 10
                "\n"  # 11
                "\n"  # 12
                "def through_a_shell(commit):\n"  # 13
                "    run_module.run(f'git archive {commit}', shell=True)\n"  # 14: a formatted string is not read
                "    return run_module.run('git archive HEAD > tree.tar', shell=True)\n"  # 15
                "\n"  # 16
                "\n"  # 17
                "def system():\n"  # 18
                "    return os.system('git ls-tree HEAD')\n"  # 19
                "\n"  # 20
                "\n"  # 21
                "def committed(client, checkout):\n"  # 22
                "    return client.file_at('HEAD', 'README.md', directory=checkout)\n"  # 23
            ),
            "scripts/export.sh": '#!/usr/bin/env bash\ngit -C "$1" archive HEAD\n',
        }
        problems = self.problems(files, {})
        self.assertEqual(
            [
                ("scripts/export.sh:1 <module>", "git archive"),
                ("spellings.py:6 program_first", "git cat-file"),
                ("spellings.py:10 options_first", "git ls-tree"),
                ("spellings.py:15 through_a_shell", "git archive"),
                ("spellings.py:19 system", "git ls-tree"),
                ("spellings.py:23 committed", "git cat-file"),
            ],
            [(problem.split(" reads ")[0], problem.split(" with ")[1].split(",")[0]) for problem in problems],
        )

    def test_a_call_named_for_no_git_and_words_no_reader_runs_are_not_read(self) -> None:
        files = {
            "paths.py": (
                "from pathlib import Path\n"
                "\n"
                "\n"
                "def folders(root, commit, git):\n"
                "    git.output(['rev-parse', commit])\n"  # another subcommand
                "    git.output(['-C', root, 'log', 'archive'])\n"  # a reader's name as a later word
                "    return Path(root, 'archive'), print('ls-tree', commit)\n"  # calls that name no git
            ),
        }
        self.assertEqual([], self.problems(files, {}))

    def test_each_cat_file_show_and_grep_option_gives_the_bytes_the_policy_names(self) -> None:
        rows = {
            "'cat-file', '--filters', blob": CHECKOUT,
            "'cat-file', '--textconv', blob": CHECKOUT,
            "'cat-file', '-e', blob": ENTRIES,
            "'cat-file', '-s', blob": ENTRIES,
            "'cat-file', '-t', blob": ENTRIES,
            "'cat-file', '--batch-check=%(objectsize)'": ENTRIES,
            "'cat-file', '-p', blob": COMMITTED,
            "'show', blob": COMMITTED,
            "'show', '--textconv', blob": CHECKOUT,
            "'grep', '-z', '-e', 'x', blob": COMMITTED,
            "'grep', '--textconv', '-e', 'x', blob": CHECKOUT,
        }
        for words, given in rows.items():
            module = {"tool.py": f"def read(git, blob):\n    return git.output([{words}])\n"}
            with self.subTest(words=words):
                self.assertEqual([], self.problems(module, {"tool.py": {"read": given}}))
                wrong = COMMITTED if given != COMMITTED else ENTRIES
                self.assertEqual(1, len(self.problems(module, {"tool.py": {"read": wrong}})))

    def test_each_stated_limit_reads_as_its_docstring_says(self) -> None:
        # Each passes unread, or, for the cat-file mode only a value names and a script's git read by its subcommand
        # alone, passes with the committed bytes its entry names.
        python = "import shlex\nimport subprocess\n\n\ndef limit(git, client, verb, mode, commit):\n    {}\n"
        rows = {
            "a subcommand only a value names": ("py", "git.output([verb, commit])", None),
            "a cat-file mode only a value names": ("py", "git.output(['cat-file', mode, commit])", COMMITTED),
            "a string literal before the subcommand": ("py", "git_output('/repo', 'cat-file', 'blob', commit)", None),
            "a call named for no git": ("py", "runner('archive', commit)", None),
            "a file_at bound to a name": ("py", "read = client.file_at", None),
            "one string without a shell": ("py", "subprocess.run('git archive HEAD')", None),
            "a string split by str.split": ("py", "subprocess.run('git archive HEAD'.split())", None),
            "a string split by shlex.split": ("py", "subprocess.run(shlex.split('git archive HEAD'))", None),
            "a formatted shell command": ("py", "subprocess.run(f'git archive {commit}', shell=True)", None),
            "a shell's -c script in a list": ("py", "subprocess.run(['bash', '-c', 'git archive HEAD'])", None),
            "a shell command's mode": ("py", "subprocess.run('git cat-file -s HEAD:a', shell=True)", COMMITTED),
            "a script's mode": ("sh", "git cat-file --filters HEAD:a", COMMITTED),
            "a shell function": ("sh", 'g() { git -C x "$@"; }\ng archive HEAD', None),
            "a shell array": ("sh", 'cmd=(git archive HEAD)\n"${cmd[@]}"', None),
            "a program a variable names": ("sh", '"$GIT" archive HEAD', None),
            "a PowerShell splat": ("ps1", "function g { & git @args }\ng archive HEAD", None),
            "a working tree written from a commit": ("sh", "git worktree add --detach x HEAD\ngit checkout HEAD", None),
        }
        for label, (kind, code, given) in rows.items():
            name = f"limit.{kind}"
            text = python.format(code) if kind == "py" else f"{code}\n"
            if kind == "sh":
                text = f"#!/usr/bin/env bash\n{text}"
            readers = {} if given is None else {name: {MODULE_SCOPE if kind != "py" else "limit": given}}
            with self.subTest(label):
                self.assertEqual([], self.problems({name: text}, readers))

    def test_a_script_whose_git_cannot_be_read_is_named(self) -> None:
        self.assertEqual(
            ["broken.sh: cannot read the git it runs: not Bash the grammar reads (expected `fi`)"],
            self.problems({"broken.sh": "#!/usr/bin/env bash\nif then\n"}, {}),
        )

    def test_suites_and_fixtures_are_not_read(self) -> None:
        reader = "def listing(git, commit):\n    return git.output(['ls-tree', commit])\n"
        files = {"test_tool.py": reader, "tests/fixtures/tool.py": reader, "conftest.py": reader}
        self.assertEqual([], self.problems(files, {}))
