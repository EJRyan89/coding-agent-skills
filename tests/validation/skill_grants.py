"""Grant and description policies: allowed-tools grants, descriptions a runtime adapter accepts, and the
.agents/skills shims of repository skills.
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

import frontmatter
from skill_layout import embedded_program_problems, output_placeholder_problems
from skill_roots import sibling_scripts
from skill_scripts import RESULTS_DOC, command_reader
from validation_support import (
    REPOSITORY_ROOT,
    REPOSITORY_SKILLS,
    UnreadablePython,
    import_aliases,
    parse_python,
    python_trees,
    qualified_name,
    skills_by_name,
)

from deployer import render

# The audit-skill inventory owns the grant rules, so the audit and this policy cannot disagree. It stays in that
# skill rather than ejryan89-skill-core because it is the audit program the deployed skill runs, and the rules are its
# subject; the path is set once here, at import, so no policy changes sys.path while another policy module is importing.
sys.path.insert(0, str(sibling_scripts("audit-skill")))
import skill_inventory
from path_containment import within

GRANTS_DOC = '"Granting tools" in docs/adding-a-skill.md'
# A fence command that runs the skill's own scripts, which its grants must cover: a shipped skill names them through
# its directory, and a repository skill runs the repository's tools and tests from the working tree.
OWN_SCRIPT_COMMAND = re.compile(r"\$\{CLAUDE_SKILL_DIR\}")
REPOSITORY_TOOL_COMMAND = re.compile(r"^python (?:-B )?(?:tools|tests)/")
# A repository tool a grant runs, and the option that points the tool at another directory than the checkout it runs
# from: a grant whose * covers it may only when the tool parses it with the check that refuses all but a checkout.
GRANTED_TOOL = re.compile(r"^python (?:-B )?(tools/[\w-]+\.py)(?: |$)")
ROOT_OPTION = "--root"
CHECKOUT_PARSER = "tools.checkout.checkout_root"
# The option a repository tool grants a Claude Code session tools with, and the text of one whole grant: a tool's
# name, and the pattern its parentheses hold.
ALLOWED_TOOLS_OPTION = "--allowedTools"
GRANT_TEXT = re.compile(r"[A-Za-z_][\w-]*(?:\(.*\))?", re.DOTALL)


def shipped_skill_files(root: Path) -> list[Path]:
    """Each shipped skill's SKILL.md: a skill directory with deploy-meta."""
    skills = skills_by_name(root)
    found = (skills[metadata.stem] for metadata in (root / "deploy-meta").glob("*.json") if metadata.stem in skills)
    return sorted((directory / "SKILL.md" for directory in found), key=lambda path: path.parent.name)


def repository_skill_files(root: Path) -> list[Path]:
    """Each repository skill's SKILL.md under .claude/skills, which the shipped-skill policies hold too."""
    return sorted((root / REPOSITORY_SKILLS).glob("*/SKILL.md"), key=lambda path: path.parent.name)


def skill_grant_problems(
    root: Path, skill_files: list[Path] | None = None, own: re.Pattern[str] = OWN_SCRIPT_COMMAND
) -> list[str]:
    """Report shell grants that cover every command or one shell only, a sibling's folder rather than its scripts,
    a path a * continues, such as the skill's own scripts/*, rather than a script by name, or a program with no
    script, such as python *, grants no step uses, own-script commands left ungranted, grants that pre-approve a
    script that starts code from the target repository, shell patterns that match no command the skill shows, and any
    grant in a hidden skill, which no turn starts. A grant counts as used only where a step names its tool: prose that
    only implies it, such as "read the file" for Read, names an action a script may take as well as the agent. A
    shell grant is used by the commands its pattern matches, which the idle check holds. A command is read from every
    fence Markdown allows and from each code span outside one that runs a script.

    The rules are the audit-skill inventory's, so the audit and this policy cannot disagree. A shipped skill's
    own scripts run through ${CLAUDE_SKILL_DIR}; a repository skill's are the repository's tools and tests.
    """
    problems: list[str] = []
    for skill_md in shipped_skill_files(root) if skill_files is None else skill_files:
        name = skill_md.relative_to(root).as_posix()
        found = skill_inventory.tools(skill_md)
        for line in found:
            kind, _, detail = line.partition(" ")
            if kind == "HIDDEN_ALLOWED":
                problems.append(
                    f"{name} is hidden, so no turn starts it and its allowed-tools approve nothing; see {GRANTS_DOC}"
                )
            elif kind == "IDLE_ALLOWED":
                problems.append(
                    f"{name} grants {detail}, which matches no command in its fences or code spans; see {GRANTS_DOC}"
                )
            elif (problem := pattern_problem(name, kind, detail)) is not None:
                problems.append(problem)
            elif kind == "UNUSED_ALLOWED":
                problems.append(f"{name} grants {detail}, which no step uses; see {GRANTS_DOC}")
            elif kind == "IMPLIED" and (tool_line := detail.split(" "))[0] not in skill_inventory.SHELL_TOOLS:
                tool, number = tool_line
                problems.append(
                    f"{name}:{number} implies {tool}, which it grants, but no step names it; name the tool in the "
                    f"step that uses it, or drop the grant; see {GRANTS_DOC}"
                )
            elif kind == "MISSING_ALLOWED" and detail.split(" ")[0] == "Bash":
                problems.append(f"{name}:{detail.split(' ')[1]} runs a command without a shell grant; see {GRANTS_DOC}")
            elif kind == "UNGRANTED" and own.search(detail.split(" ", 2)[2]):
                tool, number, command = detail.split(" ", 2)
                problems.append(f"{name}:{number} no {tool} grant covers {command}; see {GRANTS_DOC}")
            elif kind == "GRANTED_REPOSITORY_CODE":
                tool, number, command = detail.split(" ", 2)
                problems.append(
                    f"{name}:{number} a {tool} grant pre-approves {command}, which runs code from the target "
                    f"repository; see {GRANTS_DOC}"
                )
            elif kind == "EXPANDS":
                number, command = detail.split(" ", 1)
                problems.append(
                    f"{name}:{number} expands a shell variable, so it always prompts: {command}; see {GRANTS_DOC}"
                )
    return problems


def status_line_problems(root: Path, skill_files: list[Path] | None = None) -> list[str]:
    """Report each status line a skill's Markdown and its scripts disagree on: a line a script the skill runs prints
    whose first word the Markdown never names, a status word the Markdown names as output that none of its scripts
    prints, and an exit code it states that none of them uses.

    The comparison is the audit-skill inventory's status_lines, so the audit and this policy cannot disagree. A line
    the agent need not act on is named as such in the Markdown, which is how a reader learns it needs nothing.
    """
    tops = (root, root.resolve())

    def shown(text: str) -> str:
        """A path the inventory printed, from the repository root when it lies under the root as given or resolved."""
        path = Path(text)
        return next((path.relative_to(top).as_posix() for top in tops if within(path, top)), text)

    problems: list[str] = []
    files = [*shipped_skill_files(root), *repository_skill_files(root)] if skill_files is None else skill_files
    for skill_md in files:
        for line in skill_inventory.status_lines(skill_md.parent):
            kind, _, detail = line.partition(" ")
            path, number, word = detail.rsplit(" ", 2)
            where = f"{shown(path)}:{number}"
            if kind == "UNEXPLAINED":
                problems.append(
                    f"{where} prints a line starting {word}, which {shown(skill_md.as_posix())} never names; name it "
                    f"where the agent acts on it, or say why the agent need not; see {RESULTS_DOC}"
                )
            elif kind == "UNPRINTED":
                problems.append(
                    f"{where} names {word} as output, which no script the skill reads prints; see {RESULTS_DOC}"
                )
            elif kind == "EXIT_MISMATCH":
                problems.append(
                    f"{where} states exit code {word}, which no script the skill reads exits with; see {RESULTS_DOC}"
                )
    return problems


def pattern_problem(name: str, kind: str, detail: str) -> str | None:
    """The problem an inventory line reports about one grant's own pattern, whoever grants it, or None for a line of
    another kind."""
    if kind == "UNSCOPED_ALLOWED":
        return f"{name} grants {detail} for every command; see {GRANTS_DOC}"
    if kind == "UNPAIRED_ALLOWED":
        return f"{name} grants {detail} in one shell only; see {GRANTS_DOC}"
    if kind == "SIBLING_FOLDER_ALLOWED":
        return f"{name} grants {detail}, a sibling skill's folder rather than one script by name; see {GRANTS_DOC}"
    if kind == "OPEN_PATH_ALLOWED":
        return (
            f"{name} grants {detail}, whose * continues a path, so it reaches every skill's scripts through "
            f"..; grant each script by name; see {GRANTS_DOC}"
        )
    if kind == "NO_SCRIPT_ALLOWED":
        return (
            f"{name} grants {detail}, whose text before its * names no script or git or gh subcommand, so it "
            f"pre-approves any code the * supplies; grant each script by name; see {GRANTS_DOC}"
        )
    return None


def _strings(node: ast.AST) -> list[ast.Constant | ast.JoinedStr]:
    """Each string a node holds, an f-string whole rather than its parts."""
    if isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return [node]
    return [string for child in ast.iter_child_nodes(node) for string in _strings(child)]


def _text(node: ast.AST) -> str | None:
    """A string's text, each value an f-string fills in read as its name in braces, or None for anything else."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if not isinstance(node, ast.JoinedStr):
        return None
    parts = []
    for part in node.values:
        if isinstance(part, ast.FormattedValue):
            value = part.value
            parts.append(f"{{{ast.unparse(value) if isinstance(value, ast.Name | ast.Attribute) else 'value'}}}")
        else:
            parts.append(str(part.value) if isinstance(part, ast.Constant) else "")
    return "".join(parts)


def _function_grants(function: ast.FunctionDef) -> list[str] | None:
    """The grants a module function builds, each string in its body but its docstring, or None when one is not a
    whole grant, so the policy cannot read what it returns."""
    body = function.body[1:] if ast.get_docstring(function) is not None else function.body
    texts = [_text(string) for statement in body for string in _strings(statement)]
    grants = [text for text in texts if text is not None and GRANT_TEXT.fullmatch(text)]
    return grants if grants and len(grants) == len(texts) else None


def _passed_grants(values: list[ast.expr], functions: dict[str, ast.FunctionDef]) -> tuple[list[str], list[str]]:
    """The grants the values after --allowedTools pass, up to the next option, and the source of each value this
    policy cannot read as grants: a string or f-string that is one whole grant, or a starred call of a module function
    each string of which is one."""
    grants: list[str] = []
    unreadable: list[str] = []
    for value in values:
        text = _text(value)
        if text is not None and text.startswith("--"):
            break
        if text is not None and GRANT_TEXT.fullmatch(text):
            grants.append(text)
            continue
        called = value.value if isinstance(value, ast.Starred) else None
        if isinstance(called, ast.Call) and isinstance(called.func, ast.Name) and called.func.id in functions:
            built = _function_grants(functions[called.func.id])
            if built is not None:
                grants += built
                continue
        unreadable.append(ast.unparse(value))
    return grants, unreadable


def tool_grant_problems(root: Path) -> list[str]:
    """Report each grant a tools/ module passes to a Claude Code session with --allowedTools that the skill grant
    rules refuse, and each value after the option this policy cannot read as grants.

    A session started with these grants runs them without asking, as a skill's allowed-tools do, so they are held to
    the same rules: each script by its path, never a * that continues one, in both shells. Each value is a string or
    an f-string that is one whole grant, or a starred call of a function the module defines, each string of which,
    but its docstring, is one; a value an f-string fills in is read as its name, so the module fills in no text that
    holds a *.
    """
    problems: list[str] = []
    for _, name, tree in python_trees(sorted((root / "tools").rglob("*.py")), root, problems):
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        for display in ast.walk(tree):
            if not isinstance(display, ast.List | ast.Tuple):
                continue
            for index, item in enumerate(display.elts):
                if _text(item) != ALLOWED_TOOLS_OPTION:
                    continue
                grants, unreadable = _passed_grants(display.elts[index + 1 :], functions)
                problems += [
                    f"{name}:{item.lineno} passes {source} to {ALLOWED_TOOLS_OPTION}, which this policy cannot read "
                    f"as grants; write each grant as a string or an f-string, or return them from a function of the "
                    f"module; see {GRANTS_DOC}"
                    for source in unreadable
                ]
                for line in skill_inventory.grant_findings(skill_inventory.allowed_entries(grants), []):
                    kind, _, detail = line.partition(" ")
                    problem = pattern_problem(f"{name}:{item.lineno}", kind, detail)
                    if problem is not None:
                        problems.append(problem)
    return problems


def root_option_problems(root: Path, skill_files: list[Path]) -> list[str]:
    """Report each grant whose * covers a tools/ script's --root option that the script does not parse with
    tools/checkout.py's checkout_root, so the grant would pre-approve the tool writing into any directory, the
    deployer's skills directories included."""
    problems: list[str] = []
    for skill_md in skill_files:
        name = skill_md.relative_to(root).as_posix()
        found = frontmatter.split(skill_md.read_text(encoding="utf-8").splitlines())
        document = frontmatter.Frontmatter(found[0] if found else [])
        for tool, pattern in skill_inventory.allowed_entries(document.value("allowed-tools")):
            match = GRANTED_TOOL.match(pattern or "")
            if pattern is None or "*" not in pattern or match is None or not (root / match[1]).is_file():
                continue
            if unbounded_root_option(root / match[1]):
                problems.append(
                    f"{name} grants {tool}({pattern}), whose * covers {match[1]} {ROOT_OPTION}, which it does not "
                    f"parse with {CHECKOUT_PARSER}; bound the option or grant the forms the skill runs; "
                    f"see {GRANTS_DOC}"
                )
    return problems


def unbounded_root_option(script: Path) -> bool:
    """Whether a script declares the --root option with a type other than the checkout check, or with none, or cannot
    be read to tell."""
    try:
        tree = parse_python(script)
    except UnreadablePython:
        return True
    aliases = import_aliases(tree)
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and any(isinstance(argument, ast.Constant) and argument.value == ROOT_OPTION for argument in node.args)
        ):
            continue
        parser = next((keyword.value for keyword in node.keywords if keyword.arg == "type"), None)
        if parser is None or qualified_name(parser, aliases) != CHECKOUT_PARSER:
            return True
    return False


def repository_code_declaration_problems(root: Path) -> list[str]:
    """Report each RUNS_REPOSITORY_CODE declaration the inventory would not read, and each in a script that starts
    no process.

    The inventory leaves a command ungranted only for a script whose module-level declaration is a non-empty string,
    so a declaration of another shape would fail silently, and one in a script that starts nothing is stale.
    """
    declaration = skill_inventory.REPOSITORY_CODE_DECLARATION
    problems: list[str] = []
    scripts = sorted([*(root / "skills").rglob("*.py"), *(root / REPOSITORY_SKILLS).rglob("*.py")])
    for script, name, tree in python_trees(scripts, root, problems):
        assigned = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign | ast.AnnAssign)
            and any(
                isinstance(target, ast.Name) and target.id == declaration
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            )
        ]
        if not assigned:
            continue
        if len(assigned) != 1 or assigned[0] not in tree.body or not skill_inventory.runs_repository_code(script):
            problems.append(f"{name}: {declaration} must be one module-level assignment of a non-empty reason")
        reader = command_reader(script)
        problems += reader.problems(name)
        if not reader.starts_process():
            problems.append(f"{name}: declares {declaration} but starts no process; remove the declaration")
    return problems


def repository_skill_problems(root: Path) -> list[str]:
    """Report each repository skill under .claude/skills whose .agents/skills shim is missing or differs from what
    tools/skill_shims.py writes, each file in a shim folder the tool does not write, and each stray shim.

    Codex and Copilot CLI read .agents/skills, so a shim must carry the skill's own frontmatter, which they read before
    choosing it, point to the skill as the authoritative workflow, and, for a skill only the user starts, hold the
    agents/openai.yaml policy Codex reads instead of the frontmatter; the tool is the one writer of all three.
    """
    from tools import skill_shims

    return skill_shims.problems(root)


def description_problems(root: Path, skill_files: list[Path]) -> list[str]:
    """Report descriptions a runtime adapter would refuse, and model-invocable ones that never say when to start.

    The rules are the renderer's and the skill reference's, which hold every shipped skill as it deploys.
    """
    from deployer.errors import DeployError
    from tools import skill_reference

    problems: list[str] = []
    for skill_md in skill_files:
        name = skill_md.relative_to(root).as_posix()
        try:
            description, _, user_only = render._adapter_frontmatter(skill_md.parent.name, skill_md.read_bytes())
        except DeployError as exc:
            problems.append(f"{name}: {exc.lines[0].removeprefix('ERROR: ')}")
            continue
        if not user_only and not skill_reference.says_when(description):
            problems.append(f"{name}: the model may start it, so its description must {skill_reference.WHEN_RULE}")
    return problems


class SkillGrantsPolicies(unittest.TestCase):
    def test_skill_reference_matches_the_skills(self) -> None:
        # docs/skills.md is generated from each skill's frontmatter and metadata, so a skill change that does not
        # rerun tools/skill_reference.py --write leaves the reference describing a skill that no longer exists.
        from tools import skill_reference

        self.assertEqual([], skill_reference.problems(REPOSITORY_ROOT))

    def test_runtime_prompt_samples_match_what_the_skills_render(self) -> None:
        # audit-skill measures the prompt a skill's script writes for a subagent from its rendered sample, so a
        # prompt change that does not rerun tools/runtime_prompts.py --write has the audit measure a prompt no run sees.
        from tools import runtime_prompts

        self.assertEqual([], runtime_prompts.problems(REPOSITORY_ROOT))

    def test_repository_skills_have_matching_shims(self) -> None:
        self.assertEqual([], repository_skill_problems(REPOSITORY_ROOT))

    def test_repository_skills_meet_the_shipped_skill_policies(self) -> None:
        # A repository skill loads into every session here as a shipped one does, so the same rules hold it.
        skills = repository_skill_files(REPOSITORY_ROOT)
        self.assertTrue(skills)
        self.assertEqual([], embedded_program_problems(REPOSITORY_ROOT, REPOSITORY_SKILLS))
        self.assertEqual([], output_placeholder_problems(REPOSITORY_ROOT, REPOSITORY_SKILLS))
        self.assertEqual([], skill_grant_problems(REPOSITORY_ROOT, skills, REPOSITORY_TOOL_COMMAND))
        self.assertEqual([], description_problems(REPOSITORY_ROOT, skills))

    def test_repository_tool_grants_cover_no_root_option_outside_a_checkout(self) -> None:
        # A tool's --root redirects every write it makes, so a grant whose * covers it must find the tool refusing a
        # directory that is not a checkout of this repository, such as a home whose .agents/skills the deployer owns.
        self.assertEqual([], root_option_problems(REPOSITORY_ROOT, repository_skill_files(REPOSITORY_ROOT)))

    def test_repository_tools_grant_their_sessions_by_the_skill_grant_rules(self) -> None:
        # A tool that starts Claude Code with --allowedTools pre-approves those commands for the whole session, so a
        # * that continues a script's path there matches any file named like it, as it would in a skill's grants.
        self.assertEqual([], tool_grant_problems(REPOSITORY_ROOT))

    def test_skills_grant_only_scoped_twin_shell_patterns_that_cover_their_own_scripts(self) -> None:
        # allowed-tools pre-approves; a bare Bash would pre-approve every command in the turn that starts the skill.
        self.assertEqual([], skill_grant_problems(REPOSITORY_ROOT))

    def test_skill_markdown_names_every_status_line_its_scripts_print(self) -> None:
        # The agent acts on a script's lines through its Markdown, so a line the Markdown never names is one the
        # agent meets unexplained, and a word or exit code it names that no script gives is one it waits for in vain.
        self.assertEqual([], status_line_problems(REPOSITORY_ROOT))

    def test_repository_code_declarations_are_read_and_current(self) -> None:
        # A script that starts code the target repository configures declares it, so the grant policy above can
        # hold its command ungranted; a declaration the inventory cannot read would let a grant cover it unnoticed.
        self.assertEqual([], repository_code_declaration_problems(REPOSITORY_ROOT))
