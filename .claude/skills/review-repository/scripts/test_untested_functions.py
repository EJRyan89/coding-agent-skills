from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "untested_functions.py"


def diff_of(before: dict[str, str], after: dict[str, str]) -> str:
    """A git-style unified diff from one set of files to the other, three lines of context."""
    text = ""
    for path in sorted({*before, *after}):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        hunks = difflib.unified_diff(
            (old or "").splitlines(),
            (new or "").splitlines(),
            "/dev/null" if old is None else f"a/{path}",
            "/dev/null" if new is None else f"b/{path}",
            lineterm="",
        )
        text += f"diff --git a/{path} b/{path}\n" + "".join(f"{line}\n" for line in hunks)
    return text


SERVICE = """\
import subprocess


def total(values):
    return sum(values)


def run(command):
    return subprocess.run(command, check=False)
"""


class ConditionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "source root"
        self.root.mkdir()
        self.diff = Path(temporary.name) / "diff.patch"
        self.output = Path(temporary.name) / "untested.txt"

    def head(self, files: dict[str, str]) -> None:
        for path, text in files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))

    def run_condition(
        self,
        before: dict[str, str],
        after: dict[str, str],
        environment: dict[str, str] | None = None,
        extra: str = "",
    ) -> tuple[int, str, str]:
        """The condition's exit status, its stdout, and the file it wrote, or "", run as a review runs it on a head
        holding `after` with the diff from `before`, and `extra` after it."""
        shutil.rmtree(self.root)
        self.root.mkdir()
        self.output.unlink(missing_ok=True)
        self.head(after)
        self.diff.write_bytes((diff_of(before, after) + extra).encode("utf-8"))
        given = (
            {"CODE_REVIEW_DIFF_FILE": str(self.diff), "CODE_REVIEW_CONDITION_OUTPUT": str(self.output)}
            if environment is None
            else environment
        )
        clean = {key: value for key, value in os.environ.items() if not key.startswith("CODE_REVIEW_")}
        finished = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--source-root", str(self.root)],
            env={**clean, **given},
            capture_output=True,
            check=False,
            timeout=120,
        )
        written = self.output.read_bytes().decode("utf-8") if self.output.exists() else ""
        return finished.returncode, finished.stdout.decode("utf-8"), written

    def test_a_changed_function_no_test_names_is_listed_with_its_added_lines_and_hunk(self) -> None:
        after = {"app/service.py": SERVICE.replace("sum(values)", "sum(values) or 0")}
        code, out, written = self.run_condition({"app/service.py": SERVICE}, after)
        self.assertEqual((0, "UNTESTED 1 given=1\n"), (code, out))
        self.assertEqual(
            "UNTESTED app/service.py:4 function total, added lines 5, hunk @@ -2,7 +2,7 @@, tests of its file none\n"
            "@@ -2,7 +2,7 @@\n \n \n def total(values):\n-    return sum(values)\n+    return sum(values) or 0\n"
            " \n \n def run(command):\n",
            written,
        )

    def test_a_changed_function_a_test_names_through_its_module_closes_the_route(self) -> None:
        after = {
            "app/service.py": SERVICE.replace("sum(values)", "sum(values) or 0"),
            "tests/test_service.py": "import service\n\nassert service.total([]) == 0\n",
        }
        before = {"app/service.py": SERVICE, "tests/test_service.py": after["tests/test_service.py"]}
        self.assertEqual((1, "UNTESTED 0 given=0\n", ""), self.run_condition(before, after))
        # An import of the name counts as naming it.
        after["tests/test_service.py"] = "from app.service import total\n\nassert total([]) == 0\n"
        self.assertEqual((1, "UNTESTED 0 given=0\n", ""), self.run_condition(before, after))

    def test_a_test_only_change_closes_the_route(self) -> None:
        test = "import service\n\nassert service.total([1]) == 1\n"
        after = {"app/service.py": SERVICE, "tests/test_service.py": test + "assert service.total([]) == 0\n"}
        before = {"app/service.py": SERVICE, "tests/test_service.py": test}
        self.assertEqual((1, "UNTESTED 0 given=0\n", ""), self.run_condition(before, after))

    def test_a_converted_call_site_in_a_function_a_test_names_opens_the_route(self) -> None:
        # A conversion inside a named function must still reach the reviewer who checks its output is pinned.
        report = "import json\nimport json_writer\n\n\ndef save(path, data):\n"
        report += "    path.write_text(json.dumps(data, indent=2))\n    return path\n"
        converted = report.replace("path.write_text(json.dumps(data, indent=2))", "json_writer.write(path, data)")
        # The test names save but asserts nothing about the bytes it writes.
        test = {"tests/test_report.py": "import report\n\n\ndef test_save(path):\n    assert report.save(path, {})\n"}
        code, out, written = self.run_condition(
            {"tools/report.py": report, **test}, {"tools/report.py": converted, **test}
        )
        self.assertEqual((0, "UNTESTED 1 given=1\n"), (code, out))
        self.assertEqual(
            "CONVERTED tools/report.py:5 function save, a test names it, converted calls json_writer.write at line 6, "
            "hunk @@ -3,5 +3,5 @@, tests of its file tests/test_report.py\n"
            "@@ -3,5 +3,5 @@\n \n \n def save(path, data):\n-    path.write_text(json.dumps(data, indent=2))\n"
            "+    json_writer.write(path, data)\n     return path\n",
            written,
        )

    def test_a_conversion_beside_a_site_that_already_called_the_helper_opens_the_route(self) -> None:
        # The replaced block already calls json_writer.write at one site, so a set of callee names sees nothing new;
        # counted as call sites, the block's one call became two.
        report = "import json\nimport json_writer\n\n\ndef save(path, data):\n"
        report += (
            "    path.write_text(json.dumps(data, indent=2))\n    json_writer.write(path.with_suffix('.b'), data)\n"
        )
        report += "    return path\n"
        converted = report.replace("path.write_text(json.dumps(data, indent=2))", "json_writer.write(path, data)")
        converted = converted.replace("'.b'), data)", "'.b'), data, indent=4)")
        test = {"tests/test_report.py": "import report\n\n\ndef test_save(path):\n    assert report.save(path, {})\n"}
        code, out, written = self.run_condition(
            {"tools/report.py": report, **test}, {"tools/report.py": converted, **test}
        )
        self.assertEqual((0, "UNTESTED 1 given=1\n"), (code, out))
        # The diff does not pair the lines, so both sites of the grown callee are named.
        self.assertTrue(
            written.startswith(
                "CONVERTED tools/report.py:5 function save, a test names it, converted calls json_writer.write at "
                "line 6, json_writer.write at line 7, hunk @@ -3,6 +3,6 @@, tests of its file tests/test_report.py\n"
            ),
            written,
        )
        # Two sites on one line, where the replaced line made one, count as a conversion too.
        one_line = "import json_writer\n\n\ndef save(path, data):\n    json_writer.write(path, data); path.touch()\n"
        two_sites = one_line.replace("path.touch()", "json_writer.write(path, {})")
        code, out, written = self.run_condition(
            {"tools/report.py": one_line, **test}, {"tools/report.py": two_sites, **test}
        )
        self.assertEqual((0, "UNTESTED 1 given=1\n"), (code, out))
        self.assertIn("converted calls json_writer.write at line 5, hunk", written)

    def test_removing_one_site_of_the_helper_and_converting_another_opens_the_route(self) -> None:
        # The helper's count is unchanged, one site gone and one converted, so the pairing is ambiguous.
        test = {"tests/test_report.py": "import report\n\nassert report.save\n"}
        source = "import json\nimport json_writer\n\n\ndef save(first, second, a, b):\n"
        source += '    json_writer.write(first, a)\n    second.write_text(json.dumps(b), encoding="utf-8")\n'
        converted = "import json\nimport json_writer\n\n\ndef save(first, second, a, b):\n"
        converted += "    json_writer.write(second, b)\n"
        code, out, written = self.run_condition(
            {"tools/report.py": source, **test}, {"tools/report.py": converted, **test}
        )
        self.assertEqual((0, "UNTESTED 1 given=1\n"), (code, out))
        self.assertTrue(
            written.startswith(
                "CONVERTED tools/report.py:5 function save, a test names it, converted calls json_writer.write at "
                "line 6, hunk @@ -3,5 +3,4 @@, tests of its file tests/test_report.py\n"
            ),
            written,
        )
        # A removed site's text the replacing lines keep, beside another call dropped, is no converted site.
        moved = source.replace(
            '    json_writer.write(first, a)\n    second.write_text(json.dumps(b), encoding="utf-8")\n',
            "    second.touch()\n    json_writer.write(first, a)\n",
        )
        self.assertEqual(
            (1, "UNTESTED 0 given=0\n", ""),
            self.run_condition({"tools/report.py": source, **test}, {"tools/report.py": moved, **test}),
        )

    def test_reordering_or_rewriting_sites_that_already_called_the_helper_closes_the_route(self) -> None:
        test = {"tests/test_report.py": "import report\n\nassert report.save\n"}
        source = "import json_writer\n\n\ndef save(path, data):\n"
        source += "    json_writer.write(path, data)\n    json_writer.write(path.parent, data)\n    return path\n"
        swapped = source.replace(
            "    json_writer.write(path, data)\n    json_writer.write(path.parent, data)\n",
            "    json_writer.write(path.parent, data, indent=4)\n    json_writer.write(path, data, indent=4)\n",
        )
        self.assertEqual(
            (1, "UNTESTED 0 given=0\n", ""),
            self.run_condition({"tools/report.py": source, **test}, {"tools/report.py": swapped, **test}),
        )

    def test_a_converted_method_names_each_call_its_class_and_every_test_of_its_file(self) -> None:
        store = (
            "from json_writer import write\nfrom records import Record\n\n\nclass Store:\n"
            "    def save(self, path, data):\n        self.log(path)\n        path.write_text(str(data))\n"
        )
        # A line inserted above, which replaces nothing, does not end the search for the converted line below it.
        converted = store.replace("        self.log(path)\n", "        self.count = 1\n        self.log(path)\n")
        converted = converted.replace("path.write_text(str(data))", "write(path, Record(data))")
        tests = {
            "tests/test_store.py": "from store import Store\n\nStore().save\n",
            "tests/test_store_more.py": "from store import Store\n\nStore().save\n",
        }
        code, out, written = self.run_condition({"app/store.py": store, **tests}, {"app/store.py": converted, **tests})
        self.assertEqual((0, "UNTESTED 1 given=1\n"), (code, out))
        self.assertTrue(
            written.startswith(
                "CONVERTED app/store.py:6 method Store.save, a test names it, converted calls Record at line 9, "
                "write at line 9, hunk @@ -4,5 +4,6 @@, tests of its file tests/test_store.py, "
                "tests/test_store_more.py\n"
            ),
            written,
        )

    def test_a_call_through_a_dotted_import_is_found_by_the_name_the_import_binds(self) -> None:
        source = "import xml.etree.ElementTree\n\n\ndef load(text):\n    return parse(text)\n"
        converted = source.replace("parse(text)", "xml.etree.ElementTree.fromstring(text)")
        test = {"tests/test_loader.py": "import loader\n\nloader.load\n"}
        code, out, written = self.run_condition({"app/loader.py": source, **test}, {"app/loader.py": converted, **test})
        self.assertEqual((0, "UNTESTED 1 given=1\n"), (code, out))
        self.assertTrue(
            written.startswith(
                "CONVERTED app/loader.py:4 function load, a test names it, converted calls "
                "xml.etree.ElementTree.fromstring at line 5, "
            ),
            written,
        )

    def test_a_named_function_s_change_that_converts_no_call_site_still_closes_the_route(self) -> None:
        test = {"tests/test_report.py": "import report\n\nassert report.save\n"}
        source = "import json_writer\n\n\ndef save(path, data):\n    json_writer.write(path, data)\n    return path\n"
        cases = {
            # The replaced line already made the call.
            "same call": source.replace("(path, data)\n    return", "(path, data, indent=4)\n    return"),
            # A call through a name no import binds is the module's own.
            "own call": source.replace("json_writer.write(path, data)", "helper(path, data)"),
            # An added line that replaces nothing is new code, not a converted site.
            "inserted": source.replace("    return path", "    json_writer.flush(path)\n    return path"),
        }
        # The replaced block already made the call, on a continuation line that starts at the first column.
        continued = "import json_writer\n\n\ndef save(path, data):\n    value = (\njson_writer.write(path, data))\n"
        continued += "    return value\n"
        rewritten = continued.replace("value", "result").replace("data))", "data, indent=4))")
        for name, before, after in [
            *((name, source, after) for name, after in cases.items()),
            ("continued", continued, rewritten),
        ]:
            with self.subTest(name=name):
                self.assertEqual(
                    (1, "UNTESTED 0 given=0\n", ""),
                    self.run_condition({"tools/report.py": before, **test}, {"tools/report.py": after, **test}),
                )

    def test_a_removed_function_needs_no_test_and_lines_removed_inside_one_change_it(self) -> None:
        before = {"app/service.py": SERVICE}
        removed = SERVICE.replace("\n\ndef run(command):\n    return subprocess.run(command, check=False)\n", "\n")
        self.assertEqual((1, "UNTESTED 0 given=0\n", ""), self.run_condition(before, {"app/service.py": removed}))
        body = "def total(values):\n    values = list(values)\n    return sum(values)\n"
        trimmed = "def total(values):\n    return sum(values)\n"
        for name, old, new in (("inside", body, trimmed), ("at the end", trimmed, "def total(values):\n    pass\n")):
            with self.subTest(name=name):
                code, out, written = self.run_condition({"app/total.py": old}, {"app/total.py": new})
                self.assertEqual((0, "UNTESTED 1 given=1\n"), (code, out))
                if name == "inside":
                    self.assertTrue(
                        written.startswith(
                            "UNTESTED app/total.py:1 function total, added lines none (it only removes lines), "
                        ),
                        written,
                    )

    def test_a_renamed_function_is_the_new_name_and_a_test_of_the_old_name_does_not_cover_it(self) -> None:
        test = "import service\n\nassert service.total([]) == 0\n"
        before = {"app/service.py": SERVICE, "tests/test_service.py": test}
        after = {"app/service.py": SERVICE.replace("def total(", "def summed("), "tests/test_service.py": test}
        code, out, written = self.run_condition(before, after)
        self.assertEqual((0, "UNTESTED 1 given=1\n"), (code, out))
        self.assertTrue(written.startswith("UNTESTED app/service.py:4 function summed, added lines 4, "), written)
        self.assertIn(", tests of its file tests/test_service.py\n", written)

    def test_a_common_name_counts_only_as_its_own_module_s_and_never_in_a_string_or_comment(self) -> None:
        before = {"app/service.py": SERVICE}
        after = {"app/service.py": SERVICE.replace("check=False", "check=True")}
        tests = {
            # Another module's run, and this module's run named only in a string and a comment.
            "tests/test_other.py": "import subprocess\nimport other\n\nsubprocess.run(['x'])\nother.run()\n",
            "tests/test_service.py": (
                "import subprocess\n\nimport service  # service.run is covered elsewhere\n\n"
                "subprocess.run(['x'])\nassert service.total([]) == 0, 'service.run'\n"
            ),
        }
        code, out, written = self.run_condition({**before, **tests}, {**after, **tests})
        self.assertEqual((0, "UNTESTED 1 given=1\n"), (code, out))
        self.assertTrue(written.startswith("UNTESTED app/service.py:8 function run, added lines 9, "), written)
        tests["tests/test_service.py"] += "service.run(['y'])\n"
        self.assertEqual((1, "UNTESTED 0 given=0\n", ""), self.run_condition({**before, **tests}, {**after, **tests}))

    def test_a_function_named_only_by_a_test_the_pull_request_deletes_is_listed(self) -> None:
        test = "import service\n\nservice.run(['x'])\n"
        before = {"app/service.py": SERVICE, "tests/test_service.py": test}
        after = {"app/service.py": SERVICE.replace("check=False", "check=True")}
        code, out, written = self.run_condition(before, after)
        self.assertEqual((0, "UNTESTED 1 given=1\n"), (code, out))
        self.assertIn("function run, added lines 9, hunk @@ -6,4 +6,4 @@, tests of its file none\n", written)

    def test_a_change_inside_a_method_is_the_method_s_and_a_test_of_its_class_names_it(self) -> None:
        source = (
            "from dataclasses import dataclass\n\n\n@dataclass\nclass Point:\n    x: int\n\n\n"
            "class Shape:\n    sides = 3\n\n    def __init__(self):\n        self.n = 1\n\n"
            "    def area(self):\n        return 1\n"
        )
        changed = source.replace("x: int", "x: int = 0").replace("self.n = 1", "self.n = 2")
        changed = changed.replace("return 1", "return 2")
        code, out, written = self.run_condition({"app/shape.py": source}, {"app/shape.py": changed})
        # A class that only declares fields has nothing of its own to test.
        self.assertEqual((0, "UNTESTED 2 given=2\n"), (code, out))
        self.assertIn("UNTESTED app/shape.py:12 method Shape.__init__, added lines 13, ", written)
        self.assertIn("UNTESTED app/shape.py:15 method Shape.area, added lines 16, ", written)
        test = {"tests/test_shape.py": "from shape import Shape\n\nassert Shape().area() == 2\n"}
        self.assertEqual(
            (1, "UNTESTED 0 given=0\n", ""),
            self.run_condition({"app/shape.py": source, **test}, {"app/shape.py": changed, **test}),
        )

    def test_a_script_s_main_is_named_by_a_test_that_runs_the_script(self) -> None:
        source = "def main():\n    return 0\n"
        before, after = {"tools/tool.py": source}, {"tools/tool.py": source.replace("0", "1")}
        # A comment, or another script whose name ends the same, does not run it.
        others = {"tests/tools/test_tool.py": "# tools/tool.py\nSCRIPT = 'tools/mytool.py'\n"}
        self.assertEqual((0, "UNTESTED 1 given=1\n"), self.run_condition({**before, **others}, {**after, **others})[:2])
        test = {"tests/tools/test_tool.py": "SCRIPT = 'tools/tool.py'  # run as a program\n"}
        self.assertEqual((1, "UNTESTED 0 given=0\n", ""), self.run_condition({**before, **test}, {**after, **test}))

    def test_shell_functions_are_named_by_their_file_and_a_whole_word_a_powershell_one_ignoring_case(self) -> None:
        bash = "#!/usr/bin/env bash\ndays_old() {\n  echo 1\n}\n"
        powershell = "function Get-ReportAge {\n    param($Path)\n    1\n}\n"
        before = {"tools/report.sh": bash, "tools/Report.ps1": powershell}
        after = {"tools/report.sh": bash.replace("echo 1", "echo 2"), "tools/Report.ps1": powershell.replace("1", "2")}
        code, out, written = self.run_condition(before, after)
        self.assertEqual((0, "UNTESTED 2 given=2\n"), (code, out))
        self.assertIn("UNTESTED tools/Report.ps1:1 function Get-ReportAge, added lines 3, ", written)
        self.assertIn("UNTESTED tools/report.sh:2 function days_old, added lines 3, ", written)
        tests = {
            # A comment names neither; a Python test calls a Bash function from a string.
            "tests/test_report.py": "# days_old\nRUN = ['bash', '-c', 'source tools/report.sh; days_old x']\n",
            "tests/test-report.ps1": "# Get-ReportAge\n. ./tools/report.ps1\nget-reportage -Path x\n",
        }
        self.assertEqual((1, "UNTESTED 0 given=0\n", ""), self.run_condition({**before, **tests}, {**after, **tests}))
        tests["tests/test_report.py"] = "# days_old, from report.sh\nRUN = ['bash', 'tools/report.sh']\n"
        code, out, written = self.run_condition({**before, **tests}, {**after, **tests})
        self.assertEqual((0, "UNTESTED 1 given=1\n"), (code, out))
        self.assertIn(
            "function days_old, added lines 3, hunk @@ -1,4 +1,4 @@, tests of its file tests/test_report.py\n", written
        )

    def test_fixtures_are_neither_code_to_test_nor_tests(self) -> None:
        fixture = "tests/fixtures/skill-evals/x/head"
        before = {f"{fixture}/app/service.py": SERVICE, "app/service.py": SERVICE}
        after = {
            f"{fixture}/app/service.py": SERVICE.replace("check=False", "check=True"),
            "app/service.py": SERVICE.replace("check=False", "check=True"),
            f"{fixture}/tests/test_service.py": "import service\n\nservice.run(['x'])\n",
        }
        code, out, written = self.run_condition(before, after)
        self.assertEqual((0, "UNTESTED 1 given=1\n"), (code, out))
        self.assertTrue(written.startswith("UNTESTED app/service.py:8 function run, "), written)
        self.assertIn(", tests of its file none\n", written)

    def test_a_python_file_ast_cannot_read_is_listed_as_unparsed(self) -> None:
        code, out, written = self.run_condition({"app/broken.py": "x = 1\n"}, {"app/broken.py": "def x(:\n"})
        self.assertEqual((0, "UNTESTED 1 given=1\n"), (code, out))
        self.assertTrue(written.startswith("UNPARSED app/broken.py: SyntaxError: "), written)

    def test_python_nested_past_the_parsers_stack_is_unparsed_or_unreadable_rather_than_a_failure(self) -> None:
        # ast raises MemoryError for a unary chain this long, which the condition reads as source it cannot parse.
        nested = "x = " + "-" * 100_000 + "1\n"
        code, out, written = self.run_condition({"app/deep.py": "x = 1\n"}, {"app/deep.py": nested})
        self.assertEqual((0, "UNTESTED 1 given=1\n"), (code, out))
        self.assertEqual(
            "UNPARSED app/deep.py: SyntaxError: the source nests deeper than the parser reads (MemoryError)\n", written
        )
        service = {"app/service.py": SERVICE}
        changed = {"app/service.py": SERVICE.replace("sum(values)", "sum(values) or 0")}
        deep_test = {"tests/test_service.py": "import service\n\n" + nested}
        code, out, written = self.run_condition({**service, **deep_test}, {**changed, **deep_test})
        self.assertEqual(0, code)
        self.assertTrue(
            out.startswith(
                'UNREADABLE "tests/test_service.py": SyntaxError: the source nests deeper than the parser reads'
            ),
            out,
        )
        self.assertTrue(written.startswith("UNTESTED app/service.py:4 function total, "), written)

    def test_a_test_ast_or_tokenize_cannot_read_names_nothing_and_is_reported_by_name(self) -> None:
        tool, report = "def main():\n    return 0\n", "#!/usr/bin/env bash\ndays_old() {\n  echo 1\n}\n"
        before = {"tools/tool.py": tool, "tools/report.sh": report}
        after = {"tools/tool.py": tool.replace("0", "1"), "tools/report.sh": report.replace("echo 1", "echo 2")}
        # tokenize cannot finish an unterminated string, so the comment is all that names the script and the function.
        tests = {"tests/test_tool.py": '# runs tools/tool.py and tools/report.sh: days_old\nx = """\n'}
        code, out, written = self.run_condition({**before, **tests}, {**after, **tests})
        self.assertEqual(0, code)
        lines = out.splitlines()
        self.assertEqual(2, len(lines), out)
        self.assertTrue(lines[0].startswith('UNREADABLE "tests/test_tool.py": TokenError: '), out)
        self.assertEqual("UNTESTED 2 given=2", lines[1])
        self.assertIn("UNTESTED tools/tool.py:1 function main, ", written)
        self.assertIn("UNTESTED tools/report.sh:2 function days_old, ", written)
        # A test ast cannot read names nothing through its imports either, and says so.
        service = {"app/service.py": SERVICE}
        changed = {"app/service.py": SERVICE.replace("sum(values)", "sum(values) or 0")}
        broken = {"tests/test_service.py": "import service\n\nassert service.total([]) == 0\ndef x(:\n"}
        code, out, written = self.run_condition({**service, **broken}, {**changed, **broken})
        self.assertEqual(0, code)
        self.assertTrue(out.startswith('UNREADABLE "tests/test_service.py": SyntaxError: '), out)
        self.assertTrue(out.endswith("\nUNTESTED 1 given=1\n"), out)
        self.assertTrue(written.startswith("UNTESTED app/service.py:4 function total, "), written)

    def test_the_entries_are_bounded_and_the_rest_named(self) -> None:
        # Twenty entries at most, each hunk cut to 900 characters, so the file stays under the 32 KiB a review gives a
        # specialist even when every entry's header and hunk are long.
        name = "a_function_with_a_long_name_" + "x" * 40
        body = "".join(f"    value_{line} = {'y' * 60}\n" for line in range(30))
        before = {f"app/module_{n:02}.py": f"def {name}():\n{body}" for n in range(25)}
        after = {path: text.replace("value_", "changed_") for path, text in before.items()}
        code, out, written = self.run_condition(before, after)
        self.assertEqual((0, "UNTESTED 25 given=20\n"), (code, out))
        self.assertEqual(20, written.count("\nUNTESTED ") + written.startswith("UNTESTED "))
        self.assertTrue(written.endswith("\nOMITTED 5 more entries past the first 20\n"), written[-200:])
        self.assertLess(len(written.encode("utf-8")), 32 * 1024)
        self.assertIn("y…\n", written)

    def test_a_header_is_read_only_before_a_file_s_first_hunk_and_a_quoted_one_is_decoded(self) -> None:
        # Added lines that begin `++ ` read `+++ ` in the diff, as a removed `-- ` line reads `--- `: content, which
        # names no file and holds no quoting to decode, however it is written.
        text = 'TEXT = """\n++ "oops\n++ b/app/other.py\n"""\n' + SERVICE.replace("sum(values)", "sum(values) or 0")
        name, quoted = "app/résumé.py", "app/r\\303\\251sum\\303\\251.py"
        diff = diff_of({name: '"""\n-- "gone\n"""\n' + SERVICE}, {name: text})
        diff = diff.replace(f"--- a/{name}\n", f'--- "a/{quoted}"\n').replace(f"+++ b/{name}\n", f'+++ "b/{quoted}"\n')
        self.assertIn('\n+++ "oops\n+++ b/app/other.py\n', diff)
        self.assertIn('\n--- "gone\n', diff)
        code, out, written = self.run_condition({name: text}, {name: text}, extra=diff)
        self.assertEqual((0, "UNTESTED 1 given=1\n"), (code, out))
        self.assertTrue(written.startswith(f"UNTESTED {name}:8 function total, added lines 9, hunk "), written)

    def test_a_changed_path_that_could_name_a_file_outside_the_snapshot_is_refused_and_never_read(self) -> None:
        outside = self.root.parent / "outside.py"
        outside.write_text("def private_name_xyz():\n    pass\n", encoding="utf-8")
        safe = {"app/service.py": SERVICE.replace("sum(values)", "sum(values) or 0")}
        # This platform's absolute path to the file, in its own form and with forward slashes: a drive and
        # backslashes on Windows, a root elsewhere.
        for path in (
            "tools\\..\\..\\outside.py",
            "../outside.py",
            "tools/../../outside.py",
            str(outside.resolve()),
            outside.resolve().as_posix(),
        ):
            quoted = '"b/' + path.replace("\\", "\\\\") + '"' if "\\" in path else f"b/{path}"
            section = (
                f"diff --git a/x b/x\nnew file mode 100644\n--- /dev/null\n+++ {quoted}\n"
                "@@ -0,0 +1,2 @@\n+def something_else():\n+    pass\n"
            )
            with self.subTest(path=path):
                code, out, written = self.run_condition({"app/service.py": SERVICE}, safe, extra=section)
                self.assertEqual(
                    (
                        0,
                        f"REFUSED {json.dumps(path)}: a path that could name a file outside the snapshot\n"
                        "UNTESTED 1 given=1\n",
                    ),
                    (code, out),
                )
                self.assertTrue(written.startswith("UNTESTED app/service.py:4 function total"), written)
                self.assertNotIn("private_name_xyz", written)
                # Alone, it closes the route and writes nothing.
                code, out, written = self.run_condition({}, {}, extra=section)
                self.assertEqual((1, "UNTESTED 0 given=0\n", ""), (code, out.splitlines()[-1] + "\n", written))

    def test_a_snapshot_it_cannot_read_fails_the_review_rather_than_closing_the_route(self) -> None:
        before = {"app/service.py": SERVICE}
        after = {"app/service.py": SERVICE.replace("check=False", "check=True")}
        # The diff leaves app/service.py at the head, but the snapshot does not hold it.
        diff = diff_of(before, after)
        self.diff.write_bytes(diff.encode("utf-8"))
        environment = {"CODE_REVIEW_DIFF_FILE": str(self.diff), "CODE_REVIEW_CONDITION_OUTPUT": str(self.output)}
        clean = {key: value for key, value in os.environ.items() if not key.startswith("CODE_REVIEW_")}
        for root, reason in (
            (self.root, 'the snapshot holds no "app/service.py", which the diff leaves at the head'),
            (self.root / "absent", f"the snapshot {json.dumps((self.root / 'absent').as_posix())} is not a directory"),
        ):
            with self.subTest(reason=reason):
                finished = subprocess.run(
                    [sys.executable, "-B", str(SCRIPT), "--source-root", str(root)],
                    env={**clean, **environment},
                    capture_output=True,
                    check=False,
                    timeout=120,
                )
                self.assertEqual(
                    (2, f"FAILED MissingEvidence: {reason}\n"), (finished.returncode, finished.stdout.decode("utf-8"))
                )
                self.assertFalse(self.output.exists())

    def test_a_changed_script_the_snapshot_records_as_excluded_opens_the_route_for_review_from_the_diff(self) -> None:
        before = {"app/service.py": SERVICE, "app/other.py": SERVICE}
        after = {
            "app/service.py": SERVICE.replace("check=False", "check=True"),
            "app/other.py": SERVICE.replace("sum(values)", "sum(values) or 0"),
        }
        self.diff.write_bytes(diff_of(before, after).encode("utf-8"))
        self.head({"app/other.py": after["app/other.py"]})
        manifest = {"excluded_paths": {"app/service.py": "file-size-limit"}}
        (self.root / "source-snapshot.json").write_bytes(json.dumps(manifest).encode("utf-8"))
        environment = {"CODE_REVIEW_DIFF_FILE": str(self.diff), "CODE_REVIEW_CONDITION_OUTPUT": str(self.output)}
        clean = {key: value for key, value in os.environ.items() if not key.startswith("CODE_REVIEW_")}
        finished = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--source-root", str(self.root)],
            env={**clean, **environment},
            capture_output=True,
            check=False,
            timeout=120,
        )
        self.assertEqual((0, "UNTESTED 2 given=2\n"), (finished.returncode, finished.stdout.decode("utf-8")))
        written = self.output.read_bytes().decode("utf-8")
        self.assertTrue(
            written.startswith(
                "EXCLUDED app/service.py: the snapshot leaves it out (file-size-limit), so no definition in it was "
                "read; review its hunks from the diff, hunks @@ -6,4 +6,4 @@\n@@ -6,4 +6,4 @@\n"
            ),
            written,
        )
        self.assertIn("\n\nUNTESTED app/other.py:4 function total, added lines 5, ", written)
        # Recorded for another path, the exclusion excuses nothing: a file the snapshot lacks is still missing.
        manifest = {"excluded_paths": {"app/elsewhere.py": "file-size-limit"}}
        (self.root / "source-snapshot.json").write_bytes(json.dumps(manifest).encode("utf-8"))
        self.output.unlink()
        finished = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--source-root", str(self.root)],
            env={**clean, **environment},
            capture_output=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(
            (2, 'FAILED MissingEvidence: the snapshot holds no "app/service.py", which the diff leaves at the head\n'),
            (finished.returncode, finished.stdout.decode("utf-8")),
        )
        self.assertFalse(self.output.exists())

    def test_an_excluded_script_s_entry_names_every_hunk_and_gives_the_first(self) -> None:
        before = {"app/long.py": "".join(f"VALUE_{index} = {index}\n" for index in range(30))}
        after = {"app/long.py": before["app/long.py"].replace("VALUE_1 = 1", "VALUE_1 = 10").replace("= 25", "= 250")}
        self.diff.write_bytes(diff_of(before, after).encode("utf-8"))
        manifest = {"excluded_paths": {"app/long.py": "agent-instruction"}}
        (self.root / "source-snapshot.json").write_bytes(json.dumps(manifest).encode("utf-8"))
        environment = {"CODE_REVIEW_DIFF_FILE": str(self.diff), "CODE_REVIEW_CONDITION_OUTPUT": str(self.output)}
        clean = {key: value for key, value in os.environ.items() if not key.startswith("CODE_REVIEW_")}
        finished = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--source-root", str(self.root)],
            env={**clean, **environment},
            capture_output=True,
            check=False,
            timeout=120,
        )
        self.assertEqual((0, "UNTESTED 1 given=1\n"), (finished.returncode, finished.stdout.decode("utf-8")))
        self.assertEqual(
            "EXCLUDED app/long.py: the snapshot leaves it out (agent-instruction), so no definition in it was read; "
            "review its hunks from the diff, hunks @@ -1,5 +1,5 @@, @@ -23,7 +23,7 @@\n"
            "@@ -1,5 +1,5 @@\n VALUE_0 = 0\n-VALUE_1 = 1\n+VALUE_1 = 10\n VALUE_2 = 2\n VALUE_3 = 3\n VALUE_4 = 4\n",
            self.output.read_bytes().decode("utf-8"),
        )

    def test_a_changed_script_the_snapshot_holds_is_read_even_when_its_manifest_records_it_excluded(self) -> None:
        manifest = json.dumps({"excluded_paths": {"app/service.py": "agent-instruction"}})
        before = {"app/service.py": SERVICE, "source-snapshot.json": manifest}
        after = {"app/service.py": SERVICE.replace("sum(values)", "sum(values) or 0"), "source-snapshot.json": manifest}
        code, out, written = self.run_condition(before, after)
        self.assertEqual((0, "UNTESTED 1 given=1\n"), (code, out))
        self.assertTrue(written.startswith("UNTESTED app/service.py:4 function total, added lines 5, "), written)

    def test_a_condition_run_without_its_inputs_fails_the_review(self) -> None:
        self.assertEqual(
            (
                2,
                "FAILED CODE_REVIEW_DIFF_FILE and CODE_REVIEW_CONDITION_OUTPUT not set; a review sets them for a "
                "condition\n",
                "",
            ),
            self.run_condition({}, {}, environment={}),
        )

    def test_an_unexpected_failure_fails_the_review_rather_than_closing_the_route(self) -> None:
        missing = {"CODE_REVIEW_DIFF_FILE": str(self.root / "absent.patch"), "CODE_REVIEW_CONDITION_OUTPUT": "x"}
        code, out, _ = self.run_condition({}, {}, environment=missing)
        self.assertEqual(2, code)
        self.assertTrue(out.startswith("FAILED FileNotFoundError: "), out)
