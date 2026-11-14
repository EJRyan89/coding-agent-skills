"""The fault corpus's runs of a symbolic link to nothing in the place of a file a command reads: every driver's,
judged by fault_check.check. A link to nothing reads as the file missing, so a command that refuses a missing file
refuses it alike, one that reads a missing file as absent reads it so, and no command writes through the link to
create the file it leads to.

fault_check.py describes a run and how it is judged; test_faults.py holds the coverage and the harness's own tests.
"""

from __future__ import annotations

import sys

import pytest
from fault_check import check, run_ids, runs_of
from fault_table import LINK_TO_NOTHING, Driver

RUNS = runs_of(LINK_TO_NOTHING)


@pytest.mark.skipif(sys.platform == "win32", reason="a symbolic link to a file needs a privilege on Windows")
@pytest.mark.parametrize(("driver", "fault", "path"), RUNS, ids=run_ids(RUNS))
def test_each_command_reads_a_link_to_nothing_as_the_file_missing_and_never_writes_through_it(
    driver: Driver, fault: str, path: str | None
) -> None:
    check(driver, fault, path)
