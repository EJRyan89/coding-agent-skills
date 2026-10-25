"""The one reader of untrusted XML into an element tree, such as a repository's MSBuild project or `.slnx` solution.

A document type declaration can declare entities that expand without bound or name an external resource, and no file
the skills read needs one, so a document carrying `<!DOCTYPE` or `<!ENTITY` anywhere, in a comment or a CDATA section
included, is refused before it is parsed: the reader never tokenizes untrusted text to decide what a declaration is.
The caller has already decoded the text, so a byte-order mark and a leading XML declaration are dropped, since an
encoding the declaration names would only contradict the decoding. Elements nested past `nesting_depth`'s
`DEPTH_LIMIT` are refused after the parse, which nests far deeper than a Python walk over its tree can recurse. The
reader bounds nothing else: a caller that limits how much it reads limits the text it passes in.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree

import nesting_depth

XML_DECLARATION = re.compile(r"\A\s*<\?xml[^>]*\?>")


class XmlRefused(ValueError):
    """A document the reader would not parse; the message names the declaration it carried or the parser's reason."""


def read_xml(text: str) -> ElementTree.Element:
    """The root element of `text`, or `XmlRefused` when it declares a DOCTYPE or an entity, is not well-formed, or
    nests elements past `DEPTH_LIMIT`."""
    if "<!DOCTYPE" in text:
        raise XmlRefused("the document declares a DOCTYPE, which could expand entities without bound")
    if "<!ENTITY" in text:
        raise XmlRefused("the document declares an ENTITY, which could expand without bound")
    try:
        root = ElementTree.fromstring(  # noqa: S314 - DOCTYPE and ENTITY are refused above, so nothing expands
            XML_DECLARATION.sub("", text.removeprefix("\ufeff"), count=1)
        )
    except (ElementTree.ParseError, ValueError) as error:
        raise XmlRefused(f"the document is not well-formed XML: {error}") from error
    if nesting_depth.nests_past(root, list):
        raise XmlRefused(f"the document nests elements deeper than {nesting_depth.DEPTH_LIMIT} levels")
    return root
