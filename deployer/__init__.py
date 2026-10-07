"""Guarded, journaled deployer for the skills in this repository.

skills/skill-core/scripts is the one home of the repository's shared modules, such as the frontmatter reader, and the
deployer imports them from there as skill scripts do. The deployer always runs from a checkout, so the directory is
located from this file, never through the configured source path, and Python runs this file before any deployer
module, so every module's import of a shared module follows this statement.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "skill-core" / "scripts"))
