"""The GitHub Actions workflows: their triggers, permissions, secrets, and action pins."""

from __future__ import annotations

import re
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


def leg_problems(workflow: str, legs: int) -> list[str]:
    """Report how validate.yml's `suite` job departs from `legs` legs on windows-latest, each running its own shard
    of the validation run, every one of them required through the `validate` job."""
    tree = read_workflow(workflow)
    suite = _mapping(tree, "jobs", "suite")
    problems: list[str] = []
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
    if _mapping(tree, "jobs", "validate").get("needs") != "suite":
        problems.append("the validate job does not need the suite job, so a leg could fail without failing it")
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


class WorkflowsPolicies(unittest.TestCase):
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
        # The upgrade-notes check reads the pull request body, so an edit to the body reruns validation, and the body
        # reaches the runner as a file written from the event payload, which the guard above keeps out of expressions.
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

    def test_validate_workflow_runs_four_legs_and_restores_the_pinned_tools_from_caches(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        # Four four-core runners take a run of about 1,450 job-seconds to about three minutes; the count changes
        # here and in the workflow together.
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
                # A scheduled run has no inputs, so its README leg falls back to the same version.
                fallback = required_match(rf"inputs\.{key} \|\| '([^']+)'", workflow).group(1)
                self.assertEqual(tested, fallback)
