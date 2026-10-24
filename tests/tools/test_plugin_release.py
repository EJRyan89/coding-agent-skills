"""Regression suite for tools/plugin_release.py: building a release's plugin archives from its tag, pinning them in the
marketplace, and checking the pins, on a throwaway checkout."""

from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import sys
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from tools import plugin_release

REPOSITORY = "https://github.com/owner/fixture-skills"


def git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def write(root: Path, relative: str, text: str) -> None:
    (root / relative).parent.mkdir(parents=True, exist_ok=True)
    (root / relative).write_bytes(text.encode("utf-8"))


def marketplace(root: Path) -> dict[str, Any]:
    found: dict[str, Any] = json.loads((root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    return found


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout of a one-skill source, its release v1.2.3 tagged, under a folder whose name has spaces."""
    root = tmp_path / "Checkout With Spaces"
    write(root, "source.json", json.dumps({"id": "test/fixture-skills"}))
    write(
        root, "skills/alpha/SKILL.md", "---\nname: alpha\ndescription: Does alpha. Use it when asked.\n---\n\nBody.\n"
    )
    write(root, "deploy-meta/alpha.json", json.dumps({"required_vars": [], "shared_deps": []}))
    write(root, ".claude/skills/.keep", "")
    write(root, ".claude-plugin/README.md", "# Fixture skills\n")
    old = {"source": "archive", "url": f"{REPOSITORY}/releases/download/v1.2.2/fixture-skills.zip"}
    entry = {"name": "fixture-skills", "description": "Fixture.", "repository": REPOSITORY, "source": old}
    write(root, ".claude-plugin/marketplace.json", json.dumps({"name": "fixture", "plugins": [entry]}, indent=2) + "\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-q", "-m", "release")
    git(root, "tag", "v1.2.3")
    return root


def run(arguments: list[str]) -> tuple[int, list[str]]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = plugin_release.main(arguments)
    return code, output.getvalue().splitlines()


def test_build_writes_the_tags_archive_and_pins_the_marketplace_to_its_release_asset(
    checkout: Path, tmp_path: Path
) -> None:
    # The checkout moves on after the tag; the archive still holds what the tag holds.
    write(checkout, "skills/alpha/SKILL.md", "---\nname: alpha\ndescription: Changed.\n---\n\nLater.\n")
    code, lines = run(["build", "v1.2.3", "--root", str(checkout), "--output", str(tmp_path / "assets")])
    assert code == 0, lines
    archive = (tmp_path / "assets").resolve() / "fixture-skills.zip"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert lines == [f'ARCHIVE fixture-skills "{archive}" {digest}', f"PINNED fixture-skills v1.2.3 {digest}"]
    with zipfile.ZipFile(archive) as opened:
        assert opened.read("skills/alpha/SKILL.md").endswith(b"\n\nBody.\n")
        assert json.loads(opened.read(".claude-plugin/plugin.json"))["version"] == "1.2.3"
        assert opened.read("README.md") == b"# Fixture skills\n"
    url = f"{REPOSITORY}/releases/download/v1.2.3/fixture-skills.zip"
    assert marketplace(checkout)["plugins"] == [
        {
            "name": "fixture-skills",
            "description": "Fixture.",
            "repository": REPOSITORY,
            "source": {"source": "archive", "url": url, "sha256": digest},
        }
    ]
    code, lines = run(["check", "--root", str(checkout)])
    assert (code, lines) == (0, ["PIN_MATCHES fixture-skills v1.2.3"])


def test_check_reports_a_pin_that_differs_from_the_rebuild_and_an_entry_with_no_pin(checkout: Path) -> None:
    document = marketplace(checkout)
    url = f"{REPOSITORY}/releases/download/v1.2.3/fixture-skills.zip"
    document["plugins"] = [
        {"name": "fixture-skills", "source": {"source": "archive", "url": url, "sha256": "0" * 64}},
        {"name": "other", "source": "./other"},
    ]
    write(checkout, ".claude-plugin/marketplace.json", json.dumps(document))
    code, lines = run(["check", "--root", str(checkout)])
    assert code == 1
    assert lines[0].startswith(f"PIN_DIFFERS fixture-skills v1.2.3 {'0' * 64} ")
    assert lines[1] == 'UNPINNED other "its source names no release asset with a sha256"'


@pytest.mark.parametrize(
    ("tag", "reason"),
    [
        ("v1.2.3-rc.1", "v1.2.3-rc.1 is not a release tag"),
        ("v9.9.9", "--at v9.9.9 names no commit"),
    ],
)
def test_build_refuses_a_tag_that_is_no_release_the_checkout_holds(
    checkout: Path, tmp_path: Path, tag: str, reason: str
) -> None:
    before = marketplace(checkout)
    code, lines = run(["build", tag, "--root", str(checkout), "--output", str(tmp_path / "assets")])
    assert code == 1
    assert lines[0].startswith("FAILED ")
    assert reason in lines[0]
    assert marketplace(checkout) == before
    assert not (tmp_path / "assets").exists()


def test_build_refuses_an_entry_with_no_repository_before_writing_anything(checkout: Path, tmp_path: Path) -> None:
    document = marketplace(checkout)
    del document["plugins"][0]["repository"]
    write(checkout, ".claude-plugin/marketplace.json", json.dumps(document))
    code, lines = run(["build", "v1.2.3", "--root", str(checkout), "--output", str(tmp_path / "assets")])
    assert code == 1
    assert lines == ["FAILED marketplace entry fixture-skills names no https repository its release assets live under"]
    assert "sha256" not in json.dumps(marketplace(checkout))
    assert not (tmp_path / "assets").exists()


def test_build_refuses_an_entry_after_an_earlier_one_before_writing_anything(checkout: Path, tmp_path: Path) -> None:
    # A release of two plugins, the second of whose entries names no repository.
    write(
        checkout,
        "skills/solo/SKILL.md",
        "---\nname: solo\ndescription: Does solo. Use it when asked.\n---\n\nBody.\n",
    )
    write(checkout, "deploy-meta/solo.json", json.dumps({"required_vars": [], "shared_deps": [], "opt_in": True}))
    document = marketplace(checkout)
    document["plugins"].append({"name": "fixture-skills-solo", "description": "Solo.", "source": "./solo"})
    write(checkout, ".claude-plugin/marketplace.json", json.dumps(document))
    git(checkout, "add", "-A")
    git(checkout, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-q", "-m", "solo")
    git(checkout, "tag", "v1.2.4")
    before = (checkout / ".claude-plugin" / "marketplace.json").read_bytes()
    code, lines = run(["build", "v1.2.4", "--root", str(checkout), "--output", str(tmp_path / "assets")])
    assert code == 1
    assert lines == [
        "FAILED marketplace entry fixture-skills-solo names no https repository its release assets live under"
    ]
    assert (checkout / ".claude-plugin" / "marketplace.json").read_bytes() == before
    assert not (tmp_path / "assets").exists()


def test_check_of_a_marketplace_listing_no_plugin_fails_with_its_reason(checkout: Path) -> None:
    write(checkout, ".claude-plugin/marketplace.json", json.dumps({"name": "fixture", "plugins": []}))
    code, lines = run(["check", "--root", str(checkout)])
    assert (code, lines) == (1, ["FAILED .claude-plugin/marketplace.json lists no plugin, so there is no pin to check"])


def test_each_usage_form_in_the_docstring_parses(checkout: Path, tmp_path: Path) -> None:
    forms = re.findall(r"^  python tools/plugin_release\.py (.+)$", plugin_release.__doc__ or "", re.MULTILINE)
    assert [form.split(" ", 1)[0] for form in forms] == ["build", "check"]
    values = {"--root": str(checkout), "--output": str(tmp_path / "assets")}
    for form in forms:
        words = [word.strip("[]") for word in form.split(" ")]
        arguments = [
            values[words[index - 1]] if word == "DIR" else "v1.2.3" if word == "vX.Y.Z" else word
            for index, word in enumerate(words)
        ]
        code, lines = run(arguments)
        assert code == 0, (form, lines)


def test_the_asset_url_is_the_repositorys_release_download() -> None:
    assert plugin_release.asset_url(f"{REPOSITORY}/", "v1.0.0", "suite") == (
        f"{REPOSITORY}/releases/download/v1.0.0/suite.zip"
    )


def test_the_release_procedure_builds_attaches_pins_and_checks_the_archives_after_the_tag() -> None:
    text = (plugin_release.REPOSITORY_ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
    before_tagging = text.split("\n## Before tagging\n", 1)[1].split("\n## ", 1)[0]
    after_tagging = text.split("\n## After tagging\n", 1)[1].split("\n## ", 1)[0]
    assert "python -B tools/plugin_canary.py" in before_tagging
    step = after_tagging[after_tagging.index("**Plugin archives.**") : after_tagging.index("**Security advisories.**")]
    for phrase in (
        "python -B tools/plugin_release.py build vX.Y.Z",
        "gh release upload vX.Y.Z",
        "PINNED",
        "pull request",
        "python -B tools/plugin_release.py check",
        "`PIN_MATCHES`",
        "never the hub",
    ):
        assert phrase in step


def test_each_command_works_on_this_checkout_unless_root_names_another(checkout: Path) -> None:
    roots: list[Path] = []

    def checked(root: Path) -> tuple[list[str], bool]:
        roots.append(root)
        return ["PIN_MATCHES fixture-skills v1.2.3"], True

    def built(root: Path, tag: str, output: Path | None) -> list[str]:
        roots.append(root)
        return []

    with (
        mock.patch.object(plugin_release, "check", side_effect=checked),
        mock.patch.object(plugin_release, "build", side_effect=built),
    ):
        assert run(["check"]) == (0, ["PIN_MATCHES fixture-skills v1.2.3"])
        assert run(["build", "v1.2.3"])[0] == 0
        assert run(["check", "--root", str(checkout)])[0] == 0
    assert roots == [plugin_release.REPOSITORY_ROOT, plugin_release.REPOSITORY_ROOT, checkout]


def test_the_help_names_each_command_and_option_with_what_it_does() -> None:
    for arguments, expected in (
        (
            ["--help"],
            [
                " build build the tag's archives and pin them in the marketplace"
                " check compare each pinned digest with a rebuild from its tag "
            ],
        ),
        (["build", "--help"], ["--root DIR", "a directory holding source.json and .claude/skills", "the release tag"]),
        (["check", "--help"], ["--root DIR", "work on this checkout of the repository instead"]),
    ):
        output = io.StringIO()
        with redirect_stdout(output), pytest.raises(SystemExit) as raised:
            plugin_release.main(arguments)
        assert raised.value.code == 0
        flat = f" {' '.join(output.getvalue().split())} "
        for text in expected:
            assert text in flat, (arguments, flat)
