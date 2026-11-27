"""Fixture tests for tests/validation/tree_readers.py.

The policy fails on a reader of a commit's tree its catalogue does not name, through git or a GitHub endpoint, on one
whose reads give other bytes than its entry promises, on an archive whose scope no longer calls the check that holds it
to the committed bytes, and on an entry that names no reader, and passes on a catalogue that names each reader with the
bytes it gives.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from tree_readers import CHECKOUT, COMMITTED, ENTRIES, GITHUB_READS, tree_reader_problems
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
# A module that reads a commit's tree through GitHub's API as the repository does: a tarball held to the committed
# bytes by a check, a file through the contents and blobs endpoints, a tree's entries, and an endpoint a module constant
# holds.
GITHUB = (
    "from github_client import GitHubClient\n"  # 1
    "\n"  # 2
    "CONFIG = f'repos/{{owner}}/{{repo}}/contents/{NAME}?ref=main'\n"  # 3
    "\n"  # 4
    "\n"  # 5
    "def verify_tarball(target, tree):\n"  # 6
    "    return None\n"  # 7
    "\n"  # 8
    "\n"  # 9
    "def fetch(client, repository, commit, target):\n"  # 10
    "    client.download(['api', f'repos/{repository}/tarball/{commit}'], target)\n"  # 11
    "    tree = client.json(['api', f'repos/{repository}/git/trees/{commit}?recursive=1'])\n"  # 12
    "    verify_tarball(target, tree)\n"  # 13
    "\n"  # 14
    "\n"  # 15
    "class Client:\n"  # 16
    "    def blob(self, repository, commit, path):\n"  # 17
    "        entry = self.api_json(f'repos/{repository}/contents/{path}?ref={commit}')\n"  # 18
    "        return self.api_json(f\"repos/{repository}/git/blobs/{entry['sha']}\")\n"  # 19
    "\n"  # 20
    "    def modes(self, repository, sha):\n"  # 21
    "        return self._evidence(f'repos/{repository}/git/trees/{sha}?recursive=1')\n"  # 22
)
GITHUB_CATALOGUE = {
    "github.py": {"CONFIG": COMMITTED, "fetch": COMMITTED, "Client.blob": COMMITTED, "Client.modes": ENTRIES}
}
GITHUB_CHECKS = {"github.py": {"fetch": "verify_tarball"}}


class TreeReaderCase(unittest.TestCase):
    """Runs the policy over fixture files written to a temporary root."""

    def problems(
        self,
        files: dict[str, str],
        readers: Mapping[str, Mapping[str, str]],
        not_readers: Mapping[tuple[str, ...], str] | None = None,
        checks: Mapping[str, Mapping[str, str]] | None = None,
    ) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, files, newline="\n")
            return tree_reader_problems(root, [root / name for name in files], readers, not_readers or {}, checks or {})


class TreeReaderFixtures(TreeReaderCase):
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
        for words in ("('archive', 'HEAD')", "('archive', 'checkout', 'x')", "('archive', 'checkout', name)"):
            with self.subTest(words=words):
                problems = self.problems({"scenarios.py": f"FOLDERS = {words}\n"}, {}, excused)
                self.assertEqual(2, len(problems), problems)
                self.assertIn("scenarios.py:1 FOLDERS reads a commit's tree with git archive", problems[0])
                self.assertIn("NOT_READERS names the words ('archive', 'checkout'), which no sequence", problems[1])

    def test_the_policy_s_own_not_readers_keys_never_excuse_themselves(self) -> None:
        excused: dict[tuple[str, ...], str] = {("archive", "checkout"): "a fixture's folders"}
        policy = {
            "tests/validation/tree_readers.py": "NOT_READERS = {('archive', 'checkout'): \"a fixture's folders\"}\n"
        }
        self.assertEqual(
            [
                "tests/validation/tree_readers.py: NOT_READERS names the words ('archive', 'checkout'), which no "
                "sequence spells; remove the entry"
            ],
            self.problems(policy, {}, excused),
        )
        self.assertEqual(
            [], self.problems({**policy, "scenarios.py": "FOLDERS = ('archive', 'checkout')\n"}, {}, excused)
        )
        name = "tests/validation/tree_readers.py"
        assignment = policy[name]
        with self.subTest("the rest of the policy module is still read"):
            self.assertEqual([], self.problems({name: f"{assignment}WORDS = ('archive', 'checkout')\n"}, {}, excused))
            reader = f"{assignment}\n\ndef export(git):\n    return git.output(['archive', 'HEAD'])\n"
            problems = self.problems({name: reader}, {}, excused)
            self.assertEqual(2, len(problems), problems)
            self.assertTrue(problems[0].startswith(f"{name}:5 export reads a commit's tree with git archive,"))
        with self.subTest("an entry added in another statement excuses itself, as its stated limit says"):
            for addition in (
                "NOT_READERS[('archive', 'checkout')] = \"a fixture's folders\"\n",
                "NOT_READERS.update({('archive', 'checkout'): \"a fixture's folders\"})\n",
            ):
                self.assertEqual([], self.problems({name: f"NOT_READERS = {{}}\n{addition}"}, {}, excused))

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
            "a branch switched or files restored": ("sh", "git switch main\ngit restore --source HEAD -- .", None),
            "a command in a Markdown fence": ("md", "```bash\ngit archive HEAD\n```", None),
            "a grep given --no-index": ("py", "git.output(['grep', '--no-index', '-e', 'x'])", COMMITTED),
            "a grep given no commit": ("py", "git.output(['grep', '-e', 'x'])", COMMITTED),
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

    def test_a_python_shell_command_the_grammar_refuses_is_named(self) -> None:
        tool = 'import subprocess\n\n\ndef run():\n    subprocess.run("echo \'unclosed", shell=True)\n'
        self.assertEqual(
            ["tool.py:5: cannot read the git it runs: not Bash the grammar reads (a single quote is not closed)"],
            self.problems({"tool.py": tool}, {}),
        )

    def test_suites_and_fixtures_are_not_read(self) -> None:
        reader = "def listing(git, commit):\n    return git.output(['ls-tree', commit])\n"
        files = {"test_tool.py": reader, "tests/fixtures/tool.py": reader, "conftest.py": reader}
        self.assertEqual([], self.problems(files, {}))


class GitHubTreeReaderFixtures(TreeReaderCase):
    """GitHub's endpoints that read a commit's tree, read from the text each string expression renders."""

    def test_a_catalogue_naming_each_github_reader_with_the_bytes_it_gives_passes(self) -> None:
        self.assertEqual([], self.problems({"github.py": GITHUB}, GITHUB_CATALOGUE, checks=GITHUB_CHECKS))

    def test_a_github_reader_the_catalogue_does_not_name_fails(self) -> None:
        readers = {"github.py": {**GITHUB_CATALOGUE["github.py"]}}
        del readers["github.py"]["Client.modes"]
        problems = self.problems({"github.py": GITHUB}, readers, checks=GITHUB_CHECKS)
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("github.py:22 Client.modes reads a commit's tree with GitHub's trees "))
        self.assertIn("TREE_READERS in tests/validation/tree_readers.py does not name", problems[0])
        self.assertNotIn("NOT_READERS", problems[0])

    def test_each_github_endpoint_gives_the_bytes_the_policy_names(self) -> None:
        # Each fails unnamed, so a kind dropped from the endpoints the policy reads fails here.
        rows = {
            "f'repos/{repository}/tarball/{commit}'": ("GitHub's tarball endpoint", CHECKOUT),
            "f'repos/{repository}/zipball/{commit}'": ("GitHub's zipball endpoint", CHECKOUT),
            "f'https://github.com/{owner}/{name}/archive/{commit}.tar.gz'": ("GitHub's archive download", CHECKOUT),
            "f'https://codeload.github.com/{repository}/tar.gz/{commit}'": ("GitHub's archive download", CHECKOUT),
            "f'repos/{repository}/contents/{path}?ref={commit}'": ("GitHub's contents endpoint", COMMITTED),
            "f'repos/{repository}/readme?ref={commit}'": ("GitHub's readme endpoint", COMMITTED),
            "f'repos/{repository}/git/blobs/{sha}'": ("GitHub's blobs endpoint", COMMITTED),
            "f'https://raw.githubusercontent.com/{repository}/{commit}/{path}'": ("GitHub's raw download", COMMITTED),
            "f'https://github.com/{owner}/{name}/raw/{commit}/{path}'": ("GitHub's raw download", COMMITTED),
            "f'repos/{repository}/git/trees/{commit}'": ("GitHub's trees endpoint", ENTRIES),
        }
        self.assertEqual({how for how, _ in rows.values()}, {how for how, _ in GITHUB_READS.values()})
        for endpoint, (how, given) in rows.items():
            module = {
                "tool.py": f"def read(client, repository, owner, name, commit, path, sha):\n    return {endpoint}\n"
            }
            with self.subTest(endpoint=endpoint):
                self.assertEqual([], self.problems(module, {"tool.py": {"read": given}}))
                wrong = COMMITTED if given != COMMITTED else ENTRIES
                self.assertEqual(1, len(self.problems(module, {"tool.py": {"read": wrong}})))
                unnamed = self.problems(module, {})
                self.assertEqual(1, len(unnamed), unnamed)
                self.assertIn(f"tool.py:2 read reads a commit's tree with {how}, which TREE_READERS", unnamed[0])

    def test_each_spelling_of_a_github_endpoint_is_read(self) -> None:
        rows = {
            "an f-string after a repository of one word": "f'repos/{repository}/contents/{path}'",
            "an f-string after an owner and a name": "f'repos/{owner}/{name}/contents/{path}'",
            "a string literal": "'repos/o/r/contents/README.md'",
            "a URL": "f'https://api.github.com/repos/{repository}/contents/{path}'",
            "a % of a literal": "'repos/%s/contents/%s' % (repository, path)",
            "a + of literals": "'repos/' + repository + '/contents/' + path",
            "a .format of a literal": "'repos/{}/contents/{}'.format(repository, path)",
            "an item of gh api's words": "['gh', 'api', '-H', 'Accept: x', f'repos/{repository}/contents/{path}']",
            "a query and no path": "f'repos/{repository}/contents?ref={commit}'",
        }
        for label, endpoint in rows.items():
            module = {"tool.py": f"def read(repository, owner, name, path, commit):\n    return {endpoint}\n"}
            with self.subTest(label):
                self.assertEqual([], self.problems(module, {"tool.py": {"read": COMMITTED}}))
                problems = self.problems(module, {})
                self.assertEqual(1, len(problems), problems)
                self.assertIn("read reads a commit's tree with GitHub's contents endpoint", problems[0])

    def test_paths_that_name_no_tree_reading_endpoint_are_not_read(self) -> None:
        rows = (
            "f'repos/{repository}/pulls/{number}/files'",
            "f'repos/{repository}/compare/{base}...{head}'",
            "f'repos/{repository}/commits/{sha}/contents'",
            "'raw.githubusercontent.com'",
            "f'myrepos/{repository}/tarball/{commit}'",
            "f'repos/{repository}/tarballs/{commit}'",
        )
        for endpoint in rows:
            module = {"tool.py": f"def read(repository, number, base, head, sha, commit):\n    return {endpoint}\n"}
            with self.subTest(endpoint=endpoint):
                self.assertEqual([], self.problems(module, {}))

    def test_an_archive_gives_the_committed_bytes_only_in_a_scope_that_calls_its_check(self) -> None:
        unchecked = GITHUB.replace("    verify_tarball(target, tree)\n", "    return tree\n")
        problems = self.problems({"github.py": unchecked}, GITHUB_CATALOGUE, checks=GITHUB_CHECKS)
        self.assertEqual(2, len(problems), problems)
        self.assertTrue(problems[0].startswith("github.py:11 fetch reads checkout bytes of a commit's tree, "))
        self.assertIn("promises committed", problems[0])
        self.assertTrue(problems[1].startswith("github.py fetch does not call 'verify_tarball' by its name, "))
        with self.subTest("an archive with no check named gives a checkout's bytes"):
            problems = self.problems({"github.py": GITHUB}, GITHUB_CATALOGUE)
            self.assertEqual(1, len(problems), problems)
            self.assertIn("fetch reads checkout bytes of a commit's tree", problems[0])
            readers = {"github.py": {**GITHUB_CATALOGUE["github.py"], "fetch": CHECKOUT}}
            self.assertEqual([], self.problems({"github.py": GITHUB}, readers))
        with self.subTest("git archive is held by its check too"):
            module = "def check(target):\n    pass\n\n\ndef export(git, target):\n    git.output(['archive', 'HEAD'])\n"
            readers = {"tool.py": {"export": COMMITTED}}
            checks = {"tool.py": {"export": "check"}}
            self.assertEqual(2, len(self.problems({"tool.py": module}, readers, checks=checks)))
            checked = f"{module}    check(target)\n"
            self.assertEqual([], self.problems({"tool.py": checked}, readers, checks=checks))

    def test_a_check_its_module_does_not_define_or_calls_by_another_name_does_not_hold_the_archive(self) -> None:
        rows = {
            "a check imported from another module": GITHUB.replace(
                "def verify_tarball(target, tree):\n    return None\n", "from checks import verify_tarball\n"
            ),
            "a check called as a method": GITHUB.replace("    verify_tarball(target", "    self.verify_tarball(target"),
            "a check called in another scope": GITHUB.replace(
                "    verify_tarball(target, tree)\n", "    return tree\n"
            ).replace("    return None\n", "    return verify_tarball(target, tree)\n"),
        }
        for label, module in rows.items():
            with self.subTest(label):
                problems = self.problems({"github.py": module}, GITHUB_CATALOGUE, checks=GITHUB_CHECKS)
                self.assertEqual(2, len(problems), problems)
                self.assertIn("fetch does not call 'verify_tarball' by its name", problems[1])

    def test_a_check_named_for_a_scope_that_reads_no_archive_fails(self) -> None:
        checks = {"github.py": {**GITHUB_CHECKS["github.py"], "Client.modes": "verify_tarball", "gone": "check"}}
        self.assertEqual(
            [
                "tests/validation/tree_readers.py: CHECKED_READERS names github.py Client.modes, which reads no "
                "archive of a commit's tree; remove the entry",
                "tests/validation/tree_readers.py: CHECKED_READERS names github.py gone, which reads no archive of a "
                "commit's tree; remove the entry",
            ],
            self.problems({"github.py": GITHUB}, GITHUB_CATALOGUE, checks=checks),
        )

    def test_the_policy_s_own_text_is_read_for_endpoints(self) -> None:
        name = "tests/validation/tree_readers.py"
        policy = "NOT_READERS = {}\nENDPOINT = 'repos/o/r/tarball/HEAD'\n"
        problems = self.problems({name: policy}, {})
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith(f"{name}:2 ENDPOINT reads a commit's tree with GitHub's tarball "))
        with self.subTest("a NOT_READERS entry never excuses an endpoint"):
            words = ("repos/o/r/tarball/HEAD",)
            problems = self.problems({"tool.py": f"ENDPOINT = {words!r}\n"}, {}, {words: "never reaches GitHub"})
            self.assertEqual(1, len(problems), problems)
            self.assertTrue(problems[0].startswith("tool.py:1 ENDPOINT reads a commit's tree with GitHub's tarball "))

    def test_each_github_stated_limit_reads_as_its_docstring_says(self) -> None:
        # Each passes unread, or with the bytes its entry names.
        python = "import subprocess\n\n\ndef limit(client, repository, kind, commit, path):\n    {}\n"
        rows = {
            "an endpoint read where a constant holds it": (
                "py",
                "return client.json(['api', ENDPOINT])\n\n\nENDPOINT = 'repos/o/r/contents/a'",
                {"ENDPOINT": COMMITTED},
            ),
            "an endpoint held in bytes": ("py", "return b'repos/o/r/tarball/HEAD'", None),
            "an endpoint joined by str.join": ("py", "return '/'.join(['repos', repository, 'tarball', commit])", None),
            "an endpoint a helper builds": ("py", "return endpoint(repository, 'tarball', commit)", None),
            "a kind only a value names": ("py", "return f'repos/{repository}/{kind}/{commit}'", None),
            "a contents read of a folder": ("py", "return f'repos/{repository}/contents/skills'", {"limit": COMMITTED}),
            "a gh release download": ("py", "subprocess.run(['gh', 'release', 'download', '--archive=zip'])", None),
            "a gh repo clone": ("py", "subprocess.run(['gh', 'repo', 'clone', repository])", None),
            "an endpoint in a Bash script": ("sh", "gh api repos/o/r/tarball/HEAD > tree.tar", None),
            "an endpoint in a PowerShell script": ("ps1", "gh api repos/o/r/tarball/HEAD", None),
            "an endpoint in a Markdown fence": ("md", "```bash\ngh api repos/o/r/tarball/HEAD\n```", None),
            "a repository path a name holds": ("py", "return f'{base}/contents/{path}'", None),
            "a repository path a name holds, then a +": ("py", "return base + f'/contents/{path}'", None),
            "a file read through GraphQL": (
                "py",
                "return client.json(['api', 'graphql', '-f', 'query=query($e: String!) "
                "{ repository(owner: $o, name: $r) { object(expression: $e) { ... on Blob { text } } } }'])",
                None,
            ),
            "a file read through a blob page's raw view": (
                "py",
                "return f'https://github.com/{repository}/blob/{commit}/{path}?raw=true'",
                None,
            ),
        }
        for label, (kind, code, scopes) in rows.items():
            name = f"limit.{kind}"
            text = python.format(code) if kind == "py" else f"{code}\n"
            if kind == "sh":
                text = f"#!/usr/bin/env bash\n{text}"
            with self.subTest(label):
                self.assertEqual([], self.problems({name: text}, {} if scopes is None else {name: scopes}))
        call = "    verify_tarball(target, tree)\n"
        download = "    client.download(['api', f'repos/{repository}/tarball/{commit}'], target)\n"
        checks = {
            "a check given other values": GITHUB.replace(call, "    verify_tarball(None, None)\n"),
            "a check in a branch that never runs": GITHUB.replace(
                call, "    if False:\n        verify_tarball(target, tree)\n"
            ),
            "a check before the download": GITHUB.replace(call, "").replace(download, call + download),
            "a second archive beside the one it checks": GITHUB.replace(
                download, download + "    client.download(['api', f'repos/{repository}/zipball/{commit}'], target)\n"
            ),
        }
        for label, module in checks.items():
            with self.subTest(f"a check is held by the name its scope calls: {label}"):
                self.assertNotEqual(GITHUB, module)
                self.assertEqual([], self.problems({"github.py": module}, GITHUB_CATALOGUE, checks=GITHUB_CHECKS))
