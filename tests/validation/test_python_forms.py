"""The catalogue tests of the shared readers in tests/validation/validation_support.py: every expression Python's ast
module can write as a call's function, every field of an import statement, and every field of a call's arguments, read
from the runner's ast and a pinned copy of the newest Python's, is read by the readers, refused by a check validation
runs, or named in validation_support.NOT_READ with why. A node or a field a new Python adds fails here until it is
decided.
"""

from __future__ import annotations

import ast
import itertools
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import python_checks
import validation_support
from validation_support import (
    ARGUMENT_FIELDS,
    CALLEE_FORMS,
    IMPORT_FIELDS,
    LITERAL_DISPLAYS,
    NOT_READ,
    REFUSED_CALLEES,
    REPOSITORY_ROOT,
    CallableResolver,
    call_arguments,
    chosen_callee,
    import_aliases,
    resolved_calls,
    write_fixture_tree,
)

# The expression nodes the newest Python's ast module defines, its `ast.expr` subclasses, pinned so a runner whose
# Python is older still holds the decisions to every one, and the release they were read from, from which on every
# pinned node must still be one the runner's ast defines.
PINNED_EXPRESSIONS = frozenset(
    {"Attribute", "Await", "BinOp", "BoolOp", "Call", "Compare", "Constant", "Dict", "DictComp", "FormattedValue"}
    | {"GeneratorExp", "IfExp", "Interpolation", "JoinedStr", "Lambda", "List", "ListComp", "Name", "NamedExpr", "Set"}
    | {"SetComp", "Slice", "Starred", "Subscript", "TemplateStr", "Tuple", "UnaryOp", "Yield", "YieldFrom"}
)
# The fields of an import statement and of a call's arguments in the newest Python's ast module.
PINNED_IMPORT_FIELDS = frozenset(
    {"Import.names", "Import.is_lazy", "ImportFrom.module", "ImportFrom.names", "ImportFrom.level"}
    | {"ImportFrom.is_lazy", "alias.name", "alias.asname"}
)
PINNED_ARGUMENT_FIELDS = frozenset({"Call.func", "Call.args", "Call.keywords", "keyword.arg", "keyword.value"})
PINNED_FROM_PYTHON = (3, 15)
CALLEE = "callee "
IMPORT_FIELD = "import field "
ARGUMENT_FIELD = "argument field "


def runner_expressions() -> frozenset[str]:
    """The expression nodes the runner's ast module defines. A node the parser no longer writes, as Python 3.12's Num,
    subclasses Constant, not ast.expr itself, so it is not one."""
    return frozenset(node.__name__ for node in ast.expr.__subclasses__())


def runner_fields(*nodes: type[ast.AST]) -> frozenset[str]:
    """Each field of the nodes in the runner's ast module, as `<node>.<field>`."""
    return frozenset(f"{node.__name__}.{field}" for node in nodes for field in node._fields)


def not_read(prefix: str) -> frozenset[str]:
    """The entries of validation_support.NOT_READ of one catalogue, without its prefix."""
    return frozenset(key.removeprefix(prefix) for key in NOT_READ if key.startswith(prefix))


def test_every_not_read_entry_names_a_catalogue_and_a_reason() -> None:
    assert sorted(key for key in NOT_READ if not key.startswith((CALLEE, IMPORT_FIELD, ARGUMENT_FIELD))) == []
    assert sorted(key for key, reason in NOT_READ.items() if not reason.strip()) == []


@pytest.mark.parametrize(
    ("name", "runner", "pinned", "read", "prefix"),
    [
        ("expression", runner_expressions(), PINNED_EXPRESSIONS, CALLEE_FORMS | set(REFUSED_CALLEES), CALLEE),
        (
            "import field",
            runner_fields(ast.Import, ast.ImportFrom, ast.alias),
            PINNED_IMPORT_FIELDS,
            IMPORT_FIELDS,
            IMPORT_FIELD,
        ),
        (
            "argument field",
            runner_fields(ast.Call, ast.keyword),
            PINNED_ARGUMENT_FIELDS,
            ARGUMENT_FIELDS,
            ARGUMENT_FIELD,
        ),
    ],
)
def test_every_form_the_runners_ast_or_the_newest_python_defines_is_decided_once(
    name: str, runner: frozenset[str], pinned: frozenset[str], read: frozenset[str], prefix: str
) -> None:
    unread = not_read(prefix)
    catalogue = runner | pinned
    assert sorted(catalogue - read - unread) == [], (
        f"decide each {name}: read it in validation_support's {name} table, refuse it there, or name it in NOT_READ "
        "with the reason"
    )
    assert sorted(runner - pinned) == [], f"pin each {name} this Python defines"
    if sys.version_info >= PINNED_FROM_PYTHON:
        assert sorted(pinned - runner) == [], f"Python {sys.version_info[:2]} no longer defines these: unpin each"
    assert sorted((read | unread) - catalogue) == [], "remove each: no Python defines it"
    assert sorted(read & unread) == [], "each is both decided and in NOT_READ"


def test_no_callee_form_is_both_read_and_refused() -> None:
    assert sorted(CALLEE_FORMS & set(REFUSED_CALLEES)) == []


# Each callee form CALLEE_FORMS reads, with calls of os.system through it, so a policy that refuses os.system reads
# each as a call of it.
CALLEE_SAMPLES = {
    "Name": ["import os\nrun = os.system\nrun('x')", "from os import system\nsystem('x')"],
    "Attribute": ["import os\nos.system('x')", "import os as o\no.system('x')"],
    "Call": [
        "import functools\nimport os\nfunctools.partial(os.system)('x')",
        "import os\ngetattr(os, 'system')('x')",
        "import operator\nimport os\noperator.attrgetter('system')(os)('x')",
    ],
    "Subscript": ["import os\nvars(os)['system']('x')", "import os\nos.__dict__['system']('x')"],
}


def the_call(tree: ast.Module) -> ast.Call:
    """The call given the one argument 'x'."""
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and [ast.unparse(argument) for argument in node.args] == ["'x'"]
    )


def test_every_callee_form_read_has_a_sample() -> None:
    assert sorted(CALLEE_SAMPLES) == sorted(CALLEE_FORMS)


@pytest.mark.parametrize(
    ("form", "source"), [(form, source) for form, sources in CALLEE_SAMPLES.items() for source in sources]
)
def test_each_callee_form_read_names_the_function_the_call_runs(form: str, source: str) -> None:
    tree = ast.parse(source)
    call = the_call(tree)
    assert type(call.func).__name__ == form
    assert "os.system" in [found.name for found in resolved_calls(call, CallableResolver(tree))]


def test_a_callee_whose_value_a_function_returns_names_nothing() -> None:
    # What a function returns at run time is not read.
    tree = ast.parse("import os\n(lambda: os.system)()('x')")
    assert [found.name for found in resolved_calls(the_call(tree), CallableResolver(tree))] == [""]


# Each callee form that chooses a call's function by a value, with calls of os.system through it, each refused at the
# source by the indirection policy: every REFUSED_CALLEES node, a subscript of every LITERAL_DISPLAYS literal, and
# combinations of them.
REFUSED_SAMPLES = {
    "BoolOp": [
        "(os.system or print)",
        "(print and os.system)",
        "(print or (os.system and print))",
        "((run := os.system) or print)",
    ],
    "IfExp": [
        "(print if quiet else os.system)",
        "((os.system or print) if quiet else print)",
        "(print if quiet else (run := os.system))",
    ],
    "NamedExpr": ["(run := os.system)", "(run := (print or os.system))"],
    "Tuple": ["(print, os.system)[1]", "(print, (os.system if quiet else print))[1]"],
    "List": ["[os.system][0]", "[*others, os.system][0]", "[(run := os.system)][0]"],
    "Set": ["{os.system}[0]"],
    "Dict": ["{'run': os.system}['run']", "{**others, 'run': os.system}['run']"],
}


def test_every_refused_callee_form_has_a_sample() -> None:
    displays = {display.__name__ for display in LITERAL_DISPLAYS}
    assert sorted(REFUSED_SAMPLES) == sorted({*REFUSED_CALLEES, *displays})


@pytest.mark.parametrize(
    ("form", "callee"), [(form, callee) for form, callees in REFUSED_SAMPLES.items() for callee in callees]
)
def test_the_indirection_policy_refuses_each_callee_form_that_chooses_by_a_value(form: str, callee: str) -> None:
    function = ast.parse(callee, mode="eval").body
    held = function.value if isinstance(function, ast.Subscript) else function
    assert type(held).__name__ == form
    source = f"import os\n\n\ndef run(quiet, others):\n    {callee}('x')\n"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {"skills/alpha/scripts/alpha.py": source})
        found = python_checks.indirection_problems(root, sorted(root.rglob("*.py")))
    assert [problem.split(" ", 1)[0] for problem in found] == ["skills/alpha/scripts/alpha.py:5"]
    assert chosen_callee(function) in found[0]


def indirection_rows(body: str, *, cut: bool = True) -> list[str]:
    """The indirection policy's problems for a skill's module that imports os and defines run(quiet, others, paths)
    holding body, each cut to its place, or to its place and its refusal without the policy's remedy."""
    source = f"import os\n\n\ndef run(quiet, others, paths):\n    {body}\n"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_fixture_tree(root, {"skills/alpha/scripts/alpha.py": source})
        found = python_checks.indirection_problems(root, sorted(root.rglob("*.py")))
    return [problem.split(" ", 1)[0] if cut else problem.partition(",")[0] for problem in found]


@pytest.mark.parametrize(
    "body", ["(fs := os).system('x')", "(fs := os).path.join('x')", "(text := str(quiet)).strip().upper()"]
)
def test_the_indirection_policy_refuses_an_attribute_called_on_an_assignment_expressions_value(body: str) -> None:
    # A module held as a value is refused too, by its own rule.
    assert "skills/alpha/scripts/alpha.py:5 calls an attribute of the value an assignment expression binds" in (
        indirection_rows(body, cut=False)
    )


@pytest.mark.parametrize(
    "body",
    [
        # Stated limits: a method of a value an `or` or an if-expression chooses is read as a value's method; a name an
        # `or` or an if-expression binds is called as the name; an assignment expression used as a value elsewhere than
        # a call's function or an attribute's receiver is not refused; and a subscript of a subscript, a
        # comprehension, a `:=`, or an `or` is read as a dispatch table.
        "(quiet or '').strip()",
        "(quiet if others else '').strip()",
        "call = quiet or os.system\n    call('x')",
        "call = os.system if quiet else print\n    call('x')",
        "list(map((rm := os.remove), paths))",
        "call = (chosen := os.system) or print",
        "[[os.system]][0][0]('x')",
        "[os.system for _ in paths][0]('x')",
        "(table := [os.system])[0]('x')",
        "(others or [os.system])[0]('x')",
        # A dispatch table and a generic class name nothing and are not refused.
        "{'run': print}.get(quiet, print)('x')",
        "frozenset[int]()",
    ],
)
def test_the_indirection_policy_leaves_a_value_chosen_otherwise_alone(body: str) -> None:
    assert indirection_rows(body) == []


def import_rows() -> list[tuple[str, str, str]]:
    """Every import statement the read fields can spell, generated from them: an import of a plain or a dotted module,
    and a from-import from no module, a plain, or a dotted one, at each level, each with and without `as`, with the
    call through the name it binds and the dotted name that call runs."""
    rows: list[tuple[str, str, str]] = []
    for imported, alias in itertools.product(("os", "os.path"), (None, "bound")):
        statement = f"import {imported}" + (f" as {alias}" if alias else "")
        rows.append((statement, f"{alias or imported}.run", f"{imported}.run"))
    for source, level, alias in itertools.product(("", "os", "os.path"), (0, 1, 2), (None, "bound")):
        if not source and level == 0:
            continue
        statement = f"from {'.' * level}{source} import run" + (f" as {alias}" if alias else "")
        rows.append((statement, alias or "run", f"{source}.run" if level == 0 else "<local>.run"))
    return rows


@pytest.mark.parametrize(("statement", "callee", "expected"), import_rows())
def test_every_import_form_binds_the_name_its_call_resolves_through(statement: str, callee: str, expected: str) -> None:
    tree = ast.parse(f"{statement}\n{callee}('x')")
    assert [found.name for found in resolved_calls(the_call(tree), CallableResolver(tree))] == [expected]
    if not expected.startswith("<local>."):
        assert qualified_name_through_imports(tree, callee) == expected


def qualified_name_through_imports(tree: ast.Module, callee: str) -> str:
    """The dotted name a callee stands for through module_imports alone, as import_aliases reads it."""
    return validation_support.qualified_name(ast.parse(callee, mode="eval").body, import_aliases(tree))


def test_a_star_import_is_refused_by_ruff() -> None:
    # A star import binds names no reader sees; ruff's F family, F403 among it, refuses every one in every module.
    lint = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["ruff"]["lint"]
    ignored = [*lint.get("ignore", []), *itertools.chain.from_iterable(lint.get("per-file-ignores", {}).values())]
    assert "F" in lint["select"]
    assert sorted(code for code in ignored if "F403".startswith(code)) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("run('x')", ["Constant"]),
        ("run(*parts)", ["Starred"]),
        ("run(command='x')", ["Constant"]),
        ("run(**options)", ["Starred"]),
        ("run(*parts, **options)", ["Starred", "Starred"]),
    ],
)
def test_every_argument_form_may_give_a_parameter(source: str, expected: list[str]) -> None:
    call = ast.parse(source, mode="eval").body
    assert isinstance(call, ast.Call)
    assert [type(found).__name__ for found in call_arguments(call, 0, ("command",))] == expected


def test_a_double_star_of_a_literal_gives_its_keywords() -> None:
    tree = ast.parse("import os\nos.system(**{'command': 'x'}, check=True)")
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call))
    found = resolved_calls(call, CallableResolver(tree))
    assert [(item.name, sorted(item.keywords), item.unread) for item in found] == [
        ("os.system", ["check", "command"], [])
    ]
