from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import fsops, platform_support
from .errors import DeployError


@dataclass(frozen=True)
class Paths:
    source_dir: Path
    home: Path

    @property
    def source_file(self) -> Path:
        return self.source_dir / "source.json"

    @property
    def meta_dir(self) -> Path:
        return self.source_dir / "deploy-meta"

    @property
    def skills_src(self) -> Path:
        return self.source_dir / "skills"

    @property
    def agents_src(self) -> Path:
        return self.source_dir / "agents"

    @property
    def dest_dir(self) -> Path:
        return self.home / ".claude" / "skills"

    @property
    def adapter_dest_dir(self) -> Path:
        return self.home / ".agents" / "skills"

    @property
    def agent_dest_dir(self) -> Path:
        """Claude Code subagent definitions; one owned Markdown file per agent."""
        return self.home / ".claude" / "agents"

    @property
    def copilot_skills_dir(self) -> Path:
        return self.home / ".copilot" / "skills"

    @property
    def deployer_dir(self) -> Path:
        return self.home / ".claude" / "deployer"

    @property
    def config_dir(self) -> Path:
        return self.deployer_dir / "config"

    @property
    def staging_root(self) -> Path:
        return self.deployer_dir / "staging"

    @property
    def lock_dir(self) -> Path:
        return self.deployer_dir / ".deploy.lock.d"

    @property
    def manifest_file(self) -> Path:
        return self.dest_dir / ".deploy-manifest.json"

    def config_file(self, source_id: str) -> Path:
        digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:12]
        return self.config_dir / f"{digest}.config"

    @property
    def managed_roots(self) -> tuple[Path, ...]:
        """Every directory the deployer reads recovery state from or writes into, beneath HOME."""
        return (
            self.dest_dir,
            self.adapter_dest_dir,
            self.agent_dest_dir,
            self.deployer_dir,
            self.config_dir,
            self.staging_root,
        )


CANARY_MARKER = ".deploy-canary-home"
CANARY_MARKER_TEXT = (
    b"A --canary-home deployment used this directory as a throwaway home. Delete the directory when done.\n"
)


def canary_home(value: str) -> Path:
    """The throwaway home a --canary-home deployment uses, refused unless it cannot be anyone's real home.

    It must be a real directory strictly inside the temporary directory, and either empty or already marked by an
    earlier canary deployment, so a second source can join it. The canonical path is returned, because a short
    name such as RUNNER~1 would be rejected as a configured value.
    """
    value = platform_support.normalize_path_input(value)
    if not platform_support.is_absolute(value):
        raise DeployError(f"ERROR: --canary-home must be an absolute path (got: {value})")
    home = Path(value)
    if platform_support.is_link(home) or platform_support.is_reparse_point(home):
        raise DeployError(f"ERROR: --canary-home must not be a symlink or junction: {platform_support.normalize(home)}")
    if not home.is_dir():
        raise DeployError(f"ERROR: --canary-home must be an existing directory: {platform_support.normalize(home)}")
    temporary = Path(platform_support.canonical_directory(tempfile.gettempdir()))
    resolved = Path(platform_support.canonical_directory(home))
    if resolved == temporary or not resolved.is_relative_to(temporary):
        raise DeployError(
            f"ERROR: --canary-home must be inside the temporary directory {platform_support.normalize(temporary)} "
            f"(got: {platform_support.normalize(resolved)})"
        )
    entries = [entry.name for entry in resolved.iterdir()]
    if entries and CANARY_MARKER not in entries:
        raise DeployError(
            "ERROR: --canary-home must be an empty directory or one an earlier --canary-home deployment used: "
            f"{platform_support.normalize(resolved)}"
        )
    return resolved


def claim_canary_home(home: Path) -> None:
    """Mark a validated canary home, so a later --canary-home deployment of another source may join it."""
    marker = home / CANARY_MARKER
    if not os.path.lexists(marker):
        fsops.write_atomic(marker, CANARY_MARKER_TEXT)


def validate_managed_roots(paths: Paths) -> None:
    """Reject a symlink, junction, or non-directory at HOME, any managed root, or any directory between them.

    A link anywhere on these paths, HOME included, would redirect recovery deletions or installs outside HOME.
    """
    home = Path(paths.home)
    for root in paths.managed_roots:
        parts = Path(root).relative_to(home).parts
        components = [home, *(home.joinpath(*parts[:depth]) for depth in range(1, len(parts) + 1))]
        for component in components:
            if platform_support.is_link(component) or platform_support.is_reparse_point(component):
                raise DeployError(
                    f"ERROR: Deployment path contains a symlink or junction: {platform_support.normalize(component)}",
                    "Replace it with a real directory, then retry.",
                )
            if os.path.lexists(component) and not component.is_dir():
                raise DeployError(
                    f"ERROR: Deployment path component is not a directory: {platform_support.normalize(component)}"
                )
