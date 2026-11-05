"""The fault corpus's runs of a directory in the place of a file a command reads: every driver's, judged by
fault_check.check.

fault_check.py describes a run and how it is judged; test_faults.py holds the coverage and the harness's own tests.
"""

from __future__ import annotations

import pytest
from fault_check import check, run_ids, runs_of
from fault_table import DIRECTORY, Driver

RUNS = runs_of(DIRECTORY)


@pytest.mark.parametrize(("driver", "fault", "path"), RUNS, ids=run_ids(RUNS))
def test_each_command_refuses_a_directory_in_a_file_s_place(driver: Driver, fault: str, path: str | None) -> None:
    check(driver, fault, path)
