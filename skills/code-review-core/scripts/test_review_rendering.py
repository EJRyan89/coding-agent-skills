"""Literal report fields, through validated reviewer results and hash-linked records."""

from __future__ import annotations

import copy
import html
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import review_records as records
from review_documents import extract, locations
from review_operation import archive_base, commit_adapter_result
from test_review_core import valid_adapter_result, valid_request
from test_review_documents import paragraph, word_document

MARKUP = "![image](https://example.invalid/a) <img src=x> `code` | &amp;"
LITERAL = (
    "&#33;&#91;image&#93;&#40;https&#58;&#47;&#47;example&#46;invalid&#47;a&#41; "
    "&lt;img src&#61;x&gt; &#96;code&#96; &#124; &amp;amp&#59;"
)
PROSE = MARKUP + "\n[link](https://example.invalid/b)\n\n# Heading\n    ```\n> quote\n1. item"
LITERAL_PROSE = (
    LITERAL + "<br>\n&#91;link&#93;&#40;https&#58;&#47;&#47;example&#46;invalid&#47;b&#41;\n\n"
    "&#35; Heading<br>\n&#32;&#32;&#32;&#32;&#96;&#96;&#96;<br>\n&gt; quote<br>\n1&#46; item"
)


def validated_result() -> records.AdapterResult:
    result = valid_adapter_result()
    result["summary"] = PROSE
    result["reviewer"] = MARKUP
    result["findings"][0].update(title=MARKUP, category=MARKUP, source=MARKUP, body=PROSE)
    return records.validate_adapter_result(
        result, expected_repository="example/one", expected_number=12, expected_head_sha="b" * 40
    )


class LiteralReportTests(unittest.TestCase):
    def test_summary_body_title_category_and_source_are_literal_without_changing_the_record(self) -> None:
        result = validated_result()
        record = records.build_record(valid_request(), result, version=1, policy={})
        before = copy.deepcopy(record)
        records.validate_record(record)
        report = records.render_markdown(record, record_payload_hash="0" * 64)
        self.assertIn("## Summary\n\n" + LITERAL_PROSE + "\n\n## Findings", report)
        self.assertIn(f"<summary>v1 F001. [{LITERAL}] {LITERAL}</summary>", report)
        self.assertIn(f"| **Source:** {LITERAL}\n", report)
        self.assertIn("\n".join("> " + line if line else ">" for line in LITERAL_PROSE.splitlines()), report)
        self.assertIn(f"| **Reviewer** | {LITERAL} (complete) |", report)
        self.assertIn("<summary><strong>SHOULD FIX (1)</strong></summary>", report)
        self.assertIn("| **URL** | https://github.com/example/one/pull/12 |", report)
        self.assertIn("> **Evidence:** `The added branch excludes zero.`", report)
        self.assertTrue(report.endswith(f"<!-- reviewed_head_sha: {'b' * 40} -->\n"))
        self.assertEqual(before, record)
        self.assertEqual(PROSE, record["review"]["summary"])

    def test_word_heading_location_is_literal_on_both_evidence_routes_in_a_validated_pair(self) -> None:
        heading = "![i](https://example.invalid/a) [l](https://example.invalid/b) <img> `x` \\|"
        content = word_document(paragraph(html.escape(heading), style="Titre1"), paragraph("Body"))
        extracted = extract("docx", content).decode("utf-8")
        places = locations(extracted)
        self.assertEqual(f'paragraph 2, under the heading "{heading}"', places[1])
        literal_place = (
            "paragraph 2&#44; under the heading &#34;&#33;&#91;i&#93;&#40;https&#58;&#47;&#47;"
            "example&#46;invalid&#47;a&#41; &#91;l&#93;&#40;https&#58;&#47;&#47;example&#46;"
            "invalid&#47;b&#41; &lt;img&gt; &#96;x&#96; &#92;&#124;&#34;"
        )
        for evidence in ("docs/design.docx:2 adds: [P2] Body", "The paragraph omits the limit."):
            with self.subTest(evidence=evidence), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                request = valid_request()
                request["pull_request"] = {
                    "url": request["pull_url"],
                    **{key: request[key] for key in ("title", "base_ref", "base_sha", "head_sha")},
                }
                result = valid_adapter_result()
                result["findings"][0].update(path="docs/design.docx", line=2, evidence=evidence)
                request_path, result_path = root / "request.json", root / "result.json"
                request_path.write_text(json.dumps(request), encoding="utf-8")
                result_path.write_text(json.dumps(result), encoding="utf-8")
                json_path, markdown_path, record = commit_adapter_result(
                    request_path=request_path,
                    result_path=result_path,
                    archive_root=root / "archive",
                    policy={},
                    adapter=request["adapter"],
                    base=archive_base(None, []),
                    documents={"docs/design.docx": places},
                )
                self.assertEqual(places[1], record["findings"][0]["location"])
                self.assertEqual(record, records.validate_record_pair(json_path, markdown_path))
                self.assertIn(f"({literal_place})", markdown_path.read_text(encoding="utf-8"))
                self.assertEqual(f"[P1] # {heading}\n[P2] Body\n", extracted)

    def test_escape_parity_autolinks_tables_and_newlines_cannot_introduce_markup(self) -> None:
        values = {
            r"\![x](https://example.invalid)": (
                "&#92;&#33;&#91;x&#93;&#40;https&#58;&#47;&#47;example&#46;invalid&#41;"
            ),
            r"\\![x](https://example.invalid)": (
                "&#92;&#92;&#33;&#91;x&#93;&#40;https&#58;&#47;&#47;example&#46;invalid&#41;"
            ),
            "` `` ``` *bold* _em_ ~~strike~~ $math$": (
                "&#96; &#96;&#96; &#96;&#96;&#96; &#42;bold&#42; &#95;em&#95; "
                "&#126;&#126;strike&#126;&#126; &#36;math&#36;"
            ),
            "https://example.invalid www.example.invalid mail@example.invalid @example": (
                "https&#58;&#47;&#47;example&#46;invalid www&#46;example&#46;invalid "
                "mail&#64;example&#46;invalid &#64;example"
            ),
            "a|b\n\nc\\\nd": "a&#124;b\n\nc&#92;<br>\nd",
            "<details>\n</summary>\n&#33; &lt;": (
                "&lt;details&gt;<br>\n&lt;&#47;summary&gt;<br>\n&amp;&#35;33&#59; &amp;lt&#59;"
            ),
        }
        for supplied, expected in values.items():
            with self.subTest(supplied=supplied):
                result = valid_adapter_result()
                result["summary"] = supplied
                record = records.build_record(valid_request(), result, version=1, policy={})
                records.validate_record(record)
                report = records.render_markdown(record, record_payload_hash="0" * 64)
                self.assertIn("## Summary\n\n" + expected + "\n\n## Findings", report)
                self.assertEqual(supplied, html.unescape(expected.replace("<br>", "")))

    def test_comment_rationale_preserves_lines_inside_its_table_and_the_generated_comment_link(self) -> None:
        request = valid_request()
        request["github_comments"] = [
            {
                "id": "C1",
                "author": "example",
                "path": "src/file.cs",
                "line": 42,
                "outdated": False,
                "body": MARKUP,
                "url": "https://github.com/example/one/pull/12#discussion_r1",
            }
        ]
        result = validated_result()
        result["comment_dispositions"] = [{"comment_id": "C1", "disposition": "addressed", "rationale": PROSE}]
        records.validate_adapter_result(
            result,
            expected_repository="example/one",
            expected_number=12,
            expected_head_sha="b" * 40,
            comment_ids=["C1"],
        )
        record = records.build_record(request, result, version=1, policy={})
        records.validate_record(record)
        report = records.render_markdown(record, record_payload_hash="0" * 64)
        row = next(line for line in report.splitlines() if line.startswith("| [C1]"))
        self.assertIn("[C1](https://github.com/example/one/pull/12#discussion_r1)", row)
        self.assertIn(LITERAL_PROSE.replace("<br>\n", "<br>").replace("\n", "<br>"), row)
        self.assertEqual(5, row.count("|"), "only generated table separators remain")

    def test_ledger_status_flags_repeats_and_closed_rationales_render_literally(self) -> None:
        first = records.build_record(valid_request(), validated_result(), version=1, policy={})
        request = {**valid_request(), "mode": "re-review"}
        for disposition in records.DISPOSITIONS:
            with self.subTest(disposition=disposition):
                result = validated_result()
                result["findings"][0]["repeats"] = "v1:F001"
                result["prior_dispositions"] = [
                    {"finding_id": "v1:F001", "disposition": disposition, "rationale": PROSE}
                ]
                if disposition not in records.OPEN_DISPOSITIONS:
                    del result["findings"][0]["repeats"]
                records.validate_adapter_result(
                    result,
                    expected_repository="example/one",
                    expected_number=12,
                    expected_head_sha="b" * 40,
                    prior_ids=["v1:F001"],
                    prior_severities={"v1:F001": "SHOULD_FIX"},
                )
                record = records.build_record(request, result, version=2, policy={}, prior_ledger=first["ledger"])
                records.validate_record(record)
                flags = [
                    {
                        "id": "RF-000001",
                        "repository": "example/one",
                        "pull_number": 12,
                        "review_version": 1,
                        "finding_id": "F001",
                        "category": MARKUP,
                        "body": PROSE,
                    }
                ]
                report = records.render_markdown(
                    record, record_payload_hash="0" * 64, prior_records=[first], flags=flags
                )
                self.assertIn(": " + LITERAL + "<br>", report)
                if disposition in ("addressed", "superseded"):
                    self.assertIn("<summary><strong>Addressed since v1</strong></summary>", report)
                    self.assertIn(f"[{LITERAL}] {LITERAL},", report)
                else:
                    self.assertIn(f"> **Flagged:** RF&#45;000001 ({LITERAL}): {LITERAL}<br>", report)
                    if disposition in records.OPEN_DISPOSITIONS:
                        self.assertIn("Repeats v1 F001:", report)

    def test_reviewer_metadata_and_scope_prose_are_literal_in_table_cells(self) -> None:
        first = records.build_record(valid_request(), validated_result(), version=1, policy={})
        request = {
            **valid_request(),
            "mode": "re-review",
            "reviewers": [
                {
                    "id": "fixture",
                    "category": MARKUP,
                    "model": MARKUP,
                    "files": 1,
                    "findings": 1,
                    "retries": 0,
                    "dispositions_only": False,
                }
            ],
            "scope": {
                "requested": "full",
                "used": "full",
                "reason": PROSE,
                "since_version": 1,
                "files_changed": 1,
                "files_total": 1,
                "lines_changed": 1,
                "lines_total": 1,
            },
        }
        result = validated_result()
        result["prior_dispositions"] = [{"finding_id": "v1:F001", "disposition": "still_present", "rationale": "Same"}]
        record = records.build_record(request, result, version=2, policy={}, prior_ledger=first["ledger"])
        records.validate_record(record)
        for names in ({}, {MARKUP: MARKUP + " mapped"}):
            with self.subTest(names=names):
                report = records.render_markdown(
                    record, record_payload_hash="0" * 64, prior_records=[first], model_names=names
                )
                self.assertIn(f"| `fixture` | {LITERAL} | {LITERAL}", report)
                if names:
                    code = MARKUP.replace("|", "\\|")
                    self.assertIn(f"| **Reviewer models** | {LITERAL} mapped: ``{code}`` |", report)
                scope = next(line for line in report.splitlines() if line.startswith("| **Scope**"))
                self.assertIn(LITERAL_PROSE.replace("<br>\n", "<br>").replace("\n", "<br>"), scope)

    def test_only_https_navigation_is_active_and_destinations_cannot_add_markup(self) -> None:
        request = valid_request()
        request["github_comments"] = [
            {
                "id": "C1",
                "author": "example",
                "path": "src/file.cs",
                "line": 42,
                "outdated": False,
                "body": "Comment",
                "url": "https://example.invalid/comment",
            }
        ]
        result = validated_result()
        result["comment_dispositions"] = [{"comment_id": "C1", "disposition": "addressed", "rationale": "Done"}]
        for url in ("javascript:example", "data:text/html,example", "//example.invalid/a", "https://example.invalid/a"):
            with self.subTest(url=url):
                request["pull_url"] = request["github_comments"][0]["url"] = url
                record = records.build_record(request, result, version=1, policy={})
                records.validate_record(record)
                report = records.render_markdown(record, record_payload_hash="0" * 64)
                self.assertEqual(url.startswith("https://"), "| [C1](" in report)
                if not url.startswith("https://"):
                    self.assertNotIn(url, report)
        url = "https://example.invalid/a)![i](https://example.invalid/b)|<img>"
        request["pull_url"] = request["github_comments"][0]["url"] = url
        record = records.build_record(request, result, version=1, policy={})
        records.validate_record(record)
        report = records.render_markdown(record, record_payload_hash="0" * 64)
        self.assertIn("[C1](https://example.invalid/a%29!%5Bi%5D%28https://example.invalid/b%29%7C%3Cimg%3E)", report)
        self.assertNotIn("![i]", report)

    def test_multiline_evidence_stays_code_and_preserves_its_line_breaks(self) -> None:
        for prefix in ("", "src/file.cs:42 adds: "):
            with self.subTest(prefix=prefix):
                result = validated_result()
                result["findings"][0]["evidence"] = prefix + MARKUP + "\n\n``code``\nlast"
                record = records.build_record(valid_request(), result, version=1, policy={})
                records.validate_record(record)
                report = records.render_markdown(record, record_payload_hash="0" * 64)
                self.assertIn(f"``{MARKUP}``<br><br>``` ``code`` ```<br>`last`", report)

    def test_code_labels_with_pipes_and_backslashes_cannot_split_a_table_cell(self) -> None:
        for branch, encoded in ((r"x\|`y`", "x&#92;&#124;&#96;y&#96;"), (r"x\\|", "x&#92;&#92;&#124;")):
            with self.subTest(branch=branch):
                request = {**valid_request(), "head_ref": branch}
                record = records.build_record(request, validated_result(), version=1, policy={})
                records.validate_record(record)
                report = records.render_markdown(record, record_payload_hash="0" * 64)
                row = next(line for line in report.splitlines() if line.startswith("| **Branch**"))
                self.assertEqual(f"| **Branch** | <code>{encoded}</code> → `main` |", row)
                self.assertEqual(3, row.count("|"))

    def test_a_review_comment_excerpt_is_one_literal_line_cut_at_the_limit(self) -> None:
        comment = {
            "id": "C1",
            "author": "dev",
            "path": "src/file.cs",
            "line": 7,
            "outdated": False,
            "body": "Why\u00a0is\t\tthis\r\n\n  here? ![i](https://example.invalid/a) | <b>",
            "url": "https://example.invalid/1",
        }
        long = {**comment, "id": "C2", "body": "word " * 40}
        result: records.AdapterResult = {
            **validated_result(),
            "comment_dispositions": [
                {"comment_id": "C1", "disposition": "still_present", "rationale": "Unchanged."},
                {"comment_id": "C2", "disposition": "addressed", "rationale": "Done."},
            ],
        }
        request = {**valid_request(), "github_comments": [comment, long]}
        record = records.build_record(request, result, version=1, policy={})
        records.validate_record(record)
        report = records.render_markdown(record, record_payload_hash="0" * 64)
        rows = [line for line in report.splitlines() if line.startswith("| [C")]
        self.assertEqual(
            "| [C1](https://example.invalid/1) | &#64;dev on `src/file.cs:7`: Why is this here&#63; "
            "&#33;&#91;i&#93;&#40;https&#58;&#47;&#47;example&#46;invalid&#47;a&#41; &#124; &lt;b&gt; "
            "| STILL PRESENT | Unchanged&#46; |",
            rows[0],
        )
        excerpt = rows[1].split(": ", 1)[1].rsplit(" | ", 2)[0]
        self.assertEqual(" ".join(["word"] * 23) + " wo&#46;&#46;&#46;", excerpt, "117 characters, then three dots")

    def test_an_evidence_line_is_one_code_span_with_its_whitespace_folded(self) -> None:
        result = validated_result()
        result["findings"][0]["evidence"] = "x\t=\u00a0 1\u2003 \n `tick` \n``\x1f\x1f``"
        record = records.build_record(valid_request(), result, version=1, policy={})
        records.validate_record(record)
        report = records.render_markdown(record, record_payload_hash="0" * 64)
        self.assertIn("> **Evidence:** `x = 1`<br>`` `tick` ``<br>``` `` `` ```", report)
