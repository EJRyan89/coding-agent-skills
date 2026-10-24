"""Regression suite for tools/plugin_build.py: assembling the Claude Code plugins from a source tree."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import skill_menu
from skill_roots import SKILLS_ROOT, sibling_scripts

sys.path.insert(0, str(sibling_scripts("audit-skill")))

import skill_inventory

from tools import plugin_agents, plugin_build

AGENT = "---\nname: {name}\ndescription: An agent.\ntools: Read\n{extra}---\n\nRun {{{{PYTHON}}}} -B it.\n"


class Source:
    """A source tree under a folder whose name has spaces, built one skill at a time."""

    def __init__(self, root: Path) -> None:
        self.root = root / "Source With Spaces"
        for folder in ("skills", "deploy-meta", "agents"):
            (self.root / folder).mkdir(parents=True)
        self.document: dict[str, object] = {"id": "test/fixture-skills", "bundles": {}}
        self.plugins = ["fixture-skills"]
        self.save()

    def save(self) -> None:
        (self.root / "source.json").write_text(json.dumps(self.document), encoding="utf-8", newline="\n")
        self.market(*self.plugins)

    def market(self, *names: str) -> None:
        """Write the marketplace file, listing these plugins."""
        entries = [
            {"name": name, "description": f"The {name} plugin.", "source": {"source": "archive"}} for name in names
        ]
        (self.root / ".claude-plugin").mkdir(exist_ok=True)
        (self.root / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps({"name": "fixture", "plugins": entries}), encoding="utf-8", newline="\n"
        )

    def bundle(self, name: str, members: list[str], *, opt_in: bool = False) -> None:
        bundles = self.document["bundles"]
        assert isinstance(bundles, dict)
        bundles[name] = {"members": members, **({"opt_in": True} if opt_in else {})}
        if opt_in:
            self.plugins.append(f"fixture-skills-{name}")
        self.save()

    def skill(self, name: str, body: str = "Body.\n", **metadata: object) -> Path:
        directory = self.root / "skills" / name
        (directory / "scripts").mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Does {name}. Use it when asked.\n---\n\n{body}",
            encoding="utf-8",
            newline="\n",
        )
        meta = {"required_vars": [], "shared_deps": [], **metadata}
        (self.root / "deploy-meta" / f"{name}.json").write_text(json.dumps(meta), encoding="utf-8", newline="\n")
        if metadata.get("opt_in"):
            self.plugins.append(f"fixture-skills-{name}")
            self.save()
        return directory

    def agent(self, name: str, extra: str = "") -> None:
        (self.root / "agents" / f"{name}.md").write_text(
            AGENT.format(name=name, extra=extra), encoding="utf-8", newline="\n"
        )


@pytest.fixture
def source(tmp_path: Path) -> Source:
    return Source(tmp_path)


def assembled(source: Source, version: str = plugin_build.DEVELOPMENT_VERSION) -> plugin_build.Assembly:
    return plugin_build.assemble(
        plugin_build.load_source(source.root), plugin_build.load_catalogue(source.root), version
    )


def by_name(assembly: plugin_build.Assembly) -> dict[str, plugin_build.Plugin]:
    return {plugin.name: plugin for plugin in assembly.plugins}


def test_the_default_selection_is_one_plugin_named_for_the_repository_with_its_closure(source: Source) -> None:
    source.skill("core", selectable=False)
    source.skill("alpha", skill_deps=["core"])
    source.skill("beta")
    source.bundle("pair", ["beta"])
    plugins = by_name(assembled(source))
    assert list(plugins) == ["fixture-skills"]
    assert plugins["fixture-skills"].skills == ["alpha", "beta", "core"]
    assert "skills/core/SKILL.md" in plugins["fixture-skills"].files


def test_each_opt_in_bundle_and_skill_is_a_plugin_of_its_own_with_its_own_closure(source: Source) -> None:
    source.skill("core", selectable=False)
    source.skill("alpha")
    source.skill("dotnet-one", skill_deps=["core"])
    source.skill("solo", opt_in=True, skill_deps=["core"])
    source.bundle("dotnet", ["dotnet-one"], opt_in=True)
    plugins = by_name(assembled(source))
    assert list(plugins) == ["fixture-skills", "fixture-skills-dotnet", "fixture-skills-solo"]
    assert plugins["fixture-skills"].skills == ["alpha"]
    assert plugins["fixture-skills-dotnet"].skills == ["core", "dotnet-one"]
    assert plugins["fixture-skills-solo"].skills == ["core", "solo"]


def test_a_skill_ships_without_its_suites_and_with_python_answered_where_it_declares_it(source: Source) -> None:
    directory = source.skill("alpha", body="Run {{PYTHON}} -B tool.\n", required_vars=["PYTHON"])
    (directory / "scripts" / "tool.py").write_text("print('{{PYTHON}}')\n", encoding="utf-8", newline="\n")
    (directory / "scripts" / "test_tool.py").write_text("def test_it():\n    pass\n", encoding="utf-8", newline="\n")
    (directory / "scripts" / "data.bin").write_bytes(b"\xff\x00{{")
    files = by_name(assembled(source))["fixture-skills"].files
    assert sorted(files) == [
        ".claude-plugin/plugin.json",
        "skills/alpha/SKILL.md",
        "skills/alpha/scripts/data.bin",
        "skills/alpha/scripts/tool.py",
    ]
    assert files["skills/alpha/SKILL.md"].endswith(b"Run python -B tool.\n")
    assert files["skills/alpha/scripts/tool.py"] == b"print('python')\n"
    assert files["skills/alpha/scripts/data.bin"] == b"\xff\x00{{"


def test_a_token_a_skill_does_not_declare_is_refused_rather_than_shipped(source: Source) -> None:
    source.skill("alpha", body="Run {{PYTHON}} -B tool.\n")
    with pytest.raises(plugin_build.BuildError, match=r"keeps template tokens: skills/alpha/SKILL.md:\d+:Run"):
        assembled(source)


def test_an_item_needing_a_value_with_no_plugin_answer_is_left_out_with_its_dependents(source: Source) -> None:
    source.skill("rooted", selectable=False, required_vars=["SOURCE_ROOT"])
    source.skill("alpha", skill_deps=["rooted"])
    source.skill("beta")
    assembly = assembled(source)
    assert by_name(assembly)["fixture-skills"].skills == ["beta"]
    assert assembly.left_out == {"alpha": "rooted takes {{SOURCE_ROOT}}, which a plugin has no value for"}


ROOTED = (
    "---\nname: rooted\ndescription: Sweeps the repos under {{REPOS_ROOT}}. Use it when asked.\n---\n\n"
    'Run {{PYTHON}} -B sweep.py --repos-root "{{REPOS_ROOT}}" --protected "{{REPO_CLEANUP_PROTECTED}}" '
    '--regenerable "{{REPO_CLEANUP_REGENERABLE}}".\n'
)


def test_the_repositories_root_is_the_plugin_option_in_the_body_and_a_phrase_in_the_frontmatter(
    source: Source,
) -> None:
    # Claude Code substitutes a stored plugin option in a skill's body but leaves the frontmatter's placeholder as
    # written, so the description names the root in words.
    variables = ["PYTHON", "REPOS_ROOT", "REPO_CLEANUP_PROTECTED", "REPO_CLEANUP_REGENERABLE"]
    directory = source.skill("rooted", required_vars=variables)
    (directory / "SKILL.md").write_text(ROOTED, encoding="utf-8", newline="\n")
    solo = source.skill("solo", opt_in=True)
    # A script naming the option as text is no reason to declare it; only a variable the skill takes is.
    (solo / "scripts" / "names.py").write_text('OPTION = "${user_config.repos_root}"\n', encoding="utf-8")
    plugins = by_name(assembled(source))
    assert plugins["fixture-skills"].files["skills/rooted/SKILL.md"].decode("utf-8") == (
        "---\nname: rooted\ndescription: Sweeps the repos under your repositories root. Use it when asked.\n---\n\n"
        'Run python -B sweep.py --repos-root "${user_config.repos_root}" --protected "" --regenerable "".\n'
    )
    manifest = json.loads(plugins["fixture-skills"].files[skill_menu.PLUGIN_MANIFEST])
    assert manifest["userConfig"] == {
        "repos_root": {
            "type": "directory",
            "title": "Repositories root",
            "description": "The absolute path of the folder holding your git repositories, which repo-cleanup "
            "sweeps. It is used as typed.",
            "required": True,
        }
    }
    assert "userConfig" not in json.loads(plugins["fixture-skills-solo"].files[skill_menu.PLUGIN_MANIFEST])


def test_a_plugin_value_naming_an_option_no_plugin_declares_is_a_build_error(
    source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    variables = ["PYTHON", "REPOS_ROOT", "REPO_CLEANUP_PROTECTED", "REPO_CLEANUP_REGENERABLE"]
    directory = source.skill("rooted", required_vars=variables)
    (directory / "SKILL.md").write_text(ROOTED, encoding="utf-8", newline="\n")
    monkeypatch.setattr(plugin_build, "PLUGIN_OPTIONS", {})
    with pytest.raises(plugin_build.BuildError, match=r"^no plugin option is declared for repos_root$"):
        assembled(source)


GUARD_HOOK = (
    "hooks:\n"
    "  PreToolUse:\n"
    '    - matcher: "Read|Bash"\n'
    "      hooks:\n"
    "        - type: command\n"
    "          command: >-\n"
    '            {{PYTHON}} -I -c "import os, runpy;\n'
    "            runpy.run_path(os.path.expanduser('~/.claude/skills/core/scripts/guard.py'))\"\n"
    "          timeout: 30\n"
    "          onFailure: block\n"
)


def test_an_agent_ships_answered_and_namespaced_where_a_skill_names_it(source: Source) -> None:
    source.agent("plain")
    source.skill("alpha", body="Start a subagent of type `plain`, not plain.\n", agent_deps=["plain"])
    plugin = by_name(assembled(source))["fixture-skills"]
    assert plugin.agents == ["plain"]
    assert plugin.files["agents/plain.md"].endswith(b"Run python -B it.\n")
    assert plugin.files["skills/alpha/SKILL.md"].endswith(
        b"Start a subagent of type `fixture-skills:plain`, not plain.\n"
    )
    assert skill_menu.PLUGIN_HOOKS not in plugin.files


def test_an_agents_hooks_move_into_the_plugins_hooks_file_with_its_scripts_under_the_plugin(source: Source) -> None:
    source.agent("guarded", extra=GUARD_HOOK)
    source.skill("beta", agent_deps=["guarded"])
    source.bundle("reviews", ["beta"])
    assembly = assembled(source)
    assert assembly.left_out == {}
    plugin = by_name(assembly)["fixture-skills"]
    agent = plugin.files["agents/guarded.md"].decode()
    assert agent == "---\nname: guarded\ndescription: An agent.\ntools: Read\n---\n\nRun python -B it.\n"
    command = (
        'python -I -c "import os, runpy; runpy.run_path(os.path.join(os.environ['
        "'CLAUDE_PLUGIN_ROOT'], 'skills', 'core', 'scripts', 'guard.py'))\""
    )
    assert json.loads(plugin.files[skill_menu.PLUGIN_HOOKS]) == {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Read|Bash",
                    "hooks": [{"type": "command", "command": command, "timeout": 30, "onFailure": "block"}],
                }
            ]
        }
    }


@pytest.mark.parametrize(
    ("hook", "problem"),
    [
        (GUARD_HOOK.replace("os.path.expanduser('~/.claude/skills/", "('~/.claude/skills/"), "home's Claude folder"),
        ("hooks: none\n", "not a block mapping of event names"),
        ("hooks:\n  PreToolUse: {matcher: Read}\n", "agent guarded: hooks:"),
        ("hooks:\n  PreToolUse:\n    - matcher: Read\n    matcher: Bash\n", "share an indentation"),
    ],
)
def test_hooks_a_plugin_cannot_carry_as_written_are_refused(source: Source, hook: str, problem: str) -> None:
    source.agent("guarded", extra=hook)
    source.skill("beta", agent_deps=["guarded"])
    with pytest.raises(plugin_build.BuildError, match=problem):
        assembled(source)


def test_the_manifest_is_the_marketplace_entry_with_the_name_and_version_and_no_source(source: Source) -> None:
    source.skill("alpha")
    manifest = json.loads(by_name(assembled(source, "1.2.3"))["fixture-skills"].files[skill_menu.PLUGIN_MANIFEST])
    assert manifest == {"name": "fixture-skills", "version": "1.2.3", "description": "The fixture-skills plugin."}
    default = json.loads(by_name(assembled(source))["fixture-skills"].files[skill_menu.PLUGIN_MANIFEST])
    assert default["version"] == "0.0.0-dev"


@pytest.mark.parametrize(
    ("listed", "problem"),
    [
        ((), "the marketplace lists no plugin, but the source makes fixture-skills"),
        (("fixture-skills", "other"), "lists fixture-skills, other, but the source makes fixture-skills"),
    ],
)
def test_the_marketplace_must_name_exactly_the_plugins_the_source_makes(
    source: Source, listed: tuple[str, ...], problem: str
) -> None:
    source.skill("alpha")
    source.market(*listed)
    with pytest.raises(plugin_build.BuildError, match=problem):
        assembled(source)


@pytest.mark.parametrize(
    "document", ['{"plugins": [{"name": "a"}, {"name": "a"}]}', '{"plugins": [{}]}', '{"plugins": {}}', "[]", "{"]
)
def test_a_marketplace_that_lists_no_plugin_once_by_name_is_refused(source: Source, document: str) -> None:
    (source.root / ".claude-plugin" / "marketplace.json").write_text(document, encoding="utf-8")
    with pytest.raises(plugin_build.BuildError, match=r"marketplace.json (must list|cannot be read)"):
        plugin_build.load_catalogue(source.root)


def test_a_source_tree_holding_a_plugin_manifest_is_refused(source: Source) -> None:
    (source.root / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    with pytest.raises(plugin_build.BuildError, match="a source tree is never a plugin itself"):
        plugin_build.load_catalogue(source.root)


def test_a_source_the_deployer_refuses_is_a_build_error(source: Source) -> None:
    source.skill("alpha", skill_deps=["missing"])
    with pytest.raises(plugin_build.BuildError, match="missing"):
        assembled(source)


def test_an_archive_depends_on_its_files_alone_and_holds_each_with_a_fixed_time_and_mode() -> None:
    first = plugin_build.archive_bytes({"b/two.txt": b"2", "a/one.md": b"1"})
    second = plugin_build.archive_bytes({"a/one.md": b"1", "b/two.txt": b"2"})
    assert first == second
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        entries = archive.infolist()
        assert [entry.filename for entry in entries] == ["a/one.md", "b/two.txt"]
        assert {(entry.date_time, entry.external_attr >> 16, entry.compress_type) for entry in entries} == {
            ((1980, 1, 1, 0, 0, 0), 0o100644, zipfile.ZIP_STORED)
        }
        assert archive.read("b/two.txt") == b"2"


@pytest.mark.parametrize("inside", [".", "skills/alpha"])
def test_an_output_inside_the_source_tree_is_refused(source: Source, inside: str) -> None:
    source.skill("alpha")
    with pytest.raises(plugin_build.BuildError, match="inside the source tree"):
        plugin_build.output_directory(source.root / inside, source.root)


def test_an_output_inside_a_skills_directory_is_refused(source: Source) -> None:
    target = SKILLS_ROOT / "never-written"
    with pytest.raises(plugin_build.BuildError, match="inside the skills directory"):
        plugin_build.output_directory(target, source.root)
    assert not target.exists()


def test_an_output_that_is_a_file_or_holds_files_is_refused_and_an_absent_one_is_made(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "kept.txt").write_text("kept", encoding="utf-8", newline="\n")
    (tmp_path / "file").write_text("kept", encoding="utf-8", newline="\n")
    for name in ("full", "file"):
        with pytest.raises(plugin_build.BuildError, match="is not an empty directory"):
            plugin_build.output_directory(tmp_path / name, source_root)
    assert (tmp_path / "full" / "kept.txt").read_text(encoding="utf-8") == "kept"
    made = plugin_build.output_directory(tmp_path / "new folder" / "plugins", source_root)
    assert made.is_dir()


def run(arguments: list[str]) -> tuple[int, list[str]]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = plugin_build.main(arguments)
    return code, output.getvalue().splitlines()


def test_main_writes_each_plugin_tree_and_reports_what_it_carries_and_left_out(source: Source, tmp_path: Path) -> None:
    source.skill("alpha", body="Run {{PYTHON}} it.\n", required_vars=["PYTHON"])
    source.skill("beta", required_vars=["SOURCE_ROOT"])
    output = tmp_path / "out put"
    code, lines = run(["--source", str(source.root), "--output", str(output)])
    assert code == 0
    tree = output.resolve() / "fixture-skills"
    assert lines == [
        f'OUTPUT "{output.resolve()}"',
        f'PLUGIN fixture-skills "{tree}"',
        "SKILL fixture-skills alpha",
        'LEFT_OUT beta "beta takes {{SOURCE_ROOT}}, which a plugin has no value for"',
    ]
    assert (tree / "skills" / "alpha" / "SKILL.md").read_bytes().endswith(b"Run python it.\n")
    assert sorted(path.relative_to(tree).as_posix() for path in tree.rglob("*") if path.is_file()) == [
        ".claude-plugin/plugin.json",
        "skills/alpha/SKILL.md",
    ]


def test_main_writes_reproducible_archives_with_their_digests(source: Source, tmp_path: Path) -> None:
    source.skill("alpha")
    code, lines = run(["--source", str(source.root), "--output", str(tmp_path / "out"), "--archive"])
    assert code == 0
    archive = (tmp_path / "out").resolve() / "fixture-skills.zip"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert lines[1] == f'ARCHIVE fixture-skills "{archive}" {digest}'
    with zipfile.ZipFile(archive) as opened:
        assert opened.namelist() == [".claude-plugin/plugin.json", "skills/alpha/SKILL.md"]
    assert archive.read_bytes() == plugin_build.archive_bytes(by_name(assembled(source))["fixture-skills"].files)


def test_main_reports_a_failure_on_one_line_and_writes_nothing(source: Source, tmp_path: Path) -> None:
    source.skill("alpha", body="Run {{PYTHON}}\nagain.\n")
    code, lines = run(["--source", str(source.root), "--output", str(tmp_path / "out")])
    assert code == 1
    assert len(lines) == 1
    assert lines[0].startswith("FAILED plugin fixture-skills keeps template tokens:")
    assert not (tmp_path / "out").exists()


def test_an_agent_without_hooks_and_a_body_mentioning_hooks_is_kept_byte_for_byte() -> None:
    text = "---\nname: plain\ntools: Read\n---\n\nhooks: are not frontmatter here.\n"
    assert plugin_agents.plugin_agent(text) == (text, {})


def test_an_agents_hooks_with_two_events_and_a_plain_command_keep_their_order_and_values() -> None:
    text = (
        "---\nname: two\nhooks:\n  PreToolUse:\n    - matcher: Read\n      hooks:\n        - type: command\n"
        "          command: echo one\n  Stop:\n    - hooks:\n        - type: command\n          command: 'echo two'\n"
        "model: inherit\n---\n\nBody.\n"
    )
    definition, hooks = plugin_agents.plugin_agent(text)
    assert definition == "---\nname: two\nmodel: inherit\n---\n\nBody.\n"
    assert hooks == {
        "PreToolUse": [{"matcher": "Read", "hooks": [{"type": "command", "command": "echo one"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "echo two"}]}],
    }


def test_the_sources_readme_is_each_plugins_readme_and_a_source_without_one_gives_none(source: Source) -> None:
    source.skill("alpha")
    source.skill("solo", opt_in=True)
    assert plugin_build.load_readme(source.root) is None
    assert all("README.md" not in plugin.files for plugin in assembled(source).plugins)
    (source.root / ".claude-plugin" / "README.md").write_bytes(b"# Fixture\n")
    readme = plugin_build.load_readme(source.root)
    assert readme == b"# Fixture\n"
    catalogue = plugin_build.load_catalogue(source.root)
    plugins = plugin_build.assemble(plugin_build.load_source(source.root), catalogue, readme=readme).plugins
    assert [plugin.files["README.md"] for plugin in plugins] == [b"# Fixture\n", b"# Fixture\n"]


def test_audit_skill_locates_each_skill_a_built_plugin_carries_and_measures_its_hooks(
    source: Source, tmp_path: Path
) -> None:
    source.skill("core", selectable=False, required_vars=["PYTHON"])
    source.skill("alpha", body="Start a `guard` subagent.\n", skill_deps=["core"], agent_deps=["guard"])
    source.skill("solo", opt_in=True, skill_deps=["core"])
    source.agent("guard", GUARD_HOOK)
    output = tmp_path / "out put"
    assert run(["--source", str(source.root), "--output", str(output)])[0] == 0
    home = tmp_path / "home"
    alpha = skill_inventory.locate("fixture-skills:alpha", home, None, output)
    assert alpha[-2:] == ["SCOPE plugin", f"PLUGIN fixture-skills {(output / 'fixture-skills').resolve().as_posix()}"]
    core = skill_inventory.locate("fixture-skills-solo:core", home, None, output)
    assert core[-1] == f"PLUGIN fixture-skills-solo {(output / 'fixture-skills-solo').resolve().as_posix()}"
    scanned = skill_inventory.scan([Path(alpha[1].split(" ", 1)[1]).parent])
    assert [line for line in scanned if line.startswith(("AGENT ", "PLUGIN_HOOK"))] == [
        f"AGENT fixture-skills:guard {(output / 'fixture-skills' / 'agents' / 'guard.md').resolve().as_posix()}",
        "PLUGIN_HOOK PreToolUse Read|Bash python timeout=30 on_failure=block",
    ]


def test_audit_skill_names_the_plugins_this_checkout_makes_for_each_skill_as_the_build_plans_them() -> None:
    # audit-skill reads the menu without the deployer's loader, so its reading is held to the build's on every skill.
    source = plugin_build.load_source(plugin_build.REPOSITORY_ROOT)
    closures, left_out = skill_menu.plugin_plan(source)
    for name in sorted(source.skills):
        lines = [
            line
            for line in skill_inventory.plugin_lines(plugin_build.REPOSITORY_ROOT / "skills" / name)
            if line.startswith(("CARRIED_BY", "LEFT_OUT"))
        ]
        carried = [f"CARRIED_BY {plugin}" for plugin, closure in closures.items() if name in closure]
        reasons = [
            f"LEFT_OUT {item} {reason}"
            for item, reason in left_out.items()
            if name in skill_menu.expand(source, [], skill_menu.members(source, item))
        ]
        assert lines == (carried or reasons), name
