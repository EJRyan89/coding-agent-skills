"""The token variables no suite is given.

Validation reads GitHub once, in `tests/known_failure_issues.py`, which runs nothing else, so CI gives the run's token
to that run alone. Each process the pool starts, through `job_pool.run_process`, and each suite pytest
starts directly, through the root conftest.py, runs without these variables as well, so a token in a contributor's
shell or a workflow step reaches no suite either.
"""

from __future__ import annotations

from collections.abc import Mapping

# The variables gh, and the GitHub libraries that follow it, read a token from.
CREDENTIAL_VARIABLES = frozenset({"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"})


def is_credential(name: str) -> bool:
    """Whether an environment variable's name is one of CREDENTIAL_VARIABLES, in any case, as Windows reads a name."""
    return name.upper() in CREDENTIAL_VARIABLES


def without_credentials(environment: Mapping[str, str]) -> dict[str, str]:
    """`environment` without CREDENTIAL_VARIABLES."""
    return {name: value for name, value in environment.items() if not is_credential(name)}
