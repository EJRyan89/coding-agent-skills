"""Every reader of a file or record in the repository, gathered from the readers_*.py modules by area.

A reader joins its area's table in the pull request that adds it, as "Validation" in docs/adding-a-skill.md says;
reader_table.py describes an entry. PENDING names each reader not yet in a table, with the open issue whose pull
request adds it, so a reader is either held to the corpus or named as owed.
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

# Each reader of another program's output or of text a repository gives that no table holds yet, by the name its
# entry will take, mapped to the issue whose pull request adds it.
PENDING: dict[str, int] = {}

__all__ = ["PENDING", "READERS", "NotText", "Reader", "applicable_cases"]
