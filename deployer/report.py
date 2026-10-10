"""The reports a deployment prints: report lines grouped by action, their labels, and shared warnings."""

from __future__ import annotations

import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

from . import manifest, platform_support, source
from .kinds import ADAPTER, DIFFERS, DIFFERS_FROM_SKILL, KINDS, MODIFIED

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

DRY_RUN = "DRY RUN"
DEPLOYED = "DEPLOYED"
# Each planned action, in report order with attention first, and its label in the dry run and the deployment report.
ACTION_LABELS = {
    "SKIP": ("CONFLICT", "SKIPPED"),
    "PRESERVE": ("PRESERVE", "PRESERVED"),
    "REPLACE": ("REPLACE", "REPLACED"),
    "REMOVE": ("REMOVE", "REMOVED"),
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

ATTENTION_ACTIONS = frozenset(label for action in ("SKIP", "PRESERVE", "REPLACE") for label in ACTION_LABELS[action])


def _fold_adapters(lines: list[ReportLine]) -> list[ReportLine]:
    """Drop runtime adapter lines that only repeat their skill's line.

    An adapter is a thin pointer to its skill, so it keeps its own line only when it needs attention for a
    reason of its own, or when it does something other than its skill and other than staying unchanged.
    """
    skill_actions = {line.name: line.action for line in lines if not line.kind}

    def repeats_skill(line: ReportLine) -> bool:
        if line.kind != ADAPTER or line.name not in skill_actions:
            return False
        if line.action == "UNCHANGED" or line.detail == SKIPPED_WITH_SKILL:
            return True
        return line.action == skill_actions[line.name] and line.action not in ATTENTION_ACTIONS

    return [line for line in lines if not repeats_skill(line)]


REPORT_WIDTH = 80


def wrap(text: str) -> str:
    """Wrap a long report line between words, never inside a hyphenated name, with an indented continuation."""
    return textwrap.fill(
        text, width=REPORT_WIDTH, subsequent_indent="    ", break_long_words=False, break_on_hyphens=False
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
        textwrap.fill(
            f"WARNING: HOME is set to {ignored}, but Claude Code, Codex, and Copilot CLI read skills from "
            f"{platform_support.RUNTIME_HOME}, so this run uses {platform_support.normalize(home)}. To deploy into a "
            "throwaway home instead, use --canary-home.",
            width=REPORT_WIDTH,
            break_long_words=False,
            break_on_hyphens=False,
        ),
        file=sys.stderr,
    )
    sys.stderr.flush()


def currently_chosen(src: source.Source, owned: manifest.Ownership, name: str, kind: str) -> bool:
    """Whether a menu item is currently deployed from this source."""
    if kind == "bundle":
        return name in owned.requested_bundles or all(member in owned.skills for member in src.bundles[name])
    return name in owned.requested_skills or name in owned.skills
