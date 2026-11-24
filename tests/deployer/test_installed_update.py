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
from harness import REPOSITORY_ROOT, Deployment, forward, tree_state

sys.path.insert(0, str(REPOSITORY_ROOT / "skills" / "ejryan89-skill-core" / "scripts"))

from git_client import GitClient
from release_tags import last_release, release_order

from deployer import platform_support, source_commit

UPDATE = "update-coding-agent-skills"
# Where the deployer installs skills, their runtime adapters, and agents, and the script the update skill runs, under
# the home.
SKILLS = Path(".claude") / "skills"
ADAPTERS = Path(".agents") / "skills"
AGENTS = Path(".claude") / "agents"
INSTALLED_SCRIPT = SKILLS / UPDATE / "scripts" / "update.sh"
SOURCE = json.loads((REPOSITORY_ROOT / "source.json").read_text("utf-8"))
# How a user pins the installed release by hand: with the tag's short name, or with its commit.
PIN_FORMS = ("tag", "commit")
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


SKIP_ALLOWED = {
    "installation": "a release whose deployer refuses this platform, as every release before the macOS port refuses "
    "macOS, cannot be installed here, so there is nothing to update from; the first release that supports the "
    "platform runs the suite",
}


def release_supports_this_platform(release: str) -> bool:
    """Whether the release's deployer names this platform among those it runs on, read from its own
    platform_support.py's SUPPORTED_PLATFORMS line, so the answer holds for the code the release ships."""
    source = git(REPOSITORY_ROOT, "show", f"refs/tags/{release}:deployer/platform_support.py")
    declared = next((line for line in source.splitlines() if line.startswith("SUPPORTED_PLATFORMS = ")), "")
    assert declared, f"{release}'s deployer/platform_support.py declares no SUPPORTED_PLATFORMS"
    return f'"{sys.platform}"' in declared


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

    def installed_state(self) -> dict[Path, dict[str, str]]:
        """Each folder the deployer writes under the home, by its tree_state, without the __pycache__ folders the
        update's Python writes beside the installed scripts, which the deployer's hash of an item leaves out too. The
        rest of the home is left out, since the tools `deploy.py check` starts write their own caches there."""
        return {
            root: {
                path: state
                for path, state in tree_state(self.home / root).items()
                if "__pycache__" not in Path(path).parts
            }
            for root in (SKILLS, ADAPTERS, AGENTS)
        }

    def source_entry(self) -> dict[str, object]:
        """The manifest's entry for this repository's source."""
        sources = self.manifest()["sources"]
        assert isinstance(sources, dict), sources
        entry: dict[str, object] = sources[SOURCE["id"]]
        return entry

    def deploy(self, *options: str) -> subprocess.CompletedProcess[str]:
        """The clone's deployer, run as a user runs it by hand."""
        return run([sys.executable, "-B", str(self.clone / "deploy.py"), *options], self.environment)

    def pin_by_hand(self, form: str) -> tuple[str, str]:
        """Pin the installation at its release by hand, with the clone's deployer, as a user steps back from a
        regression: `deploy.py --all --at <ref>` with the tag's short name or its commit, which makes the pin the
        user's rather than the release channel's own. Returns the ref and the commit the update names the pin by."""
        commit = git(self.clone, "rev-parse", f"refs/tags/{self.release}^{{commit}}")
        ref = {"tag": self.release, "commit": commit}[form]
        pinned = self.deploy("--all", "--at", ref)
        assert pinned.returncode == 0, f"pinning with --at {ref} failed:\n{pinned.stdout}{pinned.stderr}"
        assert self.source_entry()["source_ref"] == ref, "the deployer did not record the user's pin as written"
        return ref, commit

    def hold_under_old_names(self, renamed: dict[str, str]) -> None:
        """Make the installation hold each renamed skill under its old name, as a release before the rename left it:
        its folder and its adapter's, its manifest ownership, and every record of the name in the manifest."""
        path = self.home / SKILLS / ".deploy-manifest.json"
        manifest = json.loads(path.read_text("utf-8"))
        entry = manifest["sources"][SOURCE["id"]]
        for old, new in renamed.items():
            for key, root in (("skills", SKILLS), ("wrappers", ADAPTERS)):
                if new in entry[key]:
                    entry[key][old] = entry[key].pop(new)
                    (self.home / root / new).rename(self.home / root / old)
        for record in entry["skills"].values():
            record["skill_deps"] = [renamed_back(name, renamed) for name in record["skill_deps"]]
        entry["requested_skills"] = [renamed_back(name, renamed) for name in entry["requested_skills"]]
        path.write_bytes(json.dumps(manifest, indent=2).encode("utf-8"))

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
    if not release_supports_this_platform(release):
        pytest.skip(f"{release}'s deployer refuses {sys.platform}, so no installation of it exists here to update")
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


def renamed_back(name: str, renamed: dict[str, str]) -> str:
    """The old name of a skill `renamed` maps an old name to, or the name itself."""
    return {new: old for old, new in renamed.items()}.get(name, name)


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
    # The next update reads the release this one recorded, so it finds the installation at the patch, not behind it.
    again = installation.update()
    assert_deployed(again, f"UP_TO_DATE {patch}")
    assert installation.deployed_commit() == commit


def test_a_later_release_on_the_releases_commit_leaves_the_pin_the_updates_own_to_the_next_release(
    installation: Installation,
) -> None:
    """A patch release tagged on the installed release's own commit: the update deploys it at that commit, the current
    deployer keeps the pin the update's own, and the release after it, on a new commit, updates without PINNED."""
    pinned = git(installation.origin, "rev-parse", f"refs/tags/{installation.release}^{{commit}}")
    retagged = next_release(installation.release, breaking=False)
    git(installation.origin, "tag", retagged, pinned)
    result = installation.update()
    assert_deployed(result, f"UP_TO_DATE {retagged}")
    assert installation.deployed_commit() == pinned
    following = next_release(retagged, breaking=False)
    commit = installation.fixture_commit(following)
    again = installation.update()
    assert_deployed(again, f"UPDATED {git(installation.clone, 'rev-parse', '--short', pinned)}..{following}")
    assert installation.deployed_commit() == commit


def test_a_breaking_release_stops_until_crossed_then_deploys_moving_renamed_skills(
    installation: Installation,
) -> None:
    # The release already ships each renamed skill under its new name, so the installation is made to hold the old
    # names first, as one deployed before the rename holds them; otherwise no move happens and nothing below can fail.
    # A skill the installed update.sh names, such as the ejryan89-skill-core it runs from, keeps its new name, since
    # the release's update cannot run without it.
    renamed: dict[str, str] = SOURCE.get("renamed", {})
    script = (installation.home / INSTALLED_SCRIPT).read_text("utf-8")
    moved = {old: new for old, new in renamed.items() if new not in script}
    assert moved, "source.json renames no skill the release's update runs without, so this case has no move to check"
    installation.hold_under_old_names(moved)
    skills = installation.home / SKILLS
    for old, new in moved.items():
        assert (skills / old).is_dir(), f"the installation does not hold {old}, renamed {new}"
        assert not (skills / new).exists(), f"the installation holds {new} beside {old}, its old name"
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
    # Each skill the current tree renamed was moved: the deployment reports the move, the manifest owns the new name
    # alone, and the skill is installed under its new name alone. Removing the old copy and installing the new one
    # leaves the same folders, so the report and the ownership are what tell a move from the two.
    report = Deployment().report_groups(crossed.stdout, "DEPLOYED")
    moves = sorted(f"{new} (from {old})" for old, new in moved.items())
    assert sorted(report.get("RENAMED", [])) == moves, crossed.stdout
    owned = installation.source_entry()["skills"]
    assert isinstance(owned, dict), owned
    for old, new in renamed.items():
        assert old not in owned, f"the manifest still owns {old}, renamed {new}"
        assert new in owned, f"the manifest does not own {new}, renamed from {old}"
        assert not (skills / old).exists(), f"{old}, renamed {new}, is still installed"
        assert (skills / new).is_dir(), f"{new}, renamed from {old}, is not installed"


def stopped_at_the_pin(installation: Installation, form: str) -> tuple[str, str, str]:
    """Pin the installed release by hand, publish a patch release after it, and run the installed update over the pin:
    it stops with PINNED alone and changes neither the home nor the clone's branch. Returns the pin's ref and commit
    and the patch release's commit."""
    ref, pinned = installation.pin_by_hand(form)
    patch = next_release(installation.release, breaking=False)
    commit = installation.fixture_commit(patch)
    installed = installation.installed_state()
    head = git(installation.clone, "rev-parse", "HEAD")
    stopped = installation.update()
    shown = f"stdout:\n{stopped.stdout}\nstderr:\n{stopped.stderr}"
    assert stopped.stdout.splitlines() == [f"PINNED {ref} {pinned}"], shown
    assert stopped.returncode == 1, shown
    assert installation.installed_state() == installed, "the update stopped at the pin changed the installation"
    assert git(installation.clone, "rev-parse", "HEAD") == head, "the update stopped at the pin moved the clone"
    return ref, pinned, commit


def replaced_pin_line(installation: Installation, ref: str, pinned: str) -> str:
    """The line the deployer prints when it replaces the user's pin, naming the pinned commit as check does."""
    return f"Replaced the deployment pinned with --at {ref} to {source_commit.describe(installation.clone, pinned)}."


# Each case below starts from the stop at the user's pin, which stopped_at_the_pin holds, rather than a case of its
# own that would deploy and update the release once more for the same assertions.
@pytest.mark.parametrize("form", PIN_FORMS)
def test_an_update_over_a_users_pin_stops_with_pinned_then_with_unpin_deploys_under_the_pin_of_its_own(
    installation: Installation, form: str
) -> None:
    """The update stops at the user's pin with PINNED and changes nothing; --unpin then deploys the release it stopped
    before and replaces the user's pin with the release channel's own, which the next release moves without
    --unpin."""
    ref, pinned, commit = stopped_at_the_pin(installation, form)
    patch = next_release(installation.release, breaking=False)
    before = git(installation.clone, "rev-parse", "--short", "HEAD")
    unpinned = installation.update("--unpin")
    assert_deployed(unpinned, f"UPDATED {before}..{patch}")
    assert replaced_pin_line(installation, ref, pinned) in unpinned.stdout.splitlines(), unpinned.stdout
    assert installation.deployed_commit() == commit
    assert installation.source_entry()["source_ref"] == f"refs/tags/{patch}"
    following = next_release(patch, breaking=False)
    later = installation.fixture_commit(following)
    again = installation.update()
    assert_deployed(again, f"UPDATED {git(installation.clone, 'rev-parse', '--short', commit)}..{following}")
    assert installation.deployed_commit() == later


@pytest.mark.parametrize("form", PIN_FORMS)
def test_an_update_over_a_users_pin_stops_with_pinned_then_deploying_the_updated_clone_with_unpin_clears_it(
    installation: Installation, form: str
) -> None:
    """The other route from the update's PINNED: update the clone, then `deploy.py --all --unpin`, which deploys the
    clone's HEAD unpinned, so the next update deploys without --unpin."""
    ref, pinned, commit = stopped_at_the_pin(installation, form)
    git(installation.clone, "pull", "--quiet", "--ff-only", "origin", "main")
    deployed = installation.deploy("--all", "--unpin")
    shown = f"stdout:\n{deployed.stdout}\nstderr:\n{deployed.stderr}"
    assert deployed.returncode == 0, shown
    assert replaced_pin_line(installation, ref, pinned) in deployed.stdout.splitlines(), shown
    assert installation.deployed_commit() == commit
    assert "source_ref" not in installation.source_entry(), "the deployment kept a pin"
    patch = next_release(installation.release, breaking=False)
    again = installation.update()
    assert_deployed(again, f"UP_TO_DATE {patch}")
    assert installation.deployed_commit() == commit


def test_the_update_fails_when_a_commit_moves_a_path_the_release_reads(installation: Installation) -> None:
    """The guard itself: a fixture commit that moves deploy.py, which the release's update.sh runs from the clone,
    fails the assertions the update tests above hold."""
    patch = next_release(installation.release, breaking=False)
    installation.fixture_commit(patch, moved=("deploy.py", "moved-deploy.py"))
    before = git(installation.clone, "rev-parse", "--short", "HEAD")
    result = installation.update()
    with pytest.raises(AssertionError):
        assert_deployed(result, f"UPDATED {before}..{patch}")
