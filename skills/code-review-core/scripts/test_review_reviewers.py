from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_reviewers import (
    entrypoint_manifest,
    frontmatter_value,
    inspect_configured_skill,
    inspect_skill,
    manifest_location,
    repository_files,
    resolve_reviewer,
)
from review_runtime import CommandResult, RuntimeContractError

SCRIPT_DIRECTORY = Path(__file__).resolve().parent

FILES = {".claude/agents/review.md", ".claude/agents/db-review.md", "docs/rules.md", "src/A.cs"}
COMMIT = "a" * 40
CHECKOUT = Path("C:/fixture/checkout")
SKILL = ".claude/skills/review/SKILL.md"


def inspect(text: str, skill: str = ".claude/agents/review.md"):
    return inspect_skill(skill, text, "a" * 40, FILES)


class FakeGit:
    """Answers the git commands the reviewer resolution runs, from an in-memory tree at COMMIT.

    A bytes value is a file that is not UTF-8: it is decoded with surrogateescape, as `subprocess_runner` decodes
    what git prints, so each undecodable byte survives as a lone surrogate.
    """

    def __init__(self, files: Mapping[str, str | bytes], modes: dict[str, str] | None = None) -> None:
        self.files = files
        self.modes = modes or {}
        self.calls: list[list[str]] = []

    def __call__(self, arguments: Sequence[str]) -> CommandResult:
        arguments = list(arguments)
        self.calls.append(arguments)
        if arguments[:3] != ["git", "-C", str(CHECKOUT)]:
            return CommandResult(128, "", f"fatal: unexpected {arguments}")
        command = arguments[3:]
        if command == ["ls-tree", "-r", "--name-only", "-z", COMMIT]:
            return CommandResult(0, "".join(f"{path}\0" for path in sorted(self.files)), "")
        if command[:3] == ["ls-tree", COMMIT, "--"] and len(command) == 4:
            path = command[3]
            if path not in self.files:
                return CommandResult(0, "", "")
            return CommandResult(0, f"{self.modes.get(path, '100644')} blob {'b' * 40}\t{path}\n", "")
        if command[0] == "show" and command[1].startswith(f"{COMMIT}:"):
            content = self.files[command[1].split(":", 1)[1]]
            text = content.decode("utf-8", "surrogateescape") if isinstance(content, bytes) else content
            return CommandResult(0, text, "")
        return CommandResult(128, "", f"fatal: unexpected {command}")


class InspectionTests(unittest.TestCase):
    def test_tool_lists_in_every_frontmatter_form(self) -> None:
        for frontmatter, expected in (
            ("tools: Read, Grep, Glob", ["Read", "Grep", "Glob"]),
            ('allowed-tools: ["Bash", "Read"]', ["Bash", "Read"]),
            ("tools:\n  - Read\n  - 'Agent'", ["Read", "Agent"]),
        ):
            with self.subTest(frontmatter=frontmatter):
                self.assertEqual(expected, inspect(f"---\nname: x\n{frontmatter}\n---\nReview it.\n").tools)
        self.assertIsNone(inspect("---\nname: x\n---\nReview it.\n").tools, "no list inherits every tool")
        self.assertIsNone(inspect("No frontmatter at all.\n").tools)

    def test_delegation_needs_both_the_tool_and_the_instruction(self) -> None:
        cases = {
            "yes": "---\nname: x\n---\nStart a subagent per area.\n",
            "no": "---\ntools: Read, Grep\n---\nSpawn a subagent per area.\n",  # the tools forbid it
            "unknown": "---\ntools: Read, Agent(general-purpose)\n---\nReview the change.\n",
        }
        for expected, text in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(expected, inspect(text).delegates)
        self.assertEqual("no", inspect("---\nname: x\n---\nReview the change carefully.\n").delegates)
        self.assertEqual([], inspect(cases["no"]).evidence, "evidence is moot when the tools forbid delegation")

    def test_named_agents_count_as_delegation_and_as_references(self) -> None:
        result = inspect("---\nname: x\n---\nAsk db-review about SQL changes.\nSee `docs/rules.md`.\n")
        self.assertEqual("yes", result.delegates)
        self.assertEqual([(4, "Ask db-review about SQL changes.")], result.evidence, "line numbers count the file")
        self.assertEqual([".claude/agents/db-review.md", "docs/rules.md"], result.references)
        self.assertNotIn(".claude/agents/review.md", result.references, "the skill never references itself")

    def test_references_come_from_code_spans_links_and_bare_paths_that_exist(self) -> None:
        result = inspect("Read [the rules](docs/rules.md#style), then src/A.cs and `missing/file.md`.\n")
        self.assertEqual(["docs/rules.md", "src/A.cs"], result.references)

    def test_an_entrypoint_carries_what_the_skill_names_and_needs_no_delegation(self) -> None:
        result = inspect("---\ntools: Read\n---\nApply `docs/rules.md`.\n")
        manifest = entrypoint_manifest("solo", result)
        self.assertEqual(".claude/agents/review.md", manifest["entrypoint"])
        self.assertEqual(["docs/rules.md"], manifest["resources"])
        self.assertNotIn("agent-delegation", manifest["required_capabilities"])

    def test_frontmatter_edge_forms(self) -> None:
        for text, expected in (
            ("---\ntools: []\n---\nBody.\n", []),
            ("---\r\ntools: Read\r\n---\r\nBody.\r\n", ["Read"]),
            ("---\nallowed-tools: Bash\ntools: Read\n---\nBody.\n", ["Read"]),
            ("---\ntools:\n  - Read\nmetadata:\n  - Agent\n---\nBody.\n", ["Read"]),
            ("\n---\ntools: Read\n---\n", None),
        ):
            with self.subTest(text=text):
                self.assertEqual(expected, inspect(text).tools)

    def test_malformed_frontmatter_fails_with_the_readers_message(self) -> None:
        # A grant the shared reader cannot read fails the review: guessing it once made a skill look unable to delegate.
        prefix = "The review skill .claude/agents/review.md has frontmatter that cannot be read: "
        for text, message in (
            ("---\ntools:\n  read: true\n---\nSpawn reviewers.\n", "tools: a nested mapping is not supported"),
            ("---\ntools: Read\ntools: Agent\n---\nReview it.\n", "tools appears more than once"),
            (
                "---\nname: x\njust prose\ntools: Read\n---\nSpawn reviewers.\n",
                "frontmatter line 2 is not a 'key: value' line",
            ),
            ("---\ntools: Read\nSpawn reviewers.\n", "frontmatter is not closed"),
            ("---\ntools: *\n---\nReview it.\n", "tools: an alias is not supported"),
        ):
            with self.subTest(text=text), self.assertRaises(RuntimeContractError) as raised:
                inspect(text)
            self.assertEqual(prefix + message, str(raised.exception))

    def test_frontmatter_rows_the_shared_reader_reads(self) -> None:
        for text, tools, delegates in (
            ("---\nTools: Read\n---\nSpawn reviewers.\n", None, "yes"),  # `Tools` is not `tools`: all are inherited
            ("---\ntools: Read # note\n---\nSpawn reviewers.\n", ["Read"], "no"),
            ('---\ntools: ["Bash(a,b)", Read]\n---\nSpawn reviewers.\n', ["Bash(a,b)", "Read"], "no"),
            (" ---\ntools: Read\n---\nSpawn reviewers.\n", None, "yes"),  # not a delimiter, so no frontmatter
            ("---\nallowed-tools: Read Agent\n---\nSpawn reviewers.\n", ["Read", "Agent"], "yes"),
            ("---\nallowed-tools: Read, Bash(git log:*)\n---\nSpawn reviewers.\n", ["Read", "Bash(git log:*)"], "no"),
            ("---\ntools: Bash(a,b) Read\n---\nSpawn reviewers.\n", ["Bash(a,b)", "Read"], "no"),
        ):
            with self.subTest(text=text):
                result = inspect(text)
                self.assertEqual((tools, delegates), (result.tools, result.delegates))

    def test_every_delegation_branch_and_its_reason(self) -> None:
        for text, delegates, reason in (
            (
                "---\ntools: Read, Task\n---\nSpawn reviewers.\n",
                "yes",
                "it may start subagents and its text says it does",
            ),
            ("---\nname: x\n---\nSpawn reviewers.\n", "yes", "it may start subagents and its text says it does"),
            (
                '---\ntools: "*"\n---\nReview it.\n',
                "unknown",
                "its tool list grants Agent or Task, but its text never says it starts one",
            ),
            (
                "---\nallowed-tools: Agent\n---\nReview it.\n",
                "unknown",
                "its tool list grants Agent or Task, but its text never says it starts one",
            ),
            ("---\ntools: Read\n---\nSpawn reviewers.\n", "no", "its tool list grants neither Agent nor Task"),
            ("---\ntools: []\n---\nSpawn reviewers.\n", "no", "its tool list grants neither Agent nor Task"),
            ("---\nname: x\n---\nReview it.\n", "no", "its text never mentions starting subagents"),
        ):
            with self.subTest(text=text):
                result = inspect(text)
                self.assertEqual((delegates, reason), (result.delegates, result.reason))
                self.assertEqual("a" * 40, result.commit)
                self.assertEqual(".claude/agents/review.md", result.skill)

    def test_delegation_phrases(self) -> None:
        for line in (
            "Use a sub-agent.",
            "Two subagents run.",
            "Set subagent_type.",
            "Call the Agent tool.",
            "Use the task tool.",
            "It spawns reviewers.",
            "Spawned workers report.",
            "Keep spawning.",
        ):
            with self.subTest(line=line):
                self.assertEqual([(4, line)], inspect(f"---\nname: x\n---\n{line}\n").evidence)
        for line in ("Act as an agent.", "The spawner runs.", "Be subagentic.", "Track the task."):
            with self.subTest(line=line):
                self.assertEqual([], inspect(f"---\nname: x\n---\n{line}\n").evidence)

    def test_evidence_skips_the_frontmatter_and_is_normalized(self) -> None:
        long = "Spawn " + "x" * 200
        result = inspect(f"---\ndescription: uses subagents\n---\n  Start   a\tsubagent.  \n{long}\n")
        self.assertEqual([(4, "Start a subagent."), (5, long[:160])], result.evidence)
        without = inspect(" ---\ndescription: uses subagents\n---\n")
        self.assertEqual(
            [(2, "description: uses subagents")], without.evidence, "text with no frontmatter is body from line 1"
        )

    def test_named_agents_live_in_agent_directories_and_match_whole_names(self) -> None:
        files = {
            ".claude/agents/db-review.md",
            ".claude/agents/team/sec.md",
            ".claude/agents/notes.txt",
            "docs/style.md",
            SKILL,
        }

        def named(line: str) -> list[str]:
            return inspect_skill(SKILL, f"---\nname: x\n---\n{line}\n", COMMIT, files).references

        self.assertEqual([".claude/agents/db-review.md"], named("Ask db-review."))
        self.assertEqual([".claude/agents/team/sec.md"], named("Ask `sec` about secrets"))
        self.assertEqual([".claude/agents/team/sec.md"], named("Ask the sec agent about secrets"))
        for line in (
            "Ask my-db-review.",
            "Ask db-reviewer.",
            "Ask DB-REVIEW.",
            "Follow style.",
            "Read notes.",
            "Ask sec about secrets",
        ):
            with self.subTest(line=line):
                self.assertEqual([], named(line))
        forbidden = inspect_skill(SKILL, "---\ntools: Read\n---\nAsk db-review.\n", COMMIT, files)
        self.assertEqual(("no", []), (forbidden.delegates, forbidden.evidence))
        self.assertEqual(
            [".claude/agents/db-review.md"], forbidden.references, "a named agent is still a file the skill needs"
        )

    def test_a_common_word_agent_name_in_prose_is_not_delegation(self) -> None:
        files = {".claude/agents/review.md", ".claude/skills/x/SKILL.md"}
        result = inspect_skill(
            ".claude/skills/x/SKILL.md",
            "---\nname: x\n---\nRead the diff, then review it and report findings.\n",
            COMMIT,
            files,
        )
        self.assertEqual(
            ("no", "its text never mentions starting subagents", [], []),
            (result.delegates, result.reason, result.evidence, result.references),
        )
        self.assertEqual([], entrypoint_manifest("solo", result)["resources"], "no agent file is materialized")

    def test_a_common_word_agent_name_in_an_agent_context_is_delegation(self) -> None:
        files = {".claude/agents/review.md", ".claude/agents/SecReview.md", ".claude/skills/x/SKILL.md"}

        def inspected(line: str):
            return inspect_skill(".claude/skills/x/SKILL.md", f"---\nname: x\n---\n{line}\n", COMMIT, files)

        result = inspected("Start the review agent.")
        self.assertEqual(
            ("yes", [(4, "Start the review agent.")], [".claude/agents/review.md"]),
            (result.delegates, result.evidence, result.references),
        )
        for line in (
            "Ask `review` about it.",
            "Hand it to the review subagent.",
            "Pass it to the review Agent.",
            "Delegate the diff to review.",
            "Run the agent named review.",
            "Spawn review for each area.",
        ):
            with self.subTest(line=line):
                self.assertEqual(
                    ("yes", [".claude/agents/review.md"]), (inspected(line).delegates, inspected(line).references)
                )
        for line in (
            "Start with the diff, then review it.",
            "Review the change.",
            "Run the tests and review them.",
            "Read `docs/review-notes.md`, then review it.",
            "Agents aside, review it.",
            "Ask Review about it.",
        ):
            with self.subTest(line=line):
                self.assertEqual(
                    ("no", [], []), (inspected(line).delegates, inspected(line).evidence, inspected(line).references)
                )
        self.assertEqual(
            [".claude/agents/SecReview.md"],
            inspected("Ask SecReview about secrets.").references,
            "a name that is not a plain word keeps the whole-word match",
        )

    def test_an_agent_named_in_both_agent_directories_references_both_files_in_every_process(self) -> None:
        # Agents were keyed by name, so one of the two files was dropped, and which one followed the set's
        # iteration order, which changes with PYTHONHASHSEED: the materialized reviewer differed run to run.
        files = [".github/agents/db-review.md", ".claude/agents/db-review.md", SKILL]
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(SCRIPT_DIRECTORY)!r})\n"
            "from review_reviewers import inspect_skill\n"
            f"print(inspect_skill({SKILL!r}, 'Ask db-review about SQL.\\n', {COMMIT!r}, set({files!r})).references)\n"
        )
        seen = set()
        for seed in ("0", "1", "2", "3", "4", "5", "6", "7"):
            result = subprocess.run(
                [sys.executable, "-B", "-c", script],
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONHASHSEED": seed},
                check=True,
            )
            seen.add(result.stdout.strip())
        self.assertEqual({"['.claude/agents/db-review.md', '.github/agents/db-review.md']"}, seen)

    def test_reference_paths_are_normalized_and_must_be_repository_files(self) -> None:
        result = inspect("[a](./docs/rules.md#x) `./src/A.cs` /src/A.cs rules.md `.claude/agents/review.md`\n")
        self.assertEqual(["docs/rules.md", "src/A.cs"], result.references)
        self.assertEqual(
            [],
            inspect("Read /src/A.cs and rules.md.\n").references,
            "an absolute path or a bare file name is not a repository path",
        )

    def test_frontmatter_value(self) -> None:
        text = (
            "---\nname: x\nModel: 'sonnet'\nmodel: opus # strong\neffort: \"high\"\nempty:\nquoted: ''\n---\n"
            "model: haiku\n"
        )
        self.assertEqual("opus", frontmatter_value("db.md", text, "model"), "`Model` is another key; # is a comment")
        self.assertEqual("high", frontmatter_value("db.md", text, "effort"))
        self.assertIsNone(frontmatter_value("db.md", text, "EFFORT"), "keys are matched exactly")
        self.assertIsNone(frontmatter_value("db.md", text, "empty"))
        self.assertIsNone(frontmatter_value("db.md", text, "quoted"))
        self.assertIsNone(frontmatter_value("db.md", text, "missing"))
        self.assertIsNone(frontmatter_value("db.md", "---\nname: x\n---\nmodel: haiku\n", "model"), "body is not read")
        self.assertIsNone(frontmatter_value("db.md", "model: haiku\n", "model"))
        self.assertIsNone(frontmatter_value("db.md", "", "model"))
        for text, message in (
            ("---\nmodel: haiku\n", "frontmatter is not closed"),
            ("---\nmodel: [haiku]\n---\n", "model must be a single value, not a list"),
        ):
            with self.subTest(text=text), self.assertRaises(RuntimeContractError) as raised:
                frontmatter_value("Specialist profile db.md", text, "model")
            self.assertEqual(
                f"Specialist profile db.md has frontmatter that cannot be read: {message}", str(raised.exception)
            )

    def test_entrypoint_manifest_shape_and_validation(self) -> None:
        inspection = inspect("---\ntools: Read\n---\nApply `docs/rules.md` and `src/A.cs`.\n")
        self.assertEqual(
            {
                "schema_version": 1,
                "id": "solo",
                "protocol_version": 1,
                "supports": ["initial", "re-review"],
                "required_capabilities": ["read-diff", "write-result"],
                "entrypoint": ".claude/agents/review.md",
                "resources": ["docs/rules.md", "src/A.cs"],
                "agent_profiles": [],
            },
            entrypoint_manifest("solo", inspection),
        )
        with self.assertRaises(RuntimeContractError) as raised:
            entrypoint_manifest("Not A Slug", inspection)
        self.assertEqual("Adapter manifest id is invalid", str(raised.exception))


class RepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config = self.root / "config.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def raises(self, message: str, call, *arguments, **keywords) -> None:
        with self.assertRaises(RuntimeContractError) as raised:
            call(*arguments, **keywords)
        self.assertEqual(message, str(raised.exception))

    def test_repository_files_lists_the_commit_tree(self) -> None:
        git = FakeGit({"a b/c.md": "", "src/A.cs": ""})
        self.assertEqual({"a b/c.md", "src/A.cs"}, repository_files(CHECKOUT, COMMIT, git))
        self.assertEqual([["git", "-C", str(CHECKOUT), "ls-tree", "-r", "--name-only", "-z", COMMIT]], git.calls)
        self.assertEqual(set(), repository_files(CHECKOUT, COMMIT, FakeGit({})))
        self.raises(
            "fatal: not a tree object",
            repository_files,
            CHECKOUT,
            COMMIT,
            lambda arguments: CommandResult(128, "", "fatal: not a tree object\n"),
        )
        self.raises(
            "git command failed", repository_files, CHECKOUT, COMMIT, lambda arguments: CommandResult(1, "", "")
        )

    def test_a_path_that_is_not_utf8_is_left_out_of_the_listing(self) -> None:
        # No configuration, manifest, or skill text can name such a path, so it can never be a reviewer file.
        self.assertEqual({"ok.md"}, repository_files(CHECKOUT, COMMIT, FakeGit({"caf\udce9.md": "", "ok.md": ""})))
        checkout = self.root / "checkout"
        checkout.mkdir()

        def git(*arguments: str, data: bytes | None = None) -> bytes:
            result = subprocess.run(
                ["git", "-C", str(checkout), *arguments], input=data, capture_output=True, check=False
            )
            self.assertEqual(0, result.returncode, result.stderr)
            return result.stdout.strip()

        git("init", "-q")
        blob = git("hash-object", "-w", "--stdin", data=b"Rules\n")
        tree = git(
            "mktree", "-z", data=b"100644 blob " + blob + b"\tcaf\xe9.md\x00100644 blob " + blob + b"\tok.md\x00"
        )
        commit = git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit-tree",
            tree.decode("ascii"),
            "-m",
            "names",
        ).decode("ascii")
        self.assertEqual({"ok.md"}, repository_files(checkout, commit))

    def test_inspect_configured_skill(self) -> None:
        files = {SKILL: "﻿---\ntools: Read\n---\nApply `docs/rules.md`.\n", "docs/rules.md": "Rules\n"}
        inspection = inspect_configured_skill(CHECKOUT, COMMIT, SKILL, FakeGit(files))
        self.assertEqual(
            (SKILL, COMMIT, ["Read"], "no", ["docs/rules.md"]),
            (inspection.skill, inspection.commit, inspection.tools, inspection.delegates, inspection.references),
            "a byte-order mark does not hide the frontmatter",
        )
        self.raises(
            f"The configured review skill .claude/skills/other/SKILL.md does not exist at {'a' * 12}",
            inspect_configured_skill,
            CHECKOUT,
            COMMIT,
            ".claude/skills/other/SKILL.md",
            FakeGit(files),
        )
        self.raises(
            f"The review skill {SKILL} is not UTF-8 text",
            inspect_configured_skill,
            CHECKOUT,
            COMMIT,
            SKILL,
            FakeGit({SKILL: b"caf\xe9\n"}),
        )
        self.raises(
            f"Declared reviewer file is not a regular file: {SKILL}",
            inspect_configured_skill,
            CHECKOUT,
            COMMIT,
            SKILL,
            FakeGit(files, {SKILL: "120000"}),
        )

    def test_manifest_location(self) -> None:
        location = manifest_location({"manifest": True}, self.config, "Octo/Repo")
        assert location is not None, "a manifest set to true has a default location"
        self.assertEqual(self.config.parent, location.parents[3])
        self.assertEqual(
            ("reviewers", "octo", "repo", "manifest.json"),
            location.parts[-4:],
            "the default sits beside the config under the lowercased repository",
        )
        explicit = self.root / "elsewhere" / "manifest.json"
        self.assertEqual(str(explicit), str(manifest_location({"manifest": str(explicit)}, self.config, "Octo/Repo")))
        for reviewer in ({}, {"manifest": None}, {"manifest": False}):
            with self.subTest(reviewer=reviewer):
                self.assertIsNone(manifest_location(reviewer, self.config, "Octo/Repo"))

    def manifest(self, entrypoint: str = SKILL) -> dict:
        return {
            "schema_version": 1,
            "id": "repo-review",
            "protocol_version": 1,
            "supports": ["initial"],
            "required_capabilities": ["read-diff", "write-result"],
            "entrypoint": entrypoint,
            "resources": [],
            "agent_profiles": [],
        }

    def resolve(self, reviewer: dict, git: FakeGit):
        return resolve_reviewer(
            reviewer, checkout=CHECKOUT, commit=COMMIT, config_path=self.config, repository="Octo/Repo", runner=git
        )

    def test_a_repository_manifest_is_read_from_the_commit_without_inspecting_a_skill(self) -> None:
        git = FakeGit({".review/manifest.json": json.dumps(self.manifest())})
        resolved = self.resolve({"id": "r", "manifest_path": ".review/manifest.json", "skill": "missing/SKILL.md"}, git)
        self.assertEqual(
            (self.manifest(), "repository-manifest", ".review/manifest.json", None, None),
            (resolved.manifest, resolved.source, resolved.location, resolved.local_root, resolved.inspection),
        )
        self.assertNotIn(["git", "-C", str(CHECKOUT), "ls-tree", "-r", "--name-only", "-z", COMMIT], git.calls)

    def test_a_local_manifest_runs_even_a_delegating_skill(self) -> None:
        git = FakeGit({SKILL: "---\nname: x\n---\nStart a subagent per area.\n"})
        default = self.root / "reviewers" / "octo" / "repo" / "manifest.json"
        explicit = self.root / "elsewhere" / "manifest.json"
        for path in (default, explicit):
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(self.manifest()), encoding="utf-8")
        for setting, path in ((True, default), (str(explicit), explicit)):
            with self.subTest(setting=setting):
                resolved = self.resolve({"id": "r", "skill": SKILL, "manifest": setting}, git)
                self.assertEqual(
                    (self.manifest(), "local-manifest", str(path), path.parent),
                    (resolved.manifest, resolved.source, resolved.location, resolved.local_root),
                )
                self.assertEqual("yes", resolved.inspection.delegates)

    def test_the_skill_must_exist_even_with_a_local_manifest(self) -> None:
        self.raises(
            f"The configured review skill {SKILL} does not exist at {'a' * 12}",
            self.resolve,
            {"id": "r", "skill": SKILL, "manifest": True},
            FakeGit({}),
        )

    def test_a_skill_that_starts_subagents_needs_a_manifest(self) -> None:
        git = FakeGit({SKILL: "---\nname: x\n---\nReview.\nStart   a subagent per area.\n"})
        self.raises(
            f"The review skill {SKILL} starts its own subagents (line 5: Start a subagent per area.), which fails "
            "when it runs as a reviewer subagent. Give it a specialists manifest (reviewer.manifest); see "
            "inspect-reviewer.",
            self.resolve,
            {"id": "r", "skill": SKILL},
            git,
        )

    def test_a_skill_that_may_not_delegate_runs_as_an_entrypoint(self) -> None:
        for text, delegates in (
            ("---\ntools: Read\n---\nApply `docs/rules.md`.\n", "no"),
            ("---\ntools: Read, Agent\n---\nApply `docs/rules.md`.\n", "unknown"),
        ):
            with self.subTest(delegates=delegates):
                git = FakeGit({SKILL: text, "docs/rules.md": "Rules\n"})
                resolved = self.resolve({"id": "solo", "skill": SKILL, "manifest": False}, git)
                self.assertEqual(
                    ("skill", SKILL, None, delegates),
                    (resolved.source, resolved.location, resolved.local_root, resolved.inspection.delegates),
                )
                self.assertEqual(
                    ("solo", SKILL, ["docs/rules.md"]),
                    (resolved.manifest["id"], resolved.manifest["entrypoint"], resolved.manifest["resources"]),
                )


if __name__ == "__main__":
    unittest.main()
