"""prepare for a pull request over a limit of GitHub's diff endpoint: its diff from git in the checkout or from GitHub's
files API, a changed file GitHub gives no patch for, the refusal when neither source serves, and the record saying
which source it was."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import review_pipeline as rp
from github_client import CommandResult
from review_github import GitHubClient, PullFiles
from review_specialists import parse_unified_diff
from test_review_pipeline import checked_run_list
from test_review_pipeline_prepare import (
    DIFF,
    HEAD_FILES,
    LINK_NOTE,
    NUMBER,
    REPOSITORY,
    SELECTOR,
    PrepareFixture,
    RecordingGitHub,
    git,
    request,
)

# gh's answer to the diff of a pull request over the diff endpoint's file limit.
REFUSAL = CommandResult(
    1,
    "",
    "gh: Sorry, the diff exceeded the maximum number of files (300). Consider using 'List pull requests files' API "
    "or locally cloning the repository instead. (HTTP 406)",
)
DIFF_COMMAND = ("gh", "api", "-H", "Accept: application/vnd.github.diff", f"repos/{REPOSITORY}/pulls/{NUMBER}")
PULL_COMMAND = ("gh", "api", f"repos/{REPOSITORY}/pulls/{NUMBER}")
FILES_COMMAND = ("gh", "api", "--paginate", "--slurp", f"repos/{REPOSITORY}/pulls/{NUMBER}/files?per_page=100")
# The change DIFF makes, as GitHub's files API lists it, over two pages; GitHub names no file mode or base blob.
FILES = [
    [
        {
            "filename": "CLAUDE.md",
            "status": "added",
            "changes": 1,
            "sha": "1" * 40,
            "patch": "@@ -0,0 +1 @@\n+Project notes",
        },
        {
            "filename": "app/cache",
            "status": "added",
            "changes": 1,
            "sha": "2" * 40,
            "patch": "@@ -0,0 +1 @@\n+/opt/tool/cache\n\\ No newline at end of file",
        },
    ],
    [
        {
            "filename": "app/service.py",
            "status": "modified",
            "changes": 2,
            "sha": "4" * 40,
            "patch": (
                "@@ -1,2 +1,4 @@\n def total(items):\n+    if not items:\n+        return 0\n     return sum(items)"
            ),
        }
    ],
]
FILES_NOTE = "GitHub's diff endpoint refuses a diff over 300 files, so the diff was read from GitHub's files API."
LOCAL_NOTE = (
    "GitHub's diff endpoint refuses a diff over 300 files, so the diff was read from git's diff in the checkout."
)
# The members of the head's tarball the change needs.
MEMBERS: list[tuple[str, bytes | str | None, str]] = [
    ("CLAUDE.md", HEAD_FILES["CLAUDE.md"].encode("utf-8"), "file"),
    ("app/service.py", HEAD_FILES["app/service.py"].encode("utf-8"), "file"),
    ("app/cache", "/opt/tool/cache", "symlink"),
]


class OverLimitGitHub(RecordingGitHub):
    """RecordingGitHub, whose diff GitHub's diff endpoint refuses over its file limit. The real reads then run, each
    gh command they make served from `served` and recorded in `commands`."""

    def __init__(self, head: str, base: str) -> None:
        super().__init__()
        self.github.runner = self._serve
        self.commands: list[tuple[str, ...]] = []
        self.pages: list[list[dict[str, Any]]] = FILES
        self.changed = 3
        self.served: dict[tuple[str, ...], CommandResult] = {
            DIFF_COMMAND: REFUSAL,
            ("gh", "api", f"repos/{REPOSITORY}/compare/{base}...{head}?per_page=1"): CommandResult(
                0, json.dumps({"merge_base_commit": {"sha": base}}), ""
            ),
        }
        self.head = head

    def _serve(self, arguments: Sequence[str]) -> CommandResult:
        command = tuple(arguments)
        self.commands.append(command)
        if command == PULL_COMMAND:
            return CommandResult(0, json.dumps({"changed_files": self.changed, "head": {"sha": self.head}}), "")
        if command == FILES_COMMAND:
            return CommandResult(0, json.dumps(self.pages), "")
        if command not in self.served:
            raise AssertionError(f"unexpected gh call: {list(arguments)}")
        return self.served[command]

    def get_pull_diff(self, repository: str, number: int) -> tuple[str, int]:
        self.calls.append(("get_pull_diff", repository, number))
        return GitHubClient.get_pull_diff(self, repository, number)

    def list_pull_files(self, repository: str, number: int) -> PullFiles:
        self.calls.append(("list_pull_files", repository, number))
        return super().list_pull_files(repository, number)


class LargeChangeFixture(PrepareFixture):
    def setUp(self) -> None:
        super().setUp()
        self.over = OverLimitGitHub(self.head, self.base)
        self.over.pulls = self.github.pulls
        self.services.github = self.over

    def main_prepare(self) -> str:
        """What `prepare` prints for the pull request, its run list's line left off, once it has failed."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rp.main(["--config", str(self.config_path), "prepare", "--pull", SELECTOR], services=self.services)
        self.assertEqual((1, ""), (code, err.getvalue()))
        return checked_run_list(self, ["prepare"], out.getvalue())


class FilesApiTests(LargeChangeFixture):
    """Without a checkout, GitHub's files API serves the change, every page of it."""

    def setUp(self) -> None:
        super().setUp()
        self.configure(checkout=False)
        self.tarball.members = list(MEMBERS)

    def test_a_change_over_the_limit_is_prepared_from_the_files_api_as_the_diff_endpoint_would_prepare_it(self) -> None:
        self.services.github = self.github  # the diff endpoint serves the change, as below the limit
        within, _ = self.prepare(run_directory=self.root / "within")
        expected = self.normalize(json.loads((self.root / "within" / "request.json").read_text(encoding="utf-8")))
        self.services.github = self.over
        result, printed = self.prepare()
        self.assertEqual("", printed)
        self.assertEqual([FILES_NOTE, LINK_NOTE], result["notes"])
        self.assertEqual({"source": "files-api", "limit": "300 files"}, result["snapshot"]["diff"])
        self.assertNotIn("diff", within["snapshot"])
        # The same request, coverage included, for the same change, but for where the run is.
        self.assertEqual(
            json.loads(json.dumps(expected).replace("<root>/within", "<root>/run")), self.json_file("request.json")
        )
        self.assertEqual(
            self.text_file("work/generic-review.files.txt"),
            (self.root / "within" / "work" / "generic-review.files.txt").read_text(encoding="utf-8"),
        )
        served = parse_unified_diff((self.run_dir / "diff.patch").read_text(encoding="utf-8"))
        self.assertEqual(
            {path: entry["added"] for path, entry in parse_unified_diff(DIFF).items()},
            {path: entry["added"] for path, entry in served.items()},
        )
        self.assertEqual(
            [
                ("get_pull", REPOSITORY, NUMBER),
                ("get_pull_diff", REPOSITORY, NUMBER),
                ("list_pull_files", REPOSITORY, NUMBER),
                ("get_pull", REPOSITORY, NUMBER),
                ("list_open_review_threads", REPOSITORY, NUMBER),
            ],
            self.over.calls,
        )
        self.assertEqual([DIFF_COMMAND, PULL_COMMAND, FILES_COMMAND], self.over.commands)

    def test_the_files_api_s_diff_is_git_s_form_of_each_entry(self) -> None:
        self.over.pages = [
            [
                *FILES[0],
                # A binary file, an empty one, a pure rename with a name git quotes, and a deletion.
                {"filename": "assets/logo.png", "status": "added", "changes": 0, "sha": "5" * 40},
                {
                    "filename": "app/__init__.py",
                    "status": "added",
                    "changes": 0,
                    "sha": "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
                },
                {
                    "filename": "docs/résumé b.md",
                    "previous_filename": "docs/old.md",
                    "status": "renamed",
                    "changes": 0,
                    "sha": "6" * 40,
                },
                {
                    "filename": "docs/gone.md",
                    "status": "removed",
                    "changes": 1,
                    "sha": "7" * 40,
                    "patch": "@@ -1 +0,0 @@\n-Gone",
                },
            ],
            FILES[1],
        ]
        self.over.changed = 7
        self.tarball.members = [
            *MEMBERS,
            ("assets/logo.png", b"\x89PNG\0data", "file"),
            ("app/__init__.py", b"", "file"),
            ("docs/résumé b.md", b"Notes\n", "file"),
        ]
        self.prepare()
        self.assertEqual(
            "diff --git a/CLAUDE.md b/CLAUDE.md\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/CLAUDE.md\n"
            "@@ -0,0 +1 @@\n"
            "+Project notes\n"
            "diff --git a/app/cache b/app/cache\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/app/cache\n"
            "@@ -0,0 +1 @@\n"
            "+/opt/tool/cache\n"
            "\\ No newline at end of file\n"
            "diff --git a/assets/logo.png b/assets/logo.png\n"
            "new file mode 100644\n"
            "Binary files /dev/null and b/assets/logo.png differ\n"
            "diff --git a/app/__init__.py b/app/__init__.py\n"
            "new file mode 100644\n"
            'diff --git a/docs/old.md "b/docs/r\\303\\251sum\\303\\251 b.md"\n'
            "rename from docs/old.md\n"
            'rename to "docs/r\\303\\251sum\\303\\251 b.md"\n'
            "diff --git a/docs/gone.md b/docs/gone.md\n"
            "deleted file mode 100644\n"
            "--- a/docs/gone.md\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-Gone\n"
            "diff --git a/app/service.py b/app/service.py\n"
            "--- a/app/service.py\n"
            "+++ b/app/service.py\n"
            "@@ -1,2 +1,4 @@\n"
            " def total(items):\n"
            "+    if not items:\n"
            "+        return 0\n"
            "     return sum(items)\n",
            (self.run_dir / "diff.patch").read_text(encoding="utf-8"),
        )
        # A binary of a type the review does not read is named as unreviewed, as from the diff endpoint.
        self.assertEqual(
            {"unavailable_sources": [], "unreviewed_files": {"assets/logo.png": ".png"}},
            self.json_file("request.json")["coverage"],
        )
        self.assertEqual(
            [
                "CLAUDE.md",
                "app/cache",
                "assets/logo.png",
                "app/__init__.py",
                "docs/résumé b.md",
                "docs/gone.md",
                "app/service.py",
            ],
            list(parse_unified_diff((self.run_dir / "diff.patch").read_text(encoding="utf-8"))),
        )

    def test_a_changed_file_github_gives_no_patch_for_is_a_coverage_gap_and_noted(self) -> None:
        service = {key: value for key, value in FILES[1][0].items() if key != "patch"}
        self.over.pages = [FILES[0], [service]]
        result, _ = self.prepare()
        self.assertIn(
            "diff --git a/app/service.py b/app/service.py\n", (self.run_dir / "diff.patch").read_text(encoding="utf-8")
        )
        self.assertEqual(
            {"unavailable_sources": ["app/service.py"], "unreviewed_files": {}},
            self.json_file("request.json")["coverage"],
        )
        self.assertEqual(
            [
                FILES_NOTE,
                "Not reviewed in full: 1 changed file whose patch GitHub's files API leaves out, as it does for a diff "
                'too large to show, recorded as unavailable sources: "app/service.py".',
                LINK_NOTE,
            ],
            result["notes"],
        )

    def test_a_change_the_files_api_does_not_list_whole_fails_naming_the_limit_and_the_pull_request(self) -> None:
        self.over.changed = 3_512
        self.assertEqual(
            "FAILED example/one#12 example/one#12 is over GitHub's diff limit of 300 files, and GitHub's files API "
            "lists 3 of its 3,512 changed files; set checkout_path for example/one so prepare reads its diff from the "
            "checkout\n",
            self.main_prepare(),
        )
        self.assertFalse(self.run_dir.exists())
        self.assertEqual([], self.tarball.calls)

    def test_a_push_before_the_files_are_listed_fails_as_a_moved_pull_request(self) -> None:
        self.over.head = "f" * 40
        self.assertEqual(
            "FAILED example/one#12 example/one#12 changed while it was being prepared (head <head12> is now "
            "ffffffffffff); run prepare again\n",
            self.normalize(self.main_prepare()),
        )


class CheckoutDiffTests(LargeChangeFixture):
    """With a checkout, git's diff from the merge base to the head serves the change, and the files API is not read."""

    def test_a_change_over_the_limit_is_prepared_from_git_s_diff_in_the_checkout(self) -> None:
        result, printed = self.prepare()
        self.assertEqual("", printed)
        self.assertEqual([LOCAL_NOTE, LINK_NOTE], result["notes"])
        self.assertEqual({"source": "checkout", "limit": "300 files"}, result["snapshot"]["diff"])
        # The request the diff endpoint's diff of the same change gives.
        self.assertEqual(request(), self.json_file("request.json"))
        # git's own diff, with none of the options a configuration could change, as GitHub computes it.
        options = ("--no-color", "--no-ext-diff", "--no-textconv", "--find-renames", "--unified=3", "--submodule=short")
        expected = git(self.checkout, "diff", *options, "--src-prefix=a/", "--dst-prefix=b/", self.base, self.head)
        self.assertEqual(expected + "\n", (self.run_dir / "diff.patch").read_text(encoding="utf-8"))
        self.assertEqual(
            {path: entry["added"] for path, entry in parse_unified_diff(DIFF).items()},
            {path: entry["added"] for path, entry in parse_unified_diff(expected + "\n").items()},
        )
        compare = ("gh", "api", f"repos/{REPOSITORY}/compare/{self.base}...{self.head}?per_page=1")
        self.assertEqual([DIFF_COMMAND, compare], self.over.commands)
        self.assertNotIn(("list_pull_files", REPOSITORY, NUMBER), self.over.calls)

    def test_a_merge_base_the_checkout_lacks_fails_naming_the_limit_and_the_pull_request(self) -> None:
        compare = ("gh", "api", f"repos/{REPOSITORY}/compare/{self.base}...{self.head}?per_page=1")
        self.over.served[compare] = CommandResult(0, json.dumps({"merge_base_commit": {"sha": "f" * 40}}), "")
        self.assertEqual(
            "FAILED example/one#12 example/one#12 is over GitHub's diff limit of 300 files, and its merge base "
            "ffffffffffff is not in <root>/checkout\n",
            self.normalize(self.main_prepare()),
        )
        self.assertFalse(self.run_dir.exists())
