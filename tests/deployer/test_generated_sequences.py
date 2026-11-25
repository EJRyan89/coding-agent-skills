"""Generated deployment sequences: Hypothesis's stateful testing drives a fixture source and a throwaway home.

Each run builds a fresh source and home in a temporary folder with the harness's deployment(), then takes a sequence of
actions Hypothesis chooses: deploy, deploy forcing everything or one skill, include the opt-in skill, uninstall, edit
an installed skill, change, remove, or restore a source template, a deployment the deployer must refuse, and a
deployment killed partway through writing its journal. After each action it holds the home to a small model kept here,
apart from the deployer, of what the actions so far should have left:

- the harness compares the source around every run, so no run changes it, and the home around every refusal, so a
  refusal changes nothing;
- each skill the source owns is the render of the template it was last deployed from, or, once the user edited it, the
  user's bytes, which no later run replaces unless it forces that skill;
- a forced replacement leaves the user's bytes in a permanent backup, and no backup any run made is ever removed or
  changed, the uninstall included;
- the skills the manifest owns are the ones selected, with each edited skill the selection dropped, which is preserved,
  and the runtime adapters, the shared asset, and the agent follow them;
- a killed run that journaled a change leaves a recovery the dry run announces, one killed before it journaled any is
  only deleted, and the next run recovers either: a run killed before it committed, which its manifest shows by not
  naming it as the last run, is as if it never started, and one killed after is as if it had completed, however many
  runs are killed in a row; a completed run leaves no lock and no staged run behind.

Every run is deterministic, as "Generated tests" in docs/dependency-updates.md says, with the example and step budget
GENERATED names. A failing run prints the minimized action sequence, which rerunning the suite reproduces.
"""

from __future__ import annotations

import contextlib
import json
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, override
from unittest import mock

import pytest
from harness import REPOSITORY_ROOT, Deployment, Result, deployment, forward, tree_state
from hypothesis import HealthCheck, configuration, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from deployer import fsops, platform_support

# The budget: sequences, and actions in each, with no deadline and no too_slow health check, which fail on wall-clock
# time; derandomize and no database, so every run tries the same sequences.
GENERATED = settings(
    derandomize=True,
    database=None,
    max_examples=80,
    stateful_step_count=15,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# alpha is always in the source and needs the shared asset and the agent; beta is a plain skill the source can drop;
# gamma is opt-in, deployed only once a run includes it.
ALWAYS, PLAIN, OPT_IN = "alpha", "beta", "gamma"
SKILLS = (ALWAYS, PLAIN, OPT_IN)
SHARED, AGENT = "guide.md", "reviewer"
BACKUPS = ".backups"

# Why a killed run changes the home: it stops partway, leaving what it did and its journal for the next run to recover.
KILLED = "the run is killed partway through writing its journal and leaves its recovery behind"


def dead(pid: int) -> platform_support.ProcessStatus:
    """The owner a killed run left on the lock is not running, as after a real kill."""
    return platform_support.ProcessStatus(False, None)


@pytest.fixture(autouse=True, scope="module")
def hypothesis_storage() -> Iterator[None]:
    """Hypothesis's own files in a temporary folder of this suite's, never a .hypothesis folder in the checkout."""
    checkout = REPOSITORY_ROOT / ".hypothesis"
    before = hypothesis_files(checkout)
    with tempfile.TemporaryDirectory(prefix="hypothesis-") as folder:
        configuration.set_hypothesis_home_dir(folder)
        try:
            yield
        finally:
            configuration.set_hypothesis_home_dir(None)
    assert hypothesis_files(checkout) == before, "a generated test wrote into .hypothesis in the checkout"


def hypothesis_files(folder: Path) -> dict[str, tuple[int, int]] | None:
    """Each file in a .hypothesis folder with its size and modification time, or None when there is none: a folder an
    earlier run outside the runner left must stay as it was, as a missing one must stay missing."""
    if not folder.exists():
        return None
    return {
        path.relative_to(folder).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in folder.rglob("*")
        if path.is_file()
    }


def killed_at(line: int) -> mock._patch[mock.MagicMock | mock.AsyncMock]:
    """Kill the run as it writes its journal's line-th line, leaving half of it unterminated, as a killed process
    leaves it; a run that writes fewer lines completes."""
    real_append = fsops.append_line
    written = 0

    def append_line(path: Path, text: str) -> None:
        nonlocal written
        if path.name == "journal.jsonl":
            written += 1
            if written == line:
                with path.open("a", encoding="utf-8", newline="") as handle:
                    handle.write(text[: len(text) // 2])
                raise SystemExit(1)
        real_append(path, text)

    return mock.patch("deployer.fsops.append_line", side_effect=append_line)


class DeploymentSequences(RuleBasedStateMachine):
    """One fixture source and home, the actions taken on them, and the model of what those actions should leave."""

    def __init__(self) -> None:
        super().__init__()
        self.stack = contextlib.ExitStack()
        self.deployed: Deployment = self.stack.enter_context(deployment())
        d = self.deployed
        d.make_source_json(shared_assets={SHARED: "owner"})
        d.make_shared_asset(SHARED)
        d.make_agent(AGENT)
        d.make_config()
        # The model: each template's version in the source, or None once the source drops it.
        self.versions: dict[str, int | None] = dict.fromkeys(SKILLS, 0)
        for name in SKILLS:
            self.write_template(name)
        # The bytes each owned skill was last installed with, the user's bytes for each skill edited since, the
        # backups each forced replacement must leave, whether the opt-in skill is chosen, and every backup file seen.
        self.installed: dict[str, bytes] = {}
        self.edits: dict[str, bytes] = {}
        self.expected_backups: list[tuple[str, bytes]] = []
        self.chosen = False
        self.uninstalled = False
        self.backups_seen: dict[str, str] = {}
        self.pending = False
        self.killed_after_commit = False

    @override
    def teardown(self) -> None:
        self.stack.close()

    # The source.

    def write_template(self, name: str) -> None:
        version = self.versions[name]
        assert version is not None
        d = self.deployed
        if name == ALWAYS:
            d.make_skill(name, f"{name} v{version} {{{{HOME}}}}", ["HOME"], [SHARED], agent_deps=[AGENT])
        else:
            d.make_skill(name, f"{name} v{version} {{{{HOME}}}}", ["HOME"], opt_in=name == OPT_IN)

    def render(self, name: str) -> bytes:
        """The template as the deployer renders it, worked out here from the template's bytes: {{HOME}} replaced."""
        text = (self.deployed.source / "skills" / name / "SKILL.md").read_bytes()
        return text.replace(b"{{HOME}}", forward(self.deployed.home).encode())

    def present(self) -> set[str]:
        return {name for name, version in self.versions.items() if version is not None}

    def selected(self) -> set[str]:
        """The skills a deployment with --all selects: every one present but the opt-in skill, unless it was chosen."""
        return {name for name in self.present() if name != OPT_IN or self.chosen}

    # Runs.

    def run(self, *arguments: str, kill_at: int | None = None) -> Result | None:
        """Run a deployment; None when it was killed partway."""
        d = self.deployed
        journaled = self.journaled()
        if kill_at is None:
            result = d.deploy(*arguments, probe=dead)
        else:
            try:
                with killed_at(kill_at):
                    result = d.deploy(*arguments, changes_home=KILLED, probe=dead)
            except SystemExit:
                self.pending = True
                self.killed_after_commit = self.committed()
                return None
        assert result.code == 0, result.output
        if journaled:
            assert "Recovery complete." in result.output, result.output
        elif self.pending:
            assert "Dropped the unterminated last line" in result.output, result.output
        self.pending = False
        return result

    def committed(self) -> bool:
        """Whether the killed run committed before it was killed: the manifest names a run whose staging folder
        remains as the last run, the point after which recovery completes a run rather than rolling it back."""
        d = self.deployed
        if not d.manifest_file.is_file():
            return False
        last = json.loads(d.manifest_file.read_bytes()).get("last_run_id", "")
        return bool(last) and (d.home / ".claude" / "deployer" / "staging" / last).is_dir()

    def journaled(self) -> bool:
        """Whether a killed run left a journal holding a whole entry, which records a change to recover, worked out
        here from the journal's bytes: a run killed while writing its first entry left only that entry's first half."""
        staging = self.deployed.home / ".claude" / "deployer" / "staging"
        return staging.is_dir() and any(b"\n" in path.read_bytes() for path in staging.glob("*/journal.jsonl"))

    def completed(self, selected: set[str], forced: set[str]) -> None:
        """Bring the model up to a completed run that selected selected and forced forced."""
        owned = set(self.installed)
        for name in selected:
            if name in self.edits and name in owned:
                if name not in forced:
                    continue
                self.expected_backups.append((name, self.edits.pop(name)))
            self.installed[name] = self.render(name)
        for name in owned - selected:
            if name not in self.edits:
                del self.installed[name]

    def deploy_all(self, forced: set[str], *arguments: str, kill_at: int | None = None) -> None:
        if self.uninstalled:
            # An uninstall chose nothing, so --all no longer carries the opt-in skill a run before it chose.
            self.chosen = False
        selected = self.selected()
        if self.run("--all", *arguments, kill_at=kill_at) is None and not self.killed_after_commit:
            return
        self.uninstalled = False
        if OPT_IN not in self.present():
            self.chosen = False
        self.completed(selected, forced)

    # Rules.

    @initialize()
    def first_deployment(self) -> None:
        # Every sequence starts from a deployed home, so each action has installed skills to act on.
        self.deploy_all(set())

    @rule()
    def deploy(self) -> None:
        self.deploy_all(set())

    @rule()
    def deploy_forcing_everything(self) -> None:
        self.deploy_all(set(SKILLS), "--force")

    @rule(name=st.sampled_from(SKILLS))
    def deploy_forcing_one(self, name: str) -> None:
        # Only a skill the run installs can be forced; naming another is the refused rule's refusal.
        if name in self.selected() and not self.uninstalled:
            self.deploy_all({name}, "--force-item", name)

    @precondition(lambda self: self.versions[OPT_IN] is not None)
    @rule()
    def include_the_opt_in_skill(self) -> None:
        if self.uninstalled:
            self.chosen = False
        selected = self.selected() | {OPT_IN}
        if self.run("--all", "--include", OPT_IN) is None:
            return
        self.uninstalled = False
        self.chosen = True
        self.completed(selected, set())

    @rule()
    def uninstall(self) -> None:
        if self.run("--uninstall") is None:
            return
        self.uninstalled = True
        self.completed(set(), set())

    @rule(line=st.integers(min_value=1, max_value=6), force=st.booleans())
    def deploy_killed_partway(self, line: int, force: bool) -> None:
        # A new template version gives the run a change to journal, so the kill lands inside it.
        self.change_a_template(ALWAYS)
        self.deploy_all(set(SKILLS) if force else set(), *(("--force",) if force else ()), kill_at=line)

    @rule(name=st.sampled_from(SKILLS), text=st.sampled_from(("edited\n", "Ünïcödé ✓\n", "\n")))
    def edit_an_installed_skill(self, name: str, text: str) -> None:
        path = self.deployed.skills_dir / name / "SKILL.md"
        if self.pending or name not in self.installed:
            return
        self.deployed.append(path, text)
        self.edits[name] = path.read_bytes()

    @rule(name=st.sampled_from(SKILLS))
    def change_a_template(self, name: str) -> None:
        version = self.versions[name]
        if version is None:
            return
        self.versions[name] = version + 1
        self.write_template(name)

    @rule(name=st.sampled_from((PLAIN, OPT_IN)))
    def remove_or_restore_a_template(self, name: str) -> None:
        if self.versions[name] is None:
            self.versions[name] = 0
            self.write_template(name)
        else:
            self.versions[name] = None
            self.deployed.remove_skill(name)

    @rule(refusal=st.sampled_from(("--force-item", "--include")))
    def a_refused_deployment(self, refusal: str) -> None:
        # The harness holds the home unchanged around the refusal, a pending recovery included.
        self.deployed.deploy_fails("--all", refusal, "no-such-skill", pattern="no-such-skill", code=2, probe=dead)

    # Invariants.

    @invariant()
    def a_pending_recovery_is_announced_and_a_completed_run_leaves_nothing_behind(self) -> None:
        d = self.deployed
        staging = d.home / ".claude" / "deployer" / "staging"
        runs = sorted(path.name for path in staging.iterdir()) if staging.is_dir() else []
        if not self.pending:
            assert runs == []
            assert not d.lock_dir.exists()
            return
        assert runs
        before = tree_state(d.home)
        preview = d.deploy("--all", "--dry-run", probe=dead)
        assert preview.code == 0, preview.output
        announced = "Pending recovery: the next deployment will recover run" in preview.output
        assert announced == self.journaled(), preview.output
        assert tree_state(d.home) == before, "the dry run changed the home"

    @invariant()
    def each_owned_skill_holds_its_last_render_or_the_users_edit(self) -> None:
        if self.pending:
            return
        d = self.deployed
        manifest: dict[str, Any] = (
            json.loads(d.manifest_file.read_bytes())["sources"].get("test/skills", {})
            if d.manifest_file.is_file()
            else {}
        )
        owned = set(manifest.get("skills", {}))
        assert owned == set(self.installed), manifest
        assert set(manifest.get("wrappers", {})) == owned
        on_disk = (
            {path.name for path in d.skills_dir.iterdir() if path.is_dir() and path.name != BACKUPS}
            if d.skills_dir.is_dir()
            else set()
        )
        assert on_disk == owned
        adapters = (
            {path.name for path in d.agents_dir.iterdir() if path.name != BACKUPS} if d.agents_dir.is_dir() else set()
        )
        assert adapters == owned
        for name in owned:
            assert (d.skills_dir / name / "SKILL.md").read_bytes() == self.edits.get(name, self.installed[name]), name
        # The shared asset and the agent follow the one skill that needs them.
        assert (d.skills_dir / SHARED).is_file() == (ALWAYS in owned)
        assert (d.claude_agents_dir / f"{AGENT}.md").is_file() == (ALWAYS in owned)

    @invariant()
    def no_backup_is_ever_removed_or_changed_and_each_forced_edit_has_one(self) -> None:
        d = self.deployed
        found: dict[str, str] = {}
        for root in (d.skills_dir, d.agents_dir, d.claude_agents_dir):
            backups = root / BACKUPS
            if backups.is_dir():
                found |= {f"{root.name}/{BACKUPS}/{key}": value for key, value in tree_state(backups).items()}
        for path, state in self.backups_seen.items():
            assert found.get(path) == state, f"backup {path} was removed or changed"
        self.backups_seen = found
        if self.pending and self.killed_after_commit:
            # A run killed after it committed moves the edit into its permanent backup as recovery completes it.
            return
        kept = [
            (path.parent.name, path.read_bytes())
            for path in (d.skills_dir / BACKUPS).glob("*/*/SKILL.md")
            if (d.skills_dir / BACKUPS).is_dir()
        ]
        for backup in self.expected_backups:
            assert backup in kept, f"no permanent backup holds the edit of {backup[0]}"


def test_every_generated_deployment_sequence_keeps_the_deployers_invariants() -> None:
    run_state_machine_as_test(DeploymentSequences, settings=GENERATED)


def test_a_forced_run_killed_after_it_committed_is_completed_and_keeps_the_edit_in_a_backup() -> None:
    """The sequence a randomized run found while the model held every killed run to never having started: a forced
    run killed after it committed, as it journals the preservation of the user's edit, is completed by the next
    run's recovery, which keeps the edit in a permanent backup, and a later uninstall removes the skill it replaced."""
    machine = DeploymentSequences()
    try:
        machine.first_deployment()
        steps: list[Callable[[], None]] = [
            lambda: machine.deploy_forcing_everything(),
            lambda: machine.edit_an_installed_skill(ALWAYS, "edited\n"),
            lambda: machine.deploy_killed_partway(3, True),
            lambda: machine.deploy_killed_partway(3, False),
            lambda: machine.uninstall(),
        ]
        for step in steps:
            step()
            machine.a_pending_recovery_is_announced_and_a_completed_run_leaves_nothing_behind()
            machine.each_owned_skill_holds_its_last_render_or_the_users_edit()
            machine.no_backup_is_ever_removed_or_changed_and_each_forced_edit_has_one()
        assert [name for name, _ in machine.expected_backups] == [ALWAYS]
    finally:
        machine.teardown()
