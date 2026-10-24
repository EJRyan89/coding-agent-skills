"""Fixture tests for tests/validation/workflows.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from workflows import (
    DEFENDER_LINES,
    IMAGE_MODULE_LINES,
    IMAGE_MODULE_STEP,
    PINNED_TOOL_LINES,
    WorkflowSyntaxError,
    changelog_job_problems,
    command_line_job_problems,
    configure_answer_problems,
    copilot_auto_update_problems,
    defender_problems,
    deployable_linux_problems,
    image_module_problems,
    leg_problems,
    linux_job_problems,
    linux_verify_log_problems,
    mutation_job_problems,
    mypy_cache_problems,
    pinned_tool_problems,
    read_workflow,
    required_check_problems,
    tool_cache_problems,
    uninstall_check_problems,
    unknown_workflow_problems,
    verify_log_problems,
    workflow_guard_problems,
)


class WorkflowsFixtures(unittest.TestCase):
    def test_workflow_guard_reports_each_drift(self) -> None:
        clean = (
            "name: Example\n\non:\n  pull_request:\n  # A comment is not a trigger.\n  push:\n    branches: [main]\n\n"
            "permissions:\n  contents: read\n\njobs:\n  run:\n    runs-on: windows-latest\n    steps:\n"
            f"      - uses: actions/checkout@{'a' * 40} # v7.0.1\n"
        )
        expected = ["pull_request", "push"]
        self.assertEqual([], workflow_guard_problems(clean, expected, {"actions/checkout"}))
        for drifted, problem in (
            (clean.replace("  push:\n", "  pull_request_target:\n  push:\n"), "triggers"),
            (clean.replace("  contents: read\n", "  contents: write\n"), "permissions"),
            (clean.replace("  contents: read\n", "  contents: read\n  pull-requests: write\n"), "permissions"),
            (clean.replace("    steps:\n", "    permissions: write-all\n    steps:\n"), "permissions"),
            (clean + "        env:\n          TOKEN: ${{ secrets.TOKEN }}\n", "secret"),
            (clean + "        env:\n          TOKEN: ${{ github.token }}\n          OTHER: $GITHUB_TOKEN\n", "token"),
            (clean.replace(f"@{'a' * 40} # v7.0.1", "@v7"), "not pinned"),
            (clean.replace(" # v7.0.1", ""), "not pinned"),
            (clean + f"      - uses: actions/cache@{'b' * 40} # v5.0.0\n", "actions"),
            # What the line-based policy missed: a job's own permissions, other spellings of a secret or the token,
            # and an expression in a run block.
            (clean.replace("    steps:\n", "    permissions:\n      contents: write\n    steps:\n"), "permissions"),
            (clean.replace("    steps:\n", "    permissions:\n      id-token: write\n    steps:\n"), "permissions"),
            (clean + "        env:\n          TOKEN: ${{ secrets['TOKEN'] }}\n", "secret"),
            (clean + "        with:\n          token: ${{ github['token'] }}\n", "token"),
            (clean + "        with:\n          token: ${{github.token}}\n", "token"),
            (clean + "      - run: |\n          echo '${{ secrets.TOKEN }}'\n", "secret"),
            (clean + "      - run: |\n          echo '${{ github.event.pull_request.body }}'\n", "event data"),
            (clean + "        env:\n          TITLE: ${{ github['event']['pull_request']['title'] }}\n", "event data"),
            (clean + '      - run: echo "${{ github.head_ref }}"\n', "event data"),
            (
                clean.replace(
                    "  run:\n", "  call:\n    uses: o/r/.github/workflows/x.yml@main\n    secrets: inherit\n  run:\n"
                ),
                "secrets",
            ),
            (
                clean.replace(
                    "on:\n  pull_request:\n  # A comment is not a trigger.\n  push:\n    branches: [main]\n",
                    "on: [pull_request_target]\n",
                ),
                "triggers",
            ),
            (clean + "      - uses: 'actions/checkout@v7'\n", "pin can be read"),
            (clean.replace("jobs:\n", "jobs: &shared\n"), "outside the YAML"),
            (clean + "        env:\n          TOKEN: ${{ SECRETS.TOKEN }}\n", "secret"),
            # The github context reached other than by a dotted read of an allowed field, in a run step or anywhere.
            *(
                (clean + f"      - run: echo '${{{{ {expression} }}}}'\n", "github context")
                for expression in (
                    "toJSON(github)",
                    "github",
                    "github[format('{0}','event')]",
                    "github['event_name']",
                    "fromJSON(toJSON(github)).event",
                    "GITHUB.EVENT.PULL_REQUEST.TITLE",
                    "github.event.pull_request.body",
                    "github.event.pull_request.head.ref",
                    "github.event.comment.body",
                    "github.repository",
                    "github.ref.foo",
                    "github.ref['x']",
                    "github . token",
                    "format('{0}', github)",
                    "github.*",
                )
            ),
            (clean + "        with:\n          data: ${{ toJSON(github) }}\n", "github context"),
            # An expression the policy cannot split into tokens fails rather than passes unread.
            *(
                (clean + f'      - run: echo "${{{{ {expression} }}}}"\n', "cannot read")
                for expression in ("format('{0}", 'github["event"]', "github.ref - 1", "a ? b")
            ),
        ):
            with self.subTest(problem=problem, drifted=drifted):
                problems = workflow_guard_problems(drifted, expected, {"actions/checkout"})
                self.assertTrue(problems)
                self.assertTrue(any(problem in found for found in problems), problems)

    def test_workflow_guard_pins_dispatch_and_the_weekly_cron(self) -> None:
        clean = (
            "on:\n  workflow_dispatch:\n    inputs:\n      version:\n        type: string\n"
            "  schedule:\n    - cron: '23 6 * * 2'\n\npermissions:\n  contents: read\n\njobs:\n  run:\n"
            "    steps:\n      - run: echo done\n"
        )
        expected = ["workflow_dispatch", "schedule"]
        self.assertEqual([], workflow_guard_problems(clean, expected, set(), ("23 6 * * 2",)))
        for drifted, problem in (
            (clean.replace("  schedule:\n", "  push:\n  schedule:\n"), "triggers"),
            (clean.replace("  schedule:\n", "  pull_request:\n  schedule:\n"), "triggers"),
            (clean.replace("  schedule:\n    - cron: '23 6 * * 2'\n", ""), "triggers"),
            (clean.replace("* * 2'", "* * 1'"), "crons"),
            (clean.replace("* * 2'\n", "* * 2'\n    - cron: '23 6 * * 5'\n"), "crons"),
        ):
            with self.subTest(problem=problem, drifted=drifted):
                problems = workflow_guard_problems(drifted, expected, set(), ("23 6 * * 2",))
                self.assertTrue(any(problem in found for found in problems), problems)
        # A schedule the caller did not expect is drift too.
        self.assertTrue(
            any("crons" in found for found in workflow_guard_problems(clean, expected, set())),
        )

    def test_a_read_only_job_and_text_that_only_mentions_secrets_pass(self) -> None:
        workflow = (
            "on:\n  workflow_dispatch:\n\npermissions:\n  contents: read\n\njobs:\n  run:\n"
            "    permissions:\n      contents: read\n      checks: none\n    steps:\n"
            "      - run: |\n          # This step needs no secret and no token.\n          echo ${{ github.ref }}\n"
            "  other:\n    permissions: read-all\n    steps:\n      - run: echo done\n"
        )
        self.assertEqual([], workflow_guard_problems(workflow, ["workflow_dispatch"], set()))

    def test_only_the_workflows_the_policy_knows_may_sit_in_the_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            known = ("deployable.yml", "validate.yml")
            for name in known:
                (directory / name).write_text("on: push\n", encoding="utf-8")
            self.assertEqual([], unknown_workflow_problems(directory, known))
            for extra in ("extra.yml", "other.yaml", "README.md", "nested"):
                with self.subTest(extra=extra):
                    path = directory / extra
                    if extra == "nested":
                        path.mkdir()
                    else:
                        path.write_text("on: [pull_request_target]\n", encoding="utf-8")
                    problems = unknown_workflow_problems(directory, known)
                    self.assertEqual(1, len(problems), problems)
                    self.assertIn(f"{extra} is not a workflow the policy knows", problems[0])
                    if path.is_dir():
                        path.rmdir()
                    else:
                        path.unlink()
            (directory / "validate.yml").unlink()
            self.assertEqual([".github/workflows/validate.yml is missing"], unknown_workflow_problems(directory, known))
        self.assertEqual(
            [".github/workflows/validate.yml is missing"], unknown_workflow_problems(directory, ["validate.yml"])
        )


@pytest.mark.parametrize(
    "expression",
    [
        "github.event_name == 'schedule' && fromJSON('[\"3.11\", \"3.x\"]') || fromJSON('[\"3.11\"]')",
        "github.ref != 'refs/heads/main'",
        "GitHub.Workflow",
        "github.workflow",
        "!cancelled()",
        "matrix.runtimes == 'latest' && 'latest' || inputs.claude-version || '2.1.289'",
        "steps.shellcheck.outputs.cache-hit",
        "needs.suite.result",
        "format('{0} it''s github', matrix.shard)",
        "inputs.github",
    ],
)
def test_the_allowed_github_fields_and_other_contexts_pass(expression: str) -> None:
    clean = "on:\n  push:\n\npermissions:\n  contents: read\n\njobs:\n  run:\n    steps:\n"
    workflow = clean + f"      - run: echo '${{{{ {expression} }}}}'\n"
    assert workflow_guard_problems(workflow, ["push"], set()) == []


LEGS = (
    "env:\n  TOOL_VERSION: '1.2.3'\n\njobs:\n  suite:\n"
    "    name: Windows shard ${{ matrix.shard }}, Python ${{ matrix.python-version }}\n"
    "    runs-on: windows-latest\n    strategy:\n      matrix:\n"
    "        shard: ['1/2', '2/2']\n    steps:\n"
    "      - uses: actions/setup-python@SHA # v1\n        with:\n          cache: pip\n"
    "          cache-dependency-path: requirements-dev.txt\n"
    "      - uses: actions/cache@SHA # v1\n        with:\n          key: tool-${{ env.TOOL_VERSION }}\n"
    "      - run: install tool --version $env:TOOL_VERSION --quiet\n"
    "      - name: Validate\n        env:\n          SHARD: ${{ matrix.shard }}\n"
    "        run: python tests/run_validation.py --shard $env:SHARD\n"
    "  validate:\n    needs: suite\n"
)


CACHES = {"tool-${{ env.TOOL_VERSION }}": "install tool --version $env:TOOL_VERSION "}


class LegFixtures(unittest.TestCase):
    def test_each_leg_is_named_as_a_windows_leg_and_runs_its_own_shard(self) -> None:
        self.assertEqual([], leg_problems(LEGS, 2))

    def test_each_tool_cache_is_keyed_on_a_workflow_pin_its_install_reads(self) -> None:
        self.assertEqual([], tool_cache_problems(LEGS, CACHES))


@pytest.mark.parametrize(
    ("drifted", "problem"),
    [
        pytest.param(LEGS, "expected ['1/3', '2/3', '3/3']", id="fewer legs than shards"),
        pytest.param(LEGS.replace("['1/2', '2/2']", "['1/2', '1/2']"), "shards", id="a shard twice"),
        pytest.param(
            LEGS.replace("runs-on: windows-latest", "runs-on: ubuntu-latest"), "not windows-latest", id="not on Windows"
        ),
        pytest.param(LEGS.replace(" --shard $env:SHARD", ""), "does not run its leg's --shard", id="no --shard"),
        pytest.param(
            LEGS.replace("SHARD: ${{ matrix.shard }}", "SHARD: 1/2"),
            "does not run its leg's --shard",
            id="a literal shard",
        ),
        pytest.param(LEGS.replace("tests/run_validation.py", "tests/other.py"), "no step runs", id="another runner"),
        # The name the seconds recorder reads, which says Windows beside the Linux job.
        pytest.param(
            LEGS.replace("Windows shard ${{ matrix.shard }}, ", "Shard ${{ matrix.shard }} on "),
            "are named",
            id="named without Windows",
        ),
        pytest.param(
            LEGS.replace("    name: Windows shard", "    name: Windows Shard"), "are named", id="named in other case"
        ),
    ],
)
def test_a_leg_misnamed_or_off_its_own_shard_fails(drifted: str, problem: str) -> None:
    count = 3 if problem.startswith("expected") else 2
    problems = leg_problems(drifted, count)
    assert any(problem in found for found in problems), problems


@pytest.mark.parametrize(
    ("drifted", "problem"),
    [
        pytest.param(
            LEGS.replace("key: tool-${{ env.TOOL_VERSION }}", "key: tool-1.2.3"),
            "the cache keys are",
            id="a literal key",
        ),
        pytest.param(
            LEGS.replace("env:\n  TOOL_VERSION: '1.2.3'\n", "env:\n  OTHER: '1'\n"), "does not pin", id="no pin"
        ),
        pytest.param(
            LEGS.replace("--version $env:TOOL_VERSION", "--version 1.2.3"), "no step runs", id="a literal version"
        ),
        pytest.param(LEGS.replace("          cache: pip\n", ""), "setup-python does not cache pip", id="no pip cache"),
        pytest.param(
            LEGS.replace("requirements-dev.txt", "requirements.txt"),
            "setup-python does not cache pip",
            id="another requirements file",
        ),
    ],
)
def test_a_tool_cache_off_its_workflow_pin_fails(drifted: str, problem: str) -> None:
    problems = tool_cache_problems(drifted, CACHES)
    assert any(problem in found for found in problems), problems


MYPY_SETUP = "jobs:\n  suite:\n    steps:\n      - id: python\n        uses: actions/setup-python@SHA # v1\n"
MYPY_RESTORE = (
    "      - uses: actions/cache@SHA # v1\n        with:\n"
    "          path: ${{ runner.temp }}/mypy-cache\n"
    "          key: mypy-${{ runner.os }}-${{ steps.python.outputs.python-version }}-${{ strategy.job-index }}-"
    "${{ hashFiles('requirements-dev.txt') }}\n"
)
MYPY_VALIDATE = (
    "      - name: Validate\n        env:\n          SHARD: ${{ matrix.shard }}\n"
    "          VALIDATION_MYPY_CACHE: ${{ runner.temp }}/mypy-cache\n"
    "        run: python tests/run_validation.py --shard $env:SHARD\n"
)
MYPY_CACHE = MYPY_SETUP + MYPY_RESTORE + MYPY_VALIDATE


def test_mypy_cache_restored_before_validation_and_named_to_it_passes() -> None:
    assert mypy_cache_problems(MYPY_CACHE, "suite") == []
    # The tool caches' policy leaves mypy's cache, which no pin keys, to this one.
    assert tool_cache_problems(LEGS.replace("tool-${{ env", "mypy-${{ env"), {}) == []


@pytest.mark.parametrize(
    ("drifted", "problem"),
    [
        pytest.param(MYPY_CACHE.replace("-${{ strategy.job-index }}", ""), "restores mypy's cache as", id="no leg"),
        pytest.param(
            MYPY_CACHE.replace("${{ hashFiles('requirements-dev.txt') }}", "1"), "restores mypy's cache as", id="no pin"
        ),
        pytest.param(
            MYPY_CACHE.replace("path: ${{ runner.temp }}/mypy-cache", "path: .mypy_cache"),
            "restores mypy's cache as",
            id="another folder",
        ),
        pytest.param(
            MYPY_CACHE.replace("      - id: python\n", "      - id: setup\n"), "is not the step `python`", id="no id"
        ),
        pytest.param(
            MYPY_CACHE.replace("          VALIDATION_MYPY_CACHE: ${{ runner.temp }}/mypy-cache\n", ""),
            "does not name",
            id="not named to validation",
        ),
        pytest.param(
            MYPY_SETUP + MYPY_VALIDATE + MYPY_RESTORE, "after validation runs", id="restored after validation"
        ),
    ],
)
def test_a_mypy_cache_off_its_key_folder_or_order_fails(drifted: str, problem: str) -> None:
    problems = mypy_cache_problems(drifted, "suite")
    assert any(problem in found for found in problems), problems


LINUX = (
    "env:\n  TOOL_VERSION: '1.2.3'\n\njobs:\n  suite:\n    runs-on: windows-latest\n"
    "  linux:\n    name: Linux shard ${{ matrix.shard }}, Python ${{ matrix.python-version }}\n"
    "    runs-on: ubuntu-latest\n    strategy:\n      fail-fast: false\n      matrix:\n"
    "        python-version: ['3.11']\n        shard: ['1/2', '2/2']\n    steps:\n"
    "      - uses: actions/checkout@SHA # v1\n        with:\n          fetch-depth: 0\n"
    "      - uses: actions/setup-python@SHA # v1\n        with:\n"
    "          python-version: ${{ matrix.python-version }}\n"
    "          cache: pip\n          cache-dependency-path: requirements-dev.txt\n"
    "      - uses: actions/cache@SHA # v1\n        with:\n          key: tool-${{ env.TOOL_VERSION }}\n"
    "      - run: |\n"
    '          echo "$SHELLCHECK_LINUX_SHA256  $archive" | sha256sum --check --strict\n'
    "      - shell: pwsh\n"
    "        run: Install-Module PSScriptAnalyzer -RequiredVersion $env:PSSCRIPTANALYZER_VERSION -Force\n"
    "      - run: |\n          python -m pip install --require-hashes --only-binary :all: -r requirements-dev.txt\n"
    "      - name: Run\n        shell: pwsh\n        env:\n          SHARD: ${{ matrix.shard }}\n        run: |\n"
    "          $arguments = @('-B', 'tests/run_validation.py', '-v', '--shard', $env:SHARD)\n"
    "          if ($env:GITHUB_EVENT_NAME -ne 'pull_request') { $arguments += '--full' }\n"
    "          [System.IO.File]::WriteAllText($bodyFile, [string]$payload.pull_request.body)\n"
    "          $arguments += @('--pr-body', $bodyFile)\n"
    "          [System.IO.File]::WriteAllText($titleFile, [string]$payload.pull_request.title)\n"
    "          $arguments += @('--pr-title', $titleFile)\n"
    "          python @arguments\n"
    "  validate:\n    needs: suite\n"
)


class LinuxJobFixtures(unittest.TestCase):
    def test_the_linux_job_runs_the_whole_run_across_its_legs_named_as_linux(self) -> None:
        self.assertEqual([], linux_job_problems(LINUX, 2))

    def test_the_linux_job_restores_its_tools_from_caches_keyed_on_the_workflow_pins(self) -> None:
        caches = {"tool-${{ env.TOOL_VERSION }}": "sha256sum --check --strict"}
        self.assertEqual([], tool_cache_problems(LINUX, caches, job="linux"))
        # The Windows legs' caches are not the Linux job's.
        problems = tool_cache_problems(LINUX, caches)
        self.assertTrue(any("the cache keys are []" in problem for problem in problems), problems)


@pytest.mark.parametrize(
    ("drifted", "problem"),
    [
        pytest.param(LINUX.replace("  linux:\n", "  other:\n"), "there is no linux job", id="no linux job"),
        pytest.param(
            LINUX.replace("runs-on: ubuntu-latest", "runs-on: ubuntu-22.04"), "not ubuntu-latest", id="a pinned Ubuntu"
        ),
        pytest.param(
            LINUX.replace("    name: Linux shard ", "    name: Ubuntu shard "), "legs are named", id="named for Ubuntu"
        ),
        pytest.param(
            LINUX.replace("    name: Linux shard ${{ matrix.shard }}, Python ${{ matrix.python-version }}\n", ""),
            "legs are named",
            id="unnamed",
        ),
        # The name the seconds recorder reads, with each leg's shard.
        pytest.param(
            LINUX.replace("    name: Linux shard ${{ matrix.shard }}, ", "    name: Linux, "),
            "legs are named",
            id="named without the shard",
        ),
        # The name before the Linux legs were named as the Windows legs are.
        pytest.param(
            LINUX.replace(
                "    name: Linux shard ${{ matrix.shard }}, Python ${{ matrix.python-version }}\n",
                "    name: Validation on Linux, shard ${{ matrix.shard }}\n",
            ),
            "legs are named",
            id="the old name",
        ),
        # A typed version in the name could say another Python than the one set up.
        pytest.param(
            LINUX.replace("Python ${{ matrix.python-version }}\n", "Python 3.11\n"),
            "legs are named",
            id="a typed version in the name",
        ),
        pytest.param(LINUX, "expected ['1/3', '2/3', '3/3']", id="fewer legs than shards"),
        pytest.param(LINUX.replace("['1/2', '2/2']", "['1/2', '1/2']"), "shards", id="a shard twice"),
        pytest.param(LINUX.replace("      fail-fast: false\n", ""), "fail-fast", id="fail-fast left on"),
        pytest.param(
            LINUX.replace("      fail-fast: false\n", "      fail-fast: true\n"), "fail-fast", id="fail-fast set on"
        ),
        pytest.param(
            LINUX.replace("          SHARD: ${{ matrix.shard }}\n", "          SHARD: 1/2\n"),
            "does not run its leg's --shard",
            id="a literal shard",
        ),
        pytest.param(
            LINUX.replace("          fetch-depth: 0\n", "          fetch-depth: 1\n"),
            "the whole history",
            id="a shallow checkout",
        ),
        pytest.param(LINUX.replace("['3.11']", "['3.x']"), "matrix's Python", id="another Python"),
        pytest.param(LINUX.replace("['3.11']", "['3.11', '3.x']"), "matrix's Python", id="a second Python"),
        pytest.param(
            LINUX.replace("        python-version: ['3.11']\n", ""), "matrix's Python", id="no Python in the matrix"
        ),
        # The name reads the matrix, so setup-python must install what it says.
        pytest.param(
            LINUX.replace("python-version: ${{ matrix.python-version }}", "python-version: '3.11'"),
            "sets up Python",
            id="a typed version in setup-python",
        ),
        pytest.param(LINUX.replace("          cache: pip\n", ""), "sets up Python", id="no pip cache"),
        pytest.param(
            LINUX.replace(" | sha256sum --check --strict", ""),
            "sha256sum --check --strict",
            id="an unchecked ShellCheck archive",
        ),
        pytest.param(
            LINUX.replace("-RequiredVersion $env:PSSCRIPTANALYZER_VERSION ", ""),
            "Install-Module PSScriptAnalyzer",
            id="an unpinned analyzer",
        ),
        pytest.param(
            LINUX.replace(
                "      - run: |\n"
                "          python -m pip install --require-hashes --only-binary :all: -r requirements-dev.txt\n",
                "",
            ),
            "requirements-dev",
            id="no development dependencies",
        ),
        pytest.param(
            LINUX.replace(" --require-hashes --only-binary :all: -r", " --only-binary :all: -r"),
            "requirements-dev",
            id="no hash checking",
        ),
        pytest.param(
            LINUX.replace(" --require-hashes --only-binary :all: -r", " --require-hashes -r"),
            "requirements-dev",
            id="source distributions allowed",
        ),
        pytest.param(
            LINUX.replace("      - name: Run\n        shell: pwsh\n", "      - name: Run\n        shell: bash\n"),
            "not pwsh",
            id="run in bash",
        ),
        pytest.param(LINUX.replace(", '--shard', $env:SHARD)", ")"), "--shard", id="the whole run on each leg"),
        pytest.param(LINUX.replace("{ $arguments += '--full' }", "{ }"), "--full", id="never --full"),
        pytest.param(
            LINUX.replace("          $arguments += @('--pr-title', $titleFile)\n", ""), "--pr-title", id="no title"
        ),
        pytest.param(
            LINUX.replace("      - name: Run\n", "      - run: python tests/run_validation.py\n      - name: Run\n"),
            "not one",
            id="two runs",
        ),
    ],
)
def test_a_linux_job_that_runs_less_than_the_whole_run_or_is_misnamed_fails(drifted: str, problem: str) -> None:
    count = 3 if problem.startswith("expected") else 2
    problems = linux_job_problems(drifted, count)
    assert any(problem in found for found in problems), problems


def _pwsh_step(name: str, lines: tuple[str, ...], condition: str = "") -> str:
    step = f"      - name: {name}\n" + (f"        if: {condition}\n" if condition else "")
    return step + "        shell: pwsh\n        run: |\n" + "".join(f"          {line}\n" for line in lines)


VALIDATION_STEP = "      - name: Validate\n        run: python tests/run_validation.py\n"
GALLERY_STEP = (
    "Show the latest release",
    ('Write-Host "latest: $((Find-Module PSScriptAnalyzer -ErrorAction Stop).Version)"',),
    "github.event_name == 'schedule'",
)


def _job(name: str, *steps: str) -> str:
    return f"jobs:\n  {name}:\n    steps:\n" + "".join(steps)


PINNED = _job("suite", _pwsh_step("Verify", PINNED_TOOL_LINES), _pwsh_step(*GALLERY_STEP), VALIDATION_STEP)


class PinnedToolFixtures(unittest.TestCase):
    def test_the_tools_are_verified_at_their_pins_and_the_gallery_queried_on_the_schedule_only(self) -> None:
        self.assertEqual([], pinned_tool_problems(PINNED, "suite"))


@pytest.mark.parametrize(
    ("drifted", "problem"),
    [
        pytest.param(
            _job("suite", VALIDATION_STEP, _pwsh_step("Verify", PINNED_TOOL_LINES)),
            "verifies the pinned tools before validation runs",
            id="verified after validation",
        ),
        pytest.param(
            _job("suite", _pwsh_step("Verify", PINNED_TOOL_LINES[1:]), VALIDATION_STEP),
            "verifies the pinned tools",
            id="ShellCheck's version unread",
        ),
        pytest.param(
            _job("suite", _pwsh_step("Verify", PINNED_TOOL_LINES[:3]), VALIDATION_STEP),
            "verifies the pinned tools",
            id="PSScriptAnalyzer unchecked",
        ),
        pytest.param(
            PINNED.replace("        shell: pwsh\n", "        shell: bash\n", 1),
            "verifies the pinned tools",
            id="not PowerShell",
        ),
        pytest.param(
            PINNED.replace("        if: github.event_name == 'schedule'\n", ""),
            "queries the gallery on every event",
            id="gallery queried on every event",
        ),
        pytest.param(
            PINNED.replace("github.event_name == 'schedule'", "github.event_name != 'schedule'"),
            "queries the gallery on every event",
            id="gallery queried off the schedule",
        ),
        pytest.param(
            _job("linux", _pwsh_step("Verify", PINNED_TOOL_LINES), VALIDATION_STEP),
            "no pwsh step of the suite job",
            id="verified in another job",
        ),
    ],
)
def test_a_tool_off_its_pin_or_a_gallery_query_on_every_event_fails(drifted: str, problem: str) -> None:
    problems = pinned_tool_problems(drifted, "suite")
    assert any(problem in found for found in problems), problems


SCHEDULE = "github.event_name == 'schedule'"
DEFENDER = _job("suite", _pwsh_step("Show", DEFENDER_LINES, SCHEDULE), VALIDATION_STEP)
UNPRINTED = "no pwsh step of the suite job prints Defender's real-time protection state on the schedule"


class DefenderFixtures(unittest.TestCase):
    def test_the_weekly_run_alone_prints_real_time_protection(self) -> None:
        self.assertEqual([], defender_problems(DEFENDER))


@pytest.mark.parametrize(
    ("drifted", "problem"),
    [
        pytest.param(DEFENDER.replace("Get-MpComputerStatus", "Get-MpPreference"), UNPRINTED, id="the state unread"),
        pytest.param(
            DEFENDER.replace('          Write-Host "Defender', '          # "Defender'),
            UNPRINTED,
            id="the state unprinted",
        ),
        pytest.param(
            _job("linux", _pwsh_step("Show", DEFENDER_LINES, SCHEDULE), VALIDATION_STEP), UNPRINTED, id="another job"
        ),
        pytest.param(
            DEFENDER.replace(f"        if: {SCHEDULE}\n", ""),
            "reads Defender's real-time protection on every event",
            id="read on every event",
        ),
        pytest.param(
            DEFENDER.replace(SCHEDULE, "github.event_name != 'schedule'"),
            "reads Defender's real-time protection on every event",
            id="read off the schedule",
        ),
        pytest.param(
            _job(
                "suite",
                _pwsh_step("Show", DEFENDER_LINES, SCHEDULE),
                _pwsh_step("Exclude", ("Add-MpPreference -ExclusionPath $env:RUNNER_TEMP -ErrorAction Stop",)),
                VALIDATION_STEP,
            ),
            "adds a Defender exclusion",
            id="an exclusion added",
        ),
    ],
)
def test_a_defender_reading_on_every_event_or_an_exclusion_fails(drifted: str, problem: str) -> None:
    problems = defender_problems(drifted)
    assert any(problem in found for found in problems), problems


def _image_job(probe: str = "", cache: str = "", install: str = "", first: str = "") -> str:
    """A Linux job that reads whether the image has the pinned analyzer and restores and installs it only if not, with
    `first` as its first step and its validation step last unless `first` is that step."""
    lines = "".join(f"          {line}\n" for line in IMAGE_MODULE_LINES)
    cache_if = cache or f"steps.{IMAGE_MODULE_STEP}.outputs.installed != 'true'"
    install_if = install or (
        f"steps.{IMAGE_MODULE_STEP}.outputs.installed != 'true' && steps.analyzer.outputs.cache-hit != 'true'"
    )
    return _job(
        "linux",
        first,
        f"      - name: Find\n        id: {IMAGE_MODULE_STEP}\n{probe}        shell: pwsh\n        run: |\n{lines}",
        f"      - name: Restore\n        id: analyzer\n        if: {cache_if}\n        uses: actions/cache@SHA # v1\n"
        "        with:\n          key: psscriptanalyzer-${{ runner.os }}-${{ env.PSSCRIPTANALYZER_VERSION }}\n",
        f"      - name: Install\n        if: {install_if}\n        shell: pwsh\n"
        "        run: Install-Module PSScriptAnalyzer -RequiredVersion $env:PSSCRIPTANALYZER_VERSION -Force\n",
        "" if first == VALIDATION_STEP else VALIDATION_STEP,
    )


class ImageModuleFixtures(unittest.TestCase):
    def test_the_analyzer_is_restored_and_installed_only_when_the_image_lacks_the_pin(self) -> None:
        self.assertEqual([], image_module_problems(_image_job(), "linux"))


@pytest.mark.parametrize(
    ("drifted", "problem"),
    [
        pytest.param(
            _image_job().replace(f"        id: {IMAGE_MODULE_STEP}\n", ""),
            f"steps with id {IMAGE_MODULE_STEP}, not one",
            id="no image step",
        ),
        pytest.param(
            _image_job().replace(
                "Where-Object { [string]$_.Version -eq $env:PSSCRIPTANALYZER_VERSION }", "Select-Object -First 1"
            ),
            "does not record, on every event, whether the image has the pinned PSScriptAnalyzer",
            id="any version counted",
        ),
        pytest.param(
            _image_job(probe=f"        if: {SCHEDULE}\n"),
            "does not record, on every event, whether the image has the pinned PSScriptAnalyzer",
            id="the image read on the schedule only",
        ),
        pytest.param(
            _image_job(cache="always()"),
            "restores PSScriptAnalyzer's cache whether or not the image has the pin",
            id="the cache restored whatever the image",
        ),
        pytest.param(
            _image_job(install="steps.analyzer.outputs.cache-hit != 'true'"),
            "installs PSScriptAnalyzer whether or not the image has the pin",
            id="installed whatever the image",
        ),
        pytest.param(
            _image_job(install=f"steps.{IMAGE_MODULE_STEP}.outputs.installed != 'true'"),
            "installs PSScriptAnalyzer whether or not the image has the pin",
            id="installed over a cache hit",
        ),
        pytest.param(
            _image_job(first=VALIDATION_STEP),
            "after validation",
            id="read after validation",
        ),
    ],
)
def test_an_analyzer_restored_or_installed_whatever_the_image_has_fails(drifted: str, problem: str) -> None:
    problems = image_module_problems(drifted, "linux")
    assert any(problem in found for found in problems), problems


REQUIRED_CHECK = (
    "jobs:\n  suite:\n    runs-on: windows-latest\n  linux:\n    runs-on: ubuntu-latest\n"
    "  validate:\n    name: Validation\n    if: ${{ !cancelled() }}\n    needs: [suite, linux]\n    steps:\n"
    "      - name: Require every leg to pass\n        env:\n          WINDOWS: ${{ needs.suite.result }}\n"
    "          LINUX: ${{ needs.linux.result }}\n        run: |\n"
    '          test "$WINDOWS" = success\n          test "$LINUX" = success\n'
)


class RequiredCheckFixtures(unittest.TestCase):
    def test_the_one_required_check_fails_unless_every_platform_passes(self) -> None:
        self.assertEqual([], required_check_problems(REQUIRED_CHECK, ("suite", "linux")))


@pytest.mark.parametrize(
    ("drifted", "problem"),
    [
        # The shape before the Linux job joined the check: it reported, and a red run could still merge.
        pytest.param(
            REQUIRED_CHECK.replace("    needs: [suite, linux]\n", "    needs: suite\n")
            .replace("          LINUX: ${{ needs.linux.result }}\n", "")
            .replace('          test "$LINUX" = success\n', ""),
            "does not need the linux job",
            id="the suite job alone",
        ),
        pytest.param(
            REQUIRED_CHECK.replace("    needs: [suite, linux]\n", "    needs: linux\n"),
            "the suite job",
            id="the linux job alone",
        ),
        pytest.param(
            REQUIRED_CHECK.replace('          test "$LINUX" = success\n', ""),
            "linux job's result",
            id="the linux result unchecked",
        ),
        pytest.param(
            REQUIRED_CHECK.replace("${{ needs.linux.result }}", "${{ needs.suite.result }}"),
            "linux job's result",
            id="the suite result read twice",
        ),
        pytest.param(
            REQUIRED_CHECK.replace('"$WINDOWS" = success', '"$WINDOWS" != cancelled'),
            "suite job's result",
            id="a failed suite passes",
        ),
        pytest.param(REQUIRED_CHECK.replace("    if: ${{ !cancelled() }}\n", ""), "skip the check", id="no condition"),
        pytest.param(
            REQUIRED_CHECK.replace("    if: ${{ !cancelled() }}\n", "    if: ${{ success() }}\n"),
            "skip the check",
            id="only on success",
        ),
        pytest.param(
            REQUIRED_CHECK.replace("    name: Validation\n", "    name: Validation on Windows\n"),
            "required check",
            id="renamed",
        ),
    ],
)
def test_a_required_check_that_passes_without_every_platform_fails(drifted: str, problem: str) -> None:
    problems = required_check_problems(drifted, ("suite", "linux"))
    assert any(problem in found for found in problems), problems


VERIFY_LOGS = (
    "jobs:\n  deployable:\n    steps:\n"
    "      - name: First discovery\n        run: |\n"
    "          python deploy.py verify 2>&1 | Tee-Object -FilePath (Join-Path $env:LOGS 'verify-first.log')\n"
    "          $deployVerify = $LASTEXITCODE\n"
    "          python tools/deployable_report.py verify --label 'first deploy' "
    "--deploy-verify-exit $deployVerify --deploy-verify-log (Join-Path $env:LOGS 'verify-first.log')\n"
    "      - name: Second discovery\n        run: |\n"
    "          python deploy.py verify 2>&1 | Tee-Object -FilePath (Join-Path $env:LOGS 'verify-second.log')\n"
    "          $deployVerify = $LASTEXITCODE\n"
    "          python tools/deployable_report.py verify --label 'after uninstall and redeploy' "
    "--deploy-verify-exit $deployVerify --deploy-verify-log (Join-Path $env:LOGS 'verify-second.log')\n"
)


class VerifyLogFixtures(unittest.TestCase):
    def test_each_report_reads_the_log_its_own_verify_command_saved(self) -> None:
        self.assertEqual([], verify_log_problems(VERIFY_LOGS))
        self.assertEqual([], verify_log_problems(VERIFY_LOGS.replace("'", '"')))
        self.assertEqual([], verify_log_problems(VERIFY_LOGS.replace("python ", "  python ").replace(" --", "   --")))

    def test_wrong_swapped_or_missing_verify_logs_fail(self) -> None:
        first = "--deploy-verify-log (Join-Path $env:LOGS 'verify-first.log')"
        second = "--deploy-verify-log (Join-Path $env:LOGS 'verify-second.log')"
        for name, drifted in (
            (
                "swapped",
                VERIFY_LOGS.replace(first, "FIRST_ARGUMENT").replace(second, first).replace("FIRST_ARGUMENT", second),
            ),
            ("misspelt", VERIFY_LOGS.replace(first, first.replace("first.log", "frist.log"))),
            ("missing option", VERIFY_LOGS.replace(first, "")),
            ("missing argument", VERIFY_LOGS.replace(first, "--deploy-verify-log")),
            ("wrong directory", VERIFY_LOGS.replace(first, first.replace("$env:LOGS", "$env:OTHER"))),
            ("literal directory", VERIFY_LOGS.replace(first, first.replace("$env:LOGS", "'$env:LOGS'"))),
            ("wrong producer", VERIFY_LOGS.replace("'verify-first.log'", "'other.log'", 1)),
            ("both use wrong log", VERIFY_LOGS.replace("verify-first.log", "other.log")),
            ("both pairs use same log", VERIFY_LOGS.replace("verify-second.log", "verify-first.log")),
            ("missing save", VERIFY_LOGS.replace(" | Tee-Object -FilePath", " # Tee-Object -FilePath", 1)),
            ("missing producer", VERIFY_LOGS.replace("python deploy.py verify", "# python deploy.py verify", 1)),
            (
                "missing consumer",
                VERIFY_LOGS.replace(
                    "python tools/deployable_report.py verify", "# python tools/deployable_report.py verify", 1
                ),
            ),
            ("duplicate option", VERIFY_LOGS.replace(first, f"{first} {first}")),
            ("empty workflow", "jobs:\n  deployable:\n    steps: []\n"),
            ("missing job", "jobs:\n  other:\n    steps: []\n"),
            ("unclosed quote", VERIFY_LOGS.replace("'verify-first.log')", "'verify-first.log)", 1)),
        ):
            with self.subTest(drift=name):
                self.assertTrue(verify_log_problems(drifted), name)

    def test_comments_and_other_steps_cannot_supply_the_log_wiring(self) -> None:
        command = "python tools/deployable_report.py verify"
        drifted = VERIFY_LOGS.replace(command, f"# {command}", 1)
        drifted += (
            "      - run: |\n"
            "          python tools/deployable_report.py verify "
            "--deploy-verify-log (Join-Path $env:LOGS 'verify-first.log')\n"
        )
        self.assertTrue(verify_log_problems(drifted))
        self.assertTrue(verify_log_problems(VERIFY_LOGS.replace(command, f"echo '{command}", 1)))

    def test_a_report_before_its_producer_cannot_read_that_runs_log(self) -> None:
        lines = VERIFY_LOGS.splitlines()
        producer = next(index for index, line in enumerate(lines) if "python deploy.py verify" in line)
        report = next(index for index, line in enumerate(lines) if "python tools/deployable_report.py verify" in line)
        lines[producer], lines[report] = lines[report], lines[producer]
        self.assertTrue(verify_log_problems("\n".join(lines)))


_JOB_SETTINGS = (
    "    timeout-minutes: 45\n"
    "    strategy:\n      fail-fast: false\n      matrix:\n        runtimes: readme\n"
    "    env:\n      CODEX_VERSION: '1.0'\n"
)
DEPLOYABLE_JOBS = (
    "jobs:\n  deployable:\n    runs-on: windows-latest\n" + _JOB_SETTINGS + "    steps:\n"
    "      - name: Check out source\n        uses: actions/checkout@SHA # v1\n"
    "      - name: Make the Codex sandbox setting and record the Python aliases\n        run: Write-Host set\n"
    "      - name: Deploy\n        run: python deploy.py --all\n"
    "  deployable-linux:\n    runs-on: ubuntu-latest\n"
    + _JOB_SETTINGS
    + "    defaults:\n      run:\n        shell: bash\n"
    "    steps:\n"
    "      - name: Check out source\n        uses: actions/checkout@SHA # v1\n"
    "      - name: Deploy\n        run: python3 deploy.py --all\n"
)


class DeployableLinuxFixtures(unittest.TestCase):
    def test_the_linux_job_runs_the_windows_jobs_steps_but_the_windows_only_one(self) -> None:
        self.assertEqual([], deployable_linux_problems(DEPLOYABLE_JOBS))
        linux = DEPLOYABLE_JOBS.index("  deployable-linux:")
        for drifted, problem in (
            (DEPLOYABLE_JOBS.replace("  deployable-linux:\n", "  other:\n"), "there is no deployable-linux job"),
            (DEPLOYABLE_JOBS.replace("runs-on: ubuntu-latest", "runs-on: ubuntu-22.04"), "not ubuntu-latest"),
            (DEPLOYABLE_JOBS.replace("runs-on: windows-latest", "runs-on: windows-2022"), "not windows-latest"),
            (DEPLOYABLE_JOBS.replace("        shell: bash\n", "        shell: sh\n"), "do not run in bash"),
            (DEPLOYABLE_JOBS[:linux] + DEPLOYABLE_JOBS[linux:].replace("'1.0'", "'2.0'"), "env is not"),
            (
                DEPLOYABLE_JOBS[:linux] + DEPLOYABLE_JOBS[linux:].replace("fail-fast: false", "fail-fast: true"),
                "strategy is not",
            ),
            (DEPLOYABLE_JOBS[:linux] + DEPLOYABLE_JOBS[linux:].replace("45", "30"), "timeout-minutes is not"),
            (DEPLOYABLE_JOBS[:linux] + DEPLOYABLE_JOBS[linux:].replace("name: Deploy\n", "name: Ship\n"), "steps are"),
            (
                DEPLOYABLE_JOBS.replace(
                    "        run: python3 deploy.py --all\n",
                    "        run: python3 deploy.py --all\n      - name: Extra\n        run: echo\n",
                ),
                "steps are",
            ),
        ):
            with self.subTest(problem=problem):
                problems = deployable_linux_problems(drifted)
                self.assertTrue(any(problem in found for found in problems), problems)

    def test_each_job_that_does_not_turn_copilots_auto_update_off_fails(self) -> None:
        pinned = DEPLOYABLE_JOBS.replace("    env:\n", "    env:\n      COPILOT_AUTO_UPDATE: 'false'\n")
        self.assertEqual([], copilot_auto_update_problems(pinned))
        linux = pinned.index("  deployable-linux:")
        for drifted, jobs in (
            (DEPLOYABLE_JOBS, ["deployable", "deployable-linux"]),
            (pinned[:linux] + pinned[linux:].replace("'false'", "'true'"), ["deployable-linux"]),
            (pinned.replace("      COPILOT_AUTO_UPDATE: 'false'\n", "", 1), ["deployable"]),
        ):
            with self.subTest(jobs=jobs):
                self.assertEqual(
                    [f"the {job} job's env does not set COPILOT_AUTO_UPDATE: 'false'" for job in jobs],
                    copilot_auto_update_problems(drifted),
                )


CONFIGURE_JOBS = (
    "jobs:\n"
    "  deployable:\n    runs-on: windows-latest\n    steps:\n"
    "      - name: Configure\n        run: |\n"
    "          $repositories = Join-Path $env:USERPROFILE 'GitHub'\n"
    "          $repositories, '', '' | python deploy.py configure 2>&1 | Tee-Object -FilePath configure.log\n"
    "  deployable-linux:\n    runs-on: ubuntu-latest\n    steps:\n"
    "      - name: Configure\n        run: |\n"
    '          repositories="$HOME/GitHub"\n'
    "          printf '%s\\n' \"$repositories\" '' '' | python3 deploy.py configure 2>&1 | tee configure.log\n"
)
CONFIGURE_PROMPTS = ("REPOS_ROOT", "UPDATE_CHANNEL", "EXTRA")
CONFIGURE_DEFAULTS = ("UPDATE_CHANNEL", "EXTRA")


class ConfigureAnswerFixtures(unittest.TestCase):
    def test_each_job_answers_every_prompt_with_an_empty_line_only_for_a_default(self) -> None:
        self.assertEqual([], configure_answer_problems(CONFIGURE_JOBS, CONFIGURE_PROMPTS, CONFIGURE_DEFAULTS))
        for prompts, defaults, problem in (
            (
                (*CONFIGURE_PROMPTS, "NEW"),
                (*CONFIGURE_DEFAULTS, "NEW"),
                "pipes 3 answers, but configure asks for 4: REPOS_ROOT, UPDATE_CHANNEL, EXTRA, NEW",
            ),
            (
                CONFIGURE_PROMPTS[:2],
                CONFIGURE_DEFAULTS,
                "pipes 3 answers, but configure asks for 2: REPOS_ROOT, UPDATE_CHANNEL",
            ),
            (CONFIGURE_PROMPTS, ("UPDATE_CHANNEL",), "answers EXTRA with an empty line, but EXTRA has no default"),
        ):
            with self.subTest(problem=problem):
                self.assertEqual(
                    [f"{job}: the 'Configure' step {problem}" for job in ("deployable", "deployable-linux")],
                    configure_answer_problems(CONFIGURE_JOBS, prompts, defaults),
                )

    def test_a_step_that_pipes_no_answers_in_its_jobs_form_fails(self) -> None:
        linux = CONFIGURE_JOBS.index("  deployable-linux:")
        missing = "deployable-linux: expected one 'Configure' step whose one line pipes answers into configure"
        for drifted, problems in (
            (
                CONFIGURE_JOBS.replace("$repositories, '', ''", "''"),
                [
                    "deployable: the 'Configure' step pipes 1 answers, but configure asks for 3: "
                    "REPOS_ROOT, UPDATE_CHANNEL, EXTRA",
                    "deployable: the first answer, to REPOS_ROOT, must be $repositories",
                ],
            ),
            (CONFIGURE_JOBS[:linux] + CONFIGURE_JOBS[linux:].replace("name: Configure", "name: Setup"), [missing]),
            (CONFIGURE_JOBS[:linux] + CONFIGURE_JOBS[linux:].replace("printf '%s\\n' ", "echo "), [missing]),
        ):
            with self.subTest(problems=problems):
                self.assertEqual(problems, configure_answer_problems(drifted, CONFIGURE_PROMPTS, CONFIGURE_DEFAULTS))


COMMAND_LINE_JOBS = (
    "jobs:\n"
    "  command-lines:\n    runs-on: windows-latest\n    env:\n      COPILOT_AUTO_UPDATE: 'false'\n    steps:\n"
    "      - name: Install\n        run: |\n"
    "          npm install --global '@anthropic-ai/claude-code@latest' "
    "'@openai/codex@latest' '@github/copilot@latest'\n"
    "      - name: Check\n        run: |\n"
    "          python tools/runtime_command_lines.py --summary $env:GITHUB_STEP_SUMMARY\n"
    "  command-lines-linux:\n    runs-on: ubuntu-latest\n    env:\n      COPILOT_AUTO_UPDATE: 'false'\n    steps:\n"
    "      - name: Install\n        run: |\n"
    "          npm install --global '@anthropic-ai/claude-code@latest' "
    "'@openai/codex@latest' '@github/copilot@latest'\n"
    "      - name: Check\n        run: |\n"
    '          python3 tools/runtime_command_lines.py --summary "$GITHUB_STEP_SUMMARY"\n'
)


class CommandLineJobFixtures(unittest.TestCase):
    def test_each_departure_from_the_command_line_check_fails(self) -> None:
        self.assertEqual([], command_line_job_problems(COMMAND_LINE_JOBS))
        linux = COMMAND_LINE_JOBS.index("  command-lines-linux:")
        windows, rest = COMMAND_LINE_JOBS[:linux], COMMAND_LINE_JOBS[linux:]
        must = (
            "the command-lines job must run \"npm install --global '@anthropic-ai/claude-code@latest' "
            "'@openai/codex@latest' '@github/copilot@latest'\" once and then "
            "'python tools/runtime_command_lines.py --summary $env:GITHUB_STEP_SUMMARY' once"
        )
        for drifted, problem in (
            (
                windows.replace("windows-latest", "windows-2022") + rest,
                "the command-lines job runs on windows-2022, not windows-latest",
            ),
            (
                windows + rest.replace("'false'", "'true'"),
                "the command-lines-linux job's env does not set COPILOT_AUTO_UPDATE: 'false'",
            ),
            (windows.replace("@openai/codex@latest", "@openai/codex@0.163.0") + rest, must),
            (windows.replace(" --summary $env:GITHUB_STEP_SUMMARY", "") + rest, must),
            (windows, "the jobs are ['command-lines'], not ['command-lines', 'command-lines-linux']"),
        ):
            with self.subTest(problem=problem):
                self.assertIn(problem, command_line_job_problems(drifted))


CHANGELOG_JOB = (
    "jobs:\n"
    "  changelogs:\n    runs-on: ubuntu-latest\n    steps:\n"
    "      - name: Watch\n        run: |\n"
    '          python3 tools/runtime_changelogs.py --summary "$GITHUB_STEP_SUMMARY"\n'
)


class ChangelogJobFixtures(unittest.TestCase):
    def test_each_departure_from_the_watch_fails(self) -> None:
        self.assertEqual([], changelog_job_problems(CHANGELOG_JOB))
        watch = 'python3 tools/runtime_changelogs.py --summary "$GITHUB_STEP_SUMMARY"'
        must = f"the changelogs job must run {watch!r} once, as the last line of its step"
        run = "        run: |\n"
        for drifted, problem in (
            (CHANGELOG_JOB.replace("ubuntu-latest", "windows-latest"), "the changelogs job runs on windows-latest"),
            (CHANGELOG_JOB.replace(' --summary "$GITHUB_STEP_SUMMARY"', ""), must),
            (CHANGELOG_JOB.replace(watch, watch + "\n          true"), must),
            (CHANGELOG_JOB + f"          {watch}\n", must),
            (CHANGELOG_JOB.replace(run, run + "          npm install --global x\n"), "installs a runtime"),
            (
                CHANGELOG_JOB.replace("    steps:\n", "    continue-on-error: true\n    steps:\n"),
                "goes on after an error",
            ),
            (CHANGELOG_JOB.replace("  changelogs:", "  watch:"), "the jobs are ['watch'], not ['changelogs']"),
        ):
            with self.subTest(problem=problem):
                self.assertTrue(any(problem in found for found in changelog_job_problems(drifted)), drifted)


LINUX_VERIFY_LOGS = (
    "jobs:\n  deployable-linux:\n    steps:\n"
    "      - name: First discovery\n        run: |\n"
    "          deploy_verify=0\n"
    '          python3 deploy.py verify 2>&1 | tee "$LOGS/verify-first.log" || deploy_verify=$?\n'
    "          python3 tools/deployable_report.py verify --label 'first deploy' "
    '--deploy-verify-exit "$deploy_verify" --deploy-verify-log "$LOGS/verify-first.log" --results "$RESULTS"\n'
    "      - name: Second discovery\n        run: |\n"
    "          deploy_verify=0\n"
    '          python3 deploy.py verify 2>&1 | tee "$LOGS/verify-second.log" || deploy_verify=$?\n'
    "          python3 tools/deployable_report.py verify --label 'after uninstall and redeploy' "
    '--deploy-verify-exit "$deploy_verify" --deploy-verify-log "$LOGS/verify-second.log" --results "$RESULTS"\n'
)


class LinuxVerifyLogFixtures(unittest.TestCase):
    def test_each_report_reads_the_log_and_the_status_its_own_verify_left(self) -> None:
        self.assertEqual([], linux_verify_log_problems(LINUX_VERIFY_LOGS))

    def test_wrong_swapped_unquoted_or_missing_wiring_fails(self) -> None:
        first = '--deploy-verify-log "$LOGS/verify-first.log"'
        second = '--deploy-verify-log "$LOGS/verify-second.log"'
        for name, drifted in (
            ("swapped", LINUX_VERIFY_LOGS.replace(first, "FIRST").replace(second, first).replace("FIRST", second)),
            ("misspelt", LINUX_VERIFY_LOGS.replace(first, first.replace("first.log", "frist.log"))),
            ("missing option", LINUX_VERIFY_LOGS.replace(first, "")),
            ("duplicate option", LINUX_VERIFY_LOGS.replace(first, f"{first} {first}")),
            ("unquoted log", LINUX_VERIFY_LOGS.replace(first, first.replace('"', ""))),
            ("unquoted status", LINUX_VERIFY_LOGS.replace('"$deploy_verify"', "$deploy_verify", 1)),
            (
                "literal status",
                LINUX_VERIFY_LOGS.replace('--deploy-verify-exit "$deploy_verify"', "--deploy-verify-exit 0", 1),
            ),
            ("no tee", LINUX_VERIFY_LOGS.replace(' | tee "$LOGS/verify-first.log"', "", 1)),
            ("no status", LINUX_VERIFY_LOGS.replace(" || deploy_verify=$?", "", 1)),
            ("no initial status", LINUX_VERIFY_LOGS.replace("          deploy_verify=0\n", "", 1)),
            ("wrong producer", LINUX_VERIFY_LOGS.replace('tee "$LOGS/verify-first.log"', 'tee "$LOGS/other.log"')),
            (
                "commented producer",
                LINUX_VERIFY_LOGS.replace("          python3 deploy.py", "          # python3 deploy.py", 1),
            ),
            ("unclosed quote", LINUX_VERIFY_LOGS.replace("'first deploy'", "'first deploy", 1)),
            ("missing job", "jobs:\n  other:\n    steps: []\n"),
        ):
            with self.subTest(drift=name):
                self.assertTrue(linux_verify_log_problems(drifted), name)

    def test_a_report_before_its_producer_cannot_read_that_runs_log(self) -> None:
        lines = LINUX_VERIFY_LOGS.splitlines()
        producer = next(index for index, line in enumerate(lines) if "python3 deploy.py verify" in line)
        report = next(index for index, line in enumerate(lines) if "python3 tools/deployable_report.py" in line)
        lines[producer], lines[report] = lines[report], lines[producer]
        self.assertTrue(linux_verify_log_problems("\n".join(lines)))


UNINSTALL_CHECK = (
    "jobs:\n  deployable:\n    steps:\n"
    "      - name: Check the Claude Code file layout after the first deployment\n        run: |\n"
    "          python tools/deployable_report.py layout --label 'first deploy' --results $env:RESULTS\n"
    "          if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
    "      # The README's selection.\n"
    "      - name: Uninstall\n        run: |\n"
    "          python deploy.py --uninstall --dry-run 2>&1 | Tee-Object -FilePath "
    "(Join-Path $env:LOGS 'uninstall-dry-run.log')\n"
    "          if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
    "          python deploy.py --uninstall 2>&1 | Tee-Object -FilePath (Join-Path $env:LOGS 'uninstall.log')\n"
    "          if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
    "      - name: Confirm the profile holds nothing from the suite\n        run: |\n"
    "          # Names what is left.\n"
    "          python tools/deployable_report.py leftovers --results $env:RESULTS\n"
    "          if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
    "  deployable-linux:\n    steps:\n"
    "      - name: Check the Claude Code file layout after the first deployment\n        run: |\n"
    "          python3 tools/deployable_report.py layout --label 'first deploy' --results \"$RESULTS\"\n"
    "      - name: Uninstall\n        run: |\n"
    '          python3 deploy.py --uninstall --dry-run 2>&1 | tee "$LOGS/uninstall-dry-run.log"\n'
    '          python3 deploy.py --uninstall 2>&1 | tee "$LOGS/uninstall.log"\n'
    "      - name: Confirm the profile holds nothing from the suite\n        run: |\n"
    '          python3 tools/deployable_report.py leftovers --results "$RESULTS"\n'
)


class UninstallCheckFixtures(unittest.TestCase):
    def test_both_jobs_check_the_layout_before_the_uninstall_and_report_what_is_left_after(self) -> None:
        self.assertEqual([], uninstall_check_problems(UNINSTALL_CHECK))

    def test_a_check_in_the_steps_own_shell_or_out_of_place_fails(self) -> None:
        linux = UNINSTALL_CHECK.index("  deployable-linux:")
        windows_part, linux_part = UNINSTALL_CHECK[:linux], UNINSTALL_CHECK[linux:]
        directories_only = "          find \"$HOME/.claude/skills\" -mindepth 1 -maxdepth 1 -type d ! -name '.*'\n"
        for name, drifted, problem in (
            (
                "a shell listing of directories",
                windows_part
                + linux_part.replace(
                    '          python3 tools/deployable_report.py leftovers --results "$RESULTS"\n',
                    directories_only,
                ),
                "deployable-linux: 'Confirm the profile holds nothing from the suite' must run exactly",
            ),
            (
                "no exit status check",
                UNINSTALL_CHECK.replace("          if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n", "", 1),
                "deployable: 'Check the Claude Code file layout after the first deployment' must run exactly",
            ),
            (
                "a saved list read again",
                windows_part
                + linux_part.replace("leftovers --results", 'leftovers --owned "$LOGS/owned.json" --results'),
                "deployable-linux: 'Confirm the profile holds nothing from the suite' must run exactly",
            ),
            (
                "a step between the layout check and the uninstall",
                UNINSTALL_CHECK.replace(
                    "      # The README's selection.\n",
                    "      - name: Record what the deployment owns\n        run: |\n"
                    "          python tools/deployable_report.py owned\n",
                    1,
                ),
                "deployable: the step before 'Uninstall' is not 'Check the Claude Code file layout after the first "
                "deployment'",
            ),
            (
                "the check missing",
                windows_part.split("      - name: Confirm")[0] + linux_part,
                "deployable: the step after 'Uninstall' is not 'Confirm the profile holds nothing from the suite'",
            ),
            (
                "a failure let through",
                UNINSTALL_CHECK.replace(
                    "      - name: Confirm the profile holds nothing from the suite\n",
                    "      - name: Confirm the profile holds nothing from the suite\n        continue-on-error: true\n",
                    1,
                ),
                "must set only its name and run",
            ),
            (
                "the answer piped into the prompt",
                windows_part
                + linux_part.replace("python3 deploy.py --uninstall 2>&1", "printf 'none\\n' | python3 deploy.py 2>&1"),
                "deployable-linux: 'Uninstall' must run exactly",
            ),
            (
                "the preview left out",
                UNINSTALL_CHECK.replace(
                    "          python deploy.py --uninstall --dry-run 2>&1 | Tee-Object -FilePath "
                    "(Join-Path $env:LOGS 'uninstall-dry-run.log')\n",
                    "",
                    1,
                ),
                "deployable: 'Uninstall' must run exactly",
            ),
            ("no uninstall", windows_part.replace("name: Uninstall", "name: Remove") + linux_part, "expected one"),
            ("no linux job", windows_part, "deployable-linux: expected one 'Uninstall' step, found 0"),
        ):
            with self.subTest(drift=name):
                problems = uninstall_check_problems(drifted)
                self.assertTrue(any(problem in found for found in problems), problems)


class WorkflowReaderFixtures(unittest.TestCase):
    def test_the_subset_reads_as_dictionaries_lists_and_strings(self) -> None:
        text = (
            "# A comment.\n"
            "name: Example  # trailing\n"
            "on:\n"
            "  push:\n"
            "    branches: [main, 'release/*']\n"
            "  schedule:\n"
            "    - cron: '23 6 * * 1'\n"
            "jobs:\n"
            "  run:\n"
            "    if: ${{ !cancelled() }}\n"
            "    steps:\n"
            '      - name: "Say \\"hi\\""\n'
            "        # A comment inside a mapping.\n"
            "        with:\n"
            "          python-version: '3.11'\n"
            "          empty: ''\n"
            "        run: |\n"
            "          echo one  # kept: a run block has no comments\n"
            "\n"
            "          echo 'two'\n"
            "      - plain\n"
            "      -\n"
            "        nested: value\n"
            "    needs: []\n"
            "    env:\n"
        )
        self.assertEqual(
            {
                "name": "Example",
                "on": {"push": {"branches": ["main", "release/*"]}, "schedule": [{"cron": "23 6 * * 1"}]},
                "jobs": {
                    "run": {
                        "if": "${{ !cancelled() }}",
                        "steps": [
                            {
                                "name": 'Say "hi"',
                                "with": {"python-version": "3.11", "empty": ""},
                                "run": "echo one  # kept: a run block has no comments\n\necho 'two'\n",
                            },
                            "plain",
                            {"nested": "value"},
                        ],
                        "needs": [],
                        "env": None,
                    }
                },
            },
            read_workflow(text),
        )


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        pytest.param("a:\n\tb: c\n", "tab", id="a tab"),
        pytest.param("a: 1\na: 2\n", "repeated", id="a repeated key"),
        pytest.param("a: {b: c}\n", "outside the subset", id="a flow mapping"),
        pytest.param("a: &anchor b\n", "outside the subset", id="an anchor"),
        pytest.param("a: *anchor\n", "outside the subset", id="an alias"),
        pytest.param("a: !!str b\n", "outside the subset", id="a tag"),
        pytest.param("a: b: c\n", "outside the subset", id="a mapping in a value"),
        pytest.param("a: [b, [c]]\n", "flow sequence", id="a nested flow sequence"),
        pytest.param("a: 'b\n", "unclosed", id="an unclosed quote"),
        pytest.param("a: 'b' c\n", "text after", id="text after a quote"),
        pytest.param("a:\n  b: c\n d: e\n", "indentation", id="a dedent to no level"),
        pytest.param("a:\n  b: c\n    d: e\n", "indentation", id="an indent under a value"),
        pytest.param("just text\n", "expected `key: value`", id="no key"),
        pytest.param("  a: b\n", "column 0", id="an indented start"),
    ],
)
def test_yaml_outside_the_subset_is_refused(text: str, reason: str) -> None:
    with pytest.raises(WorkflowSyntaxError, match=reason):
        read_workflow(text)


MUTATION = (
    "on:\n  pull_request:\n    types: [opened, synchronize, reopened]\n"
    "jobs:\n  mutation:\n    name: Mutation gate\n    runs-on: ubuntu-latest\n    timeout-minutes: 60\n    steps:\n"
    "      - uses: actions/checkout@abc\n        with:\n          fetch-depth: 0\n"
    "      - uses: actions/setup-python@abc\n        with:\n          python-version: '3.11'\n          cache: pip\n"
    "          cache-dependency-path: requirements-mutation.txt\n"
    "      - name: Install\n        run: |\n"
    "          python -m pip install --require-hashes --only-binary :all: -r requirements-mutation.txt\n"
    "      - name: Gate\n        run: python -B tools/mutation_gate.py\n"
)


def test_the_mutation_job_runs_the_gate_on_linux_outside_the_required_check() -> None:
    assert mutation_job_problems(MUTATION) == []


@pytest.mark.parametrize(
    ("drifted", "problem"),
    [
        pytest.param(MUTATION.replace(", reopened]", ", reopened, edited]"), "pull request events", id="on an edit"),
        pytest.param(
            MUTATION.replace("    name: Mutation gate\n", "    name: Validation\n"), "required", id="required"
        ),
        pytest.param(MUTATION.replace("    name: Mutation gate\n", ""), "required check", id="unnamed"),
        pytest.param(MUTATION.replace("ubuntu-latest", "windows-latest"), "not ubuntu-latest", id="on Windows"),
        pytest.param(MUTATION.replace("timeout-minutes: 60", "timeout-minutes: 600"), "time limit", id="a long limit"),
        pytest.param(MUTATION.replace("fetch-depth: 0", "fetch-depth: 1"), "whole history", id="a shallow checkout"),
        pytest.param(
            MUTATION.replace("path: requirements-mutation.txt", "path: requirements-dev.txt"),
            "sets up Python",
            id="the cache keyed on another file",
        ),
        pytest.param(
            MUTATION.replace("-r requirements-mutation.txt", "-r requirements-dev.txt"), "runs", id="no mutation tools"
        ),
        pytest.param(MUTATION.replace(" --require-hashes", ""), "runs", id="no hash checking"),
        pytest.param(MUTATION.replace(" --only-binary :all:", ""), "runs", id="source distributions allowed"),
        pytest.param(
            MUTATION.replace("python -B tools/mutation_gate.py", "python -B tools/mutation_gate.py || true"),
            "runs",
            id="a swallowed failure",
        ),
        pytest.param(MUTATION.replace("  mutation:\n", "  gate:\n"), "the one mutation job", id="renamed job"),
        pytest.param(MUTATION + "  extra:\n    runs-on: ubuntu-latest\n", "the one mutation job", id="a second job"),
    ],
)
def test_a_mutation_job_off_its_shape_fails(drifted: str, problem: str) -> None:
    problems = mutation_job_problems(drifted)
    assert any(problem in found for found in problems), problems
