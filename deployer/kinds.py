"""The four kinds of item a source deploys, described once for the manifest, the plan, the reports, and migration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .render import ADAPTER_STAGING, AGENT_STAGING

# Runtime adapters are recorded under "wrappers", their name before they were called adapters. Renaming the key
# would need a manifest version and a migration, so it stays.
ADAPTERS = "wrappers"

ADAPTER = "runtime adapter"
SHARED_ASSET = "shared asset"
AGENT = "agent"

MODIFIED = "modified since last deploy"
DIFFERS = "unmanaged and differs"
DIFFERS_FROM_SKILL = "unmanaged and differs from the rendered skill"


@dataclass(frozen=True)
class ItemKind:
    """Where one kind of item is recorded and deployed, how messages name it, and the reasons the plan gives."""

    key: str  # the manifest's key for this kind's ownership entries
    label: str  # how a report line names the kind; empty for skills, which reports name bare
    noun: str  # one item, as a count or a sentence names it
    title: str  # one item, as an error names it: "Skill 'alpha'"
    root: str  # the journal's name for the managed root
    staging: str  # the kind's directory under the run's staging directory
    directory: bool
    removal_reason: str
    unmanaged_reason: str
    suffix: str = ""  # what the deployed file name adds to the item's own name
    # The field of a skill's manifest entry that lists the items of this kind it was deployed with, for the kinds a
    # skill depends on; empty for the others.
    dependency_key: str = ""

    def item_name(self, deployed: str) -> str:
        """The item's own name, as the source and --force-item give it: an agent's file name without its .md."""
        return deployed.removesuffix(self.suffix)

    @property
    def type_name(self) -> str:
        return "a directory" if self.directory else "a file"

    def has_expected_type(self, path: Path) -> bool:
        return path.is_dir() if self.directory else path.is_file()


SKILL = ItemKind(
    "skills", "", "skill", "Skill", "claude", "", True, "deselected or absent from source", DIFFERS_FROM_SKILL
)
SHARED = ItemKind(
    "shared",
    SHARED_ASSET,
    SHARED_ASSET,
    "Shared asset",
    "claude",
    "",
    False,
    "obsolete",
    DIFFERS,
    dependency_key="shared_deps",
)
ADAPTER_KIND = ItemKind(
    ADAPTERS, ADAPTER, ADAPTER, "Runtime adapter", "agents", ADAPTER_STAGING, True, "obsolete", DIFFERS
)
AGENT_KIND = ItemKind(
    "agents",
    AGENT,
    AGENT,
    "Agent",
    "claude-agents",
    AGENT_STAGING,
    False,
    "no selected skill needs it",
    DIFFERS,
    ".md",
    dependency_key="agent_deps",
)
# In the manifest's order, which is also the order every pass over the kinds checks, writes, and reports them in.
KINDS = (SKILL, SHARED, ADAPTER_KIND, AGENT_KIND)
BY_LABEL = {kind.label: kind for kind in KINDS}
# The kinds a skill depends on, which a skill left in place keeps.
DEPENDENCY_KINDS = tuple(kind for kind in KINDS if kind.dependency_key)
