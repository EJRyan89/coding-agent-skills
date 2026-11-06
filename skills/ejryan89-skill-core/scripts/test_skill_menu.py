"""The menu a source offers and the plugins it makes (skill_menu.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import skill_menu
from skill_menu import MenuEntry, SourceMenu

# Why no plugin carries an item needing SOURCE_ROOT, built so that no template token appears in a skill's files,
# where the deployer would render one.
LEFT_OUT = "rooted takes {" + "{SOURCE_ROOT}" + "}, which a plugin has no value for"


def menu() -> SourceMenu:
    """A source with a hidden core, a default skill, a default bundle, an opt-in bundle, an opt-in skill, and a skill
    whose dependency takes a value no plugin answers."""
    return SourceMenu(
        "owner/fixture-skills",
        skills={
            "core": MenuEntry(selectable=False),
            "alpha": MenuEntry(skill_deps=("core",)),
            "beta": MenuEntry(),
            "dotnet-one": MenuEntry(skill_deps=("core",)),
            "solo": MenuEntry(opt_in=True),
            "rooted": MenuEntry(selectable=False, required_vars=("SOURCE_ROOT",)),
            "cleanup": MenuEntry(skill_deps=("rooted",)),
            "orphan": MenuEntry(selectable=False),
        },
        bundles={"pair": ["beta"], "dotnet": ["dotnet-one"]},
        skill_bundle={"beta": "pair", "dotnet-one": "dotnet"},
        opt_in_bundles={"dotnet"},
    )


def test_the_menu_offers_bundles_and_root_skills_and_expands_through_dependencies() -> None:
    source = menu()
    assert skill_menu.root_names(source) == ["alpha", "cleanup", "solo"]
    assert skill_menu.expand(source, ["dotnet"], ["alpha", "cleanup"]) == [
        "dotnet-one",
        "core",
        "alpha",
        "cleanup",
        "rooted",
    ]
    assert skill_menu.is_opt_in(source, "dotnet")
    assert skill_menu.is_opt_in(source, "solo")
    assert not skill_menu.is_opt_in(source, "pair")
    assert skill_menu.members(source, "pair") == ["beta"]
    assert skill_menu.members(source, "alpha") == ["alpha"]


def test_the_plan_names_the_default_plugin_each_opt_in_plugin_and_each_item_left_out() -> None:
    # The answers are pinned against literals, never against the constant under test.
    assert skill_menu.PLUGIN_VALUES == {
        "PYTHON": "python",
        "REPOS_ROOT": "${user_config.repos_root}",
        "REPO_CLEANUP_REGENERABLE": "",
        "REPO_CLEANUP_PROTECTED": "",
    }
    closures, left_out = skill_menu.plugin_plan(menu())
    assert closures == {
        "fixture-skills": ["beta", "alpha", "core"],
        "fixture-skills-dotnet": ["dotnet-one", "core"],
        "fixture-skills-solo": ["solo"],
    }
    assert left_out == {"cleanup": LEFT_OUT}


@pytest.mark.parametrize(
    ("skill", "carried", "reasons"),
    [
        ("alpha", ["fixture-skills"], {}),
        ("core", ["fixture-skills", "fixture-skills-dotnet"], {}),
        ("cleanup", [], {"cleanup": LEFT_OUT}),
        ("rooted", [], {"cleanup": LEFT_OUT}),
        ("orphan", [], {"orphan": "no menu item needs it"}),
    ],
)
def test_carriers_name_the_plugins_that_carry_a_skill_or_why_none_does(
    skill: str, carried: list[str], reasons: dict[str, str]
) -> None:
    assert skill_menu.carriers(menu(), skill) == (carried, reasons)


def write_source(root: Path, document: object, metadata: dict[str, object]) -> Path:
    (root / "deploy-meta").mkdir(parents=True)
    (root / "source.json").write_text(json.dumps(document), encoding="utf-8")
    for name, value in metadata.items():
        (root / "deploy-meta" / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")
    return root


def test_read_menu_reads_the_source_document_and_each_skills_metadata(tmp_path: Path) -> None:
    root = write_source(
        tmp_path / "Source With Spaces",
        {"id": "owner/fixture-skills", "bundles": {"dotnet": {"members": ["dotnet-one"], "opt_in": True}}},
        {
            "core": {"selectable": False, "required_vars": ["PYTHON"]},
            "alpha": {"skill_deps": ["core"], "opt_in": False},
            "dotnet-one": {"skill_deps": ["core"]},
        },
    )
    read = skill_menu.read_menu(root)
    assert read == SourceMenu(
        "owner/fixture-skills",
        skills={
            "alpha": MenuEntry(skill_deps=("core",)),
            "core": MenuEntry(required_vars=("PYTHON",), selectable=False),
            "dotnet-one": MenuEntry(skill_deps=("core",)),
        },
        bundles={"dotnet": ["dotnet-one"]},
        skill_bundle={"dotnet-one": "dotnet"},
        opt_in_bundles={"dotnet"},
    )
    assert skill_menu.carriers(read, "core") == (["fixture-skills", "fixture-skills-dotnet"], {})


@pytest.mark.parametrize(
    ("document", "metadata", "problem"),
    [
        (None, {}, "source.json cannot be read"),
        ([], {}, "source.json is not a JSON object"),
        ({"id": "no-slash"}, {}, "names no source ID"),
        ({"id": "owner/repo"}, {"alpha": []}, "alpha.json is not a JSON object"),
        ({"id": "owner/repo"}, {"alpha": {"skill_deps": ["ghost"]}}, "has no metadata for: ghost"),
        ({"id": "owner/repo", "bundles": {"pair": {"members": ["ghost"]}}}, {}, "has no metadata for: ghost"),
        # A list of names of another kind is not none: the deployer refuses it, so the menu cannot be planned.
        ({"id": "owner/repo"}, {"alpha": {"skill_deps": "beta"}}, "alpha.json skill_deps is not a list of names"),
        ({"id": "owner/repo"}, {"alpha": {"skill_deps": [1]}}, "alpha.json skill_deps is not a list of names"),
        ({"id": "owner/repo"}, {"alpha": {"skill_deps": None}}, "alpha.json skill_deps is not a list of names"),
        ({"id": "owner/repo"}, {"alpha": {"required_vars": "X"}}, "alpha.json required_vars is not a list of names"),
        ({"id": "owner/repo", "bundles": []}, {}, "source.json bundles is not a JSON object"),
        ({"id": "owner/repo", "bundles": {"pair": []}}, {}, "source.json bundle pair is not a JSON object"),
        ({"id": "owner/repo", "bundles": {"pair": {"members": "alpha"}}}, {"alpha": {}}, "pair members is not a list"),
        ({"id": "owner/repo", "bundles": {"pair": {}}}, {}, "source.json bundle pair members is not a list of names"),
    ],
)
def test_read_menu_refuses_a_source_it_cannot_read(
    tmp_path: Path, document: object, metadata: dict[str, object], problem: str
) -> None:
    root = write_source(tmp_path / "source", document, metadata)
    if document is None:
        (root / "source.json").unlink()
    with pytest.raises(skill_menu.MenuError, match=problem):
        skill_menu.read_menu(root)


def test_closure_walks_skill_deps_depth_first_once_each_past_a_cycle() -> None:
    deps = {"alpha": ["beta", "delta"], "beta": ["gamma", "alpha"], "gamma": [], "delta": ["gamma"]}
    assert skill_menu.closure(["alpha"], deps.__getitem__) == ["alpha", "beta", "gamma", "delta"]
    assert skill_menu.closure(["delta", "alpha"], deps.__getitem__) == ["delta", "gamma", "alpha", "beta"]


def test_closure_walks_a_chain_longer_than_the_recursion_limit() -> None:
    length = sys.getrecursionlimit() * 2
    chain = skill_menu.closure(["0"], lambda name: [str(int(name) + 1)] if int(name) < length else [])
    assert [str(number) for number in range(length + 1)] == chain
