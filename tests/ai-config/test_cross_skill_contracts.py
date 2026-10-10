#!/usr/bin/env python3
"""Repository-only checks for contracts duplicated by standalone skills."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "code-review-core" / "scripts"))

import frontmatter as skill_frontmatter
import review_pipeline
import review_source
from review_config import COMPUTED_DASHBOARD_STATES

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def literal_assignment(path: Path, name: str) -> object:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not a literal assignment in {path}")


def literal_int(path: Path, name: str) -> int:
    value = literal_assignment(path, name)
    if not isinstance(value, int):
        raise AssertionError(f"{name} in {path} is not an integer literal")
    return value


def pinned_models(root: Path) -> dict[str, str]:
    """Each shipped and repository skill under root that pins a model, by its SKILL.md path, with the model."""
    paths = [*(root / "skills").glob("**/SKILL.md"), *(root / ".claude/skills").glob("*/SKILL.md")]
    return {
        path.relative_to(root).as_posix(): line.split(":", 1)[1].strip()
        for path in sorted(paths)
        for line in path.read_text(encoding="utf-8-sig").split("---", 2)[1].splitlines()
        if re.match(r"model\s*:", line)
    }


def declared_options(path: Path, parser: str) -> dict[str, dict[str, str]]:
    """Each option `<parser>.add_argument` declares, with its keywords as source, however the call is wrapped."""
    options: dict[str, dict[str, str]] = {}
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == parser
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            # A keyword without a name is a ** unpacking, which names no option setting.
            options[node.args[0].value] = {
                keyword.arg: ast.unparse(keyword.value) for keyword in node.keywords if keyword.arg is not None
            }
    return options


def replayed_skills(text: str, own: str, names: set[str]) -> list[str]:
    """Other skills a skill's text reads the SKILL.md of, or names in a clause with a step number or section title."""
    replayed = set(re.findall(r"\.\./([a-z0-9-]+)/SKILL\.md", text)) - {own}
    for clause in re.split(r"(?<=[.!?;,])\s+|\n", text):
        if re.search(r"\bsteps? \d|\"[^\"]+\" section|\bsection \"", clause, re.IGNORECASE):
            replayed |= {
                name for name in names - {own} if re.search(rf"`{re.escape(name)}`|\.\./{re.escape(name)}/", clause)
            }
    return sorted(replayed)


def computed_states_named(text: str) -> list[str]:
    """The backticked states in the parentheses after "computed" on the one line of text that describes
    `status_overrides`."""
    lines = [line for line in text.splitlines() if "status_overrides`" in line and "computed" in line]
    if len(lines) != 1:
        raise AssertionError(f"{len(lines)} lines describe status_overrides and its computed states, not one")
    return re.findall(r"`([^`]+)`", lines[0].split("computed", 1)[1].split(")", 1)[0])


def claude_code_grants(pattern: str, command: str, skill: Path) -> bool:
    """Whether a skill's Bash or PowerShell grant matches a command as Claude Code documents its matching:
    `${CLAUDE_SKILL_DIR}` filled in with the skill's directory, then the command's whole text compared, quotes and all,
    with `*` standing for any text and no path normalized."""
    filled = pattern.replace("${CLAUDE_SKILL_DIR}", str(skill))
    return re.fullmatch(".*".join(re.escape(part) for part in filled.split("*")), command, re.DOTALL) is not None


class CrossSkillContractTests(unittest.TestCase):
    def test_runtime_compatibility_keeps_skill_tool_mapping(self) -> None:
        contract = (REPOSITORY_ROOT / "skills/runtime-compatibility.md").read_text(encoding="utf-8-sig")
        self.assertIn("For `Skill`, use native skill invocation.", contract)

    def test_runtime_compatibility_holds_only_rules_a_shipped_skill_uses(self) -> None:
        # Every Codex and Copilot run of any skill reads this file first, so a rule no shipped skill needs is paid for
        # on every run.
        contract = (REPOSITORY_ROOT / "skills/runtime-compatibility.md").read_text(encoding="utf-8-sig")
        mapping = next(line for line in contract.splitlines() if "as Claude adapter names" in line)
        mapped = set(re.findall(r"`([A-Za-z]+)`", mapping))
        granted: set[str] = set()
        for path in [*(REPOSITORY_ROOT / "skills").glob("*/SKILL.md"), *(REPOSITORY_ROOT / "agents").glob("*.md")]:
            document = skill_frontmatter.read(path)
            for key in ("allowed-tools", "tools"):
                if key in document:
                    value = document.value(key)
                    entries = value if isinstance(value, list) else str(value).split(",")
                    granted |= {entry.strip().split("(", 1)[0] for entry in entries if entry.strip()}
        self.assertTrue(mapped, "the tool-mapping line was not found")
        self.assertEqual(set(), mapped - granted, "runtime-compatibility.md maps a tool nothing shipped grants")
        for unused in ("EnterWorktree", "CLAUDE_SESSION_ID", "model:", "MCP", "<usage>", "gh pr review", "KEY=value"):
            with self.subTest(unused=unused):
                self.assertNotIn(unused, contract)

    def test_only_skills_no_other_skill_invokes_leave_the_model_skill_list(self) -> None:
        def flag(name: str, key: str) -> str | None:
            return skill_frontmatter.read(REPOSITORY_ROOT / "skills" / name / "SKILL.md").string(key)

        skills = sorted((REPOSITORY_ROOT / "skills").glob("*/SKILL.md"))
        names = {path.parent.name for path in skills}
        # A backticked skill name on a line that says "invoke" is a skill another skill starts.
        invoked = {
            span.split()[0]
            for path in skills
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if "invoke" in line
            for span in re.findall(r"`([^`]+)`", line)
            if span.split() and span.split()[0] in names
        }
        self.assertEqual({"review-prs", "flag-review-finding"}, invoked)
        for name in sorted(invoked):
            with self.subTest(invoked=name):
                self.assertIsNone(flag(name, "disable-model-invocation"))
        # A hidden dependency loads no description into any session and is never started directly.
        hidden = sorted(
            path.stem
            for path in (REPOSITORY_ROOT / "deploy-meta").glob("*.json")
            if json.loads(path.read_text(encoding="utf-8")).get("selectable") is False
        )
        self.assertEqual(["code-review-core", "skill-core"], hidden)
        for name in hidden:
            with self.subTest(hidden=name):
                self.assertEqual(
                    ("true", "false"), (flag(name, "disable-model-invocation"), flag(name, "user-invocable"))
                )
        for name in ("repo-cleanup", "update-coding-agent-skills"):  # they delete or deploy; the user starts them
            with self.subTest(user_only=name):
                self.assertEqual("true", flag(name, "disable-model-invocation"))

    def test_the_cost_audit_finds_every_skill_in_this_source_tree(self) -> None:
        # change-skill audits a changed skill from its worktree, which must reach the source, not the deployed copy.
        change_skill = (REPOSITORY_ROOT / ".claude/skills/change-skill/SKILL.md").read_text(encoding="utf-8-sig")
        self.assertIn("analyze-skill-cost <name>", change_skill)
        self.assertIn("SCOPE source", change_skill)
        script = REPOSITORY_ROOT / "skills/analyze-skill-cost/scripts/skill_inventory.py"
        names = sorted(path.stem for path in (REPOSITORY_ROOT / "deploy-meta").glob("*.json"))
        self.assertIn("analyze-skill-cost", names)
        with tempfile.TemporaryDirectory() as home:
            # A deployed copy in the home must not win over the source.
            deployed = Path(home) / ".claude/skills/analyze-skill-cost/SKILL.md"
            deployed.parent.mkdir(parents=True)
            deployed.write_text("deployed", encoding="utf-8")
            for name in names:
                with self.subTest(skill=name):
                    completed = subprocess.run(
                        [
                            sys.executable,
                            "-B",
                            str(script),
                            "locate",
                            name,
                            "--repo",
                            str(REPOSITORY_ROOT),
                            "--home",
                            home,
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
                    lines = completed.stdout.splitlines()
                    expected = (REPOSITORY_ROOT / "skills" / name / "SKILL.md").as_posix()
                    self.assertEqual([f"SKILL_FILE {expected}", "SCOPE source"], [lines[1], lines[3]])

    def test_the_cost_audit_finds_every_repository_skill_past_its_shim(self) -> None:
        # change-skill audits a changed repository skill too; its .agents/skills shim must not make the name ambiguous.
        change_skill = (REPOSITORY_ROOT / ".claude/skills/change-skill/SKILL.md").read_text(encoding="utf-8-sig")
        self.assertIn("SCOPE project-claude", change_skill)
        script = REPOSITORY_ROOT / "skills/analyze-skill-cost/scripts/skill_inventory.py"
        names = sorted(path.parent.name for path in (REPOSITORY_ROOT / ".claude/skills").glob("*/SKILL.md"))
        self.assertIn("change-skill", names)
        with tempfile.TemporaryDirectory() as home:
            for name in names:
                with self.subTest(skill=name):
                    arguments = ["locate", name, "--repo", str(REPOSITORY_ROOT), "--home", home]
                    completed = subprocess.run(
                        [sys.executable, "-B", str(script), *arguments], capture_output=True, text=True, check=False
                    )
                    self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
                    lines = completed.stdout.splitlines()
                    expected = (REPOSITORY_ROOT / ".claude/skills" / name / "SKILL.md").as_posix()
                    self.assertEqual([f"SKILL_FILE {expected}", "SCOPE project-claude"], [lines[1], lines[3]])

    def test_the_runtime_canary_is_a_manual_gate_that_validation_never_runs(self) -> None:
        canary = (REPOSITORY_ROOT / ".claude/skills/runtime-canary/SKILL.md").read_text(encoding="utf-8-sig")
        self.assertIn('python -B tools/runtime_canary.py "<skill>"', canary)
        self.assertTrue((REPOSITORY_ROOT / "tools/runtime_canary.py").is_file())
        change_skill = (REPOSITORY_ROOT / ".claude/skills/change-skill/SKILL.md").read_text(encoding="utf-8-sig")
        self.assertIn("run the `runtime-canary` repository skill", change_skill)
        template = (REPOSITORY_ROOT / ".github/pull_request_template.md").read_text(encoding="utf-8-sig")
        self.assertIn("the `runtime-canary` lines are below", template)
        # Each run calls a model, so neither the runner nor a policy check may start it.
        runner = [
            REPOSITORY_ROOT / "tests/run_validation.py",
            *sorted((REPOSITORY_ROOT / "tests/validation").glob("*.py")),
        ]
        self.assertGreater(len(runner), 1)
        for path in runner:
            with self.subTest(path=path.name):
                self.assertNotIn("runtime_canary", path.read_text(encoding="utf-8"))

    def test_the_runtime_support_matrix_agrees_with_the_review_pipeline_on_agent_delegation(self) -> None:
        # The matrix says which runtimes start subagents; the review pipeline decides how reviewers run from its own
        # table. When one changes, review-prs's declared support has to change with it.
        from deployer import runtime_support

        pipeline = literal_assignment(
            REPOSITORY_ROOT / "skills/code-review-core/scripts/review_runtime.py", "RUNTIME_CAPABILITIES"
        )
        if not isinstance(pipeline, dict):
            self.fail("RUNTIME_CAPABILITIES in review_runtime.py is not a dictionary")
        self.assertEqual(set(runtime_support.RUNTIMES), set(pipeline))
        for runtime in runtime_support.RUNTIMES:
            with self.subTest(runtime=runtime):
                self.assertEqual(
                    "agent-delegation" in pipeline[runtime],
                    "agent-delegation" in runtime_support.RUNTIME_CAPABILITIES[runtime],
                )
        self.assertEqual(
            {"claude-code": True, "codex": True, "copilot-cli": False},
            {runtime: "agent-delegation" in pipeline[runtime] for runtime in runtime_support.RUNTIMES},
        )

    def test_no_skill_pins_a_model(self) -> None:
        # In Claude Code a skill's `model` applies for the rest of the turn that invoked it, so a code review
        # started in the same turn as `update-coding-agent-skills`, then pinned to Haiku, ran every reviewer on Haiku
        # and missed a must-fix finding. A skill runs on the session's model; pin one only with a measured reason
        # and an entry here. Repository skills under .claude/skills are held to the same rule.
        allowed: dict[str, str] = {}
        self.assertEqual(allowed, pinned_models(REPOSITORY_ROOT))

    def test_a_model_pin_is_found_under_both_skill_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path, model in (
                ("skills/shipped", "haiku"),
                ("skills/category/nested", "sonnet"),
                (".claude/skills/repository", "opus"),
            ):
                (root / path).mkdir(parents=True)
                (root / path / "SKILL.md").write_text(f"---\nname: x\nmodel: {model}\n---\n\n# X\n", encoding="utf-8")
            (root / ".claude/skills/unpinned").mkdir()
            (root / ".claude/skills/unpinned/SKILL.md").write_text("---\nname: y\n---\n\n# Y\n", encoding="utf-8")
            self.assertEqual(
                {
                    ".claude/skills/repository/SKILL.md": "opus",
                    "skills/category/nested/SKILL.md": "sonnet",
                    "skills/shipped/SKILL.md": "haiku",
                },
                pinned_models(root),
            )

    def test_reviewers_run_as_the_deployed_reviewer_agent(self) -> None:
        # A general-purpose reviewer carried about 20,000 tokens of system prompt and tool definitions, plus the
        # session's CLAUDE.md chain, on every turn. The reviewer agent names only the tools a role needs.
        agent = (REPOSITORY_ROOT / "agents/code-review-reviewer.md").read_text(encoding="utf-8")
        self.assertEqual(
            [
                "name: code-review-reviewer",
                "description: Internal to the code-review-core pipeline. Started only by review-prs, with a prepared "
                "prompt file for one reviewer role. Do not use it for anything else.",
                "tools: Read, Grep, Glob, Write, Edit, Bash",
                "model: inherit",
                "omitClaudeMd: true",
                # Reviewers told not to read the local checkout did anyway; this hook enforces the run boundary.
                "hooks:",
                "  PreToolUse:",
                '    - matcher: "Read|Grep|Glob|Write|Edit|Bash"',
                "      hooks:",
                "        - type: command",
                # Python finds the profile folder in either shell; -I keeps the working directory off sys.path.
                # A guard that cannot run would exit 1, which lets the call through, so the hook denies it instead.
                "          command: >-",
                '            python -I -B -c "import json, os, runpy, sys;',
                "            sys.excepthook = lambda kind, error, trace: (sys.__excepthook__(kind, error, trace),",
                "            print(json.dumps({'hookSpecificOutput': "
                "{'hookEventName': 'PreToolUse', 'permissionDecision':"
                " 'deny',",
                "            'permissionDecisionReason': 'Code-review reviewer boundary: the guard could not run (' +"
                " kind.__name__ + ').'}}),",
                "            flush=True), os._exit(0));",
                "            runpy.run_path(os.path.expanduser("
                "'~/.claude/skills/code-review-core/scripts/review_guard.py'),",
                "            run_name='__main__')\"",
                "          timeout: 30",
            ],
            agent.split("---", 2)[1].strip().splitlines(),
        )
        guard = REPOSITORY_ROOT / "skills/code-review-core/scripts/review_guard.py"
        self.assertTrue(guard.is_file(), "the hook runs the guard code-review-core deploys")
        self.assertIn('READ_TOOLS = {"Read", "Grep", "Glob"}', guard.read_text(encoding="utf-8"))
        self.assertIn('WRITE_TOOLS = {"Write", "Edit"}', guard.read_text(encoding="utf-8"))
        meta = json.loads((REPOSITORY_ROOT / "deploy-meta/code-review-core.json").read_text(encoding="utf-8"))
        self.assertEqual(["code-review-reviewer"], meta["agent_deps"])
        pipeline = (REPOSITORY_ROOT / "skills/code-review-core/scripts/review_pipeline.py").read_text(encoding="utf-8")
        self.assertIn('REVIEWER_AGENT = "code-review-reviewer"', pipeline)
        skill = (REPOSITORY_ROOT / "skills/review-prs/SKILL.md").read_text(encoding="utf-8-sig")
        self.assertIn("start one fresh native subagent of type `code-review-reviewer`", skill)
        self.assertIn("If this session has no `code-review-reviewer` type", skill)
        self.assertIn("use a general-purpose subagent instead", skill)

    @staticmethod
    def reviewer_hook_shells(command: str) -> dict[str, tuple[str | None, list[str]]]:
        """The reviewer hook's shell, if found, and its arguments in each shell Claude Code may run hooks in."""
        from deployer import platform_support

        return {
            "Git Bash": (platform_support.find_bash(), ["-c", command]),
            "PowerShell": (platform_support.find_pwsh(os.environ.get("PATH", "")), ["-NoProfile", "-Command", command]),
        }

    def reviewer_hook_command(self) -> str:
        frontmatter = (REPOSITORY_ROOT / "agents/code-review-reviewer.md").read_text(encoding="utf-8").split("---")[1]
        lines = frontmatter.splitlines()
        start = lines.index("          command: >-") + 1
        folded = [line.strip() for line in lines[start:] if line.startswith("            ")]
        command = " ".join(folded)  # a folded block scalar joins its lines with single spaces
        self.assertNotIn("$", command, "neither shell may expand anything in the command")
        return command

    def test_the_reviewer_hook_blocks_the_call_when_the_guard_is_missing_or_broken(self) -> None:
        # A PreToolUse hook that fails with any exit code but 2 lets the call through unguarded, and PowerShell's
        # -Command reports every failing program as exit 1, so exit 2 cannot block there. A profile without the
        # deployed guard, or a guard that cannot even load, must instead deny the call with exit 0, as the guard
        # does when it cannot evaluate one.
        command = self.reviewer_hook_command()
        event = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "C:/anything.txt"}, "cwd": "C:/"})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            missing = root / "profile without guard"
            missing.mkdir()
            broken = root / "profile with broken guard"
            guard = broken / ".claude/skills/code-review-core/scripts/review_guard.py"
            guard.parent.mkdir(parents=True)
            guard.write_text("raise RuntimeError('the guard failed to load')\n", encoding="utf-8")
            # A guard deployed without the skill-core it imports cannot load either.
            coreless = root / "profile without skill-core"
            real_guard = coreless / ".claude/skills/code-review-core/scripts/review_guard.py"
            real_guard.parent.mkdir(parents=True)
            real_guard.write_bytes((REPOSITORY_ROOT / "skills/code-review-core/scripts/review_guard.py").read_bytes())
            cases = {
                "missing": (missing, "review_guard.py", "FileNotFoundError"),
                "broken": (broken, "the guard failed to load", "RuntimeError"),
                "coreless": (coreless, "No module named 'console'", "ModuleNotFoundError"),
            }
            for case, (profile, traceback, error) in cases.items():
                environment = {**os.environ, "USERPROFILE": str(profile)}
                for shell, (executable, arguments) in self.reviewer_hook_shells(command).items():
                    with self.subTest(case=case, shell=shell):
                        if executable is None:
                            self.fail(f"{shell} is a validation prerequisite")
                        result = subprocess.run(
                            [executable, *arguments],
                            input=event,
                            capture_output=True,
                            text=True,
                            cwd=root,
                            env=environment,
                            check=False,
                        )
                        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                        self.assertIn(traceback, result.stderr)
                        self.assertEqual(
                            {
                                "hookSpecificOutput": {
                                    "hookEventName": "PreToolUse",
                                    "permissionDecision": "deny",
                                    "permissionDecisionReason": "Code-review reviewer boundary: "
                                    f"the guard could not run ({error}).",
                                }
                            },
                            json.loads(result.stdout),
                        )

    def test_the_reviewer_hook_runs_the_deployed_guard_whatever_home_says(self) -> None:
        # Claude Code runs hooks in Git Bash, whose $HOME follows HOME, or in PowerShell when it finds no Git Bash.
        # The deployer and Claude Code both use the profile folder, so the hook must find the guard there in both.
        command = self.reviewer_hook_command()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            profile = root / "Some O'Neil (profile)"
            guard = profile / ".claude/skills/code-review-core/scripts/review_guard.py"
            guard.parent.mkdir(parents=True)
            guard.write_bytes((REPOSITORY_ROOT / "skills/code-review-core/scripts/review_guard.py").read_bytes())
            # The guard imports skill-core's console setup from the sibling skill the deployer installs beside it.
            core = profile / ".claude/skills/skill-core/scripts/console.py"
            core.parent.mkdir(parents=True)
            core.write_bytes((REPOSITORY_ROOT / "skills/skill-core/scripts/console.py").read_bytes())
            # A session's working directory is untrusted: its modules must never shadow the standard library or the
            # core.
            cwd = root / "session"
            cwd.mkdir()
            for module in ("json", "runpy", "pathlib", "re", "tempfile", "contextlib", "console"):
                (cwd / f"{module}.py").write_text("print('HIJACKED')\nraise SystemExit(0)\n", encoding="utf-8")
            other = root / "other home"
            environment = {
                **os.environ,
                "USERPROFILE": str(profile),
                "HOME": "/" + other.as_posix()[0].lower() + other.as_posix()[2:],
            }
            # A subagent's event carries its agent ID; a reviewer that has read no prompt yet may read nothing else.
            event = json.dumps(
                {
                    "tool_name": "Read",
                    "tool_input": {"file_path": str(cwd / "json.py")},
                    "cwd": str(cwd),
                    "agent_id": "a71dab35ebc1b97eb",
                }
            )
            for shell, (executable, arguments) in self.reviewer_hook_shells(command).items():
                with self.subTest(shell=shell):
                    if executable is None:
                        self.fail(f"{shell} is a validation prerequisite")
                    result = subprocess.run(
                        [executable, *arguments],
                        input=event,
                        capture_output=True,
                        text=True,
                        cwd=cwd,
                        env=environment,
                        check=False,
                    )
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertNotIn("HIJACKED", result.stdout)
                    decision = json.loads(result.stdout)["hookSpecificOutput"]
                    self.assertEqual("deny", decision["permissionDecision"])
                    self.assertIn("read the prompt file your task names first", decision["permissionDecisionReason"])
            self.assertFalse(other.exists())

    def test_native_and_workflow_reviewers_get_the_same_task(self) -> None:
        task = "Read <prompt file> and follow it exactly. It is your complete task."
        skill = (REPOSITORY_ROOT / "skills/review-prs/SKILL.md").read_text(encoding="utf-8-sig")
        pipeline = (REPOSITORY_ROOT / "skills/code-review-core/scripts/review_pipeline.py").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn(f"with exactly this prompt and nothing else: `{task}`", skill)
        self.assertIn(f'REVIEWER_TASK = "{task.replace("<prompt file>", "{prompt}")}"', pipeline)
        self.assertIn('"Workflow"', skill.split("---", 2)[1], "the Workflow path needs the tool allowed")
        # Both paths start a role on the model its trusted profile names, and otherwise name none.
        self.assertIn("When a `MODEL` line names that role, start its subagent on that model", skill)
        self.assertIn("on the model of any `MODEL` line that follows it", skill)
        self.assertIn("...(role.model ? {{ model: role.model }} : {{}})", pipeline)

    def test_the_tracker_and_the_contract_name_the_computed_states_the_configuration_refuses(self) -> None:
        # An override set to a computed state is refused, so the states a user is told to avoid must be those.
        for path in (
            "skills/update-pr-tracker/SKILL.md",
            "docs/code-review-operations-contract.md",
            "docs/skills.md",
        ):
            with self.subTest(path=path):
                named = computed_states_named((REPOSITORY_ROOT / path).read_text(encoding="utf-8-sig"))
                self.assertEqual(len(named), len(set(named)), named)
                self.assertEqual(sorted(COMPUTED_DASHBOARD_STATES), sorted(named))

    def test_a_computed_state_list_is_read_from_its_one_line(self) -> None:
        line = "- `dashboard.status_overrides`: `on hold`, not a computed state (`drafts` or `stale`); see `--remove`."
        self.assertEqual(["drafts", "stale"], computed_states_named(f"intro `drafts`\n{line}\nafter `missing`"))
        for text in ("no such line", f"{line}\n{line}"):
            with self.subTest(text=text), self.assertRaises(AssertionError):
                computed_states_named(text)

    def test_the_tracker_reviews_its_candidates_in_one_review_prs_pass(self) -> None:
        # One invocation per candidate made each pull request wait for the previous one's slowest reviewer.
        def read(name: str) -> str:
            return (REPOSITORY_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8-sig")

        tracker = read("update-pr-tracker")
        self.assertIn(
            "invoke `review-prs` once for every confirmed pull request, with `--pull <owner/repo#number>` for each "
            "`missing` and `--re-review <owner/repo#number>` for each `stale`",
            tracker,
        )
        self.assertNotIn("`re-review <owner/repo#number>`", tracker)
        review_prs = read("review-prs")
        self.assertIn("--pull owner/repo#number ... --re-review owner/repo#number ...", review_prs.split("---", 2)[1])
        self.assertIn("(or `--re-review` for one given with `--re-review`)", review_prs)
        pipeline = REPOSITORY_ROOT / "skills/code-review-core/scripts/review_pipeline.py"
        self.assertEqual("'append'", declared_options(pipeline, "prepare_parser")["--re-review"]["action"])

    def test_review_prs_waits_for_the_copilot_host_in_bounded_calls(self) -> None:
        # A foreground command ends after 2 minutes by default in Claude Code; a host review can take 30.
        skill = (REPOSITORY_ROOT / "skills/review-prs/SKILL.md").read_text(encoding="utf-8-sig")
        waits = re.findall(
            r"`wait --run <run directory> --timeout (\d+)` with a command timeout of at least (\d+) "
            r"minutes, again each time it prints `RUNNING <seconds>s`",
            skill,
        )
        self.assertEqual(1, len(waits), "step 3 waits for the host in repeated bounded calls")
        timeout, command_minutes = (int(value) for value in waits[0])
        self.assertLessEqual(timeout, 100, "each wait fits well inside a 2-minute command limit")
        self.assertGreater(command_minutes * 60, timeout)
        pipeline = REPOSITORY_ROOT / "skills/code-review-core/scripts/review_pipeline.py"
        self.assertLessEqual(timeout, literal_int(pipeline, "MAX_WAIT_SECONDS"))
        self.assertIn("prints `STARTED <run directory>` at once", skill)
        self.assertIn("`RUNNING <selector> <id> <seconds>s` means that run's Copilot CLI host is still going", skill)

    def test_review_prs_finishes_the_workflow_path_in_the_turn_that_invoked_it(self) -> None:
        # A session scheduled a wakeup instead of waiting for the Workflow. The skill's grants end with the
        # turn that invoked it, so check and finalize were denied later, nothing was recorded, and it still
        # reported success.
        skill = (REPOSITORY_ROOT / "skills/review-prs/SKILL.md").read_text(encoding="utf-8-sig")
        body = skill.split("---", 2)[2]
        pipeline = REPOSITORY_ROOT / "skills/code-review-core/scripts/review_pipeline.py"
        self.assertIn(
            "Never end the turn, reply, or schedule a wakeup (ScheduleWakeup, CronCreate, `/loop`) while a "
            "prepared run is not finalized",
            body,
        )
        workflow = body.split("## With the Workflow tool", 1)[1]
        self.assertNotIn(
            "Wait for it to finish", workflow, "the Workflow tool returns at once; there is nothing to wait on"
        )
        minutes = re.findall(
            r"Never end the turn to wait for it: run `wait-reviewers` with one `--run` per run and a "
            r"command timeout of at least (\d+) minutes, again each time it prints a "
            r"`RUNNING <selector> <id> <seconds>s` line",
            workflow,
        )
        timeouts = re.findall(
            r'review_pipeline\.py" wait-reviewers --run "<run directory>" --run "<run directory>" '
            r"--timeout (\d+)\n",
            workflow,
        )
        self.assertEqual((1, 1), (len(minutes), len(timeouts)), "the Workflow path waits in repeated bounded calls")
        timeout, command_minutes = int(timeouts[0]), int(minutes[0])
        self.assertLessEqual(timeout, 100, "each wait fits well inside a 2-minute command limit")
        self.assertGreater(command_minutes * 60, timeout)
        self.assertLessEqual(timeout, literal_int(pipeline, "MAX_WAIT_SECONDS"))
        self.assertIn(
            "`OVERDUE <selector> <id> <seconds>s` means that role ran past the reviewer limit; step 4 retries it.",
            workflow,
        )
        # Every mode ends by listing the runs that never reached finalize, and a denied command is a failure.
        self.assertIn(
            "run the pipeline's `unfinalized` command with one `--run` per `RUN` directory `prepare` printed. "
            "Report each `UNFINALIZED <selector> <run directory>` as that pull request's failure",
            body,
        )
        self.assertIn(
            "If any pipeline command was denied or could not run, report every pull request without a "
            "`RECORDED` line as failed, and never report the run as a success.",
            body,
        )
        source = pipeline.read_text(encoding="utf-8")
        self.assertIn('wait_reviewers_parser = commands.add_parser("wait-reviewers")', source)
        self.assertIn('for name in ("check", "finalize", "unfinalized"):', source)

    def test_review_prs_works_an_inline_run_through_next_role_and_states_its_boundary(self) -> None:
        # Copilot CLI cannot start subagents, so the orchestrating session works each role itself.
        skill = (REPOSITORY_ROOT / "skills/review-prs/SKILL.md").read_text(encoding="utf-8-sig")
        body = skill.split("---", 2)[2]
        self.assertIn("Add `--inline` when this session cannot start subagents", body)
        self.assertIn("or one `INLINE <run directory>` line.", body)
        self.assertIn(
            "run the pipeline's `next-role --run <run directory>` command, and for "
            "`INLINE_ROLE <selector> <id> <prompt file>` read that prompt file",
            body,
        )
        self.assertIn("until it prints `INLINE_DONE <selector>`", body)
        self.assertIn("you are its reviewer without the reviewer agent's guard", body)
        self.assertIn("If any run printed `HOST copilot-cli` or `INLINE`, use the groups instead.", body)
        pipeline = REPOSITORY_ROOT / "skills/code-review-core/scripts/review_pipeline.py"
        self.assertIn("--inline", declared_options(pipeline, "prepare_parser"))
        source = pipeline.read_text(encoding="utf-8")
        self.assertIn('commands.add_parser("next-role")', source)
        for line in ("f\"INLINE {result['run']}\"", 'f"INLINE_ROLE {selector} ', 'f"INLINE_DONE {selector}"'):
            self.assertIn(line, source)

    def test_an_inline_role_s_commands_match_the_grant_review_prs_started_the_pipeline_under(self) -> None:
        # An inline role runs its prompt's commands in the session review-prs runs in, under the skill's grant, which
        # Claude Code matches as text: the resolved path of the same script, with no `..`, never matches it.
        skill = REPOSITORY_ROOT / "skills/review-prs"
        value = skill_frontmatter.read(skill / "SKILL.md").value("allowed-tools")
        if not isinstance(value, list):
            self.fail(f"review-prs grants no list of tools: {value!r}")
        grants = [
            entry.removeprefix(f"{shell}(").removesuffix(")")
            for entry in value
            for shell in ("Bash", "PowerShell")
            if entry.startswith(f"{shell}(")
        ]
        self.assertEqual(2, len(grants), value)
        body = (skill / "SKILL.md").read_text(encoding="utf-8-sig").split("---", 2)[2]
        started = re.search(r'python -B "(\$\{CLAUDE_SKILL_DIR\}[^"]+)" prepare ', body)
        if started is None:
            self.fail("review-prs names no prepare command")
        run = Path(tempfile.gettempdir()) / "code-review-run-example"

        def commands(argv: list[str]) -> list[str]:
            with mock.patch.object(sys, "argv", argv):
                fetch, search = review_source.source_commands(
                    run, "generic-review", script=review_pipeline.command_script("review_source.py")
                )
                check = review_pipeline.self_check_command(run, "generic-review")
            return [check, fetch.replace("<path>", "src/app.py"), search.replace("<pattern>", "def main")]

        spelled = commands([started.group(1).replace("${CLAUDE_SKILL_DIR}", str(skill)), "prepare"])
        resolved = commands(["review_pipeline.py", "prepare"])
        for grant in grants:
            for command in spelled:
                with self.subTest(grant=grant, command=command):
                    self.assertTrue(claude_code_grants(grant, command, skill))
            for command in resolved:
                with self.subTest(grant=grant, resolved=command):
                    self.assertFalse(claude_code_grants(grant, command, skill))

    def test_review_prs_states_its_runtime_instead_of_leaving_it_to_path(self) -> None:
        # PATH says which CLIs are installed, not which one is orchestrating, so review-prs names its host.
        skill = (REPOSITORY_ROOT / "skills/review-prs/SKILL.md").read_text(encoding="utf-8-sig")
        self.assertIn('review_pipeline.py" prepare --host "<runtime>" --pull', skill)
        self.assertIn(
            "`--host` names the runtime this session is running in: `claude-code`, `codex`, or `copilot-cli`.", skill
        )
        pipeline = REPOSITORY_ROOT / "skills/code-review-core/scripts/review_pipeline.py"
        host = declared_options(pipeline, "prepare_parser")["--host"]
        self.assertEqual("sorted(RUNTIME_CAPABILITIES)", host["choices"])
        runtime = (REPOSITORY_ROOT / "skills/code-review-core/scripts/review_runtime.py").read_text(encoding="utf-8")
        hosts = runtime.split("RUNTIME_CAPABILITIES = {", 1)[1].split("\n}", 1)[0]
        self.assertEqual(
            ["claude-code", "codex", "copilot-cli"], [line.split('"')[1] for line in hosts.strip().splitlines()]
        )

    def test_review_prs_prepares_canaries_in_the_shape_the_pipeline_accepts(self) -> None:
        # The skill takes `--canary owner/repo#number`, but the pipeline's --canary is a bare flag before --pull
        # selectors; with only the --pull fence to copy, a canary run first tried `--canary <selector>`.
        skill = (REPOSITORY_ROOT / "skills/review-prs/SKILL.md").read_text(encoding="utf-8-sig")
        body = skill.split("---", 2)[2]
        self.assertIn('review_pipeline.py" prepare --host "<runtime>" --canary --pull "<owner/repo#number>"', body)
        # A fixture canary passes the skill's own arguments through, the fixture directory after --fixture.
        self.assertIn('review_pipeline.py" prepare --host "<runtime>" --canary --fixture "<directory>"', body)
        self.assertIn('adding `--re-review --prior "<record>"` when given', body)
        pipeline_lines = [line for line in body.splitlines() if "review_pipeline.py" in line and "--canary" in line]
        self.assertEqual(2, len(pipeline_lines))
        for line in pipeline_lines:
            with self.subTest(line=line):
                self.assertRegex(line, r"--canary --(pull|fixture) ")
        self.assertIsNone(
            re.search(r"`[^`\n]*--canary \"?<?owner[^`\n]*`", body.replace("`--canary owner/repo#number`", ""))
        )
        script = REPOSITORY_ROOT / "skills/code-review-core/scripts/review_pipeline.py"
        self.assertIn(
            'prepare_parser.add_argument("--canary", action="store_true")', script.read_text(encoding="utf-8")
        )
        options = declared_options(script, "prepare_parser")
        self.assertLessEqual({"--fixture", "--prior", "--re-review"}, set(options))
        self.assertEqual(("'?'", "''"), (options["--re-review"].get("nargs"), options["--re-review"].get("const")))

    def test_a_re_review_scope_is_asked_for_and_never_assumed(self) -> None:
        # A full or incremental pass is the user's call: the tracker asks once per run, a direct call asks itself.
        def read(name: str) -> tuple[str, str]:
            text = (REPOSITORY_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8-sig")
            return text.split("---", 2)[1], text

        for name in ("review-prs", "update-pr-tracker"):
            with self.subTest(asks=name):
                self.assertIn('"AskUserQuestion"', read(name)[0])
        self.assertIn(
            "If `--re-review` was given without `--scope`, ask the user once with AskUserQuestion, offering "
            "`auto`, `full`, and `incremental` in that order; never choose one yourself.",
            read("review-prs")[1],
        )
        tracker = read("update-pr-tracker")[1]
        self.assertIn("Ask once for the run, never per pull request.", tracker)
        self.assertIn("plus `--scope <scope>` with the chosen scope when any is `stale`", tracker)
        pipeline = (REPOSITORY_ROOT / "skills/code-review-core/scripts/review_pipeline.py").read_text(encoding="utf-8")
        self.assertIn('parser.error("--scope is required with --re-review and taken only with it")', pipeline)

    def test_no_skill_replays_another_skills_steps(self) -> None:
        # re-review replayed "steps 2 to 5" of review-prs, so renumbering review-prs silently broke it.
        # A skill invokes another skill by name instead; it never reads that skill's SKILL.md or cites its steps.
        retired = (
            "Read `${CLAUDE_SKILL_DIR}/../review-prs/SKILL.md` and follow its steps 2 to 5 exactly for this one pull "
            'request (or its "With the Workflow tool" section when that tool is available).'
        )
        names = {path.parent.name for path in (REPOSITORY_ROOT / "skills").glob("*/SKILL.md")}
        self.assertEqual(["review-prs"], replayed_skills(retired, "re-review", names))
        self.assertEqual(["review-prs"], replayed_skills("Then do step 3 of `review-prs` again.", "x", names))
        self.assertEqual(["review-prs"], replayed_skills('Apply the "Posting" section of `review-prs`.', "x", names))
        self.assertEqual(
            [], replayed_skills("Run step 2 again, then invoke `review-prs` with `--re-review`.", "x", names)
        )
        for path in sorted((REPOSITORY_ROOT / "skills").glob("*/SKILL.md")):
            with self.subTest(skill=path.parent.name):
                body = path.read_text(encoding="utf-8-sig").split("---", 2)[2]
                self.assertEqual([], replayed_skills(body, path.parent.name, names))

    def test_re_review_is_retired_into_review_prs(self) -> None:
        # review-prs --re-review does everything the re-review alias did, so the alias is gone.
        self.assertFalse((REPOSITORY_ROOT / "skills/re-review").exists())
        self.assertFalse((REPOSITORY_ROOT / "deploy-meta/re-review.json").exists())
        source = json.loads((REPOSITORY_ROOT / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(
            ["review-prs", "update-pr-tracker", "review-insights", "flag-review-finding"],
            source["bundles"]["code-review-operations"]["members"],
        )
        review_prs = (REPOSITORY_ROOT / "skills/review-prs/SKILL.md").read_text(encoding="utf-8-sig")
        # It now interprets every re-review-specific line of the pipeline itself.
        for line in (
            "(for a re-review, `has no review yet` means it needs `--pull` instead)",
            "A re-review's `NOTE` that the review supersedes a migrated legacy review means there were no structured "
            "findings to carry forward, so it runs as an initial review that becomes version 1; say so in the report.",
            "A version race or archive failure is that pull request's failure, never permission to guess the next "
            "version.",
        ):
            with self.subTest(explains=line):
                self.assertIn(line, review_prs)

    def test_generic_reviewer_resources_do_not_depend_on_checkout_path(self) -> None:
        skill_paths = [
            REPOSITORY_ROOT / "skills/code-review-core/SKILL.md",
            REPOSITORY_ROOT / "skills/review-prs/SKILL.md",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8-sig") for path in skill_paths)
        # The pipeline plans the generic reviewer, so the code, not the skill text, must resolve its
        # instructions from the installed core skill rather than from any repository checkout.
        specialists = (REPOSITORY_ROOT / "skills/code-review-core/scripts/review_specialists.py").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn(
            'DEFAULT_GENERIC_INSTRUCTIONS = Path(__file__).resolve().parents[1] / "references" / "generic-reviewer.md"',
            specialists,
        )
        self.assertTrue((REPOSITORY_ROOT / "skills/code-review-core/references/generic-reviewer.md").is_file())
        self.assertNotIn("C:/GitHub", combined)
        self.assertNotIn("C:\\GitHub", combined)


if __name__ == "__main__":
    unittest.main()
