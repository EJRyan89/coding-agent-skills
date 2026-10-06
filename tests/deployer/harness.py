"""Isolated fixture repositories and homes for deployer tests."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from deployer import configure, pipeline
from deployer.paths import Paths

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

SOURCE_ID = "test/skills"
# The grant shipped skills use: Bash and PowerShell for the skill's own scripts only. See "Granting tools" in
# docs/adding-a-skill.md.
OWN_SCRIPTS = 'python -B "${CLAUDE_SKILL_DIR}/scripts/*'
ALLOWED_TOOLS = json.dumps([f"Bash({OWN_SCRIPTS})", f"PowerShell({OWN_SCRIPTS})"])
REPORT_GROUP = re.compile(r"([A-Z][A-Z ]*[A-Z]) \(([0-9]+)\):")


def forward(path: Path) -> str:
    return str(path).replace("\\", "/")


@dataclass
class Result:
    code: int
    output: str


class DeployerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="deploy-test.")
        # Expand 8.3 short names such as RUNNER~1, which configured paths reject.
        self.root = Path(self._temporary.name).resolve()
        self.home = self.root / "home"
        self.source = self.root / "source"
        self.repos = self.root / "repos"
        for directory in (
            self.home / ".claude" / "skills",
            self.home / ".claude" / "deployer" / "config",
            self.home / ".claude" / "deployer" / "staging",
            self.source / "skills",
            self.source / "deploy-meta",
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    @property
    def paths(self) -> Paths:
        return Paths(self.source, self.home)

    @property
    def skills_dir(self) -> Path:
        return self.home / ".claude" / "skills"

    @property
    def agents_dir(self) -> Path:
        return self.home / ".agents" / "skills"

    @property
    def claude_agents_dir(self) -> Path:
        return self.home / ".claude" / "agents"

    @property
    def manifest_file(self) -> Path:
        return self.skills_dir / ".deploy-manifest.json"

    def make_source_json(
        self,
        source_id: str = SOURCE_ID,
        shared_assets: dict[str, str] | None = None,
        bundles: dict[str, Any] | None = None,
    ) -> None:
        document = {
            "id": source_id,
            "name": "Test Skills",
            "shared_assets": shared_assets or {},
            "bundles": bundles or {},
        }
        (self.source / "source.json").write_text(json.dumps(document, indent=4), encoding="utf-8")

    def make_skill(
        self,
        name: str,
        content: str = "# Test skill",
        required_vars: tuple[str, ...] | list[str] = (),
        shared_deps: tuple[str, ...] | list[str] = (),
        skill_deps: tuple[str, ...] | list[str] = (),
        selectable: bool = True,
        category: str | None = None,
        opt_in: bool = False,
        agent_deps: tuple[str, ...] | list[str] = (),
        description: str | None = None,
        user_only: bool = False,
    ) -> Path:
        """A skill whose description is the raw YAML scalar given; only the user may start it when user_only."""
        directory = self.source / "skills" / (f"{category}/{name}" if category else name)
        directory.mkdir(parents=True, exist_ok=True)
        description = description or f'"Test skill {name}"'
        flags = "disable-model-invocation: true\n" if user_only else ""
        (directory / "SKILL.md").write_bytes(
            (
                f"---\nname: {name}\ndescription: {description}\nallowed-tools: {ALLOWED_TOOLS}\n{flags}---\n\n"
                f"{content}\n"
            ).encode()
        )
        metadata = {
            "required_vars": list(required_vars),
            "shared_deps": list(shared_deps),
            "skill_deps": list(skill_deps),
            "selectable": selectable,
            **({"opt_in": True} if opt_in else {}),
            **({"agent_deps": list(agent_deps)} if agent_deps else {}),
        }
        (self.source / "deploy-meta" / f"{name}.json").write_text(json.dumps(metadata), encoding="utf-8")
        return directory

    def make_agent(self, name: str, body: str = "Review the change.", declared: str | None = None) -> bytes:
        """A Claude Code subagent definition under agents/; returns its exact bytes."""
        content = (
            f"---\nname: {declared or name}\ndescription: Test agent {name}\ntools: Read, Write\n"
            f"model: inherit\n---\n\n{body}\n"
        ).encode()
        target = self.source / "agents" / f"{name}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return content

    def make_shared_asset(self, name: str, content: str = "# Shared asset") -> None:
        (self.source / "skills" / name).write_bytes(f"{content}\n".encode())

    def config_file(self, source_id: str = SOURCE_ID) -> Path:
        digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:12]
        return self.home / ".claude" / "deployer" / "config" / f"{digest}.config"

    def make_config(self, source_id: str = SOURCE_ID, repos_root: Path | None = None, extra: str = "") -> Path:
        repos = repos_root or self.repos
        repos.mkdir(parents=True, exist_ok=True)
        path = self.config_file(source_id)
        path.write_bytes(f"_source_id={source_id}\nREPOS_ROOT={forward(repos)}\n{extra}".encode())
        return path

    def deploy(self, *arguments: str, stdin: str = "", **options: Any) -> Result:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            code = pipeline.run(list(arguments), self.paths, stdin=io.StringIO(stdin), **options)
        return Result(code, captured.getvalue())

    def repository_source(self) -> Path:
        """Copy this repository's deployable source, so a test deploys it even when run from a linked worktree."""
        destination = self.root / "repository"
        destination.mkdir()
        shutil.copy2(REPOSITORY_ROOT / "source.json", destination / "source.json")
        for directory in ("skills", "deploy-meta", "agents"):
            shutil.copytree(
                REPOSITORY_ROOT / directory,
                destination / directory,
                ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]"),
            )
        return destination

    def snapshot_source(self, name: str) -> Path:
        """Copy the current fixture source so it can be deployed later as a separate source."""
        destination = self.root / name
        shutil.copytree(self.source, destination)
        return destination

    def deploy_from(self, source: Path, *arguments: str, stdin: str = "") -> Result:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            code = pipeline.run(list(arguments), Paths(source, self.home), stdin=io.StringIO(stdin))
        self.assertEqual(0, code, captured.getvalue())
        return Result(code, captured.getvalue())

    def remove_skill(self, name: str) -> None:
        shutil.rmtree(self.source / "skills" / name)
        (self.source / "deploy-meta" / f"{name}.json").unlink()

    def configure(self, *arguments: str, stdin: str = "") -> Result:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            code = configure.run(list(arguments), self.paths, stdin=io.StringIO(stdin))
        return Result(code, captured.getvalue())

    def deploy_ok(self, *arguments: str, stdin: str = "", **options: Any) -> Result:
        result = self.deploy(*arguments, stdin=stdin, **options)
        self.assertEqual(0, result.code, result.output)
        return result

    def deploy_fails(self, *arguments: str, pattern: str, stdin: str = "", **options: Any) -> Result:
        result = self.deploy(*arguments, stdin=stdin, **options)
        self.assertNotEqual(0, result.code, result.output)
        self.assertRegex(result.output, pattern)
        return result

    def report_groups(self, output: str, title: str) -> dict[str, list[str]]:
        """Parse one '=== title ===' report into its action groups, checking each header's count."""
        lines = output.replace("\r\n", "\n").split("\n")
        groups: dict[str, list[str]] = {}
        counts: dict[str, int] = {}
        current: list[str] | None = None
        in_extra = False
        for line in lines[lines.index(f"=== {title} ===") + 1 :]:
            header = REPORT_GROUP.fullmatch(line)
            if header:
                current = groups.setdefault(header.group(1), [])
                counts[header.group(1)] = int(header.group(2))
                in_extra = False
            elif not line:
                current = None
            elif current is None or in_extra:
                continue
            elif line.startswith("    ") and current:
                current[-1] += " " + line.strip()  # a wrapped continuation of the previous item
            elif line.startswith("  "):
                current.append(line[2:])
            else:
                in_extra = True  # a diff or other detail printed under the previous item
        for action, items in groups.items():
            self.assertEqual(counts[action], len(items), f"{action} header count in:\n{output}")
        return groups

    def manifest(self) -> dict[str, Any]:
        return json.loads(self.manifest_file.read_text(encoding="utf-8"))

    def write_manifest(self, data: dict[str, Any]) -> None:
        self.manifest_file.write_text(json.dumps(data), encoding="utf-8")

    def owned(self, kind: str, source_id: str = SOURCE_ID) -> dict[str, Any]:
        return self.manifest()["sources"].get(source_id, {}).get(kind, {})

    def skill_text(self, name: str) -> str:
        return (self.skills_dir / name / "SKILL.md").read_text(encoding="utf-8")

    def write(self, path: Path, text: str) -> None:
        """Write fixture text with LF line endings, which read_text would not preserve on Windows."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def append(self, path: Path, text: str) -> None:
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def selection_number(self, name: str, bundle: bool = False) -> str:
        output = self.deploy("--dry-run", stdin="\n").output
        match = re.search(rf"^  \[[ *]\] ([0-9]+)\. {re.escape(name)}(?: \(([^)]*)\))?$", output, re.MULTILINE)
        self.assertIsNotNone(match, output)
        labels = (match.group(2) or "").split(", ")
        self.assertEqual(bundle, "bundle" in labels, output)
        return match.group(1)
