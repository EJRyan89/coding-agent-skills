from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIRECTORY))

import csharp_layout as layout  # noqa: E402


def source(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def rules(text: str) -> list[tuple[int, str]]:
    """(one-based line, rule) for each violation."""
    return [(violation.line + 1, violation.rule) for violation in layout.check_text(text)]


# Every kind of literal and comment, each containing braces, quotes, and directive-like lines that are not code.
TRICKY = source(
    "class C",                                           # 1
    "{",                                                 # 2
    "    #region Literals",                              # 3
    "",                                                  # 4
    "    void M()",                                      # 5
    "    {",                                             # 6
    '        var a = "{ \\" }";',                        # 7
    '        var b = @"{ "" }',                          # 8
    "#endregion",                                        # 9  inside a verbatim string
    '";',                                                # 10
    '        var c = $"{{ {d["k"]:N2} }} {(x ? "}" : "{")}";',
    '        var e = $@"{{ {f} ',                        # 12
    '#endregion }}";',                                   # 13 inside an interpolated verbatim string
    '        var g = """',                               # 14
    "            { #endregion",                          # 15
    "            \"\" }",                                # 16
    '            """;',                                  # 17
    '        var h = $$"""{{{i}}} { }""";',              # 18
    "        char j = '{', k = '\\'', l = '\"';",        # 19
    "        /* {",                                      # 20
    "#endregion",                                        # 21 inside a block comment
    "        */ // }",                                   # 22
    "        var m = global::System.Math.PI;",           # 23
    '        var n = $"{global::System.Math.PI}";',      # 24
    "    }",                                             # 25
    "",                                                  # 26
    "    #endregion",                                    # 27
    "}",                                                 # 28
)


class LexerTests(unittest.TestCase):
    def test_literals_and_comments_hide_braces_and_directives(self) -> None:
        scan = layout.Lexer(TRICKY).lex()
        directives = [(event.line + 1, event.name) for event in scan.events if event.kind == "directive"]
        braces = [(event.line + 1, event.kind) for event in scan.events if event.kind != "directive"]
        self.assertEqual([(3, "region"), (27, "endregion")], directives)
        self.assertEqual([(2, "open"), (6, "open"), (25, "close"), (28, "close")], braces)
        self.assertEqual({9, 10, 13, 15, 16, 17, 21, 22}, {line + 1 for line in scan.continuation})
        self.assertEqual([], rules(TRICKY))

    def test_line_endings_are_counted_alike(self) -> None:
        for newline in ("\n", "\r\n", "\r"):
            with self.subTest(newline=repr(newline)):
                text = newline.join(("class C", "{", "    #region A", "", "    #endregion", "", "}", ""))
                self.assertEqual([(5, "endregion-brace-spacing")], rules(text))

    def test_multi_dollar_raw_strings_open_holes_only_at_their_brace_count(self) -> None:
        text = source('var s = $$$"""{{ {{{x}}} }}""";', "{")
        self.assertEqual([(1, "open")],
                         [(event.line, event.kind) for event in layout.Lexer(text).lex().events])


class RuleTests(unittest.TestCase):
    def test_endregion_must_close_in_the_scope_its_region_opened(self) -> None:
        text = source(
            "class C",                 # 1
            "{",                       # 2
            "    #region Outer",       # 3
            "",                        # 4
            "    void A()",            # 5
            "    {",                   # 6
            "    #endregion",          # 7  deeper than its #region
            "",                        # 8
            "        #region Inner",   # 9
            "    }",                   # 10
            "",                        # 11
            "    void B()",            # 12
            "    {",                   # 13
            "        #endregion",      # 14 same depth as #region Inner, but another method
            "    }",                   # 15
            "    #region Last",        # 16
            "",                        # 17
            "    int f;",              # 18
            "}",                       # 19
            "#endregion",              # 20 shallower than its #region
        )
        found = [(violation.line + 1, violation.rule, violation.severity) for violation in layout.check_text(text)]
        self.assertEqual([(7, "region-scope", "error"), (14, "region-scope", "error"),
                          (20, "region-scope", "error")], found)
        self.assertIn("(line 9)", layout.check_text(text)[1].message)

    def test_conditional_branches_do_not_double_count_braces(self) -> None:
        text = source(
            "#region All",
            "#if NET48",
            "class C : Base {",
            "#else",
            "class C {",
            "#endif",
            "    int f;",
            "}",
            "",
            "#endregion",
        )
        self.assertEqual([], rules(text))

    def test_endregion_description_and_spacing_rules(self) -> None:
        text = source(
            "class C",                  # 1
            "{",                        # 2
            "    #region A",            # 3
            "",                         # 4
            "    int a;",               # 5
            "",                         # 6
            "    #endregion A",         # 7  description, and no blank line before #region B
            "    #region B",            # 8
            "",                         # 9
            "    int b;",               # 10
            "",                         # 11
            "    #endregion",           # 12 two blank lines before #region C
            "",                         # 13
            "",                         # 14
            "    #region C",            # 15
            "",                         # 16
            "    #region Nested",       # 17
            "",                         # 18
            "    int c;",               # 19
            "",                         # 20
            "    #endregion",           # 21 directly before another #endregion: allowed
            "    #endregion // done",   # 22 description, and a blank line before the closing brace
            "",                         # 23
            "}",                        # 24
        )
        self.assertEqual([
            (7, "endregion-description"), (7, "region-spacing"), (12, "region-spacing"),
            (22, "endregion-brace-spacing"), (22, "endregion-description"),
        ], rules(text))
        fixed, applied = layout.fix_text(text)
        self.assertEqual(5, len(applied))
        self.assertEqual([], rules(fixed))
        self.assertEqual(source(
            "class C", "{", "    #region A", "", "    int a;", "", "    #endregion", "", "    #region B", "",
            "    int b;", "", "    #endregion", "", "    #region C", "", "    #region Nested", "", "    int c;", "",
            "    #endregion", "    #endregion", "}",
        ), fixed)
        self.assertEqual((fixed, []), layout.fix_text(fixed))

    def test_endregion_at_end_of_file_without_newline(self) -> None:
        text = "#region A\nclass C { }\n\n#endregion trailing"
        fixed, applied = layout.fix_text(text)
        self.assertEqual("#region A\nclass C { }\n\n#endregion", fixed)
        self.assertEqual(["endregion-description"], [violation.rule for violation in applied])


class FileTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def main(self, *arguments: str) -> tuple[int, list[list[str]]]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = layout.main(list(arguments))
        return status, [line.split("\t") for line in output.getvalue().splitlines()]

    def test_check_and_fix_preserve_bom_crlf_and_legacy_encoding(self) -> None:
        crlf = self.root / "Src" / "With Space.cs"
        crlf.parent.mkdir()
        crlf_text = "class C\r\n{\r\n    #region A\r\n\r\n    int a;\r\n\r\n    #endregion A\r\n\r\n}\r\n"
        crlf.write_bytes(b"\xef\xbb\xbf" + crlf_text.encode("utf-8"))
        legacy = self.root / "Legacy.cs"
        legacy.write_bytes("// caf\xe9\n#region A\n\n#endregion\n#region B\n\n#endregion\n".encode("cp1252"))
        clean = self.root / "Clean.cs"
        clean.write_bytes(b"class D { }\n")
        file_list = self.root / "files.txt"
        file_list.write_text("Src/With Space.cs\nLegacy.cs\nClean.cs\nMissing.cs\n", encoding="utf-8")

        status, lines = self.main("check", "--repo-root", str(self.root), "--file-list", str(file_list))
        self.assertEqual(0, status)
        self.assertEqual([
            ["VIOLATION", "Src/With Space.cs", "7", "warning", "endregion-brace-spacing",
             "expected no blank lines between #endregion and the closing brace, found 1"],
            ["VIOLATION", "Src/With Space.cs", "7", "warning", "endregion-description",
             "#endregion must not have a description; remove the trailing text"],
            ["VIOLATION", "Legacy.cs", "4", "warning", "region-spacing",
             "expected exactly 1 blank line between #endregion and the next #region, found 0"],
            ["SUMMARY", "3", "2", "endregion-brace-spacing,endregion-description,region-spacing"],
        ], lines)

        status, lines = self.main("check", "--repo-root", str(self.root), "--file-list", str(file_list), "--fix")
        self.assertEqual(0, status)
        self.assertEqual([
            ["FIXED", "Src/With Space.cs", "7", "endregion-brace-spacing"],
            ["FIXED", "Src/With Space.cs", "7", "endregion-description"],
            ["FIXED", "Legacy.cs", "4", "region-spacing"],
            ["SUMMARY", "0", "0", "-"],
        ], lines)
        self.assertEqual(
            b"\xef\xbb\xbf" + b"class C\r\n{\r\n    #region A\r\n\r\n    int a;\r\n\r\n    #endregion\r\n}\r\n",
            crlf.read_bytes(),
        )
        self.assertEqual(
            "// caf\xe9\n#region A\n\n#endregion\n\n#region B\n\n#endregion\n".encode("cp1252"), legacy.read_bytes()
        )
        self.assertEqual(b"class D { }\n", clean.read_bytes())

    def test_scope_violations_are_reported_but_not_fixed(self) -> None:
        path = self.root / "Scope.cs"
        original = b"class C\n{\n    #region A\n\n    void M()\n    {\n    #endregion\n    }\n}\n"
        path.write_bytes(original)
        file_list = self.root / "files.txt"
        file_list.write_text("Scope.cs\n", encoding="utf-8")
        status, lines = self.main("check", "--repo-root", str(self.root), "--file-list", str(file_list), "--fix")
        self.assertEqual(0, status)
        self.assertEqual(["VIOLATION", "Scope.cs", "7", "error", "region-scope"], lines[0][:5])
        self.assertEqual(["SUMMARY", "1", "1", "region-scope"], lines[1])
        self.assertEqual(original, path.read_bytes())


SDK_PROJECT = '<Project Sdk="Microsoft.NET.Sdk"></Project>\n'
SOLUTION = (
    'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "App", "src\\App\\App.csproj", "{1}"\r\nEndProject\r\n'
)


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        (self.root / "src" / "App").mkdir(parents=True)
        (self.root / "src" / "App" / "App.csproj").write_text(SDK_PROJECT, encoding="utf-8")
        (self.root / "App.sln").write_text(SOLUTION, encoding="utf-8")
        self.file_list = self.root / "files.txt"
        self.changed("src/App/Program.cs")

    def changed(self, *names: str) -> None:
        self.file_list.write_text("".join(f"{name}\n" for name in names), encoding="utf-8")

    def config(self, *extra: str, solution: str = "App.sln") -> dict[str, list[list[str]]]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = layout.main(["config", "--repo-root", str(self.root), "--solution", solution,
                                  "--file-list", str(self.file_list), *extra])
        self.assertEqual(0, status)
        grouped: dict[str, list[list[str]]] = {}
        for line in output.getvalue().splitlines():
            kind, *fields = line.split("\t")
            grouped.setdefault(kind, []).append(fields)
        return grouped

    def states(self, grouped: dict[str, list[list[str]]]) -> dict[str, str]:
        return {key: state for key, _, state in grouped["SETTING"]}

    def test_reports_missing_configuration_and_adds_only_editorconfig_settings(self) -> None:
        grouped = self.config()
        self.assertEqual([["Roslynator.Formatting.Analyzers", "missing"]], grouped["PACKAGE"])
        self.assertEqual({key: "missing" for key, _ in layout.SETTINGS}, self.states(grouped))
        self.assertNotIn("ADDED", grouped)
        self.assertFalse((self.root / ".editorconfig").exists())

        grouped = self.config("--apply")
        self.assertEqual([[".editorconfig", key, value] for key, value in layout.SETTINGS], grouped["ADDED"])
        self.assertEqual(
            "[*.cs]\n" + "".join(f"{key} = {value}\n" for key, value in layout.SETTINGS),
            (self.root / ".editorconfig").read_text(encoding="utf-8"),
        )
        grouped = self.config("--apply")
        self.assertEqual({key: "present" for key, _ in layout.SETTINGS}, self.states(grouped))
        self.assertNotIn("ADDED", grouped)
        self.assertEqual(["missing"], [state for _, state in grouped["PACKAGE"]])
        self.assertFalse(any(path.name == "Directory.Build.props" for path in self.root.rglob("*")))

    def test_respects_existing_values_sections_and_root_files(self) -> None:
        (self.root / ".editorconfig").write_text("[*.cs]\ndotnet_diagnostic.RCS0002.severity = warning\n",
                                                 encoding="utf-8")
        nested = self.root / "src" / ".editorconfig"
        nested.write_bytes(
            b"\xef\xbb\xbf# comment\r\nroot = true\r\n\r\n[*]\r\ninsert_final_newline = false\r\n\r\n"
            b"[*.{cs,vb}]\r\ndotnet_diagnostic.RCS0041.severity = error\r\n\r\n"
            b"[*.vb]\r\ndotnet_diagnostic.RCS0010.severity = warning\r\n\r\n"
            b"[tests/**.cs]\r\ndotnet_diagnostic.RCS0012.severity = warning"
        )
        (self.root / "src" / "App.sln").write_text(SOLUTION.replace("src\\", ""), encoding="utf-8")
        states = self.states(self.config(solution="src/App.sln"))
        self.assertEqual("other:false", states["insert_final_newline"])
        self.assertEqual("present", states["dotnet_diagnostic.RCS0041.severity"])
        self.assertEqual("missing", states["dotnet_diagnostic.RCS0010.severity"])
        self.assertEqual("missing", states["dotnet_diagnostic.RCS0012.severity"])
        self.assertEqual("missing", states["dotnet_diagnostic.RCS0002.severity"])  # above root = true

        added = self.config("--apply", solution="src/App.sln")["ADDED"]
        self.assertEqual({"src/.editorconfig"}, {target for target, _, _ in added})
        self.assertNotIn("insert_final_newline", {key for _, key, _ in added})
        content = nested.read_bytes()
        self.assertTrue(content.startswith(b"\xef\xbb\xbf# comment\r\n"))
        self.assertTrue(content.startswith(
            b"\xef\xbb\xbf# comment\r\nroot = true\r\n\r\n[*.cs]\r\ndotnet_diagnostic.RCS0010.severity = warning\r\n"
        ), "the added section comes first, so the existing sections still override it")
        self.assertTrue(content.endswith(b"[tests/**.cs]\r\ndotnet_diagnostic.RCS0012.severity = warning"))
        self.assertNotIn(b"\n", content.replace(b"\r\n", b""))
        states = self.states(self.config(solution="src/App.sln"))
        self.assertEqual("other:false", states["insert_final_newline"])
        self.assertEqual({"present"}, {state for key, state in states.items() if key != "insert_final_newline"})

    def test_path_specific_choices_are_judged_per_changed_file_and_never_overridden(self) -> None:
        editorconfig = self.root / ".editorconfig"
        editorconfig.write_text("root = true\n\n[src/**.cs]\ndotnet_diagnostic.RCS0041.severity = none\n",
                                encoding="utf-8")
        key = "dotnet_diagnostic.RCS0041.severity"
        self.assertEqual("other:none", self.states(self.config())[key])
        self.assertNotIn(key, {added for _, added, _ in self.config("--apply").get("ADDED", [])})

        editorconfig.write_text("root = true\n\n[src/**.cs]\ndotnet_diagnostic.RCS0041.severity = none\n",
                                encoding="utf-8")
        self.changed("src/App/Program.cs", "tools/Build.cs")
        self.assertEqual("missing", self.states(self.config())[key])
        self.config("--apply")
        self.assertEqual("none", layout.effective_settings([editorconfig], self.root / "src/App/Program.cs")
                         [key.casefold()])
        self.assertEqual("warning", layout.effective_settings([editorconfig], self.root / "tools/Build.cs")
                         [key.casefold()])
        self.assertTrue(editorconfig.read_text(encoding="utf-8").startswith("root = true\n\n[*.cs]\n"))

    def test_detects_package_in_project_or_directory_files(self) -> None:
        project = self.root / "src" / "App" / "App.csproj"
        project.write_text('<Project><ItemGroup><PackageReference Include="roslynator.formatting.analyzers" '
                           'Version="4.12.0" /></ItemGroup></Project>\n', encoding="utf-8")
        self.assertEqual([["Roslynator.Formatting.Analyzers", "present"]], self.config()["PACKAGE"])
        project.write_text(SDK_PROJECT, encoding="utf-8")
        (self.root / "Directory.Build.props").write_bytes(
            '<Project><ItemGroup><PackageReference Include="Roslynator.Formatting.Analyzers" /></ItemGroup>'
            '</Project>\n'.encode("utf-16")
        )
        self.assertEqual([["Roslynator.Formatting.Analyzers", "present"]], self.config()["PACKAGE"])

    def test_a_single_star_section_does_not_configure_deeper_files(self) -> None:
        settings = "".join(f"{key} = {value}\n" for key, value in layout.SETTINGS)
        (self.root / ".editorconfig").write_text(f"root = true\n\n[src/*.cs]\n{settings}", encoding="utf-8")
        self.assertEqual({"missing"}, set(self.states(self.config()).values()), "src/App/Program.cs is not in src/")
        self.changed("src/Top.cs")
        self.assertEqual({"present"}, set(self.states(self.config()).values()))

    def test_section_matching_follows_the_editorconfig_glob_specification(self) -> None:
        cases = (
            ("*", "A.cs", True),
            ("*.{cs,vb}", "src/A.cs", True),
            ("*.vb", "A.cs", False),
            ("*.cs", "src/deep/A.cs", True),
            # `*` never crosses a path separator; `**` does.
            ("src/*.cs", "src/A.cs", True),
            ("src/*.cs", "src/deep/A.cs", False),
            ("src/**.cs", "src/deep/A.cs", True),
            ("tests/**.cs", "src/A.cs", False),
            ("**/A.cs", "A.cs", True),
            ("**/A.cs", "src/deep/A.cs", True),
            ("src/**/A.cs", "src/A.cs", True),
            ("src/**/A.cs", "src/deep/A.cs", True),
            # A separator anywhere, including a leading one, anchors the glob to the .editorconfig directory.
            ("/A.cs", "A.cs", True),
            ("/A.cs", "src/A.cs", False),
            ("src/A.cs", "other/src/A.cs", False),
            ("?.cs", "src/A.cs", True),
            ("src?A.cs", "src/A.cs", False),
            ("[AB].cs", "B.cs", True),
            ("[AB].cs", "C.cs", False),
            ("[!AB].cs", "C.cs", True),
            ("[!AB].cs", "A.cs", False),
            ("[a-c].cs", "b.cs", True),
            ("[a-c].cs", "d.cs", False),
            ("[*].cs", "*.cs", True),
            ("[*].cs", "A.cs", False),
            ("[a/b].cs", "x/[a/b].cs", True),  # a bracket holding a separator is literal and anchors nothing
            ("[a/b].cs", "a.cs", False),
            ("{a,{b,c}}.cs", "c.cs", True),
            ("{a,{b,c}}.cs", "d.cs", False),
            ("{single}.cs", "{single}.cs", True),
            ("{single}.cs", "single.cs", False),
            ("{src/A,B}.cs", "src/A.cs", True),
            ("{src/A,B}.cs", "deep/B.cs", False),
            ("File{1..3}.cs", "File2.cs", True),
            ("File{1..3}.cs", "File4.cs", False),
            ("File{3..1}.cs", "File1.cs", True),
            ("File{-3..-1}.cs", "File-2.cs", True),
            ("File{-3..-1}.cs", "File-4.cs", False),
            ("{a{1..2},b}.cs", "b.cs", True),
            ("{a{1..2},b}.cs", "a3.cs", False),
            (r"\*.cs", "*.cs", True),
            (r"\*.cs", "A.cs", False),
            ("[.cs", "[.cs", True),
            ("{.cs", "{.cs", True),
            ("*.CS", "A.cs", False),
        )
        for section, path, expected in cases:
            with self.subTest(section=section, path=path):
                self.assertEqual(expected, layout.section_matches(section, path))


if __name__ == "__main__":
    unittest.main()
