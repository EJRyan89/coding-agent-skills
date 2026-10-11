"""The shared provider policy rejects an unverified PDF parser before it receives a document."""

from __future__ import annotations

import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pdf_tool

XPDF = "pdftotext version {version} [www.xpdfreader.com]\nCopyright 1996-2025 Glyph & Cog, LLC\n"
POPPLER = (
    "pdftotext version {version}\nCopyright 2005-2026 The Poppler Developers - https://poppler.freedesktop.org\n"
    "Copyright 1996-2011, 2022 Glyph & Cog, LLC\n"
)


class PdfPolicyTests(unittest.TestCase):
    def test_vendor_banners_have_separate_floors_and_expected_exit_statuses(self) -> None:
        for banner, version, provider, status in (
            (XPDF, "4.06", "Xpdf", 99),
            (XPDF, "4.06.0", "Xpdf", 0),
            (XPDF, "4.07", "Xpdf", 99),
            (POPPLER, "26.10.0", "Poppler", 0),
            (POPPLER, "26.11.0", "Poppler", 0),
        ):
            with self.subTest(provider=provider, version=version, status=status):
                identified = pdf_tool.parse_banner(banner.format(version=version), status)
                self.assertEqual(provider, identified.provider)
                self.assertEqual(tuple(map(int, version.split("."))), identified.version)

    def test_old_versions_are_refused_using_the_identified_provider_not_a_generic_number(self) -> None:
        for banner, version, provider, status in (
            (XPDF, "4.00", "Xpdf", 99),
            (XPDF, "4.05", "Xpdf", 99),
            (POPPLER, "0.86.1", "Poppler", 0),
            (POPPLER, "25.12.0", "Poppler", 0),
            (POPPLER, "26.9.99", "Poppler", 0),
        ):
            with self.subTest(provider=provider, version=version):
                with self.assertRaises(pdf_tool.PdfToolRefused) as raised:
                    pdf_tool.parse_banner(banner.format(version=version), status)
                identity = raised.exception.identity
                if identity is None:
                    self.fail("an outdated vendor banner must retain its identified provider")
                self.assertEqual(provider, identity.provider)
                self.assertIn("upgrade", str(raised.exception))

    def test_unknown_ambiguous_malformed_and_failed_banners_are_refused(self) -> None:
        banners = (
            ("pdftotext version 26.10.0\n", 0),
            ("unrelated version 99.99\n" + XPDF.format(version="4.05"), 99),
            (XPDF.format(version="4.06") + POPPLER.format(version="26.10.0"), 0),
            (XPDF.format(version="4.06") + "The Poppler Developers\n", 0),
            (XPDF.format(version="4.06-beta"), 99),
            (XPDF.format(version="4.06.0.1"), 99),
            (XPDF.format(version="4"), 99),
            (POPPLER.format(version="26.10"), 0),
            (POPPLER.format(version="26.13.0"), 0),
            (POPPLER.format(version="26.10.0") + "\nUsage: pdftotext\n", 99),
            (XPDF.format(version="4.06"), 1),
            (POPPLER.format(version="26.10.0"), 99),
        )
        for banner, status in banners:
            with self.subTest(banner=banner, status=status), self.assertRaises(pdf_tool.PdfToolRefused):
                pdf_tool.parse_banner(banner, status)

    def test_probe_is_no_input_bounded_and_reads_both_streams(self) -> None:
        running = mock.Mock()
        running.read.return_value = POPPLER.format(version="26.10.0").encode()
        running.wait.return_value = 0

        @contextmanager
        def stream(*args: Any, **kwargs: Any) -> Any:
            yield running

        with mock.patch.object(pdf_tool, "streaming", side_effect=stream) as called:
            identified = pdf_tool.probe_pdftotext("checked path")
        self.assertEqual("Poppler", identified.provider)
        self.assertEqual(["checked path", "-v"], called.call_args.args[0])
        self.assertTrue(called.call_args.kwargs["merge_stderr"])
        self.assertEqual(pdf_tool.PROBE_SECONDS, called.call_args.kwargs["timeout"])
        running.stdin.close.assert_called_once_with()
        running.read.assert_called_once_with(pdf_tool.MAX_PROBE_BYTES + 1)

    def test_probe_refuses_timeout_start_failure_flood_and_invalid_utf8_without_echoing_output(self) -> None:
        for failure in (OSError("private path"), subprocess.TimeoutExpired("private command", 5)):
            with (
                self.subTest(failure=failure),
                mock.patch.object(pdf_tool, "streaming", side_effect=failure),
                self.assertRaises(pdf_tool.PdfToolRefused) as raised,
            ):
                pdf_tool.probe_pdftotext("checked path")
            self.assertNotIn("private", str(raised.exception))
        for output in (b"x" * (pdf_tool.MAX_PROBE_BYTES + 1), b"\xff"):
            running = mock.Mock()
            running.read.return_value = output
            with (
                mock.patch.object(pdf_tool, "streaming") as stream,
                self.assertRaises(pdf_tool.PdfToolRefused),
            ):
                stream.return_value.__enter__.return_value = running
                pdf_tool.probe_pdftotext("checked path")


if __name__ == "__main__":
    unittest.main()
