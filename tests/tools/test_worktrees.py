"""Regression suite for tools/worktrees.py: the hub guard hook, worktree creation, and the fleet survey."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "tools" / "worktrees.py"
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools import worktrees

HUB = "Repo With Spaces"
TREE = Path(".claude") / "worktrees" / "feat-demo"


def isolated_environment(root: Path) -> dict[str, str]:
    """Git settings that ignore the developer's own configuration, with a fixed author."""
    empty = root / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(empty),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }


def run_git(cwd: Path, environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *arguments], capture_output=True, text=True, env=environment, check=False
    )


def run_worktrees(
    arguments: list[str], cwd: Path, environment: dict[str, str], stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *arguments],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )


def build_hub(hub: Path, environment: dict[str, str]) -> None:
    """A hub on main with one commit, the guard enabled, and a feat/demo task worktree made by `new`."""
    hub.mkdir()
    run_git(hub, environment, "init", "-q", "-b", "main").check_returncode()
    (hub / "README.md").write_text("hub\n", encoding="utf-8")
    (hub / ".gitignore").write_text("local.json\n.claude/worktrees/\n", encoding="utf-8")
    for arguments in (["add", "."], ["commit", "-q", "-m", "initial"]):
        run_git(hub, environment, *arguments).check_returncode()
    created = run_worktrees(["new", "feat", "demo"], cwd=hub, environment=environment)
    if created.returncode != 0:
        raise AssertionError(created.stderr)
    run_git(hub, environment, "config", "coding-agent-skills.hubGuard", "true").check_returncode()


# Building the hub starts about eight git processes and a Python one, so each process builds it once and every test
# gets a copy. The hub and its worktree name each other by absolute path, so `git worktree repair` reconnects them.
_TEMPLATE_DIRECTORY = tempfile.TemporaryDirectory(prefix="worktrees-template.")
_TEMPLATE_LOCK = threading.Lock()
_TEMPLATE_BUILT = False


def copy_hub(hub: Path, environment: dict[str, str]) -> None:
    global _TEMPLATE_BUILT
    root = Path(_TEMPLATE_DIRECTORY.name).resolve()
    with _TEMPLATE_LOCK:
        if not _TEMPLATE_BUILT:
            build_hub(root / HUB, isolated_environment(root))
            _TEMPLATE_BUILT = True
    shutil.copytree(root / HUB, hub, symlinks=True)
    repaired = run_git(hub, environment, "worktree", "repair", str(hub / TREE))
    if repaired.returncode != 0:
        raise AssertionError(repaired.stderr)


class WorktreesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="worktrees-test.")
        self.root = Path(self._temporary.name).resolve()
        self.environment = isolated_environment(self.root)
        self.hub = self.root / HUB
        copy_hub(self.hub, self.environment)
        self.worktrees = self.hub / ".claude" / "worktrees"
        self.tree = self.hub / TREE

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def git(self, cwd: Path, *arguments: str) -> str:
        result = run_git(cwd, self.environment, *arguments)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def enable(self, value: str | None) -> None:
        if value is None:
            subprocess.run(
                ["git", "-C", str(self.hub), "config", "--unset", "coding-agent-skills.hubGuard"],
                env=self.environment,
                check=False,
            )
        else:
            self.git(self.hub, "config", "coding-agent-skills.hubGuard", value)

    def run_script(
        self, arguments: list[str], cwd: Path, stdin: str = "", environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return run_worktrees(arguments, cwd, environment or self.environment, stdin)

    def decision(self, event: dict[str, object] | str, environment: dict[str, str] | None = None) -> str | None:
        stdin = event if isinstance(event, str) else json.dumps(event)
        result = self.run_script(["guard"], cwd=self.root, stdin=stdin, environment=environment)
        self.assertEqual(0, result.returncode, result.stderr)
        self.stderr = result.stderr
        if not result.stdout.strip():
            return None
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual("PreToolUse", output["hookEventName"])
        self.assertIn("tools/worktrees.py new", output["permissionDecisionReason"])
        return output["permissionDecision"]

    def edit(self, path: Path | str, cwd: Path | None = None, tool: str = "Edit") -> str | None:
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        return self.decision({"tool_name": tool, "tool_input": {key: str(path)}, "cwd": str(cwd or self.root)})

    def bash(self, command: str, cwd: Path) -> str | None:
        return self.decision({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)})

    def powershell(self, command: str, cwd: Path, environment: dict[str, str] | None = None) -> str | None:
        event = {"tool_name": "PowerShell", "tool_input": {"command": command}, "cwd": str(cwd)}
        return self.decision(event, environment)


class FixtureTemplateTests(unittest.TestCase):
    def test_a_copied_hub_matches_a_freshly_built_one_and_its_worktree_is_its_own(self) -> None:
        def shape(root: Path, environment: dict[str, str]) -> dict[str, str]:
            hub, tree = root / HUB, root / HUB / TREE

            def output(cwd: Path, *arguments: str) -> str:
                result = run_git(cwd, environment, *arguments)
                self.assertEqual(0, result.returncode, result.stderr)
                return result.stdout.replace(root.as_posix(), "<root>").replace(str(root), "<root>")

            return {
                "worktrees": output(hub, "worktree", "list", "--porcelain").replace(
                    output(hub, "rev-parse", "HEAD").strip(), "<head>"
                ),
                "refs": output(hub, "for-each-ref", "--format=%(refname) %(tree) %(subject)"),
                "config": output(hub, "config", "--local", "--list"),
                "tree common directory": output(tree, "rev-parse", "--path-format=absolute", "--git-common-dir"),
                "tree status": output(tree, "status", "--porcelain", "--branch"),
                "hub status": output(hub, "status", "--porcelain", "--branch"),
            }

        with tempfile.TemporaryDirectory() as fresh_directory, tempfile.TemporaryDirectory() as copy_directory:
            fresh, copied = Path(fresh_directory).resolve(), Path(copy_directory).resolve()
            fresh_environment, copied_environment = isolated_environment(fresh), isolated_environment(copied)
            build_hub(fresh / HUB, fresh_environment)
            copy_hub(copied / HUB, copied_environment)
            copied_shape = shape(copied, copied_environment)
            self.assertEqual(shape(fresh, fresh_environment), copied_shape)
            self.assertEqual("<root>/Repo With Spaces/.git\n", copied_shape["tree common directory"])
            template = Path(_TEMPLATE_DIRECTORY.name).resolve()
            for record in (copied / HUB / TREE / ".git", *(copied / HUB / ".git" / "worktrees").rglob("gitdir")):
                text = record.read_text(encoding="utf-8")
                self.assertNotIn(template.as_posix(), text)
                self.assertIn(copied.as_posix(), text)


class FileGuardTests(WorktreesTestCase):
    def test_edits_inside_the_hub_are_denied(self) -> None:
        for tool, path, cwd in (
            ("Edit", self.hub / "README.md", None),
            ("Write", self.hub / "new" / "nested" / "file.txt", None),
            ("MultiEdit", self.hub / "README.md", None),
            ("NotebookEdit", self.hub / "notebook.ipynb", None),
            ("Edit", "README.md", self.hub),
            ("Edit", self.hub / "README.md", self.tree),
        ):
            with self.subTest(tool=tool, path=str(path), cwd=str(cwd)):
                self.assertEqual("deny", self.edit(path, cwd, tool))

    def test_edits_in_a_nested_worktree_outside_the_repository_or_to_ignored_hub_files_are_allowed(self) -> None:
        for path in (
            self.tree / "README.md",
            self.tree / "new.txt",
            self.worktrees / "not-a-worktree" / "file.txt",
            self.root / "elsewhere.txt",
            self.hub / "local.json",
        ):
            with self.subTest(path=str(path)):
                self.assertIsNone(self.edit(path))

    def test_git_bash_paths_are_understood(self) -> None:
        drive, rest = str(self.hub / "README.md").replace("\\", "/").split(":", 1)
        self.assertEqual("deny", self.edit(f"/{drive.lower()}{rest}"))

    def test_guard_is_inert_unless_this_clone_opts_in(self) -> None:
        for value in (None, "false"):
            with self.subTest(value=value):
                self.enable(value)
                self.assertIsNone(self.edit(self.hub / "README.md"))
                self.assertIsNone(self.bash("git switch feat/demo", self.hub))


class CommandGuardTests(WorktreesTestCase):
    def test_commands_that_move_or_write_the_hub_are_denied(self) -> None:
        for command in (
            "git switch feat/demo",
            "git checkout -b other",
            "git checkout -- README.md",
            'git commit -m "a message with spaces"',
            "git add .",
            "git stash",
            "git stash pop",
            "git pull",
            "git merge feat/demo",
            "git reset --hard",
            "git restore README.md",
            "git rebase main",
            "git clean -fd",
            "git apply change.patch",
            "echo ok && git commit -am message",
            "git status; git switch feat/demo",
            "GIT_EDITOR=true git rebase --continue",
            "git -c core.autocrlf=false commit -m x",
            "git --no-pager commit -m x 2>&1 | tail -1",
            "git commit -F - <<'EOF'\nIt's a message with an apostrophe\nEOF",
            f'cd "{self.tree}" && cd "{self.hub}" && git switch feat/demo',
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.hub))

    def test_read_only_and_hub_maintenance_commands_are_allowed(self) -> None:
        for command in (
            "git status",
            "git log --oneline -5",
            "git diff",
            "git fetch origin",
            "git pull --ff-only",
            "git merge --ff-only origin/main",
            "git switch main",
            "git checkout -q main",
            "git stash list",
            "git apply --check change.patch",
            "git worktree add ../x -b y main",
            "git worktree remove ../x",
            "git branch -d feat/demo",
            "echo git commit",
            "python -B tools/worktrees.py new fix thing",
            f'git -C "{self.tree}" commit -m x',
            f'cd "{self.tree}" && git commit -m x',
            "cd .claude/worktrees/feat-demo && git commit -m x",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.hub))

    def test_worktree_sessions_may_commit_but_not_reach_into_the_hub(self) -> None:
        self.assertIsNone(self.bash("git commit -m x && git rebase main", self.tree))
        self.assertEqual("deny", self.bash(f'git -C "{self.hub}" switch feat/demo', self.tree))
        self.assertEqual("deny", self.bash(f'cd "{self.hub}" && git commit -m x', self.tree))
        self.assertEqual("deny", self.bash(f'git --work-tree="{self.hub}" add .', self.tree))
        self.assertEqual("deny", self.bash(f'GIT_WORK_TREE="{self.hub}" git checkout -- .', self.tree))
        self.assertIsNone(self.bash(f'git -C "{self.hub}" --work-tree="{self.tree}" add .', self.tree))

    def test_guard_fails_open_on_input_it_cannot_judge(self) -> None:
        outside = self.root / "not-a-repository"
        outside.mkdir()
        self.assertIsNone(self.bash("git commit -m x", outside))
        self.assertIsNone(
            self.decision({"tool_name": "Read", "tool_input": {"file_path": str(self.hub / "README.md")}})
        )
        result = self.run_script(["guard"], cwd=self.root, stdin="{not json")
        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("worktrees guard skipped", result.stderr)


def shown(directory: Path | None) -> str | None:
    return None if directory is None else os.path.normcase(os.path.normpath(directory))


class CommandReadingTests(WorktreesTestCase):
    """Each case starts a real pwsh, as the hook does, and packs several forms into one command to keep that cheap."""

    def read(
        self, command: str, cwd: Path | None = None, tool: str = "PowerShell", environment: dict[str, str] | None = None
    ) -> list[tuple[str | None, str]]:
        reading = worktrees.read_command(tool, command, cwd or self.hub, environment or self.environment)
        self.notes = reading.notes
        return [(shown(call.directory), call.subcommand) for call in reading.calls]

    def at(self, directory: Path | None, *subcommands: str) -> list[tuple[str | None, str]]:
        return [(shown(directory), subcommand) for subcommand in subcommands]

    def test_set_location_and_its_aliases_are_followed(self) -> None:
        forward = str(self.hub).replace("\\", "/")
        command = (
            f"Set-Location -Path '{self.tree}'; git commit -m x\n"
            f'sl -LiteralPath "{self.hub}" && git add .\n'
            f"cd -LP '{self.tree}' ; git rm x\n"
            f"chdir -Pat '{forward}' -PassThru; git mv a b\n"
            f"Set-Location -Path:'{self.tree}'; cd ..; git restore x\n"
            "cd; git reset\n"
            "cd ~/sub; git revert x\n"
            "cd -; git clean -fd"
        )
        self.assertEqual(
            [
                *self.at(self.tree, "commit"),
                *self.at(self.hub, "add"),
                *self.at(self.tree, "rm"),
                *self.at(self.hub, "mv"),
                *self.at(self.worktrees, "restore"),
                *self.at(Path.home(), "reset"),
                *self.at(Path.home() / "sub", "revert"),
                *self.at(None, "clean"),
            ],
            self.read(command),
        )

    def test_push_location_keeps_a_stack_and_a_pop_past_its_bottom_is_unknown(self) -> None:
        command = (
            f"Push-Location '{self.tree}'; git commit; pushd -Path '{self.hub}'; git add .; Pop-Location; git rm x;"
            " popd; git mv a b; Push-Location; git am; popd; popd; git reset"
        )
        self.assertEqual(
            [
                *self.at(self.tree, "commit"),
                *self.at(self.hub, "add"),
                *self.at(self.tree, "rm"),
                *self.at(self.hub, "mv", "am"),
                *self.at(None, "reset"),
            ],
            self.read(command),
        )

    def test_git_options_and_environment_variables_settle_the_directory_or_leave_it_unknown(self) -> None:
        environment = {**self.environment, "TASK_TREE": str(self.tree)}
        environment.pop("UNSET_TASK_TREE", None)
        command = (
            f"git -C '{self.tree}' commit; git \"--work-tree={self.tree}\" add .; git --work-tree '{self.tree}' rm x;"
            ' git --git-dir=.git mv a b; git -C $env:TASK_TREE reset; git -C "$env:TASK_TREE\\.." restore x;'
            " git -C \"$env:UNSET_TASK_TREE\" revert x; $env:GIT_WORK_TREE = 'elsewhere'; git clean -fd"
        )
        self.assertEqual(
            [
                *self.at(self.tree, "commit", "add", "rm"),
                *self.at(None, "mv"),
                *self.at(self.tree, "reset"),
                *self.at(self.worktrees, "restore"),
                *self.at(None, "revert", "clean"),
            ],
            self.read(command, environment=environment),
        )

    def test_quoting_comments_and_every_separator_are_read_as_powershell_reads_them(self) -> None:
        command = (
            'git commit -m @"\nIt\'s a "quoted" message; cd elsewhere\n"@\n'
            'git add `"odd name`" || git rm x | Out-Null && git mv a b # git reset in a comment\n'
            "& 'C:\\Program Files\\Git\\cmd\\git.exe' stash"
        )
        self.assertEqual(self.at(self.hub, "commit", "add", "rm", "mv", "stash"), self.read(command))

    def test_commands_are_read_in_execution_order_and_nested_shells_are_followed(self) -> None:
        bash_hub = str(self.hub).replace("\\", "/")
        command = (
            f"git add (Set-Location '{self.tree}'); & {{ Set-Location '{self.hub}' }}; git commit;"
            f" iex 'Set-Location ''{self.tree}'''; git rm x;"
            f" pwsh -c \"Set-Location '{self.hub}'; git mv a b\"; git reset;"
            f" bash -c 'cd \"{bash_hub}\" && git clean -fd'; git restore x"
        )
        self.assertEqual(
            [
                *self.at(self.tree, "add"),
                *self.at(self.hub, "commit"),
                *self.at(self.tree, "rm"),
                *self.at(self.hub, "mv"),
                *self.at(self.tree, "reset"),
                *self.at(self.hub, "clean"),
                *self.at(self.tree, "restore"),
            ],
            self.read(command),
        )
        self.assertEqual([], self.notes)

    def test_bash_follows_pwsh_and_git_work_tree_settings(self) -> None:
        command = (
            f'pwsh -Command "Set-Location \'{self.tree}\'; git switch x"; git --work-tree="{self.tree}" add .;'
            f' GIT_WORK_TREE="{self.tree}" git rm x; GIT_DIR=.git git commit;'
            f' bash -c "cd \\"{self.tree}\\" && git mv a b"; git reset'
        )
        self.assertEqual(
            [
                *self.at(self.tree, "switch", "add", "rm"),
                *self.at(None, "commit"),
                *self.at(self.tree, "mv"),
                *self.at(self.hub, "reset"),
            ],
            self.read(command, tool="Bash"),
        )

    def test_what_cannot_be_read_is_left_unjudged_with_a_note(self) -> None:
        self.assertEqual([], self.read("& $git commit; git $verb x; pwsh -c $script; iex $text; pwsh -c"))
        self.assertIn("a git subcommand is computed at run time", self.notes)
        self.assertIn("could not read the command passed to pwsh", self.notes)
        self.assertIn("could not read the command passed to iex", self.notes)
        self.assertEqual([], self.read("git commit -m 'unterminated"))
        self.assertEqual(1, len(self.notes))
        self.assertIn("PowerShell would not parse the command, so none of it runs", self.notes[0])

    def test_pwsh_starts_only_for_text_naming_git_and_a_refused_subcommand(self) -> None:
        # A PATH that names no existing directory: an empty or missing one would let Windows find pwsh anyway.
        unavailable = {**self.environment, "PATH": str(self.root / "no-such-directory")}
        self.assertEqual([], self.read("git status; git log --oneline; Get-ChildItem", environment=unavailable))
        self.assertEqual([], self.notes)
        self.assertEqual([], self.read("git commit -m x", environment=unavailable))
        self.assertEqual(["could not find pwsh to read a PowerShell command"], self.notes)


class PowerShellGuardTests(WorktreesTestCase):
    """End to end through the hook, which starts a real pwsh; the reading itself is covered by CommandReadingTests."""

    def test_the_hub_refuses_powershell_git_writes_but_allows_maintenance_and_work_in_a_worktree(self) -> None:
        self.assertEqual("deny", self.powershell("git switch feat/demo", self.hub))
        self.assertIsNone(self.powershell("git status; git switch main; git pull --ff-only; git stash list", self.hub))
        self.assertIsNone(self.powershell(f"Set-Location '{self.tree}'; git commit -m \"it's done\"", self.hub))

    def test_a_worktree_session_cannot_reach_into_the_hub_through_either_shell(self) -> None:
        self.assertIsNone(self.powershell("git commit -m x", self.tree))
        self.assertEqual("deny", self.powershell(f"Set-Location '{self.hub}'; git commit -m x", self.tree))
        self.assertEqual("deny", self.bash(f"pwsh -c 'git -C \"{self.hub}\" switch feat/demo'", self.tree))

    def test_a_powershell_command_is_allowed_with_a_note_when_pwsh_cannot_be_started(self) -> None:
        git = shutil.which("git")
        self.assertIsNotNone(git)
        path = os.pathsep.join([str(Path(git).parent), str(self.root / "no-such-directory")])
        self.assertIsNone(self.powershell("git switch feat/demo", self.hub, {**self.environment, "PATH": path}))
        self.assertIn("could not find pwsh", self.stderr)


class NewWorktreeTests(WorktreesTestCase):
    def test_new_creates_a_nested_worktree_on_a_branch_from_main_without_upstream(self) -> None:
        self.assertTrue((self.tree / "README.md").is_file())
        self.assertEqual("feat/demo", self.git(self.tree, "branch", "--show-current").strip())
        self.assertEqual(self.git(self.hub, "rev-parse", "main"), self.git(self.tree, "rev-parse", "HEAD"))
        upstream = subprocess.run(
            ["git", "-C", str(self.tree), "rev-parse", "--abbrev-ref", "@{upstream}"],
            capture_output=True,
            env=self.environment,
            check=False,
        )
        self.assertNotEqual(0, upstream.returncode)

    def test_new_from_inside_a_worktree_still_creates_it_in_the_hub(self) -> None:
        result = self.run_script(["new", "fix", "second"], cwd=self.tree)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((self.worktrees / "fix-second" / "README.md").is_file())

    def test_new_rejects_unsafe_names_and_existing_branches_or_paths(self) -> None:
        (self.worktrees / "fix-taken").mkdir()
        self.git(self.hub, "branch", "inv/existing")
        for arguments, message in (
            (["Feat", "x"], "lowercase"),
            (["feat", "a/b"], "lowercase"),
            (["feat", "-x"], "lowercase"),
            (["feat", "demo"], "already exists"),
            (["inv", "existing"], "already exists"),
            (["fix", "taken"], "already exists"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_script(["new", "--", *arguments], cwd=self.hub)
                self.assertEqual(1, result.returncode)
                self.assertIn(message, result.stderr)

    def test_new_refuses_while_the_hub_does_not_ignore_its_worktrees(self) -> None:
        (self.hub / ".gitignore").write_text("local.json\n", encoding="utf-8")
        result = self.run_script(["new", "fix", "unignored"], cwd=self.hub)
        self.assertEqual(1, result.returncode)
        self.assertIn(".claude/worktrees/ must be ignored", result.stderr)
        self.assertFalse((self.worktrees / "fix-unignored").exists())


class ListTests(WorktreesTestCase):
    def rows(self) -> dict[str, list[str]]:
        result = self.run_script(["list"], cwd=self.tree)
        self.assertEqual(0, result.returncode, result.stderr)
        lines = result.stdout.splitlines()
        self.assertTrue(lines[0].startswith("BRANCH"))
        return {line.split()[0]: [cell.strip() for cell in line.split("  ") if cell.strip()] for line in lines[1:]}

    def test_list_reports_each_worktree_state(self) -> None:
        rows = self.rows()
        self.assertEqual(["main", "hub", "0", "0", "0"], rows["main"][:5])
        self.assertEqual(["feat/demo", "no commits", "0", "0", "0"], rows["feat/demo"][:5])
        (self.tree / "work.txt").write_text("work\n", encoding="utf-8")
        self.git(self.tree, "add", "work.txt")
        self.git(self.tree, "commit", "-q", "-m", "work")
        self.assertEqual(["feat/demo", "in progress", "1", "0", "0"], self.rows()["feat/demo"][:5])

    def test_list_flags_a_hub_that_is_off_main_or_dirty(self) -> None:
        self.git(self.hub, "switch", "-q", "-c", "stray")
        (self.hub / "README.md").write_text("edited in the hub\n", encoding="utf-8")
        self.assertEqual(["stray", "hub · off main · dirty", "0", "0", "1"], self.rows()["stray"][:5])


if __name__ == "__main__":
    unittest.main()
