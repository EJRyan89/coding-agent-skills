#!/usr/bin/env python3
"""Repository-only checks for contracts duplicated by standalone skills."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from deployer import frontmatter as skill_frontmatter

ACTION_PIN = re.compile(r"uses:\s*(actions/[A-Za-z0-9_.-]+)@([0-9a-f]{40})")


def literal_assignment(path: Path, name: str) -> object:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name and node.value is not None:
                return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not a literal assignment in {path}")


def replayed_skills(text: str, own: str, names: set[str]) -> list[str]:
    """Other skills a skill's text reads the SKILL.md of, or names in a clause with a step number or section title."""
    replayed = set(re.findall(r"\.\./([a-z0-9-]+)/SKILL\.md", text)) - {own}
    for clause in re.split(r"(?<=[.!?;,])\s+|\n", text):
        if re.search(r"\bsteps? \d|\"[^\"]+\" section|\bsection \"", clause, re.IGNORECASE):
            replayed |= {name for name in names - {own} if re.search(rf"`{re.escape(name)}`|\.\./{re.escape(name)}/", clause)}
    return sorted(replayed)


class CrossSkillContractTests(unittest.TestCase):
    def test_transport_compatibility_matches(self) -> None:
        audit = REPOSITORY_ROOT / "skills/audit-ai-config/scripts/audit_ai_config.py"
        generator = REPOSITORY_ROOT / "skills/init-ai-config/scripts/ai_config_template.py"
        self.assertEqual(
            literal_assignment(audit, "TRANSPORT_COMPATIBILITY"),
            literal_assignment(generator, "TRANSPORT_COMPATIBILITY"),
            "Standalone audit and generator transport matrices drifted",
        )

    def test_inventory_file_sets_match(self) -> None:
        audit = REPOSITORY_ROOT / "skills/audit-ai-config/scripts/audit_ai_config.py"
        setup = REPOSITORY_ROOT / "skills/init-ai-config/scripts/init_ai_config.py"
        for name in ("INVENTORY_FILES", "RECURSIVE_INVENTORY_NAMES", "GENERATOR_CANDIDATES"):
            with self.subTest(constant=name):
                self.assertEqual(literal_assignment(audit, name), literal_assignment(setup, name))

    def test_audit_accepts_every_path_the_generator_may_own(self) -> None:
        audit = REPOSITORY_ROOT / "skills/audit-ai-config/scripts/audit_ai_config.py"
        generator = REPOSITORY_ROOT / "skills/init-ai-config/scripts/ai_config_template.py"
        audit_paths = set(literal_assignment(audit, "MANIFEST_ALLOWED_PATHS"))
        generator_paths = set(literal_assignment(generator, "MANIFEST_ALLOWED_PATHS"))
        self.assertEqual(set(), generator_paths - audit_paths)
        self.assertIn(".github/workflows/ai-config-parity-pr.yml", generator_paths)

    def test_audit_derives_scope_from_an_installed_generator(self) -> None:
        for skill in ("audit-ai-config", "init-ai-config"):
            scripts = str(REPOSITORY_ROOT / "skills" / skill / "scripts")
            if scripts not in sys.path:
                sys.path.insert(0, scripts)
        import audit_ai_config
        import init_ai_config

        spec = {"runtimes": ["claude", "codex"], "surfaces": ["copilot_cli", "code_review"],
                "features": ["ci_parity", "ci_parity_caller"]}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".git").mkdir()
            (root / "CLAUDE.md").write_text(
                "# CLAUDE.md\n\n## Overview\n\nx\n\n## Build and Test Commands\n\nmake\n\n"
                "## Formatting Rules\n\ntabs\n\n## CI / Quality Gates\n\nEvery check must pass.\n",
                encoding="utf-8",
            )
            spec_path = root / "spec.json"
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            code, lines = init_ai_config.install(root, spec_path, replace=False)
            self.assertEqual(0, code, lines)
            derived, status = audit_ai_config.derive_generator_scope(root)
        self.assertEqual("independently-derived", status)
        self.assertEqual(spec, derived)

    def test_generated_workflows_pin_the_reviewed_action_commits(self) -> None:
        # Dependabot updates only validate.yml; this keeps generated workflows in step.
        reviewed = dict(
            ACTION_PIN.findall(
                (REPOSITORY_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
            )
        )
        skill = REPOSITORY_ROOT / "skills/init-ai-config"
        sources = [
            skill / "scripts/ai_config_template.py",
            *sorted((skill / "references").glob("*.yml")),
        ]
        found = 0
        for source in sources:
            for action, sha in ACTION_PIN.findall(source.read_text(encoding="utf-8")):
                found += 1
                with self.subTest(source=source.name, action=action):
                    self.assertIn(action, reviewed, f"{action} is not pinned in validate.yml")
                    self.assertEqual(reviewed[action], sha, f"{source.name} pins a stale {action}")
        self.assertGreater(found, 0, "No generated action pins were found")

    def test_runtime_compatibility_keeps_skill_tool_mapping(self) -> None:
        contract = (
            REPOSITORY_ROOT / "skills/runtime-compatibility.md"
        ).read_text(encoding="utf-8-sig")
        self.assertIn("For `Skill`, use native skill invocation.", contract)

    def test_only_skills_no_other_skill_invokes_leave_the_model_skill_list(self) -> None:
        def flag(name: str, key: str) -> str | None:
            return skill_frontmatter.read(REPOSITORY_ROOT / "skills" / name / "SKILL.md").string(key)

        skills = sorted((REPOSITORY_ROOT / "skills").glob("*/SKILL.md"))
        names = {path.parent.name for path in skills}
        # A backticked skill name on a line that says "invoke" is a skill another skill starts.
        invoked = {
            span.split()[0]
            for path in skills
            for line in path.read_text(encoding="utf-8-sig").splitlines() if "invoke" in line
            for span in re.findall(r"`([^`]+)`", line) if span.split() and span.split()[0] in names
        }
        self.assertEqual({"review-prs", "flag-review-finding", "audit-ai-config"}, invoked)
        for name in sorted(invoked):
            with self.subTest(invoked=name):
                self.assertIsNone(flag(name, "disable-model-invocation"))
        self.assertEqual(
            ("true", "false"),
            (flag("code-review-core", "disable-model-invocation"), flag("code-review-core", "user-invocable")),
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
                        [sys.executable, "-B", str(script), "locate", name, "--repo", str(REPOSITORY_ROOT),
                         "--home", home],
                        capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
                    lines = completed.stdout.splitlines()
                    expected = (REPOSITORY_ROOT / "skills" / name / "SKILL.md").as_posix()
                    self.assertEqual([f"SKILL_FILE {expected}", "SCOPE source"], [lines[1], lines[3]])

    def test_the_runtime_canary_is_a_manual_gate_that_validation_never_runs(self) -> None:
        canary = (REPOSITORY_ROOT / ".claude/skills/runtime-canary/SKILL.md").read_text(encoding="utf-8-sig")
        self.assertIn("python -B tools/runtime_canary.py <skill>", canary)
        self.assertTrue((REPOSITORY_ROOT / "tools/runtime_canary.py").is_file())
        change_skill = (REPOSITORY_ROOT / ".claude/skills/change-skill/SKILL.md").read_text(encoding="utf-8-sig")
        self.assertIn("run the `runtime-canary` repository skill", change_skill)
        template = (REPOSITORY_ROOT / ".github/pull_request_template.md").read_text(encoding="utf-8-sig")
        self.assertIn("the `runtime-canary` lines are below", template)
        # Each run calls a model, so neither the runner nor a policy check may start it.
        self.assertNotIn("runtime_canary", (REPOSITORY_ROOT / "tests/run_validation.py").read_text(encoding="utf-8"))

    def test_no_skill_pins_a_model(self) -> None:
        # In Claude Code a skill's `model` applies for the rest of the turn that invoked it (#75), so a code review
        # started in the same turn as `update-coding-agent-skills`, then pinned to Haiku, ran every reviewer on Haiku
        # and missed a must-fix finding. A skill runs on the session's model; pin one only with a measured reason
        # and an entry here.
        allowed: dict[str, str] = {}
        pinned = {
            path.parent.name: line.split(":", 1)[1].strip()
            for path in sorted((REPOSITORY_ROOT / "skills").glob("*/SKILL.md"))
            for line in path.read_text(encoding="utf-8-sig").split("---", 2)[1].splitlines()
            if re.match(r"model\s*:", line)
        }
        self.assertEqual(allowed, pinned)

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
                "          command: >-",
                '            python -I -B -c "import os, runpy;',
                "            runpy.run_path(os.path.expanduser('~/.claude/skills/code-review-core/scripts/review_guard.py'),",
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

    def test_the_reviewer_hook_runs_the_deployed_guard_whatever_home_says(self) -> None:
        # Claude Code runs hooks in Git Bash, whose $HOME follows HOME, or in PowerShell when it finds no Git Bash.
        # The deployer and Claude Code both use the profile folder, so the hook must find the guard there in both.
        from deployer import platform_support

        frontmatter = (REPOSITORY_ROOT / "agents/code-review-reviewer.md").read_text(encoding="utf-8").split("---")[1]
        lines = frontmatter.splitlines()
        start = lines.index("          command: >-") + 1
        folded = [line.strip() for line in lines[start:] if line.startswith("            ")]
        command = " ".join(folded)  # a folded block scalar joins its lines with single spaces
        self.assertNotIn("$", command, "neither shell may expand anything in the command")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            profile = root / "Some O'Neil (profile)"
            guard = profile / ".claude/skills/code-review-core/scripts/review_guard.py"
            guard.parent.mkdir(parents=True)
            guard.write_bytes((REPOSITORY_ROOT / "skills/code-review-core/scripts/review_guard.py").read_bytes())
            # A session's working directory is untrusted: its modules must never shadow the standard library.
            cwd = root / "session"
            cwd.mkdir()
            for module in ("json", "runpy", "pathlib", "re"):
                (cwd / f"{module}.py").write_text("print('HIJACKED')\nraise SystemExit(0)\n", encoding="utf-8")
            other = root / "other home"
            environment = {**os.environ, "USERPROFILE": str(profile), "HOME": "/" + other.as_posix()[0].lower()
                           + other.as_posix()[2:]}
            event = json.dumps({"tool_name": "Read", "tool_input": {"file_path": str(cwd / "json.py")},
                                "cwd": str(cwd)})
            shells = {
                "Git Bash": [platform_support.find_bash(), "-c", command],
                "PowerShell": [platform_support.find_pwsh(os.environ.get("PATH", "")), "-NoProfile", "-Command",
                               command],
            }
            for shell, arguments in shells.items():
                with self.subTest(shell=shell):
                    self.assertIsNotNone(arguments[0], f"{shell} is a validation prerequisite")
                    result = subprocess.run(arguments, input=event, capture_output=True, text=True, cwd=cwd,
                                            env=environment, check=False)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertNotIn("HIJACKED", result.stdout)
                    decision = json.loads(result.stdout)["hookSpecificOutput"]
                    self.assertEqual("deny", decision["permissionDecision"])
                    self.assertIn("may only look inside the review run folder", decision["permissionDecisionReason"])
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

    def test_the_tracker_reviews_its_candidates_in_one_review_prs_pass(self) -> None:
        # One invocation per candidate made each pull request wait for the previous one's slowest reviewer.
        def read(name: str) -> str:
            return (REPOSITORY_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8-sig")

        tracker = read("update-pr-tracker")
        self.assertIn(
            "invoke `review-prs` once for every confirmed pull request, with `--pull <owner/repo#number>` for each "
            "`missing` and `--re-review <owner/repo#number>` for each `stale`", tracker)
        self.assertNotIn("`re-review <owner/repo#number>`", tracker)
        review_prs = read("review-prs")
        self.assertIn('--pull owner/repo#number ... --re-review owner/repo#number ...', review_prs.split("---", 2)[1])
        self.assertIn("(or `--re-review` for one given with `--re-review`)", review_prs)
        pipeline = (REPOSITORY_ROOT / "skills/code-review-core/scripts/review_pipeline.py").read_text(encoding="utf-8")
        self.assertIn('prepare_parser.add_argument("--re-review", action="append"', pipeline)

    def test_review_prs_states_its_runtime_instead_of_leaving_it_to_path(self) -> None:
        # PATH says which CLIs are installed, not which one is orchestrating, so review-prs names its host (#45).
        skill = (REPOSITORY_ROOT / "skills/review-prs/SKILL.md").read_text(encoding="utf-8-sig")
        self.assertIn('review_pipeline.py" prepare --host "<runtime>" --pull', skill)
        self.assertIn("`--host` names the runtime this session is running in: `claude-code`, `codex`, or `copilot-cli`.",
                      skill)
        pipeline = (REPOSITORY_ROOT / "skills/code-review-core/scripts/review_pipeline.py").read_text(encoding="utf-8")
        self.assertIn('prepare_parser.add_argument("--host", choices=sorted(RUNTIME_CAPABILITIES)', pipeline)
        runtime = (REPOSITORY_ROOT / "skills/code-review-core/scripts/review_runtime.py").read_text(encoding="utf-8")
        hosts = runtime.split("RUNTIME_CAPABILITIES = {", 1)[1].split("\n}", 1)[0]
        self.assertEqual(["claude-code", "codex", "copilot-cli"],
                         [line.split('"')[1] for line in hosts.strip().splitlines()])

    def test_a_re_review_scope_is_asked_for_and_never_assumed(self) -> None:
        # A full or incremental pass is the user's call: the tracker asks once per run, a direct call asks itself.
        def read(name: str) -> tuple[str, str]:
            text = (REPOSITORY_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8-sig")
            return text.split("---", 2)[1], text

        for name in ("review-prs", "update-pr-tracker"):
            with self.subTest(asks=name):
                self.assertIn('"AskUserQuestion"', read(name)[0])
        self.assertIn("If `--re-review` was given without `--scope`, ask the user once with AskUserQuestion, offering "
                      "`auto`, `full`, and `incremental` in that order; never choose one yourself.", read("review-prs")[1])
        tracker = read("update-pr-tracker")[1]
        self.assertIn("Ask once for the run, never per pull request.", tracker)
        self.assertIn("plus `--scope <scope>` with the chosen scope when any is `stale`", tracker)
        pipeline = (REPOSITORY_ROOT / "skills/code-review-core/scripts/review_pipeline.py").read_text(encoding="utf-8")
        self.assertIn('parser.error("--scope is required with --re-review and taken only with it")', pipeline)

    def test_no_skill_replays_another_skills_steps(self) -> None:
        # re-review replayed "steps 2 to 5" of review-prs, so renumbering review-prs silently broke it (#116).
        # A skill invokes another skill by name instead; it never reads that skill's SKILL.md or cites its steps.
        retired = (
            'Read `${CLAUDE_SKILL_DIR}/../review-prs/SKILL.md` and follow its steps 2 to 5 exactly for this one pull '
            'request (or its "With the Workflow tool" section when that tool is available).'
        )
        names = {path.parent.name for path in (REPOSITORY_ROOT / "skills").glob("*/SKILL.md")}
        self.assertEqual(["review-prs"], replayed_skills(retired, "re-review", names))
        self.assertEqual(["review-prs"], replayed_skills('Then do step 3 of `review-prs` again.', "x", names))
        self.assertEqual(["review-prs"], replayed_skills('Apply the "Posting" section of `review-prs`.', "x", names))
        self.assertEqual([], replayed_skills("Run step 2 again, then invoke `review-prs` with `--re-review`.", "x",
                                             names))
        for path in sorted((REPOSITORY_ROOT / "skills").glob("*/SKILL.md")):
            with self.subTest(skill=path.parent.name):
                body = path.read_text(encoding="utf-8-sig").split("---", 2)[2]
                self.assertEqual([], replayed_skills(body, path.parent.name, names))

    def test_re_review_is_retired_into_review_prs(self) -> None:
        # review-prs --re-review does everything the re-review alias did, so the alias is gone (#116).
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
        combined = "\n".join(
            path.read_text(encoding="utf-8-sig") for path in skill_paths
        )
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
