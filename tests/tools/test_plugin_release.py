"""Regression suite for tools/plugin_release.py: building a release's plugin archives from its tag, pinning them in the
marketplace, and checking the pins, on a throwaway checkout."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

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
    return json.loads((root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))


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
    code, lines = run(["--root", str(checkout), "build", "v1.2.3", "--output", str(tmp_path / "assets")])
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
    code, lines = run(["--root", str(checkout), "check"])
    assert (code, lines) == (0, ["PIN_MATCHES fixture-skills v1.2.3"])


def test_check_reports_a_pin_that_differs_from_the_rebuild_and_an_entry_with_no_pin(checkout: Path) -> None:
    document = marketplace(checkout)
    url = f"{REPOSITORY}/releases/download/v1.2.3/fixture-skills.zip"
    document["plugins"] = [
        {"name": "fixture-skills", "source": {"source": "archive", "url": url, "sha256": "0" * 64}},
        {"name": "other", "source": "./other"},
    ]
    write(checkout, ".claude-plugin/marketplace.json", json.dumps(document))
    code, lines = run(["--root", str(checkout), "check"])
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
    code, lines = run(["--root", str(checkout), "build", tag, "--output", str(tmp_path / "assets")])
    assert code == 1
    assert lines[0].startswith("FAILED ")
    assert reason in lines[0]
    assert marketplace(checkout) == before
    assert not (tmp_path / "assets").exists()


def test_build_refuses_an_entry_with_no_repository_before_writing_anything(checkout: Path, tmp_path: Path) -> None:
    document = marketplace(checkout)
    del document["plugins"][0]["repository"]
    write(checkout, ".claude-plugin/marketplace.json", json.dumps(document))
    code, lines = run(["--root", str(checkout), "build", "v1.2.3", "--output", str(tmp_path / "assets")])
    assert code == 1
    assert lines == ["FAILED marketplace entry fixture-skills names no https repository its release assets live under"]
    assert "sha256" not in json.dumps(marketplace(checkout))


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
