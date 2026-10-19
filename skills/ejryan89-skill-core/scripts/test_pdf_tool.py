"""The shared provider policy rejects an unverified PDF parser before it receives a document."""

from __future__ import annotations

import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, get_args
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pdf_tool

XPDF = "pdftotext version {version} [www.xpdfreader.com]\nCopyright 1996-2025 Glyph & Cog, LLC\n"
POPPLER = (
    "pdftotext version {version}\nCopyright 2005-2026 The Poppler Developers - https://poppler.freedesktop.org\n"
    "Copyright 1996-2011, 2022 Glyph & Cog, LLC\n"
)


# What a refusal tells the user to install: each provider's floor, and on Linux, where a distribution's Poppler keeps a
# version below its floor as it backports fixes, the Xpdf command-line tools Xpdf publishes for it.
LINUX_HINT = (
    "a distribution's Poppler keeps a version below its floor as it backports fixes, so install the Xpdf command-line "
    "tools from https://www.xpdfreader.com/download.html"
)
INSTALL = "install Xpdf 4.06 or newer or Poppler 26.10.0 or newer" + (
    "" if sys.platform == "win32" else f"; {LINUX_HINT}"
)
# The banner of a distribution's Poppler, which keeps its upstream version while it backports fixes.
DISTRIBUTION_POPPLER = (
    "pdftotext version 24.02.0\nCopyright 2005-2024 The Poppler Developers - http://poppler.freedesktop.org\n"
    "Copyright 1996-2011, 2022 Glyph & Cog, LLC\n"
)


class PdfPolicyTests(unittest.TestCase):
    def test_versions_are_named_as_each_vendor_numbers_them_and_compared_as_numbers(self) -> None:
        for banner, version, status, refusal in (
            (XPDF, "4.00", 99, "Xpdf 4.00, older than Xpdf 4.06"),
            (XPDF, "4.05", 99, "Xpdf 4.05, older than Xpdf 4.06"),
            (XPDF, "3.04", 99, "Xpdf 3.04, older than Xpdf 4.06"),
            (POPPLER, "26.09.0", 0, "Poppler 26.09.0, older than Poppler 26.10.0"),
            (POPPLER, "0.86.1", 0, "Poppler 0.86.1, older than Poppler 26.10.0"),
        ):
            with self.subTest(version=version), self.assertRaises(pdf_tool.PdfToolRefused) as raised:
                pdf_tool.parse_banner(banner.format(version=version), status)
            self.assertEqual(refusal, str(raised.exception))
        for banner, version, status, named in (
            (XPDF, "4.06", 99, "4.06"),
            (XPDF, "4.10", 99, "4.10"),
            (XPDF, "4.06.0", 0, "4.06.0"),
            (POPPLER, "26.10.0", 0, "26.10.0"),
            (POPPLER, "27.01.2", 0, "27.01.2"),
        ):
            with self.subTest(version=version):
                self.assertEqual(named, pdf_tool.parse_banner(banner.format(version=version), status).version_text)
        self.assertEqual("Xpdf 4.06 or newer or Poppler 26.10.0 or newer", pdf_tool.requirements())
        with self.assertRaises(pdf_tool.PdfToolRefused) as raised:
            pdf_tool.parse_banner("pdftotext version 26.10.0\n", 0)
        self.assertEqual("provider/version is unidentifiable", str(raised.exception))

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


# Where Git for Windows keeps the Xpdf it bundles, which Git Bash's login profile puts ahead of the Windows PATH.
GIT_BUNDLED = r"C:\Program Files\Git\mingw64\bin\pdftotext.exe"
SUPPORTED = r"C:\Tools\Xpdf\pdftotext.exe"
POPPLER_OLD = r"C:\Tools\Poppler\pdftotext.exe"


class SelectPdftotextTests(unittest.TestCase):
    """The choice among every pdftotext the locator finds, with each probe answered by a banner per path."""

    def select(self, found: list[str], answers: dict[str, tuple[str, int]]) -> tuple[pdf_tool.PdfSelection, list[str]]:
        probed: list[str] = []
        by_resolved = {str(Path(path).resolve()): answer for path, answer in answers.items()}

        def probe(path: str) -> pdf_tool.PdfIdentity:
            probed.append(path)
            if path not in by_resolved:
                raise pdf_tool.PdfToolRefused("version probe could not be started")
            return pdf_tool.parse_banner(*by_resolved[path])

        return pdf_tool.select_pdftotext(found, probe), probed

    def test_a_supported_parser_behind_an_unsupported_one_is_chosen_and_the_skip_named(self) -> None:
        selection, probed = self.select(
            [GIT_BUNDLED, SUPPORTED],
            {GIT_BUNDLED: (XPDF.format(version="4.00"), 99), SUPPORTED: (XPDF.format(version="4.06"), 99)},
        )
        self.assertEqual((str(Path(SUPPORTED).resolve()), (4, 6)), (selection.path, selection.identity.version))
        self.assertEqual([str(Path(path).resolve()) for path in (GIT_BUNDLED, SUPPORTED)], probed)
        self.assertEqual(
            r"skipped C:\Program Files\Git\mingw64\bin\pdftotext.exe (Xpdf 4.00, older than Xpdf 4.06)",
            pdf_tool.skipped_text(selection.skipped),
        )

    def test_the_first_supported_parser_is_chosen_without_probing_the_rest(self) -> None:
        selection, probed = self.select([SUPPORTED, GIT_BUNDLED], {SUPPORTED: (XPDF.format(version="4.06"), 99)})
        self.assertEqual(((), [str(Path(SUPPORTED).resolve())]), (selection.skipped, probed))

    def test_only_unsupported_parsers_are_refused_naming_each_with_its_provider_version_and_place(self) -> None:
        with self.assertRaises(pdf_tool.PdfToolRefused) as raised:
            self.select(
                [GIT_BUNDLED, POPPLER_OLD],
                {GIT_BUNDLED: (XPDF.format(version="4.00"), 99), POPPLER_OLD: (POPPLER.format(version="0.86.1"), 0)},
            )
        self.assertEqual(
            r"no supported pdftotext: skipped C:\Program Files\Git\mingw64\bin\pdftotext.exe (Xpdf 4.00, older than "
            r"Xpdf 4.06), C:\Tools\Poppler\pdftotext.exe (Poppler 0.86.1, older than Poppler 26.10.0); "
            f"{INSTALL}",
            str(raised.exception),
        )
        self.assertEqual(pdf_tool.PdfIdentity("Xpdf", (4, 0)), raised.exception.identity)

    def test_gits_bundled_parser_alone_is_named_as_skipped_and_the_supported_providers_as_what_to_install(
        self,
    ) -> None:
        with self.assertRaises(pdf_tool.PdfToolRefused) as raised:
            self.select([GIT_BUNDLED], {GIT_BUNDLED: (XPDF.format(version="4.00"), 99)})
        self.assertEqual(
            r"no supported pdftotext: skipped C:\Program Files\Git\mingw64\bin\pdftotext.exe (Xpdf 4.00, older than "
            f"Xpdf 4.06); {INSTALL}",
            str(raised.exception),
        )

    def test_a_parser_that_cannot_be_identified_or_started_is_skipped_with_what_the_probe_found(self) -> None:
        with self.assertRaises(pdf_tool.PdfToolRefused) as raised:
            self.select([GIT_BUNDLED, SUPPORTED], {GIT_BUNDLED: ("pdftotext version 26.10.0\n", 0)})
        self.assertEqual(
            r"no supported pdftotext: skipped C:\Program Files\Git\mingw64\bin\pdftotext.exe (provider/version is "
            r"unidentifiable), C:\Tools\Xpdf\pdftotext.exe (version probe could not be started); "
            f"{INSTALL}",
            str(raised.exception),
        )
        self.assertIsNone(raised.exception.identity)

    def test_a_path_that_cannot_be_resolved_is_skipped_unprobed(self) -> None:
        with (
            mock.patch.object(Path, "resolve", side_effect=OSError("private")),
            self.assertRaises(pdf_tool.PdfToolRefused) as raised,
        ):
            pdf_tool.select_pdftotext([SUPPORTED], mock.Mock(side_effect=AssertionError("probed")))
        self.assertEqual(
            r"no supported pdftotext: skipped C:\Tools\Xpdf\pdftotext.exe (could not be accessed); "
            f"{INSTALL}",
            str(raised.exception),
        )

    if sys.platform == "win32":

        def test_the_refusal_names_each_providers_floor_as_what_to_install(self) -> None:
            with self.assertRaises(pdf_tool.PdfToolRefused) as raised:
                self.select([POPPLER_OLD], {POPPLER_OLD: (DISTRIBUTION_POPPLER, 0)})
            self.assertEqual(
                r"no supported pdftotext: skipped C:\Tools\Poppler\pdftotext.exe (Poppler 24.02.0, older than Poppler "
                "26.10.0); install Xpdf 4.06 or newer or Poppler 26.10.0 or newer",
                str(raised.exception),
            )
            self.assertIsNone(pdf_tool.INSTALL_HINT, "Windows needs no word beyond the floors")

    else:

        def test_a_distributions_poppler_is_refused_naming_its_version_and_floor_and_the_xpdf_tools(self) -> None:
            with self.assertRaises(pdf_tool.PdfToolRefused) as raised:
                self.select(["/usr/bin/pdftotext"], {"/usr/bin/pdftotext": (DISTRIBUTION_POPPLER, 0)})
            self.assertEqual(
                "no supported pdftotext: skipped /usr/bin/pdftotext (Poppler 24.02.0, older than Poppler 26.10.0); "
                f"install Xpdf 4.06 or newer or Poppler 26.10.0 or newer; {LINUX_HINT}",
                str(raised.exception),
            )
            self.assertEqual(pdf_tool.PdfIdentity("Poppler", (24, 2, 0)), raised.exception.identity)

    def test_the_xpdf_tools_behind_a_distributions_poppler_are_chosen_and_the_poppler_named(self) -> None:
        # The Xpdf command-line tools a user puts on PATH after the distribution's package, as on Linux.
        xpdf, poppler = "/opt/xpdf-tools/bin64/pdftotext", "/usr/bin/pdftotext"
        selection, _ = self.select(
            [poppler, xpdf], {poppler: (DISTRIBUTION_POPPLER, 0), xpdf: (XPDF.format(version="4.06"), 99)}
        )
        self.assertEqual((str(Path(xpdf).resolve()), "Xpdf"), (selection.path, selection.identity.provider))
        self.assertEqual(
            f"skipped {poppler} (Poppler 24.02.0, older than Poppler 26.10.0)", pdf_tool.skipped_text(selection.skipped)
        )

    def test_nothing_to_choose_from_is_the_callers_error(self) -> None:
        with self.assertRaises(ValueError) as raised:
            pdf_tool.select_pdftotext([], mock.Mock())
        self.assertIs(ValueError, type(raised.exception), "not a refusal: the caller reports a missing parser")


def banner_id(value: object) -> str | None:
    """A test ID naming a vendor's banner by its vendor, leaving every other value's ID to pytest."""
    return {XPDF: "xpdf-banner", POPPLER: "poppler-banner"}.get(value) if isinstance(value, str) else None


@pytest.mark.parametrize(
    ("banner", "version", "provider", "status"),
    [
        (XPDF, "4.06", "Xpdf", 99),
        (XPDF, "4.06.0", "Xpdf", 0),
        (XPDF, "4.07", "Xpdf", 99),
        (POPPLER, "26.10.0", "Poppler", 0),
        (POPPLER, "26.11.0", "Poppler", 0),
    ],
    ids=banner_id,
)
def test_vendor_banners_have_separate_floors_and_expected_exit_statuses(
    banner: str, version: str, provider: str, status: int
) -> None:
    identified = pdf_tool.parse_banner(banner.format(version=version), status)
    assert provider == identified.provider
    assert tuple(map(int, version.split("."))) == identified.version


@pytest.mark.parametrize(
    ("banner", "version", "provider", "status"),
    [
        (XPDF, "4.00", "Xpdf", 99),
        (XPDF, "4.05", "Xpdf", 99),
        (POPPLER, "0.86.1", "Poppler", 0),
        (POPPLER, "25.12.0", "Poppler", 0),
        (POPPLER, "26.9.99", "Poppler", 0),
    ],
    ids=banner_id,
)
def test_old_versions_are_refused_using_the_identified_provider_not_a_generic_number(
    banner: str, version: str, provider: str, status: int
) -> None:
    with pytest.raises(pdf_tool.PdfToolRefused) as raised:
        pdf_tool.parse_banner(banner.format(version=version), status)
    identity = raised.value.identity
    if identity is None:
        raise AssertionError("an outdated vendor banner must retain its identified provider")
    assert provider == identity.provider
    assert "older than" in str(raised.value)


@pytest.mark.parametrize(
    ("banner", "status"),
    [
        pytest.param("pdftotext version 26.10.0\n", 0, id="a-version-line-alone"),
        pytest.param("unrelated version 99.99\n" + XPDF.format(version="4.05"), 99, id="an-unrelated-version-first"),
        pytest.param(XPDF.format(version="4.06") + POPPLER.format(version="26.10.0"), 0, id="both-vendors-banners"),
        pytest.param(XPDF.format(version="4.06") + "The Poppler Developers\n", 0, id="xpdf-naming-poppler"),
        pytest.param(XPDF.format(version="4.06-beta"), 99, id="xpdf-4.06-beta"),
        pytest.param(XPDF.format(version="4.06.0.1"), 99, id="xpdf-4.06.0.1"),
        pytest.param(XPDF.format(version="4"), 99, id="xpdf-4"),
        pytest.param(POPPLER.format(version="26.10"), 0, id="poppler-26.10"),
        pytest.param(POPPLER.format(version="26.13.0"), 0, id="poppler-26.13.0"),
        pytest.param(POPPLER.format(version="26.10.0") + "\nUsage: pdftotext\n", 99, id="poppler-with-a-usage-line"),
        pytest.param(XPDF.format(version="4.06"), 1, id="xpdf-4.06-status-1"),
        pytest.param(POPPLER.format(version="26.10.0"), 99, id="poppler-26.10.0-status-99"),
    ],
)
def test_unknown_ambiguous_malformed_and_failed_banners_are_refused(banner: str, status: int) -> None:
    with pytest.raises(pdf_tool.PdfToolRefused):
        pdf_tool.parse_banner(banner, status)


def test_each_provider_the_probe_accepts_has_a_floor() -> None:
    assert get_args(pdf_tool.PdfProvider) == ("Xpdf", "Poppler")
    assert tuple(pdf_tool.MINIMUMS) == ("Xpdf", "Poppler")
