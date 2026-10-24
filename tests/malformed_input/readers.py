"""Every reader of a file or record in the repository, gathered from the readers_*.py modules by area.

A reader joins its area's table in the pull request that adds it, as "Validation" in docs/adding-a-skill.md says;
reader_table.py describes an entry.
"""

from __future__ import annotations

import readers_core_review
import readers_deployer
import readers_repository
import readers_skills
from reader_table import NotText, Reader, applicable_cases

READERS: list[Reader] = sorted(
    [*readers_core_review.READERS, *readers_deployer.READERS, *readers_repository.READERS, *readers_skills.READERS],
    key=lambda reader: reader.name,
)

__all__ = ["READERS", "NotText", "Reader", "applicable_cases"]
