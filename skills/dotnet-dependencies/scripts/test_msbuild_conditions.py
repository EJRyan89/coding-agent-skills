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
    result = subprocess.run([sys.executable, "-I", "-c", program], capture_output=True, text=True, check=False)
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
