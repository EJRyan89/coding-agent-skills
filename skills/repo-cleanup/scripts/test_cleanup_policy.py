from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import cleanup_policy
from cleanup_policy import PolicyError, Refused, Regenerable

SKILL = Path(__file__).resolve().parents[1]
DOTNET = ("*.csproj", "*.vbproj", "*.fsproj")


def test_the_shipped_lists_are_exactly_the_documented_ones_and_live_beside_the_skill() -> None:
    assert SKILL / "references" / "cleanup-policy.json" == cleanup_policy.POLICY_FILE
    regenerable, protected = cleanup_policy.shipped()
    assert {"/".join(entry.directory): entry.projects for entry in regenerable} == {
        "__pycache__": (),
        ".pytest_cache": (),
        ".mypy_cache": (),
        ".ruff_cache": (),
        "node_modules/.cache": (),
        ".vs": (),
        "node_modules": ("package.json",),
        "bin": DOTNET,
        "obj": DOTNET,
    }
    assert not any(entry.added for entry in regenerable)
    assert protected == ("release/",)


def test_the_script_holds_no_copy_of_either_list() -> None:
    script = (SKILL / "scripts" / "repo_cleanup.py").read_text(encoding="utf-8")
    for name in ("release/", "__pycache__", ".pytest_cache", "package.json", "csproj", "node_modules"):
        assert name not in script, name


def test_without_additions_the_policy_is_the_shipped_lists() -> None:
    policy = cleanup_policy.configured()
    assert (policy.regenerable, policy.protected) == cleanup_policy.shipped()
    assert (policy.added_regenerable(), policy.added_protected, policy.refused) == ([], (), ())
    assert policy.given == ("", "")


def test_additions_are_added_after_the_shipped_entries_as_written_and_once() -> None:
    policy = cleanup_policy.configured(".venv .tox=tox.ini,setup.cfg  build/cache .venv", "hotfix/ support/ hotfix/")
    defaults, prefixes = cleanup_policy.shipped()
    assert policy.regenerable == (
        *defaults,
        Regenerable((".venv",), (), added=True),
        Regenerable((".tox",), ("tox.ini", "setup.cfg"), added=True),
        Regenerable(("build", "cache"), (), added=True),
    )
    assert policy.added_regenerable() == [".venv", ".tox=tox.ini,setup.cfg", "build/cache"]
    assert policy.protected == (*prefixes, "hotfix/", "support/")
    assert policy.added_protected == ("hotfix/", "support/")
    assert policy.refused == ()
    assert policy.given == (".venv .tox=tox.ini,setup.cfg  build/cache .venv", "hotfix/ support/ hotfix/")


def test_a_shipped_prefix_in_another_letter_case_is_not_an_addition() -> None:
    policy = cleanup_policy.configured(protected="RELEASE/ Release/")
    assert policy.added_protected == ()
    assert policy.protected == cleanup_policy.shipped()[1]


def test_a_protected_prefix_matches_a_branch_in_any_letter_case() -> None:
    policy = cleanup_policy.configured(protected="hotfix/")
    for name, prefix in (
        ("release/4.10", "release/"),
        ("Release/4.10", "release/"),
        ("hotfix/login", "hotfix/"),
        ("HOTFIX/login", "hotfix/"),
        ("hotfixes/login", None),
        ("topic/hotfix/login", None),
    ):
        assert policy.protecting(name) == prefix, name


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ("=tox.ini", "names no directory"),
        ("/cache", "is an absolute path; name a directory inside the worktree"),
        ("\\\\server\\cache", "is an absolute path; name a directory inside the worktree"),
        ("C:/cache", "is an absolute path; name a directory inside the worktree"),
        ("c:cache", "is an absolute path; name a directory inside the worktree"),
        ("../cache", "is a parent-relative path, which reaches outside the worktree"),
        ("build/../../cache", "is a parent-relative path, which reaches outside the worktree"),
        ("..", "is a parent-relative path, which reaches outside the worktree"),
        (".", "names the whole worktree"),
        ("./", "names the whole worktree"),
        ("./cache", "holds an empty or '.' folder; write the path from the folder that holds the directory"),
        ("build//cache", "holds an empty or '.' folder; write the path from the folder that holds the directory"),
        ("*.egg-info", "holds a wildcard; a directory is matched by its name as written"),
        ("bin", "is a shipped entry; an addition cannot change the project files it needs"),
        ("node_modules=yarn.lock", "is a shipped entry; an addition cannot change the project files it needs"),
        ("node_modules\\.cache", "is a shipped entry; an addition cannot change the project files it needs"),
        (".tox=", "names an empty project file"),
        (".tox=tox.ini,,setup.cfg", "names an empty project file"),
        (".tox=sub/tox.ini", "names project file sub/tox.ini, which is not a file name beside the directory"),
        (".tox=..", "names project file .., which is not a file name beside the directory"),
        (".tox=**", "names project file **, which is not a file name beside the directory"),
        (".tox=a:b", "names project file a:b, which is not a file name beside the directory"),
        (".tox=a=b", "names project file a=b, which is not a file name beside the directory"),
        ("C:/cache=tox.ini", "is an absolute path; name a directory inside the worktree"),
    ],
)
def test_an_added_entry_that_could_hold_work_is_refused_with_its_reason_and_not_used(entry: str, reason: str) -> None:
    policy = cleanup_policy.configured(f"{entry} .venv")
    assert policy.refused == (Refused(entry, reason),)
    assert policy.added_regenerable() == [".venv"], "the other additions are still used"


def write_policy(directory: Path, document: object) -> Path:
    path = directory / "cleanup-policy.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def shipped_document(**changes: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "protected_prefixes": ["release/"],
        "regenerable": [{"directory": "__pycache__", "projects": []}],
        **changes,
    }


def test_a_sound_shipped_file_is_read_as_written(tmp_path: Path) -> None:
    path = write_policy(
        tmp_path,
        shipped_document(regenerable=[{"directory": "bin", "projects": ["*.csproj"]}], protected_prefixes=["x/"]),
    )
    assert cleanup_policy.shipped(path) == ((Regenerable(("bin",), ("*.csproj",)),), ("x/",))
    assert cleanup_policy.configured("obj", "y/", path).added_regenerable() == ["obj"]


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("not json", "cannot read the cleanup policy"),
        (shipped_document(schema_version=2), r"\$\.schema_version must be one of 1"),
        (shipped_document(protected_prefixes=[]), r"\$\.protected_prefixes must be a non-empty array"),
        (shipped_document(protected_prefixes=[""]), r"\$\.protected_prefixes\[0\] must be a non-empty"),
        (shipped_document(regenerable=[{"directory": "x"}]), r"\$\.regenerable\[0\] must be an object with"),
        (shipped_document(extra=1), r"\$ must be an object of known fields"),
        (
            shipped_document(regenerable=[{"directory": "../x", "projects": []}]),
            r"\$\.regenerable\[0\] \.\./x is a parent-relative path",
        ),
        (
            shipped_document(regenerable=[{"directory": "x", "projects": []}, {"directory": "x", "projects": []}]),
            "names a directory more than once",
        ),
    ],
)
def test_a_broken_shipped_file_stops_the_skill(tmp_path: Path, document: object, message: str) -> None:
    path = tmp_path / "cleanup-policy.json"
    path.write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")
    with pytest.raises(PolicyError, match=message):
        cleanup_policy.configured(path=path)


def test_a_missing_shipped_file_stops_the_skill(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="cannot read the cleanup policy"):
        cleanup_policy.shipped(tmp_path / "absent.json")
