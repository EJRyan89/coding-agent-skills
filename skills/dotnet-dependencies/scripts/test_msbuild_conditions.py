"""Regression tests for the expansion of property references and the evaluation of MSBuild conditions."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import pytest

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


def expand(text: str, **values: str | tuple[str | None, ...]) -> Value:
    known = properties(**values)
    return expanded(text, lambda name: known.get(name.casefold(), UNKNOWN), FILES)


class ConditionTests(unittest.TestCase):
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

    def test_negations_and_parentheses_nested_past_the_shared_depth_limit_are_not_settled(self) -> None:
        # The limit is pinned as a literal: 100 levels settle, and 101 are malformed, never a RecursionError.
        self.assertIs(True, holds("!" * 100 + "'a'=='a'"))
        self.assertIs(True, holds("(" * 100 + "'a'=='a'" + ")" * 100))
        self.assertIs(True, holds("!(" * 50 + "'a'=='a'" + ")" * 50 + "or 'a'=='b'"))
        for text in ("!" * 101 + "'a'=='a'", "(" * 101 + "'a'=='a'" + ")" * 101, "!(" * 2000 + "'a'" + ")" * 2000):
            with self.subTest(text=text[:4], length=len(text)):
                self.assertIsNone(holds(text))


def test_the_module_puts_skill_core_first_on_the_path_itself_and_imports_alone() -> None:
    # A caller that puts only this folder on sys.path, as this suite does, still finds nesting_depth. Isolated mode
    # (-I) starts the child with the standard library alone on its path.
    folder = Path(__file__).resolve().parent
    core = folder.parents[1] / "ejryan89-skill-core" / "scripts"
    program = (
        "import sys\n"
        f"sys.path.insert(0, {str(folder)!r})\n"
        "import msbuild_conditions, nesting_depth\n"
        "print(sys.path[0])\n"
        "print(nesting_depth.__file__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", program], capture_output=True, text=True, encoding="utf-8", check=False
    )
    assert result.returncode == 0, result.stderr
    first, found = result.stdout.splitlines()
    # Compared as text, so a folder spelled in another case fails here too, as its import fails where case matters.
    assert first == str(core)
    assert str(Path(found).parent) == str(core)


class ExpansionTests(unittest.TestCase):
    def test_property_functions_nested_past_the_shared_depth_limit_are_not_settled(self) -> None:
        # The limit is pinned as a literal: 100 nested property functions settle, and 101 do not, never a
        # RecursionError.
        def nested(levels: int) -> str:
            opening = "$([MSBuild]::GetPathOfFileAbove('Directory.Build.props', "
            return opening * levels + "'/repo/src'" + "))" * levels

        self.assertEqual(("/repo/Directory.Build.props",), expand(nested(100)))
        for levels in (101, 2000):
            with self.subTest(levels=levels):
                self.assertEqual(UNKNOWN, expand(nested(levels)))

    def test_a_reference_is_replaced_by_each_value_the_property_may_hold(self) -> None:
        self.assertEqual(("net472",), expand("$(Tfm)", tfm="net472"))
        self.assertEqual(("lib;net8.0",), expand("$(LIB);net8.0", lib="lib"))
        self.assertEqual(("a-x", "b-x"), expand("$(Name)-x", name=("a", "b")))
        self.assertEqual(("True",), expand("$(Tfm.StartsWith('net4'))", tfm="net472"))
        self.assertEqual(("plain",), expand("plain"))

    def test_the_file_above_functions_search_the_folders_above(self) -> None:
        found = expand(
            "$([MSBuild]::GetPathOfFileAbove('Directory.Build.props', '$(MSBuildThisFileDirectory)../'))",
            msbuildthisfiledirectory="/repo/src/",
        )
        self.assertEqual(("/repo/Directory.Build.props",), found)
        found = expand(
            "$([MSBuild]::GetDirectoryNameOfFileAbove($(MSBuildThisFileDirectory), 'Directory.Build.props'))",
            msbuildthisfiledirectory="/repo/src/",
        )
        self.assertEqual(("/repo",), found)
        self.assertEqual(("",), expand("$([MSBuild]::GetPathOfFileAbove('Absent.props', '/repo'))"))


@pytest.mark.parametrize(
    ("expected", "text", "framework"),
    [
        (True, "'$(TargetFramework)'=='net472'", "net472"),
        (True, "'$(TargetFramework)' == 'NET472'", "net472"),
        (True, "'net8.0' == '$(TargetFramework)'", "net8.0"),
        (True, "$(TargetFramework) == 'net8.0'", "net8.0"),
        (
            True,
            "('$(TargetFramework)' == 'a' or '$(TargetFramework)' == 'b') AND '$(TargetFramework)' != 'c'",
            "b",
        ),
        (True, "$(TargetFramework.StartsWith('net4'))", "net472"),
        (True, "'' == ''", "net8.0"),
        (False, "'$(TargetFramework)'=='net472'", "net8.0"),
        (False, "'$(TargetFramework)' != 'net8.0'", "net8.0"),
        (False, "$(TargetFramework.StartsWith('net4'))", "net8.0"),
        (False, "'$(TargetFramework)' == 'a' and '$(TargetFramework)' == 'b'", "a"),
        (None, "'$(Configuration)' == 'Debug'", "net8.0"),
        (None, "'$(TargetFrameworkIdentifier)' == '.NETFramework'", "net8.0"),
        (None, "$([MSBuild]::IsTargetFrameworkCompatible('$(TargetFramework)', 'net6.0'))", "net8.0"),
        (None, "'$(TargetFramework)' ==", "net8.0"),
        (None, "('$(TargetFramework)' == 'net8.0'", "net8.0"),
        (None, "", "net8.0"),
    ],
)
def test_conditions_are_evaluated_only_in_the_forms_the_reader_knows(
    expected: bool | None, text: str, framework: str
) -> None:
    assert expected is holds(text, framework)


DEBUG = "'$(Configuration)'=='Debug'"
MIRRORED = "'Debug'=='$(Configuration)'"
FRAMEWORK = "'$(TargetFramework)'=='net472'"


@pytest.mark.parametrize(
    ("expected", "text", "framework"),
    [
        (True, f"{FRAMEWORK} or {DEBUG}", "net472"),
        (True, f"{FRAMEWORK} or {MIRRORED}", "net472"),
        (True, f"{DEBUG} or {FRAMEWORK}", "net472"),
        (True, f"({DEBUG}) or ({FRAMEWORK})", "net472"),
        (True, "$(TargetFramework.StartsWith('net'))", "net8.0"),
        (False, f"{FRAMEWORK} and {DEBUG}", "net8.0"),
        (False, f"{FRAMEWORK} and {MIRRORED}", "net8.0"),
        (False, f"{DEBUG} and {FRAMEWORK}", "net8.0"),
        # MSBuild's StartsWith compares with regard to case.
        (False, "$(TargetFramework.StartsWith('NET'))", "net8.0"),
        # An operand the reader cannot evaluate leaves undecided what no other operand decides.
        (None, f"{FRAMEWORK} or {DEBUG}", "net8.0"),
        (None, f"{FRAMEWORK} and {DEBUG}", "net472"),
        # A dangling, leading, or doubled keyword, an unclosed or unopened parenthesis, a lone operand
        # that is not a boolean, and a missing operand make a condition MSBuild refuses.
        (None, f"{FRAMEWORK} or or", "net472"),
        (None, f"or {FRAMEWORK}", "net472"),
        (None, f"{FRAMEWORK} or", "net472"),
        (None, f"{FRAMEWORK} and", "net472"),
        (None, f"{FRAMEWORK} or and {FRAMEWORK}", "net472"),
        (None, f"{FRAMEWORK})", "net472"),
        (None, "'net472'", "net472"),
        (None, "== 'net472'", "net472"),
    ],
)
def test_one_operand_decides_whatever_its_place_and_a_malformed_condition_is_never_evaluated(
    expected: bool | None, text: str, framework: str
) -> None:
    assert expected is holds(text, framework)


@pytest.mark.parametrize(
    ("expected", "text"),
    [
        (True, "!'$(A)'=='b'"),
        (True, "!false"),
        (True, "true"),
        (True, "'$(Flag)'"),
        (True, "'ON' and yes"),
        (True, "$(Version) > 4"),
        (True, "'$(Version)' <= '4.5'"),
        (True, "0x10 >= 16"),
        (True, "$(A.EndsWith('a'))"),
        (True, "$(A.Contains('a'))"),
        (True, "Exists('here.props')"),
        (True, "Exists('$(A)/here.props')"),
        (True, "HasTrailingSlash('$(Dir)')"),
        (True, "'$(A)' == 'a' or $(Undefined.ToUpper())"),
        (False, "!true"),
        (False, "no"),
        (False, "'$(Version)' < '4'"),
        (False, "Exists('absent.props')"),
        (False, "Exists('')"),
        (False, "$(A.Contains('z'))"),
        (False, "HasTrailingSlash('$(A)')"),
        (None, "'$(Version)' < 'b'"),
        (None, "'@(Compile)' != ''"),
        (None, "'%(Identity)' == 'a'"),
        (None, "'$(A.ToUpper())' == 'A'"),
        (None, "Exists('$(Undefined)')"),
        (None, "SomeFunction('a')"),
        (None, "'$(A)"),
    ],
)
def test_the_operators_and_functions_the_reader_reads(expected: bool | None, text: str) -> None:
    known = {"a": "a", "flag": "True", "version": "4.5", "dir": "src/"}
    assert expected is holds(text, **known)


@pytest.mark.parametrize(
    "text",
    [
        "$(Undefined)",
        "$(A.Substring(1))",
        "@(Items)",
        "%(Identity)",
        "$(A",
        "$([System.IO.Path]::Combine('a', 'b'))",
    ],
)
def test_a_reference_the_reader_cannot_settle_leaves_the_text_unsettled(text: str) -> None:
    assert expand(text, a="a") == UNKNOWN


# MSBuild's verdicts, captured from the .NET 10 SDK's MSBuild (`dotnet msbuild -getProperty`) on a project holding
# `'<spelling>' > '0'` and `'<spelling>' < '0'` as conditions: "greater" or "less" for the one that held, "neither"
# when both failed, MSB4086 for a comparison MSBuild refuses as not numeric, and "version" for a spelling MSBuild orders
# as a version, which the reader leaves unsettled. The reader settles exactly what MSBuild settles as a number.
NUMBER_VERDICTS = [
    ("5", "greater"),
    ("+5", "greater"),
    ("-5", "less"),
    ("05", "greater"),
    (".5", "greater"),
    ("5.", "greater"),
    ("-.5", "less"),
    ("+.5", "greater"),
    ("-0", "neither"),
    ("NaN", "neither"),
    ("nan", "neither"),
    ("-NaN", "neither"),
    ("0x10", "greater"),
    ("0X10", "greater"),
    ("0x7FFFFFFF", "greater"),
    ("0x80000000", "less"),
    ("0xFFFFFFFF", "less"),
    ("0x00000000F", "greater"),
    (".", "MSB4086"),
    ("-", "MSB4086"),
    ("+", "MSB4086"),
    ("", "MSB4086"),
    ("1e3", "MSB4086"),
    ("1E3", "MSB4086"),
    ("1_0", "MSB4086"),
    ("0x1_0", "MSB4086"),
    ("inf", "MSB4086"),
    ("Infinity", "MSB4086"),
    ("-Infinity", "MSB4086"),
    ("\u0665", "MSB4086"),
    ("\uff15", "MSB4086"),
    (" 5", "MSB4086"),
    ("5 ", "MSB4086"),
    ("\u00a05", "MSB4086"),
    ("1,000", "MSB4086"),
    ("0x", "MSB4086"),
    ("-0x10", "MSB4086"),
    ("+0x10", "MSB4086"),
    ("0x100000000", "MSB4086"),
    ("0xg", "MSB4086"),
    ("0x\uff11", "MSB4086"),
    ("0x-1", "MSB4086"),
    ("9" * 400, "MSB4086"),
    ("--5", "MSB4086"),
    ("5-", "MSB4086"),
    ("(5)", "MSB4086"),
    ("0b1", "MSB4086"),
    ("1d", "MSB4086"),
    ("5f", "MSB4086"),
    ("1.2.3", "version"),
    ("1.5.0", "version"),
    (" 5.0", "version"),
]


@pytest.mark.parametrize(("spelling", "verdict"), NUMBER_VERDICTS)
def test_a_number_is_read_as_msbuild_reads_one_and_any_other_spelling_leaves_the_comparison_unsettled(
    spelling: str, verdict: str
) -> None:
    expected = {
        "greater": (True, False),
        "less": (False, True),
        "neither": (False, False),
        "MSB4086": (None, None),
        "version": (None, None),
    }[verdict]
    assert expected == (holds("'$(Level)' > '0'", Level=spelling), holds("'$(Level)' < '0'", Level=spelling))


# MSBuild's `==` verdicts, captured the same way: it compares two numbers as numbers, two booleans as booleans, and
# anything else as text with .NET's ordinal comparison ignoring case, which folds no character beyond ASCII onto an
# ASCII one and none onto several.
EQUALITY_VERDICTS = [
    ("1.0", "1", True),
    ("0x10", "16", True),
    ("1.0", "01", True),
    ("+1", "1", True),
    (".5", "0.5", True),
    ("-0", "0", True),
    ("5.", "5", True),
    ("0x0F", "0xf", True),
    ("0x80000000", "-2147483648", True),
    ("NaN", "NaN", False),
    ("nan", "NaN", False),
    ("-NaN", "NaN", False),
    ("+NaN", "1", False),
    ("NaN", "x", False),
    ("true", "false", False),
    ("on", "off", False),
    ("yes", "true", True),
    ("on", "!false", True),
    ("no", "!yes", True),
    ("ON", "on", True),
    ("true", "TRUE", True),
    ("yes", "YES", True),
    ("yes", "1", False),
    (" 1", "1", False),
    ("1.2.3", "1.2.3", True),
    ("1.0.0", "1.0", False),
    ("1e3", "1000", False),
    ("\u00c4", "\u00e4", True),
    ("\u01c5", "\u01c6", True),
    ("\u01c5", "\u01c4", True),
    ("\u03c2", "\u03c3", True),
    ("\u03c2", "\u03a3", True),
    ("\u00b5", "\u03bc", True),
    ("\u00b5", "\u039c", True),
    ("\U00010400", "\U00010428", True),
    ("\u03d0", "\u03b2", True),
    ("\u03a9", "\u03c9", True),
    ("\u0131", "I", False),
    ("\u0131", "i", False),
    ("\u017f", "s", False),
    ("\u017f", "S", False),
    ("\u00df", "ss", False),
    ("\u00df", "\u1e9e", False),
    ("\u212a", "k", False),
    ("\u212a", "K", False),
    ("\ufb00", "ff", False),
    ("\u0130", "i", False),
    ("\u2126", "\u03c9", False),
]


@pytest.mark.parametrize(("left", "right", "verdict"), EQUALITY_VERDICTS)
def test_equality_compares_numbers_booleans_and_text_as_msbuild_does(left: str, right: str, verdict: bool) -> None:
    assert verdict is holds("'$(Left)' == '$(Right)'", Left=left, Right=right)
    assert (not verdict) is holds("'$(Left)' != '$(Right)'", Left=left, Right=right)


def test_text_whose_simple_uppercase_python_cannot_tell_leaves_equality_unsettled() -> None:
    # A Greek letter with a subscript iota: Python's uppercase of it is two letters, so its simple one is not known.
    assert holds("'$(Left)' == '$(Right)'", Left="\u1fb3", Right="\u1fbc") is None
    assert holds("'$(Left)' == '$(Right)'", Left="\u1fb3x", Right="\u1fbcy") is False
    # A number or a boolean beside such text is compared as text too, since the other side is neither, so it stays
    # unsettled rather than unequal.
    for known, text in (("1", "\u1fb3"), ("yes", "\u1fb3\u1fb3\u1fb3"), ("true", "\u1fb3\u1fb3\u1fb3\u1fb3")):
        assert holds("'$(Left)' == '$(Right)'", Left=known, Right=text) is None, known
        assert holds("'$(Left)' == '$(Right)'", Left=text, Right=known) is None, known


# MSBuild's verdicts on a lone operand, captured the same way: only the whole value, in any case, is a boolean, and any
# other is MSB4113, a condition MSBuild refuses.
BOOLEAN_VERDICTS = [
    ("TRUE", True),
    ("!FALSE", True),
    ("On", True),
    ("nO", False),
    (" true", None),
    ("true ", None),
    ("\ttrue", None),
    ("! false", None),
    ("ye\u017f", None),
]


@pytest.mark.parametrize(("value", "verdict"), BOOLEAN_VERDICTS)
def test_a_lone_operand_is_a_boolean_only_as_msbuild_reads_one(value: str, verdict: bool | None) -> None:
    assert verdict is holds("'$(Flag)'", Flag=value)


@pytest.mark.parametrize(
    "text",
    [
        "$(A.\u017ftartsWith('a'))",
        "$(A.Contain\u017f('a'))",
        "$([M\u017fBuild]::GetPathOfFileAbove('Directory.Build.props', '/repo'))",
    ],
)
def test_a_method_or_function_name_is_matched_in_ascii_letters_alone(text: str) -> None:
    assert expand(text, a="a") == UNKNOWN


def test_a_method_or_function_name_is_matched_in_any_ascii_case() -> None:
    # MSBuild finds a property function and a [MSBuild] function by its name in any case.
    assert expand("$(A.startswith('a'))", a="a") == ("True",)
    assert expand("$(A.CONTAINS('z'))", a="a") == ("False",)
    assert expand("$([msbuild]::getpathoffileabove('Directory.Build.props', '/repo'))") == (
        "/repo/Directory.Build.props",
    )
