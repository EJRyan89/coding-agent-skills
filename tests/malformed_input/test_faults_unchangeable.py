"""The fault corpus's runs of a file a command changes made read-only: every driver that lists the files its command
changes, judged by fault_check.check; the same file in a folder made read-only, on Linux and macOS, where a folder's
permission refuses a change inside it and a file's does not refuse a delete; and each such command shown changing every
file it lists when nothing stops it.

A shipped skill's command that deletes, renames, or rewrites several files, or one in place, lists them in its
driver's `changes`, and the read-only fault is placed at each of them but the first, one at a time, so a command that
changes one before it checks a later one is caught; a directory in that file's place runs in
test_faults_directory.py. change_scan.py and
test_change_places.py hold every place in a skill's scripts that changes a file to a driver here or a reason it changes
no file of the user's. fault_check.py describes a run and how it is judged; test_faults.py holds the coverage and the
harness's own tests.
"""

from __future__ import annotations

import sys

import faults
import pytest
from fault_check import changed, check, run_ids, runs_of
from fault_table import READ_ONLY, READ_ONLY_FOLDER, Driver

RUNS = runs_of(READ_ONLY)
FOLDER_RUNS = runs_of(READ_ONLY_FOLDER)
CHANGING = [driver for driver in faults.DRIVERS if driver.changes]


@pytest.mark.parametrize(("driver", "fault", "path"), RUNS, ids=run_ids(RUNS))
def test_each_command_refuses_a_read_only_file_it_changes_before_its_first_change(
    driver: Driver, fault: str, path: str | None
) -> None:
    check(driver, fault, path)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows gives a folder no permission that refuses a change",
)
@pytest.mark.parametrize(("driver", "fault", "path"), FOLDER_RUNS, ids=run_ids(FOLDER_RUNS))
def test_each_command_refuses_a_file_in_a_read_only_folder_before_its_first_change(
    driver: Driver, fault: str, path: str | None
) -> None:
    check(driver, fault, path)


@pytest.mark.parametrize("driver", CHANGING, ids=run_ids([(driver, "unfaulted", None) for driver in CHANGING]))
def test_each_command_changes_every_file_its_driver_lists_when_nothing_stops_it(driver: Driver) -> None:
    changed(driver)
