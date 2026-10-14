"""Fixture tests for tests/validation/workflows.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

from git_test_base import GitTestCase
from workflows import (
    WorkflowSyntaxError,
    deployable_linux_problems,
    leg_problems,
    linux_job_problems,
    linux_verify_log_problems,
    read_workflow,
    required_check_problems,
    tool_cache_problems,
    uninstall_check_problems,
    verify_log_problems,
    workflow_guard_problems,
)


class WorkflowsFixtures(GitTestCase):
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


class LegFixtures(GitTestCase):
    def test_each_leg_is_named_as_a_windows_leg_and_runs_its_own_shard(self) -> None:
        self.assertEqual([], leg_problems(LEGS, 2))
        for drifted, problem in (
            (LEGS, "expected ['1/3', '2/3', '3/3']"),
            (LEGS.replace("['1/2', '2/2']", "['1/2', '1/2']"), "shards"),
            (LEGS.replace("runs-on: windows-latest", "runs-on: ubuntu-latest"), "not windows-latest"),
            (LEGS.replace(" --shard $env:SHARD", ""), "does not run its leg's --shard"),
            (LEGS.replace("SHARD: ${{ matrix.shard }}", "SHARD: 1/2"), "does not run its leg's --shard"),
            (LEGS.replace("tests/run_validation.py", "tests/other.py"), "no step runs"),
            # The name the seconds recorder reads, which says Windows beside the Linux job.
            (LEGS.replace("Windows shard ${{ matrix.shard }}, ", "Shard ${{ matrix.shard }} on "), "are named"),
            (LEGS.replace("    name: Windows shard", "    name: Windows Shard"), "are named"),
        ):
            with self.subTest(problem=problem):
                count = 3 if problem.startswith("expected") else 2
                problems = leg_problems(drifted, count)
                self.assertTrue(any(problem in found for found in problems), problems)

    def test_each_tool_cache_is_keyed_on_a_workflow_pin_its_install_reads(self) -> None:
        caches = {"tool-${{ env.TOOL_VERSION }}": "install tool --version $env:TOOL_VERSION "}
        self.assertEqual([], tool_cache_problems(LEGS, caches))
        for drifted, problem in (
            (LEGS.replace("key: tool-${{ env.TOOL_VERSION }}", "key: tool-1.2.3"), "the cache keys are"),
            (LEGS.replace("env:\n  TOOL_VERSION: '1.2.3'\n", "env:\n  OTHER: '1'\n"), "does not pin"),
            (LEGS.replace("--version $env:TOOL_VERSION", "--version 1.2.3"), "no step runs"),
            (LEGS.replace("          cache: pip\n", ""), "setup-python does not cache pip"),
            (LEGS.replace("requirements-dev.txt", "requirements.txt"), "setup-python does not cache pip"),
        ):
            with self.subTest(problem=problem):
                problems = tool_cache_problems(drifted, caches)
                self.assertTrue(any(problem in found for found in problems), problems)


LINUX = (
    "jobs:\n  suite:\n    runs-on: windows-latest\n"
    "  linux:\n    name: Validation on Linux\n    runs-on: ubuntu-latest\n    steps:\n"
    "      - uses: actions/checkout@SHA # v1\n        with:\n          fetch-depth: 0\n"
    "      - uses: actions/setup-python@SHA # v1\n        with:\n          python-version: '3.11'\n"
    "          cache: pip\n          cache-dependency-path: requirements-dev.txt\n"
    "      - run: |\n          sudo apt-get update\n          sudo apt-get install --yes shellcheck\n"
    "      - shell: pwsh\n"
    "        run: Install-Module PSScriptAnalyzer -RequiredVersion $env:PSSCRIPTANALYZER_VERSION -Force\n"
    "      - run: python -m pip install -r requirements-dev.txt\n"
    "      - name: Run\n        shell: pwsh\n        run: |\n"
    "          $arguments = @('-B', 'tests/run_validation.py', '-v')\n"
    "          if ($env:GITHUB_EVENT_NAME -ne 'pull_request') { $arguments += '--full' }\n"
    "          [System.IO.File]::WriteAllText($bodyFile, [string]$payload.pull_request.body)\n"
    "          $arguments += @('--pr-body', $bodyFile)\n"
    "          [System.IO.File]::WriteAllText($titleFile, [string]$payload.pull_request.title)\n"
    "          $arguments += @('--pr-title', $titleFile)\n"
    "          python @arguments\n"
    "  validate:\n    needs: suite\n"
)


class LinuxJobFixtures(GitTestCase):
    def test_the_linux_job_runs_the_whole_run_named_as_linux(self) -> None:
        self.assertEqual([], linux_job_problems(LINUX))
        for drifted, problem in (
            (LINUX.replace("  linux:\n", "  other:\n"), "there is no linux job"),
            (LINUX.replace("runs-on: ubuntu-latest", "runs-on: ubuntu-22.04"), "not ubuntu-latest"),
            (LINUX.replace("    name: Validation on Linux\n", "    name: Validation on Ubuntu\n"), "say Linux"),
            (LINUX.replace("    name: Validation on Linux\n", ""), "say Linux"),
            (LINUX.replace("          fetch-depth: 0\n", "          fetch-depth: 1\n"), "the whole history"),
            (LINUX.replace("'3.11'", "'3.x'"), "sets up Python"),
            (LINUX.replace("          cache: pip\n", ""), "sets up Python"),
            (LINUX.replace("install --yes shellcheck", "install shellcheck"), "sudo apt-get install --yes shellcheck"),
            (LINUX.replace("-RequiredVersion $env:PSSCRIPTANALYZER_VERSION ", ""), "Install-Module PSScriptAnalyzer"),
            (LINUX.replace("      - run: python -m pip install -r requirements-dev.txt\n", ""), "requirements-dev"),
            (LINUX.replace("        shell: pwsh\n        run: |", "        shell: bash\n        run: |"), "not pwsh"),
            (LINUX.replace("'-v')", "'-v', '--shard', $env:SHARD)"), "runs a shard"),
            (LINUX.replace("{ $arguments += '--full' }", "{ }"), "--full"),
            (LINUX.replace("          $arguments += @('--pr-title', $titleFile)\n", ""), "--pr-title"),
            (
                LINUX.replace(
                    "      - name: Run\n", "      - run: python tests/run_validation.py\n      - name: Run\n"
                ),
                "not one",
            ),
        ):
            with self.subTest(problem=problem):
                problems = linux_job_problems(drifted)
                self.assertTrue(any(problem in found for found in problems), problems)


REQUIRED_CHECK = (
    "jobs:\n  suite:\n    runs-on: windows-latest\n  linux:\n    runs-on: ubuntu-latest\n"
    "  validate:\n    name: Validation\n    if: ${{ !cancelled() }}\n    needs: [suite, linux]\n    steps:\n"
    "      - name: Require every leg to pass\n        env:\n          WINDOWS: ${{ needs.suite.result }}\n"
    "          LINUX: ${{ needs.linux.result }}\n        run: |\n"
    '          test "$WINDOWS" = success\n          test "$LINUX" = success\n'
)


class RequiredCheckFixtures(GitTestCase):
    def test_the_one_required_check_fails_unless_every_platform_passes(self) -> None:
        self.assertEqual([], required_check_problems(REQUIRED_CHECK, ("suite", "linux")))
        for drifted, problem in (
            # The shape before the Linux job joined the check: it reported, and a red run could still merge.
            (
                REQUIRED_CHECK.replace("    needs: [suite, linux]\n", "    needs: suite\n")
                .replace("          LINUX: ${{ needs.linux.result }}\n", "")
                .replace('          test "$LINUX" = success\n', ""),
                "does not need the linux job",
            ),
            (REQUIRED_CHECK.replace("    needs: [suite, linux]\n", "    needs: linux\n"), "the suite job"),
            (REQUIRED_CHECK.replace('          test "$LINUX" = success\n', ""), "linux job's result"),
            (REQUIRED_CHECK.replace("${{ needs.linux.result }}", "${{ needs.suite.result }}"), "linux job's result"),
            (REQUIRED_CHECK.replace('"$WINDOWS" = success', '"$WINDOWS" != cancelled'), "suite job's result"),
            (REQUIRED_CHECK.replace("    if: ${{ !cancelled() }}\n", ""), "skip the check"),
            (REQUIRED_CHECK.replace("    if: ${{ !cancelled() }}\n", "    if: ${{ success() }}\n"), "skip the check"),
            (REQUIRED_CHECK.replace("    name: Validation\n", "    name: Validation on Windows\n"), "required check"),
        ):
            with self.subTest(problem=problem):
                problems = required_check_problems(drifted, ("suite", "linux"))
                self.assertTrue(any(problem in found for found in problems), problems)


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


class VerifyLogFixtures(GitTestCase):
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


class DeployableLinuxFixtures(GitTestCase):
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


class LinuxVerifyLogFixtures(GitTestCase):
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
    "      - name: Record what the deployment owns\n        run: |\n"
    "          python tools/deployable_report.py owned --owned (Join-Path $env:LOGS 'owned.json')\n"
    "          if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
    "      # The README's selection.\n"
    "      - name: Uninstall\n        run: |\n"
    "          'none' | python deploy.py\n"
    "      - name: Confirm the profile holds nothing from the suite\n        run: |\n"
    "          # Names what is left.\n"
    "          python tools/deployable_report.py leftovers --owned (Join-Path $env:LOGS 'owned.json') "
    "--results $env:RESULTS\n"
    "          if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
    "  deployable-linux:\n    steps:\n"
    "      - name: Record what the deployment owns\n        run: |\n"
    '          python3 tools/deployable_report.py owned --owned "$LOGS/owned.json"\n'
    "      - name: Uninstall\n        run: |\n"
    "          printf 'none\\n' | python3 deploy.py\n"
    "      - name: Confirm the profile holds nothing from the suite\n        run: |\n"
    '          python3 tools/deployable_report.py leftovers --owned "$LOGS/owned.json" --results "$RESULTS"\n'
)


class UninstallCheckFixtures(GitTestCase):
    def test_both_jobs_save_what_is_owned_before_the_uninstall_and_report_what_is_left_after(self) -> None:
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
                    '          python3 tools/deployable_report.py leftovers --owned "$LOGS/owned.json" '
                    '--results "$RESULTS"\n',
                    directories_only,
                ),
                "deployable-linux: 'Confirm the profile holds nothing from the suite' must run exactly",
            ),
            (
                "no exit status check",
                UNINSTALL_CHECK.replace("          if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n", "", 1),
                "deployable: 'Record what the deployment owns' must run exactly",
            ),
            (
                "another saved file",
                windows_part
                + linux_part.replace('--owned "$LOGS/owned.json" --results', '--owned "$LOGS/x.json" --results'),
                "deployable-linux: 'Confirm the profile holds nothing from the suite' must run exactly",
            ),
            (
                "the record after the uninstall",
                UNINSTALL_CHECK.replace("Record what the deployment owns", "Record", 1),
                "deployable: the step before 'Uninstall' is not 'Record what the deployment owns'",
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
            ("no uninstall", windows_part.replace("name: Uninstall", "name: Remove") + linux_part, "expected one"),
            ("no linux job", windows_part, "deployable-linux: expected one 'Uninstall' step, found 0"),
        ):
            with self.subTest(drift=name):
                problems = uninstall_check_problems(drifted)
                self.assertTrue(any(problem in found for found in problems), problems)


class WorkflowReaderFixtures(GitTestCase):
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

    def test_yaml_outside_the_subset_is_refused(self) -> None:
        for text, reason in (
            ("a:\n\tb: c\n", "tab"),
            ("a: 1\na: 2\n", "repeated"),
            ("a: {b: c}\n", "outside the subset"),
            ("a: &anchor b\n", "outside the subset"),
            ("a: *anchor\n", "outside the subset"),
            ("a: !!str b\n", "outside the subset"),
            ("a: b: c\n", "outside the subset"),
            ("a: [b, [c]]\n", "flow sequence"),
            ("a: 'b\n", "unclosed"),
            ("a: 'b' c\n", "text after"),
            ("a:\n  b: c\n d: e\n", "indentation"),
            ("a:\n  b: c\n    d: e\n", "indentation"),
            ("just text\n", "expected `key: value`"),
            ("  a: b\n", "column 0"),
        ):
            with self.subTest(text=text), self.assertRaisesRegex(WorkflowSyntaxError, reason):
                read_workflow(text)


if __name__ == "__main__":
    unittest.main()
