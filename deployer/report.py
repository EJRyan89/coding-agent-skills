"""The reports a deployment prints: report lines grouped by action, their labels, and shared warnings."""

from __future__ import annotations

import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import manifest, platform_support, source
from .kinds import ADAPTER, DIFFERS, DIFFERS_FROM_SKILL, KINDS, MODIFIED
from .paths import COPILOT_HOME

KINDS_ALLOWED = {
    "_fold_adapters.repeats_skill": "a runtime adapter is a thin pointer to its skill, so only its lines fold into "
    "its skill's",
}


@dataclass(frozen=True)
class ReportLine:
    name: str
    action: str
    detail: str = ""
    extra: tuple[str, ...] = ()
    kind: str = ""

    @property
    def text(self) -> str:
        detail = _detail(self.kind, self.detail)
        return f"  {self.name}{f' ({detail})' if detail else ''}"


# Lines naming one item are ordered by kind, in the table's order.
KIND_ORDER = {kind.label: index for index, kind in enumerate(KINDS)}


def _detail(*parts: str) -> str:
    return ", ".join(part for part in parts if part)


SKIPPED_WITH_SKILL = "its skill was skipped"
KEPT_WITH_SKILL = "its skill stays"

DRY_RUN = "DRY RUN"
DEPLOYED = "DEPLOYED"
# What the plan does to one item, which plan.PlanEntry holds.
Action = Literal[
    "SKIP", "PRESERVE", "REPLACE", "REMOVE", "RENAME", "UPDATE", "INSTALL", "ADOPT", "DROP", "KEEP", "UNCHANGED"
]
# Each planned action, in report order with attention first, and its label in the dry run and the deployment report.
ACTION_LABELS: dict[Action, tuple[str, str]] = {
    "SKIP": ("CONFLICT", "SKIPPED"),
    "PRESERVE": ("PRESERVE", "PRESERVED"),
    "REPLACE": ("REPLACE", "REPLACED"),
    "REMOVE": ("REMOVE", "REMOVED"),
    "RENAME": ("RENAME", "RENAMED"),
    "UPDATE": ("UPDATE", "UPDATED"),
    "INSTALL": ("FRESH INSTALL", "INSTALLED"),
    "ADOPT": ("ADOPT", "ADOPTED"),
    "DROP": ("DROP OWNERSHIP", "DROPPED OWNERSHIP"),
    "KEEP": ("KEEP", "KEPT"),
    "UNCHANGED": ("UNCHANGED", "UNCHANGED"),
}
DRY_RUN_ACTIONS = tuple(dry for dry, _ in ACTION_LABELS.values())
DEPLOY_ACTIONS = tuple(applied for _, applied in ACTION_LABELS.values())
# Reasons that --force or --force-item resolve; a destination of the wrong type needs manual cleanup instead.
FORCEABLE = {label: frozenset({MODIFIED, DIFFERS, DIFFERS_FROM_SKILL}) for label in ACTION_LABELS["SKIP"]}
FORCE_HINTS = {
    DRY_RUN: "Replace conflicting items with --force-item NAME or --force (backups are kept).",
    DEPLOYED: "Replace skipped items with --force-item NAME or --force (backups are kept).",
}

ATTENTION_ACTIONS = frozenset(
    label for action, labels in ACTION_LABELS.items() if action in ("SKIP", "PRESERVE", "REPLACE") for label in labels
)


def _fold_adapters(lines: list[ReportLine]) -> list[ReportLine]:
    """Drop runtime adapter lines that only repeat their skill's line.

    An adapter is a thin pointer to its skill, so it keeps its own line only when it needs attention for a
    reason of its own, or when it does something other than its skill and other than staying unchanged.
    """
    skill_actions = {line.name: line.action for line in lines if not line.kind}

    def repeats_skill(line: ReportLine) -> bool:
        if line.kind != ADAPTER or line.name not in skill_actions:
            return False
        if line.action == "UNCHANGED" or line.detail in (SKIPPED_WITH_SKILL, KEPT_WITH_SKILL):
            return True
        return line.action == skill_actions[line.name] and line.action not in ATTENTION_ACTIONS

    return [line for line in lines if not repeats_skill(line)]


REPORT_WIDTH = 80


def wrap(text: str) -> str:
    """Wrap a long report line between words, never inside a hyphenated name, with an indented continuation."""
    return textwrap.fill(
        text, width=REPORT_WIDTH, subsequent_indent="    ", break_long_words=False, break_on_hyphens=False
    )


def paragraph(text: str) -> str:
    """Wrap a warning or a note between words, never inside a hyphenated name, with no indent."""
    return textwrap.fill(text, width=REPORT_WIDTH, break_long_words=False, break_on_hyphens=False)


def copilot_home_note(configured: Path, adapters: Path, default: Path) -> str:
    """Why Copilot CLI does not read the adapters while COPILOT_HOME names a directory other than its default, and
    what the user can do."""
    return (
        f"{COPILOT_HOME} is set to {platform_support.normalize(configured)}, so GitHub Copilot CLI reads personal "
        f"skills from {platform_support.normalize(configured / 'skills')} and not from "
        f"{platform_support.normalize(adapters)}, where the runtime adapters are. To use these skills in Copilot, "
        f"unset {COPILOT_HOME} or set it to {platform_support.normalize(default)}; see \"Copilot's configuration "
        'directory" in docs/copilot-support.md.'
    )


def print_report(title: str, order: tuple[str, ...], lines: list[ReportLine]) -> None:
    """Print report lines grouped by action, attention first and each group alphabetized, then any force hint."""
    lines = _fold_adapters(lines)
    forceable = any(line.detail in FORCEABLE.get(line.action, ()) for line in lines)
    print("")
    print(f"=== {title} ===")
    actions = [*order, *sorted({line.action for line in lines} - set(order))]
    for action in actions:
        group = sorted(
            (line for line in lines if line.action == action),
            key=lambda line: (line.name, KIND_ORDER.get(line.kind, len(KIND_ORDER)), line.kind, line.detail),
        )
        if not group:
            continue
        print("")
        print(f"{action} ({len(group)}):")
        for line in group:
            print(wrap(line.text))
            for extra in line.extra:
                print(extra)
    print("")
    if forceable and title in FORCE_HINTS:
        print(FORCE_HINTS[title])
        print("")


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def warn_ignored_home(home: Path) -> None:
    """Warn on stderr when HOME names another directory than the profile folder this run uses."""
    ignored = platform_support.ignored_home_variable(home)
    if ignored is None:
        return
    # Flush first, so the warning cannot appear out of order when both streams go to one terminal or file.
    sys.stdout.flush()
    print("", file=sys.stderr)
    print(
        paragraph(
            f"WARNING: HOME is set to {ignored}, but Claude Code, Codex, and Copilot CLI read skills from "
            f"{platform_support.RUNTIME_HOME}, so this run uses {platform_support.normalize(home)}. To deploy into a "
            "throwaway home instead, use --canary-home."
        ),
        file=sys.stderr,
    )
    sys.stderr.flush()


def members_recorded(owned: manifest.Ownership) -> bool:
    """Whether the manifest records the members of every bundle request, as each deployment and migration now does.

    An entry an earlier release wrote records none, whether that release deployed it or migrated into it.
    """
    return owned.bundle_members_recorded and owned.requested_bundles <= owned.requested_bundle_members.keys()


def requested_members(owned: manifest.Ownership, src: source.Source) -> set[str]:
    """The skills the manifest's requested roots stand for: each skill requested on its own and each member of a
    requested bundle.

    A bundle request stands for the members the deployment that recorded it expanded it to, so it survives the source
    renaming the bundle or moving a member out of it. An entry that records the members of every bundle it requests is
    read as recorded. One an earlier release wrote records none, and that release's --migrate-from recorded no bundle
    request at all, relying on the installed members, so such an entry's requests also stand for each installed skill
    that no other installed skill depends on by a dependency the entry records and this source still declares
    (`Ownership.installed_roots`). It is read from the installed skills rather than from the bundle this source gives
    the request's name, which may hold other skills since the source regrouped them. A name the source has since
    renamed, which its source.json records, stands for the skill's name now.
    """
    members = set(owned.requested_skills)
    for bundle in owned.requested_bundles:
        members |= owned.requested_bundle_members.get(bundle, set())
    if not members_recorded(owned):
        members |= owned.installed_roots({name: skill.skill_deps for name, skill in src.skills.items()})
    return {src.renamed.get(name, name) for name in members}


def currently_chosen(src: source.Source, owned: manifest.Ownership, name: str, kind: str) -> bool:
    """Whether a menu item is currently chosen from this source, which `--all` keeps and the menu marks.

    An item is chosen when the manifest's requested roots stand for it, never because it, or a bundle's member, is
    installed as another root's dependency: an opt-in item the user never chose stays out of `--all`. A skill is chosen
    when it was requested on its own or through a bundle that held it, whatever bundle, if any, holds it now. A bundle
    is chosen when it was requested itself or any of its members was requested: a bundle is atomic, so a skill that was
    deployed on its own, or through a bundle the source renamed, and then joined this bundle keeps being deployed by
    `--all`, which completes the bundle, instead of being removed as no longer selected.
    """
    requested = requested_members(owned, src)
    if kind == "bundle":
        return name in owned.requested_bundles or any(member in requested for member in src.bundles[name])
    return name in requested
