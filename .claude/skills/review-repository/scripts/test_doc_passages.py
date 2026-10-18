from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import doc_passages

SCRIPT = Path(__file__).resolve().parent / "doc_passages.py"


def changed(path: str, *hunks: tuple[str, list[str]]) -> str:
    """A diff of one changed file, each hunk its header's context and its lines with their markers."""
    text = f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n--- a/{path}\n+++ b/{path}\n"
    for number, (context, lines) in enumerate(hunks):
        start = 10 * number + 1
        added = sum(1 for line in lines if not line.startswith("-"))
        removed = sum(1 for line in lines if not line.startswith("+"))
        text += f"@@ -{start},{removed} +{start},{added} @@{' ' + context if context else ''}\n"
        text += "".join(f"{line}\n" for line in lines)
    return text


class UnitTests(unittest.TestCase):
    def test_a_document_splits_into_headings_rows_items_paragraphs_and_fenced_blocks(self) -> None:
        text = (
            "# Title\n\nA paragraph\nthat wraps.\n\n- An item\n  that wraps.\n- Another item.\n\n"
            "| A | B |\n| --- | :-: |\n| one | two |\n\n```bash\nrun it\n```\n"
        )
        self.assertEqual(
            [
                (1, "# Title", False),
                (3, "A paragraph\nthat wraps.", False),
                (6, "- An item\n  that wraps.", False),
                (8, "- Another item.", False),
                (10, "| A | B |", False),
                (12, "| one | two |", False),
                (15, "run it", True),
            ],
            [(unit.line, unit.text, unit.code) for unit in doc_passages.units(text)],
        )


class ConditionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "source root"
        self.root.mkdir()
        self.diff = Path(temporary.name) / "diff.patch"
        self.output = Path(temporary.name) / "passages.txt"

    def names(self, diff: str) -> list[doc_passages.Name]:
        """The names the condition looks for in a diff whose paths all stay in the snapshot."""
        files, refused = doc_passages.read_files(diff, self.root)
        self.assertEqual([], refused)
        return doc_passages.names_to_find(files)

    def document(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))

    def run_condition(self, diff: str, environment: dict[str, str] | None = None) -> tuple[int, str, str]:
        """The condition's exit status and stdout, run as a review runs it, and the file it wrote, or ""."""
        self.diff.write_bytes(diff.encode("utf-8"))
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

    def test_a_changed_script_a_guide_names_gives_the_passage_and_its_hunk(self) -> None:
        self.document(
            "docs/guide.md",
            "# Guide\n\nAn introduction.\n\nAfter a change, run `python -B tools/skill_evals.py --write`\n"
            "and commit what it wrote.\n",
        )
        diff = changed("tools/skill_evals.py", ("", [" x = 1", "-y = 2", "+y = 3"]))
        self.assertEqual(
            (
                0,
                "PASSAGES 1 given=1\n",
                "PASSAGE docs/guide.md:5 names skill_evals.py (script in tools/skill_evals.py), hunk @@ -1,2 +1,2 @@ "
                "at new lines 1-2\n"
                "After a change, run `python -B tools/skill_evals.py --write`\nand commit what it wrote.\n",
            ),
            self.run_condition(diff),
        )

    def test_a_changed_function_the_contract_names_gives_its_table_row(self) -> None:
        self.document(
            "docs/code-review-operations-contract.md",
            "| Field | Meaning |\n| --- | --- |\n"
            "| `when` | Run by `evaluate_condition`, which kills it at a limit. |\n",
        )
        self.document("README.md", "# Readme\n\nNothing about it.\n")
        diff = changed(
            "skills/code-review-core/scripts/review_specialists.py",
            (
                "def evaluate_condition(reviewer_root: Path) -> bool:",
                ["     path = 1", "-    return True", "+    return 0"],
            ),
        )
        code, out, written = self.run_condition(diff)
        self.assertEqual((0, "PASSAGES 1 given=1\n"), (code, out))
        self.assertEqual(
            "PASSAGE docs/code-review-operations-contract.md:3 names evaluate_condition (function in "
            "skills/code-review-core/scripts/review_specialists.py), hunk @@ -1,2 +1,2 @@ at new lines 1-2\n"
            "| `when` | Run by `evaluate_condition`, which kills it at a limit. |\n",
            written,
        )

    def test_a_definition_a_hunk_adds_or_removes_and_a_fenced_command_count(self) -> None:
        self.document("skills/demo/SKILL.md", "# Demo\n\n```bash\npython -B scripts/sweep.py --dry-run\n```\n")
        self.document("CLAUDE.md", "Call `sweep.Sweeper` to plan it.\n")
        diff = changed("skills/demo/scripts/sweep.py", ("", ["+class Sweeper:", "+    pass"]))
        code, out, written = self.run_condition(diff)
        self.assertEqual((0, "PASSAGES 2 given=2\n"), (code, out))
        self.assertIn("PASSAGE CLAUDE.md:1 names Sweeper (class in skills/demo/scripts/sweep.py)", written)
        self.assertIn("PASSAGE skills/demo/SKILL.md:4 names sweep.py (script in skills/demo/scripts/sweep.py)", written)

    def test_every_document_an_agent_or_a_user_acts_on_is_searched(self) -> None:
        # The top-level Markdown beside the README, the agent definitions, and the references a skill or a repository
        # skill reads, at any depth below them, each name scripts as the guides do.
        searched = [
            "SECURITY.md",
            "AGENTS.md",
            "agents/helper.md",
            "skills/demo/references/policy.md",
            "skills/demo/references/nested/policy.md",
            ".claude/skills/reviewer/references/brief.md",
        ]
        for path in searched:
            self.document(path, "# Notes\n\nRun `python -B tools/sweep.py` daily.\n")
        self.document("skills/demo/notes.md", "Run `python -B tools/sweep.py` daily.\n")
        diff = changed("tools/sweep.py", ("", ["-DAYS = 30", "+DAYS = 7"]))
        code, out, written = self.run_condition(diff)
        self.assertEqual((0, f"PASSAGES {len(searched)} given={len(searched)}\n"), (code, out))
        self.assertEqual(
            sorted(f"{path}:3" for path in searched),
            sorted(line.split(" ", 2)[1] for line in written.splitlines() if line.startswith("PASSAGE ")),
        )

    def test_a_change_nothing_documents_closes_the_route_and_writes_nothing(self) -> None:
        self.document("docs/guide.md", "Run `python -B tools/other.py` first.\n")
        # A release's notes record what it shipped, so a name there is history, not a passage to keep current.
        self.document("docs/upgrade-notes.md", "Changed `tools/skill_evals.py`.\n")
        diff = changed("tools/skill_evals.py", ("def judge(record):", ["-    return 1", "+    return 2"]))
        self.assertEqual((1, "PASSAGES 0 given=0\n", ""), self.run_condition(diff))

    def test_a_name_that_is_also_a_common_word_matches_only_as_code(self) -> None:
        self.document(
            "docs/guide.md",
            "The hub stays on `main`, and you `run` it.\n\nRun the check before pushing: `python deploy.py check`.\n\n"
            "The service and service.py are described in prose only.\n",
        )
        diff = changed(
            "app/service.py",
            ("def main() -> int:", ["-    return 1", "+    return 0"]),
            ("", ["+def run():", "+    pass", "+def check():", "+    pass"]),
        )
        self.assertEqual((1, "PASSAGES 0 given=0\n", ""), self.run_condition(diff))
        self.document("docs/guide.md", "Its `check()` does it.\n")
        code, out, written = self.run_condition(diff)
        self.assertEqual((0, "PASSAGES 1 given=1\n"), (code, out))
        self.assertTrue(written.startswith("PASSAGE docs/guide.md:1 names check (function in app/service.py)"))

    def test_the_passages_are_bounded_and_the_rest_named(self) -> None:
        # Twenty passages at most, each cut to 1,200 characters around its name, so the file stays under the 32 KiB a
        # review gives a specialist.
        long = "x" * 3000
        for number in range(25):
            self.document(f"docs/guide-{number:02}.md", f"{long} `tools/skill_evals.py` {long}\n")
        code, out, written = self.run_condition(changed("tools/skill_evals.py", ("", ["+y = 3"])))
        self.assertEqual((0, "PASSAGES 25 given=20\n"), (code, out))
        self.assertEqual(20, written.count("\nPASSAGE ") + written.startswith("PASSAGE "))
        self.assertTrue(written.endswith("\nOMITTED 5 more passages past the first 20\n"), written[-200:])
        self.assertLess(len(written.encode("utf-8")), 32 * 1024)
        self.assertIn("…" + "x" * 10, written)

    def test_passages_are_taken_in_turn_from_each_name_the_rarest_first(self) -> None:
        # A script every command example names would fill the bound before the one passage a changed function has.
        self.document("docs/a.md", "".join(f"Line {n} runs `tools/skill_evals.py`.\n\n" for n in range(30)))
        self.document("docs/b.md", "Then `judge_record` reads it.\n")
        diff = changed("tools/skill_evals.py", ("def judge_record(record):", ["-    return 1", "+    return 2"]))
        code, out, written = self.run_condition(diff)
        self.assertEqual((0, "PASSAGES 31 given=20\n"), (code, out))
        self.assertTrue(
            written.startswith("PASSAGE docs/b.md:1 names judge_record (function in tools/skill_evals.py)"), written
        )

    def test_a_test_file_s_names_are_left_to_the_validation_that_holds_them(self) -> None:
        self.document("docs/contract.md", "Held by `test_specialists.py::test_route`, `test_route`.\n")
        diff = changed("tests/test_specialists.py", ("", ["+def test_route():", "+    pass"]))
        self.assertEqual((1, "PASSAGES 0 given=0\n", ""), self.run_condition(diff))

    def test_every_suite_name_validation_runs_is_left_out_and_another_script_is_not(self) -> None:
        for path in (
            "tests/test_run.sh",
            "tests/test-run.sh",
            "skills/x/scripts/check_test.py",
            "skills/x/scripts/check-test.ps1",
            "skills/x/scripts/a.test.sh",
        ):
            with self.subTest(path=path):
                self.assertEqual([], self.names(changed(path, ("", ["+def sweep_all():"]))))
        self.assertEqual(["contest.sh"], [name.text for name in self.names(changed("tools/contest.sh"))])

    def test_a_header_s_tab_and_quoting_are_not_part_of_the_path(self) -> None:
        diff = (
            "diff --git a/tools/report age.py b/tools/report age.py\n--- a/tools/report age.py\t\n"
            '+++ b/tools/report age.py\t\n@@ -1 +1 @@\n-DAYS = 30\n+DAYS = 7\ndiff --git "a/tools/r\\303\\251sum'
            '\\303\\251.sh" "b/tools/r\\303\\251sum\\303\\251.sh"\n--- "a/tools/r\\303\\251sum\\303\\251.sh"\n'
            '+++ "b/tools/r\\303\\251sum\\303\\251.sh"\n@@ -1 +1 @@\n-x=1\n+x=2\n'
        )
        self.assertEqual(
            [("report age.py", "script"), ("résumé.sh", "script"), ("report age", "module")],
            [(name.text, name.kind) for name in self.names(diff)],
        )

    def test_a_pure_rename_looks_for_the_old_names_and_the_new(self) -> None:
        self.document("docs/guide.md", "Run `python -B tools/old_name.py` first.\n")
        diff = (
            "diff --git a/tools/old_name.py b/tools/new_name.py\nsimilarity index 100%\n"
            "rename from tools/old_name.py\nrename to tools/new_name.py\n"
        )
        self.assertEqual(
            [
                ("new_name.py", "script", "tools/new_name.py", None),
                ("old_name.py", "script", "tools/new_name.py", "tools/old_name.py"),
                ("new_name", "module", "tools/new_name.py", None),
                ("old_name", "module", "tools/new_name.py", "tools/old_name.py"),
            ],
            [(name.text, name.kind, name.hunk.path, name.renamed_from) for name in self.names(diff)],
        )
        self.assertEqual(
            (
                0,
                "PASSAGES 1 given=1\n",
                "PASSAGE docs/guide.md:1 names old_name.py (script in tools/new_name.py, renamed from "
                "tools/old_name.py), hunk (no hunk) at new lines 0-0\nRun `python -B tools/old_name.py` first.\n",
            ),
            self.run_condition(diff),
        )

    def test_a_rename_with_an_edit_looks_for_the_old_names_with_the_edit_s_hunk(self) -> None:
        self.document("docs/guide.md", "The `old_name.report` call writes it.\n")
        diff = (
            "diff --git a/tools/old_name.py b/tools/new_name.py\nsimilarity index 90%\n"
            "rename from tools/old_name.py\nrename to tools/new_name.py\nindex 1111111..2222222 100644\n"
            "--- a/tools/old_name.py\n+++ b/tools/new_name.py\n@@ -3,2 +3,2 @@\n x = 1\n-y = 2\n+y = 3\n"
        )
        self.assertEqual(
            ["new_name.py", "old_name.py", "new_name", "old_name"], [name.text for name in self.names(diff)]
        )
        self.assertEqual(
            (
                0,
                "PASSAGES 1 given=1\n",
                "PASSAGE docs/guide.md:1 names old_name (module in tools/new_name.py, renamed from "
                "tools/old_name.py), hunk @@ -3,2 +3,2 @@ at new lines 3-4\nThe `old_name.report` call writes it.\n",
            ),
            self.run_condition(diff),
        )

    def test_a_line_inside_a_hunk_is_content_however_it_begins(self) -> None:
        # An added `++ "x` line reads `+++ "x` and a removed `-- b/y` line reads `--- b/y`: neither is a header,
        # so neither is decoded nor names a file.
        diff = changed(
            "tools/quoted_text.py", ("", ['+++ "oops', "+++ b/tools/elsewhere.py", "--- a/tools/gone.py"])
        ) + changed("tools/next_tool.py", ("", ["+def write_all():"]))
        self.assertEqual(
            [
                ("write_all", "function"),
                ("quoted_text.py", "script"),
                ("next_tool.py", "script"),
                ("quoted_text", "module"),
                ("next_tool", "module"),
            ],
            [(name.text, name.kind) for name in self.names(diff)],
        )

    def test_a_changed_path_that_could_name_a_file_outside_the_snapshot_is_refused(self) -> None:
        self.document("docs/guide.md", "Run `outside.py` and `report_tool.py`.\n")
        for path in ("tools\\..\\..\\outside.py", "../outside.py", "C:/outside.py", "/outside.py"):
            quoted = '"b/' + path.replace("\\", "\\\\") + '"' if "\\" in path else f"b/{path}"
            section = f"diff --git a/x b/x\n--- a/x\n+++ {quoted}\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
            with self.subTest(path=path):
                self.assertEqual(
                    (
                        0,
                        f"REFUSED {json.dumps(path)}: a path that could name a file outside the snapshot\n"
                        "PASSAGES 1 given=1\n",
                    ),
                    self.run_condition(section + changed("tools/report_tool.py", ("", ["+y = 2"])))[:2],
                )

    def test_a_condition_run_without_its_inputs_fails_the_review(self) -> None:
        code, out, written = self.run_condition("", environment={})
        self.assertEqual(
            (
                2,
                "FAILED CODE_REVIEW_DIFF_FILE and CODE_REVIEW_CONDITION_OUTPUT not set; a review sets them for a "
                "condition\n",
                "",
            ),
            (code, out, written),
        )

    def test_an_unexpected_failure_fails_the_review_rather_than_closing_the_route(self) -> None:
        missing = {"CODE_REVIEW_DIFF_FILE": str(self.root / "absent.patch"), "CODE_REVIEW_CONDITION_OUTPUT": "x"}
        code, out, _ = self.run_condition("", environment=missing)
        self.assertEqual(2, code)
        self.assertTrue(out.startswith("FAILED FileNotFoundError: "), out)
