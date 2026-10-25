"""Regression tests for the reader of the guidance each audit skill's checks derive from."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_provenance
from check_provenance import CONTENT_ERRORS, PROVENANCE_FILE, ProvenanceError, Source, lines, load

PAGE = {"page": "https://example.com/docs/skills", "passage": "Keep the body short."}
DECISION = {"decision": "This source grants each script by name (docs/design.md, Decisions)."}


class LoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.path = Path(self._temporary.name) / "provenance.json"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def write(self, document: Any) -> Path:
        self.path.write_text(json.dumps(document), encoding="utf-8")
        return self.path

    def refusal(self, document: Any, checks: tuple[str, ...] = ("alpha",)) -> str:
        with self.assertRaises(ProvenanceError) as caught:
            load(self.write(document), checks)
        return str(caught.exception)

    def test_it_reads_a_page_and_a_decision_in_the_file_order(self) -> None:
        found = load(self.write({"checks": {"BETA": [DECISION], "alpha": [PAGE, DECISION]}}), {"alpha", "BETA"})
        self.assertEqual(
            {
                "alpha": (
                    Source("https://example.com/docs/skills", "Keep the body short."),
                    Source(None, "This source grants each script by name (docs/design.md, Decisions)."),
                ),
                "BETA": (Source(None, "This source grants each script by name (docs/design.md, Decisions)."),),
            },
            found,
        )

    def test_a_check_without_an_entry_and_an_entry_without_a_check_are_refused_by_name(self) -> None:
        message = self.refusal({"checks": {"gone": [PAGE]}}, ("alpha", "beta"))
        self.assertTrue(message.endswith("provenance.json: no entry for alpha, beta; an entry for no check: gone"))

    def test_an_entry_without_a_source_is_refused(self) -> None:
        self.assertIn(
            "$.checks.alpha must be a non-empty array, found an empty array", self.refusal({"checks": {"alpha": []}})
        )

    def test_a_source_of_another_shape_is_refused_at_its_field(self) -> None:
        cases = {
            "page without passage": (
                {"page": "https://example.com"},
                "$.checks.alpha[0] must be an object with field passage, found an object without it",
            ),
            "page and decision": (
                {**PAGE, **DECISION},
                '$.checks.alpha[0] must be an object of known fields, found unknown field "decision"',
            ),
            "neither": ({"url": "https://example.com"}, "$.checks.alpha[0] must be an object with field decision"),
            "http page": (
                {**PAGE, "page": "http://example.com"},
                '$.checks.alpha[0].page must be a URL starting https:// without whitespace, found string "http://',
            ),
            "space in page": (
                {**PAGE, "page": "https://example.com/a b"},
                "$.checks.alpha[0].page must be a URL starting https:// without whitespace",
            ),
            "two-line passage": (
                {**PAGE, "passage": "One.\nTwo."},
                "$.checks.alpha[0].passage must be a non-empty trimmed single-line string",
            ),
            "blank decision": ({"decision": " "}, "$.checks.alpha[0].decision must be a non-empty"),
            "not an object": ("https://example.com", "$.checks.alpha[0] must be an object"),
        }
        for name, (source, expected) in cases.items():
            with self.subTest(name):
                self.assertIn(expected, self.refusal({"checks": {"alpha": [source]}}))

    def test_a_file_that_is_not_the_object_is_refused(self) -> None:
        self.assertIn("$ must be an object with field checks", self.refusal({"rules": {}}))
        self.assertIn("$.checks must be an object", self.refusal({"checks": []}))

    def test_a_missing_or_unparsable_file_is_refused_with_its_path(self) -> None:
        with self.assertRaises(ProvenanceError) as missing:
            load(self.path, ())
        self.assertTrue(str(missing.exception).startswith(f"cannot read {self.path.as_posix()}: "))
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaises(ProvenanceError) as broken:
            load(self.path, ())
        self.assertTrue(str(broken.exception).startswith(f"cannot read {self.path.as_posix()}: "))


class LinesTests(unittest.TestCase):
    def test_each_source_is_one_line_naming_the_check(self) -> None:
        provenance = {
            "wide-glob": (Source("https://example.com/p", "Bound what a tool returns."), Source(None, "A decision.")),
        }
        self.assertEqual(
            ["PAGE wide-glob https://example.com/p Bound what a tool returns.", "DECISION wide-glob A decision."],
            lines(provenance, "wide-glob"),
        )


def test_the_file_sits_in_the_skill_references() -> None:
    assert PROVENANCE_FILE.as_posix() == "references/provenance.json"


class ContentErrorsTests(unittest.TestCase):
    def test_the_family_is_the_provenance_error_and_covers_an_unreadable_and_a_malformed_file(self) -> None:
        self.assertEqual((ProvenanceError,), CONTENT_ERRORS)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provenance.json"
            for text in ("{", '{"checks": []}'):
                path.write_text(text, encoding="utf-8")
                with self.subTest(text=text), self.assertRaises(CONTENT_ERRORS):
                    load(path, ["a"])


def test_the_readers_that_raise_the_family_are_named() -> None:
    assert check_provenance.CONTENT_READERS == ("load",)
