"""Which folders below a sweep's anchor the cleanup reads as plain: lexically below it, through no link or junction,
as path_containment judges the anchor."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import repo_cleanup as rc
from test_repo_cleanup import GIT_CONFIG, link_folder

# The settings conftest.py adds to the global configuration of every home this suite's git runs in.
git_config = GIT_CONFIG


def test_linkless_below_holds_a_path_strictly_below_its_anchor_through_plain_folders(tmp_path: Path) -> None:
    anchor = tmp_path / "anchor"
    (anchor / "a" / "b").mkdir(parents=True)
    (tmp_path / "anchor2" / "a").mkdir(parents=True)
    link_folder(anchor / "a", anchor / "linked")
    assert rc.linkless_below(str(anchor / "a" / "b"), str(anchor))
    assert rc.linkless_below(str(anchor / "a" / "b" / ".." / "b"), str(anchor))
    for path in (anchor, tmp_path / "anchor2" / "a", tmp_path, anchor / "linked" / "b", anchor / "a" / ".." / ".."):
        assert not rc.linkless_below(str(path), str(anchor)), path


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows compares names without their case")
def test_linkless_below_reads_the_anchor_in_any_case_where_the_platform_folds_it(tmp_path: Path) -> None:
    anchor = tmp_path / "Anchor"
    (anchor / "a").mkdir(parents=True)
    assert rc.linkless_below(str(tmp_path / "ANCHOR" / "a"), str(anchor))
