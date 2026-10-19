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


@dataclass(frozen=True, kw_only=True)
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
    # Whether each item is named for the skill it serves and stays with it: an item of this kind is kept while its
    # skill stays in the home, preserved or kept for another skill, and goes with it otherwise.
    follows_skill: bool = False

    def item_name(self, deployed: str) -> str:
        """The item's own name, as the source and --force-item give it: an agent's file name without its .md."""
        return deployed.removesuffix(self.suffix)

    @property
    def type_name(self) -> str:
        return "a directory" if self.directory else "a file"

    def has_expected_type(self, path: Path) -> bool:
        return path.is_dir() if self.directory else path.is_file()


SKILL = ItemKind(
    key="skills",
    label="",
    noun="skill",
    title="Skill",
    root="claude",
    staging="",
    directory=True,
    removal_reason="deselected or absent from source",
    unmanaged_reason=DIFFERS_FROM_SKILL,
    dependency_key="skill_deps",
)
SHARED = ItemKind(
    key="shared",
    label=SHARED_ASSET,
    noun=SHARED_ASSET,
    title="Shared asset",
    root="claude",
    staging="",
    directory=False,
    removal_reason="obsolete",
    unmanaged_reason=DIFFERS,
    dependency_key="shared_deps",
)
ADAPTER_KIND = ItemKind(
    key=ADAPTERS,
    label=ADAPTER,
    noun=ADAPTER,
    title="Runtime adapter",
    root="agents",
    staging=ADAPTER_STAGING,
    directory=True,
    removal_reason="obsolete",
    unmanaged_reason=DIFFERS,
    follows_skill=True,
)
AGENT_KIND = ItemKind(
    key="agents",
    label=AGENT,
    noun=AGENT,
    title="Agent",
    root="claude-agents",
    staging=AGENT_STAGING,
    directory=False,
    removal_reason="no selected skill needs it",
    unmanaged_reason=DIFFERS,
    suffix=".md",
    dependency_key="agent_deps",
)
# In the manifest's order, which is also the order every pass over the kinds checks, writes, and reports them in.
KINDS = (SKILL, SHARED, ADAPTER_KIND, AGENT_KIND)
BY_LABEL = {kind.label: kind for kind in KINDS}
# The kinds a skill depends on, which a skill left in place keeps.
DEPENDENCY_KINDS = tuple(kind for kind in KINDS if kind.dependency_key)
