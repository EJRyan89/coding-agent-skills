"""The fault corpus's runs of a file a command reads, there but not readable: every driver's, judged by
fault_check.check.

fault_check.py describes a run and how it is judged; test_faults.py holds the coverage and the harness's own tests.
"""

from __future__ import annotations

import pytest
from fault_check import check, run_ids, runs_of
from fault_table import UNREADABLE, Driver

RUNS = runs_of(UNREADABLE)


@pytest.mark.parametrize(("driver", "fault", "path"), RUNS, ids=run_ids(RUNS))
def test_each_command_refuses_a_file_it_reads_that_it_cannot_read(driver: Driver, fault: str, path: str | None) -> None:
    check(driver, fault, path)
