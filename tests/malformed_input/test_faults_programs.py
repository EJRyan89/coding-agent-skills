"""The fault corpus's runs of git exiting 128 or timing out, and gh failing: every driver's, judged by
fault_check.check.

fault_check.py describes a run and how it is judged; test_faults.py holds the coverage and the harness's own tests.
"""

from __future__ import annotations

import pytest
from fault_check import check, run_ids, runs_of
from fault_table import GH_FAILURE, GIT_EXIT, GIT_TIMEOUT, Driver

RUNS = runs_of(GIT_EXIT, GIT_TIMEOUT, GH_FAILURE)


@pytest.mark.parametrize(("driver", "fault", "path"), RUNS, ids=run_ids(RUNS))
def test_each_command_refuses_a_git_or_gh_that_fails(driver: Driver, fault: str, path: str | None) -> None:
    check(driver, fault, path)
