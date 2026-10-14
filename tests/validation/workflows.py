"""The GitHub Actions workflows: their triggers, permissions, secrets, and action pins."""

from __future__ import annotations

import re
import shlex
import unittest

from validation_support import REPOSITORY_ROOT


def required_match(pattern: str, text: str) -> re.Match[str]:
    """re.search for a check that fails, naming the pattern, when the text does not match it."""
    match = re.search(pattern, text)
    if match is None:
        raise AssertionError(f"nothing matches {pattern!r}")
    return match


class WorkflowSyntaxError(ValueError):
    """The workflow uses YAML outside the subset read_workflow reads, or is not valid YAML."""


Node = dict[str, "Node"] | list["Node"] | str | None
KEY_LINE = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_.-]*):(?: +(.*))?")
# What a plain scalar may not start with: YAML flow mappings, anchors, aliases, tags, directives, and reserved signs.
UNSUPPORTED_START = tuple("{&*!%@`")


class _WorkflowReader:
    """A strict reader for the YAML the repository's workflows use: block mappings with plain keys, block sequences,
    flow sequences of scalars, plain and quoted scalars, `|` and `>` block scalars, and comments. Every scalar stays a
    string, so `on` is a key and `3.11` is text. Anything else, a tab, or a repeated key is a WorkflowSyntaxError."""

    def __init__(self, text: str) -> None:
        if "\t" in text:
            raise WorkflowSyntaxError("a tab is outside the subset; indent with spaces")
        self.lines = text.replace("\r\n", "\n").split("\n")
        self.index = 0

    def fail(self, reason: str) -> WorkflowSyntaxError:
        return WorkflowSyntaxError(f"line {self.index + 1}: {reason}")

    def peek(self) -> tuple[int, str] | None:
        """The indent and content of the next line that is neither blank nor a comment, or None at the end."""
        while self.index < len(self.lines):
            line = self.lines[self.index]
            content = line.lstrip(" ")
            if content and not content.startswith("#"):
                return len(line) - len(content), content.rstrip(" ")
            self.index += 1
        return None

    def document(self) -> Node:
        first = self.peek()
        if first is None:
            return None
        if first[0] != 0:
            raise self.fail("the document must start at column 0")
        node = self.node(0)
        if self.peek() is not None:
            raise self.fail("unexpected indentation")
        return node

    def node(self, indent: int) -> Node:
        found = self.peek()
        if found is None:
            raise self.fail("the document ends where a value was expected")
        content = found[1]
        return self.sequence(indent) if content == "-" or content.startswith("- ") else self.mapping(indent)

    def mapping(self, indent: int) -> dict[str, Node]:
        result: dict[str, Node] = {}
        while (found := self.peek()) is not None and found[0] >= indent:
            if found[0] > indent:
                raise self.fail("unexpected indentation")
            match = KEY_LINE.fullmatch(found[1])
            if match is None:
                raise self.fail(f"expected `key: value`, found {found[1]!r}")
            key, rest = match.group(1), match.group(2) or ""
            if key in result:
                raise self.fail(f"the key {key!r} is repeated")
            self.index += 1
            result[key] = self.value(rest, indent)
        return result

    def sequence(self, indent: int) -> list[Node]:
        items: list[Node] = []
        while (found := self.peek()) is not None and found[0] == indent and (found[1] == "-" or found[1][:2] == "- "):
            rest = found[1][1:].lstrip(" ")
            if not rest:
                self.index += 1
                following = self.peek()
                items.append(self.node(following[0]) if following and following[0] > indent else None)
            elif KEY_LINE.fullmatch(rest):
                # A mapping that starts on the dash's line continues at the column its first key starts in.
                column = indent + len(found[1]) - len(rest)
                self.lines[self.index] = " " * column + rest
                items.append(self.mapping(column))
            else:
                self.index += 1
                items.append(self.scalar(rest))
        if found is not None and found[0] > indent:
            raise self.fail("unexpected indentation")
        return items

    def value(self, rest: str, indent: int) -> Node:
        text = self.without_comment(rest) if not rest.startswith(("'", '"', "[")) else rest
        if not text:
            following = self.peek()
            return self.node(following[0]) if following and following[0] > indent else None
        if text in ("|", "|-", ">", ">-"):
            return self.block(indent, folded=text.startswith(">"), keep_end=not text.endswith("-"))
        return self.scalar(rest)

    def block(self, indent: int, *, folded: bool, keep_end: bool) -> str:
        lines: list[str] = []
        while self.index < len(self.lines):
            line = self.lines[self.index]
            if line.strip() and len(line) - len(line.lstrip(" ")) <= indent:
                break
            lines.append(line)
            self.index += 1
        while lines and not lines[-1].strip():
            lines.pop()
        body = [line for line in lines if line.strip()]
        margin = min((len(line) - len(line.lstrip(" ")) for line in body), default=0)
        text = ("\n" if not folded else " ").join(line[margin:] for line in lines)
        return text + "\n" if keep_end and text else text

    def scalar(self, text: str) -> str | list[Node]:
        if text.startswith("'"):
            end = 1
            while (end := text.find("'", end)) >= 0 and text[end + 1 : end + 2] == "'":
                end += 2
            if end < 0:
                raise self.fail("an unclosed single-quoted scalar")
            self.only_comment(text[end + 1 :])
            return text[1:end].replace("''", "'")
        if text.startswith('"'):
            match = re.match(r'"((?:[^"\\]|\\["\\/nt])*)"', text)
            if match is None:
                raise self.fail("an unclosed double-quoted scalar, or an escape outside the subset")
            self.only_comment(text[match.end() :])
            escapes = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t"}
            return re.sub(r"\\(.)", lambda escape: escapes[escape.group(1)], match.group(1))
        if text.startswith("["):
            end = text.find("]")
            if end < 0 or "[" in text[1:end]:
                raise self.fail("a flow sequence outside the subset")
            self.only_comment(text[end + 1 :])
            inner = text[1:end].strip()
            return [self.scalar(item.strip()) for item in inner.split(",")] if inner else []
        plain = self.without_comment(text)
        if plain.startswith(UNSUPPORTED_START) or plain.startswith(("- ", "? ", "---")) or ": " in plain:
            raise self.fail(f"the scalar {plain!r} is outside the subset")
        return plain

    @staticmethod
    def without_comment(text: str) -> str:
        """A plain value without its comment, which starts at a # after a space."""
        match = re.search(r"(?:^| )#", text)
        return (text[: match.start()] if match else text).rstrip(" ")

    def only_comment(self, rest: str) -> None:
        if rest.strip() and not rest.strip().startswith("#"):
            raise self.fail(f"text after a quoted scalar or flow sequence: {rest.strip()!r}")


def read_workflow(text: str) -> Node:
    """A workflow's YAML as dictionaries, lists, strings, and None; see _WorkflowReader for the subset it reads."""
    return _WorkflowReader(text).document()


def _strings(node: Node, path: str = "") -> list[tuple[str, str, bool]]:
    """Every key and scalar in a parsed workflow, as (its path, its text, whether it is a key)."""
    found: list[tuple[str, str, bool]] = []
    if isinstance(node, dict):
        for key, item in node.items():
            here = f"{path}.{key}" if path else key
            found.append((here, key, True))
            found += _strings(item, here)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found += _strings(item, f"{path}[{index}]")
    elif isinstance(node, str):
        found.append((path, node, False))
    return found


def action_pins(workflow: str) -> list[tuple[str, str, str]]:
    """Each `uses:` in a workflow as (action, reference, the rest of the line)."""
    return re.findall(r"(?m)^\s*-?\s*uses:\s*(\S+)@(\S+)(.*)$", workflow)


EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
SECRET_REFERENCE = re.compile(r"\bsecrets\b")
TOKEN_REFERENCE = re.compile(r"\bgithub\s*(?:\.\s*token\b|\[\s*['\"]token['\"]\s*\])")
# The event payload and the head branch's name, which a pull request's author writes: an expression pastes them into
# the step's script, so a step reads the payload from the file GITHUB_EVENT_PATH names instead.
EVENT_REFERENCE = re.compile(r"\bgithub\s*(?:\.\s*(?:event|head_ref)\b|\[\s*['\"](?:event|head_ref)['\"]\s*\])")
# What a job may grant its token: nothing beyond reading.
READ_ONLY_SCOPES = frozenset({"read", "none"})


def _triggers(on: Node) -> list[str]:
    if isinstance(on, dict):
        return list(on)
    if isinstance(on, list):
        return [str(item) for item in on]
    return [on] if isinstance(on, str) else []


def _crons(on: Node) -> list[str]:
    schedule = on.get("schedule") if isinstance(on, dict) else None
    if not isinstance(schedule, list):
        return []
    return [str(item.get("cron")) if isinstance(item, dict) else str(item) for item in schedule]


def _job_permission_problems(jobs: Node) -> list[str]:
    if not isinstance(jobs, dict) or not jobs:
        return ["the workflow has no jobs"]
    problems: list[str] = []
    for name, job in jobs.items():
        permissions = job.get("permissions") if isinstance(job, dict) else None
        read_only = (
            permissions is None
            or permissions == "read-all"
            or (isinstance(permissions, dict) and all(scope in READ_ONLY_SCOPES for scope in permissions.values()))
        )
        if not isinstance(job, dict) or not read_only:
            problems.append(f"job {name} grants permissions beyond read: {permissions}")
    return problems


def _string_problems(tree: Node) -> list[str]:
    """Secrets, the token, and author-written event data, in any expression anywhere, any key, and any value, run
    blocks included."""
    problems: list[str] = []
    for path, text, is_key in _strings(tree):
        if is_key and text == "secrets":
            problems.append(f"{path} passes the workflow's secrets on")
        for expression in EXPRESSION.findall(text):
            if SECRET_REFERENCE.search(expression):
                problems.append(f"{path} reads a secret: ${{{{{expression}}}}}")
            if TOKEN_REFERENCE.search(expression):
                problems.append(f"{path} passes the token to a step: ${{{{{expression}}}}}")
            if EVENT_REFERENCE.search(expression):
                problems.append(f"{path} pastes event data an author writes into the workflow: ${{{{{expression}}}}}")
        if "GITHUB_TOKEN" in text:
            problems.append(f"{path} names GITHUB_TOKEN, which passes the token to a step")
    return problems


def workflow_guard_problems(
    workflow: str, triggers: list[str], actions: set[str], crons: tuple[str, ...] = ()
) -> list[str]:
    """Report how a workflow departs from its expected triggers, crons, and actions, a read-only token, and no secrets.

    The workflow is parsed, so every trigger, every job's permissions, and every `${{ }}` expression, key, and value
    are checked wherever they sit. Every action is pinned to a full commit SHA with its version in a comment, which
    docs/dependency-updates.md states and Dependabot reviews.
    """
    try:
        tree = read_workflow(workflow)
    except WorkflowSyntaxError as exc:
        return [f"the workflow is outside the YAML the policy reads, so nothing else is checked: {exc}"]
    if not isinstance(tree, dict):
        return ["the workflow is not a mapping"]
    problems: list[str] = []
    found = _triggers(tree.get("on"))
    if found != triggers:
        problems.append(f"the triggers are {found}, expected {triggers}")
    scheduled = _crons(tree.get("on"))
    if scheduled != list(crons):
        problems.append(f"the schedule's crons are {scheduled}, expected {list(crons)}")
    if tree.get("permissions") != {"contents": "read"}:
        problems.append(f"the top-level permissions are {tree.get('permissions')}, not exactly `contents: read`")
    problems += _job_permission_problems(tree.get("jobs"))
    problems += _string_problems(tree)
    pins = action_pins(workflow)
    for action, reference, rest in pins:
        if not re.fullmatch(r"[0-9a-f]{40}", reference) or not re.fullmatch(r"\s+# v\d+(?:\.\d+)*", rest):
            problems.append(f"{action}@{reference} is not pinned to a commit SHA with its version in a comment")
    parsed = sorted(text for path, text, is_key in _strings(tree) if not is_key and path.endswith(".uses"))
    if parsed != sorted(f"{action}@{reference}" for action, reference, _ in pins):
        problems.append(f"the actions used are {parsed}, and not every one is a `uses:` line whose pin can be read")
    used = {text.split("@")[0] for text in parsed}
    if used != actions:
        problems.append(f"the actions used are {sorted(used)}, expected {sorted(actions)}")
    return problems


def _mapping(node: Node, *keys: str) -> dict[str, Node]:
    """The mapping at `keys` below `node`, or an empty one when any step of the way is not a mapping."""
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def _steps(job: dict[str, Node]) -> list[dict[str, Node]]:
    steps = job.get("steps")
    return [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []


# The legs' name, which says their platform beside the Linux job's and from which tools/record_job_seconds.py reads
# each leg's shard index and count.
LEG_NAME = "Windows shard ${{ matrix.shard }}, Python ${{ matrix.python-version }}"


def leg_problems(workflow: str, legs: int) -> list[str]:
    """Report how validate.yml's `suite` job departs from `legs` legs on windows-latest named as Windows legs, each
    running its own shard of the validation run."""
    tree = read_workflow(workflow)
    suite = _mapping(tree, "jobs", "suite")
    problems: list[str] = []
    if suite.get("name") != LEG_NAME:
        problems.append(f"the suite's legs are named {suite.get('name')!r}, not {LEG_NAME!r}")
    expected = [f"{index}/{legs}" for index in range(1, legs + 1)]
    shards = _mapping(suite, "strategy", "matrix").get("shard")
    if shards != expected:
        problems.append(f"the suite matrix's shards are {shards}, expected {expected}")
    if suite.get("runs-on") != "windows-latest":
        problems.append(f"the suite runs on {suite.get('runs-on')}, not windows-latest")
    runs = [step for step in _steps(suite) if "tests/run_validation.py" in str(step.get("run", ""))]
    if not runs:
        problems.append("no step runs tests/run_validation.py")
    for step in runs:
        if _mapping(step, "env").get("SHARD") != "${{ matrix.shard }}" or "$env:SHARD" not in str(step.get("run")):
            problems.append(f"the step {step.get('name')} does not run its leg's --shard from matrix.shard")
    return problems


def required_check_problems(workflow: str, jobs: tuple[str, ...]) -> list[str]:
    """Report how validate.yml's `validate` job departs from the one required check over `jobs`: named `Validation`,
    as branch protection requires it, needing each of them, running when one fails, since a skipped required check
    counts as passing, and failing unless each one's result is success."""
    validate = _mapping(read_workflow(workflow), "jobs", "validate")
    problems: list[str] = []
    if validate.get("name") != "Validation":
        problems.append(f"the validate job is named {validate.get('name')!r}, not the required check 'Validation'")
    if validate.get("if") != "${{ !cancelled() }}":
        problems.append("the validate job does not run when a job it needs fails, so a red job would skip the check")
    needs = validate.get("needs")
    needed = [needs] if isinstance(needs, str) else needs if isinstance(needs, list) else []
    steps = _steps(validate)
    for job in jobs:
        if job not in needed:
            problems.append(f"the validate job does not need the {job} job, so it could fail without failing the check")
        result = f"${{{{ needs.{job}.result }}}}"
        names = [name for step in steps for name, value in _mapping(step, "env").items() if value == result]
        if not any(f'test "${name}" = success' in str(step.get("run", "")) for step in steps for name in names):
            problems.append(f"no step of the validate job fails unless the {job} job's result is success")
    return problems


# What the linux job's validation step runs, as the Windows legs' step does but on one leg: every suite outside a
# pull request, and a pull request's body and title as files written from the event payload.
LINUX_VALIDATION_LINES = (
    "$arguments = @('-B', 'tests/run_validation.py', '-v')",
    "if ($env:GITHUB_EVENT_NAME -ne 'pull_request') { $arguments += '--full' }",
    "[System.IO.File]::WriteAllText($bodyFile, [string]$payload.pull_request.body)",
    "$arguments += @('--pr-body', $bodyFile)",
    "[System.IO.File]::WriteAllText($titleFile, [string]$payload.pull_request.title)",
    "$arguments += @('--pr-title', $titleFile)",
    "python @arguments",
)


def linux_job_problems(workflow: str) -> list[str]:
    """Report how validate.yml's `linux` job departs from one unsharded validation run on ubuntu-latest, named as
    the Linux run: the checkout's whole history, Python at the floor with pip's cache keyed on requirements-dev.txt,
    ShellCheck from Ubuntu's archive, PSScriptAnalyzer at the workflow's pin, and the Windows legs' validation step
    without its shard. `required_check_problems` holds it inside the required check."""
    tree = read_workflow(workflow)
    linux = _mapping(tree, "jobs", "linux")
    if not linux:
        return ["there is no linux job"]
    problems: list[str] = []
    if linux.get("runs-on") != "ubuntu-latest":
        problems.append(f"the linux job runs on {linux.get('runs-on')}, not ubuntu-latest")
    if "Linux" not in str(linux.get("name", "")):
        problems.append(f"the linux job is named {linux.get('name')!r}, which does not say Linux")
    steps = _steps(linux)
    checkouts = [_mapping(step, "with") for step in steps if str(step.get("uses", "")).startswith("actions/checkout@")]
    if checkouts != [{"fetch-depth": "0"}]:
        problems.append(f"the linux job checks out {checkouts}, not the whole history with fetch-depth 0")
    python = [_mapping(step, "with") for step in steps if str(step.get("uses", "")).startswith("actions/setup-python@")]
    if python != [{"python-version": "3.11", "cache": "pip", "cache-dependency-path": "requirements-dev.txt"}]:
        problems.append(f"the linux job sets up Python as {python}, not 3.11 with pip's cache on requirements-dev.txt")
    scripts = [str(step.get("run", "")) for step in steps]
    for install in (
        "sudo apt-get install --yes shellcheck",
        "Install-Module PSScriptAnalyzer -RequiredVersion $env:PSSCRIPTANALYZER_VERSION ",
        "python -m pip install -r requirements-dev.txt",
    ):
        if not any(install in script for script in scripts):
            problems.append(f"no step of the linux job runs {install!r}")
    runs = [step for step in steps if "tests/run_validation.py" in str(step.get("run", ""))]
    if len(runs) != 1:
        problems.append(f"the linux job has {len(runs)} steps that run tests/run_validation.py, not one")
    for step in runs:
        script = str(step.get("run", ""))
        if step.get("shell") != "pwsh":
            problems.append(f"the step {step.get('name')} runs in {step.get('shell')}, not pwsh as the legs' does")
        if "--shard" in script:
            problems.append(f"the step {step.get('name')} runs a shard, not the whole run")
        lines = {line.strip() for line in script.splitlines()}
        for expected in LINUX_VALIDATION_LINES:
            if expected not in lines:
                problems.append(f"the step {step.get('name')} does not run {expected!r}")
    return problems


def tool_cache_problems(workflow: str, caches: dict[str, str]) -> list[str]:
    """Report how validate.yml's tool caches depart from `caches`: each `actions/cache` key, mapped to the install
    command that runs when it misses. Every version a key names is a workflow-level `env` pin, so the install and
    the key read one declaration, and pip's cache is keyed on requirements-dev.txt."""
    tree = read_workflow(workflow)
    steps = _steps(_mapping(tree, "jobs", "suite"))
    keys = [
        str(_mapping(step, "with").get("key"))
        for step in steps
        if str(step.get("uses", "")).startswith("actions/cache@")
    ]
    problems: list[str] = []
    if sorted(keys) != sorted(caches):
        problems.append(f"the cache keys are {keys}, expected {sorted(caches)}")
    pins = _mapping(tree, "env")
    scripts = "\n".join(str(step.get("run", "")) for step in steps)
    for key, command in caches.items():
        for name in re.findall(r"\$\{\{ env\.(\w+) \}\}", key):
            if name not in pins:
                problems.append(f"the cache key {key} reads {name}, which the workflow's env does not pin")
        if command not in scripts:
            problems.append(f"no step runs {command!r} when the cache keyed {key} misses")
    python = [_mapping(step, "with") for step in steps if str(step.get("uses", "")).startswith("actions/setup-python@")]
    if [{"cache": item.get("cache"), "path": item.get("cache-dependency-path")} for item in python] != [
        {"cache": "pip", "path": "requirements-dev.txt"}
    ]:
        problems.append("setup-python does not cache pip keyed on requirements-dev.txt")
    return problems


def _verify_commands(script: str) -> list[list[str]]:
    """Read the verify invocations as command data, not executable PowerShell. The pinned syntax uses literal
    arguments and parenthesized Join-Path calls; quoted labels stay one argument and comments supply no command."""
    commands = []
    for line in script.splitlines():
        if not re.match(r"^\s*python\s+(?:deploy\.py|tools/deployable_report\.py)\s+verify(?:\s|$)", line):
            continue
        lexer = shlex.shlex(line, posix=False, punctuation_chars="|()")
        lexer.whitespace_split = True
        commands.append(list(lexer))
    return commands


def _verify_pair_problems(commands: list[list[str]], filename: str) -> list[str]:
    saved = [["(", "Join-Path", "$env:LOGS", f"{quote}{filename}{quote}", ")"] for quote in ("'", '"')]
    producers = [command for command in commands if command[:3] == ["python", "deploy.py", "verify"]]
    expected = [["python", "deploy.py", "verify", "2>&1", "|", "Tee-Object", "-FilePath", *path] for path in saved]
    problems = []
    if len(commands) != 2 or len(producers) != 1 or commands[0] not in expected:
        problems.append(f"deploy.py verify must save exactly {filename} through Tee-Object -FilePath before its report")
    reports = [command for command in commands if command[:3] == ["python", "tools/deployable_report.py", "verify"]]
    option = "--deploy-verify-log"
    if len(reports) != 1 or reports[0].count(option) != 1:
        problems.append(f"exactly one deployable_report.py verify must pass {option} for {filename} in this step")
    else:
        offset = reports[0].index(option) + 1
        if reports[0][offset : offset + 5] not in saved:
            problems.append(f"deployable_report.py verify must read {filename} with {option} (Join-Path $env:LOGS)")
    return problems


def verify_log_problems(workflow: str) -> list[str]:
    """Hold deployable.yml's first and second verify reports to the exact log their own step writes, in order."""
    steps = _steps(_mapping(read_workflow(workflow), "jobs", "deployable"))
    pairs: list[tuple[str, list[list[str]]]] = []
    problems: list[str] = []
    for index, step in enumerate(steps):
        name = str(step.get("name", f"step {index + 1}"))
        try:
            commands = _verify_commands(str(step.get("run", "")))
        except ValueError as error:
            problems.append(f"{name}: verify command arguments cannot be read: {error}")
            continue
        if commands:
            pairs.append((name, commands))
    if len(pairs) != 2:
        problems.append(f"expected first and second verify steps, found {len(pairs)}")
    for filename, (name, commands) in zip(("verify-first.log", "verify-second.log"), pairs, strict=False):
        problems += [f"{name}: {problem}" for problem in _verify_pair_problems(commands, filename)]
    return problems


# The step of deployable.yml's Windows job that its Linux job has no counterpart for: Codex CLI's machine settings
# exist only on Windows (docs/codex-support.md).
WINDOWS_ONLY_DEPLOYABLE_STEPS = ("Make the Codex sandbox setting and record the Python aliases",)


def deployable_linux_problems(workflow: str) -> list[str]:
    """Report how deployable.yml's `deployable-linux` job departs from its Windows job run on a fresh ubuntu-latest
    runner in Bash: the same matrix, runtime versions, and time limit, and the same steps in the same order, but the
    Windows-only ones."""
    tree = read_workflow(workflow)
    windows = _mapping(tree, "jobs", "deployable")
    linux = _mapping(tree, "jobs", "deployable-linux")
    if not linux:
        return ["there is no deployable-linux job"]
    problems: list[str] = []
    if windows.get("runs-on") != "windows-latest":
        problems.append(f"the deployable job runs on {windows.get('runs-on')}, not windows-latest")
    if linux.get("runs-on") != "ubuntu-latest":
        problems.append(f"the deployable-linux job runs on {linux.get('runs-on')}, not ubuntu-latest")
    if _mapping(linux, "defaults", "run").get("shell") != "bash":
        problems.append("the deployable-linux job's steps do not run in bash")
    for key in ("strategy", "env", "timeout-minutes"):
        if linux.get(key) != windows.get(key):
            problems.append(f"the deployable-linux job's {key} is not the deployable job's")
    expected = [
        str(step.get("name")) for step in _steps(windows) if step.get("name") not in WINDOWS_ONLY_DEPLOYABLE_STEPS
    ]
    found = [str(step.get("name")) for step in _steps(linux)]
    if found != expected:
        problems.append(f"the deployable-linux job's steps are {found}, expected {expected}")
    return problems


LINUX_VERIFY = re.compile(r"python3\s+(?:deploy\.py|tools/deployable_report\.py)\s+verify(?:\s|$)")


def _linux_verify_pair_problems(lines: list[str], filename: str) -> list[str]:
    producer = f'python3 deploy.py verify 2>&1 | tee "$LOGS/{filename}" || deploy_verify=$?'
    problems: list[str] = []
    reports = [line for line in lines if line.startswith("python3 tools/deployable_report.py verify")]
    if lines.count("deploy_verify=0") != 1 or lines.count(producer) != 1 or len(reports) != 1:
        return [f"the step must set deploy_verify=0, then run {producer!r}, then one deployable_report.py verify"]
    if not lines.index("deploy_verify=0") < lines.index(producer) < lines.index(reports[0]):
        problems.append(f"deploy_verify=0, the verify that saves {filename}, and its report must come in that order")
    try:
        words = shlex.split(reports[0])
    except ValueError as error:
        return [f"the report's arguments cannot be read: {error}"]
    for option, value, written in (
        ("--deploy-verify-log", f"$LOGS/{filename}", f'--deploy-verify-log "$LOGS/{filename}"'),
        ("--deploy-verify-exit", "$deploy_verify", '--deploy-verify-exit "$deploy_verify"'),
    ):
        if words.count(option) != 1 or words[words.index(option) + 1 : words.index(option) + 2] != [value]:
            problems.append(f"deployable_report.py verify must pass {option} {value} once")
        elif written not in reports[0]:
            problems.append(f"deployable_report.py verify must quote {value} as {written!r}")
    return problems


def linux_verify_log_problems(workflow: str) -> list[str]:
    """Hold deployable.yml's Linux verify reports to the exact log their own step's verify writes, in order, with that
    verify's exit status, which a failing pipeline would otherwise end the step on before its report."""
    steps = _steps(_mapping(read_workflow(workflow), "jobs", "deployable-linux"))
    pairs: list[tuple[str, list[str]]] = []
    for index, step in enumerate(steps):
        lines = [line.strip() for line in str(step.get("run", "")).splitlines()]
        lines = [line for line in lines if line and not line.startswith("#")]
        if any(LINUX_VERIFY.search(line) for line in lines):
            pairs.append((str(step.get("name", f"step {index + 1}")), lines))
    problems: list[str] = []
    if len(pairs) != 2:
        problems.append(f"expected first and second verify steps, found {len(pairs)}")
    for filename, (name, lines) in zip(("verify-first.log", "verify-second.log"), pairs, strict=False):
        problems += [f"{name}: {problem}" for problem in _linux_verify_pair_problems(lines, filename)]
    return problems


# The uninstall check in each deployable job, in its own shell: the step before the uninstall saves what the
# deployment owns, and the step after it names what is left, files included, through tools/deployable_report.py, which
# tests/tools/test_deployable_report.py covers. A listing the step wrote for itself looked only at directories, so a
# shared file left behind passed.
RECORD_OWNED_STEP = "Record what the deployment owns"
UNINSTALL_STEP = "Uninstall"
CONFIRM_UNINSTALL_STEP = "Confirm the profile holds nothing from the suite"
UNINSTALL_CHECKS = {
    "deployable": (
        [
            "python tools/deployable_report.py owned --owned (Join-Path $env:LOGS 'owned.json')",
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        ],
        [
            "python tools/deployable_report.py leftovers --owned (Join-Path $env:LOGS 'owned.json') "
            "--results $env:RESULTS",
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        ],
    ),
    "deployable-linux": (
        ['python3 tools/deployable_report.py owned --owned "$LOGS/owned.json"'],
        ['python3 tools/deployable_report.py leftovers --owned "$LOGS/owned.json" --results "$RESULTS"'],
    ),
}


def _script_lines(step: dict[str, Node]) -> list[str]:
    """A step's run script as its lines, stripped, without blank lines and comments."""
    lines = [line.strip() for line in str(step.get("run", "")).splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def uninstall_check_problems(workflow: str) -> list[str]:
    """Hold each deployable job's uninstall between the step that saves what the deployment owns and the step that
    names what the uninstall left, each running exactly its deployable_report.py command and nothing else."""
    tree = read_workflow(workflow)
    problems: list[str] = []
    for job, (record, confirm) in UNINSTALL_CHECKS.items():
        steps = _steps(_mapping(tree, "jobs", job))
        names = [step.get("name") for step in steps]
        if names.count(UNINSTALL_STEP) != 1:
            problems.append(f"{job}: expected one {UNINSTALL_STEP!r} step, found {names.count(UNINSTALL_STEP)}")
            continue
        index = names.index(UNINSTALL_STEP)
        for position, where, name, lines in (
            (index - 1, "before", RECORD_OWNED_STEP, record),
            (index + 1, "after", CONFIRM_UNINSTALL_STEP, confirm),
        ):
            step = steps[position] if 0 <= position < len(steps) else {}
            if step.get("name") != name:
                problems.append(f"{job}: the step {where} {UNINSTALL_STEP!r} is not {name!r}")
            elif set(step) != {"name", "run"}:
                problems.append(
                    f"{job}: {name!r} must set only its name and run, not {sorted(set(step) - {'name', 'run'})}"
                )
            elif _script_lines(step) != lines:
                problems.append(f"{job}: {name!r} must run exactly {lines}")
    return problems


class WorkflowsPolicies(unittest.TestCase):
    def test_deployable_verify_reports_read_the_logs_saved_by_their_own_steps(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/deployable.yml").read_text(encoding="utf-8")
        self.assertEqual([], verify_log_problems(workflow))
        self.assertEqual([], linux_verify_log_problems(workflow))

    def test_deployable_workflow_checks_what_the_uninstall_left_through_the_report_in_both_jobs(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/deployable.yml").read_text(encoding="utf-8")
        self.assertEqual([], uninstall_check_problems(workflow))

    def test_deployable_workflow_runs_the_windows_job_again_on_a_fresh_linux_runner(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/deployable.yml").read_text(encoding="utf-8")
        self.assertEqual([], deployable_linux_problems(workflow))

    def test_deployable_workflow_is_a_dispatched_and_weekly_pinned_check_without_secrets(self) -> None:
        workflows = REPOSITORY_ROOT / ".github/workflows"
        workflow = (workflows / "deployable.yml").read_text(encoding="utf-8")
        reviewed = (workflows / "validate.yml").read_text(encoding="utf-8")
        # Dispatch and a weekly schedule are its only triggers, so it can never be a required status check or run on a
        # pull request. The schedule is Tuesday's, the day after validate.yml's Monday run.
        self.assertEqual(
            [],
            workflow_guard_problems(
                workflow,
                ["workflow_dispatch", "schedule"],
                {"actions/checkout", "actions/setup-python", "actions/setup-node", "actions/upload-artifact"},
                ("23 6 * * 2",),
            ),
        )
        # An action both workflows use is pinned to the commit Dependabot reviews in validate.yml.
        shared = {action for action, _, _ in action_pins(workflow)} & set(re.findall(r"uses:\s*(\S+)@", reviewed))
        self.assertTrue(shared)
        for action in shared:
            with self.subTest(shared=action):
                self.assertEqual(
                    required_match(rf"{re.escape(action)}@(\S+ +# v\S+)", reviewed).group(1),
                    required_match(rf"{re.escape(action)}@(\S+ +# v\S+)", workflow).group(1),
                )

    def test_validate_workflow_is_pinned_read_only_and_runs_on_its_own_events(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        # No pull_request_target: a pull request's code never runs with the base repository's token or secrets.
        self.assertEqual(
            [],
            workflow_guard_problems(
                workflow,
                ["pull_request", "push", "schedule", "workflow_dispatch"],
                {"actions/checkout", "actions/setup-python", "actions/cache"},
                ("23 6 * * 1",),
            ),
        )
        # The upgrade-notes check reads the pull request body and the title check its title, so an edit to either
        # reruns validation, and each reaches the runner as a file written from the event payload, which the guard
        # above keeps out of expressions.
        tree = read_workflow(workflow)
        triggers = tree.get("on") if isinstance(tree, dict) else None
        self.assertEqual(
            {"types": ["opened", "synchronize", "reopened", "edited"]},
            triggers.get("pull_request") if isinstance(triggers, dict) else None,
        )
        self.assertIn(
            "Get-Content -LiteralPath $env:GITHUB_EVENT_PATH -Raw -Encoding utf8 | ConvertFrom-Json", workflow
        )
        self.assertIn("[System.IO.File]::WriteAllText($bodyFile, [string]$payload.pull_request.body)", workflow)
        self.assertIn("$arguments += @('--pr-body', $bodyFile)", workflow)
        self.assertIn("[System.IO.File]::WriteAllText($titleFile, [string]$payload.pull_request.title)", workflow)
        self.assertIn("$arguments += @('--pr-title', $titleFile)", workflow)

    def test_validate_workflow_runs_four_legs_and_restores_the_pinned_tools_from_caches(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        # Keep the leg count here and in the workflow synchronized.
        self.assertEqual([], leg_problems(workflow, 4))
        self.assertEqual(
            [],
            tool_cache_problems(
                workflow,
                {
                    "shellcheck-${{ runner.os }}-${{ env.SHELLCHECK_VERSION }}": (
                        "choco install shellcheck --version $env:SHELLCHECK_VERSION "
                    ),
                    "psscriptanalyzer-${{ runner.os }}-${{ env.PSSCRIPTANALYZER_VERSION }}": (
                        "Install-Module PSScriptAnalyzer -RequiredVersion $env:PSSCRIPTANALYZER_VERSION "
                    ),
                },
            ),
        )

    def test_validate_workflow_runs_the_whole_run_on_linux_inside_the_required_check(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        self.assertEqual([], linux_job_problems(workflow))
        # Every supported platform gates a merge through the one check branch protection requires.
        self.assertEqual([], required_check_problems(workflow, ("suite", "linux")))

    def test_deployable_workflow_installs_the_runtime_versions_the_readme_lists_for_the_fresh_runner(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/deployable.yml").read_text(encoding="utf-8")
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        for runtime, key in (
            ("Claude Code", "claude-version"),
            ("Codex CLI", "codex-version"),
            ("GitHub Copilot CLI", "copilot-version"),
        ):
            with self.subTest(runtime=runtime):
                # The third column: the maintainer's machines come first and may be ahead of the runner.
                tested = required_match(rf"(?m)^\| {runtime} \| \S+ \| (\d+(?:\.\d+)+) \|", readme).group(1)
                default = required_match(
                    rf"(?m)^      {key}:\n(?:        .*\n)*?        default: '([^']+)'", workflow
                ).group(1)
                self.assertEqual(tested, default)
                # A scheduled run has no inputs, so its README leg falls back to the same version, in each job.
                fallbacks = re.findall(rf"inputs\.{key} \|\| '([^']+)'", workflow)
                self.assertEqual([tested, tested], fallbacks)
