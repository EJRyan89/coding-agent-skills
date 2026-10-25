"""The one reader of untrusted XML into an element tree, such as a repository's MSBuild project or `.slnx` solution.

A document type declaration can declare entities that expand without bound or name an external resource, and no file
the skills read needs one, so a document carrying `<!DOCTYPE` or `<!ENTITY` anywhere, in a comment or a CDATA section
included, is refused before it is parsed: the reader never tokenizes untrusted text to decide what a declaration is.
The caller has already decoded the text, so a byte-order mark and a leading XML declaration are dropped, since an
encoding the declaration names would only contradict the decoding. The reader bounds nothing else: a caller that limits
how much it reads limits the text it passes in.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree

XML_DECLARATION = re.compile(r"\A\s*<\?xml[^>]*\?>")


class XmlRefused(ValueError):
    """A document the reader would not parse; the message names the declaration it carried or the parser's reason."""


# What `read_xml` raises on bad content.
CONTENT_ERRORS: tuple[type[Exception], ...] = (XmlRefused,)


def read_xml(text: str) -> ElementTree.Element:
    """The root element of `text`, or `XmlRefused` when it declares a DOCTYPE or an entity or is not well-formed."""
    if "<!DOCTYPE" in text:
        raise XmlRefused("the document declares a DOCTYPE, which could expand entities without bound")
    if "<!ENTITY" in text:
        raise XmlRefused("the document declares an ENTITY, which could expand without bound")
    try:
        return ElementTree.fromstring(  # noqa: S314 - DOCTYPE and ENTITY are refused above, so nothing expands
            XML_DECLARATION.sub("", text.removeprefix("\ufeff"), count=1)
        )
    except (ElementTree.ParseError, ValueError) as error:
        raise XmlRefused(f"the document is not well-formed XML: {error}") from error
