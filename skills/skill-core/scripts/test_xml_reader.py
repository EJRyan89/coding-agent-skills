"""Regression tests for the one guarded reader of untrusted XML the skills share."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xml_reader import XmlRefused, read_xml

DOCTYPE_REFUSED = "the document declares a DOCTYPE, which could expand entities without bound"
ENTITY_REFUSED = "the document declares an ENTITY, which could expand without bound"
NOT_WELL_FORMED = "the document is not well-formed XML: "


class ReaderTestCase(unittest.TestCase):
    def refusal(self, text: str) -> str:
        with self.assertRaises(XmlRefused) as raised:
            read_xml(text)
        return str(raised.exception)

    def assert_refused(self, text: str, reason: str) -> None:
        self.assertEqual(reason, self.refusal(text))


class RefusalTests(ReaderTestCase):
    def test_a_doctype_is_refused(self) -> None:
        self.assert_refused('<?xml version="1.0"?>\n<!DOCTYPE Project>\n<Project />', DOCTYPE_REFUSED)

    def test_an_internal_entity_is_refused_before_it_expands(self) -> None:
        laughs = (
            '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
            "<lolz>&lol2;</lolz>"
        )
        self.assert_refused(laughs, DOCTYPE_REFUSED)

    def test_an_external_entity_is_refused_before_anything_is_fetched(self) -> None:
        self.assert_refused(
            '<!DOCTYPE Solution [<!ENTITY ext SYSTEM "file:///C:/Windows/win.ini">]><Solution>&ext;</Solution>',
            DOCTYPE_REFUSED,
        )
        self.assert_refused(
            '<!DOCTYPE Solution SYSTEM "https://example.invalid/solution.dtd"><Solution />', DOCTYPE_REFUSED
        )

    def test_an_entity_declaration_without_a_doctype_is_refused(self) -> None:
        self.assert_refused('<Project><!ENTITY % p SYSTEM "p.dtd"></Project>', ENTITY_REFUSED)

    def test_refusal_comes_before_the_parse(self) -> None:
        # Neither document is well-formed, so only a refusal made before parsing names the declaration.
        self.assert_refused("<!DOCTYPE", DOCTYPE_REFUSED)
        self.assert_refused("<!ENTITY", ENTITY_REFUSED)

    def test_declaration_text_in_a_comment_or_cdata_still_refuses(self) -> None:
        # The reader never tokenizes the document to decide what a declaration is: any declaration text refuses it,
        # so no comment or CDATA section can carry one past the guard.
        self.assert_refused("<Project><!-- see <!DOCTYPE in the docs --></Project>", DOCTYPE_REFUSED)
        self.assert_refused("<Project><![CDATA[<!ENTITY x 'y'>]]></Project>", ENTITY_REFUSED)

    def test_a_lowercase_doctype_is_not_xml_and_is_refused_by_the_parser(self) -> None:
        # XML names are case-sensitive, so the refusal matches the declaration as XML spells it; the parser refuses
        # any other spelling before it could declare anything.
        self.assertTrue(self.refusal("<!doctype Project><Project />").startswith(NOT_WELL_FORMED))

    def test_an_undeclared_entity_reference_never_resolves(self) -> None:
        # Expat's wording of this reason differs between versions, so only its start is pinned.
        self.assertTrue(self.refusal("<Solution>&ext;</Solution>").startswith(NOT_WELL_FORMED + "undefined entity"))

    def test_a_document_that_is_not_well_formed_is_refused_with_the_parsers_reason(self) -> None:
        self.assert_refused("", "the document is not well-formed XML: no element found: line 1, column 0")
        self.assert_refused(
            '<Solution><Project Path="Lib/Lib.csproj">\n',
            "the document is not well-formed XML: no element found: line 2, column 0",
        )

    def test_the_error_is_a_value_error(self) -> None:
        self.assertTrue(issubclass(XmlRefused, ValueError))


class ParseTests(ReaderTestCase):
    def test_a_clean_document_parses(self) -> None:
        root = read_xml(
            '<Solution>\n  <Project Path="Lib/Lib.csproj" />\n  <Project Path="App/App.csproj" />\n</Solution>'
        )
        self.assertEqual("Solution", root.tag)
        self.assertEqual(
            ["Lib/Lib.csproj", "App/App.csproj"], [project.get("Path") for project in root.iter("Project")]
        )

    def test_a_declaration_naming_another_encoding_never_overrides_the_decoded_text(self) -> None:
        for encoding in ("windows-1252", "utf-16", "iso-8859-1"):
            with self.subTest(encoding=encoding):
                root = read_xml(f'  <?xml version="1.0" encoding="{encoding}"?>\n<NoWarn>CS1591;\u00e9</NoWarn>')
                self.assertEqual("CS1591;\u00e9", root.text)

    def test_a_byte_order_mark_is_dropped_before_the_declaration(self) -> None:
        root = read_xml('\ufeff<?xml version="1.0" encoding="windows-1252"?><NoWarn>\u00e9</NoWarn>')
        self.assertEqual("\u00e9", root.text)
        self.assertEqual("Project", read_xml("\ufeff<Project />").tag)

    def test_only_the_leading_declaration_is_stripped(self) -> None:
        self.assertTrue(self.refusal('<a/><?xml version="1.0"?>').startswith(NOT_WELL_FORMED))


if __name__ == "__main__":
    unittest.main()
