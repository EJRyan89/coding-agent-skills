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

from skill_roots import SKILLS_ROOT

from tools import plugin_build

AGENT = "---\nname: {name}\ndescription: An agent.\ntools: Read\n{extra}---\n\nRun {{{{PYTHON}}}} -B it.\n"


class Source:
    """A source tree under a folder whose name has spaces, built one skill at a time."""

    def __init__(self, root: Path) -> None:
        self.root = root / "Source With Spaces"
        for folder in ("skills", "deploy-meta", "agents"):
            (self.root / folder).mkdir(parents=True)
        self.document: dict[str, object] = {"id": "test/fixture-skills", "bundles": {}}
        self.save()

    def save(self) -> None:
        (self.root / "source.json").write_text(json.dumps(self.document), encoding="utf-8", newline="\n")

    def bundle(self, name: str, members: list[str], *, opt_in: bool = False) -> None:
        bundles = self.document["bundles"]
        assert isinstance(bundles, dict)
        bundles[name] = {"members": members, **({"opt_in": True} if opt_in else {})}
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
        return directory

    def agent(self, name: str, extra: str = "") -> None:
        (self.root / "agents" / f"{name}.md").write_text(
            AGENT.format(name=name, extra=extra), encoding="utf-8", newline="\n"
        )


@pytest.fixture
def source(tmp_path: Path) -> Source:
    return Source(tmp_path)


def assembled(source: Source) -> plugin_build.Assembly:
    return plugin_build.assemble(plugin_build.load_source(source.root))


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
    assert sorted(files) == ["skills/alpha/SKILL.md", "skills/alpha/scripts/data.bin", "skills/alpha/scripts/tool.py"]
    assert files["skills/alpha/SKILL.md"].endswith(b"Run python -B tool.\n")
    assert files["skills/alpha/scripts/tool.py"] == b"print('python')\n"
    assert files["skills/alpha/scripts/data.bin"] == b"\xff\x00{{"


def test_a_token_a_skill_does_not_declare_is_refused_rather_than_shipped(source: Source) -> None:
    source.skill("alpha", body="Run {{PYTHON}} -B tool.\n")
    with pytest.raises(plugin_build.BuildError, match=r"keeps template tokens: skills/alpha/SKILL.md:\d+:Run"):
        assembled(source)


def test_an_item_needing_a_value_with_no_plugin_answer_is_left_out_with_its_dependents(source: Source) -> None:
    source.skill("rooted", selectable=False, required_vars=["REPOS_ROOT"])
    source.skill("alpha", skill_deps=["rooted"])
    source.skill("beta")
    assembly = assembled(source)
    assert by_name(assembly)["fixture-skills"].skills == ["beta"]
    assert assembly.left_out == {"alpha": "rooted takes {{REPOS_ROOT}}, which a plugin has no value for"}


def test_an_agent_without_hooks_ships_answered_and_one_with_hooks_keeps_its_bundle_out(source: Source) -> None:
    source.agent("plain")
    source.agent("guarded", extra="hooks:\n  PreToolUse:\n    - matcher: Read\n")
    source.skill("alpha", agent_deps=["plain"])
    source.skill("beta", agent_deps=["guarded"])
    source.bundle("reviews", ["beta"])
    assembly = assembled(source)
    plugin = by_name(assembly)["fixture-skills"]
    assert plugin.skills == ["alpha"]
    assert plugin.agents == ["plain"]
    assert plugin.files["agents/plain.md"].endswith(b"Run python -B it.\n")
    assert assembly.left_out == {"reviews": "agent guarded declares hooks, which a plugin agent ignores"}


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
        "skills/alpha/SKILL.md"
    ]


def test_main_writes_reproducible_archives_with_their_digests(source: Source, tmp_path: Path) -> None:
    source.skill("alpha")
    code, lines = run(["--source", str(source.root), "--output", str(tmp_path / "out"), "--archive"])
    assert code == 0
    archive = (tmp_path / "out").resolve() / "fixture-skills.zip"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert lines[1] == f'ARCHIVE fixture-skills "{archive}" {digest}'
    files = {"skills/alpha/SKILL.md": (source.root / "skills" / "alpha" / "SKILL.md").read_bytes()}
    assert archive.read_bytes() == plugin_build.archive_bytes(files)


def test_main_reports_a_failure_on_one_line_and_writes_nothing(source: Source, tmp_path: Path) -> None:
    source.skill("alpha", body="Run {{PYTHON}}\nagain.\n")
    code, lines = run(["--source", str(source.root), "--output", str(tmp_path / "out")])
    assert code == 1
    assert len(lines) == 1
    assert lines[0].startswith("FAILED plugin fixture-skills keeps template tokens:")
    assert not (tmp_path / "out").exists()
