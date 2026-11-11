"""The GitHub Actions workflows: their triggers, permissions, secrets, and action pins."""

from __future__ import annotations

import re
import shlex
import tomllib
import unicodedata
import unittest
from collections.abc import Collection, Iterable, Mapping
from pathlib import Path

import file_presence
from python_checks import MYPY_CACHE_VARIABLE
from validation_support import REPOSITORY_ROOT, repository_files

from deployer import config


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


def shared_pin_problems(reviewed: str, workflow: str) -> list[str]:
    """Report each action of a workflow that validate.yml's text `reviewed` also uses, or that comes from the
    repository of one it uses, such as actions/cache/restore beside actions/cache, pinned to another commit and version
    than validate.yml's, which Dependabot reviews, and a workflow that shares no action with it."""
    reviewed_actions = set(re.findall(r"uses:\s*(\S+)@", reviewed))
    shared = sorted(
        {
            (action, repository)
            for action, _, _ in action_pins(workflow)
            if (repository := "/".join(action.split("/")[:2])) in reviewed_actions
        }
    )
    if not shared:
        return ["the workflow shares no action with validate.yml, whose pins it is held to"]
    problems = []
    for action, repository in shared:
        wanted = required_match(rf"{re.escape(repository)}@(\S+ +# v\S+)", reviewed).group(1)
        found = required_match(rf"{re.escape(action)}@(\S+ +# v\S+)", workflow).group(1)
        if found != wanted:
            problems.append(f"{action} is pinned to {found}, not to validate.yml's pin of {repository}, {wanted}")
    return problems


EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
# Context names are case-insensitive in an expression, so SECRETS.X reads a secret as secrets.x does.
SECRET_REFERENCE = re.compile(r"\bsecrets\b", re.IGNORECASE)
# The fields of the github context an expression may read, none of which holds text a pull request's author writes:
# event_name and workflow are fixed by the event and the workflow file, and ref, for the triggers the workflows take,
# is refs/heads/main, refs/pull/<number>/merge, or the branch a writer dispatched on. The rest of the context carries
# the token and the event payload, which an expression would paste into the step, so a step reads the payload from the
# file GITHUB_EVENT_PATH names instead.
GITHUB_FIELDS = frozenset({"event_name", "ref", "workflow"})
# One token of an expression: a string literal, a name, a number, or an operator. Anything else is unreadable.
EXPRESSION_TOKEN = re.compile(
    r"\s*(?:'(?:[^']|'')*'|(?P<name>[A-Za-z_][A-Za-z0-9_-]*)|\d+(?:\.\d+)?|==|!=|<=|>=|&&|\|\||[.()\[\],!<>*])\s*"
)
# What a job may grant its token: nothing beyond reading.
READ_ONLY_SCOPES = frozenset({"read", "none"})
# The one form in which a sanctioned step is given the token, for gh, and the permissions of a job that holds one:
# reading the checkout and the issues, so the known-failure check can ask whether each entry's issue is open. A pinned
# contract value of the 2026-10-29 decision in docs/design.md on what validation may reach.
CREDENTIAL_VARIABLE = "GH_TOKEN"
CREDENTIAL_VALUE = "${{ github.token }}"
TOKEN_JOB_PERMISSIONS = {"contents": "read", "issues": "read"}
# How a step given the token is written: its name, its env holding the token alone, and its one command, so no key
# changes how or where that command runs, such as `shell`, `working-directory`, or another variable like BASH_ENV.
TOKEN_STEP_KEYS = frozenset({"name", "env", "run"})
# The keys a job holding the token may have: none that changes how or where its steps run, such as `defaults`, an
# `env`, a `container`, `services`, or a matrix, and it runs on a runner GitHub hosts, never one a label could place
# elsewhere.
TOKEN_JOB_KEYS = frozenset({"name", "runs-on", "timeout-minutes", "permissions", "steps", "if", "needs"})
HOSTED_RUNNER = re.compile(r"(?:ubuntu|windows|macos)-(?:latest|\d+(?:\.\d+)?)(?:-arm)?")
# What a workflow that gives the token may set for every job: no `defaults`, and only env variables that pin a tool's
# version or a download's digest, whose names no shell or interpreter reads.
TOKEN_WORKFLOW_ENV = re.compile(r"[A-Z][A-Z0-9_]*_(?:VERSION|SHA256)")
# The inputs every actions/checkout step gives, whatever else it gives: the action otherwise writes the job's token into
# the checkout's git configuration, where every later step of the job, and every process one starts, can read it.
NO_STORED_CREDENTIAL = {"persist-credentials": "false"}


def _expression_tokens(expression: str) -> list[tuple[str, bool]] | None:
    """An expression's tokens, each with whether it is a name, or None when some of it reads as no token."""
    tokens: list[tuple[str, bool]] = []
    position = 0
    while position < len(expression):
        match = EXPRESSION_TOKEN.match(expression, position)
        if match is None or match.end() == position:
            return None
        tokens.append((match.group().strip(), match.group("name") is not None))
        position = match.end()
    return tokens


def github_context_problem(expression: str) -> str | None:
    """Why an expression may reach the github context beyond GITHUB_FIELDS, or None when it cannot.

    Each reference to the context must be a dotted read of one allowed field and nothing after it, so the whole
    context, toJSON or fromJSON of it, a bracketed or computed field, and a field below an allowed one all fail, and so
    does an expression this reader cannot split into tokens."""
    tokens = _expression_tokens(expression)
    if tokens is None:
        return "the policy cannot read it, so it may reach the token or event data"
    for index, (text, is_name) in enumerate(tokens):
        if not is_name or text.casefold() != "github" or (index and tokens[index - 1][0] == "."):
            continue
        field = tokens[index + 1 : index + 3]
        after = tokens[index + 3 : index + 4]
        if (
            len(field) != 2
            or field[0][0] != "."
            or not field[1][1]
            or field[1][0].casefold() not in GITHUB_FIELDS
            or after in ([(".", False)], [("[", False)])
        ):
            allowed = ", ".join(f"github.{name}" for name in sorted(GITHUB_FIELDS))
            return f"it reads the github context, which carries the token and event data, beyond {allowed}"
    return None


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


def _job_permission_problems(
    jobs: Node, token_jobs: Collection[str] = (), token_permissions: Mapping[str, str] = TOKEN_JOB_PERMISSIONS
) -> list[str]:
    if not isinstance(jobs, dict) or not jobs:
        return ["the workflow has no jobs"]
    problems: list[str] = []
    for name, job in jobs.items():
        permissions = job.get("permissions") if isinstance(job, dict) else None
        if name in token_jobs:
            if permissions != dict(token_permissions):
                problems.append(
                    f"job {name} hands a step the token, so its permissions are exactly {dict(token_permissions)}, "
                    f"not {permissions}"
                )
            continue
        read_only = (
            permissions is None
            or permissions == "read-all"
            or (isinstance(permissions, dict) and all(scope in READ_ONLY_SCOPES for scope in permissions.values()))
        )
        if not isinstance(job, dict) or not read_only:
            problems.append(f"job {name} grants permissions beyond read: {permissions}")
    return problems


def _expressions(path: str, text: str, is_key: bool) -> list[str]:
    """The expressions GitHub evaluates in a key or value: each `${{ }}` in it, or for an `if:` value with none, the
    whole value, which GitHub evaluates as an expression without the wrapping."""
    wrapped = EXPRESSION.findall(text)
    if not is_key and not wrapped and path.rsplit(".", 1)[-1] == "if":
        return [text]
    return wrapped


def _token_paths(tree: Node, token_steps: Mapping[tuple[str, str], str]) -> tuple[set[str], list[str]]:
    """The path of the one GH_TOKEN env entry each sanctioned (job, step name) holds, and a problem for each such step
    the workflow lacks, that gives the token as anything but CREDENTIAL_VALUE, or that runs anything but its command,
    and for each other step of its job that runs a command, which would start in a job holding the token."""
    paths: set[str] = set()
    problems: list[str] = []
    for (job, step_name), command in sorted(token_steps.items()):
        steps = _steps(_mapping(tree, "jobs", job))
        index = next((index for index, step in enumerate(steps) if step.get("name") == step_name), None)
        env = _mapping(steps[index], "env") if index is not None else {}
        if index is None or env.get(CREDENTIAL_VARIABLE) != CREDENTIAL_VALUE:
            problems.append(f"job {job}'s step {step_name!r} does not give {CREDENTIAL_VARIABLE}: {CREDENTIAL_VALUE}")
            continue
        paths.add(f"jobs.{job}.steps[{index}].env.{CREDENTIAL_VARIABLE}")
        if steps[index].get("run") != command:
            problems.append(f"job {job}'s step {step_name!r} runs {steps[index].get('run')!r}, not {command!r}")
        if extra := sorted(set(steps[index]) - TOKEN_STEP_KEYS):
            problems.append(
                f"job {job}'s step {step_name!r} holds the token, so it has only {sorted(TOKEN_STEP_KEYS)}, "
                f"not {extra}, which change how or where its command runs"
            )
        if others := sorted(set(env) - {CREDENTIAL_VARIABLE}):
            problems.append(
                f"job {job}'s step {step_name!r} holds the token, so its env gives {CREDENTIAL_VARIABLE} alone, "
                f"not {others}"
            )
        problems += [
            f"job {job} holds the token, so no step but {step_name!r} runs a command, yet {step.get('name')!r} runs one"
            for other, step in enumerate(steps)
            if other != index and "run" in step
        ]
    return paths, problems


def _token_job_problems(tree: Node, token_jobs: Collection[str]) -> list[str]:
    """Each key of a job holding the token, or of its workflow, that changes how or where the job's steps run: a job
    key outside TOKEN_JOB_KEYS, a runner GitHub does not host, the workflow's `defaults`, and a workflow env variable
    TOKEN_WORKFLOW_ENV does not name."""
    if not token_jobs:
        return []
    problems: list[str] = []
    for job in sorted(token_jobs):
        keys = _mapping(tree, "jobs", job)
        if extra := sorted(set(keys) - TOKEN_JOB_KEYS):
            problems.append(
                f"job {job} holds the token, so it has only {sorted(TOKEN_JOB_KEYS)}, not {extra}, which change how "
                "or where its steps run"
            )
        runner = keys.get("runs-on")
        if not isinstance(runner, str) or not HOSTED_RUNNER.fullmatch(runner):
            problems.append(f"job {job} holds the token, so it runs on a runner GitHub hosts, not {runner!r}")
    if isinstance(tree, dict) and "defaults" in tree:
        problems.append("the workflow gives a step the token, so it sets no defaults for how its jobs' steps run")
    problems += [
        f"the workflow gives a step the token, so its env sets only a version or a digest, not {name}"
        for name in sorted(_mapping(tree, "env"))
        if not TOKEN_WORKFLOW_ENV.fullmatch(name)
    ]
    return problems


def _checkout_problems(tree: Node) -> list[str]:
    """Each actions/checkout step whose inputs do not include NO_STORED_CREDENTIAL."""
    return [
        f"job {job}'s step {step.get('name')!r} checks out without persist-credentials: false, so the job's token is "
        "stored where every later step can read it"
        for job in _mapping(tree, "jobs")
        for step in _steps(_mapping(tree, "jobs", job))
        if str(step.get("uses", "")).startswith("actions/checkout@")
        and any(_mapping(step, "with").get(key) != value for key, value in NO_STORED_CREDENTIAL.items())
    ]


def _string_problems(tree: Node, token_paths: Collection[str] = ()) -> list[str]:
    """Secrets, the token, and author-written event data, in any expression anywhere, any key, and any value, run
    blocks included, and any expression the policy cannot read; the token is allowed only at `token_paths`, each a
    sanctioned step's GH_TOKEN entry holding exactly CREDENTIAL_VALUE."""
    problems: list[str] = []
    for path, text, is_key in _strings(tree):
        if not is_key and path in token_paths and text == CREDENTIAL_VALUE:
            continue
        if is_key and text == "secrets":
            problems.append(f"{path} passes the workflow's secrets on")
        for expression in _expressions(path, text, is_key):
            if SECRET_REFERENCE.search(expression):
                problems.append(f"{path} reads a secret: ${{{{{expression}}}}}")
            reason = github_context_problem(expression)
            if reason:
                problems.append(f"{path} has an expression the policy refuses, because {reason}: ${{{{{expression}}}}}")
        if "GITHUB_TOKEN" in text:
            problems.append(f"{path} names GITHUB_TOKEN, which passes the token to a step")
    return problems


def workflow_guard_problems(
    workflow: str,
    triggers: list[str],
    actions: set[str],
    crons: tuple[str, ...] = (),
    token_steps: Mapping[tuple[str, str], str] | None = None,
    token_permissions: Mapping[str, str] = TOKEN_JOB_PERMISSIONS,
) -> list[str]:
    """Report how a workflow departs from its expected triggers, crons, and actions, a read-only token, and no secrets.

    The workflow is parsed, so every trigger, every job's permissions, and every `${{ }}` expression, key, and value
    are checked wherever they sit. Every action is pinned to a full commit SHA with its version in a comment, which
    docs/dependency-updates.md states and Dependabot reviews. The token reaches a step only as one of `token_steps`
    names, a (job, step name), whose env gives GH_TOKEN exactly CREDENTIAL_VALUE and nothing else, whose command is the
    one it maps to, and which has no key but TOKEN_STEP_KEYS, in a job whose permissions are exactly
    `token_permissions`, TOKEN_JOB_PERMISSIONS unless the workflow's entry in TOKEN_STEP_PERMISSIONS names others,
    whose keys are TOKEN_JOB_KEYS on a runner GitHub hosts, and whose other steps run no command,
    in a workflow with no defaults and no env but TOKEN_WORKFLOW_ENV's. Every checkout stores no credential. The pinned
    actions that run before the token step in its job, such as actions/setup-python, which puts Python on its PATH,
    may set its environment, the checkout's post step runs after it, and every job's pinned actions take the token as
    the default of their `token` input; they are trusted at their pins.
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
    token_steps = token_steps or {}
    token_paths, token_problems = _token_paths(tree, token_steps)
    problems += token_problems
    problems += _job_permission_problems(tree.get("jobs"), {job for job, _ in token_steps}, token_permissions)
    problems += _token_job_problems(tree, {job for job, _ in token_steps})
    problems += _checkout_problems(tree)
    problems += _string_problems(tree, token_paths)
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


WORKFLOWS_DIRECTORY = ".github/workflows"
# Every file under .github/workflows, by name, with the triggers, actions, and crons workflow_guard_problems holds it
# to. A file not named here fails validation, so a new workflow is added here in the pull request that adds the file.
# validate.yml takes no pull_request_target, so a pull request's code never runs with the base repository's token or
# secrets. deployable.yml takes only dispatch and a weekly schedule, so it can never be a required status check or run
# on a pull request, and its schedule is Tuesday's, the day after validate.yml's Monday run. runtime-command-lines.yml
# likewise takes only dispatch and a weekly schedule, Wednesday's, the day after deployable.yml's, and
# runtime-changelogs.yml Thursday's, the day after that. mutation.yml runs on pull requests only, also without
# pull_request_target, and its job is not the required check.
WORKFLOW_GUARDS: dict[str, tuple[list[str], set[str], tuple[str, ...]]] = {
    "deployable.yml": (
        ["workflow_dispatch", "schedule"],
        {"actions/checkout", "actions/setup-python", "actions/setup-node", "actions/upload-artifact"},
        ("23 6 * * 2",),
    ),
    "mutation.yml": (
        ["pull_request"],
        {
            "actions/checkout",
            "actions/setup-python",
            "actions/upload-artifact",
            "actions/download-artifact",
            "actions/cache/restore",
            "actions/cache/save",
        },
        (),
    ),
    "runtime-command-lines.yml": (
        ["workflow_dispatch", "schedule"],
        {"actions/checkout", "actions/setup-python", "actions/setup-node"},
        ("23 6 * * 3",),
    ),
    "runtime-changelogs.yml": (
        ["workflow_dispatch", "schedule"],
        {"actions/checkout", "actions/setup-python"},
        ("23 6 * * 4",),
    ),
    "validate.yml": (
        ["pull_request", "push", "schedule", "workflow_dispatch"],
        {"actions/checkout", "actions/setup-python", "actions/cache"},
        ("23 6 * * 1",),
    ),
}


# The changelog watch's one command, which CHANGELOG_JOB runs and TOKEN_STEPS gives the token.
CHANGELOG_WATCH = 'python3 tools/runtime_changelogs.py --summary "$GITHUB_STEP_SUMMARY"'


# Each step, by its job and its name, that is given the token, with the one command it runs: validate.yml's open-issue
# query, which asks GitHub whether each issue a malformed-input known failure names is open, from a program that
# imports none of the policy checks and starts no suite; and runtime-changelogs.yml's watch, which sends it to
# api.github.com alone, so the releases API answers under the token's limit rather than a hosted runner address's
# shared one. No other step of any workflow is given it, and no other step of its job runs a command.
TOKEN_STEPS: dict[str, dict[tuple[str, str], str]] = {
    "validate.yml": {
        ("known-failures", "Ask whether each known failure's issue is open"): "python -B tests/known_failure_issues.py",
    },
    "runtime-changelogs.yml": {
        ("changelogs", "Watch the changelogs and the guidance pages"): CHANGELOG_WATCH,
    },
}
# A token step's job permissions where they are not TOKEN_JOB_PERMISSIONS: the watch reads the checkout and GitHub's
# public releases, and no issue, so its job reads `contents` alone.
TOKEN_STEP_PERMISSIONS: dict[str, dict[str, str]] = {"runtime-changelogs.yml": {"contents": "read"}}


# What a token step's job runs when the workflow runs on a pull request and the job's checkout takes no input but
# NO_STORED_CREDENTIAL's: the pull request's merge commit, from which GitHub also reads the workflow, so the program the
# step starts, every module it imports, and the job itself are the pull request's own, and only a fork's pull request is
# held to a read-only token. docs/design.md's row on what validation may reach and the comment directly above the job
# each state both, in these words. Pinned contract values of the 2026-11-01 refinement of that row.
PULL_REQUEST_CODE_STATEMENT = (
    "on a pull request, the token reaches the code the pull request's merge commit holds at those paths"
)
SAME_REPOSITORY_STATEMENT = (
    "a pull request from a branch of this repository can change the job itself, its permissions included"
)
REACH_STATEMENTS = (PULL_REQUEST_CODE_STATEMENT, SAME_REPOSITORY_STATEMENT)
# What every job's pinned actions do with the run's token, which docs/design.md's row states in these words:
# actions/checkout and actions/setup-python take it as the default of their `token` input, so "no step is given the
# token" is true only of the steps that run a command.
ACTION_INPUT_STATEMENT = "the pinned actions take the run's token as the default of their `token` input"
# The words the retired claims below are written in, matched in text _claim_text has folded. The token by any of its
# names, an expression that reads it included, singular, plural, or possessive, after any of the words that qualify
# it: any possessive, as "the workflow's token" and "the legs' token" are, and "read-only" spelled with a hyphen,
# a space, or neither.
_CREDENTIAL = (
    r"(?:(?:(?:[\w.-]+'s?|read(?:-| )?only|workflow|github|actions|repository|repo|default|own|automatic|built-in) )*"
    r"(?:tokens?|github_token|gh_token)(?:'s?)?(?!\w)"
    r"|\$\{\{ ?(?:secrets\.github_token|github\.token) ?\}\}|(?:secrets\.github_token|github\.token)(?!\w))"
)
_CREDENTIAL_PHRASE = rf"(?:(?:the|a|an|any|its|their|this|that) )?{_CREDENTIAL}"
# An adverb that may stand between a verb's words, as "is ever given" does.
_ADVERB = r"(?:(?:ever|also|then|otherwise|yet|still|now|\w+ly) )?"
# What may hold the token: a step, job, leg, or workflow, after up to two words that qualify it, as "no other job"
# does, and in a pair with any other noun, as "no command or step" does.
_HOLDER_NOUN = r"(?:steps?|jobs?|legs?|workflows?)"
_QUALIFIERS = r"(?:(?:other|single|one|more|further|additional|such|later|individual|remaining|separate) ){0,2}"
_HOLDER = (
    rf"{_QUALIFIERS}(?:[\w-]+ (?:or|nor|and) {_QUALIFIERS})?{_HOLDER_NOUN}(?: (?:or|nor|and) [\w-]+)?"
    r"(?: here| (?:of|in|within|inside|from) (?:(?:this|the|its|that|each|every|any|a) )?(?:[\w.-]+ )?[\w.-]+)?"
)
# The words that say none: "no", "none of the", "not one", "not a single", "zero", "neither".
_NONE = r"(?:no|none(?: of(?: the| its| these)?)?|not (?:one|a single|any)(?: of(?: the| its| these)?)?|zero|neither)"
# Each verb that gives the token to a holder, and each that withholds it, after any auxiliary: "is given", "has been
# given", "will be given", "is being given"; "holds", "should hold", "has held"; and each negated.
_GIVE = r"(?:given|passed|handed|granted|sent)"
_MODALS = r"(?:will|would|shall|should|can|could|may|might|must)"
_PASSIVE = (
    rf"(?:(?:is|are|was|were)(?: being)?|gets?|got|(?:has|have|had) been|{_MODALS} (?:be|get)|(?:does|do|did) get)"
)
_ACTIVE = (
    r"(?:holds?|has|have|receives?|gets?|sees?|uses?|runs? with|read|reach|access|ha(?:s|ve) access to|gets? access to)"
)
_PERFECT = r"(?:has|have|had) (?:held|had|received|got|gotten|seen|used|run with|read|reached|accessed|had access to)"
_GIVEN = (
    rf"(?:{_PASSIVE} {_ADVERB}{_GIVE}|{_ADVERB}(?:(?:{_MODALS}|does|do|did) {_ADVERB})?{_ACTIVE}|{_ADVERB}{_PERFECT})"
)
_NOT = r"(?: not|n't| never)"
_NEGATED_PASSIVE = (
    rf"(?:(?:is|are|was|were){_NOT}(?: being)?|(?:has|have|had){_NOT} been|{_MODALS}{_NOT} (?:be|get)"
    rf"|(?:won't|can't|cannot|shan't) (?:be|get)|(?:does|do|did){_NOT} get)"
)
_NEGATED_ACTIVE = rf"(?:(?:do|does|did|{_MODALS}){_NOT}|won't|can't|cannot|shan't|never)"
_NOT_GIVEN = (
    rf"(?:{_NEGATED_PASSIVE} {_ADVERB}{_GIVE}"
    rf"|{_NEGATED_ACTIVE} {_ADVERB}(?:hold|have|receive|get|see|use|read|reach|access|run with|have access to)"
    rf"|(?:has|have|had){_NOT} (?:held|had|received|got|gotten|seen|used|read|reached|accessed|run with)"
    r"|never (?:holds?|has|receives?|gets?|sees?|uses?)|runs? without)"
)
# The token named first: "the token is given to", "has been passed to", "reaches", "will reach", "goes to", or "is
# available to".
_TOKEN_GOES = (
    rf"(?:{_PASSIVE} {_ADVERB}(?:{_GIVE}|available|visible) to|(?:(?:{_MODALS}|does|do|did) )?(?:reach|go to)"
    r"|reaches|goes to|(?:has|have|had) (?:reached|gone to))"
)
# The token named first with its verb negated: "is not given to", "has never been passed to", "does not reach", "will
# not go to", before "any", "a", or "the" and the holder.
_TOKEN_KEPT = (
    rf"(?:{_NEGATED_PASSIVE} {_ADVERB}(?:{_GIVE}|available|visible) to|{_NEGATED_ACTIVE} (?:reach|go to)"
    rf"|(?:has|have|had){_NOT} (?:reached|gone to)|never (?:reaches|goes to))"
    r"(?: (?:any|a|an|the|one)(?: single)?)?"
)
# The end of a holder named after the token: no letter or apostrophe follows, so "no step's command", whose
# holder owns what the token reaches, is not read as the holder.
_HOLDER_END = r"(?![\w'])"
# "Alone" or "only" closing a holder, and not opening a condition, as "only as", "only through", or "only when" does.
_ALONE = r"(?:alone|only)(?! (?:as|through|by|via|when|if|where|while|in|from|with|under|after|before|because)\b)"
# Claims a workflow comment, the row, or the runner's docstring made before the actions' default input was counted,
# each false while ACTION_INPUT_STATEMENT holds, by the meaning each is named for: that a step, job, leg, or workflow
# is given no token, or that one job alone is given it. A true statement about a step's command names the command, as
# "no command a step runs is given the token" and "the only job whose command is given the run's token" do, so no
# pattern reads it. The patterns read the verbs above and no others: a sentence in other words is the review's to judge.
RETIRED_TOKEN_CLAIMS: dict[str, re.Pattern[str]] = {
    "no step is given the token": re.compile(
        rf"\b{_NONE} {_HOLDER} {_GIVEN} {_CREDENTIAL_PHRASE}"
        rf"|\b{_HOLDER} {_NOT_GIVEN} {_CREDENTIAL_PHRASE}"
        rf"|(?<!\w){_CREDENTIAL_PHRASE} (?:{_TOKEN_GOES}|{_PASSIVE} {_ADVERB}{_GIVE}) {_NONE} "
        rf"{_HOLDER}{_HOLDER_END}"
        rf"|(?<!\w){_CREDENTIAL_PHRASE} {_TOKEN_KEPT} {_HOLDER}{_HOLDER_END}"
    ),
    "the legs hold no token": re.compile(
        rf"\b(?:holds?|held|holding) no {_CREDENTIAL}|\b{_HOLDER} {_GIVEN} no {_CREDENTIAL}"
    ),
    "the only job given the run's token": re.compile(
        rf"\bthe (?:only|one|sole|single) {_HOLDER} (?:(?:that|which) {_GIVEN}|given|holding|with) {_CREDENTIAL_PHRASE}"
        rf"|\bonly (?:the |one |this |that )?(?:[\w-]+ )?{_HOLDER} {_GIVEN} {_CREDENTIAL_PHRASE}"
        rf"|(?<!\w){_CREDENTIAL_PHRASE} {_TOKEN_GOES} (?:the |this |that |one |a )?(?:[\w-]+ )?"
        rf"{_HOLDER_NOUN} {_ALONE}"
        rf"|(?<!\w){_CREDENTIAL_PHRASE} (?:{_PASSIVE} {_ADVERB}{_GIVE} |reaches |goes )?"
        r"(?:only|solely|exclusively) "
        rf"(?:to )?(?:the |this |that |one )?(?:[\w-]+ )?{_HOLDER_NOUN}{_HOLDER_END}"
    ),
}
# The parts of the repository token_claim_texts leaves out, by path or folder, each with why no claim about this
# repository's workflows is read there. Every other file a commit could include is read whole.
TOKEN_CLAIM_OMISSIONS = {
    "tests/validation/test_workflows.py": "its fixture rows write each retired claim",
    "skills/": "a shipped skill speaks of the user's repository, its jobs and steps, and the user's own gh token",
    "agents/": "a shipped agent speaks of the user's repository, as a shipped skill does",
    "tests/fixtures/": "a fixture stands for another repository, not this one's workflows",
}
# The part of a file token_claim_texts blanks rather than reads, by the openings of its first line and of the line
# after its last, with why: the rest of the file, its docstrings among them, is read.
TOKEN_CLAIM_SPANS = {
    "tests/validation/workflows.py": (
        "# What every job's pinned actions do with the run's token",
        "# The parts of the repository token_claim_texts leaves out",
        "the claim list and the patterns that spell each claim, which quote the claims they refuse",
    ),
}
# The files besides the workflows where a retired claim was made, which token_claim_read_problems requires read.
TOKEN_CLAIM_REQUIRED = (
    "tests/run_validation.py",
    "tests/known_failure_issues.py",
    "tests/validation/credentials.py",
    "docs/implementing-changes.md",
    "CONTRIBUTING.md",
)
# How far either side of a mention of the token a claim can reach: each pattern's words stand within it, so
# token_claim_problems matches only the stretches of a text around a mention.
_CLAIM_REACH = 300
# A comment or quotation marker opening a line, which _claim_text drops so a claim wrapped across such lines reads
# whole.
_LINE_MARKER = re.compile(r"^\s*[#>]+")
# What _claim_text folds after NFKC: each hyphen and dash variant and the minus sign to a hyphen, the non-breaking
# hyphen among them, which NFKC makes U+2010; each typographic apostrophe to a straight one; and Markdown's backtick
# and asterisk markers, the soft hyphen, and the zero-width characters, which NFKC keeps, dropped.
_CLAIM_FOLD: dict[int, str | None] = {
    **dict.fromkeys((0x2010, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212), "-"),
    **dict.fromkeys((0x2018, 0x2019, 0x02BC, 0x2032), "'"),
    **dict.fromkeys((ord("`"), ord("*"), 0x00AD, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF), None),
}
# An underscore that opens or closes a word, as Markdown's emphasis writes one; `GITHUB_TOKEN`'s stays.
_EMPHASIS_UNDERSCORE = re.compile(r"(?<!\w)_+|_+(?!\w)")
# The row of docs/design.md that states what the token reaches, found by the words it opens with.
CREDENTIALS_ROW = "| Development and CI gates need no model and no credentials:"


def _job_comment(workflow: str, job: str) -> str:
    """The comment lines directly above a job's key in the workflow's text, with each `#` and indentation removed,
    joined by spaces, so a statement wrapped across them reads as one sentence; empty when the job has none."""
    lines = workflow.splitlines()
    key = next((index for index, line in enumerate(lines) if line.startswith(" ") and line.strip() == f"{job}:"), None)
    if key is None:
        return ""
    comment: list[str] = []
    for line in reversed(lines[:key]):
        if not line.strip().startswith("#"):
            break
        comment.insert(0, line.strip().removeprefix("#").strip())
    return " ".join(comment)


def _claim_text(text: str) -> str:
    """`text` as one case-folded line for RETIRED_TOKEN_CLAIMS: each line's opening `#` or `>` markers dropped; the
    text in Unicode's compatibility form, NFKC, so a no-break or thin space is a space; each _CLAIM_FOLD character
    replaced or dropped; an underscore that opens or closes a word dropped, as Markdown's emphasis writes it; and every
    run of whitespace, line breaks included, one space. A claim capitalized, emphasized, or wrapped across comment lines
    reads as one sentence."""
    joined = " ".join(_LINE_MARKER.sub("", line) for line in text.splitlines())
    folded = _EMPHASIS_UNDERSCORE.sub("", unicodedata.normalize("NFKC", joined).translate(_CLAIM_FOLD))
    return " ".join(folded.casefold().split())


def token_reach_problems(workflow: str, token_steps: Mapping[tuple[str, str], str], design: str) -> list[str]:
    """Report a token step, of a workflow that runs on pull requests, whose job runs code the pull request can change
    without docs/design.md's CREDENTIALS_ROW and the comment directly above the job each stating REACH_STATEMENTS in
    their words. A token job's checkout may take only NO_STORED_CREDENTIAL's input, so it checks out the event's own
    commit, the pull request's merge commit, into the workspace; any other input, such as `ref`, `repository`, `path`,
    or `sparse-checkout`, is a form this policy does not read, so a job that starts from another tree is described here
    before it is sanctioned."""
    tree = read_workflow(workflow)
    if "pull_request" not in _triggers(tree.get("on") if isinstance(tree, dict) else None) or not token_steps:
        return []
    rows = [line for line in design.splitlines() if line.startswith(CREDENTIALS_ROW)]
    problems: list[str] = []
    for job in sorted({job for job, _ in token_steps}):
        checkouts = [
            step
            for step in _steps(_mapping(tree, "jobs", job))
            if str(step.get("uses", "")).startswith("actions/checkout@")
        ]
        problems += [
            f"job {job}'s step {step.get('name')!r} checks out with `{key}`, a form the token-reach policy does not "
            "read; state what the token reaches from that tree before sanctioning it"
            for step in checkouts
            for key in sorted(set(_mapping(step, "with")) - set(NO_STORED_CREDENTIAL))
        ]
        if not checkouts:
            continue
        comment = _job_comment(workflow, job)
        for statement in REACH_STATEMENTS:
            if len(rows) != 1 or statement not in rows[0].casefold():
                problems.append(
                    f"job {job} runs the pull request's own code with the token, and docs/design.md's row "
                    f"{CREDENTIALS_ROW!r} does not say {statement!r}"
                )
            if statement not in comment.casefold():
                problems.append(
                    f"job {job} runs the pull request's own code with the token, and the comment directly above it "
                    f"does not say {statement!r}"
                )
    return problems


def token_claim_problems(texts: Mapping[str, str], design: str) -> list[str]:
    """Report docs/design.md's CREDENTIALS_ROW not stating ACTION_INPUT_STATEMENT, and each sentence, in `design` or in
    any of `texts`, a name mapped to a text such as a workflow or a module, that makes one of RETIRED_TOKEN_CLAIMS in
    any spelling the patterns read, folded by _claim_text."""
    rows = [line for line in design.splitlines() if line.startswith(CREDENTIALS_ROW)]
    problems = []
    if len(rows) != 1 or ACTION_INPUT_STATEMENT not in rows[0]:
        problems.append(f"docs/design.md's row {CREDENTIALS_ROW!r} does not say {ACTION_INPUT_STATEMENT!r}")
    for name, text in {**texts, "docs/design.md": design}.items():
        problems += [
            f"{name} says {match.group(0)!r}, the retired claim {claim!r}, which the pinned actions' default `token` "
            "input makes untrue"
            for window in _claim_windows(_claim_text(text))
            for claim, pattern in RETIRED_TOKEN_CLAIMS.items()
            for match in pattern.finditer(window)
        ]
    return problems


def _claim_windows(folded: str) -> list[str]:
    """The stretches of a folded text within _CLAIM_REACH of a mention of the token, each name of which says "token",
    with stretches that meet joined, so no match is found twice and none is cut."""
    spans: list[list[int]] = []
    for mention in re.finditer("token", folded):
        start, end = max(0, mention.start() - _CLAIM_REACH), mention.end() + _CLAIM_REACH
        if spans and start <= spans[-1][1]:
            spans[-1][1] = end
        else:
            spans.append([start, end])
    return [folded[start:end] for start, end in spans]


def token_claim_read_problems(texts: Mapping[str, str]) -> list[str]:
    """Each of the places a retired token claim was once made, every workflow WORKFLOW_GUARDS names among them, that
    `texts` lacks: a read that found nothing finds no claim, so it fails here rather than passing."""
    required = sorted({f"{WORKFLOWS_DIRECTORY}/{name}" for name in WORKFLOW_GUARDS} | set(TOKEN_CLAIM_REQUIRED))
    return [
        f"the token-claim policy read no {name}, where a retired claim was made"
        for name in required
        if name not in texts
    ]


def token_claim_texts(root: Path, files: Iterable[Path]) -> dict[str, str]:
    """Each of `files` under `root` that token_claim_problems reads, by its path from `root`, mapped to its text: every
    one but docs/design.md, which it is given apart, and those TOKEN_CLAIM_OMISSIONS names. A byte that is not UTF-8
    is read as a replacement character, so no file is passed over for its encoding."""
    texts = {}
    for path in files:
        name = path.relative_to(root).as_posix()
        if name != "docs/design.md" and not any(
            name == omitted or (omitted.endswith("/") and name.startswith(omitted)) for omitted in TOKEN_CLAIM_OMISSIONS
        ):
            texts[name] = _outside_claim_span(name, path.read_bytes().decode("utf-8", errors="replace"))
    return texts


def _outside_claim_span(name: str, text: str) -> str:
    """`text` with the lines of its TOKEN_CLAIM_SPANS entry blanked, from the line opening with its first marker to the
    line before the one opening with its second; the whole text when `name` has no entry or a marker is missing, so a
    moved marker reads the claims and fails rather than leaving the file unread."""
    if name not in TOKEN_CLAIM_SPANS:
        return text
    first, after = TOKEN_CLAIM_SPANS[name][:2]
    lines = text.split("\n")
    start = next((index for index, line in enumerate(lines) if line.startswith(first)), None)
    end = next((index for index, line in enumerate(lines) if line.startswith(after)), None)
    if start is None or end is None or end <= start:
        return text
    return "\n".join(lines[:start] + [""] * (end - start) + lines[end:])


def unknown_workflow_problems(directory: Path, known: Collection[str]) -> list[str]:
    """Every entry of the workflows directory the policy does not know by name, which GitHub may run unguarded, and
    every known workflow the directory lacks."""
    entries = {entry.name for entry in directory.iterdir()} if file_presence.is_directory(directory) else set()
    problems = [
        f"{WORKFLOWS_DIRECTORY}/{name} is not a workflow the policy knows; add it to WORKFLOW_GUARDS with its triggers,"
        " actions, and crons"
        for name in sorted(entries - set(known))
    ]
    problems += [f"{WORKFLOWS_DIRECTORY}/{name} is missing" for name in sorted(set(known) - entries)]
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
    problems.extend(
        f"the step {step.get('name')} does not run its leg's --shard from matrix.shard"
        for step in runs
        if _mapping(step, "env").get("SHARD") != "${{ matrix.shard }}" or "$env:SHARD" not in str(step.get("run"))
    )
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


# The Linux legs' name, shaped as the Windows legs' is, from which tools/record_job_seconds.py reads each Linux leg's
# shard index and count. Its Python is the matrix's, which setup-python installs, so the name says what runs.
LINUX_LEG_NAME = "Linux shard ${{ matrix.shard }}, Python ${{ matrix.python-version }}"
# The Linux legs' Python, as the Windows legs': the floor in deployer/tools.py on every run, and the latest stable
# release beside it on the weekly schedule only, so the Linux-only branches meet a new interpreter without it blocking
# a pull request through the required check.
LINUX_PYTHON = "${{ github.event_name == 'schedule' && fromJSON('[\"3.11\", \"3.x\"]') || fromJSON('[\"3.11\"]') }}"
# What each Linux leg's validation step runs, as the Windows legs' step does: its own shard, every suite outside a pull
# request, and a pull request's body and title as files written from the event payload.
LINUX_VALIDATION_LINES = (
    "$arguments = @('-B', 'tests/run_validation.py', '-v', '--shard', $env:SHARD)",
    "if ($env:GITHUB_EVENT_NAME -ne 'pull_request') { $arguments += '--full' }",
    "[System.IO.File]::WriteAllText($bodyFile, [string]$payload.pull_request.body)",
    "$arguments += @('--pr-body', $bodyFile)",
    "[System.IO.File]::WriteAllText($titleFile, [string]$payload.pull_request.title)",
    "$arguments += @('--pr-title', $titleFile)",
    "python @arguments",
)


# The Linux legs check ShellCheck's release archive against the workflow's pinned digest before extracting it.
LINUX_SHELLCHECK_DIGEST_CHECK = 'echo "$SHELLCHECK_LINUX_SHA256  $archive" | sha256sum --check --strict'


def linux_job_problems(workflow: str, legs: int) -> list[str]:
    """Report how validate.yml's `linux` job departs from one validation run on ubuntu-latest dealt across `legs` legs
    named as Linux legs, each running its own shard, every leg reporting rather than the first failure cancelling the
    rest: the checkout's whole history, the matrix's Pythons as LINUX_PYTHON gives them, the floor and, on the weekly
    schedule, the latest stable release beside it, each leg's own read by its name and installed by setup-python,
    with pip's cache keyed on requirements-dev.txt, ShellCheck from the pinned release's archive checked against its
    pinned digest, PSScriptAnalyzer at the workflow's pin, and the Windows legs' validation step.
    `required_check_problems` holds it inside the required check, `tool_cache_problems` its tool caches, and
    `pinned_tool_problems` its verify step."""
    tree = read_workflow(workflow)
    linux = _mapping(tree, "jobs", "linux")
    if not linux:
        return ["there is no linux job"]
    problems: list[str] = []
    if linux.get("runs-on") != "ubuntu-latest":
        problems.append(f"the linux job runs on {linux.get('runs-on')}, not ubuntu-latest")
    if linux.get("name") != LINUX_LEG_NAME:
        problems.append(f"the linux job's legs are named {linux.get('name')!r}, not {LINUX_LEG_NAME!r}")
    strategy = _mapping(linux, "strategy")
    expected = [f"{index}/{legs}" for index in range(1, legs + 1)]
    shards = _mapping(strategy, "matrix").get("shard")
    if shards != expected:
        problems.append(f"the linux matrix's shards are {shards}, expected {expected}")
    pythons = _mapping(strategy, "matrix").get("python-version")
    if pythons != LINUX_PYTHON:
        problems.append(f"the linux matrix's Python is {pythons}, expected {LINUX_PYTHON}")
    if strategy.get("fail-fast") != "false":
        problems.append("the linux matrix does not set fail-fast: false, so one red leg would cancel the other")
    steps = _steps(linux)
    checkouts = [_mapping(step, "with") for step in steps if str(step.get("uses", "")).startswith("actions/checkout@")]
    if checkouts != [{"fetch-depth": "0", **NO_STORED_CREDENTIAL}]:
        problems.append(
            f"the linux job checks out {checkouts}, not the whole history with fetch-depth 0 and no stored credential"
        )
    python = [_mapping(step, "with") for step in steps if str(step.get("uses", "")).startswith("actions/setup-python@")]
    setup = {
        "python-version": "${{ matrix.python-version }}",
        "cache": "pip",
        "cache-dependency-path": "requirements-dev.txt",
    }
    if python != [setup]:
        problems.append(
            f"the linux job sets up Python as {python}, not the matrix's with pip's cache on requirements-dev.txt"
        )
    scripts = [str(step.get("run", "")) for step in steps]
    problems.extend(
        f"no step of the linux job runs {install!r}"
        for install in (
            LINUX_SHELLCHECK_DIGEST_CHECK,
            "Install-Module PSScriptAnalyzer -RequiredVersion $env:PSSCRIPTANALYZER_VERSION ",
            "python -m pip install --require-hashes --only-binary :all: -r requirements-dev.txt",
        )
        if not any(install in script for script in scripts)
    )
    runs = [step for step in steps if "tests/run_validation.py" in str(step.get("run", ""))]
    if len(runs) != 1:
        problems.append(f"the linux job has {len(runs)} steps that run tests/run_validation.py, not one")
    for step in runs:
        script = str(step.get("run", ""))
        if step.get("shell") != "pwsh":
            problems.append(f"the step {step.get('name')} runs in {step.get('shell')}, not pwsh as the legs' does")
        if _mapping(step, "env").get("SHARD") != "${{ matrix.shard }}":
            problems.append(f"the step {step.get('name')} does not run its leg's --shard from matrix.shard")
        lines = {line.strip() for line in script.splitlines()}
        problems.extend(
            f"the step {step.get('name')} does not run {expected!r}"
            for expected in LINUX_VALIDATION_LINES
            if expected not in lines
        )
    return problems


def tool_cache_problems(workflow: str, caches: dict[str, str], job: str = "suite") -> list[str]:
    """Report how validate.yml's tool caches in `job` depart from `caches`: each `actions/cache` key, mapped to the
    install command that runs when it misses. Every version a key names is a workflow-level `env` pin, so the install
    and the key read one declaration, and pip's cache is keyed on requirements-dev.txt."""
    tree = read_workflow(workflow)
    steps = _steps(_mapping(tree, "jobs", job))
    keys = [
        str(_mapping(step, "with").get("key"))
        for step in steps
        if str(step.get("uses", "")).startswith("actions/cache@")
        and not str(_mapping(step, "with").get("key")).startswith(MYPY_CACHE_PREFIX)
    ]
    problems: list[str] = []
    if sorted(keys) != sorted(caches):
        problems.append(f"the cache keys are {keys}, expected {sorted(caches)}")
    pins = _mapping(tree, "env")
    scripts = "\n".join(str(step.get("run", "")) for step in steps)
    for key, command in caches.items():
        problems.extend(
            f"the cache key {key} reads {name}, which the workflow's env does not pin"
            for name in re.findall(r"\$\{\{ env\.(\w+) \}\}", key)
            if name not in pins
        )
        if command not in scripts:
            problems.append(f"no step runs {command!r} when the cache keyed {key} misses")
    python = [_mapping(step, "with") for step in steps if str(step.get("uses", "")).startswith("actions/setup-python@")]
    if [{"cache": item.get("cache"), "path": item.get("cache-dependency-path")} for item in python] != [
        {"cache": "pip", "path": "requirements-dev.txt"}
    ]:
        problems.append("setup-python does not cache pip keyed on requirements-dev.txt")
    return problems


# mypy's cache, which tool_cache_problems leaves to mypy_cache_problems: a folder the type checks write under, each in
# a folder of its own, restored for the leg, since each leg's deal runs its own type checks, on the runner's platform
# and the Python setup-python installed, which mypy writes its cache for, and requirements-dev.txt, which pins mypy. A
# key is saved once, so the standard library's stubs, most of what mypy parses, stay cached until one of them changes,
# while the repository's own modules are checked again as they change.
MYPY_CACHE_PREFIX = "mypy-"
MYPY_CACHE_KEY = (
    f"{MYPY_CACHE_PREFIX}${{{{ runner.os }}}}-${{{{ steps.python.outputs.python-version }}}}-"
    "${{ strategy.job-index }}-${{ hashFiles('requirements-dev.txt') }}"
)
MYPY_CACHE_PATH = "${{ runner.temp }}/mypy-cache"


def mypy_cache_problems(workflow: str, job: str) -> list[str]:
    """Report how validate.yml's `job` departs from restoring mypy's cache, keyed by MYPY_CACHE_KEY, before validation
    runs, and from naming its folder in MYPY_CACHE_VARIABLE to every step that runs validation."""
    steps = _steps(_mapping(read_workflow(workflow), "jobs", job))
    caches = [
        (index, _mapping(step, "with"))
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("actions/cache@")
        and str(_mapping(step, "with").get("key")).startswith(MYPY_CACHE_PREFIX)
    ]
    expected = {"path": MYPY_CACHE_PATH, "key": MYPY_CACHE_KEY}
    problems: list[str] = []
    if [settings for _, settings in caches] != [expected]:
        problems.append(
            f"the {job} job restores mypy's cache as {[settings for _, settings in caches]}, not {expected}"
        )
    elif caches[0][0] > _validation_index(steps):
        problems.append(f"the {job} job restores mypy's cache after validation runs")
    if [step.get("id") for step in steps if str(step.get("uses", "")).startswith("actions/setup-python@")] != [
        "python"
    ]:
        problems.append(f"the {job} job's setup-python step is not the step `python` the cache key reads")
    runs = [step for step in steps if "tests/run_validation.py" in str(step.get("run", ""))]
    problems.extend(
        f"the step {step.get('name')} does not name {MYPY_CACHE_PATH} in {MYPY_CACHE_VARIABLE}"
        for step in runs
        if _mapping(step, "env").get(MYPY_CACHE_VARIABLE) != MYPY_CACHE_PATH
    )
    return problems


# What each job's verify step runs before validation: the ShellCheck validation runs and the PSScriptAnalyzer it loads
# are the workflow's pins, or the step fails.
PINNED_TOOL_LINES = (
    "$shellcheck = [string]((shellcheck --version) -match '^version: ' -replace '^version: ')",
    "if ($shellcheck -ne $env:SHELLCHECK_VERSION) { throw \"ShellCheck is '$shellcheck', not the pin "
    '$env:SHELLCHECK_VERSION" }',
    "$analyzer = [string](Get-Module -ListAvailable PSScriptAnalyzer | Sort-Object Version -Descending | "
    "Select-Object -First 1).Version",
    "if ($analyzer -ne $env:PSSCRIPTANALYZER_VERSION) { throw \"PSScriptAnalyzer is '$analyzer', not the pin "
    '$env:PSSCRIPTANALYZER_VERSION" }',
)
# The verify step's loop over the tools, which starts at Python and prints the version each resolves to, so a leg whose
# matrix names `3.x` logs the release it ran.
RESOLVED_PYTHON_LOOP = "foreach ($command in @('python', "
RESOLVED_VERSION_LINE = "& $resolved.Source --version"
# The informational lookup of the gallery's latest PSScriptAnalyzer, which only the weekly run waits on.
GALLERY_LOOKUP = "Find-Module PSScriptAnalyzer"
SCHEDULE_ONLY = "github.event_name == 'schedule'"


def _validation_index(steps: list[dict[str, Node]]) -> int:
    """The index of the first step that runs tests/run_validation.py, or the step count when none does."""
    runs = [index for index, step in enumerate(steps) if "tests/run_validation.py" in str(step.get("run", ""))]
    return runs[0] if runs else len(steps)


def pinned_tool_problems(workflow: str, job: str) -> list[str]:
    """Report how validate.yml's `job` departs from verifying, in a pwsh step before validation runs, that its
    ShellCheck and newest PSScriptAnalyzer are the workflow's pins and printing the Python it resolves, and from
    querying the gallery for the latest PSScriptAnalyzer anywhere but in a step the weekly schedule alone runs."""
    steps = _steps(_mapping(read_workflow(workflow), "jobs", job))
    before = steps[: _validation_index(steps)]
    verifies = [
        step
        for step in before
        if step.get("shell") == "pwsh"
        and all(line in {raw.strip() for raw in str(step.get("run", "")).splitlines()} for line in PINNED_TOOL_LINES)
    ]
    problems: list[str] = []
    if not verifies:
        problems.append(f"no pwsh step of the {job} job verifies the pinned tools before validation runs")
    for step in verifies:
        lines = [raw.strip() for raw in str(step.get("run", "")).splitlines()]
        if not any(line.startswith(RESOLVED_PYTHON_LOOP) for line in lines) or RESOLVED_VERSION_LINE not in lines:
            problems.append(f"the step {step.get('name')} of the {job} job does not print the Python it resolves")
    problems.extend(
        f"the step {step.get('name')} of the {job} job queries the gallery on every event, not on the schedule only"
        for step in steps
        if GALLERY_LOOKUP in str(step.get("run", "")) and step.get("if") != SCHEDULE_ONLY
    )
    return problems


# The Windows legs' real-time protection reading. It read off on every leg, so no exclusion applies, and the reading
# costs seconds, so only the weekly run waits on it.
DEFENDER_LINES = (
    "$enabled = (Get-MpComputerStatus -ErrorAction Stop).RealTimeProtectionEnabled",
    'Write-Host "Defender real-time protection enabled: $enabled"',
)
DEFENDER_READ = "Get-MpComputerStatus"
DEFENDER_EXCLUSION = "Add-MpPreference"


def defender_problems(workflow: str) -> list[str]:
    """Report how validate.yml's Windows legs depart from printing Defender's real-time protection state in a pwsh
    step the weekly schedule alone runs, reading it in no other step, and adding no exclusion."""
    steps = _steps(_mapping(read_workflow(workflow), "jobs", "suite"))
    problems: list[str] = []
    if not any(
        step.get("shell") == "pwsh"
        and step.get("if") == SCHEDULE_ONLY
        and all(line in {raw.strip() for raw in str(step.get("run", "")).splitlines()} for line in DEFENDER_LINES)
        for step in steps
    ):
        problems.append("no pwsh step of the suite job prints Defender's real-time protection state on the schedule")
    problems.extend(
        f"the step {step.get('name')} of the suite job reads Defender's real-time protection on every event"
        for step in steps
        if DEFENDER_READ in str(step.get("run", "")) and step.get("if") != SCHEDULE_ONLY
    )
    problems.extend(
        f"the step {step.get('name')} of the suite job adds a Defender exclusion, which never applied"
        for step in steps
        if DEFENDER_EXCLUSION in str(step.get("run", ""))
    )
    return problems


# The Linux legs' reading of the image's PSScriptAnalyzer: the step of this id records whether the pinned version is
# installed, and the module's cache and its install run only when it is not.
IMAGE_MODULE_STEP = "psscriptanalyzer-image"
IMAGE_MODULE_LINES = (
    "$found = @(Get-Module -ListAvailable PSScriptAnalyzer | Where-Object { [string]$_.Version -eq "
    "$env:PSSCRIPTANALYZER_VERSION })",
    'Add-Content -LiteralPath $env:GITHUB_OUTPUT -Value "installed=$(([string]($found.Count -gt 0))'
    '.ToLowerInvariant())"',
)
NOT_ON_IMAGE = f"steps.{IMAGE_MODULE_STEP}.outputs.installed != 'true'"
MODULE_CACHE_KEY = "psscriptanalyzer-${{ runner.os }}-${{ env.PSSCRIPTANALYZER_VERSION }}"
MODULE_INSTALL = "Install-Module PSScriptAnalyzer -RequiredVersion $env:PSSCRIPTANALYZER_VERSION "


def image_module_problems(workflow: str, job: str) -> list[str]:
    """Report how validate.yml's `job` departs from restoring and installing PSScriptAnalyzer only when the image lacks
    the pin: one pwsh step with id IMAGE_MODULE_STEP records on every event, before validation runs, whether the
    pinned version is installed; the module's cache, after it, runs only when it is not; and each install, after it,
    only when it is not and the cache missed. `pinned_tool_problems` holds the verify step that checks the result
    either way, and `tool_cache_problems` the cache's key and install."""
    steps = _steps(_mapping(read_workflow(workflow), "jobs", job))
    probes = [index for index, step in enumerate(steps) if step.get("id") == IMAGE_MODULE_STEP]
    if len(probes) != 1:
        return [f"the {job} job has {len(probes)} steps with id {IMAGE_MODULE_STEP}, not one"]
    first = probes[0]
    probe = steps[first]
    problems: list[str] = []
    if first > _validation_index(steps):
        problems.append(f"the step {probe.get('name')} of the {job} job reads the image after validation runs")
    lines = {raw.strip() for raw in str(probe.get("run", "")).splitlines()}
    if probe.get("shell") != "pwsh" or "if" in probe or not all(line in lines for line in IMAGE_MODULE_LINES):
        problems.append(
            f"the step {probe.get('name')} of the {job} job does not record, on every event, whether the image has "
            "the pinned PSScriptAnalyzer"
        )
    caches = [
        (index, step) for index, step in enumerate(steps) if _mapping(step, "with").get("key") == MODULE_CACHE_KEY
    ]
    problems.extend(
        f"the step {step.get('name')} of the {job} job restores PSScriptAnalyzer's cache whether or not the image has "
        "the pin"
        for index, step in caches
        if index < first or step.get("if") != NOT_ON_IMAGE
    )
    hits = " && ".join(f"steps.{step.get('id')}.outputs.cache-hit != 'true'" for _, step in caches)
    expected = f"{NOT_ON_IMAGE} && {hits}" if hits else NOT_ON_IMAGE
    problems.extend(
        f"the step {step.get('name')} of the {job} job installs PSScriptAnalyzer whether or not the image has the pin, "
        f"or over a cache hit: its condition is {step.get('if')!r}, not {expected!r}"
        for index, step in enumerate(steps)
        if MODULE_INSTALL in str(step.get("run", "")) and (index < first or step.get("if") != expected)
    )
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
CODEX_SETTINGS_STEP = "Make the Codex sandbox settings and record the Python aliases"
WINDOWS_ONLY_DEPLOYABLE_STEPS = (CODEX_SETTINGS_STEP,)


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
    problems.extend(
        f"the deployable-linux job's {key} is not the deployable job's"
        for key in ("strategy", "env", "timeout-minutes")
        if linux.get(key) != windows.get(key)
    )
    expected = [
        str(step.get("name")) for step in _steps(windows) if step.get("name") not in WINDOWS_ONLY_DEPLOYABLE_STEPS
    ]
    found = [str(step.get("name")) for step in _steps(linux)]
    if found != expected:
        problems.append(f"the deployable-linux job's steps are {found}, expected {expected}")
    return problems


# The file docs/codex-support.md tells a user to write the settings to, ~/.codex/config.toml, as the parts the Codex
# step's $config is joined from: the runner's profile folder, then each folder and the file under it.
CODEX_CONFIG_PARTS = ("$env:USERPROFILE", ".codex", "config.toml")
ASSIGNMENT = re.compile(r"^\$(?P<name>\w+)\s*=\s*(?P<value>.*?)\s*$", re.MULTILINE)
JOIN_PATH = re.compile(r"Join-Path (?P<parent>\$[\w:]+) '(?P<child>[^']+)'")


def _joined_path(script: str, name: str) -> tuple[str, ...] | None:
    """The parts of the path the script's variable `name` holds, followed back through each
    `$name = Join-Path $parent '<child>'` to an environment variable, or None when a variable on the way is assigned
    otherwise, more than once, or in a cycle."""
    values: dict[str, list[str]] = {}
    for match in ASSIGNMENT.finditer(script):
        values.setdefault(match["name"], []).append(match["value"])
    parts: list[str] = []
    seen: set[str] = set()
    while name not in seen:
        seen.add(name)
        assigned = values.get(name, [])
        joined = JOIN_PATH.fullmatch(assigned[0]) if len(assigned) == 1 else None
        if joined is None:
            return None
        parts[:0] = joined["child"].split("/")
        if joined["parent"].startswith("$env:"):
            return (joined["parent"], *parts)
        name = joined["parent"][1:]
    return None


def _toml_problem(text: str | None, where: str) -> tuple[dict[str, object] | None, str | None]:
    """The TOML text as a table, or the problem that keeps it from being read."""
    if text is None:
        return None, f"{where} has no Codex settings block"
    try:
        return tomllib.loads(text), None
    except tomllib.TOMLDecodeError as error:
        return None, f"{where}'s Codex settings are not TOML: {error}"


def codex_settings_problems(workflow: str, guide: str) -> list[str]:
    """Hold the Codex settings deployable.yml's Windows job writes, the `$settings` here-string its Codex step passes
    to Set-Content, to the TOML block docs/codex-support.md gives after "For MXC with the elevated fallback", and its
    `$config` to ~/.codex/config.toml under the runner's profile, so the runner is configured as that page tells a user
    to configure a machine."""
    steps = [
        step
        for step in _steps(_mapping(read_workflow(workflow), "jobs", "deployable"))
        if step.get("name") == CODEX_SETTINGS_STEP
    ]
    if len(steps) != 1:
        return [f"the deployable job has {len(steps)} steps named {CODEX_SETTINGS_STEP!r}, expected one"]
    script = str(steps[0].get("run", ""))
    written = re.search(r"^\$settings = @'\n(?P<toml>.*?)\n'@$", script, re.MULTILINE | re.DOTALL)
    block = re.search(
        r"^For MXC with the elevated fallback[^\n]*\n\n```toml\n(?P<toml>.*?)\n```$", guide, re.MULTILINE | re.DOTALL
    )
    problems: list[str] = []
    if not re.search(r"^Set-Content -LiteralPath \$config -Value \$settings ", script, re.MULTILINE):
        problems.append(f"{CODEX_SETTINGS_STEP!r} does not write $settings to $config")
    if _joined_path(script, "config") != CODEX_CONFIG_PARTS:
        problems.append(f"{CODEX_SETTINGS_STEP!r} does not set $config to .codex/config.toml under $env:USERPROFILE")
    workflow_table, problem = _toml_problem(written.group("toml") if written else None, CODEX_SETTINGS_STEP)
    problems += [problem] if problem else []
    guide_table, problem = _toml_problem(block.group("toml") if block else None, "docs/codex-support.md")
    problems += [problem] if problem else []
    if workflow_table is not None and guide_table is not None and workflow_table != guide_table:
        problems.append(
            f"{CODEX_SETTINGS_STEP!r} writes {workflow_table}, but docs/codex-support.md gives {guide_table}"
        )
    return problems


# Each deployable job's Configure step pipes its answers into configure, one line per prompt, in the job's shell: the
# repositories directory, then an empty line for each later prompt, which keeps that variable's default.
CONFIGURE_STEP = "Configure"
CONFIGURE_PIPES = {
    "deployable": re.compile(r"(?P<answers>.+?)\s*\|\s*python deploy\.py configure(?:\s|$)"),
    "deployable-linux": re.compile(r"printf '%s\\n' (?P<answers>.+?)\s*\|\s*python3 deploy\.py configure(?:\s|$)"),
}
REPOSITORIES_ANSWER = "$repositories"


def _configure_answers(job: str, line: str) -> list[str] | None:
    """The answers a line pipes into configure, unquoted, or None when it pipes none in its job's form."""
    match = CONFIGURE_PIPES[job].match(line)
    if match is None:
        return None
    answers = match["answers"]
    if job == "deployable":
        # A PowerShell array of a variable and single-quoted strings.
        items = [item.strip() for item in answers.split(",")]
        return [item[1:-1] if len(item) >= 2 and item[0] == item[-1] == "'" else item for item in items]
    return shlex.split(answers)


def configure_answer_problems(workflow: str, prompts: Collection[str], defaulted: Collection[str]) -> list[str]:
    """Hold each deployable job's Configure step to configure's prompts, in order: one answer per prompt, the
    repositories directory for the first, and an empty line, which keeps the default, only for a variable that has one.

    configure takes its answers from standard input and stops at the first it is not given, so a prompt added without
    an answer here would fail the workflow on the runner rather than validation.
    """
    tree = read_workflow(workflow)
    problems: list[str] = []
    for job in CONFIGURE_PIPES:
        steps = [step for step in _steps(_mapping(tree, "jobs", job)) if step.get("name") == CONFIGURE_STEP]
        pipes = [line for step in steps for line in _script_lines(step) if " configure" in line]
        found = [_configure_answers(job, line) for line in pipes]
        if len(steps) != 1 or len(found) != 1 or found[0] is None:
            problems.append(f"{job}: expected one {CONFIGURE_STEP!r} step whose one line pipes answers into configure")
            continue
        answers = found[0]
        keys = list(prompts)
        if len(answers) != len(keys):
            problems.append(
                f"{job}: the {CONFIGURE_STEP!r} step pipes {len(answers)} answers, but configure asks for "
                f"{len(keys)}: {', '.join(keys)}"
            )
        if answers[:1] != [REPOSITORIES_ANSWER]:
            problems.append(f"{job}: the first answer, to {keys[0]}, must be {REPOSITORIES_ANSWER}")
        problems.extend(
            f"{job}: the {CONFIGURE_STEP!r} step answers {key} with an empty line, but {key} has no default"
            for key, answer in zip(keys[1:], answers[1:], strict=False)
            if answer == "" and key not in defaulted
        )
    return problems


def copilot_auto_update_problems(workflow: str) -> list[str]:
    """Report each deployable.yml job whose env does not set COPILOT_AUTO_UPDATE to false.

    Copilot CLI otherwise runs the newest package it has downloaded rather than the one the job installed, so a later
    step could run a version other than the one the job checked and the README's fresh-runner column records.
    """
    jobs = _mapping(read_workflow(workflow), "jobs")
    return [
        f"the {job} job's env does not set COPILOT_AUTO_UPDATE: 'false'"
        for job in ("deployable", "deployable-linux")
        if _mapping(jobs, job, "env").get("COPILOT_AUTO_UPDATE") != "false"
    ]


# What runtime-command-lines.yml's two jobs run, by job: the runner, and the check, whose facts go to the step summary
# and whose failure fails the job, after the latest release of every runtime is installed.
COMMAND_LINE_JOBS = {
    "command-lines": ("windows-latest", "python tools/runtime_command_lines.py --summary $env:GITHUB_STEP_SUMMARY"),
    "command-lines-linux": ("ubuntu-latest", 'python3 tools/runtime_command_lines.py --summary "$GITHUB_STEP_SUMMARY"'),
}
LATEST_RUNTIMES = (
    "npm install --global '@anthropic-ai/claude-code@latest' '@openai/codex@latest' '@github/copilot@latest'"
)


def command_line_job_problems(workflow: str) -> list[str]:
    """Report how runtime-command-lines.yml departs from its check: each job on its runner, with Copilot CLI's
    auto-update off, installing the latest release of every runtime and then running the check once."""
    jobs = _mapping(read_workflow(workflow), "jobs")
    problems = (
        [] if set(jobs) == set(COMMAND_LINE_JOBS) else [f"the jobs are {sorted(jobs)}, not {sorted(COMMAND_LINE_JOBS)}"]
    )
    for name, (runner, check) in COMMAND_LINE_JOBS.items():
        job = _mapping(jobs, name)
        if not job:
            continue
        if job.get("runs-on") != runner:
            problems.append(f"the {name} job runs on {job.get('runs-on')}, not {runner}")
        if _mapping(job, "env").get("COPILOT_AUTO_UPDATE") != "false":
            problems.append(f"the {name} job's env does not set COPILOT_AUTO_UPDATE: 'false'")
        scripts = [_script_lines(step) for step in _steps(job) if "run" in step]
        installs = [index for index, lines in enumerate(scripts) if LATEST_RUNTIMES in lines]
        checks = [index for index, lines in enumerate(scripts) if check in lines]
        if len(installs) != 1 or len(checks) != 1 or installs[0] > checks[0]:
            problems.append(f"the {name} job must run {LATEST_RUNTIMES!r} once and then {check!r} once")
    return problems


# What runtime-changelogs.yml's one job runs: the watch, on Ubuntu, whose findings go to the step summary and whose
# own failure alone fails the job. It installs no runtime, since it reads changelogs and pages, not programs.
CHANGELOG_JOB = ("changelogs", "ubuntu-latest", CHANGELOG_WATCH)


def changelog_job_problems(workflow: str) -> list[str]:
    """Report how runtime-changelogs.yml departs from its watch: one job on its runner, running the watch once as the
    last line of its step, so its exit status is the step's, installing no runtime, and never told to go on after an
    error."""
    name, runner, watch = CHANGELOG_JOB
    jobs = _mapping(read_workflow(workflow), "jobs")
    if set(jobs) != {name}:
        return [f"the jobs are {sorted(jobs)}, not [{name!r}]"]
    job = _mapping(jobs, name)
    problems = [] if job.get("runs-on") == runner else [f"the {name} job runs on {job.get('runs-on')}, not {runner}"]
    steps = _steps(job)
    scripts = [_script_lines(step) for step in steps if "run" in step]
    if [lines[-1] for lines in scripts if watch in lines] != [watch] or sum(
        lines.count(watch) for lines in scripts
    ) != 1:
        problems.append(f"the {name} job must run {watch!r} once, as the last line of its step")
    if any("npm install" in line for lines in scripts for line in lines):
        problems.append(f"the {name} job installs a runtime, which the watch never starts")
    if "continue-on-error" in job or any("continue-on-error" in step for step in steps):
        problems.append(f"the {name} job goes on after an error, so a watch that cannot run would not fail it")
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


# The uninstall check in each deployable job, in its own shell: the step before the uninstall checks the first
# deployment's file layout, which refuses a profile with nothing deployed, the uninstall previews and then makes the
# removal with deploy.py's --uninstall, saving each log, and the step after it names what is left, files included,
# through tools/deployable_report.py, which tests/tools/test_deployable_report.py covers. A listing the step wrote for
# itself looked only at directories, so a shared file left behind passed, and an answer piped into the selection prompt
# relied on its wording. Every path the deployment owns is an entry of a directory the report lists after the
# uninstall, so nothing is saved before it.
FIRST_LAYOUT_STEP = "Check the Claude Code file layout after the first deployment"
UNINSTALL_STEP = "Uninstall"
CONFIRM_UNINSTALL_STEP = "Confirm the profile holds nothing from the suite"
UNINSTALL_CHECKS = {
    "deployable": (
        [
            "python tools/deployable_report.py layout --label 'first deploy' --results $env:RESULTS",
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        ],
        [
            "python deploy.py --uninstall --dry-run 2>&1 | Tee-Object -FilePath "
            "(Join-Path $env:LOGS 'uninstall-dry-run.log')",
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
            "python deploy.py --uninstall 2>&1 | Tee-Object -FilePath (Join-Path $env:LOGS 'uninstall.log')",
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        ],
        [
            "python tools/deployable_report.py leftovers --results $env:RESULTS",
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
        ],
    ),
    "deployable-linux": (
        ["python3 tools/deployable_report.py layout --label 'first deploy' --results \"$RESULTS\""],
        [
            'python3 deploy.py --uninstall --dry-run 2>&1 | tee "$LOGS/uninstall-dry-run.log"',
            'python3 deploy.py --uninstall 2>&1 | tee "$LOGS/uninstall.log"',
        ],
        ['python3 tools/deployable_report.py leftovers --results "$RESULTS"'],
    ),
}


def _script_lines(step: dict[str, Node]) -> list[str]:
    """A step's run script as its lines, stripped, without blank lines and comments."""
    lines = [line.strip() for line in str(step.get("run", "")).splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def uninstall_check_problems(workflow: str) -> list[str]:
    """Hold each deployable job's uninstall, run by --uninstall, between the first deployment's layout check and the
    step that names what the uninstall left, each running exactly its commands and nothing else."""
    tree = read_workflow(workflow)
    problems: list[str] = []
    for job, (layout, uninstall, confirm) in UNINSTALL_CHECKS.items():
        steps = _steps(_mapping(tree, "jobs", job))
        names = [step.get("name") for step in steps]
        if names.count(UNINSTALL_STEP) != 1:
            problems.append(f"{job}: expected one {UNINSTALL_STEP!r} step, found {names.count(UNINSTALL_STEP)}")
            continue
        index = names.index(UNINSTALL_STEP)
        for position, where, name, lines in (
            (index - 1, "before", FIRST_LAYOUT_STEP, layout),
            (index, "as", UNINSTALL_STEP, uninstall),
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


# What mutation.yml's jobs run. mutmut refuses Windows, so the select job, on Linux, installs the lock, which holds the
# development dependencies and the mutation tools, with hash checking and wheels only, restores the newest verdict a
# finished run of the pull request cached under its ref, makes the mutants, carrying that verdict when the gate finds
# nothing against it, and hands them on as an artifact, with step outputs listing the shards each leg's mutants are
# dealt into. Each leg runs one job for each of its shards, none stopping another, and checks out what its platform's
# validation job does, since a suite run unmutated there reads main's history and tags, installs the same lock, and
# ShellCheck at validate.yml's pin, which the deployer's suites run: the Linux leg from the release's archive checked
# against its pinned digest, the Windows leg through Chocolatey, each refusing another version on PATH. Each shard runs
# its share of its platform's leg of the mutants, never mutmut, within its own cap (DEFAULT_MINUTES in
# tools/mutation_gate.py), which sits well inside the job's time limit, and hands on its outcomes under its shard's
# name. The join, named as the gate and never as the required check, runs whatever the legs did, reads the files, the
# standard library, and git alone, fails an unfinished run, and caches a finished run's verdict under the key its
# `verdict` output names.
MUTATION_INSTALL = "python -m pip install --require-hashes --only-binary :all: -r requirements-mutation.txt"
MUTATION_GATE = "python -B tools/mutation_gate.py"
# The pins mutation.yml's env repeats from validate.yml's, since a workflow cannot read another's.
MUTATION_PINS = ("SHELLCHECK_VERSION", "SHELLCHECK_LINUX_SHA256")
MUTATION_LINUX_SHELLCHECK = [
    'shellcheck="$RUNNER_TEMP/shellcheck"',
    'release="https://github.com/koalaman/shellcheck/releases/download/v$SHELLCHECK_VERSION"',
    'archive="$RUNNER_TEMP/shellcheck.tar.xz"',
    'curl --fail --silent --show-error --location --retry 3 --output "$archive" '
    '"$release/shellcheck-v$SHELLCHECK_VERSION.linux.x86_64.tar.xz"',
    LINUX_SHELLCHECK_DIGEST_CHECK,
    'mkdir -p "$shellcheck"',
    'tar --extract --xz --file "$archive" --directory "$shellcheck" --strip-components 1 '
    '"shellcheck-v$SHELLCHECK_VERSION/shellcheck"',
    'echo "$shellcheck" >> "$GITHUB_PATH"',
]
MUTATION_WINDOWS_SHELLCHECK = [
    "choco install shellcheck --version $env:SHELLCHECK_VERSION --yes --no-progress",
    "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
]
# The verify step's ShellCheck lines, which the validation legs' verify step runs too.
MUTATION_SHELLCHECK_VERIFY = list(PINNED_TOOL_LINES[:2])
MUTATION_LOCKED_PYTHON = {
    "python-version": "3.11",
    "cache": "pip",
    "cache-dependency-path": "requirements-mutation.txt",
}
MUTATION_MUTANTS = {"name": "mutation-mutants", "path": "mutation"}
MUTATION_OUTPUTS = {leg: f"${{{{ steps.select.outputs.{leg} }}}}" for leg in ("linux", "windows")}


MUTATION_SHARD = "${{ matrix.shard }}"
MUTATION_VERDICT_PATH = "mutation/verdict"
# The order of a job's steps: each action by its name after actions/, and each script as `run`.
MUTATION_LEG_ORDER = ["checkout", "setup-python", "run", "run", "run", "download-artifact", "run", "upload-artifact"]


def _mutation_leg(platform: str, runner: str, shellcheck: tuple[str, list[str]]) -> dict[str, object]:
    """The job of one leg: after the select job, only when it dealt the leg a shard, one job for each shard it lists,
    none cancelling another, on the platform's runner, installing ShellCheck in the shell `shellcheck` names and
    verifying it in pwsh before the lock, and running and handing on its own shard."""
    leg = platform.casefold()
    shell, install = shellcheck
    return {
        "name": f"Mutation gate on {platform}",
        "needs": "select",
        "if": f"needs.select.outputs.{leg} != '[]'",
        "strategy": {"fail-fast": "false", "matrix": {"shard": f"${{{{ fromJSON(needs.select.outputs.{leg}) }}}}"}},
        "runs-on": runner,
        "timeout-minutes": "60",
        "checkout": {"fetch-depth": "0", **NO_STORED_CREDENTIAL},
        "python": MUTATION_LOCKED_PYTHON,
        "scripts": [
            install,
            MUTATION_SHELLCHECK_VERIFY,
            [MUTATION_INSTALL],
            [f"{MUTATION_GATE} leg mutation --shard {MUTATION_SHARD}"],
        ],
        "shells": [shell, "pwsh", None, None],
        "ids": [],
        "order": MUTATION_LEG_ORDER,
        "artifacts": [
            ["download-artifact", MUTATION_MUTANTS],
            [
                "upload-artifact",
                {
                    "name": f"mutation-{leg}-{MUTATION_SHARD}",
                    "path": f"mutation/outcomes-{leg}-{MUTATION_SHARD}.json",
                    "if-no-files-found": "error",
                },
            ],
        ],
        "caches": [],
    }


# Each job of mutation.yml, in order, with what mutation_job_problems holds it to.
MUTATION_JOBS: dict[str, dict[str, object]] = {
    "select": {
        "name": "Mutation gate mutants",
        "needs": None,
        "if": None,
        "runs-on": "ubuntu-latest",
        "strategy": None,
        "timeout-minutes": "15",
        "checkout": {"fetch-depth": "0", **NO_STORED_CREDENTIAL},
        "python": MUTATION_LOCKED_PYTHON,
        "scripts": [[MUTATION_INSTALL], [f"{MUTATION_GATE} select mutation"]],
        "shells": [None, None],
        "ids": ["select"],
        # The verdict is restored before the select step reads it.
        "order": ["checkout", "setup-python", "run", "cache/restore", "run", "upload-artifact"],
        "artifacts": [
            [
                "upload-artifact",
                {"name": "mutation-mutants", "path": "mutation/mutants.json", "if-no-files-found": "error"},
            ]
        ],
        "caches": [
            [
                "cache/restore",
                {
                    "path": MUTATION_VERDICT_PATH,
                    "key": "mutation-verdict-${{ github.ref }}",
                    "restore-keys": "mutation-verdict-${{ github.ref }}-",
                },
                None,
            ]
        ],
    },
    "linux": _mutation_leg("Linux", "ubuntu-latest", ("bash", MUTATION_LINUX_SHELLCHECK)),
    "windows": _mutation_leg("Windows", "windows-latest", ("pwsh", MUTATION_WINDOWS_SHELLCHECK)),
    "mutation": {
        "name": "Mutation gate",
        "needs": ["select", "linux", "windows"],
        "if": "${{ !cancelled() }}",
        "strategy": None,
        "runs-on": "ubuntu-latest",
        "timeout-minutes": "15",
        "checkout": NO_STORED_CREDENTIAL,
        "python": {"python-version": "3.11"},
        "scripts": [[f"{MUTATION_GATE} join mutation"]],
        "shells": [None],
        "ids": ["join"],
        # The verdict is saved after the join writes it, and only when the join names its key.
        "order": ["checkout", "setup-python", "download-artifact", "run", "cache/save"],
        "artifacts": [["download-artifact", {"pattern": "mutation-*", "path": "mutation", "merge-multiple": "true"}]],
        "caches": [
            [
                "cache/save",
                {"path": MUTATION_VERDICT_PATH, "key": "${{ steps.join.outputs.verdict }}"},
                "steps.join.outputs.verdict != ''",
            ]
        ],
    },
}


def _uses(step: dict[str, Node], action: str) -> bool:
    return str(step.get("uses", "")).startswith(f"actions/{action}@")


def _mutation_job_shape(job: dict[str, Node]) -> dict[str, object]:
    """A job of mutation.yml read into the fields MUTATION_JOBS gives each: its matrix, its checkout's and Python's
    inputs, its scripts, the shell each names, and the ids they take, the order of its steps, each artifact step with
    its inputs, and each cache step with its inputs and condition."""
    steps = _steps(job)
    checkouts: list[object] = [_mapping(step, "with") for step in steps if _uses(step, "checkout")]
    pythons: list[object] = [_mapping(step, "with") for step in steps if _uses(step, "setup-python")]
    artifacts: list[object] = [
        [action, _mapping(step, "with")]
        for step in steps
        for action in ("upload-artifact", "download-artifact")
        if _uses(step, action)
    ]
    caches: list[object] = [
        [action, _mapping(step, "with"), step.get("if")]
        for step in steps
        for action in ("cache/restore", "cache/save")
        if _uses(step, action)
    ]
    return {
        **{key: job.get(key) for key in ("name", "needs", "if", "strategy", "runs-on", "timeout-minutes")},
        "checkout": checkouts[0] if len(checkouts) == 1 else checkouts,
        "python": pythons[0] if len(pythons) == 1 else pythons,
        "scripts": [_script_lines(step) for step in steps if "run" in step],
        "shells": [step.get("shell") for step in steps if "run" in step],
        "ids": [step.get("id") for step in steps if "id" in step],
        "order": [
            "run" if "run" in step else str(step.get("uses", "")).split("@")[0].removeprefix("actions/")
            for step in steps
        ],
        "artifacts": artifacts,
        "caches": caches,
    }


def mutation_job_problems(workflow: str) -> list[str]:
    """Report how mutation.yml departs from the mutation gate: run on a pull request's opening, pushes, and reopening
    but not an edit of its text, as the jobs MUTATION_JOBS gives, in its order, each with its name, none the required
    check's, its dependencies and condition, matrix, runner, time limit, checkout, Python, scripts, step ids, the order
    of its steps, and its artifact and cache steps, and the select job's outputs listing each leg's shards."""
    tree = read_workflow(workflow)
    problems: list[str] = []
    types = _mapping(tree, "on", "pull_request").get("types")
    if types != ["opened", "synchronize", "reopened"]:
        problems.append(f"the gate runs on pull request events {types}, not opened, synchronize, and reopened")
    jobs = _mapping(tree, "jobs")
    if list(jobs) != list(MUTATION_JOBS):
        return [*problems, f"the workflow's jobs are {list(jobs)}, not {list(MUTATION_JOBS)}"]
    for name, expected in MUTATION_JOBS.items():
        found = _mutation_job_shape(_mapping(jobs, name))
        problems += [
            f"the {name} job's {key} is {found[key]!r}, not {value!r}"
            for key, value in expected.items()
            if found[key] != value
        ]
    outputs = _mapping(jobs, "select").get("outputs")
    if outputs != MUTATION_OUTPUTS:
        problems.append(f"the select job's outputs are {outputs}, not {MUTATION_OUTPUTS}")
    return problems


def mutation_pin_problems(workflow: str, validate: str) -> list[str]:
    """Report how mutation.yml's env departs from holding exactly MUTATION_PINS, each at validate.yml's value, so the
    legs install and verify the ShellCheck the validation legs do."""
    pins = _mapping(read_workflow(workflow), "env")
    reviewed = _mapping(read_workflow(validate), "env")
    problems = (
        [f"the workflow's env is {sorted(pins)}, not {sorted(MUTATION_PINS)}"]
        if set(pins) != set(MUTATION_PINS)
        else []
    )
    problems += [
        f"the workflow's {name} is {pins.get(name)!r}, not validate.yml's {reviewed.get(name)!r}"
        for name in MUTATION_PINS
        if name in pins and pins.get(name) != reviewed.get(name)
    ]
    return problems


# Each mutation leg, by its job, and the job of validate.yml that runs the suites on the same platform.
MUTATION_LEG_VALIDATION_JOBS = {"linux": "linux", "windows": "suite"}


def _checkout_inputs(tree: Node, job: str) -> list[dict[str, Node]]:
    return [_mapping(step, "with") for step in _steps(_mapping(tree, "jobs", job)) if _uses(step, "checkout")]


def mutation_checkout_problems(workflow: str, validate: str) -> list[str]:
    """Report each mutation leg whose checkout's inputs differ from those of the validate.yml job that runs the suites
    on its platform, so a suite the leg runs unmutated reads the history, branches, and tags it passes with there."""
    legs = read_workflow(workflow)
    reviewed = read_workflow(validate)
    return [
        f"the {leg} job's checkout is {found!r}, not validate.yml's {job} job's {expected!r}"
        for leg, job in MUTATION_LEG_VALIDATION_JOBS.items()
        if (found := _checkout_inputs(legs, leg)) != (expected := _checkout_inputs(reviewed, job))
    ]


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

    def test_deployable_workflow_writes_the_codex_settings_the_codex_guide_gives(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/deployable.yml").read_text(encoding="utf-8")
        guide = (REPOSITORY_ROOT / "docs/codex-support.md").read_text(encoding="utf-8")
        self.assertEqual([], codex_settings_problems(workflow, guide))

    def test_deployable_workflow_answers_every_prompt_configure_declares_in_both_jobs(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/deployable.yml").read_text(encoding="utf-8")
        self.assertEqual([], configure_answer_problems(workflow, config.PROMPTS, config.DEFAULTS))

    def test_deployable_workflow_keeps_copilot_on_the_version_it_installed_in_both_jobs(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/deployable.yml").read_text(encoding="utf-8")
        self.assertEqual([], copilot_auto_update_problems(workflow))

    def test_every_workflow_file_is_known_pinned_read_only_and_runs_on_its_own_events(self) -> None:
        workflows = REPOSITORY_ROOT / WORKFLOWS_DIRECTORY
        self.assertEqual([], unknown_workflow_problems(workflows, WORKFLOW_GUARDS))
        for name, (triggers, actions, crons) in WORKFLOW_GUARDS.items():
            with self.subTest(workflow=name):
                workflow = (workflows / name).read_text(encoding="utf-8")
                tokens = TOKEN_STEPS.get(name, {})
                permissions = TOKEN_STEP_PERMISSIONS.get(name, TOKEN_JOB_PERMISSIONS)
                self.assertEqual([], workflow_guard_problems(workflow, triggers, actions, crons, tokens, permissions))

    def test_a_token_step_that_runs_the_pull_requests_own_code_is_described_as_doing_so(self) -> None:
        design = (REPOSITORY_ROOT / "docs" / "design.md").read_text(encoding="utf-8")
        for name in WORKFLOW_GUARDS:
            with self.subTest(workflow=name):
                workflow = (REPOSITORY_ROOT / WORKFLOWS_DIRECTORY / name).read_text(encoding="utf-8")
                self.assertEqual([], token_reach_problems(workflow, TOKEN_STEPS.get(name, {}), design))

    def test_no_document_says_a_job_holds_no_token_while_its_actions_take_it_by_default(self) -> None:
        design = (REPOSITORY_ROOT / "docs" / "design.md").read_text(encoding="utf-8")
        texts = token_claim_texts(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT))
        self.assertEqual([], token_claim_read_problems(texts))
        self.assertEqual([], token_claim_problems(texts, design))

    def test_deployable_and_mutation_workflows_share_validates_action_pins(self) -> None:
        workflows = REPOSITORY_ROOT / WORKFLOWS_DIRECTORY
        reviewed = (workflows / "validate.yml").read_text(encoding="utf-8")
        for name in sorted(set(WORKFLOW_GUARDS) - {"validate.yml"}):
            with self.subTest(workflow=name):
                workflow = (workflows / name).read_text(encoding="utf-8")
                self.assertEqual([], shared_pin_problems(reviewed, workflow))

    def test_the_dependency_document_names_every_workflow_that_shares_validates_action_pins(self) -> None:
        document = (REPOSITORY_ROOT / "docs" / "dependency-updates.md").read_text(encoding="utf-8")
        paragraph = next(part for part in document.split("\n\n") if "pin their actions the same way" in part)
        named = re.findall(r"`\.github/workflows/([^`]+)`", paragraph)
        self.assertEqual(sorted(set(WORKFLOW_GUARDS) - {"validate.yml"}), sorted(named))

    def test_runtime_command_lines_workflow_checks_the_latest_runtimes_on_both_platforms(self) -> None:
        workflow = (REPOSITORY_ROOT / WORKFLOWS_DIRECTORY / "runtime-command-lines.yml").read_text(encoding="utf-8")
        self.assertEqual([], command_line_job_problems(workflow))

    def test_runtime_changelogs_workflow_runs_the_watch_whose_own_failure_alone_fails_it(self) -> None:
        workflow = (REPOSITORY_ROOT / WORKFLOWS_DIRECTORY / "runtime-changelogs.yml").read_text(encoding="utf-8")
        self.assertEqual([], changelog_job_problems(workflow))

    def test_mutation_workflow_runs_the_gate_on_pull_requests_outside_the_required_check(self) -> None:
        workflow = (REPOSITORY_ROOT / WORKFLOWS_DIRECTORY / "mutation.yml").read_text(encoding="utf-8")
        self.assertEqual([], mutation_job_problems(workflow))
        validate = (REPOSITORY_ROOT / WORKFLOWS_DIRECTORY / "validate.yml").read_text(encoding="utf-8")
        self.assertEqual([], mutation_pin_problems(workflow, validate))
        self.assertEqual([], mutation_checkout_problems(workflow, validate))

    def test_validate_workflow_reads_the_pull_requests_body_and_title_from_the_event_payload(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
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

    def test_validate_workflow_runs_six_legs_and_restores_the_pinned_tools_from_caches(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        # Keep the leg count here and in the workflow synchronized.
        self.assertEqual([], leg_problems(workflow, 6))
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
        self.assertEqual([], pinned_tool_problems(workflow, "suite"))
        self.assertEqual([], mypy_cache_problems(workflow, "suite"))
        # Real-time protection read off on every leg, so only the weekly run prints it, and nothing is excluded.
        self.assertEqual([], defender_problems(workflow))

    def test_validate_workflow_runs_the_whole_run_on_linux_inside_the_required_check(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        # Keep the Linux leg count here and in the workflow synchronized.
        self.assertEqual([], linux_job_problems(workflow, 2))
        # The Linux legs restore the same pinned tools as the Windows legs, and install them from the network only on
        # a miss: ShellCheck from its release archive, so the Linux pin is exact.
        self.assertEqual(
            [],
            tool_cache_problems(
                workflow,
                {
                    "shellcheck-${{ runner.os }}-${{ env.SHELLCHECK_VERSION }}": LINUX_SHELLCHECK_DIGEST_CHECK,
                    "psscriptanalyzer-${{ runner.os }}-${{ env.PSSCRIPTANALYZER_VERSION }}": (
                        "Install-Module PSScriptAnalyzer -RequiredVersion $env:PSSCRIPTANALYZER_VERSION "
                    ),
                },
                job="linux",
            ),
        )
        self.assertEqual([], pinned_tool_problems(workflow, "linux"))
        self.assertEqual([], mypy_cache_problems(workflow, "linux"))
        # The image ships the pinned analyzer, so the cache and the install run only on an image that lacks it.
        self.assertEqual([], image_module_problems(workflow, "linux"))
        # Every supported platform gates a merge through the one check branch protection requires.
        self.assertEqual([], required_check_problems(workflow, ("suite", "linux", "known-failures")))

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
