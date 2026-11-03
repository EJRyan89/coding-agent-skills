"""The one reader of a module-level assignment to a single name, for every reader that takes a table from a module's
syntax tree without running it, such as validation's PENDING table and audit-repository's runtime catalogue.

A reader that must know the value a module runs with takes the one statement `sole_assignment` returns for the name
and refuses every other place the module may change it; this module only says which statements assign one name.
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
