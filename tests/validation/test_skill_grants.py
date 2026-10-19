"""Fixture tests for tests/validation/skill_grants.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_grants import (
    GRANTS_DOC,
    REPOSITORY_TOOL_COMMAND,
    description_problems,
    repository_code_declaration_problems,
    repository_skill_files,
    repository_skill_problems,
    root_option_problems,
    skill_grant_problems,
    tool_grant_problems,
)
from validation_support import write_fixture_tree

# The allowance a script that starts the command a repository configures states, since its program is not the
# script's to name.
CONFIGURED = '{"<module>": "starts the command the repository configures"}'


class SkillGrantsFixtures(unittest.TestCase):
    def test_repository_skill_shim_check_detects_each_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def write(relative: str, text: str) -> None:
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_text(text, encoding="utf-8")

            pointer = (
                "Read and follow `../../../.claude/skills/{0}/SKILL.md`, a path relative to this file, as the "
                "authoritative workflow.\n"
                "Run its commands from the repository root, the folder that holds `.agents` and `.claude`, and "
                "resolve every relative path it names from there.\n"
                "A command that writes a shim under `.agents` needs an approval to run outside Codex's sandbox, "
                "which keeps `.agents` read-only.\n"
            )
            write(".claude/skills/good/SKILL.md", "---\nname: good\ndescription: Good.\n---\n\nBody.\n")
            write(
                ".agents/skills/good/SKILL.md", "---\nname: good\ndescription: Good.\n---\n\n" + pointer.format("good")
            )
            write(".claude/skills/missing/SKILL.md", "---\nname: missing\ndescription: Missing.\n---\n")
            write(".claude/skills/drifted/SKILL.md", "---\nname: drifted\ndescription: New.\n---\n")
            write(
                ".agents/skills/drifted/SKILL.md",
                "---\nname: drifted\ndescription: Old.\n---\n\n" + pointer.format("drifted"),
            )
            write(".claude/skills/astray/SKILL.md", "---\nname: astray\ndescription: Astray.\n---\n")
            write(
                ".agents/skills/astray/SKILL.md",
                "---\nname: astray\ndescription: Astray.\n---\n\n" + pointer.format("other"),
            )
            write(".claude/skills/bare/SKILL.md", "# No frontmatter\n")
            write(".agents/skills/stray/SKILL.md", "---\nname: stray\ndescription: Stray.\n---\n")
            differs = "differs from what python tools/skill_shims.py --write writes; run it"
            self.assertEqual(
                [
                    ".agents/skills/stray/SKILL.md has no .claude/skills/stray/SKILL.md; delete it",
                    f".agents/skills/astray/SKILL.md {differs}",
                    ".claude/skills/bare/SKILL.md has no closed frontmatter for its shim to carry",
                    f".agents/skills/drifted/SKILL.md {differs}",
                    ".claude/skills/missing/SKILL.md has no .agents/skills/missing/SKILL.md shim; "
                    "run python tools/skill_shims.py --write",
                ],
                repository_skill_problems(root),
            )

    def test_grant_policy_holds_repository_skills_to_the_repository_tools_they_run(self) -> None:
        own = "python -B tools/a.py"
        twin = json.dumps([f"Bash({own} *)", f"PowerShell({own} *)"])
        skills = {
            "bare": ('["Bash"]', own),
            "unpaired": (json.dumps([f"Bash({own} *)"]), own),
            # gh acts outside the conversation, so it may keep prompting; tools/b.py is the repository's own.
            "ungranted": (twin, f'{own}\npython -B tools/b.py\ngh pr create --body-file "<file>"'),
            "good": (twin, f'{own} "<skill>"'),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    f".claude/skills/{skill}/SKILL.md": (
                        f"---\nname: {skill}\nallowed-tools: {allowed}\n---\n\n```bash\n{command}\n```\n"
                    )
                    for skill, (allowed, command) in skills.items()
                },
            )
            self.assertEqual(
                [
                    f".claude/skills/bare/SKILL.md grants Bash for every command; see {GRANTS_DOC}",
                    f".claude/skills/ungranted/SKILL.md:8 no Bash grant covers python -B tools/b.py; see {GRANTS_DOC}",
                    ".claude/skills/ungranted/SKILL.md:8 no PowerShell grant covers python -B tools/b.py; "
                    f"see {GRANTS_DOC}",
                    f".claude/skills/unpaired/SKILL.md grants Bash({own} *) in one shell only; see {GRANTS_DOC}",
                ],
                skill_grant_problems(root, repository_skill_files(root), REPOSITORY_TOOL_COMMAND),
            )

    def test_root_option_policy_refuses_a_star_over_an_unbounded_root(self) -> None:
        parser = 'import argparse\nparser = argparse.ArgumentParser()\nparser.add_argument("--write")\n'
        tools = {
            # Each bounds --root through the checkout check, imported by name or reached through its module.
            "named": "from tools.checkout import checkout_root\n"
            + parser
            + ('parser.add_argument("--root", type=checkout_root)\n'),
            "module": "from tools import checkout\n"
            + parser
            + ('parser.add_argument("--root", type=checkout.checkout_root)\n'),
            # Each lets --root name any directory.
            "path": "from pathlib import Path\n" + parser + 'parser.add_argument("--root", type=Path)\n',
            "untyped": parser + 'parser.add_argument("--root")\n',
            "lookalike": "from tools.other import checkout_root\n"
            + parser
            + ('parser.add_argument("--root", type=checkout_root)\n'),
            "plain": parser,
        }
        skills = {f"star-{tool}": f"python tools/{tool}.py *" for tool in tools}
        # A grant with no * names one command, which cannot carry --root.
        skills["exact-path"] = "python tools/path.py --write"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    **{f"tools/{tool}.py": text for tool, text in tools.items()},
                    **{
                        f".claude/skills/{skill}/SKILL.md": (
                            f"---\nname: {skill}\nallowed-tools: "
                            f"{json.dumps([f'Bash({command})', f'PowerShell({command})'])}\n---\n"
                        )
                        for skill, command in skills.items()
                    },
                },
            )
            found = root_option_problems(root, repository_skill_files(root))
        self.assertEqual(
            [
                f".claude/skills/star-{tool}/SKILL.md grants {shell}(python tools/{tool}.py *), whose * covers "
                f"tools/{tool}.py --root, which it does not parse with tools.checkout.checkout_root; bound the option "
                f"or grant the forms the skill runs; see {GRANTS_DOC}"
                for tool in ("lookalike", "path", "untyped")
                for shell in ("Bash", "PowerShell")
            ],
            found,
        )

    def test_tool_grant_policy_holds_a_sessions_grants_to_the_skill_grant_rules(self) -> None:
        # A grant a tool passes with --allowedTools pre-approves its commands for the whole session, so a * that
        # continues a script's path matches any file named like it there too; a quoted path ends before the *.
        wildcard = 'python -B "*review_pipeline.py" validate-result --run *'
        exact = '{PYTHON} -B "{folder}review_pipeline.py" validate-result --run *'
        start = 'command = [\n    "claude",\n    "--allowedTools",\n'
        tools = {
            "wildcard": f"{start}    'Bash({wildcard})',\n    'PowerShell({wildcard})',\n    \"--model\",\n]\n",
            "exact": (
                "def grants(folder):\n"
                '    """The grants, each script by its quoted path."""\n'
                f"    return [f'Bash({exact})', f'PowerShell({exact})']\n"
                f"{start}    *grants('C:/home/'),\n]\n"
            ),
            "one_shell": f"PYTHON = 'python'\n{start}    f'Bash({exact})',\n]\n",
            "built": (
                f"COMMANDS = ('{wildcard}',)\nSHELLS = ('Bash', 'PowerShell')\n"
                f"{start}    *(f'{{tool}}({{command}})' for command in COMMANDS for tool in SHELLS),\n]\n"
            ),
            "helper": (
                "def grants():\n"
                "    script = 'review_pipeline.py'\n"
                "    return [f'Bash(python -B \"{script}\" *)', f'PowerShell(python -B \"{script}\" *)']\n"
                f"{start}    *grants(),\n]\n"
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, {f"tools/{tool}.py": text for tool, text in tools.items()})
            found = tool_grant_problems(root)

        def unreadable(tool: str, line: int, source: str) -> str:
            return (
                f"tools/{tool}.py:{line} passes {source} to --allowedTools, which this policy cannot read as "
                f"grants; write each grant as a string or an f-string, or return them from a function of the module; "
                f"see {GRANTS_DOC}"
            )

        self.assertEqual(
            [
                unreadable("built", 5, "*(f'{tool}({command})' for command in COMMANDS for tool in SHELLS)"),
                unreadable("helper", 6, "*grants()"),
                f"tools/one_shell.py:4 grants Bash({exact}) in one shell only; see {GRANTS_DOC}",
                *(
                    f"tools/wildcard.py:3 grants {shell}({wildcard}), whose * continues a path, so it reaches every "
                    f"skill's scripts through ..; grant each script by name; see {GRANTS_DOC}"
                    for shell in ("Bash", "PowerShell")
                ),
            ],
            found,
        )

    def test_description_policy_holds_repository_skills(self) -> None:
        descriptions = {
            "long": "x" * 1020 + " Use it when asked.",
            "silent": "Does things.",
            "user": "Does things.\ndisable-model-invocation: true",
            "good": "Does things. Use it when asked.",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    f".claude/skills/{skill}/SKILL.md": f"---\nname: {skill}\ndescription: {text}\n---\n\nBody.\n"
                    for skill, text in descriptions.items()
                },
            )
            from tools import skill_reference

            self.assertEqual(
                [
                    ".claude/skills/long/SKILL.md: Skill 'long' description is 1039 characters; runtime adapters "
                    "allow at most 1024",
                    ".claude/skills/silent/SKILL.md: the model may start it, so its description must "
                    f"{skill_reference.WHEN_RULE}",
                ],
                description_problems(root, repository_skill_files(root)),
            )

    def test_skill_grant_policy_detects_unscoped_unpaired_unused_and_ungranted_grants(self) -> None:
        own = 'python -B "${CLAUDE_SKILL_DIR}/scripts/'
        script = f'{own}x.py" *'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            skills = {
                "bare": ('["Bash"]', f'{own}x.py"'),
                "unpaired": (json.dumps([f"Bash({script})"]), f'{own}x.py"'),
                "ungranted": (
                    json.dumps([f"Bash({script})", f"PowerShell({script})"]),
                    f'{own}x.py"\npython -B "${{CLAUDE_SKILL_DIR}}/../core/scripts/y.py"\ngit status',
                ),
                "none": ('["Read"]', f'{own}x.py"'),
                "unused": (json.dumps([f"Bash({script})", f"PowerShell({script})", "Glob"]), f'{own}x.py"'),
                "expands": (json.dumps([f"Bash({script})", f"PowerShell({script})"]), f'{own}x.py" --cwd "$PWD"'),
                "good": (json.dumps([f"Bash({script})", f"PowerShell({script})"]), f'{own}x.py" --plan "<plan file>"'),
            }
            for skill, (allowed, command) in skills.items():
                (root / "deploy-meta" / f"{skill}.json").write_text("{}", encoding="utf-8")
                directory = root / "skills" / skill
                directory.mkdir(parents=True)
                (directory / "SKILL.md").write_text(
                    f"---\nname: {skill}\nallowed-tools: {allowed}\n---\n\n```bash\n{command}\n```\n", encoding="utf-8"
                )
            (root / "skills" / "asset").mkdir()
            self.assertEqual(
                [
                    f"skills/bare/SKILL.md grants Bash for every command; see {GRANTS_DOC}",
                    "skills/expands/SKILL.md:7 expands a shell variable, "
                    f'so it always prompts: {own}x.py" --cwd "$PWD"; '
                    f"see {GRANTS_DOC}",
                    f"skills/none/SKILL.md grants Read, which no step uses; see {GRANTS_DOC}",
                    f"skills/none/SKILL.md:6 runs a command without a shell grant; see {GRANTS_DOC}",
                    f"skills/ungranted/SKILL.md:8 no Bash grant covers "
                    f'python -B "${{CLAUDE_SKILL_DIR}}/../core/scripts/y.py"; see {GRANTS_DOC}',
                    f"skills/ungranted/SKILL.md:8 no PowerShell grant covers "
                    f'python -B "${{CLAUDE_SKILL_DIR}}/../core/scripts/y.py"; see {GRANTS_DOC}',
                    f"skills/unpaired/SKILL.md grants Bash({script}) in one shell only; see {GRANTS_DOC}",
                    f"skills/unused/SKILL.md grants Glob, which no step uses; see {GRANTS_DOC}",
                ],
                skill_grant_problems(root),
            )

    def test_grant_policy_fails_a_sibling_grant_that_does_not_name_its_script(self) -> None:
        sibling = 'python -B "${CLAUDE_SKILL_DIR}/../core/scripts/'
        command = f'{sibling}y.py" run'
        skills = {
            "folder": json.dumps([f"Bash({sibling}*)", f"PowerShell({sibling}*)"]),
            "named": json.dumps([f'Bash({sibling}y.py" *)', f'PowerShell({sibling}y.py" *)']),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files: dict[str, str] = {}
            for skill, allowed in skills.items():
                files[f"deploy-meta/{skill}.json"] = "{}"
                files[f"skills/{skill}/SKILL.md"] = (
                    f"---\nname: {skill}\nallowed-tools: {allowed}\n---\n\n```bash\n{command}\n```\n"
                )
            write_fixture_tree(root, files)
            self.assertEqual(
                [
                    f"skills/folder/SKILL.md grants {tool}({sibling}*), a sibling skill's folder rather than one "
                    f"script by name; see {GRANTS_DOC}"
                    for tool in ("Bash", "PowerShell")
                ],
                skill_grant_problems(root),
            )

    def test_grant_policy_fails_a_wildcard_that_continues_a_path(self) -> None:
        # Claude Code normalizes no path in a shell grant, so a pattern whose * continues the path before it also
        # matches scripts/../../<other skill>/scripts/<script>.py; one that ends the path first matches one script.
        own = 'python -B "${CLAUDE_SKILL_DIR}/scripts/'
        shipped = {
            "folder": f"{own}*",
            "prefix": f'{own}x*.py" *',
            "named": f'{own}x.py" *',
        }
        repository = {"glued": "python -B tools/a.py*", "spaced": "python -B tools/a.py *"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files: dict[str, str] = {}
            for skill, pattern in shipped.items():
                allowed = json.dumps([f"Bash({pattern})", f"PowerShell({pattern})"])
                files[f"deploy-meta/{skill}.json"] = "{}"
                files[f"skills/{skill}/SKILL.md"] = (
                    f'---\nname: {skill}\nallowed-tools: {allowed}\n---\n\n```bash\n{own}x.py" run\n```\n'
                )
            for skill, pattern in repository.items():
                allowed = json.dumps([f"Bash({pattern})", f"PowerShell({pattern})"])
                files[f".claude/skills/{skill}/SKILL.md"] = (
                    f"---\nname: {skill}\nallowed-tools: {allowed}\n---\n\n```bash\npython -B tools/a.py\n```\n"
                )
            write_fixture_tree(root, files)

            def problems(name: str, pattern: str) -> list[str]:
                return [
                    f"{name} grants {tool}({pattern}), whose * continues a path, so it reaches every skill's scripts "
                    f"through ..; grant each script by name; see {GRANTS_DOC}"
                    for tool in ("Bash", "PowerShell")
                ]

            self.assertEqual(
                [
                    *problems("skills/folder/SKILL.md", shipped["folder"]),
                    *problems("skills/prefix/SKILL.md", shipped["prefix"]),
                ],
                skill_grant_problems(root),
            )
            self.assertEqual(
                problems(".claude/skills/glued/SKILL.md", repository["glued"]),
                skill_grant_problems(root, repository_skill_files(root), REPOSITORY_TOOL_COMMAND),
            )

    def test_grant_policy_fails_a_grant_that_names_a_program_but_no_script(self) -> None:
        # A pattern that ends at a program and its switches pre-approves whatever code or subcommand its * supplies.
        own = 'python -B "${CLAUDE_SKILL_DIR}/scripts/'
        failing = {
            "interpreter": ("python *", "python x"),
            "inline": ("bash -c *", "bash -c x"),
            "command": ("pwsh -Command *", "pwsh -Command x"),
            "git": ("git *", "git x"),
        }
        passing = {
            "script": (f'{own}x.py" *', f'{own}x.py" run'),
            "subcommand": ("gh pr create *", "gh pr create --fill"),
            "exact": ("git rev-parse --show-toplevel", "git rev-parse --show-toplevel"),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files: dict[str, str] = {}
            for skill, (pattern, command) in {**failing, **passing}.items():
                allowed = json.dumps([f"Bash({pattern})", f"PowerShell({pattern})"])
                files[f"deploy-meta/{skill}.json"] = "{}"
                files[f"skills/{skill}/SKILL.md"] = (
                    f"---\nname: {skill}\nallowed-tools: {allowed}\n---\n\n```bash\n{command}\n```\n"
                )
            write_fixture_tree(root, files)
            self.assertEqual(
                [
                    f"skills/{skill}/SKILL.md grants {tool}({pattern}), whose text before its * names no script or "
                    f"git or gh subcommand, so it pre-approves any code the * supplies; grant each script by name; "
                    f"see {GRANTS_DOC}"
                    for skill, (pattern, _) in sorted(failing.items())
                    for tool in ("Bash", "PowerShell")
                ],
                skill_grant_problems(root),
            )

    def test_grant_policy_reads_repository_code_commands_wherever_the_body_shows_them(self) -> None:
        # A code span, or an indented, list item, blockquoted, tilde, or zsh fence, shows a command as a top-level
        # bash fence does, so a grant over a script that runs repository code fails in each.
        own = 'python -B "${CLAUDE_SKILL_DIR}/scripts/'
        handshake = f'{own}start.py" --root "<repository root>"'
        declaring = (
            'import subprocess\n\nRUNS_REPOSITORY_CODE = "starts each configured server"\nsubprocess.run(command)\n'
            f"COMMANDS_ALLOWED = {CONFIGURED}\n"
        )
        shapes = {
            "span": f"Run `{handshake}` from the root.\n",
            "indented": f"1. Start the servers:\n\n    ```bash\n    {handshake}\n    ```\n",
            "marker": f"- ```bash\n  {handshake}\n  ```\n",
            "quoted": f"> ```bash\n> {handshake}\n> ```\n",
            "tilde": f"~~~sh\n{handshake}\n~~~\n",
            "zsh": f"```zsh\n{handshake}\n```\n",
        }
        wide = json.dumps(
            [f'{tool}({own}{script}.py" *)' for script in ("audit", "start") for tool in ("Bash", "PowerShell")]
        )
        narrow = json.dumps([f'Bash({own}audit.py" *)', f'PowerShell({own}audit.py" *)'])
        audit = f'Then run `{own}audit.py" --json`.\n'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files: dict[str, str] = {}
            for shape, body in shapes.items():
                for grant, allowed in (("wide", wide), ("narrow", narrow)):
                    skill = f"{shape}-{grant}"
                    files[f"deploy-meta/{skill}.json"] = "{}"
                    files[f"skills/{skill}/scripts/start.py"] = declaring
                    files[f"skills/{skill}/SKILL.md"] = (
                        f"---\nname: {skill}\nallowed-tools: {allowed}\n---\n\n{body}{audit}"
                    )
            write_fixture_tree(root, files)
            found = skill_grant_problems(root)
            for shape in shapes:
                with self.subTest(shape=shape):
                    wide_problems = [line for line in found if line.startswith(f"skills/{shape}-wide/")]
                    self.assertEqual(2, len(wide_problems), found)
                    for tool, line in zip(("Bash", "PowerShell"), wide_problems, strict=True):
                        self.assertIn(f"a {tool} grant pre-approves {handshake}, which runs code from the target", line)
                    self.assertEqual([], [line for line in found if line.startswith(f"skills/{shape}-narrow/")])

    def test_grant_policy_fails_grants_that_approve_nothing_the_skill_runs(self) -> None:
        own = 'python -B "${CLAUDE_SKILL_DIR}/scripts/'
        script = f'{own}x.py" *'
        hidden = "disable-model-invocation: true\nuser-invocable: false\n"
        skills = {
            "hidden": (f'{hidden}allowed-tools: ["Read"]\n', "Use the `Read` tool."),
            "quiet": (hidden, "Imported by other skills."),
            "idle": (
                "allowed-tools: " + json.dumps([f"Bash({script})", f"PowerShell({script})"]) + "\n",
                "```bash\ngit status\n```",
            ),
            "spanned": (
                'allowed-tools: ["Bash(git rev-parse --show-toplevel)", "PowerShell(git rev-parse --show-toplevel)"]\n',
                "Confirm the target is a repository: run `git rev-parse --show-toplevel`.",
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files: dict[str, str] = {}
            for skill, (front, body) in skills.items():
                files[f"deploy-meta/{skill}.json"] = "{}"
                files[f"skills/{skill}/SKILL.md"] = f"---\nname: {skill}\n{front}---\n\n{body}\n"
            write_fixture_tree(root, files)
            self.assertEqual(
                [
                    f"skills/hidden/SKILL.md is hidden, so no turn starts it and its allowed-tools approve nothing; "
                    f"see {GRANTS_DOC}",
                    *(
                        f"skills/idle/SKILL.md grants {tool}({script}), which matches no command in its fences or code "
                        f"spans; see {GRANTS_DOC}"
                        for tool in ("Bash", "PowerShell")
                    ),
                ],
                skill_grant_problems(root),
            )

    def test_grant_policy_fails_a_grant_over_a_script_that_runs_repository_code(self) -> None:
        own = 'python -B "${CLAUDE_SKILL_DIR}/scripts/'
        handshake = f'{own}start.py" --root "<repository root>"'
        declaring = (
            'import subprocess\n\nRUNS_REPOSITORY_CODE = "starts each configured server"\nsubprocess.run(command)\n'
            f"COMMANDS_ALLOWED = {CONFIGURED}\n"
        )
        grants = {
            "wide": json.dumps(
                [f'{tool}({own}{script}.py" *)' for script in ("audit", "start") for tool in ("Bash", "PowerShell")]
            ),
            "narrow": json.dumps([f'Bash({own}audit.py" *)', f'PowerShell({own}audit.py" *)']),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files: dict[str, str] = {}
            for skill, allowed in grants.items():
                files[f"deploy-meta/{skill}.json"] = "{}"
                files[f"skills/{skill}/scripts/start.py"] = declaring
                files[f"skills/{skill}/SKILL.md"] = (
                    f"---\nname: {skill}\nallowed-tools: {allowed}\n---\n\n"
                    f'```bash\n{own}audit.py" --json\n{handshake}\n```\n'
                )
            write_fixture_tree(root, files)
            self.assertEqual([], repository_code_declaration_problems(root))
            self.assertEqual(
                [
                    f"skills/wide/SKILL.md:8 a {tool} grant pre-approves {handshake}, which runs code from the "
                    f"target repository; see {GRANTS_DOC}"
                    for tool in ("Bash", "PowerShell")
                ],
                skill_grant_problems(root),
            )

    def test_repository_code_declarations_report_unresolved_aliases_and_unreadable_commands(self) -> None:
        alias_error = "cannot resolve callable alias runner; use a single binding to a known callable"
        unread = (
            "; name the program with a literal or a lookup, or state why in COMMANDS_ALLOWED; "
            'see "Commands skills may run" in docs/adding-a-skill.md'
        )
        rebound = "import os\nrunner = os.system\nrunner = None\nrunner(command)\n"
        sources = (
            (rebound, [(5, alias_error)], False),
            (
                rebound + "from os import popen as valid_start\nvalid_start(command)\n",
                [
                    (5, alias_error),
                    (7, f"cannot read the program in `command`, which the module binds to nothing it reads{unread}"),
                ],
                True,
            ),
            (
                'import subprocess\nrunner = subprocess.run\nCOMMAND = f"{program} arguments"\nrunner(COMMAND)\n',
                [(4, f"cannot read f-string command without a literal program prefix{unread}")],
                True,
            ),
        )
        for source, errors, starts_process in sources:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                name = "skills/a/scripts/start.py"
                write_fixture_tree(root, {name: 'RUNS_REPOSITORY_CODE = "starts servers"\n' + source})
                expected = [f"{name}:{line} {error}" for line, error in errors]
                if not starts_process:
                    expected.append(
                        f"{name}: declares RUNS_REPOSITORY_CODE but starts no process; remove the declaration"
                    )
                self.assertEqual(expected, repository_code_declaration_problems(root))

    def test_repository_code_declarations_must_be_readable_and_current(self) -> None:
        declared = 'RUNS_REPOSITORY_CODE = "starts servers"\n'
        command = '["server", "--stdio"]'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "skills/a/scripts/good.py": f"from subprocess import Popen\n\n{declared}Popen({command})\n",
                    "skills/a/scripts/bounded.py": "import bounded_process\n\n"
                    f'RUNS_REPOSITORY_CODE: str = "x"\nbounded_process.run_bounded({command}, 5)\n',
                    "skills/a/scripts/plain.py": "import json\n",
                    "skills/a/scripts/empty.py": 'import subprocess\n\nRUNS_REPOSITORY_CODE = ""\n'
                    f"subprocess.run({command})\n",
                    "skills/a/scripts/twice.py": f"import subprocess\n\n{declared}{declared}"
                    f"subprocess.run({command})\n",
                    "skills/a/scripts/nested.py": f"import subprocess\n\n\ndef f() -> None:\n    {declared}"
                    f"    subprocess.run({command})\n",
                    ".claude/skills/b/scripts/stale.py": f"import json\n\n{declared}",
                },
            )
            rule = "RUNS_REPOSITORY_CODE must be one module-level assignment of a non-empty reason"
            self.assertEqual(
                [
                    ".claude/skills/b/scripts/stale.py: declares RUNS_REPOSITORY_CODE but starts no process; "
                    "remove the declaration",
                    f"skills/a/scripts/empty.py: {rule}",
                    f"skills/a/scripts/nested.py: {rule}",
                    f"skills/a/scripts/twice.py: {rule}",
                ],
                repository_code_declaration_problems(root),
            )


# A script that starts a configured command states why, beside the code, for each scope that starts one.
CONFIGURED_ALLOWANCE = f"COMMANDS_ALLOWED = {CONFIGURED}\n"


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('import subprocess as sp\nsp.run(f"python {script}")\n', id="subprocess under an alias"),
        pytest.param(
            f"from subprocess import Popen as start\nstart(command)\n{CONFIGURED_ALLOWANCE}", id="Popen renamed"
        ),
        pytest.param(
            f"import os as operating\noperating.system(command)\n{CONFIGURED_ALLOWANCE}", id="os under an alias"
        ),
        pytest.param(
            f"from os import popen as start\nstart(cmd=command)\n{CONFIGURED_ALLOWANCE}", id="popen by keyword"
        ),
        pytest.param(
            f"from os import system\nrunner = system\nalias = runner\nalias(command)\n{CONFIGURED_ALLOWANCE}",
            id="system through aliases",
        ),
        pytest.param(
            f"import asyncio as tasks\ntasks.create_subprocess_exec(program=program)\n{CONFIGURED_ALLOWANCE}",
            id="asyncio exec by keyword",
        ),
        pytest.param(
            f"from asyncio import create_subprocess_shell as start\nstart(cmd=command)\n{CONFIGURED_ALLOWANCE}",
            id="asyncio shell renamed",
        ),
        pytest.param(
            f"import bounded_process as bounded\nbounded.run_bounded(command, 5)\n{CONFIGURED_ALLOWANCE}",
            id="run_bounded",
        ),
        pytest.param(
            f"from bounded_process import streaming as start\nstart(command, 5)\n{CONFIGURED_ALLOWANCE}",
            id="streaming renamed",
        ),
        pytest.param('from git_client import GitClient as Client\nClient().run(["status"])\n', id="GitClient renamed"),
        pytest.param('import github_client as api\napi.GitHubClient().run(["api"])\n', id="GitHubClient by module"),
        pytest.param(
            "from github_client import GitHubClient\nclient = GitHubClient\nalias = client\nalias()\n",
            id="GitHubClient through aliases",
        ),
        pytest.param(
            "import os\n"
            "def first():\n    runner = os.system\n    runner(command)\n"
            "def second():\n    runner = os.popen\n    runner(command)\n"
            'COMMANDS_ALLOWED = {"first": "starts the configured server", "second": "reads the configured server"}\n',
            id="a runner in each of two scopes",
        ),
    ],
)
def test_repository_code_declarations_accept_process_routes_but_refuse_their_grants(
    tmp_path: Path, source: str
) -> None:
    own = 'python -B "${CLAUDE_SKILL_DIR}/scripts/'
    command = f'{own}start.py" --root "<repository root>"'
    files: dict[str, str] = {}
    for skill, scripts in (("wide", ("audit", "start")), ("narrow", ("audit",))):
        grants = json.dumps([f'{tool}({own}{script}.py" *)' for script in scripts for tool in ("Bash", "PowerShell")])
        files[f"deploy-meta/{skill}.json"] = "{}"
        files[f"skills/{skill}/scripts/start.py"] = (
            'RUNS_REPOSITORY_CODE = "starts configured repository code"\n' + source
        )
        # Starting a process alone does not make an own-script command repository code.
        files[f"skills/{skill}/scripts/audit.py"] = "import os\nos.system(command)\n"
        files[f"skills/{skill}/SKILL.md"] = (
            f'---\nname: {skill}\nallowed-tools: {grants}\n---\n\n```bash\n{own}audit.py" --check\n{command}\n```\n'
        )
    write_fixture_tree(tmp_path, files)
    assert repository_code_declaration_problems(tmp_path) == []
    assert [
        f"skills/wide/SKILL.md:8 a {tool} grant pre-approves {command}, which runs code from the "
        f"target repository; see {GRANTS_DOC}"
        for tool in ("Bash", "PowerShell")
    ] == skill_grant_problems(tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import subprocess\n", id="an import"),
        pytest.param("from bounded_process import run_bounded\nrunner = run_bounded\n", id="a runner never called"),
        pytest.param("from github_client import GitHubClient\nclient = GitHubClient\n", id="a client never made"),
        pytest.param("import os\nos.getenv('COMMAND')\n", id="an environment lookup"),
        pytest.param("import asyncio\nasyncio.run(main())\n", id="asyncio.run"),
        pytest.param('import shutil\nshutil.which("python")\n', id="a program lookup"),
        pytest.param('COMMAND = ["python", script]\nprint(COMMAND)\n', id="a command never run"),
        pytest.param(
            "import os\nrunner = os.system\ndef run(runner):\n    runner(command)\n", id="a parameter shadows a runner"
        ),
        pytest.param("import os\ndef run():\n    os = service\n    os.system(command)\n", id="a local shadows os"),
        pytest.param("import subprocess\ndef run(command):\n    pass\nrun(command)\n", id="a function named run"),
        pytest.param("from .os import system\nsystem(command)\n", id="a relative module named os"),
    ],
)
def test_repository_code_declarations_refuse_imports_lookups_and_shadowed_calls_without_a_process(
    tmp_path: Path, source: str
) -> None:
    name = ".claude/skills/a/scripts/stale.py"
    write_fixture_tree(tmp_path, {name: 'RUNS_REPOSITORY_CODE = "starts servers"\n' + source})
    assert [
        f"{name}: declares RUNS_REPOSITORY_CODE but starts no process; remove the declaration"
    ] == repository_code_declaration_problems(tmp_path)
