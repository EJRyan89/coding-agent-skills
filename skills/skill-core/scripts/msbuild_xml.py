"""The one reading of an MSBuild file's elements, shared by the skills that read a repository's project files.

An element is named without its namespace (`local_name`), since an older project file carries the MSBuild 2003
namespace, and a project is SDK-style as MSBuild reads one (`is_sdk_style`): its `Project` element names an `Sdk`, or
holds an `Sdk` element, both of which MSBuild imports the SDK for by itself (`implicit_sdk`), or an `Import` of an
`Sdk`, which imports it explicitly. An `Sdk` attribute left empty names none, and element and attribute names are
compared with case. The document is parsed by skill-core's `xml_reader` and its text decoded by
`dotnet_solution.read_text`; this module reads only the element tree it is given.
"""

from __future__ import annotations

from typing import Any
from xml.etree.ElementTree import Element


def local_name(element: Element[Any]) -> str:
    """An element's tag without its namespace, or an empty name for a node without a tag, such as a comment."""
    tag = element.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def implicit_sdk(root: Element) -> bool:
    """Whether MSBuild imports an SDK into the project by itself: its `Project` element names an `Sdk`, or holds an
    `Sdk` element."""
    if local_name(root) != "Project":
        return False
    return bool(root.get("Sdk")) or any(local_name(child) == "Sdk" for child in root)


def is_sdk_style(root: Element) -> bool:
    """Whether a project is SDK-style: MSBuild imports an SDK by itself (`implicit_sdk`), or the `Project` element
    holds an `Import` of an `Sdk`."""
    if implicit_sdk(root):
        return True
    return local_name(root) == "Project" and any(
        local_name(child) == "Import" and bool(child.get("Sdk")) for child in root
    )
