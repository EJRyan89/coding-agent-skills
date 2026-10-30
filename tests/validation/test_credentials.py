"""Fixture tests for tests/validation/credentials.py."""

from __future__ import annotations

import pytest
from credentials import is_credential, without_credentials


@pytest.mark.parametrize(
    ("name", "credential"),
    [
        pytest.param("GH_TOKEN", True, id="gh's token"),
        pytest.param("GITHUB_TOKEN", True, id="the Actions token's name"),
        pytest.param("GH_ENTERPRISE_TOKEN", True, id="gh's enterprise token"),
        pytest.param("GITHUB_ENTERPRISE_TOKEN", True, id="the enterprise token's other name"),
        pytest.param("gh_token", True, id="lower case, as Windows reads it"),
        pytest.param("Github_Token", True, id="mixed case"),
        pytest.param("GH_HOST", False, id="a gh setting that is no token"),
        pytest.param("GITHUB_STEP_SUMMARY", False, id="an Actions path"),
        pytest.param("GH_TOKEN_FILE", False, id="a longer name"),
    ],
)
def test_each_token_variable_is_named_in_any_case_and_nothing_else(name: str, credential: bool) -> None:
    assert is_credential(name) is credential


def test_an_environment_keeps_everything_but_the_tokens() -> None:
    environment = {"PATH": "bin", "GH_TOKEN": "t", "github_token": "t", "GH_HOST": "github.com"}
    assert without_credentials(environment) == {"PATH": "bin", "GH_HOST": "github.com"}
