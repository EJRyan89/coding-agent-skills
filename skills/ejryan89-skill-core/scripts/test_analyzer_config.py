"""Regression tests for the one reader of an analyzer config's lines as the compiler reads them."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyzer_config import ConfigLine, analyzer_config_lines, is_section_header, read_line, roslyn_lower

NEXT_LINE = chr(0x85)
LINE_SEPARATOR = chr(0x2028)
PARAGRAPH_SEPARATOR = chr(0x2029)
# Characters str.splitlines breaks at and the compiler does not: a vertical tab, a form feed, and a file separator.
WITHIN_A_LINE = "g" + chr(0x0B) + "h" + chr(0x0C) + "i" + chr(0x1C) + "j"


class AnalyzerConfigLinesTests(unittest.TestCase):
    def test_lines_break_where_the_compiler_breaks_them_and_keep_their_terminators(self) -> None:
        text = f"a\r\nb\rc\nd{NEXT_LINE}e{LINE_SEPARATOR}f{PARAGRAPH_SEPARATOR}{WITHIN_A_LINE}"
        self.assertEqual(
            ["a\r\n", "b\r", "c\n", f"d{NEXT_LINE}", f"e{LINE_SEPARATOR}", f"f{PARAGRAPH_SEPARATOR}", WITHIN_A_LINE],
            analyzer_config_lines(text),
            "a vertical tab, a form feed, and a file separator, where str.splitlines breaks, are within a line",
        )
        self.assertEqual([], analyzer_config_lines(""))


class ReadLineTests(unittest.TestCase):
    def test_each_kind_of_line(self) -> None:
        cases = {
            "": ConfigLine("comment"),
            "   \n": ConfigLine("comment"),
            "  # a comment\n": ConfigLine("comment"),
            "; a comment": ConfigLine("comment"),
            "[*.cs] # C# files\r\n": ConfigLine("section", "*.cs"),
            "[src/\\#tag]": ConfigLine("section", "src/\\#tag"),
            "key = value": ConfigLine("property", "key", "value"),
            "  dotnet_diagnostic.CA1000.severity: warning ; noisy\n": ConfigLine(
                "property", "dotnet_diagnostic.CA1000.severity", "warning"
            ),
            "key =": ConfigLine("property", "key", ""),
            "a-b_c.d = x = y": ConfigLine("property", "a-b_c.d", "x = y"),
            "clé = valeur": ConfigLine("property", "clé", "valeur"),
            "not a setting": ConfigLine("other"),
            "  = error": ConfigLine("other"),
            "[]": ConfigLine("other"),
            "key\u00a0name = value": ConfigLine("other"),
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(expected, read_line(line))

    def test_a_section_header_is_read_alike_by_both_readers(self) -> None:
        self.assertTrue(is_section_header("[*.cs] ; comment"))
        self.assertFalse(is_section_header("key = [value]"))


class RoslynLowerTests(unittest.TestCase):
    def test_lowers_one_character_for_one_without_case_folding(self) -> None:
        self.assertEqual("dotnet_diagnostic.ca1000.severity", roslyn_lower("DOTNET_DIAGNOSTIC.CA1000.SEVERITY"))
        self.assertEqual("i", roslyn_lower("\u0130"))
        self.assertEqual("\u00df", roslyn_lower("\u00df"), "the sharp s is not folded to ss")
        self.assertEqual("\u017f", roslyn_lower("\u017f"), "the long s is not folded to s")
