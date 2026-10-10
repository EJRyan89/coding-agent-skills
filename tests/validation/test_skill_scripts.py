"""Fixture tests for tests/validation/skill_scripts.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skill_scripts import (
    client_command_problems,
    console_setup_problems,
    gh_filter_problems,
    script_contract_problems,
    secret_named_function_problems,
    shell_commands,
    skill_command_problems,
)
from validation_support import write_fixture_tree


def mark_skills(root: Path) -> Path:
    """Give each skills/<name> folder with a scripts/ directory the SKILL.md that makes it a skill, and return root."""
    for scripts in (root / "skills").glob("*/scripts"):
        (scripts.parent / "SKILL.md").write_text(f"# {scripts.parent.name}\n", encoding="utf-8")
    return root


class SkillScriptsFixtures(unittest.TestCase):
    def test_script_contract_policy_detects_each_breach_and_honors_a_stated_exemption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "skills" / "alpha" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "run.py").write_text(
                "import json, sys\n"
                "def main(parser, value):\n"
                "    try:\n"
                "        value()\n"
                "    except OSError as exc:\n"
                "        parser.error(str(exc))\n"
                "    print(f'FAILED {value}', file=sys.stderr)\n"
                "    print(json.dumps(value))\n"
                "    if value:\n"
                "        return 3\n"
                "    parser.error('usage before any work is fine')\n"
                "    print('FAILED on stdout is fine')\n"
                "    return 2\n"
                "if __name__ == '__main__':\n"
                "    sys.exit(4)\n",
                encoding="utf-8",
            )
            (scripts / "hook.py").write_text(
                "import json\nEXIT_CONTRACT_EXEMPT = 'A hook protocol answers in JSON'\nprint(json.dumps({}))\n",
                encoding="utf-8",
            )
            (scripts / "vague.py").write_text("EXIT_CONTRACT_EXEMPT = ' '\n", encoding="utf-8")
            (scripts / "test_run.py").write_text("import sys\nsys.exit(5)\n", encoding="utf-8")
            (scripts / "tool.sh").write_text(
                'set -e\n[ -n "$1" ] || exit 3\nexit 0  # exit 6 in a comment\n', encoding="utf-8"
            )
            (scripts / "test_tool.sh").write_text("exit 7\n", encoding="utf-8")
            doc = '"Script results" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    f"skills/alpha/scripts/run.py:6 reports a failure through parser.error; print FAILED <reason> and "
                    f"exit 1; see {doc}",
                    f"skills/alpha/scripts/run.py:7 prints FAILED on stderr; print it on stdout; see {doc}",
                    f"skills/alpha/scripts/run.py:8 prints JSON; print one fact per line; see {doc}",
                    f"skills/alpha/scripts/run.py:10 exits 3; scripts exit only 0, 1, or 2; see {doc}",
                    f"skills/alpha/scripts/run.py:15 exits 4; scripts exit only 0, 1, or 2; see {doc}",
                    f"skills/alpha/scripts/tool.sh:2 exits 3; scripts exit only 0, 1, or 2; see {doc}",
                    "skills/alpha/scripts/vague.py: EXIT_CONTRACT_EXEMPT must be a non-empty string saying why",
                ],
                script_contract_problems(mark_skills(root)),
            )

    def test_secret_named_function_policy_flags_trusted_but_not_untrusted_or_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "skills" / "example" / "scripts"
            scripts.mkdir(parents=True)
            (root / "deployer").mkdir()
            (scripts / "runtime.py").write_text(
                "def resolve_trusted_commit():\n    pass\n\n\n"
                "class Reader:\n    def _trusted_files(self):\n        pass\n\n\n"
                "def is_trusted():\n    pass\n\n\ndef untrusted_text():\n    pass\n",
                encoding="utf-8",
            )
            (scripts / "test_runtime.py").write_text("def test_trusted_commit():\n    pass\n", encoding="utf-8")
            (root / "deployer" / "check.py").write_text("def trusted_paths():\n    pass\n", encoding="utf-8")
            self.assertEqual(
                [
                    "deployer/check.py:1: function trusted_paths",
                    "skills/example/scripts/runtime.py:1: function resolve_trusted_commit",
                    "skills/example/scripts/runtime.py:6: function _trusted_files",
                ],
                [problem.split(" is named", 1)[0] for problem in secret_named_function_problems(mark_skills(root))],
            )

    def test_shell_command_extraction_ignores_keywords_patterns_and_functions(self) -> None:
        script = (
            "set -euo pipefail\n"
            'BASE=$(git rev-parse HEAD | tr -d "\\r") && echo "$BASE" >/dev/null\n'
            'case "$1" in\n'
            '  main|release/*) grep -q "x|jq" file ;;\n'
            '  *) helper "rg" ;;\n'
            "esac\n"
            'for name in alpha beta; do basename "$name"; done\n'
            'helper() { if [ -n "$1" ]; then sed -n 1p "$1"; fi; }\n'
            'while read -r line; do printf "%s\\n" "$line" | jq .; done < list\n'
            '"$SCRIPT" --flag; ./local.sh\n'
        )
        self.assertEqual(
            {"basename", "echo", "git", "grep", "jq", "printf", "read", "sed", "set", "tr"}, shell_commands(script)
        )

    def test_client_command_policy_detects_git_and_gh_a_script_runs_itself(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "skills" / "alpha" / "scripts"
            (scripts / "nested").mkdir(parents=True)
            (scripts / "tool.py").write_text(
                "import subprocess\n"
                'subprocess.run(["git", "fetch", "--all"], capture_output=True)\n'
                'runner(("gh", "api", "user"))\n'
                'subprocess.run("git status", shell=True)\n'
                'subprocess.check_output("gh")\n'
                'client.run(["fetch", "--all"])\n'
                'which("gh")\n'
                'print(["github", "gitlab"])\n',
                encoding="utf-8",
            )
            (scripts / "nested" / "helper.py").write_text('run(["git", "status"])\n', encoding="utf-8")
            (scripts / "test_tool.py").write_text('run(["git", "init"])\n', encoding="utf-8")
            (scripts / "tool.sh").write_text("git fetch --all\n", encoding="utf-8")
            core = root / "skills" / "skill-core" / "scripts"
            core.mkdir(parents=True)
            (core / "git_client.py").write_text('command = ["git", *arguments]\n', encoding="utf-8")
            doc = '"Script results" in docs/adding-a-skill.md'
            git = "run it through skill-core's git_client.py's GitClient, which bounds it and turns prompts off"
            gh = "run it through skill-core's github_client.py's GitHubClient, which bounds it and turns prompts off"
            self.assertEqual(
                [
                    f"skills/alpha/scripts/nested/helper.py:1 runs git itself; {git}; see {doc}",
                    f"skills/alpha/scripts/tool.py:2 runs git itself; {git}; see {doc}",
                    f"skills/alpha/scripts/tool.py:3 runs gh itself; {gh}; see {doc}",
                    f"skills/alpha/scripts/tool.py:4 runs git itself; {git}; see {doc}",
                    f"skills/alpha/scripts/tool.py:5 runs gh itself; {gh}; see {doc}",
                ],
                client_command_problems(mark_skills(root)),
            )

    def test_client_command_policy_resolves_aliases_imported_runners_and_bound_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "skills" / "alpha" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "tool.py").write_text(
                "import shutil\n"
                "import subprocess as sp\n"
                "from subprocess import Popen\n"
                "\n"
                "from bounded_process import run_bounded\n"
                "\n"
                'SHELL = "gh auth status"\n'  # 7
                'GIT = shutil.which("git")\n'  # 8
                "sp.run(SHELL, shell=True)\n"
                'Popen("git log")\n'  # 10
                'run_bounded([GIT, "status"], 5)\n'
                'sp.run("echo git")\n',
                encoding="utf-8",
            )
            doc = '"Script results" in docs/adding-a-skill.md'
            git = "run it through skill-core's git_client.py's GitClient, which bounds it and turns prompts off"
            gh = "run it through skill-core's github_client.py's GitHubClient, which bounds it and turns prompts off"
            self.assertEqual(
                [
                    f"skills/alpha/scripts/tool.py:7 runs gh itself; {gh}; see {doc}",
                    f"skills/alpha/scripts/tool.py:8 runs git itself; {git}; see {doc}",
                    f"skills/alpha/scripts/tool.py:10 runs git itself; {git}; see {doc}",
                ],
                client_command_problems(mark_skills(root)),
            )

    def test_gh_filter_policy_detects_jq_template_and_json_query_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "skills" / "alpha" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "tool.py").write_text(
                'run(["gh", "api", "repos/o/r/pulls", "--jq", ".[].number"])\n'
                'run(["pr", "view", "--json", "baseRefName", "-q", ".baseRefName"])\n'
                'run(["gh", "api", "x", "--template", "{{.}}"])\n'
                'run(["git", "fetch", "-q"])\n'
                'run(["gh", "pr", "view", "--json", "baseRefName"])\n',
                encoding="utf-8",
            )
            (scripts / "tool.sh").write_text(
                "gh pr view --json baseRefName -q .baseRefName\n"
                "BASE=$(gh api repos/o/r --jq=.default_branch)\n"
                "git fetch -q origin\n"
                "gh api repos/o/r --paginate --slurp\n",
                encoding="utf-8",
            )
            (scripts / "tool.ps1").write_text("gh api repos/o/r --template '{{.name}}'\n", encoding="utf-8")
            (scripts / "test_tool.py").write_text('self.assertNotIn("--jq", calls)\n', encoding="utf-8")
            doc = '"Commands skills may run" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    f"skills/alpha/scripts/tool.ps1:1 filters gh output with --template; parse its JSON in Python "
                    f"instead; see {doc}",
                    f"skills/alpha/scripts/tool.py:1 filters gh output with --jq; parse its JSON in Python instead; "
                    f"see {doc}",
                    f"skills/alpha/scripts/tool.py:2 filters gh output with -q; parse its JSON in Python instead; "
                    f"see {doc}",
                    f"skills/alpha/scripts/tool.py:3 filters gh output with --template; parse its JSON in Python "
                    f"instead; see {doc}",
                    f"skills/alpha/scripts/tool.sh:1 filters gh output with -q; parse its JSON in Python instead; "
                    f"see {doc}",
                    f"skills/alpha/scripts/tool.sh:2 filters gh output with --jq; parse its JSON in Python instead; "
                    f"see {doc}",
                ],
                gh_filter_problems(root),
            )

    def test_skill_command_policy_detects_undeclared_unused_and_nonstandard_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            for skill, metadata in {
                "alpha": {"tools": ["copilot"]},
                "beta": {},
                "gamma": {"tools": ["gh"]},
            }.items():
                (root / "deploy-meta" / f"{skill}.json").write_text(json.dumps(metadata), encoding="utf-8")
                (root / "skills" / skill / "scripts").mkdir(parents=True)
            (root / "skills" / "alpha" / "SKILL.md").write_text("Prose that mentions `gh` only.\n", encoding="utf-8")
            (root / "skills" / "alpha" / "scripts" / "run.py").write_text(
                'run(["gh", "pr", "list"])\n', encoding="utf-8"
            )
            (root / "skills" / "alpha" / "scripts" / "test_run.py").write_text('which("copilot")\n', encoding="utf-8")
            (root / "skills" / "beta" / "SKILL.md").write_text(
                "```bash\ngit status && dotnet-format --version\n```\n", encoding="utf-8"
            )
            (root / "skills" / "gamma" / "SKILL.md").write_text("Gamma\n", encoding="utf-8")
            (root / "skills" / "gamma" / "scripts" / "run.sh").write_text(
                "  gh pr view | jq .title\nrg -n TODO .\n", encoding="utf-8"
            )
            (root / "skills" / "gamma" / "scripts" / "fetch.py").write_text(
                'subprocess.run(["yq", "."])\nshutil.which("curl")\n', encoding="utf-8"
            )
            doc = '"Commands skills may run" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    "skill alpha runs gh without declaring it in tools",
                    "skill alpha declares tool copilot but never runs it",
                    "skill beta runs dotnet-format without declaring it in tools",
                    f"skill gamma runs jq, which a standard install lacks; see {doc}",
                    f"skill gamma runs rg, which a standard install lacks; see {doc}",
                    f"skill gamma runs yq, which a standard install lacks; see {doc}",
                ],
                skill_command_problems(root, {"copilot", "dotnet-format", "gh"}, {"curl", "git"}),
            )

    def test_skill_command_policy_reads_python_commands_through_imports_names_and_skill_core(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "deploy-meta/alpha.json": "{}",
                "skills/alpha/SKILL.md": "# alpha\n",
                # A runner reached through an alias or a from-import, or a keyword argument.
                "skills/alpha/scripts/aliased.py": 'import subprocess as sp\n\nsp.run(["yq", "."])\n',
                "skills/alpha/scripts/imported.py": "from subprocess import check_output\n\n"
                'check_output(["rg", "x"])\n',
                "skills/alpha/scripts/keyword.py": 'import subprocess\n\nsubprocess.run(args=["perl", "-e", "1"])\n',
                "skills/alpha/scripts/shell.py": 'import os\n\nos.system("node --version")\n',
                # skill-core's bounded runner, given a command held in a name or built from one.
                "skills/alpha/scripts/bounded.py": 'from bounded_process import run_bounded\n\nCOMMAND = ["jq", "."]\n'
                "run_bounded(COMMAND, 5)\n",
                "skills/alpha/scripts/built.py": 'import bounded_process\n\nBASE = ["dotnet-format"]\n'
                'bounded_process.run_bounded([*BASE, "--version"], timeout=5)\n',
                # skill-core's clients run their command, and a program looked up is one the script runs.
                "skills/alpha/scripts/client.py": "from github_client import GitHubClient\n\n"
                'GitHubClient().run(["api"])\n',
                "skills/alpha/scripts/git.py": 'import git_client\n\ngit_client.GitClient().run(["status"])\n',
                "skills/alpha/scripts/lookup.py": 'import shutil as sh\n\nPROGRAM = sh.which("copilot")\n',
                # Neither a string that reads like a call nor a list of other words is a command.
                "skills/alpha/scripts/quiet.py": 'TEXT = "subprocess.run([\\"zsh\\"])"\nLABELS = ["zip", "unzip"]\n'
                'print(["python", "-B"])\n',
            }
            write_fixture_tree(root, files)
            doc = '"Commands skills may run" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    "skill alpha runs copilot without declaring it in tools",
                    "skill alpha runs dotnet-format without declaring it in tools",
                    "skill alpha runs gh without declaring it in tools",
                    *(
                        f"skill alpha runs {name}, which a standard install lacks; see {doc}"
                        for name in ("jq", "node", "perl", "rg", "yq")
                    ),
                ],
                skill_command_problems(root, {"copilot", "dotnet-format", "gh"}, {"git", "python"}),
            )

    def test_skill_command_policy_reads_powershell_scripts_and_fences_through_powershell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "deploy-meta/beta.json": json.dumps({"tools": ["gh"]}),
                "skills/beta/SKILL.md": "# beta\n\n```powershell\njq . data.json\n```\n\n"
                "```pwsh\ncopilot --version\n```\n",
                # A function the script defines, a cmdlet or alias PowerShell ships, a path, and a command held in a
                # variable are not programs it names; a cmdlet of a module PowerShell does not ship is one.
                "skills/beta/scripts/run.ps1": "function Invoke-Helper { param($Value) $Value }\n"
                "Get-ChildItem | ForEach-Object { $_.Name }\n"
                "git status\n"
                "& 'gh' api user\n"
                "dotnet-format.exe --version\n"
                "Invoke-Helper 1\n"
                "ls\n"
                ".\\local.ps1\n"
                "& $env:TOOL\n"
                "nonesuch --flag\n"
                "Invoke-ScriptAnalyzer -Path run.ps1\n",
            }
            write_fixture_tree(root, files)
            doc = '"Commands skills may run" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    "skill beta runs copilot without declaring it in tools",
                    "skill beta runs dotnet-format without declaring it in tools",
                    f"skill beta runs Invoke-ScriptAnalyzer, which a standard install lacks; see {doc}",
                    f"skill beta runs jq, which a standard install lacks; see {doc}",
                    f"skill beta runs nonesuch, which a standard install lacks; see {doc}",
                ],
                skill_command_problems(root, {"copilot", "dotnet-format", "gh"}, {"git", "python"}),
            )

    def test_skill_command_policy_reaches_a_dependency_by_its_imports_not_by_a_string(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "deploy-meta/core.json": json.dumps({"tools": ["gh"], "selectable": False}),
                "deploy-meta/aliased.json": json.dumps({"skill_deps": ["core"]}),
                "deploy-meta/quoted.json": json.dumps({"skill_deps": ["core"]}),
                # A dependency in a category is reached as well.
                "skills/shared/core/SKILL.md": "# core\n",
                "skills/shared/core/scripts/github.py": 'run(["gh", "api"])\n',
                "skills/aliased/SKILL.md": "# aliased\n",
                "skills/aliased/scripts/run.py": "import json, github as api\n",
                # Text that reads like an import is not one.
                "skills/quoted/SKILL.md": "# quoted\n",
                "skills/quoted/scripts/run.py": 'HELP = """\nimport github\nfrom github import Client\n"""\n',
            }
            write_fixture_tree(root, files)
            self.assertEqual(
                ["skill aliased runs gh through skills/shared/core/scripts/github.py without declaring it in tools"],
                skill_command_problems(root, {"gh"}, {"git", "python"}),
            )

    def test_skill_command_policy_counts_the_dependency_scripts_a_skill_reaches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            for skill, metadata in {
                "core": {"tools": ["copilot", "gh"], "selectable": False},
                "named": {"tools": ["copilot", "gh"], "skill_deps": ["core"]},
                "imports": {"optional_tools": ["gh"], "skill_deps": ["core"]},
                "unused": {"optional_tools": ["dotnet-format"], "skill_deps": ["core"]},
                "inherits": {"skill_deps": ["core"]},
                "silent": {"skill_deps": ["core"]},
            }.items():
                (root / "deploy-meta" / f"{skill}.json").write_text(json.dumps(metadata), encoding="utf-8")
                (root / "skills" / skill / "scripts").mkdir(parents=True)
                (root / "skills" / skill / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
            core = root / "skills" / "core" / "scripts"
            (core / "pipeline.py").write_text("from github import Client\nimport store\n", encoding="utf-8")
            (core / "github.py").write_text('run(["gh", "api"])\n', encoding="utf-8")
            (core / "store.py").write_text("import json\n", encoding="utf-8")
            (core / "hosts.py").write_text('shutil.which("copilot")\n', encoding="utf-8")
            (core / "test_hosts.py").write_text("import pipeline\n", encoding="utf-8")
            # A skill reaches pipeline.py by naming its path, and github.py through pipeline.py's import, but never
            # hosts.py, so copilot is declared without being run.
            (root / "skills" / "named" / "SKILL.md").write_text(
                '```bash\npython -B "${CLAUDE_SKILL_DIR}/../core/scripts/pipeline.py" run\n```\n', encoding="utf-8"
            )
            (root / "skills" / "imports" / "scripts" / "run.py").write_text(
                "sys.path.insert(0, str(CORE))\nfrom store import load\nimport github\n", encoding="utf-8"
            )
            (root / "skills" / "unused" / "scripts" / "run.py").write_text("import store\n", encoding="utf-8")
            (root / "skills" / "inherits" / "scripts" / "run.py").write_text("import store\n", encoding="utf-8")
            # silent reaches github.py, so gh is its tool too: a skill that reaches the shared client declares gh,
            # and one that reaches only store.py, like inherits, declares nothing.
            (root / "skills" / "silent" / "scripts" / "run.py").write_text("import github\n", encoding="utf-8")
            self.assertEqual(
                [
                    "skill named declares tool copilot but never runs it",
                    "skill silent runs gh through skills/core/scripts/github.py without declaring it in tools",
                    "skill unused declares tool dotnet-format but never runs it",
                ],
                skill_command_problems(root, {"copilot", "dotnet-format", "gh"}, {"git", "python"}),
            )

    def test_skill_command_policy_follows_dependencies_of_dependencies(self) -> None:
        # code-review-core depends on skill-core, so a skill that reaches code-review-core reaches skill-core too.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            for skill, metadata in {
                "base": {"tools": ["gh"], "selectable": False},
                "middle": {"tools": ["gh"], "skill_deps": ["base"], "selectable": False},
                "top": {"tools": ["gh"], "skill_deps": ["middle"]},
                "idle": {"tools": ["gh"], "skill_deps": ["middle"]},
            }.items():
                (root / "deploy-meta" / f"{skill}.json").write_text(json.dumps(metadata), encoding="utf-8")
                (root / "skills" / skill / "scripts").mkdir(parents=True)
                (root / "skills" / skill / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
            (root / "skills" / "base" / "scripts" / "client.py").write_text('run(["gh", "api"])\n', encoding="utf-8")
            middle = root / "skills" / "middle" / "scripts"
            (middle / "pipeline.py").write_text("import client\n", encoding="utf-8")
            (middle / "store.py").write_text("import json\n", encoding="utf-8")
            # top reaches base's client.py through middle's pipeline.py; idle reaches only middle's store.py.
            (root / "skills" / "top" / "scripts" / "run.py").write_text("import pipeline\n", encoding="utf-8")
            (root / "skills" / "idle" / "scripts" / "run.py").write_text("import store\n", encoding="utf-8")
            self.assertEqual(
                ["skill idle declares tool gh but never runs it"],
                skill_command_problems(root, {"gh"}, {"git", "python"}),
            )

    def test_console_setup_policy_detects_a_missing_late_unresolved_or_copied_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            main = 'if __name__ == "__main__":\n'
            imported = "from console import use_utf8_output\n\n"
            files = {
                "skills/alpha/scripts/good.py": f"{imported}{main}    use_utf8_output()\n    main()\n",
                "skills/alpha/scripts/aliased.py": f"import console as c\n\n{main}    c.use_utf8_output()\n",
                "skills/alpha/scripts/late.py": f"{imported}{main}    parse()\n    use_utf8_output()\n",
                "skills/alpha/scripts/missing.py": f"{main}    raise SystemExit(main())\n",
                # A function of the same name that is not skill-core's is a second setup.
                "skills/alpha/scripts/local.py": f"def use_utf8_output():\n    pass\n\n\n{main}    use_utf8_output()\n",
                "skills/alpha/scripts/copied.py": 'def main():\n    sys.stdout.reconfigure(encoding="utf-8")\n\n\n'
                f"{imported}{main}    use_utf8_output()\n",
                "skills/alpha/scripts/library.py": "def helper():\n    return 1\n",
                # Suites are not entry points.
                "skills/alpha/scripts/test_good.py": f"{main}    unittest.main()\n",
                "skills/skill-core/scripts/console.py": 'sys.stdout.reconfigure(encoding="utf-8")\n',
                # deploy.py, the deployer, and tools/ are held as skill scripts are.
                "deploy.py": f"{imported}{main}    raise SystemExit(main())\n",
                "deployer/platform_support.py": "def setup():\n    reconfigure = sys.stdout.reconfigure\n"
                '    reconfigure(encoding="utf-8")\n',
                "deployer/cli.py": "def main():\n    return 0\n",
                "tools/good_tool.py": f"{imported}{main}    use_utf8_output(errors='backslashreplace')\n    main()\n",
                "tools/late_tool.py": f"{imported}{main}    options = parse()\n    use_utf8_output()\n",
                "tools/test_tool.py": f"{main}    unittest.main()\n",
            }
            write_fixture_tree(root, files)
            mark_skills(root)
            doc = '"Script results" in docs/adding-a-skill.md'
            late = f"does not call skill-core's use_utf8_output() first in its __main__ block; see {doc}"
            copied = f"reconfigures a stream itself; call use_utf8_output() from skill-core instead; see {doc}"
            self.assertEqual(
                [
                    f"deploy.py:3 {late}",
                    f"deployer/platform_support.py:3 {copied}",
                    f"skills/alpha/scripts/copied.py:2 {copied}",
                    f"skills/alpha/scripts/late.py:3 {late}",
                    f"skills/alpha/scripts/local.py:5 {late}",
                    f"skills/alpha/scripts/missing.py:1 {late}",
                    f"tools/late_tool.py:3 {late}",
                ],
                console_setup_problems(root),
            )

    def test_console_setup_policy_detects_a_stream_replaced_wrapped_or_reconfigured_through_an_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "deployer/replaced.py": "import io\nimport sys\n\n\ndef setup():\n"
                '    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")\n',  # 6
                "deployer/renamed.py": "import codecs\nimport sys as system\n\n"
                'system.stderr = codecs.getwriter("utf-8")(system.stderr.buffer)\n',  # 4
                "deployer/set.py": 'import sys\n\nsetattr(sys, "stdout", open(1, "w", encoding="utf-8"))\n',  # 3
                "tools/wrapped.py": "import io\nimport sys\n\n"
                'OUT = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")\n',  # 4
                "tools/bound.py": 'import sys\n\nfix = sys.stderr.reconfigure\nfix(encoding="utf-8")\n',  # 4
                "tools/fetched.py": 'import sys\n\ngetattr(sys.stdout, "reconfigure")(encoding="utf-8")\n'  # 3
                "sys.stdout.reconfigure(**OPTIONS)\n",  # 4: the options may hold an encoding
                # Wrapping a file that is not a standard stream, writing to a stream, or naming one changes no stream.
                "tools/plain.py": "import io\nimport sys\n\n\ndef read(raw):\n"
                '    text = io.TextIOWrapper(raw, encoding="utf-8")\n'
                "    out = sys.stdout\n"
                "    out.write(text.read())\n"
                "    sys.stderr.reconfigure(line_buffering=True)\n",
            }
            write_fixture_tree(root, files)
            mark_skills(root)
            doc = '"Script results" in docs/adding-a-skill.md'
            copied = f"reconfigures a stream itself; call use_utf8_output() from skill-core instead; see {doc}"
            self.assertEqual(
                [
                    f"deployer/renamed.py:4 {copied}",
                    f"deployer/replaced.py:6 {copied}",
                    f"deployer/set.py:3 {copied}",
                    f"tools/bound.py:4 {copied}",
                    f"tools/fetched.py:3 {copied}",
                    f"tools/fetched.py:4 {copied}",
                    f"tools/wrapped.py:4 {copied}",
                ],
                console_setup_problems(root),
            )


if __name__ == "__main__":
    unittest.main()
