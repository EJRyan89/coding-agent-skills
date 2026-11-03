"""Which module-level statements assign one name and nothing else, for the readers that take a table from a
module's syntax tree without running it and refuse every other place the module may change it: validation's PENDING
table (tests/validation/known_failures.py) and audit-repository's runtime catalogue. validation_support's
module_allowance, which reads the `*_ALLOWED` sanctions, keeps a reader of its own; this module only says which
statements assign one name.
"""

from __future__ import annotations

import ast


def sole_assignment(node: ast.stmt) -> tuple[ast.Name, ast.expr] | None:
    """The one name a module-level statement assigns, with its value, when it assigns one name and nothing else: a
    plain assignment with one target, or an annotated assignment with a value; None for any other statement, a chained
    or unpacking assignment among them."""
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0], node.value
    if isinstance(node, ast.AnnAssign) and node.value is not None and isinstance(node.target, ast.Name):
        return node.target, node.value
    return None
