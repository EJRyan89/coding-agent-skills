"""Every driver of an entry point's command, gathered from the faults_*.py modules by area, and every entry point or
command the harness cannot drive, with the reason.

fault_table.py describes a driver and the faults. A new entry point or command joins its area's module in the pull
request that adds it, as "Validation" in docs/adding-a-skill.md says, or names its reason in that module's NOT_RUN.
"""

from __future__ import annotations

import faults_repository
import faults_skills
import faults_tools
from fault_table import Driver

DRIVERS: list[Driver] = sorted(
    [*faults_repository.DRIVERS, *faults_skills.DRIVERS, *faults_tools.DRIVERS], key=lambda driver: driver.name
)
# Each entry point, or "<entry point> <command>", the harness does not run, with the reason.
FAULTS_NOT_RUN: dict[str, str] = {**faults_repository.NOT_RUN, **faults_skills.NOT_RUN, **faults_tools.NOT_RUN}
