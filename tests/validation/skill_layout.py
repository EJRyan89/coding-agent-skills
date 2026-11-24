"""Layout policies: where a skill's files live, how long its fences are, how it names its own and its siblings'
files, its metadata and variables, its script languages, the fixture sources beside it, the trigger scenario that
judges the description of each skill the model may start, and the plugins assembled from the skills.
"""

from __future__ import annotations

import re
import unittest
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import file_presence
import frontmatter
import git_subcommands
import guidance_record
import json_reader
import json_writer
import skill_menu
from agent_skills import MAX_NAME, SKILL_FILE
from bash_syntax import BashSyntaxError
from link_destinations import link_destinations
from link_targets import Cover, link_spans
from markdown_sections import CODE, EXECUTABLE_FENCES, Block, blocks, code_spans, markdown_section
from path_containment import within
from runtime_support import RUNTIME_TITLES, RUNTIMES
from skill_menu import PLUGIN_HOOKS, PLUGIN_MANIFEST
from skill_roots import SKILL_CORE
from skill_scripts import WORKING_FILES_DOC, powershell_script_commands
from suite_names import is_suite_name
from text_lines import lines_and_breaks, text_lines
from validation_support import (
    EXECUTABLE_SCRIPT_EXTENSIONS,
    REPOSITORY_ROOT,
    REPOSITORY_SKILLS,
    SCRIPT_INTERPRETERS,
    SHELL_FENCES,
    SIBLING_LOOKUP,
    SKILL_GUIDE,
    TEMPLATE_TOKEN,
    UNSUPPORTED_SCRIPT_EXTENSIONS,
    VALIDATION_DOC,
    all_skill_directories,
    fence_body_line,
    fence_holders,
    find_fences,
    is_executable_script,
    is_test_script,
    listed_names,
    path_order,
    python_trees,
    scripts_entries,
    scripts_put_on_path,
    skill_directories,
    skills_by_name,
)

from deployer import platform_support
from deployer.render import shipped_files
from tools.plugin_agents import DEPLOYED_SCRIPT
from tools.plugin_build import README as PLUGIN_README
from tools.runtime_canary import RUNTIMES as RUNTIME_PROGRAMS

MAXIMUM_INLINE_EXECUTABLE_LINES = 5


# Every label a fence of commands or a program may carry, from ejryan89-skill-core's one table, which the renderer
# substitutes tokens by and the audit-skill inventory reads commands by, so a fence the grant checks read as commands,
# or the renderer escapes for a language, is one the five-line and output-placeholder rules read too.
EXECUTABLE_FENCE_LANGUAGES = EXECUTABLE_FENCES


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
    for markdown in sorted(
        file_presence.matching(root / tree, "**/*.md"), key=lambda path: path.relative_to(root).as_posix()
    ):
        name = markdown.relative_to(root).as_posix()
        problems.extend(
            f"{name}:{fence.line_number} contains a {fence.language} fence with {fence.body_line_count} "
            f"lines. Markdown may contain only short command examples of at most "
            f"{MAXIMUM_INLINE_EXECUTABLE_LINES} lines; move executable logic to the skill's scripts/ directory."
            for fence in find_executable_fences(text_lines(markdown.read_text(encoding="utf-8")))
            if fence.body_line_count > MAXIMUM_INLINE_EXECUTABLE_LINES
        )
    return problems


def _template_tokens(directory: Path) -> set[str]:
    tokens: set[str] = set()
    for path in file_presence.matching(directory, "**/*"):
        if file_presence.is_present(path, other_is_absent=True):
            tokens.update(TEMPLATE_TOKEN.findall(path.read_text(encoding="utf-8", errors="replace")))
    return tokens


def deploy_variable_problems(
    root: Path,
    configured: set[str],
    prompted: set[str],
    derived: set[str],
) -> list[str]:
    """Report configured variables and template tokens that are not used consistently."""
    problems: list[str] = [
        f"configure never prompts for configured variable {key}" for key in sorted(configured - prompted)
    ]
    problems.extend(f"configure prompts for unknown variable {key}" for key in sorted(prompted - configured))

    required_anywhere: set[str] = set()
    used_anywhere: set[str] = set()
    skills = skills_by_name(root)
    for metadata in sorted(file_presence.matching(root / "deploy-meta", "*.json"), key=path_order):
        skill = metadata.stem
        document = json_reader.loads(metadata.read_text(encoding="utf-8"))
        required = set(listed_names(metadata, document, "required_vars", problems))
        required_anywhere |= required
        directory = skills.get(skill)
        used = _template_tokens(directory) if directory is not None else set()
        used_anywhere |= used
        problems.extend(
            f"skill {skill} uses {{{{{key}}}}} without declaring it in required_vars" for key in sorted(used - required)
        )
        problems.extend(
            f"skill {skill} declares required variable {key} but never uses it" for key in sorted(required - used)
        )
        problems.extend(
            f"skill {skill} requires unknown variable {key}" for key in sorted(required - configured - derived)
        )
    problems.extend(
        f"configured variable {key} is not required by any skill" for key in sorted(configured - required_anywhere)
    )

    shared = json_reader.loads((root / "source.json").read_text(encoding="utf-8")).get("shared_assets", {})
    for asset in sorted(shared):
        path = root / "skills" / asset
        # A declared asset that is not there, a link to nothing included, is named; one whose stat fails raises.
        if not file_presence.is_there(path):
            problems.append(f"source.json declares shared asset {asset}, which skills/ does not hold")
            continue
        # A shared asset is a file or a folder; a folder's files are read where its skills are.
        text = "" if file_presence.is_directory(path, other_is_absent=True) else path.read_text(encoding="utf-8")
        tokens = set(TEMPLATE_TOKEN.findall(text))
        used_anywhere |= tokens
        problems.extend(f"shared asset {asset} uses non-derived variable {key}" for key in sorted(tokens - derived))
    # A derived value no template uses still has to pass the allowlist, so it could refuse a deployment for nothing.
    problems.extend(
        f"derived variable {key} is not used by any skill or shared asset" for key in sorted(derived - used_anywhere)
    )
    return problems


METADATA_DOC = '"Metadata" in docs/adding-a-skill.md'


def metadata_format_problems(root: Path) -> list[str]:
    """Report deploy-meta files not laid out as tools/new_skill.py writes them, so every file reads the same way."""
    from tools import new_skill

    problems: list[str] = []
    for metadata in file_presence.matching(root / "deploy-meta", "*.json"):
        text = metadata.read_bytes().decode("utf-8")
        if text != new_skill.metadata_text(json_reader.loads(text)):
            problems.append(f"deploy-meta/{metadata.name} is not in the canonical metadata format; see {METADATA_DOC}")
    return problems


def hooked_agents(root: Path) -> set[str]:
    """The agents under agents/ whose definition declares hooks, such as the code-review reviewer's guard."""
    return {path.stem for path in file_presence.matching(root / "agents", "*.md") if "hooks" in frontmatter.read(path)}


def user_only(document: frontmatter.Frontmatter) -> bool:
    """Whether a SKILL.md's frontmatter lets only the user start the skill, as frontmatter reads it."""
    return document.user_only()


def own_needs(root: Path, name: str) -> set[str]:
    """The runtime capabilities a skill's own frontmatter, metadata, and scripts show it needs.

    A skill that grants Agent and names, in backticks, an agent whose definition declares hooks starts that agent
    under them, and one whose deploy-meta declares a network tool, required or optional, or one of whose Bash,
    PowerShell, or Python scripts runs a git subcommand that may reach a remote, needs network access. The scripts are
    listed with `file_presence.listed`, so an AppleDouble sidecar macOS writes beside one on a FAT, exFAT, or SMB volume
    is never read as a script.
    """
    import runtime_support

    skills = skills_by_name(root)
    if name not in skills:
        return set()
    skill_md = skills[name] / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8-sig")
    document = frontmatter.read(skill_md)
    granted = document.value("allowed-tools") or []
    starts_hooked = any(f"`{agent}`" in text for agent in hooked_agents(root))
    metadata = root / "deploy-meta" / f"{name}.json"
    declared = json_reader.loads(metadata.read_text(encoding="utf-8")) if file_presence.is_present(metadata) else {}
    tools = frozenset(
        [*listed_names(metadata, declared, "tools", None), *listed_names(metadata, declared, "optional_tools", None)]
    )
    scripts = skills[name] / "scripts"
    git_subcommands = frozenset(
        subcommand
        for script in (file_presence.listed(scripts) if file_presence.is_directory(scripts) else [])
        if not is_test_script(script)
        for subcommand in script_git_subcommands(script)
    )
    return runtime_support.frontmatter_needs(
        granted if isinstance(granted, list) else [granted], user_only(document), starts_hooked, tools, git_subcommands
    )


class UnreadScript(ValueError):
    """A skill's script whose git subcommands cannot be read, since Bash it is, or runs, is Bash the grammar refuses."""

    def __init__(self, script: Path, reason: str) -> None:
        super().__init__(reason)
        self.script, self.reason = script, reason


def script_git_subcommands(script: Path) -> set[str]:
    """The git subcommands one of a skill's scripts runs, by ejryan89-skill-core's git_subcommands, the reader
    audit-skill's runtime-support derivation shares: a Bash script's, a PowerShell script's as powershell_readings
    lists its commands, read once for each text in a validation run as powershell_script_commands holds it, and a
    Python script's git calls; any other file runs none this reads. Raises UnreadScript, naming the script, for a
    Bash script the grammar refuses, or one the script eval, a shell's -c, a trap's action, or a PowerShell script's
    Bash shell runs,
    as audit-skill's reader fails it."""
    import runtime_support

    suffix = script.suffix.casefold()
    if suffix in (".sh", ".bash"):
        # Read as bytes, so a lone CR stays inside its line as Bash and audit-skill read it, never turned into a LF.
        text = script.read_bytes().decode("utf-8-sig")
        try:
            return git_subcommands.bash_git_subcommands(text, powershell_script_commands)
        except BashSyntaxError as exc:
            raise UnreadScript(script, f"not Bash the grammar reads ({exc})") from exc
    if suffix == ".ps1":
        commands = powershell_script_commands(script.read_text(encoding="utf-8-sig"))
        try:
            return git_subcommands.powershell_git_subcommands(commands, powershell_script_commands)
        except BashSyntaxError as exc:
            raise UnreadScript(script, f"a Bash script it runs is not Bash the grammar reads ({exc})") from exc
    if suffix == ".py":
        return runtime_support.python_git_subcommands(script.read_text(encoding="utf-8"))
    return set()


def derived_needs(root: Path, name: str) -> set[str]:
    """The runtime capabilities a skill needs: its own_needs, and what each skill it invokes carries over, by
    ejryan89-skill-core's runtime_support, the derivation audit-skill's script shares."""
    import runtime_support

    skills = skills_by_name(root)

    def invokes(skill: str) -> set[str]:
        path = skills[skill] / "SKILL.md" if skill in skills else None
        return runtime_support.invoked_skills(path.read_text(encoding="utf-8-sig"), skills) if path else set()

    needs = own_needs(root, name)
    for carried in runtime_support.carried_needs(name, lambda skill: own_needs(root, skill), invokes).values():
        needs |= carried
    return needs


def runtime_support_problems(root: Path) -> list[str]:
    """Report deploy-meta files whose runtime support is missing or does not follow from what the skill needs.

    A selectable skill is full on a runtime that offers every capability it needs (derived_needs), and otherwise
    partial or none, naming exactly the capabilities that runtime lacks. A hidden skill is none everywhere, since no
    runtime starts it.
    """
    import runtime_support

    problems: list[str] = []
    for metadata in file_presence.matching(root / "deploy-meta", "*.json"):
        name = f"deploy-meta/{metadata.name}"
        document = json_reader.loads(metadata.read_text(encoding="utf-8"))
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
        try:
            needs = derived_needs(root, metadata.stem)
        except UnreadScript as exc:
            script = exc.script.relative_to(root).as_posix()
            problems.append(f"{name}: cannot read {script}: {exc.reason}; see {METADATA_DOC}")
            continue
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


# Each runtime's support page: how the runtime behaves, and each skill's support on it.
RUNTIME_PAGES = {"codex": "docs/codex-support.md", "copilot-cli": "docs/copilot-support.md"}
# Where a support page's sentence ends: after a full stop, a question mark, or an exclamation mark, at the space after.
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def page_sentences(text: str) -> Iterator[tuple[Block, str]]:
    """Each sentence of a page's blocks outside code, as markdown_sections' `blocks` walks them, with its block: the
    block's text with each run of whitespace one space, split after each `.`, `?`, or `!` that a space follows."""
    for block in blocks(text_lines(text)):
        if block.kind != CODE:
            for sentence in SENTENCE_END.split(" ".join(block.text().split())):
                yield block, sentence


def runtime_page_problems(root: Path) -> list[str]:
    """Report a runtime's support page that states a skill's support on that runtime otherwise than its metadata.

    A statement is a sentence of a block outside code, as markdown_sections' `blocks` walks them, that names one skill
    and one level, each a code span as code_spans pairs it (`full`, `partial`, or `none`); its level must be the one
    the skill's deploy-meta declares for the page's runtime, and the capabilities it names in code spans exactly those
    the declaration needs. A sentence naming several skills or levels is no statement this reads.
    """
    import runtime_support

    levels = {runtime_support.FULL, runtime_support.PARTIAL, runtime_support.NONE}
    declared = {
        metadata.stem: json_reader.loads(metadata.read_text(encoding="utf-8")).get("runtime_support")
        for metadata in file_presence.matching(root / "deploy-meta", "*.json")
    }
    problems: list[str] = []
    for runtime, page in RUNTIME_PAGES.items():
        path = root / page
        if not file_presence.is_present(path):
            continue
        for _block, sentence in page_sentences(path.read_text(encoding="utf-8")):
            spans = {span.text for span in code_spans(sentence)}
            skills, stated = spans & declared.keys(), spans & levels
            if len(skills) != 1 or len(stated) != 1:
                continue
            skill, level = next(iter(skills)), next(iter(stated))
            try:
                support = runtime_support.parse(declared[skill])[runtime]
            except runtime_support.RuntimeSupportError:
                continue  # runtime_support_problems reports it
            named = sorted(spans & runtime_support.CAPABILITIES.keys())
            if level != support.level or (level != runtime_support.FULL and named != sorted(support.needs)):
                problems.append(
                    f"{page} says {skill} is {level}{' lacking ' + ', '.join(named) if named else ''} on "
                    f"{runtime}, but its deploy-meta declares {support.level}"
                    f"{' lacking ' + ', '.join(sorted(support.needs)) if support.needs else ''}; "
                    f"see {METADATA_DOC}"
                )
    return problems


SOURCE_LESSON = '"A statement about another program cites its current source" in docs/lessons.md'
# What a sentence's code spans and links are read as: a code span that starts a runtime's program as a runtime's
# name, and any other code span or link as text no name or verb matches.
RUNTIME_COMMAND = "\x00"
NOT_PROSE = "\x01"
# A runtime as a support page names it: its title as runtime_support.RUNTIME_TITLES gives it, with or without the
# `CLI` that ends it, and with or without a `GitHub` before it, or a code span that starts its program.
RUNTIME_NAME = "|".join(
    sorted(
        {re.escape(spelling) for title in RUNTIME_TITLES.values() for spelling in (title, title.removesuffix(" CLI"))},
        key=len,
        reverse=True,
    )
)
# A finite verb after a runtime's name: an auxiliary, or a word ending in `s` that is none of the words ending in `s`
# that follow a runtime's name in a page without being its verb, such as `as` in `on Copilot CLI as before`, or the
# plural in `the Copilot CLI versions`. One adverb may come between, as in `Codex never looks`, and a possessive and
# one word before it, as in `Codex's sandbox cannot`.
AUXILIARIES = "is|are|was|were|has|have|had|does|do|did|cannot|can|could|may|might|will|would|must|should"
ADVERBS = "also|still|then|now|never|only|always|first|already|instead|again|itself"
NOT_VERBS = "as|its|this|thus|plus|unless|across|always|less|versions|releases|skills|settings|users"
RUNTIME_STATEMENT = re.compile(
    rf"(?<![\w-])(?:(?:GitHub )?(?:{RUNTIME_NAME})|{RUNTIME_COMMAND})(?:'s [a-z-]+)? (?:(?:{ADVERBS}) )?"
    rf"(?:(?:{AUXILIARIES})\b|(?!(?:{NOT_VERBS})\b)[a-z]+s\b)"
)
# Each sentence of a support page that states how a runtime behaves with no source the guidance record rereads, by
# its page and its text as page_sentences gives it, with why it needs none.
UNSOURCED_STATEMENTS: dict[tuple[str, str], str] = {
    (
        "docs/codex-support.md",
        "After deploying, check that Codex finds every adapter without starting a model:",
    ): "an instruction to run the verifier, which states no behavior of Codex's",
    (
        "docs/codex-support.md",
        "On Windows, Codex looks for personal skills in your Windows profile folder, whatever `HOME` or `USERPROFILE` "
        "says.",
    ): "Codex's skills page names only $HOME/.agents/skills; the deployable workflow's Windows job deploys into the "
    "profile folder and fails its deploy.py verify when Codex looks elsewhere",
    (
        "docs/codex-support.md",
        "If `USERPROFILE` itself has been changed, the adapters land where Codex never looks, and the verifier reports "
        "them as `NOT FOUND`.",
    ): "follows from the sentence before it, which the deployable workflow's Windows job holds",
    (
        "docs/codex-support.md",
        "`codex debug prompt-input -c sandbox_mode=workspace-write` says `Network access is enabled` once the setting "
        "is read, and `Network access is restricted` before.",
    ): "quotes what the command beside it prints, which a reader runs to see it, and no page documents",
    (
        "docs/codex-support.md",
        "On Windows, Codex CLI runs every shell command through `pwsh` when it finds one on `PATH`, and through "
        "Windows PowerShell (`powershell.exe`) otherwise.",
    ): "observed in the runtime canary's Windows runs (#1423), which no Codex page documents; deploy.py check's pwsh "
    "command line reports the alias that breaks it",
    (
        "docs/codex-support.md",
        "With no `pwsh` on `PATH`, `check` prints no such line, since Codex then uses Windows PowerShell, which its "
        "sandbox starts; the validation suite still needs PowerShell 7.",
    ): "the other half of the pwsh observation above, from the same canary runs",
    (
        "docs/copilot-support.md",
        "A `COPILOT_HOME` that is not an absolute path is refused by every command of `deploy.py` once its command "
        "line is read, before anything else (`--help` and a usage error still exit 0 and 2): Copilot completes such a "
        "value from the directory each session starts in, so no one folder holds the skills it reads.",
    ): "observed with copilot skill list --json in throwaway homes (#2330), which Copilot's configuration directory "
    "reference does not state; the deployer refuses the value whatever Copilot does with it",
}


def statement_prose(sentence: str) -> str:
    """A sentence as RUNTIME_STATEMENT reads it: each link, its text and destination with it, and each code span outside
    one, as NOT_PROSE, but a code span whose first word is a runtime's program, which is RUNTIME_COMMAND."""
    destinations = link_destinations([sentence])
    links = Cover.of(link_spans(sentence, destinations))
    pieces: list[tuple[int, int, str]] = [
        (start, end, NOT_PROSE) for start, end in zip(links.starts, links.ends, strict=True)
    ]
    for span in code_spans(sentence):
        if not links.overlaps(span.start, span.end):
            words = span.text.split()
            pieces.append(
                (span.start, span.end, RUNTIME_COMMAND if words[:1] and words[0] in RUNTIME_PROGRAMS else NOT_PROSE)
            )
    prose: list[str] = []
    position = 0
    for start, end, placeholder in sorted(pieces):
        prose.append(sentence[position:start] + placeholder)
        position = end
    prose.append(sentence[position:])
    return "".join(prose)


def runtime_statement_source_problems(root: Path) -> list[str]:
    """Report a sentence of a runtime's support page that states how a runtime behaves without a link, in its block,
    to a page or changelog the guidance record lists, so that the guidance audit and the changelog watch reread it.

    A sentence is read as page_sentences gives it. It states how a runtime behaves when RUNTIME_STATEMENT matches it
    as statement_prose reads it: a runtime's name or a code span that starts its program, then a finite verb, as in
    `Codex looks for personal skills` or `` `copilot -p` does not expand``. Its block, the paragraph or list item that
    holds it, must link at least one page or changelog `.claude/skills/audit-repository/references/guidance.json`
    lists, compared without its fragment as guidance_record's canonical_url gives it, and no web address the record does
    not list. A sentence UNSOURCED_STATEMENTS names needs none, for the reason it gives, and an entry that names no
    sentence of its page fails.

    Limits: a name read as a verb's subject is a guess: a verb that follows the name after more than a possessive and
    one word, or more than one adverb, is not read, as in `Codex CLI before 0.88.0 does not say`, and a name followed
    by a verb it is not the subject of, as in `when Codex is installed`, is read as a statement. A link that sits in
    another block, such as a list of sources below the paragraph, does not count for it.
    """
    try:
        record = guidance_record.read(root / guidance_record.GUIDANCE_PATH, [*RUNTIMES, guidance_record.PYTHON_SUBJECT])
    except guidance_record.GuidanceRecordError as exc:
        return [f"cannot read the sources the runtime support pages may link: {exc}"]
    sources = {
        guidance_record.canonical_url(url)
        for url in [*(page.url for page in record.pages), *(changelog.url for changelog in record.changelogs)]
    }
    problems: list[str] = []
    statements = 0
    held: set[tuple[str, str]] = set()
    for page in RUNTIME_PAGES.values():
        path = root / page
        if not file_presence.is_present(path):
            continue
        for block, sentence in page_sentences(path.read_text(encoding="utf-8")):
            if RUNTIME_STATEMENT.search(statement_prose(sentence)) is None:
                continue
            statements += 1
            if (page, sentence) in UNSOURCED_STATEMENTS:
                held.add((page, sentence))
                continue
            urls = [
                destination.url.partition("#")[0]
                for destination in link_destinations(list(block.lines))
                if destination.is_url
            ]
            unread = [url for url in urls if guidance_record.canonical_url(url) not in sources]
            where = f"{page}:{block.start + 1} states how a runtime behaves"
            if unread:
                problems.append(
                    f"{where} and its block links {', '.join(unread)}, which {guidance_record.GUIDANCE_PATH} does not "
                    f"list: {sentence!r}; add the page to the record or link the page it lists; see {SOURCE_LESSON}"
                )
            elif not urls:
                problems.append(
                    f"{where} with no link in its block to a page {guidance_record.GUIDANCE_PATH} lists: {sentence!r}; "
                    f"link the runtime's page that says so, or name the sentence in UNSOURCED_STATEMENTS with why it "
                    f"needs none; see {SOURCE_LESSON}"
                )
    problems += [
        f"UNSOURCED_STATEMENTS names {sentence!r}, which {page} does not hold as a sentence stating how a runtime "
        f"behaves; remove the entry"
        for page, sentence in sorted(UNSOURCED_STATEMENTS.keys() - held)
    ]
    if not statements:
        problems.append(
            f"could not measure: no sentence of {', '.join(RUNTIME_PAGES.values())} states how a runtime behaves, so "
            f"none was held to its source"
        )
    return problems


def capability_guide_problems(root: Path) -> list[str]:
    """Report a guide section runtime_support.CAPABILITY_GUIDES names that its document no longer has, and a reason
    for lacking a capability a guide says how to give that does not carry runtime_support.guide_pointer's sentence.

    A reason that restated the guide's settings was left naming one route when the guide gained another; pointing to
    the guide's section keeps one owner for how to give the capability.
    """
    import runtime_support

    problems: list[str] = []
    checked = 0
    for document, heading, _gives in sorted(set(runtime_support.CAPABILITY_GUIDES.values())):
        path = root / document
        reason = file_presence.refusal_reason(path)
        if reason not in (None, file_presence.MISSING):
            problems.append(f"{document} {reason}, which runtime_support.CAPABILITY_GUIDES names as a guide")
        elif reason is not None or markdown_section(path.read_text(encoding="utf-8"), f"## {heading}") is None:
            problems.append(f'{document} has no "## {heading}" section, which runtime_support.CAPABILITY_GUIDES names')
    for metadata in file_presence.matching(root / "deploy-meta", "*.json"):
        try:
            declared = runtime_support.parse(json_reader.loads(metadata.read_text(encoding="utf-8"))["runtime_support"])
        except (KeyError, runtime_support.RuntimeSupportError):
            continue  # runtime_support_problems reports it
        for runtime, support in declared.items():
            for need in support.needs:
                pointer = runtime_support.guide_pointer(runtime, need)
                checked += pointer is not None
                if pointer is not None and pointer not in support.reason:
                    problems.append(
                        f"deploy-meta/{metadata.name}: the {runtime} reason for lacking {need} does not carry "
                        f"{pointer!r}; point to the guide rather than restating its settings"
                    )
    if not checked:
        problems.append(
            "could not measure: no readable deploy-meta declaration lacks a capability "
            "runtime_support.CAPABILITY_GUIDES names, so no reason was held to its guide"
        )
    return problems


# Every spelling of the home folder a skill's Markdown may hold before an install path: the deployer's token, the
# shell's variable in both forms, and the tilde a shell expands.
HOME_SPELLINGS = ("{{HOME}}", "$HOME", "${HOME}", "~")
INSTALL_PATH = re.compile(f"(?:{'|'.join(map(re.escape, HOME_SPELLINGS))})" + r"/\.claude/skills/([A-Za-z0-9._-]+)")
SIBLING_PATH = re.compile(r"\$\{CLAUDE_SKILL_DIR\}/\.\./([A-Za-z0-9._-]+)")
# A concrete file a skill names through its directory; a pattern such as scripts/* in a grant names none.
SKILL_DIR_FILE = re.compile(r"\$\{CLAUDE_SKILL_DIR\}/([A-Za-z0-9._/-]+)(?![A-Za-z0-9._/*<-])")
BARE_SCRIPT_PATH = re.compile(r"""(?:^|[\s"'=])(?:\./|\.\./[A-Za-z0-9._-]+/)?scripts/""")
# In SKILL.md prose, a code span that names the skill's own scripts/ or references/ by a relative path. Claude Code
# resolves it against the working directory, and Codex and Copilot against the adapter, which has neither folder.
# A code span is read with ejryan89-skill-core's code_spans, as CommonMark pairs backtick runs.
BARE_OWN_PATH = re.compile(r"""(?:^|[\s"'=(])(?:\./|\.\./[A-Za-z0-9._-]+/)?(?:scripts|references)/""")
# audit-skill recommends a command under `scripts/` in the skill it audits, which is not a path of its own.
BARE_OWN_PATH_EXEMPT = frozenset({("skills/audit-skill/SKILL.md", "scripts/")})
SKILL_PATHS_DOC = '"Paths to a skill\'s own files" in docs/adding-a-skill.md'
# Git Bash takes $HOME from HOME, which need not be the profile folder the deployer installs into.
HOME_VARIABLE = re.compile(r"\$(?:HOME\b|\{HOME\}|env:HOME\b)", re.IGNORECASE)
AGENTS_DOC = '"Subagent definitions" in docs/adding-a-skill.md'


def skill_path_problems(root: Path) -> list[str]:
    """Report skills and agents that reach a skill's files other than through ${CLAUDE_SKILL_DIR}. Every body line of
    a shell fence is a shell line here, a `console` session's prompts and output included, which audit-skill reads
    apart; no fence here is labelled `console`, and an output line read so is held to more rules, not fewer."""
    skills = {path.parent.name for path in file_presence.matching(root / "skills", "*/SKILL.md")}
    problems: list[str] = []
    for path, dependencies, directory in _path_documents(root):
        name = path.relative_to(root).as_posix()
        lines = text_lines(path.read_text(encoding="utf-8"))
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
        (path, None, None) for path in sorted(file_presence.matching(root / "agents", "*.md"), key=path_order)
    ]
    skills = skills_by_name(root)
    for metadata in file_presence.matching(root / "deploy-meta", "*.json"):
        document = json_reader.loads(metadata.read_text(encoding="utf-8"))
        declared: set[str] = set(listed_names(metadata, document, "skill_deps", None))
        if (skill_directory := skills.get(metadata.stem)) is not None:
            documents += [
                (path, declared, skill_directory)
                for path in sorted(file_presence.matching(skill_directory, "**/*.md"), key=path_order)
            ]
    return documents


def _prose_path_problems(name: str, number: int, line: str) -> list[str]:
    """Code spans in SKILL.md prose that name the skill's own scripts/ or references/ by a relative path."""
    return [
        f"{name}:{number} names `{span.text}` by a bare relative path; see {SKILL_PATHS_DOC}"
        for span in code_spans(line)
        if BARE_OWN_PATH.search(span.text) and (name, span.text) not in BARE_OWN_PATH_EXEMPT
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
    # An agent's hook has no ${CLAUDE_SKILL_DIR}, so it runs a skill's script through Python's expanduser, the one form
    # a plugin build rewrites to its own copy.
    hooked = [match.span() for match in DEPLOYED_SCRIPT.finditer(line)] if dependencies is None else []
    problems: list[str] = [
        f"{name}:{number} names skill {match.group(1)} by its install path; see {SKILL_PATHS_DOC}"
        for match in INSTALL_PATH.finditer(line)
        if match.group(1) in skills and not any(start <= match.start() < end for start, end in hooked)
    ]
    if in_shell_fence and BARE_SCRIPT_PATH.search(line):
        problems.append(f"{name}:{number} runs a script by a bare relative path; see {SKILL_PATHS_DOC}")
    if dependencies is None and HOME_VARIABLE.search(line):
        problems.append(f"{name}:{number} finds a file through $HOME; see {AGENTS_DOC}")
    problems.extend(
        f"{name}:{number} reaches ../{match.group(1)} without declaring it in skill_deps"
        for match in SIBLING_PATH.finditer(line)
        if dependencies is not None and match.group(1) not in dependencies
    )
    for match in SKILL_DIR_FILE.finditer(line) if directory is not None else ():
        named = match.group(1).rstrip(".")  # a path may end a sentence
        try:
            there = directory is None or file_presence.is_there(directory / named)
        except OSError as error:
            problems.append(f"{name}:{number} names ${{CLAUDE_SKILL_DIR}}/{named}, which cannot be examined: {error}")
            continue
        if not there:
            problems.append(f"{name}:{number} names ${{CLAUDE_SKILL_DIR}}/{named}, which does not exist")
    return problems


# An option that names where a script writes, given a placeholder for the agent to fill in: "--output <file>",
# "--plans=<dir>". Placeholders for paths a command printed, such as "--run <run directory>", name no output option.
OUTPUT_PLACEHOLDER = re.compile(r"""(--(?:output(?:-[a-z]+)*|out(?:-dir)?|plans))(?:\s+|=)["']?<[^>]*>""")


def output_placeholder_problems(root: Path, tree: str = "skills") -> list[str]:
    """Report command fences in a skill tree's Markdown that leave the agent to choose where a script writes.

    Given a placeholder and a skill directory it already knows, an agent writes beside SKILL.md, and the deployer
    then sees the installed skill as modified and stops updating it.
    """
    problems: list[str] = []
    for path in sorted(
        file_presence.matching(root / tree, "**/*.md"), key=lambda path: path.relative_to(root).as_posix()
    ):
        name = path.relative_to(root).as_posix()
        lines = text_lines(path.read_text(encoding="utf-8"))
        for number, (line, holder) in enumerate(zip(lines, fence_holders(lines), strict=True), start=1):
            in_command_fence = (
                fence_body_line(holder, number - 1)
                and holder is not None
                and holder.language.casefold() in EXECUTABLE_FENCE_LANGUAGES
            )
            if in_command_fence:
                problems.extend(
                    f"{name}:{number} leaves {match.group(1)} to the agent; let the script choose "
                    f"and print the path; see {WORKING_FILES_DOC}"
                    for match in OUTPUT_PLACEHOLDER.finditer(line)
                )
    return problems


def script_dependency_problems(root: Path) -> list[str]:
    """Report a skill script that puts another skill's scripts on sys.path without declaring it in skill_deps.

    The deployer installs a skill's skill_deps closure beside it and nothing else, so an undeclared sibling is missing
    wherever the skill is deployed without it, and the import fails at run time.
    """
    problems: list[str] = []
    skills = skills_by_name(root)
    for metadata in file_presence.matching(root / "deploy-meta", "*.json"):
        skill = metadata.stem
        document = json_reader.loads(metadata.read_text(encoding="utf-8"))
        declared = set(listed_names(metadata, document, "skill_deps", problems))
        scripts = file_presence.matching(skills[skill] / "scripts", "*.py") if skill in skills else []
        for path, _, tree in python_trees([path for path in scripts if not is_test_script(path)], root, problems):
            problems.extend(
                f"{path.relative_to(root).as_posix()} puts {name}'s scripts on sys.path without declaring "
                f"{name} in skill_deps; see {SKILL_PATHS_DOC}"
                for name in sorted(set(scripts_put_on_path(tree)) - declared - {skill})
            )
    return problems


SIBLING_PATH_ROOTS = ("skills", REPOSITORY_SKILLS, "tools", "tests", "deployer")
SIBLING_LOOKUP_DOC = VALIDATION_DOC


def _sibling_path_files(root: Path) -> list[Path]:
    """The Python under the roots a sibling lookup applies to, outside tests/fixtures, which are other trees."""
    files = [root / "deploy.py"] if file_presence.is_present(root / "deploy.py") else []
    for top in SIBLING_PATH_ROOTS:
        files += [
            path
            for path in file_presence.matching(
                root / top, "**/*.py", skip=lambda folder: folder == root / "tests" / "fixtures"
            )
            if not within(path, root / "tests" / "fixtures")
        ]
    return sorted(files)


def sibling_path_problems(root: Path) -> list[str]:
    """Report a sys.path entry that reaches a sibling skill's scripts other than through ejryan89-skill-core's lookup.

    skill_roots.sibling_scripts knows where a sibling lives, in the checkout and the deployed home alike, so a
    hand-written path is a copy of that layout that breaks when it changes. The line that puts ejryan89-skill-core on
    sys.path is the one hand-written entry, since it finds the lookup, and a lookup's name is a literal so the
    skill_deps check can read it.
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
            elif entry.hand_written and entry.name not in {SKILL_CORE, own}:
                problems.append(
                    f"{where} puts {entry.name}'s scripts on sys.path by a hand-written path; put {SIBLING_LOOKUP}(\""
                    f'{entry.name}") from skill_roots there instead; see {SIBLING_LOOKUP_DOC}'
                )
            elif not entry.hand_written and entry.name == SKILL_CORE:
                problems.append(
                    f"{where} looks up {SKILL_CORE} through {SIBLING_LOOKUP}; the line that puts it on sys.path "
                    f"already does; see {SIBLING_LOOKUP_DOC}"
                )
    return problems


def fixture_source_problems(root: Path) -> list[str]:
    """Report a fixture source under tests/fixtures that could ship, collide with what ships, or break the contract.

    A fixture source, such as the runtime canary's, is deployed only into a throwaway home. It must keep its own
    source ID and skill names, the shipped source must never name it, and its skills follow the path contract.
    """
    shipped = json_reader.loads((root / "source.json").read_text(encoding="utf-8"))
    found: list[str] = []
    bundles = skill_menu.bundle_definitions(shipped.get("bundles"))
    if bundles is None:
        found.append("source.json bundles is not a JSON object, which the deployer refuses")
    names = {path.stem for path in file_presence.matching(root / "deploy-meta", "*.json")} | set(bundles or {})
    for source_file in file_presence.matching(root / "tests" / "fixtures", "*/source.json"):
        fixture = source_file.parent
        label = fixture.relative_to(root).as_posix()
        try:
            source = json_reader.loads(source_file.read_text(encoding="utf-8"))
        # ValueError covers the decode errors, UnicodeError, and json_reader's refusals, nesting too deep included
        except (OSError, ValueError) as exc:
            found.append(f"{label}/source.json cannot be read: {exc}")
        else:
            if not isinstance(source, dict):
                found.append(f"{label}/source.json is not a JSON object")
            elif source.get("id") == shipped.get("id"):
                found.append(f"{label}/source.json reuses the shipped source ID")
        found.extend(
            f"{label} skill {metadata.stem} shares its name with a shipped skill or bundle"
            for metadata in file_presence.matching(fixture / "deploy-meta", "*.json")
            if metadata.stem in names
        )
        found += [f"{label}: {problem}" for problem in skill_path_problems(fixture)]
    shipped_files = [
        root / "source.json",
        *(
            path
            for folder in ("skills", "deploy-meta", "agents")
            for path in file_presence.matching(root / folder, "**/*")
            if file_presence.is_present(path, other_is_absent=True)
        ),
    ]
    for path in shipped_files:
        if "tests/fixtures" in path.read_text(encoding="utf-8", errors="replace").replace("\\", "/"):
            found.append(f"{path.relative_to(root).as_posix()} names tests/fixtures, which never ships")
    return found


PLUGIN_DOC = '"Validation" in docs/adding-a-skill.md'
# What a template token becomes in a plugin, and where a hook finds the script the deployed agent's hook names under
# the home, pinned here rather than read from tools/plugin_build.py, so a change to the tool's answers fails this
# policy until it is made here too. A pinned contract value, as "Validation" in docs/implementing-changes.md allows:
# the plugin decision on REPOS_ROOT in docs/design.md fixes these answers.
PLUGIN_ANSWERS = {
    b"{{PYTHON}}": b"python",
    b"{{REPOS_ROOT}}": b"${user_config.repos_root}",
    b"{{REPO_CLEANUP_REGENERABLE}}": b"",
    b"{{REPO_CLEANUP_PROTECTED}}": b"",
}
# Claude Code substitutes a plugin option only below a file's frontmatter, so a skill's frontmatter names it in words.
PLUGIN_FRONTMATTER_ANSWERS = {b"{{REPOS_ROOT}}": b"your repositories root"}
# A Markdown file's frontmatter as YAML delimits it, each line ending at CR, LF, or CRLF.
PLUGIN_FRONTMATTER = re.compile(rb"\A---[ \t]*(?:\r\n|\r|\n)(?:.*?(?:\r\n|\r|\n))??---[ \t]*(?=[\r\n]|\Z)", re.DOTALL)
# The plugin option a skill carrying its placeholder needs its plugin's manifest to declare.
PLUGIN_OPTION_PLACEHOLDERS = {"repos_root": b"${user_config.repos_root}"}
PLUGIN_HOOK_PATHS = {
    "os.path.expanduser('~/.claude/skills/ejryan89-code-review-core/scripts/review_guard.py')": (
        "os.path.join(os.environ['CLAUDE_PLUGIN_ROOT'], 'skills', 'ejryan89-code-review-core', 'scripts', "
        "'review_guard.py')"
    ),
}
# How a plugin hook that starts Python by the pinned answer starts it, with its arguments in place of {0}: sh, which
# runs a hook on macOS and Linux and as Git Bash on Windows, joins each `set -- \` to the next line and starts python,
# or python3 where it finds no python, and PowerShell, which runs a hook on Windows without Git Bash, sets the exit
# status to 1, which a missing python leaves, starts python, and exits with its status. Pinned here rather than read
# from skill_menu, as PLUGIN_ANSWERS is.
PLUGIN_HOOK_START = (
    "set -- \\\n$global:LASTEXITCODE = 1\nset -- \\\npython {0}\nset -- \\\nexit $LASTEXITCODE\n"
    'p=$(command -v python); exec "${{p:-python3}}" {0}'
)
PLUGIN_TOKEN = re.compile(rb"\{\{[A-Z_][A-Z0-9_]*\}\}")


def _plugin_source(root: Path, path: str) -> tuple[Path, str] | str:
    """The source file a plugin path comes from, with the skill it belongs to or "" for an agent, or why it has
    none."""
    parts = path.split("/")
    if len(parts) == 2 and parts[0] == "agents" and parts[1].endswith(".md"):
        return root / "agents" / parts[1], ""
    if len(parts) >= 3 and parts[0] == "skills" and not is_suite_name(parts[-1]):
        return root.joinpath(*parts), parts[1]
    return "comes from no skill file or agent a deployment ships"


def _plugin_source_file(root: Path, path: str) -> tuple[Path, str] | str:
    """The source file a plugin path comes from, with the skill it belongs to, or why it has none: no skill file or
    agent a deployment ships, nothing at its source, or a source of another kind or one a stat cannot examine, each
    named so."""
    origin = _plugin_source(root, path)
    if isinstance(origin, str):
        return origin
    reason = file_presence.refusal_reason(origin[0])
    if reason == file_presence.MISSING:
        return "has no source file"
    return origin if reason is None else f"has a source that {reason}"


def _answered(content: bytes, *, markdown: bool = False) -> bytes:
    """The source's bytes with the pinned answers, and in a Markdown file's frontmatter the pinned words first."""
    head = PLUGIN_FRONTMATTER.match(content) if markdown else None
    if head is not None:
        front = head.group(0)
        for token, answer in PLUGIN_FRONTMATTER_ANSWERS.items():
            front = front.replace(token, answer)
        content = front + content[head.end() :]
    for token, answer in PLUGIN_ANSWERS.items():
        content = content.replace(token, answer)
    return content


def _without_hooks(text: str) -> tuple[str, str]:
    """An agent definition without its frontmatter's top-level hooks key, and that key's lines, on one line each.

    Lines end where YAML ends one, at CR, LF, and CRLF, and each kept line keeps the break it was written with, as
    the plugin build keeps it. Limit: the delimiters are found by exact match, so an agent whose `---` lines carry
    trailing spaces or tabs is compared with its hooks kept; no agent the repository holds is written so."""
    lines, breaks = lines_and_breaks(text)
    closing = lines.index("---", 1) if lines[:1] == ["---"] and "---" in lines[1:] else 0
    start = next((index for index in range(1, closing) if lines[index].startswith("hooks:")), None)
    if start is None:
        return text, ""
    end = next((index for index in range(start + 1, closing) if lines[index][:1] not in ("", " ")), closing)
    kept = [*range(start), *range(end, len(lines))]
    return "".join(lines[index] + breaks[index] for index in kept), " ".join(" ".join(lines[start:end]).split())


def _hook_problems(name: str, hooks: bytes, declared: str) -> list[str]:
    """What a plugin's hooks file holds that its agents' declared hooks, with the pinned paths, do not."""
    try:
        document = json_reader.loads(hooks)
        entries = [entry for event in document["hooks"].values() for entry in event]
        handlers = [(entry["matcher"], handler) for entry in entries for handler in entry["hooks"]]
    except (ValueError, KeyError, TypeError, AttributeError):
        return [f"plugin {name}: {PLUGIN_HOOKS} is not a hooks file of matchers and handlers"]
    expected = _answered(declared.encode()).decode()
    for deployed, plugin in PLUGIN_HOOK_PATHS.items():
        expected = expected.replace(deployed, plugin)
    found = []
    for matcher, handler in handlers:
        command = str(handler.get("command", ""))
        arguments = command.rpartition('"${p:-python3}" ')[2]
        if PLUGIN_HOOK_START.format(arguments) == command:
            command = f"python {arguments}"
        elif command.split()[:1] == ["python"]:
            found.append(
                f"plugin {name}: {PLUGIN_HOOKS} starts python by that name alone, which a macOS or Linux install of "
                "Python may not give, so the hook blocks every call it matches there"
            )
        command = " ".join(command.split())
        fields = [f'matcher: "{matcher}"', f"command: >- {command}"]
        fields += [f"{key}: {handler[key]}" for key in ("type", "timeout", "onFailure") if key in handler]
        found += [
            f"plugin {name}: {PLUGIN_HOOKS} holds {field[:60]}, which no agent declares"
            for field in fields
            if field not in expected
        ]
    return found


def _manifest_problems(root: Path, name: str, files: Mapping[str, bytes], version: str) -> list[str]:
    """What a plugin's manifest holds but its marketplace entry, its name, the version it was built with, and the
    userConfig declaring each plugin option a skill it carries reads do not."""
    manifest = files.get(PLUGIN_MANIFEST, b"")
    options = {
        option: skill_menu.PLUGIN_OPTIONS.get(option)
        for option, placeholder in PLUGIN_OPTION_PLACEHOLDERS.items()
        if any(path.endswith("/SKILL.md") and placeholder in content for path, content in files.items())
    }
    catalogue = json_reader.loads((root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    entry = next((entry for entry in catalogue.get("plugins", []) if entry.get("name") == name), None)
    try:
        document = json_reader.loads(manifest)
    except ValueError:
        document = None
    if entry is None or not isinstance(document, dict):
        return [f"plugin {name}: {PLUGIN_MANIFEST} is no manifest of a plugin the marketplace lists"]
    kept = {key: value for key, value in entry.items() if key not in ("name", "version", "source")}
    expected = {**kept, "name": name, "version": version}
    if options:
        expected["userConfig"] = options
    if document == expected:
        return []
    if {key: value for key, value in document.items() if key != "userConfig"} == {
        key: value for key, value in expected.items() if key != "userConfig"
    }:
        return [f"plugin {name}: {PLUGIN_MANIFEST} must declare userConfig for {sorted(options) or 'no option'}"]
    return [f"plugin {name}: {PLUGIN_MANIFEST} differs from its marketplace entry"]


def plugin_tree_problems(root: Path, plugins: Mapping[str, Mapping[str, bytes]], version: str) -> list[str]:
    """Report what an assembled plugin holds that its sources do not: a file no skill or agent ships, bytes that
    differ from the source's but for the pinned token answers, an agent's hooks, which move to the plugin's hooks
    file, and each backticked mention of an agent the plugin carries, which names it as `<plugin>:<agent>`; a template
    token left; a file of a carried skill, as the builder reads it, that the plugin leaves out; a manifest that is not
    its marketplace entry with the plugin's name and `version`, the one it was built with; and a hooks file that is not
    its agents' hooks. `plugins` maps each plugin's name to its files, by path in the plugin."""
    if file_presence.is_there(root / PLUGIN_MANIFEST):
        return [f"{PLUGIN_MANIFEST} makes the checkout's skills read as a plugin's"]
    found: list[str] = []
    for name, files in sorted(plugins.items()):
        carried: set[str] = set()
        agents = [path[len("agents/") : -len(".md")] for path in files if path.startswith("agents/")]
        declared = ""
        readme = root / PLUGIN_README
        has_readme = file_presence.is_present(readme, other_is_absent=True)
        if has_readme and files.get("README.md") != readme.read_bytes():
            found.append(f"plugin {name}: README.md is not {PLUGIN_README.as_posix()}")
        for path, content in sorted(files.items()):
            if path in (PLUGIN_MANIFEST, PLUGIN_HOOKS) or (path == "README.md" and has_readme):
                continue
            origin = _plugin_source_file(root, path)
            if isinstance(origin, str):
                found.append(f"plugin {name}: {path} {origin}")
                continue
            source, skill = origin
            carried.add(skill)
            expected = _answered(source.read_bytes(), markdown=path.endswith(".md"))
            if not skill:
                kept, hooks = _without_hooks(expected.decode("utf-8"))
                expected, declared = kept.encode("utf-8"), f"{declared} {hooks}"
            elif path.endswith("/SKILL.md"):
                for agent in agents:
                    expected = expected.replace(f"`{agent}`".encode(), f"`{name}:{agent}`".encode())
            if PLUGIN_TOKEN.search(content):
                found.append(f"plugin {name}: {path} keeps a template token")
            elif content != expected:
                found.append(f"plugin {name}: {path} differs from {source.relative_to(root).as_posix()}")
        found += _manifest_problems(root, name, files, version)
        if PLUGIN_HOOKS in files:
            found += _hook_problems(name, files[PLUGIN_HOOKS], declared)
        elif declared.strip():
            found.append(f"plugin {name}: its agents declare hooks, but it has no {PLUGIN_HOOKS}")
        for skill in sorted(carried - {""}):
            shipped = {f"skills/{skill}/{relative}" for relative in shipped_files(root / "skills" / skill)}
            found.extend(f"plugin {name}: leaves out {path}" for path in sorted(shipped - set(files)))
    return found


# A marketplace entry's source: the release asset of one release tag, under the entry's own repository, pinned by a
# sha256 once the release has attached it.
RELEASE_ASSET = re.compile(
    r"(?P<repository>https://[^\s]+)/releases/download/(?P<tag>v\d+\.\d+\.\d+)/(?P<name>[a-z0-9-]+)\.zip"
)
SHA256 = re.compile(r"[0-9a-f]{64}")


def marketplace_problems(root: Path) -> list[str]:
    """Report a marketplace entry whose source is not its own release asset of the one release every entry names,
    with no digest or a sha256 one."""
    document = json_reader.loads((root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    found, tags = [], set()
    for entry in document.get("plugins", []):
        name, source = entry.get("name"), entry.get("source")
        match = RELEASE_ASSET.fullmatch(source.get("url", "")) if isinstance(source, dict) else None
        if not isinstance(source, dict) or source.get("source") != "archive" or match is None:
            found.append(f"marketplace entry {name}: its source is not an archive at a release asset")
            continue
        tags.add(match.group("tag"))
        if match.group("name") != name or match.group("repository") != entry.get("repository"):
            found.append(f"marketplace entry {name}: its asset is not its own under its repository")
        if "sha256" in source and not SHA256.fullmatch(str(source["sha256"])):
            found.append(f"marketplace entry {name}: its sha256 is not a digest")
        if set(source) - {"source", "url", "sha256"}:
            found.append(f"marketplace entry {name}: its source holds more than an archive's url and sha256")
    if len(tags) > 1:
        found.append(f"the marketplace names assets of more than one release: {', '.join(sorted(tags))}")
    return found


TRIGGER_SCENARIO_DOC = '"Skill evaluations" in docs/skill-evaluations.md'
# The mode of a scenario that judges whether a description starts its skill, and the requests one holds at least: a
# description's "Use it when" sentence names several ways a user asks for the skill, and one request samples one.
TRIGGER_MODE = "trigger"
MINIMUM_TRIGGER_REQUESTS = 3


def _trigger_problem(spec: dict[str, object], skill: str, shipped: dict[str, bool]) -> str | None:
    """Why a trigger scenario under the skill's folder cannot judge that skill's description, or None; `shipped` says
    of each shipped skill whether only the user may start it."""
    named, requests, near_misses = spec.get("skill"), spec.get("requests"), spec.get("near_misses")
    if named != skill:
        return f"names skill {json_writer.dumps(named)}, not its folder's, {skill}"
    if skill not in shipped:
        return f"names {skill}, which is no skill under skills/"
    if shipped[skill]:
        return f"names {skill}, which sets disable-model-invocation, so no request can start it"
    if not isinstance(requests, list) or len(requests) < MINIMUM_TRIGGER_REQUESTS:
        held = len(requests) if isinstance(requests, list) else 0
        return f"holds {held} requests, fewer than {MINIMUM_TRIGGER_REQUESTS}"
    if not isinstance(near_misses, list) or not near_misses:
        return "holds no near miss"
    # A neighbour's request shows only that the description beats its neighbour's; one no skill serves shows that it
    # does not take whatever comes.
    unserved = spec.get("unserved")
    if not isinstance(unserved, list) or not unserved:
        return "holds no unserved request, a near miss no skill serves"
    return None


def trigger_scenario_problems(root: Path) -> list[str]:
    """Report a shipped skill the model may start with no trigger scenario, and a trigger scenario that cannot judge
    its skill: one naming another folder's skill, a skill not under skills/, or a user-only one, or holding fewer than
    MINIMUM_TRIGGER_REQUESTS requests, no neighbour's near miss, or no unserved one.

    Only a run that calls models judges whether a description starts its skill, so validation never imports or starts
    the evaluation; it holds that every description the model reads has a scenario to be judged by, and the
    evaluation's suite loads each committed one as a run does.
    """
    shipped = {name: user_only(frontmatter.read(path / "SKILL.md")) for name, path in skills_by_name(root).items()}
    scenarios = root / "tests" / "fixtures" / "skill-evals"
    problems: list[str] = []
    covered: set[str] = set()
    for spec_file in file_presence.matching(scenarios, "*/*/scenario.json"):
        label = spec_file.parent.relative_to(root).as_posix()
        try:
            spec = json_reader.loads(spec_file.read_text(encoding="utf-8"))
        # ValueError covers the decode errors, UnicodeError, and json_reader's refusals, nesting too deep included
        except (OSError, ValueError) as exc:
            problems.append(f"{label}/scenario.json cannot be read: {exc}")
            continue
        if not isinstance(spec, dict) or spec.get("mode") != TRIGGER_MODE:
            continue
        skill = spec_file.parent.parent.name
        problem = _trigger_problem(spec, skill, shipped)
        if problem is None:
            covered.add(skill)
        else:
            problems.append(f"{label} {problem}; see {TRIGGER_SCENARIO_DOC}")
    for name, only_by_user in sorted(shipped.items()):
        if name not in covered and not only_by_user:
            problems.append(
                f"skills/{name} may be started by the model and has no trigger scenario under "
                f"{scenarios.relative_to(root).as_posix()}/{name}/; see {TRIGGER_SCENARIO_DOC}"
            )
    return problems


def _starts_with_a_shebang(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(2) == b"#!"


def unsupported_script_problems(root: Path) -> list[str]:
    """Report each JavaScript, TypeScript, PowerShell module, or Windows batch file anywhere in a shipped or repository
    skill, scripts/ included, and each file that starts with #! without an executable extension, which runs as a
    program no analyzer or command policy reads."""
    problems: list[str] = []
    files = [
        path
        for skill in all_skill_directories(root)
        for path in file_presence.matching(skill, "**/*")
        if file_presence.is_present(path, other_is_absent=True)
    ]
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


# A file extension a code span names, matched against the span's whole text.
EXTENSION = re.compile(r"\.[a-z0-9]+")


def _extensions(text: str) -> set[str]:
    """The extensions the code spans of `text` name, each span read through code_spans."""
    return {span.text for span in code_spans(text) if EXTENSION.fullmatch(span.text)}


def script_language_problems(root: Path, standard_commands: set[str]) -> list[str]:
    """Report where the skill guide or the tool catalogue disagrees with the runner on a skill's script languages."""
    found = [
        f"deployer/tools.py does not list `{interpreter}`, which runs `{extension}` scripts"
        for extension, interpreter in sorted(SCRIPT_INTERPRETERS.items())
        if interpreter not in standard_commands
    ]
    statement = "A skill's executable files are Bash, Python, and PowerShell"
    line = next(
        (line for line in text_lines((root / SKILL_GUIDE).read_text(encoding="utf-8")) if statement in line), None
    )
    if line is None:
        return [*found, f"{SKILL_GUIDE} does not state which languages a skill's executable files may use"]
    supported, _, unsupported = line.partition("JavaScript and TypeScript")
    named = _extensions(supported)
    found += [
        f"{SKILL_GUIDE} names `{extension}` as an executable extension"
        for extension in sorted(named - EXECUTABLE_SCRIPT_EXTENSIONS)
    ]
    found += [
        f"{SKILL_GUIDE} does not name `{extension}` as executable"
        for extension in sorted(EXECUTABLE_SCRIPT_EXTENSIONS - named)
    ]
    rejected = _extensions(unsupported)
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
        for path in file_presence.matching(skill, "**/*", skip=lambda folder: folder.name == BYTECODE_FOLDER):
            inside = path.relative_to(skill)
            if not file_presence.is_present(path, other_is_absent=True) or BYTECODE_FOLDER in inside.parts:
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
    if not file_presence.is_directory(skills):
        return []
    source = root / "source.json"
    shared = (
        json_reader.loads(source.read_text(encoding="utf-8")).get("shared_assets", {})
        if file_presence.is_present(source)
        else {}
    )
    problems: list[str] = []
    for directory in sorted(
        path for path in skills.iterdir() if file_presence.is_directory(path, other_is_absent=True)
    ):
        shown = directory.relative_to(root).as_posix()
        # A SKILL.md of another kind is named so, never as a folder that holds none, and one a stat cannot examine
        # raises. Either way validation fails closed: a validation reader either raises there, as this one does, or
        # records a problem naming the path as one that cannot be examined, with the reason, as
        # capability_guide_problems does.
        try:
            present = file_presence.is_present(directory / "SKILL.md")
        except file_presence.NotAFile:
            problems.append(f"{shown}/SKILL.md is not a file")
            continue
        if not present and directory.name not in shared:
            problems.append(f"{shown} holds no SKILL.md and source.json declares no such shared asset")
    problems += [
        f"{skill_md.relative_to(root).as_posix()} is deeper than skills/<name>"
        for skill_md in file_presence.matching(skills, "**/SKILL.md")
        if len(skill_md.relative_to(skills).parts) > 2
    ]
    return problems


# Where each repository skill's shim for Codex and Copilot sits, a folder named as the skill's.
REPOSITORY_SKILL_SHIMS = ".agents/skills"
SKILL_NAMES_DOC = '"Files" in docs/adding-a-skill.md'


def skill_file_spelling_problems(root: Path) -> list[str]:
    """Report a shipped skill's, a repository skill's, or a shim's main file spelled in another case than SKILL.md.

    A runtime opens SKILL.md, which such a file is only where its folder folds case, while validation, the plugin
    build, and the shims find a skill by SKILL.md exactly, so each tree holds that one spelling on every platform, as
    deployer/source.py refuses another for a shipped skill. The files are found by a pattern that matches the name in
    any case, so one is reported on a file system that keeps case as on one that folds it.
    """
    from deployer import source as deploy_source

    return [
        f"{path.relative_to(root).as_posix()} is spelled in another case than {SKILL_FILE}; rename it {SKILL_FILE}"
        for tree in ("skills", REPOSITORY_SKILLS, REPOSITORY_SKILL_SHIMS)
        for path in file_presence.matching(root / tree, f"*/{deploy_source.any_case(SKILL_FILE)}")
        if path.name != SKILL_FILE
    ]


def repository_skill_name_problems(root: Path) -> list[str]:
    """Report a repository skill or shim whose folder breaks the skill naming rules the deployer holds a shipped
    skill to, or whose frontmatter `name` is not its folder's name.

    A runtime lists a skill by its frontmatter name and the shim, the cost audit, and validation find it by its
    folder, so the two must agree; tools/new_skill.py --repository refuses the same names when it scaffolds one.
    """
    from deployer import source as deploy_source

    problems: list[str] = []
    for tree in (REPOSITORY_SKILLS, REPOSITORY_SKILL_SHIMS):
        for skill_md in sorted(file_presence.matching(root / tree, "*/SKILL.md"), key=lambda path: path.parent.name):
            name, where = skill_md.parent.name, skill_md.relative_to(root).as_posix()
            if not deploy_source.is_valid_name(name):
                problems.append(
                    f"{where}: {name!r} is not a skill name: at most {MAX_NAME} lowercase letters, digits, and "
                    "hyphens, no leading, trailing, or consecutive hyphens, and no Windows device name; see "
                    f"{SKILL_NAMES_DOC}"
                )
            elif word := deploy_source.reserved_word(name):
                problems.append(
                    f"{where}: {name!r} contains the reserved word {word!r}, {deploy_source.AGENT_SKILLS_RULE}; see "
                    f"{SKILL_NAMES_DOC}"
                )
            try:
                declared = frontmatter.read(skill_md).string("name")
            except (OSError, UnicodeError, frontmatter.FrontmatterError) as exc:
                problems.append(f"{where}: its frontmatter cannot be read: {exc}")
                continue
            if declared != name:
                shown = "no name" if declared is None else f"name {declared!r}"
                problems.append(f"{where} declares {shown}, not its folder's name {name!r}; see {SKILL_NAMES_DOC}")
    return problems


def shared_guidance_problems(root: Path) -> list[str]:
    """Report a shipped skill whose SKILL.md names a shared Markdown asset from source.json.

    Claude runs a skill directly; only the generated ~/.agents adapter tells other runtimes to read shared Markdown
    guidance first, so a skill that points to it spends a turn on every Claude run.
    """
    shared = json_reader.loads((root / "source.json").read_text(encoding="utf-8")).get("shared_assets", {})
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

    def test_every_skill_file_is_spelled_skill_md(self) -> None:
        for tree in ("skills", REPOSITORY_SKILLS, REPOSITORY_SKILL_SHIMS):
            self.assertTrue(list(file_presence.matching(REPOSITORY_ROOT / tree, "*/SKILL.md")), tree)
        self.assertEqual([], skill_file_spelling_problems(REPOSITORY_ROOT))

    def test_repository_skills_and_their_shims_are_named_as_skills_and_as_their_folders(self) -> None:
        self.assertTrue(list(file_presence.matching(REPOSITORY_ROOT / REPOSITORY_SKILL_SHIMS, "*/SKILL.md")))
        self.assertEqual([], repository_skill_name_problems(REPOSITORY_ROOT))

    def test_skill_markdown_does_not_embed_programs(self) -> None:
        self.assertEqual([], embedded_program_problems(REPOSITORY_ROOT))

    def test_skills_leave_shared_runtime_guidance_to_their_adapters(self) -> None:
        shared = json_reader.loads((REPOSITORY_ROOT / "source.json").read_text(encoding="utf-8"))["shared_assets"]
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
        self.assertTrue(list(file_presence.matching(REPOSITORY_ROOT / "tests" / "fixtures", "*/source.json")))
        self.assertEqual([], fixture_source_problems(REPOSITORY_ROOT))

    def test_plugins_hold_their_sources_and_nothing_else(self) -> None:
        from tools import plugin_build

        # The answers are pinned against the policy's own literals, and the clone-bound update skill against a name:
        # a plugin install has no clone, so no plugin may carry the skill that updates one. The code-review skills
        # are carried with their reviewer, whose guard the plugin's hooks file runs.
        self.assertEqual(
            {
                "PYTHON": "python",
                "REPOS_ROOT": "${user_config.repos_root}",
                "REPO_CLEANUP_REGENERABLE": "",
                "REPO_CLEANUP_PROTECTED": "",
            },
            skill_menu.PLUGIN_VALUES,
        )
        # An installed plugin's session was seen substituting a stored option in a skill's body but not its
        # description, so the root is a required option and the frontmatter names it in words. These are pinned
        # contract values, as "Validation" in docs/implementing-changes.md allows, which the plugin decision on
        # REPOS_ROOT in docs/design.md fixes.
        self.assertEqual({"REPOS_ROOT": "your repositories root"}, skill_menu.PLUGIN_FRONTMATTER_VALUES)
        self.assertEqual(
            {"repos_root": {"type": "directory", "title": "Repositories root", "required": True}},
            {
                key: {field: value for field, value in option.items() if field != "description"}
                for key, option in skill_menu.PLUGIN_OPTIONS.items()
            },
        )
        source = plugin_build.load_source(REPOSITORY_ROOT)
        catalogue = plugin_build.load_catalogue(REPOSITORY_ROOT)
        assembly = plugin_build.assemble(source, catalogue, readme=plugin_build.load_readme(REPOSITORY_ROOT))
        carried = {skill for plugin in assembly.plugins for skill in plugin.skills}
        self.assertNotIn("update-coding-agent-skills", carried)
        self.assertIn("update-coding-agent-skills", assembly.left_out)
        self.assertLessEqual({"review-prs", "ejryan89-code-review-core", "repo-cleanup"}, carried)
        plugins = {plugin.name: plugin.files for plugin in assembly.plugins}
        self.assertIn(PLUGIN_HOOKS, plugins["coding-agent-skills"])
        self.assertEqual(
            [], plugin_tree_problems(REPOSITORY_ROOT, plugins, plugin_build.DEVELOPMENT_VERSION), PLUGIN_DOC
        )

    def test_the_marketplace_names_each_plugins_release_asset(self) -> None:
        self.assertEqual([], marketplace_problems(REPOSITORY_ROOT), PLUGIN_DOC)

    def test_every_skill_the_model_may_start_has_a_trigger_scenario(self) -> None:
        self.assertEqual([], trigger_scenario_problems(REPOSITORY_ROOT))

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

    def test_runtime_pages_state_each_skills_support_as_its_metadata_declares(self) -> None:
        self.assertEqual([], runtime_page_problems(REPOSITORY_ROOT))

    def test_runtime_pages_link_a_source_the_guidance_record_rereads_for_each_runtime_statement(self) -> None:
        self.assertEqual([], runtime_statement_source_problems(REPOSITORY_ROOT))

    def test_each_reason_for_a_capability_a_guide_gives_points_to_that_guide(self) -> None:
        self.assertEqual([], capability_guide_problems(REPOSITORY_ROOT))

    def test_skill_scripts_declare_each_sibling_they_put_on_sys_path(self) -> None:
        self.assertEqual([], script_dependency_problems(REPOSITORY_ROOT))

    def test_modules_reach_sibling_scripts_through_skill_core(self) -> None:
        self.assertEqual([], sibling_path_problems(REPOSITORY_ROOT))
