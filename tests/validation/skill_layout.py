"""Layout policies: where a skill's files live, how long its fences are, how it names its own and its siblings'
files, its metadata and variables, its script languages, and the fixture sources beside it.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

from skill_roots import sibling_scripts
from validation_support import (
    CODE_SPAN,
    EXECUTABLE_SCRIPT_EXTENSIONS,
    REPOSITORY_ROOT,
    REPOSITORY_SKILLS,
    SCRIPT_INTERPRETERS,
    SHELL_FENCES,
    SIBLING_LOOKUP,
    SKILL_GUIDE,
    TEMPLATE_TOKEN,
    UNSUPPORTED_SCRIPT_EXTENSIONS,
    all_skill_directories,
    fence_body_line,
    fence_holders,
    find_fences,
    is_executable_script,
    is_test_script,
    path_order,
    python_trees,
    scripts_entries,
    scripts_put_on_path,
    skill_directories,
    skills_by_name,
)

from deployer import platform_support

# The analyze-skill-cost inventory owns the labels it reads as command shells, the grant checks among its readers, so
# a fence the grant checks read as commands is one the five-line and output-placeholder rules read too. The path is set
# once here, at import, as skill_grants sets it.
sys.path.insert(0, str(sibling_scripts("analyze-skill-cost")))
import skill_inventory

MAXIMUM_INLINE_EXECUTABLE_LINES = 5


# Every label a fence of commands or a program may carry: the command shells and PowerShell the inventory reads, and
# Python, JavaScript, and TypeScript under each name Markdown gives them.
EXECUTABLE_FENCE_LANGUAGES = frozenset(
    {
        *skill_inventory.SHELL_FENCES,
        *skill_inventory.POWERSHELL_FENCES,
        *("python", "python3", "py"),
        *("javascript", "js", "node", "mjs", "cjs"),
        *("typescript", "ts"),
    }
)


@dataclass(frozen=True)
class ExecutableFence:
    line_number: int
    language: str
    body_line_count: int


def find_executable_fences(lines: list[str]) -> list[ExecutableFence]:
    """Each fence in an executable language, with its body's length: up to its closer, or, for one left open, to the
    end of what holds it, as the renderer reads it; unclosed_fence_problems reports one left open."""
    return [
        ExecutableFence(fence.opener + 1, fence.language, len(fence.body()))
        for fence in find_fences(lines)
        if fence.language.casefold() in EXECUTABLE_FENCE_LANGUAGES
    ]


def embedded_program_problems(root: Path, tree: str = "skills") -> list[str]:
    """Report executable fences in a skill tree's Markdown that are programs rather than short command examples."""
    problems: list[str] = []
    for markdown in sorted((root / tree).rglob("*.md"), key=lambda path: path.relative_to(root).as_posix()):
        name = markdown.relative_to(root).as_posix()
        for fence in find_executable_fences(markdown.read_text(encoding="utf-8").splitlines()):
            if fence.body_line_count > MAXIMUM_INLINE_EXECUTABLE_LINES:
                problems.append(
                    f"{name}:{fence.line_number} contains a {fence.language} fence with {fence.body_line_count} "
                    f"lines. Markdown may contain only short command examples of at most "
                    f"{MAXIMUM_INLINE_EXECUTABLE_LINES} lines; move executable logic to the skill's scripts/ directory."
                )
    return problems


def _template_tokens(directory: Path) -> set[str]:
    tokens: set[str] = set()
    for path in directory.rglob("*"):
        if path.is_file():
            tokens.update(TEMPLATE_TOKEN.findall(path.read_text(encoding="utf-8", errors="replace")))
    return tokens


def deploy_variable_problems(
    root: Path,
    configured: set[str],
    prompted: set[str],
    derived: set[str],
) -> list[str]:
    """Report configured variables and template tokens that are not used consistently."""
    problems: list[str] = []
    for key in sorted(configured - prompted):
        problems.append(f"configure never prompts for configured variable {key}")
    for key in sorted(prompted - configured):
        problems.append(f"configure prompts for unknown variable {key}")

    required_anywhere: set[str] = set()
    used_anywhere: set[str] = set()
    skills = skills_by_name(root)
    for metadata in sorted((root / "deploy-meta").glob("*.json"), key=path_order):
        skill = metadata.stem
        required = set(json.loads(metadata.read_text(encoding="utf-8")).get("required_vars", []))
        required_anywhere |= required
        directory = skills.get(skill)
        used = _template_tokens(directory) if directory is not None else set()
        used_anywhere |= used
        for key in sorted(used - required):
            problems.append(f"skill {skill} uses {{{{{key}}}}} without declaring it in required_vars")
        for key in sorted(required - used):
            problems.append(f"skill {skill} declares required variable {key} but never uses it")
        for key in sorted(required - configured - derived):
            problems.append(f"skill {skill} requires unknown variable {key}")
    for key in sorted(configured - required_anywhere):
        problems.append(f"configured variable {key} is not required by any skill")

    shared = json.loads((root / "source.json").read_text(encoding="utf-8")).get("shared_assets", {})
    for asset in sorted(shared):
        path = root / "skills" / asset
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        tokens = set(TEMPLATE_TOKEN.findall(text))
        used_anywhere |= tokens
        for key in sorted(tokens - derived):
            problems.append(f"shared asset {asset} uses non-derived variable {key}")
    # A derived value no template uses still has to pass the allowlist, so it could refuse a deployment for nothing.
    for key in sorted(derived - used_anywhere):
        problems.append(f"derived variable {key} is not used by any skill or shared asset")
    return problems


METADATA_DOC = '"Metadata" in docs/adding-a-skill.md'


def metadata_format_problems(root: Path) -> list[str]:
    """Report deploy-meta files not laid out as tools/new_skill.py writes them, so every file reads the same way."""
    from tools import new_skill

    problems: list[str] = []
    for metadata in sorted((root / "deploy-meta").glob("*.json")):
        text = metadata.read_bytes().decode("utf-8")
        if text != new_skill.metadata_text(json.loads(text)):
            problems.append(f"deploy-meta/{metadata.name} is not in the canonical metadata format; see {METADATA_DOC}")
    return problems


INVOKED_SKILL = re.compile(r"`([a-z0-9-]+)[` ]")


def derived_needs(root: Path, name: str, seen: frozenset[str] = frozenset()) -> set[str]:
    """The runtime capabilities a skill's frontmatter shows it needs, with those of each skill it invokes.

    An invoked skill is a backticked skill name on a SKILL.md line that says "invoke", the rule
    tests/ai-config/test_cross_skill_contracts.py applies; its limits reach the step that invokes it.
    """
    import frontmatter

    from deployer import runtime_support

    skills = skills_by_name(root)
    if name not in skills:
        return set()
    skill_md = skills[name] / "SKILL.md"
    document = frontmatter.read(skill_md)
    granted = document.value("allowed-tools") or []
    user_only = (document.string("disable-model-invocation") or "").casefold() == "true"
    needs = runtime_support.frontmatter_needs(granted if isinstance(granted, list) else [granted], user_only)
    invoked = {
        match
        for line in skill_md.read_text(encoding="utf-8-sig").splitlines()
        if "invoke" in line
        for match in INVOKED_SKILL.findall(line)
        if match != name and match not in seen and match in skills
    }
    for other in sorted(invoked):
        # Starting an invoked skill is the invoker's step, so only what the invoked skill does carries over.
        needs |= derived_needs(root, other, seen | {name}) - {"user-only-start"}
    return needs


def runtime_support_problems(root: Path) -> list[str]:
    """Report deploy-meta files whose runtime support is missing or does not follow from what the skill needs.

    A selectable skill is full on a runtime that offers every capability it needs (derived_needs), and otherwise
    partial or none, naming exactly the capabilities that runtime lacks. A hidden skill is none everywhere, since no
    runtime starts it.
    """
    from deployer import runtime_support

    problems: list[str] = []
    for metadata in sorted((root / "deploy-meta").glob("*.json")):
        name = f"deploy-meta/{metadata.name}"
        document = json.loads(metadata.read_text(encoding="utf-8"))
        if "runtime_support" not in document:
            problems.append(f"{name} declares no runtime_support; see {METADATA_DOC}")
            continue
        try:
            declared = runtime_support.parse(document["runtime_support"])
        except runtime_support.RuntimeSupportError as exc:
            problems.append(f"{name}: {exc}; see {METADATA_DOC}")
            continue
        if document.get("selectable", True) is False:
            problems += [
                f"{name} declares {runtime} {support.level}, but a hidden skill is none on every runtime"
                for runtime, support in declared.items()
                if support.level != runtime_support.NONE
            ]
            continue
        needs = derived_needs(root, metadata.stem)
        for runtime, support in declared.items():
            lacking = runtime_support.lacking(runtime, needs)
            if not lacking and support.level != runtime_support.FULL:
                problems.append(
                    f"{name} declares {runtime} {support.level}, but {runtime} offers everything the skill needs, "
                    f"so it is full; see {METADATA_DOC}"
                )
            elif lacking and support.level == runtime_support.FULL:
                problems.append(
                    f"{name} declares {runtime} full, but {runtime} lacks {', '.join(lacking)}, "
                    f"so it is partial or none; see {METADATA_DOC}"
                )
            elif lacking and tuple(sorted(support.needs)) != tuple(sorted(lacking)):
                problems.append(
                    f"{name} declares {runtime} needs {', '.join(support.needs) or 'nothing'}, "
                    f"but {runtime} lacks {', '.join(lacking)}; see {METADATA_DOC}"
                )
    return problems


INSTALL_PATH = re.compile(r"\{\{HOME\}\}/\.claude/skills/([A-Za-z0-9._-]+)")
SIBLING_PATH = re.compile(r"\$\{CLAUDE_SKILL_DIR\}/\.\./([A-Za-z0-9._-]+)")
# A concrete file a skill names through its directory; a pattern such as scripts/* in a grant names none.
SKILL_DIR_FILE = re.compile(r"\$\{CLAUDE_SKILL_DIR\}/([A-Za-z0-9._/-]+)(?![A-Za-z0-9._/*<-])")
BARE_SCRIPT_PATH = re.compile(r"""(?:^|[\s"'=])(?:\./|\.\./[A-Za-z0-9._-]+/)?scripts/""")
# In SKILL.md prose, a code span that names the skill's own scripts/ or references/ by a relative path. Claude Code
# resolves it against the working directory, and Codex and Copilot against the adapter, which has neither folder.
# A code span is read with skill-core's CODE_SPAN, which validation_support re-exports.
BARE_OWN_PATH = re.compile(r"""(?:^|[\s"'=(])(?:\./|\.\./[A-Za-z0-9._-]+/)?(?:scripts|references)/""")
# analyze-skill-cost recommends a command under `scripts/` in the skill it audits, which is not a path of its own.
BARE_OWN_PATH_EXEMPT = frozenset({("skills/analyze-skill-cost/SKILL.md", "scripts/")})
SKILL_PATHS_DOC = '"Paths to a skill\'s own files" in docs/adding-a-skill.md'
# Git Bash takes $HOME from HOME, which need not be the profile folder the deployer installs into.
HOME_VARIABLE = re.compile(r"\$(?:HOME\b|\{HOME\}|env:HOME\b)", re.IGNORECASE)
AGENTS_DOC = '"Subagent definitions" in docs/adding-a-skill.md'


def skill_path_problems(root: Path) -> list[str]:
    """Report skills and agents that reach a skill's files other than through ${CLAUDE_SKILL_DIR}."""
    skills = {path.parent.name for path in (root / "skills").glob("*/SKILL.md")}
    problems: list[str] = []
    for path, dependencies, directory in _path_documents(root):
        name = path.relative_to(root).as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        holders = fence_holders(lines)
        for number, line in enumerate(lines, start=1):
            holder = holders[number - 1]
            if holder is not None and not fence_body_line(holder, number - 1):
                continue  # an opening or closing line
            in_fence = holder is not None
            in_shell_fence = holder is not None and holder.language.casefold() in SHELL_FENCES
            if path.name == "SKILL.md" and directory is not None and not in_fence:
                problems += _prose_path_problems(name, number, line)
            problems += _line_path_problems(name, number, line, in_shell_fence, skills, dependencies, directory)
    return problems


def _path_documents(root: Path) -> list[tuple[Path, set[str] | None, Path | None]]:
    """Each agent, then each skill's Markdown with the skills it declares and its directory."""
    documents: list[tuple[Path, set[str] | None, Path | None]] = [
        (path, None, None) for path in sorted((root / "agents").glob("*.md"), key=path_order)
    ]
    skills = skills_by_name(root)
    for metadata in sorted((root / "deploy-meta").glob("*.json")):
        declared: set[str] = set(json.loads(metadata.read_text(encoding="utf-8")).get("skill_deps", []))
        if (skill_directory := skills.get(metadata.stem)) is not None:
            documents += [
                (path, declared, skill_directory) for path in sorted(skill_directory.rglob("*.md"), key=path_order)
            ]
    return documents


def _prose_path_problems(name: str, number: int, line: str) -> list[str]:
    """Code spans in SKILL.md prose that name the skill's own scripts/ or references/ by a relative path."""
    return [
        f"{name}:{number} names `{span}` by a bare relative path; see {SKILL_PATHS_DOC}"
        for span in CODE_SPAN.findall(line)
        if BARE_OWN_PATH.search(span) and (name, span) not in BARE_OWN_PATH_EXEMPT
    ]


def _line_path_problems(
    name: str,
    number: int,
    line: str,
    in_shell_fence: bool,
    skills: set[str],
    dependencies: set[str] | None,
    directory: Path | None,
) -> list[str]:
    """Install paths, bare script runs, $HOME in agents, undeclared siblings, and missing files, in that order."""
    problems: list[str] = []
    for match in INSTALL_PATH.finditer(line):
        if match.group(1) in skills:
            problems.append(f"{name}:{number} names skill {match.group(1)} by its install path; see {SKILL_PATHS_DOC}")
    if in_shell_fence and BARE_SCRIPT_PATH.search(line):
        problems.append(f"{name}:{number} runs a script by a bare relative path; see {SKILL_PATHS_DOC}")
    if dependencies is None and HOME_VARIABLE.search(line):
        problems.append(f"{name}:{number} finds a file through $HOME; see {AGENTS_DOC}")
    for match in SIBLING_PATH.finditer(line):
        if dependencies is not None and match.group(1) not in dependencies:
            problems.append(f"{name}:{number} reaches ../{match.group(1)} without declaring it in skill_deps")
    for match in SKILL_DIR_FILE.finditer(line) if directory is not None else ():
        named = match.group(1).rstrip(".")  # a path may end a sentence
        if directory is not None and not (directory / named).exists():
            problems.append(f"{name}:{number} names ${{CLAUDE_SKILL_DIR}}/{named}, which does not exist")
    return problems


# An option that names where a script writes, given a placeholder for the agent to fill in: "--output <file>",
# "--plans=<dir>". Placeholders for paths a command printed, such as "--run <run directory>", name no output option.
OUTPUT_PLACEHOLDER = re.compile(r"""(--(?:output(?:-[a-z]+)*|out(?:-dir)?|plans))(?:\s+|=)["']?<[^>]*>""")
WORKING_FILES_DOC = '"Working files" in docs/adding-a-skill.md'


def output_placeholder_problems(root: Path, tree: str = "skills") -> list[str]:
    """Report command fences in a skill tree's Markdown that leave the agent to choose where a script writes.

    Given a placeholder and a skill directory it already knows, an agent writes beside SKILL.md, and the deployer
    then sees the installed skill as modified and stops updating it.
    """
    problems: list[str] = []
    for path in sorted((root / tree).rglob("*.md"), key=lambda path: path.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, (line, holder) in enumerate(zip(lines, fence_holders(lines), strict=True), start=1):
            in_command_fence = (
                fence_body_line(holder, number - 1)
                and holder is not None
                and holder.language.casefold() in EXECUTABLE_FENCE_LANGUAGES
            )
            if in_command_fence:
                for match in OUTPUT_PLACEHOLDER.finditer(line):
                    problems.append(
                        f"{name}:{number} leaves {match.group(1)} to the agent; let the script choose "
                        f"and print the path; see {WORKING_FILES_DOC}"
                    )
    return problems


def script_dependency_problems(root: Path) -> list[str]:
    """Report a skill script that puts another skill's scripts on sys.path without declaring it in skill_deps.

    The deployer installs a skill's skill_deps closure beside it and nothing else, so an undeclared sibling is missing
    wherever the skill is deployed without it, and the import fails at run time.
    """
    problems: list[str] = []
    skills = skills_by_name(root)
    for metadata in sorted((root / "deploy-meta").glob("*.json")):
        skill = metadata.stem
        declared = set(json.loads(metadata.read_text(encoding="utf-8")).get("skill_deps", []))
        scripts = sorted((skills[skill] / "scripts").glob("*.py")) if skill in skills else []
        for path, _, tree in python_trees([path for path in scripts if not is_test_script(path)], root, problems):
            for name in sorted(set(scripts_put_on_path(tree)) - declared - {skill}):
                problems.append(
                    f"{path.relative_to(root).as_posix()} puts {name}'s scripts on sys.path without declaring "
                    f"{name} in skill_deps; see {SKILL_PATHS_DOC}"
                )
    return problems


# The skill whose scripts every module reaches by its own fixed sys.path line, which skill_roots needs to be found.
BOOTSTRAP_SKILL = "skill-core"
SIBLING_PATH_ROOTS = ("skills", REPOSITORY_SKILLS, "tools", "tests", "deployer")
SIBLING_LOOKUP_DOC = '"Validation" in docs/adding-a-skill.md'


def _sibling_path_files(root: Path) -> list[Path]:
    """The Python under the roots a sibling lookup applies to, outside tests/fixtures, which are other trees."""
    files = [root / "deploy.py"] if (root / "deploy.py").is_file() else []
    for top in SIBLING_PATH_ROOTS:
        files += [path for path in (root / top).rglob("*.py") if not path.is_relative_to(root / "tests" / "fixtures")]
    return sorted(files)


def sibling_path_problems(root: Path) -> list[str]:
    """Report a sys.path entry that reaches a sibling skill's scripts other than through skill-core's lookup.

    skill_roots.sibling_scripts knows where a sibling lives, in the checkout and the deployed home alike, so a
    hand-written path is a copy of that layout that breaks when it changes. The line that puts skill-core on sys.path
    is the one hand-written entry, since it finds the lookup, and a lookup's name is a literal so the skill_deps check
    can read it.
    """
    problems: list[str] = []
    for path, name, tree in python_trees(_sibling_path_files(root), root, problems):
        own = path.parent.parent.name if path.parent.name == "scripts" else None
        for entry in scripts_entries(tree):
            where = f"{name}:{entry.line}"
            if entry.name is None:
                problems.append(
                    f"{where} passes {SIBLING_LOOKUP} a name it cannot read; pass the skill's name as one string "
                    f"literal; see {SIBLING_LOOKUP_DOC}"
                )
            elif entry.hand_written and entry.name not in {BOOTSTRAP_SKILL, own}:
                problems.append(
                    f"{where} puts {entry.name}'s scripts on sys.path by a hand-written path; put {SIBLING_LOOKUP}(\""
                    f'{entry.name}") from skill_roots there instead; see {SIBLING_LOOKUP_DOC}'
                )
            elif not entry.hand_written and entry.name == BOOTSTRAP_SKILL:
                problems.append(
                    f"{where} looks up {BOOTSTRAP_SKILL} through {SIBLING_LOOKUP}; the line that puts it on sys.path "
                    f"already does; see {SIBLING_LOOKUP_DOC}"
                )
    return problems


def fixture_source_problems(root: Path) -> list[str]:
    """Report a fixture source under tests/fixtures that could ship, collide with what ships, or break the contract.

    A fixture source, such as the runtime canary's, is deployed only into a throwaway home. It must keep its own
    source ID and skill names, the shipped source must never name it, and its skills follow the path contract.
    """
    shipped = json.loads((root / "source.json").read_text(encoding="utf-8"))
    names = {path.stem for path in (root / "deploy-meta").glob("*.json")} | set(shipped.get("bundles", {}))
    found: list[str] = []
    for source_file in sorted((root / "tests" / "fixtures").glob("*/source.json")):
        fixture = source_file.parent
        label = fixture.relative_to(root).as_posix()
        if json.loads(source_file.read_text(encoding="utf-8")).get("id") == shipped.get("id"):
            found.append(f"{label}/source.json reuses the shipped source ID")
        for metadata in sorted((fixture / "deploy-meta").glob("*.json")):
            if metadata.stem in names:
                found.append(f"{label} skill {metadata.stem} shares its name with a shipped skill or bundle")
        found += [f"{label}: {problem}" for problem in skill_path_problems(fixture)]
    shipped_files = [
        root / "source.json",
        *(
            path
            for folder in ("skills", "deploy-meta", "agents")
            for path in (root / folder).rglob("*")
            if path.is_file()
        ),
    ]
    for path in shipped_files:
        if "tests/fixtures" in path.read_text(encoding="utf-8", errors="replace").replace("\\", "/"):
            found.append(f"{path.relative_to(root).as_posix()} names tests/fixtures, which never ships")
    return found


def _starts_with_a_shebang(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(2) == b"#!"


def unsupported_script_problems(root: Path) -> list[str]:
    """Report each JavaScript, TypeScript, PowerShell module, or Windows batch file anywhere in a shipped or repository
    skill, scripts/ included, and each file that starts with #! without an executable extension, which runs as a
    program no analyzer or command policy reads."""
    problems: list[str] = []
    files = [path for skill in all_skill_directories(root) for path in skill.rglob("*") if path.is_file()]
    for path in sorted(files, key=lambda p: p.as_posix()):
        name = path.relative_to(root).as_posix()
        if path.suffix.casefold() in UNSUPPORTED_SCRIPT_EXTENSIONS:
            problems.append(f"{name}: a skill's executable files are Bash, Python, and PowerShell")
        elif not is_executable_script(path) and _starts_with_a_shebang(path):
            problems.append(
                f"{name} starts with #!, so it runs as a program; a skill's executable files are Bash, Python, and "
                "PowerShell, named by their extension"
            )
    return problems


def script_language_problems(root: Path, standard_commands: set[str]) -> list[str]:
    """Report where the skill guide or the tool catalogue disagrees with the runner on a skill's script languages."""
    found = [
        f"deployer/tools.py does not list `{interpreter}`, which runs `{extension}` scripts"
        for extension, interpreter in sorted(SCRIPT_INTERPRETERS.items())
        if interpreter not in standard_commands
    ]
    statement = "A skill's executable files are Bash, Python, and PowerShell"
    line = next(
        (line for line in (root / SKILL_GUIDE).read_text(encoding="utf-8").splitlines() if statement in line), None
    )
    if line is None:
        return [*found, f"{SKILL_GUIDE} does not state which languages a skill's executable files may use"]
    supported, _, unsupported = line.partition("JavaScript and TypeScript")
    named = set(re.findall(r"`(\.[a-z0-9]+)`", supported))
    found += [
        f"{SKILL_GUIDE} names `{extension}` as an executable extension"
        for extension in sorted(named - EXECUTABLE_SCRIPT_EXTENSIONS)
    ]
    found += [
        f"{SKILL_GUIDE} does not name `{extension}` as executable"
        for extension in sorted(EXECUTABLE_SCRIPT_EXTENSIONS - named)
    ]
    rejected = set(re.findall(r"`(\.[a-z0-9]+)`", unsupported))
    found += [
        f"{SKILL_GUIDE} does not name `{extension}` as unsupported"
        for extension in sorted(UNSUPPORTED_SCRIPT_EXTENSIONS - rejected)
    ]
    return found


# The folder Python writes its compiled modules to beside a script, which no commit holds.
BYTECODE_FOLDER = "__pycache__"


def script_layout_problems(root: Path) -> list[str]:
    """Report each executable file of a shipped or repository skill outside the skill's scripts/ directory, and each
    file in a folder below scripts/, which the script policies, the suites' discovery, and the type check read only
    one level deep."""
    problems: list[str] = []
    for skill in all_skill_directories(root):
        for path in sorted(skill.rglob("*")):
            inside = path.relative_to(skill)
            if not path.is_file() or BYTECODE_FOLDER in inside.parts:
                continue
            if is_executable_script(path) and inside.parts[0] != "scripts":
                problems.append(
                    f"{path.relative_to(root).as_posix()} is an executable file outside its skill's scripts/; move it "
                    f"to {skill.relative_to(root).as_posix()}/scripts/"
                )
            elif inside.parts[0] == "scripts" and len(inside.parts) > 2:
                problems.append(
                    f"{path.relative_to(root).as_posix()} is in a folder below its skill's scripts/, which the script "
                    f"policies do not read; move it to {skill.relative_to(root).as_posix()}/scripts/"
                )
    return problems


def skill_tree_problems(root: Path) -> list[str]:
    """Report a directory under skills/ that deployer/source.py would not read as a skill or a shared asset.

    A skill is skills/<name> holding SKILL.md and source.json declares each shared asset, so any other directory, or a
    SKILL.md deeper than skills/<name>, is a folder whose files and suites nothing would find.
    """
    skills = root / "skills"
    if not skills.is_dir():
        return []
    source = root / "source.json"
    shared = json.loads(source.read_text(encoding="utf-8")).get("shared_assets", {}) if source.is_file() else {}
    problems = [
        f"{directory.relative_to(root).as_posix()} holds no SKILL.md and source.json declares no such shared asset"
        for directory in sorted(path for path in skills.iterdir() if path.is_dir())
        if not (directory / "SKILL.md").is_file() and directory.name not in shared
    ]
    problems += [
        f"{skill_md.relative_to(root).as_posix()} is deeper than skills/<name>"
        for skill_md in sorted(skills.rglob("SKILL.md"))
        if len(skill_md.relative_to(skills).parts) > 2
    ]
    return problems


def shared_guidance_problems(root: Path) -> list[str]:
    """Report a shipped skill whose SKILL.md names a shared Markdown asset from source.json.

    Claude runs a skill directly; only the generated ~/.agents adapter tells other runtimes to read shared Markdown
    guidance first, so a skill that points to it spends a turn on every Claude run.
    """
    shared = json.loads((root / "source.json").read_text(encoding="utf-8")).get("shared_assets", {})
    guidance = sorted(name for name in shared if name.endswith(".md"))
    return [
        f"{(skill / 'SKILL.md').relative_to(root).as_posix()} points to {name}; the runtime adapter does that"
        for skill in skill_directories(root)
        for name in guidance
        if name in (skill / "SKILL.md").read_text(encoding="utf-8")
    ]


class SkillLayoutPolicies(unittest.TestCase):
    def test_skill_scripts_use_standard_layout(self) -> None:
        self.assertEqual([], script_layout_problems(REPOSITORY_ROOT))

    def test_every_directory_under_skills_is_a_skill_or_a_shared_asset(self) -> None:
        self.assertEqual([], skill_tree_problems(REPOSITORY_ROOT))

    def test_skill_markdown_does_not_embed_programs(self) -> None:
        self.assertEqual([], embedded_program_problems(REPOSITORY_ROOT))

    def test_skills_leave_shared_runtime_guidance_to_their_adapters(self) -> None:
        shared = json.loads((REPOSITORY_ROOT / "source.json").read_text(encoding="utf-8"))["shared_assets"]
        self.assertIn("runtime-compatibility.md", shared)
        self.assertEqual([], shared_guidance_problems(REPOSITORY_ROOT))

    def test_deploy_variables_are_declared_consistently_and_used(self) -> None:
        from deployer import config

        self.assertEqual(
            [],
            deploy_variable_problems(
                REPOSITORY_ROOT,
                set(config.CONFIGURED_VARIABLES),
                set(config.PROMPTS),
                set(config.DERIVED_VARIABLES),
            ),
        )

    def test_fixture_sources_never_ship_or_collide_with_what_ships(self) -> None:
        self.assertTrue(list((REPOSITORY_ROOT / "tests" / "fixtures").glob("*/source.json")))
        self.assertEqual([], fixture_source_problems(REPOSITORY_ROOT))

    def test_skill_scripts_are_bash_python_or_powershell(self) -> None:
        # Pinned here, not read back from the constants: a new script language is a decision for the skill contract,
        # the deployment prerequisites, and the renderer, never a one-line change to the runner.
        self.assertEqual({".bash", ".ps1", ".py", ".sh"}, EXECUTABLE_SCRIPT_EXTENSIONS)
        self.assertEqual({".bat", ".cjs", ".cmd", ".js", ".mjs", ".psm1", ".ts"}, UNSUPPORTED_SCRIPT_EXTENSIONS)
        python = "python" if platform_support.WINDOWS else "python3"
        self.assertEqual({".bash": "bash", ".sh": "bash", ".py": python, ".ps1": "pwsh"}, dict(SCRIPT_INTERPRETERS))
        self.assertEqual([], unsupported_script_problems(REPOSITORY_ROOT))

    def test_script_languages_agree_with_the_tool_catalogue_and_the_skill_guide(self) -> None:
        from deployer import tools

        self.assertEqual([], script_language_problems(REPOSITORY_ROOT, set(tools.STANDARD_COMMANDS)))

    def test_skills_reach_their_own_and_sibling_files_through_the_skill_directory(self) -> None:
        self.assertEqual([], skill_path_problems(REPOSITORY_ROOT))

    def test_skills_never_leave_an_output_path_to_the_agent(self) -> None:
        self.assertEqual([], output_placeholder_problems(REPOSITORY_ROOT))

    def test_deploy_metadata_is_in_the_canonical_format(self) -> None:
        self.assertEqual([], metadata_format_problems(REPOSITORY_ROOT))

    def test_runtime_support_follows_from_what_each_skill_needs(self) -> None:
        self.assertEqual([], runtime_support_problems(REPOSITORY_ROOT))

    def test_skill_scripts_declare_each_sibling_they_put_on_sys_path(self) -> None:
        self.assertEqual([], script_dependency_problems(REPOSITORY_ROOT))

    def test_modules_reach_sibling_scripts_through_skill_core(self) -> None:
        self.assertEqual([], sibling_path_problems(REPOSITORY_ROOT))
