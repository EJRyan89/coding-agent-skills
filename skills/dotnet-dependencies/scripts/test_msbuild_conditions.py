"""Regression tests for the expansion of property references and the evaluation of MSBuild conditions."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from msbuild_conditions import UNKNOWN, FileSystem, Value, condition_value, expanded

# The file system as a condition sees it in these tests: `here.props` exists, and `Directory.Build.props` is in `/repo`.
FILES = FileSystem(
    "/repo/src/App",
    lambda path: path.endswith("here.props"),
    lambda name, start: "/repo" if name == "Directory.Build.props" else "",
)


def properties(**values: str | tuple[str | None, ...]) -> dict[str, Value]:
    """Property values keyed case-folded, each one value or the values it may hold."""
    return {name.casefold(): value if isinstance(value, tuple) else (value,) for name, value in values.items()}


def holds(text: str, framework: str | None = None, **others: str | tuple[str | None, ...]) -> bool | None:
    """Whether a condition holds with `$(TargetFramework)` as `framework` and the other properties given; every
    property not given is one the reader cannot settle."""
    known = properties(**others)
    if framework is not None:
        known["targetframework"] = (framework,)
    return condition_value(text, lambda name: known.get(name.casefold(), UNKNOWN), FILES)


class ConditionTests(unittest.TestCase):
    def assert_cases(self, cases: dict[bool | None, list[tuple[str, str]]]) -> None:
        for expected, entries in cases.items():
            for text, framework in entries:
                with self.subTest(text=text, framework=framework):
                    self.assertIs(expected, holds(text, framework))

    def test_conditions_are_evaluated_only_in_the_forms_the_reader_knows(self) -> None:
        self.assert_cases(
            {
                True: [
                    ("'$(TargetFramework)'=='net472'", "net472"),
                    ("'$(TargetFramework)' == 'NET472'", "net472"),
                    ("'net8.0' == '$(TargetFramework)'", "net8.0"),
                    ("$(TargetFramework) == 'net8.0'", "net8.0"),
                    (
                        "('$(TargetFramework)' == 'a' or '$(TargetFramework)' == 'b') AND '$(TargetFramework)' != 'c'",
                        "b",
                    ),
                    ("$(TargetFramework.StartsWith('net4'))", "net472"),
                    ("'' == ''", "net8.0"),
                ],
                False: [
                    ("'$(TargetFramework)'=='net472'", "net8.0"),
                    ("'$(TargetFramework)' != 'net8.0'", "net8.0"),
                    ("$(TargetFramework.StartsWith('net4'))", "net8.0"),
                    ("'$(TargetFramework)' == 'a' and '$(TargetFramework)' == 'b'", "a"),
                ],
                None: [
                    ("'$(Configuration)' == 'Debug'", "net8.0"),
                    ("'$(TargetFrameworkIdentifier)' == '.NETFramework'", "net8.0"),
                    ("$([MSBuild]::IsTargetFrameworkCompatible('$(TargetFramework)', 'net6.0'))", "net8.0"),
                    ("'$(TargetFramework)' ==", "net8.0"),
                    ("('$(TargetFramework)' == 'net8.0'", "net8.0"),
                    ("", "net8.0"),
                ],
            }
        )

    def test_one_operand_decides_whatever_its_place_and_a_malformed_condition_is_never_evaluated(self) -> None:
        debug = "'$(Configuration)'=='Debug'"
        mirrored = "'Debug'=='$(Configuration)'"
        framework = "'$(TargetFramework)'=='net472'"
        self.assert_cases(
            {
                True: [
                    (f"{framework} or {debug}", "net472"),
                    (f"{framework} or {mirrored}", "net472"),
                    (f"{debug} or {framework}", "net472"),
                    (f"({debug}) or ({framework})", "net472"),
                    ("$(TargetFramework.StartsWith('net'))", "net8.0"),
                ],
                False: [
                    (f"{framework} and {debug}", "net8.0"),
                    (f"{framework} and {mirrored}", "net8.0"),
                    (f"{debug} and {framework}", "net8.0"),
                    # MSBuild's StartsWith compares with regard to case.
                    ("$(TargetFramework.StartsWith('NET'))", "net8.0"),
                ],
                None: [
                    # An operand the reader cannot evaluate leaves undecided what no other operand decides.
                    (f"{framework} or {debug}", "net8.0"),
                    (f"{framework} and {debug}", "net472"),
                    # A dangling, leading, or doubled keyword, an unclosed or unopened parenthesis, a lone operand
                    # that is not a boolean, and a missing operand make a condition MSBuild refuses.
                    (f"{framework} or or", "net472"),
                    (f"or {framework}", "net472"),
                    (f"{framework} or", "net472"),
                    (f"{framework} and", "net472"),
                    (f"{framework} or and {framework}", "net472"),
                    (f"{framework})", "net472"),
                    ("'net472'", "net472"),
                    ("== 'net472'", "net472"),
                ],
            }
        )

    def test_every_property_is_read_by_its_name_in_any_case(self) -> None:
        self.assertIs(True, holds("'$(targetframework)'=='net8.0'", "net8.0"))
        self.assertIs(True, holds("'$(Tfm)'=='net472' and $(TFM.StartsWith('net4'))", tfm="net472"))
        self.assertIs(
            True, holds("'$(Configuration)|$(Platform)'=='Debug|AnyCPU'", configuration="Debug", platform="AnyCPU")
        )

    def test_a_condition_reading_a_property_twice_is_decided_for_each_value_it_may_hold(self) -> None:
        either = ("net8.0", "net472")
        self.assertIs(True, holds("'$(Tfm)'=='net8.0' or '$(Tfm)'=='net472'", tfm=either))
        self.assertIs(False, holds("'$(Tfm)'=='net8.0' and '$(Tfm)'=='net472'", tfm=either))
        self.assertIsNone(holds("'$(Tfm)'=='net8.0'", tfm=either))
        self.assertIsNone(holds("'$(Tfm)'=='net8.0'", tfm=("net8.0", None)))

    def test_the_operators_and_functions_the_reader_reads(self) -> None:
        cases: dict[bool | None, list[str]] = {
            True: [
                "!'$(A)'=='b'",
                "!false",
                "true",
                "'$(Flag)'",
                "'ON' and yes",
                "$(Version) > 4",
                "'$(Version)' <= '4.5'",
                "0x10 >= 16",
                "$(A.EndsWith('a'))",
                "$(A.Contains('a'))",
                "Exists('here.props')",
                "Exists('$(A)/here.props')",
                "HasTrailingSlash('$(Dir)')",
                "'$(A)' == 'a' or $(Undefined.ToUpper())",
            ],
            False: [
                "!true",
                "no",
                "'$(Version)' < '4'",
                "Exists('absent.props')",
                "Exists('')",
                "$(A.Contains('z'))",
                "HasTrailingSlash('$(A)')",
            ],
            None: [
                "'$(Version)' < 'b'",
                "'@(Compile)' != ''",
                "'%(Identity)' == 'a'",
                "'$(A.ToUpper())' == 'A'",
                "Exists('$(Undefined)')",
                "SomeFunction('a')",
                "'$(A)",
            ],
        }
        known = {"a": "a", "flag": "True", "version": "4.5", "dir": "src/"}
        for expected, texts in cases.items():
            for text in texts:
                with self.subTest(text=text):
                    self.assertIs(expected, holds(text, **known))


class ExpansionTests(unittest.TestCase):
    def expand(self, text: str, **values: str | tuple[str | None, ...]) -> Value:
        known = properties(**values)
        return expanded(text, lambda name: known.get(name.casefold(), UNKNOWN), FILES)

    def test_a_reference_is_replaced_by_each_value_the_property_may_hold(self) -> None:
        self.assertEqual(("net472",), self.expand("$(Tfm)", tfm="net472"))
        self.assertEqual(("lib;net8.0",), self.expand("$(LIB);net8.0", lib="lib"))
        self.assertEqual(("a-x", "b-x"), self.expand("$(Name)-x", name=("a", "b")))
        self.assertEqual(("True",), self.expand("$(Tfm.StartsWith('net4'))", tfm="net472"))
        self.assertEqual(("plain",), self.expand("plain"))

    def test_a_reference_the_reader_cannot_settle_leaves_the_text_unsettled(self) -> None:
        for text in (
            "$(Undefined)",
            "$(A.Substring(1))",
            "@(Items)",
            "%(Identity)",
            "$(A",
            "$([System.IO.Path]::Combine('a', 'b'))",
        ):
            with self.subTest(text=text):
                self.assertEqual(UNKNOWN, self.expand(text, a="a"))

    def test_the_file_above_functions_search_the_folders_above(self) -> None:
        found = self.expand(
            "$([MSBuild]::GetPathOfFileAbove('Directory.Build.props', '$(MSBuildThisFileDirectory)../'))",
            msbuildthisfiledirectory="/repo/src/",
        )
        self.assertEqual(("/repo/Directory.Build.props",), found)
        found = self.expand(
            "$([MSBuild]::GetDirectoryNameOfFileAbove($(MSBuildThisFileDirectory), 'Directory.Build.props'))",
            msbuildthisfiledirectory="/repo/src/",
        )
        self.assertEqual(("/repo",), found)
        self.assertEqual(("",), self.expand("$([MSBuild]::GetPathOfFileAbove('Absent.props', '/repo'))"))


if __name__ == "__main__":
    unittest.main()
