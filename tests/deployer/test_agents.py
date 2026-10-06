"""Claude Code subagent definitions deployed alongside the skills that declare them (agents/<name>.md)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from harness import DeployerTestCase

from deployer import fsops, pipeline
from deployer.paths import Paths


def sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


class AgentDeploymentTests(DeployerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_source_json()
        self.content = self.make_agent("reviewer", 'Reviews one role: "quoted", colons: and #hashes stay literal.')
        self.make_skill("alpha", "Alpha", agent_deps=["reviewer"])
        self.make_config()

    @property
    def agent(self) -> Path:
        return self.claude_agents_dir / "reviewer.md"

    def test_a_skill_installs_its_agent_byte_for_byte_and_records_ownership(self) -> None:
        result = self.deploy_ok("--all")
        self.assertEqual(
            b"---\nname: reviewer\ndescription: Test agent reviewer\ntools: Read, Write\nmodel: inherit\n---\n\n"
            b'Reviews one role: "quoted", colons: and #hashes stay literal.\n',
            self.agent.read_bytes(),
        )
        self.assertEqual({"reviewer.md": {"hash": sha256(self.content)}}, self.owned("agents"))
        self.assertEqual(7, self.manifest()["manifest_version"])
        self.assertIn("reviewer.md (agent)", self.report_groups(result.output, "DEPLOYED")["INSTALLED"])
        again = self.deploy_ok("--all")
        self.assertIn("reviewer.md (agent)", self.report_groups(again.output, "DEPLOYED")["UNCHANGED"])
        dry = self.deploy_ok("--all", "--dry-run")
        self.assertIn("reviewer.md (agent)", self.report_groups(dry.output, "DRY RUN")["UNCHANGED"])

    def test_an_updated_agent_replaces_the_owned_copy(self) -> None:
        self.deploy_ok("--all")
        updated = self.make_agent("reviewer", "Reviews more carefully.")
        result = self.deploy_ok("--all")
        self.assertEqual(updated, self.agent.read_bytes())
        self.assertEqual(sha256(updated), self.owned("agents")["reviewer.md"]["hash"])
        self.assertIn("reviewer.md (agent)", self.report_groups(result.output, "DEPLOYED")["UPDATED"])

    def test_an_agent_no_selected_skill_needs_is_removed_unless_modified(self) -> None:
        self.deploy_ok("--all")
        self.make_skill("alpha", "Alpha")  # no longer declares the agent
        result = self.deploy_ok("--all")
        self.assertFalse(self.agent.exists())
        self.assertEqual({}, self.owned("agents"))
        self.assertIn("reviewer.md (agent, no selected skill needs it)",
                      self.report_groups(result.output, "DEPLOYED")["REMOVED"])
        self.make_skill("alpha", "Alpha", agent_deps=["reviewer"])
        self.deploy_ok("--all")
        self.agent.write_bytes(self.content + b"local edit\n")
        self.make_skill("alpha", "Alpha")
        result = self.deploy_ok("--all")
        self.assertEqual(self.content + b"local edit\n", self.agent.read_bytes(), "a modified agent is kept")
        self.assertIn("reviewer.md (agent, modified since last deploy)",
                      self.report_groups(result.output, "DEPLOYED")["PRESERVED"])

    def test_an_unmanaged_agent_file_is_skipped_unless_forced(self) -> None:
        self.write(self.agent, "my own reviewer\n")
        result = self.deploy_ok("--all")
        self.assertEqual("my own reviewer\n", self.agent.read_text(encoding="utf-8"))
        self.assertEqual({}, self.owned("agents"))
        self.assertIn("reviewer.md (agent, unmanaged and differs)", self.report_groups(result.output, "DEPLOYED")["SKIPPED"])
        dry = self.deploy_ok("--all", "--dry-run")
        self.assertIn("reviewer.md (agent, differs)", self.report_groups(dry.output, "DRY RUN")["CONFLICT"])
        forced = self.deploy_ok("--all", "--force-item", "reviewer.md")
        self.assertEqual(self.content, self.agent.read_bytes())
        self.assertIn("reviewer.md (agent, forced, was unmanaged, previous copy backed up)",
                      self.report_groups(forced.output, "DEPLOYED")["REPLACED"])
        backups = list((self.claude_agents_dir / ".backups").rglob("reviewer.md"))
        self.assertEqual(["my own reviewer\n"], [path.read_text(encoding="utf-8") for path in backups])

    def test_a_byte_identical_unmanaged_agent_is_adopted(self) -> None:
        self.agent.parent.mkdir(parents=True)
        self.agent.write_bytes(self.content)
        result = self.deploy_ok("--all")
        self.assertIn("reviewer.md (agent, byte-identical)", self.report_groups(result.output, "DEPLOYED")["ADOPTED"])
        self.assertIn("reviewer.md", self.owned("agents"))

    def test_invalid_agent_sources_fail_before_any_change(self) -> None:
        cases = {
            "unknown agent": (lambda: self.make_skill("alpha", "Alpha", agent_deps=["missing"]),
                              "Skill 'alpha' depends on unknown agent 'missing'"),
            "name mismatch": (lambda: self.make_agent("reviewer", declared="other"),
                              "Agent 'reviewer' frontmatter name 'other' does not match its file name"),
            "unreadable": (lambda: self.write(self.source / "agents" / "reviewer.md", "---\nname: reviewer\n"),
                           "Agent 'reviewer' frontmatter cannot be read: frontmatter is not closed"),
            "not markdown": (lambda: self.write(self.source / "agents" / "notes.txt", "x\n"),
                             "agents may contain only <name>.md agent definitions"),
            "bad name": (lambda: self.make_agent("Bad_Name"), "Agent name 'Bad_Name' does not match naming grammar"),
        }
        for name, (break_source, message) in cases.items():
            with self.subTest(name):
                break_source()
                self.deploy_fails("--all", pattern=message)
                self.assertFalse(self.agent.exists())
                self.assertFalse(self.manifest_file.exists())
                # Restore a valid source for the next case.
                for extra in (self.source / "agents").iterdir():
                    extra.unlink()
                self.make_agent("reviewer")
                self.make_skill("alpha", "Alpha", agent_deps=["reviewer"])

    def test_a_failed_agent_install_rolls_back_the_whole_run(self) -> None:
        self.deploy_ok("--all")
        self.make_skill("alpha", "Alpha updated", agent_deps=["reviewer"])
        updated = self.make_agent("reviewer", "Reviews more carefully.")
        real_move = fsops.move

        def failing_move(source: Path, destination: Path) -> None:
            if destination == self.agent and "staging" in source.parts:
                raise OSError("synthetic agent install failure")
            real_move(source, destination)

        with mock.patch("deployer.fsops.move", side_effect=failing_move):
            result = self.deploy_fails("--all", pattern="reconciling the current journal")
        self.assertIn("synthetic agent install failure", result.output)
        self.assertEqual(self.content, self.agent.read_bytes(), "the previous agent is restored")
        self.assertNotEqual(updated, self.agent.read_bytes())
        self.assertIn("Alpha\n", self.skill_text("alpha"))
        self.assertNotIn("updated", self.skill_text("alpha"))
        self.assertFalse((self.claude_agents_dir / "reviewer.md.deploying-bak").exists())
        self.assertEqual(sha256(self.content), self.owned("agents")["reviewer.md"]["hash"])

    def test_an_uncommitted_agent_journal_rolls_back_on_the_next_run(self) -> None:
        self.deploy_ok("--all")
        self.agent.rename(self.claude_agents_dir / "reviewer.md.deploying-bak")
        self.write(self.agent, "half-installed\n")
        run = self.home / ".claude" / "deployer" / "staging" / "20260101-000000-agent"
        lines = [
            {"op": "backup", "root": "claude-agents", "item": "reviewer.md", "from": "claude-agents/reviewer.md",
             "to": "claude-agents/reviewer.md.deploying-bak", "retain": False, "backup_hash": sha256(self.content)},
            {"op": "install", "root": "claude-agents", "item": "reviewer.md",
             "from": "staging/.claude-agents/reviewer.md", "to": "claude-agents/reviewer.md",
             "staged_hash": sha256(b"half-installed\n")},
        ]
        self.write(run / "journal.jsonl", "".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines))
        result = self.deploy_ok("--all")
        self.assertIn("Recovering uncommitted run 20260101-000000-agent (rolling back)...", result.output)
        self.assertEqual(self.content, self.agent.read_bytes())
        self.assertFalse((self.claude_agents_dir / "reviewer.md.deploying-bak").exists())

    def test_a_version_6_manifest_is_upgraded_and_older_deployers_are_locked_out(self) -> None:
        self.make_skill("alpha", "Alpha")
        self.deploy_ok("--all")
        data = self.manifest()
        data["manifest_version"] = 6
        del data["sources"]["test/skills"]["agents"]
        self.write_manifest(data)
        self.make_skill("alpha", "Alpha", agent_deps=["reviewer"])
        self.deploy_ok("--all")
        self.assertEqual(7, self.manifest()["manifest_version"], "saved as 7, which a version 6 deployer refuses")
        self.assertIn("reviewer.md", self.owned("agents"))

    def test_another_source_cannot_take_over_an_owned_agent(self) -> None:
        self.deploy_ok("--all")
        other = self.snapshot_source("other")
        document = json.loads((other / "source.json").read_text(encoding="utf-8"))
        document["id"] = "other/skills"
        (other / "source.json").write_text(json.dumps(document), encoding="utf-8")
        (other / "deploy-meta" / "alpha.json").rename(other / "deploy-meta" / "beta.json")
        (other / "skills" / "alpha").rename(other / "skills" / "beta")
        skill = other / "skills" / "beta" / "SKILL.md"
        skill.write_bytes(skill.read_bytes().replace(b"name: alpha", b"name: beta"))
        self.make_config("other/skills")
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            code = pipeline.run(["--all"], Paths(other, self.home), stdin=io.StringIO(""))
        self.assertNotEqual(0, code)
        self.assertIn("Agent 'reviewer.md' is owned by source 'test/skills'", captured.getvalue())
        self.assertEqual(self.content, self.agent.read_bytes())

    def test_malformed_agent_ownership_in_the_manifest_is_rejected(self) -> None:
        self.deploy_ok("--all")
        for bad in ("reviewer", "../reviewer.md", "Bad_Name.md"):
            with self.subTest(bad=bad):
                data = self.manifest()
                data["sources"]["test/skills"]["agents"] = {bad: {"hash": sha256(self.content)}}
                self.write_manifest(data)
                self.deploy_fails("--all", pattern="Manifest entry is malformed")


if __name__ == "__main__":
    unittest.main()
