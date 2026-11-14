"""The Agent Skills frontmatter rules and Codex's invocation-policy file, held once for every part that applies them.

The deployer refuses a skill it renders that breaks a rule, and audit-ai-config reports one in a repository it
audits. Anthropic's skills overview and best-practices pages give the field requirements: a skill's name is at most 64
characters and holds neither reserved word, its description is at most 1,024 characters, and neither holds an XML tag.
Codex's skills page reads a skill's invocation policy from `agents/openai.yaml` beside its `SKILL.md`, never from its
frontmatter. A runtime opens a skill's main file by the name `SKILL.md`, so the file a folder gives for that name is
the skill's, whatever its spelling where the folder folds case: `is_skill_file` is the one recognizer of it.
"""

from __future__ import annotations

import re
from pathlib import Path

from path_keys import name_key

RESERVED_WORDS = ("anthropic", "claude")
XML_TAG_PATTERN = re.compile(r"</?[A-Za-z][^<>]*>")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
CODEX_METADATA = "agents/openai.yaml"
# A skill's main file, which a runtime opens by this name.
SKILL_FILE = "SKILL.md"


def is_skill_file(path: Path) -> bool:
    """Whether `path` is named as its folder's SKILL.md, as the folder's file system compares names: SKILL.md in any
    case where the folder folds case, and exactly SKILL.md where it does not, as the runtime that opens it finds it.
    Only a name that is SKILL.md in some case asks the folder."""
    return path.name.casefold() == SKILL_FILE.casefold() and name_key(path.name, path.parent) == name_key(
        SKILL_FILE, path.parent
    )
