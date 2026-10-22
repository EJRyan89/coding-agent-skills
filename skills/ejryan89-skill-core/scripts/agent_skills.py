"""The Agent Skills frontmatter rules and Codex's invocation-policy file, held once for every part that applies them.

The deployer refuses a skill it renders that breaks a rule, and audit-ai-config reports one in a repository it
audits. Anthropic's skills overview and best-practices pages give the field requirements: a skill's name is at most 64
characters and holds neither reserved word, its description is at most 1,024 characters, and neither holds an XML tag.
Codex's skills page reads a skill's invocation policy from `agents/openai.yaml` beside its `SKILL.md`, never from its
frontmatter.
"""

from __future__ import annotations

import re

RESERVED_WORDS = ("anthropic", "claude")
XML_TAG_PATTERN = re.compile(r"</?[A-Za-z][^<>]*>")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
CODEX_METADATA = "agents/openai.yaml"
