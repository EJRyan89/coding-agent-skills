"""What the user selects to deploy: the menu, --all, and the checks a selection must pass before rendering."""

from __future__ import annotations

import sys

from . import config, source, tools
from .arguments import CHECK_COMMAND_LINE, CONFIGURE_COMMAND_LINE
from .context import Context, Selection
from .errors import CANCELLED, Cancelled, DeployError
from .report import currently_chosen, plural, wrap


def select_all(context: Context, include: list[str]) -> Selection:
    """Every menu item except opt-in ones, which stay only when already installed or named with --include."""
    src = context.source
    selection = Selection()
    for names, kind, chosen in (
        (sorted(src.bundles), "bundle", selection.bundles),
        (source.root_names(src), "skill", selection.skills),
    ):
        for name in names:
            if not source.is_opt_in(src, name) or name in include or currently_chosen(src, context.owned, name, kind):
                chosen.append(name)
    return selection


def select(context: Context) -> Selection | None:
    src = context.source
    selection = Selection()
    if context.options.select_all:
        roots = set(src.bundles) | set(source.root_names(src))
        unknown = [name for name in context.options.include if name not in roots]
        if unknown:
            raise DeployError(f"ERROR: --include names no bundle or skill in this source: {', '.join(unknown)}")
        return select_all(context, context.options.include)
    print("")
    if not src.skills:
        print("No current skills discovered; previously owned skills will be considered for removal.")
        selection.deselect_all = True
        return selection
    print("Select what to deploy:")
    choices = [(name, "bundle") for name in sorted(src.bundles)] + [(name, "skill") for name in source.root_names(src)]
    if not choices:
        print("No selectable bundles or skills discovered; previously owned skills will be considered for removal.")
        selection.deselect_all = True
        return selection
    for index, (name, kind) in enumerate(choices, start=1):
        labels = [*(["bundle"] if kind == "bundle" else []), *(["opt-in"] if source.is_opt_in(src, name) else [])]
        suffix = f" ({', '.join(labels)})" if labels else ""
        mark = "*" if currently_chosen(src, context.owned, name, kind) else " "
        print(f"  [{mark}] {index}. {name}{suffix}")
    print("[*] = currently deployed")
    print("Enter numbers separated by spaces, 'all', or 'none'. Ctrl+C cancels.")
    print("Selection: ", end="", flush=True)
    try:
        line = context.stdin.readline()
    except KeyboardInterrupt:
        print("")
        raise Cancelled(CANCELLED) from None
    if not line:
        # The end of input cancels like Ctrl+C: nobody is there to answer.
        print("")
        raise Cancelled(CANCELLED)
    answer = line.rstrip("\r\n")
    if answer == "all":
        selection = select_all(context, [])
    elif answer == "none":
        selection.deselect_all = True
    else:
        for token in answer.split():
            if not token.isdigit():
                raise DeployError(f"ERROR: Invalid selection '{token}'")
            index = int(token) - 1
            if index < 0 or index >= len(choices):
                raise DeployError(f"ERROR: Selection '{token}' is out of range")
            name, kind = choices[index]
            (selection.bundles if kind == "bundle" else selection.skills).append(name)
    if not selection.bundles and not selection.skills and not selection.deselect_all:
        print("", file=sys.stderr)
        print("No skills selected.", file=sys.stderr)
        print("", file=sys.stderr)
        return None
    return selection


def require_variables(context: Context, selected: list[str]) -> None:
    missing: list[str] = []
    for name in selected:
        for variable in context.source.skills[name].required_vars:
            if variable in config.DERIVED_VARIABLES or context.config.get(variable):
                continue
            if variable not in missing:
                missing.append(variable)
    if missing:
        lines = [
            f"ERROR: Selected skills require variables not set in config: {' '.join(missing)}",
            "Skills requiring these:",
        ]
        for variable in missing:
            lines += [
                f"  {name} -> {variable}" for name in selected if variable in context.source.skills[name].required_vars
            ]
        raise DeployError(*lines, f"Run '{CONFIGURE_COMMAND_LINE}' to set them.")


def warn_missing_tools(src: source.Source, selection: Selection) -> None:
    """Warn, without stopping, when a selected skill runs a required tool that is not installed."""
    if selection.deselect_all:
        return
    required = source.required_tools(src, selection.bundles, selection.skills)
    missing = {
        name: roots
        for name, roots in source.tool_users(src, selection.bundles, selection.skills).items()
        if name in required and not tools.SKILL_TOOLS[name].optional and tools.SKILL_TOOLS[name].locate() is None
    }
    if not missing:
        return
    print("", file=sys.stderr)
    print("WARNING: Selected skills use tools that are not installed:", file=sys.stderr)
    for name, roots in missing.items():
        print(wrap(f"  {name} (used by {', '.join(roots)})"), file=sys.stderr)
    print("Those skills will fail until the tools are installed.", file=sys.stderr)
    print(f"Run '{CHECK_COMMAND_LINE}' for details.", file=sys.stderr)


def print_selection(src: source.Source, selection: Selection, selected: list[str]) -> None:
    """Show what was requested, with bundle members and pulled-in dependencies indented beneath it."""
    print("")
    if selection.deselect_all:
        print("Selected nothing; unmodified items this source deployed will be removed.")
        return

    def print_dependencies(roots: list[str]) -> None:
        for dependency in sorted(set(source.expand(src, [], roots)) - set(roots)):
            print(f"    {dependency} (dependency)")

    items = len(selection.bundles) + len(selection.skills)
    print(f"Selected {plural(items, 'item')} ({plural(len(selected), 'skill')}):")
    for bundle in sorted(selection.bundles):
        print(f"  {bundle} (bundle)")
        for member in sorted(src.bundles[bundle]):
            print(f"    {member}")
        print_dependencies(src.bundles[bundle])
    for skill in sorted(selection.skills):
        print(f"  {skill}")
        print_dependencies([skill])
