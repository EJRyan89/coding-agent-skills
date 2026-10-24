"""The upgrade path every installation takes: the latest release's installed update-coding-agent-skills, run against a
clone whose origin holds the current tree.

The installed update.sh is the copy the latest release rendered, so it reaches into the clone at whatever paths that
release fixed. A later commit that moves one of them stops every installation's next update, which no suite of the
current script sees. Each test deploys the latest release from a local clone into a throwaway home, as an installation
on the release channel holds it, then runs that installed script against an origin whose main is this checkout's
HEAD and a fixture commit after it, tagged as each case needs. Nothing reaches a network: origin is a local bare
clone of this repository, which continuous integration checks out with its whole history and tags.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from harness import REPOSITORY_ROOT, forward

sys.path.insert(0, str(REPOSITORY_ROOT / "skills" / "ejryan89-skill-core" / "scripts"))

from git_client import GitClient
from release_tags import last_release, release_order

from deployer import platform_support

UPDATE = "update-coding-agent-skills"
# Where the deployer installs a skill's scripts, and the script the update skill runs, under the home.
SKILLS = Path(".claude") / "skills"
INSTALLED_SCRIPT = SKILLS / UPDATE / "scripts" / "update.sh"
# A path the script reaches in the clone, as it names one: "$CLONE/<path>". The update decision of 2026-10-22 in
# docs/design.md states the one such path, deploy.py, and test_update_installed.sh holds the current script to it.
CLONE_PATH = re.compile(r"\$\{?CLONE\}?/([^\"'`)\s]+)")
# Generous: a deployment of every skill, twice per update, on a slow runner.
DEADLINE = 600


def run(command: list[str], environment: dict[str, str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=DEADLINE,
        check=False,
    )


def git(directory: Path, *arguments: str) -> str:
    """git in a fixture repository, with the root conftest.py's isolated configuration and identity."""
    completed = subprocess.run(
        ["git", "-C", str(directory), *arguments], capture_output=True, text=True, encoding="utf-8", check=False
    )
    assert completed.returncode == 0, f"git {' '.join(arguments)} failed:\n{completed.stderr}"
    return completed.stdout.strip()


def latest_release() -> str:
    """The highest release tag this checkout's HEAD holds: the release every installation updates from."""
    release = last_release(GitClient(), REPOSITORY_ROOT, "HEAD")
    assert release is not None, "no release tag reaches HEAD; fetch the tags, as the validate workflow's checkout does"
    return release


def next_release(release: str, *, breaking: bool) -> str:
    """The release after `release` that raises its breaking component (the major, or the minor while the major is 0),
    or its patch alone."""
    assert release_order(release) is not None, release
    major, minor, patch = (int(part) for part in release.removeprefix("v").split("."))
    if not breaking:
        return f"v{major}.{minor}.{patch + 1}"
    return f"v{major + 1}.0.0" if major else f"v0.{minor + 1}.0"


@dataclass
class Installation:
    """A user's clone at the latest release, its origin, and the throwaway home the release deployed into."""

    release: str
    origin: Path
    clone: Path
    home: Path
    environment: dict[str, str]

    def fixture_commit(self, tag: str = "", *, moved: tuple[str, str] | None = None) -> str:
        """Publish a commit on origin's main after HEAD, renaming one top-level file of its tree when `moved` names
        it, and tag it when `tag` is given."""
        tree = git(self.origin, "rev-parse", "main^{tree}")
        if moved is not None:
            entries = git(self.origin, "ls-tree", "main").splitlines()
            renamed = [re.sub(rf"\t{re.escape(moved[0])}$", f"\t{moved[1]}", entry) for entry in entries]
            assert renamed != entries, f"origin's main has no top-level {moved[0]}"
            tree = subprocess.run(
                ["git", "-C", str(self.origin), "mktree"],
                input="\n".join(renamed) + "\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            ).stdout.strip()
        commit = git(self.origin, "commit-tree", tree, "-p", "main", "-m", "fixture commit after HEAD")
        git(self.origin, "update-ref", "refs/heads/main", commit)
        if tag:
            git(self.origin, "tag", "--force", tag, commit)
        return commit

    def update(self, *options: str) -> subprocess.CompletedProcess[str]:
        """The installed update, run as the skill runs it, on the release channel."""
        bash = platform_support.find_bash()
        assert bash is not None, "Git Bash is a validation prerequisite"
        script = forward(self.home / INSTALLED_SCRIPT)
        return run([bash, script, forward(self.clone), "release", *options], self.environment)

    def manifest(self) -> dict[str, object]:
        loaded: dict[str, object] = json.loads((self.home / SKILLS / ".deploy-manifest.json").read_text("utf-8"))
        return loaded

    def deployed_commit(self) -> str:
        """The commit `deploy.py check` names as deployed, through the clone's deployer as the update reads it."""
        checked = run([sys.executable, "-B", str(self.clone / "deploy.py"), "check"], self.environment)
        line = next((line for line in checked.stdout.splitlines() if line.startswith("Deployed commit: ")), "")
        assert line, f"deploy.py check named no deployed commit:\n{checked.stdout}{checked.stderr}"
        found = re.search(r"\b([0-9a-f]{40}|[0-9a-f]{64})\b", line)
        assert found is not None, line
        return found.group(1)


@pytest.fixture
def installation(tmp_path: Path) -> Installation:
    """The latest release, deployed on the release channel from a clone of an origin whose main is HEAD."""
    root = tmp_path.resolve()
    release = latest_release()
    origin = root / "origin.git"
    git(root, "clone", "--quiet", "--bare", str(REPOSITORY_ROOT), str(origin))
    git(origin, "update-ref", "refs/heads/main", git(REPOSITORY_ROOT, "rev-parse", "HEAD"))
    clone = root / "clone"
    git(root, "clone", "--quiet", "--branch", "main", str(origin), str(clone))
    git(clone, "reset", "--quiet", "--hard", f"refs/tags/{release}")
    home = root / "home"
    repos = root / "repos"
    home.mkdir()
    repos.mkdir()
    # Every variable that names the user's home or a per-user folder names the throwaway home instead, so neither the
    # release's deployer nor the update reaches the user's; the root conftest.py's git variables keep git isolated.
    environment = {
        **platform_support.signed_out_environment(os.environ, home),
        **{name: value for name, value in os.environ.items() if name.startswith("GIT_")},
    }
    deployer = [sys.executable, "-B", str(clone / "deploy.py")]
    # configure asks for the repositories root first, and every later answer left empty keeps its default.
    configured = run([*deployer, "configure"], environment, stdin=forward(repos) + "\n" * 20)
    assert configured.returncode == 0, f"the release's configure failed:\n{configured.stdout}{configured.stderr}"
    deployed = run([*deployer, "--all", "--at", f"refs/tags/{release}"], environment)
    assert deployed.returncode == 0, f"the release's deployment failed:\n{deployed.stdout}{deployed.stderr}"
    assert (home / INSTALLED_SCRIPT).is_file(), f"the release installed no {INSTALLED_SCRIPT.as_posix()}"
    return Installation(release, origin, clone, home, environment)


def assert_deployed(result: subprocess.CompletedProcess[str], first_line: str) -> None:
    """The update reported `first_line` first and ended by deploying."""
    lines = result.stdout.splitlines()
    shown = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert lines, f"the update printed nothing\n{shown}"
    assert lines[0] == first_line, shown
    assert not any(line.startswith("FAILED") for line in lines), shown
    assert lines[-1] == "DEPLOYED", shown
    assert result.returncode == 0, shown


def missing_clone_paths(script: str, tree: Path) -> list[str]:
    """Each path in the clone `script` reaches that `tree` does not hold."""
    named = sorted(set(CLONE_PATH.findall(script)))
    assert named, "the update.sh names no path in the clone"
    return [path for path in named if not (tree / path).exists()]


def test_the_release_names_only_clone_paths_the_current_tree_keeps() -> None:
    """Every path in the clone the latest release's update.sh reaches is still in this checkout."""
    release = latest_release()
    script = git(REPOSITORY_ROOT, "show", f"refs/tags/{release}:skills/{UPDATE}/scripts/update.sh")
    missing = missing_clone_paths(script, REPOSITORY_ROOT)
    assert not missing, f"the {release} update.sh reads {missing} from the clone, which this tree no longer holds"


def test_a_clone_path_the_tree_moved_is_named(tmp_path: Path) -> None:
    script = 'CHECK=$("$PYTHON" "$CLONE/deploy.py" check)\n"$PYTHON" "${CLONE}/tools/helper.py"\n'
    (tmp_path / "deploy.py").write_text("", "utf-8")
    assert missing_clone_paths(script, tmp_path) == ["tools/helper.py"]


def test_an_update_with_no_later_release_redeploys_the_release(installation: Installation) -> None:
    installation.fixture_commit()
    result = installation.update()
    assert_deployed(result, f"UP_TO_DATE {installation.release}")
    assert installation.deployed_commit() == git(installation.clone, "rev-parse", f"refs/tags/{installation.release}")


def test_a_patch_release_updates_to_it(installation: Installation) -> None:
    patch = next_release(installation.release, breaking=False)
    commit = installation.fixture_commit(patch)
    before = git(installation.clone, "rev-parse", "--short", "HEAD")
    result = installation.update()
    assert_deployed(result, f"UPDATED {before}..{patch}")
    assert installation.deployed_commit() == commit


def test_a_breaking_release_stops_until_crossed_then_deploys_moving_renamed_skills(
    installation: Installation,
) -> None:
    breaking = next_release(installation.release, breaking=True)
    commit = installation.fixture_commit(breaking)
    manifest = installation.manifest()
    stopped = installation.update()
    shown = f"stdout:\n{stopped.stdout}\nstderr:\n{stopped.stderr}"
    assert stopped.stdout.splitlines()[:1] == [f"MAJOR_UPDATE {installation.release}..{breaking}"], shown
    assert stopped.returncode == 1, shown
    assert installation.manifest() == manifest, "the stopped update changed the installation"

    before = git(installation.clone, "rev-parse", "--short", "HEAD")
    crossed = installation.update("--cross-major")
    assert_deployed(crossed, f"UPDATED {before}..{breaking}")
    assert f"CROSSED {installation.release}..{breaking}" in crossed.stdout.splitlines(), crossed.stdout
    assert installation.deployed_commit() == commit
    # Each skill the current tree renamed is installed under its new name alone.
    renamed: dict[str, str] = json.loads((REPOSITORY_ROOT / "source.json").read_text("utf-8")).get("renamed", {})
    skills = installation.home / SKILLS
    for old, new in renamed.items():
        assert not (skills / old).exists(), f"{old}, renamed {new}, is still installed"
        assert (skills / new).is_dir(), f"{new}, renamed from {old}, is not installed"


def test_the_update_fails_when_a_commit_moves_a_path_the_release_reads(installation: Installation) -> None:
    """The guard itself: a fixture commit that moves deploy.py, which the release's update.sh runs from the clone,
    fails the assertions the update tests above hold."""
    patch = next_release(installation.release, breaking=False)
    installation.fixture_commit(patch, moved=("deploy.py", "moved-deploy.py"))
    before = git(installation.clone, "rev-parse", "--short", "HEAD")
    result = installation.update()
    with pytest.raises(AssertionError):
        assert_deployed(result, f"UPDATED {before}..{patch}")
