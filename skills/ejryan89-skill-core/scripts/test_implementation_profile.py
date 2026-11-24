"""Regression tests for implementation_profile, the profile's path and its contract-files heading."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from implementation_profile import CONTRACT_FILES_HEADING, PROFILE
from markdown_sections import markdown_section

REPOSITORY = Path(__file__).resolve().parents[3]


def test_the_profile_and_its_heading_are_pinned_contract_values() -> None:
    """A pinned contract value: validation's upgrade-notes check and implement-change's brief read these, each an
    independent literal here."""
    assert PROFILE == "docs/implementing-changes.md"
    assert CONTRACT_FILES_HEADING == "## Contract files"


def test_this_repository_s_profile_holds_the_contract_files_section() -> None:
    text = (REPOSITORY / PROFILE).read_text(encoding="utf-8")
    section = markdown_section(text, CONTRACT_FILES_HEADING)
    assert section is not None
    assert "deployer/manifest.py" in section
