"""The state one deployment run carries: its options, what the user selected, and what the manifest records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TextIO

from . import manifest, source
from .manifest import Ownership
from .paths import Paths


@dataclass
class Options:
    select_all: bool = False
    force: bool = False
    force_items: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    dry_run: bool = False
    migrate_from: str = ""
    take_over_source: bool = False
    canary_home: str = ""


@dataclass
class Selection:
    bundles: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    deselect_all: bool = False


@dataclass
class Context:
    paths: Paths
    options: Options
    source: source.Source
    config: dict[str, str]
    manifest: manifest.Manifest
    owned: Ownership  # not manifest.Ownership: in this class body, manifest names the field above
    stdin: TextIO
    selection: Selection = field(default_factory=Selection)

    @property
    def source_id(self) -> str:
        return self.source.source_id

    def forced(self, item: str) -> bool:
        return self.options.force or item in self.options.force_items
