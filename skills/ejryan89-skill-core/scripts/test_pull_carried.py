"""Regression tests for the one judgment of whether a pull request carried a commit."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pull_carried import carried

TIP, MERGE, LATER, HEAD, BASE, OTHER = (character * 40 for character in "abcdef")


def in_base(commit: str) -> bool:
    return commit == BASE


class CarriedTests(unittest.TestCase):
    def test_the_head_itself_is_carried_whatever_the_commits_list(self) -> None:
        self.assertTrue(carried(HEAD, HEAD, {}, in_base))

    def test_a_tip_the_pull_request_never_held_is_not_carried(self) -> None:
        # A branch reusing the merged branch's name, at main's head or with commits of its own.
        self.assertFalse(carried(BASE, HEAD, {HEAD: [TIP], TIP: [BASE]}, in_base))
        self.assertFalse(carried(OTHER, HEAD, {HEAD: [TIP], TIP: [BASE]}, in_base))

    def test_a_tip_the_head_follows_through_base_merges_is_carried(self) -> None:
        commits = {HEAD: [MERGE, BASE], MERGE: [TIP, BASE], TIP: [BASE]}
        self.assertTrue(carried(TIP, HEAD, commits, in_base))

    def test_a_tip_followed_by_work_of_the_pull_request_s_own_is_not_carried(self) -> None:
        self.assertFalse(carried(TIP, HEAD, {HEAD: [TIP], TIP: [BASE]}, in_base))

    def test_a_merge_that_brought_in_commits_the_base_does_not_hold_is_not_carried(self) -> None:
        commits = {HEAD: [TIP, OTHER], TIP: [BASE], OTHER: [BASE]}
        self.assertFalse(carried(TIP, HEAD, commits, in_base))

    def test_a_head_outside_the_commits_proves_no_earlier_tip(self) -> None:
        self.assertFalse(carried(TIP, HEAD, {TIP: [BASE]}, in_base))

    def test_a_cycle_in_the_commits_ends_the_walk(self) -> None:
        commits = {HEAD: [LATER, BASE], LATER: [HEAD, BASE], TIP: [BASE]}
        self.assertFalse(carried(TIP, HEAD, commits, in_base))
