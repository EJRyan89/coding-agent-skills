"""Fixture tests for tests/validation/one_helper.py.

The place-lookup rule fails on a handler on a path to a place lookup that treats a GitError's kinds apart, and on a
scope nothing calls that lets one escape, and passes on paths that treat every GitError as an unknown place. The
closing-reference rule fails on a module other than the helper that matches a closing keyword before an issue reference
itself, or reads a title or a body alone, and passes on one that reads both through the helper. The status rule fails
on a module other than git_worktrees.py that gives a call git arguments running `status`, and passes on one that reads
status through the helper. The pull-files rule fails on a module other than review_github.py that spells GitHub's files
endpoint of a pull request, or reads list_pull_files' listing without checking it is whole and at the head it asked
for, and passes on one that checks both. The snapshot-path rule fails on a module other than diff_paths.py that joins a
name it judged with unsafe_snapshot_name itself, and passes on one that judges it through snapshot_path. The
repository-name rule fails on a module other than github_remote.py that spells an owner/name pattern with its own
copy of the repository characters, and passes on one that builds the pattern on the helper's.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import closing_references
import github_remote
import one_helper
from one_helper import (
    CATCHING,
    HELPER,
    KIND_ATTRIBUTES,
    PATTERN_FUNCTIONS,
    POLICY,
    PULL_FILES_HELPER,
    REFERENCE_PATTERN,
    REPOSITORY_HELPER,
    SAMPLE_REFERENCES,
    SNAPSHOT_HELPER,
    STATUS_HELPER,
    closing_reference_problems,
    error_attributes,
    module_closing_problems,
    pattern_functions,
    place_lookup_problems,
    pull_files_problems,
    repository_problems,
    snapshot_path_problems,
    status_problems,
)
from validation_support import REPOSITORY_ROOT, write_fixture_tree

TREES = "tools/trees.py"
GUARD = "tools/guard.py"
LOOKUPS = {TREES: frozenset({"find"}), GUARD: frozenset({"lookup"})}

# Two modules whose every path to a place lookup treats any GitError as an unknown place: a lookup that raises, one in
# the other module that calls it, an entry point that converts every kind to another error, a function that lets the
# error out to callers that catch it, a method chain reached through self and through a receiver one class alone
# defines a method for, and a catch-all that tests the error against GitError whole.
CONFORMING_TREES = """\
from git_client import GitError


class UsageError(Exception):
    pass


def find(path):
    raise GitError(f"cannot read {path}")


def current():
    try:
        return find(".")
    except GitError as exc:
        raise UsageError(str(exc)) from exc


def main():
    try:
        return current()
    except UsageError:
        return 1


if __name__ == "__main__":
    main()
"""
CONFORMING_GUARD = """\
import trees
from git_client import GitError


def lookup(path):
    return trees.find(path)


def reached(path):
    return lookup(path)


class Judge:
    def judge(self, path):
        return reached(path)

    def denied(self, path):
        return self.judge(path)


def guard(path):
    judge = Judge()
    try:
        return judge.denied(path)
    except Exception as exc:
        return isinstance(exc, (TimeoutError, GitError))


def view(path):
    try:
        return lookup(path)
    except (GitError, OSError):
        return None


if __name__ == "__main__":
    guard(view("."))
"""

# Each form the paragraph names that the rule refuses, as a replacement in one of the conforming modules, and what the
# refusal says.
REFUSED_PLACE_LOOKUPS: dict[str, tuple[str, str, str, str]] = {
    "a handler that reads the kind": (
        GUARD,
        "    except (GitError, OSError):\n        return None",
        "    except GitError as exc:\n        return None if exc.kind == 'not_repository' else path",
        "view catches the GitError of a place lookup that lookup raises and treats its kinds apart (reads exc.kind)",
    ),
    "a handler that reads whether git answered": (
        GUARD,
        "    except (GitError, OSError):\n        return None",
        "    except GitError as error:\n        return error.answered",
        "treats its kinds apart (reads error.answered)",
    ),
    "a handler that reads git's exit code": (
        GUARD,
        "    except (GitError, OSError):\n        return None",
        "    except GitError as exc:\n        return exc.returncode",
        "treats its kinds apart (reads exc.returncode)",
    ),
    "a catch-all that counts only another class": (
        GUARD,
        "isinstance(exc, (TimeoutError, GitError))",
        "isinstance(exc, TimeoutError)",
        "guard catches the GitError of a place lookup that Judge.denied raises and treats its kinds apart (tests exc "
        "with isinstance against TimeoutError)",
    ),
    "a handler that raises the error again": (
        GUARD,
        "    except (GitError, OSError):\n        return None",
        "    except GitError:\n        raise",
        "<module>, which nothing in tools/guard.py or tools/trees.py calls, lets the GitError of a place lookup that "
        "view raises escape",
    ),
    "a handler that raises the error again by its name": (
        GUARD,
        "    except (GitError, OSError):\n        return None",
        "    except GitError as exc:\n        raise exc",
        "lets the GitError of a place lookup that view raises escape",
    ),
    "a lookup in a try's else": (
        GUARD,
        "    try:\n        return lookup(path)\n    except (GitError, OSError):\n        return None",
        "    try:\n        found = None\n    except (GitError, OSError):\n        return None\n    else:\n"
        "        return lookup(path)",
        "lets the GitError of a place lookup that view raises escape",
    ),
    "a lookup in a try's finally": (
        GUARD,
        "    try:\n        return lookup(path)\n    except (GitError, OSError):\n        return None",
        "    try:\n        found = None\n    except (GitError, OSError):\n        return None\n    finally:\n"
        "        lookup(path)",
        "lets the GitError of a place lookup that view raises escape",
    ),
    "a handler for another class only": (
        GUARD,
        "    except (GitError, OSError):\n        return None",
        "    except OSError:\n        return None",
        "lets the GitError of a place lookup that view raises escape",
    ),
    "a lookup of the other module called through its name": (
        GUARD,
        "    try:\n        return lookup(path)\n    except (GitError, OSError):",
        "    try:\n        return trees.find(path)\n    except OSError:",
        "lets the GitError of a place lookup that view raises escape",
    ),
    "an entry point that catches nothing, through a method chain": (
        GUARD,
        "    try:\n        return judge.denied(path)\n    except Exception as exc:\n"
        "        return isinstance(exc, (TimeoutError, GitError))",
        "    return judge.denied(path)",
        "<module>, which nothing in tools/guard.py or tools/trees.py calls, lets the GitError of a place lookup that "
        "guard raises escape",
    ),
    "a module's own statements": (
        GUARD,
        "    guard(view('.'))".replace("'", '"'),
        '    guard(reached("."))',
        "<module>, which nothing in tools/guard.py or tools/trees.py calls, lets the GitError of a place lookup that "
        "reached raises escape",
    ),
    "a module-level assignment": (
        GUARD,
        "\n\nif __name__",
        "\n\nHUB = lookup('.')\n\nif __name__",
        "HUB, which nothing in tools/guard.py or tools/trees.py calls, lets the GitError of a place lookup that lookup "
        "raises escape",
    ),
    "an entry point in the other module": (
        TREES,
        '    try:\n        return find(".")\n    except GitError as exc:\n        raise UsageError(str(exc)) from exc',
        '    return find(".")',
        "tools/trees.py:24 <module>, which nothing in tools/guard.py or tools/trees.py calls, lets the GitError of a "
        "place lookup that main raises escape",
    ),
}


def _modules(changes: Mapping[str, tuple[str, str]] | None = None) -> dict[str, str]:
    texts = {TREES: CONFORMING_TREES, GUARD: CONFORMING_GUARD}
    for path, (old, new) in (changes or {}).items():
        if old not in texts[path]:
            raise AssertionError(f"{old!r} is not in the fixture {path}")
        texts[path] = texts[path].replace(old, new)
    return texts


class PlaceLookupCase(unittest.TestCase):
    def problems(
        self,
        files: Mapping[str, str],
        lookups: Mapping[str, frozenset[str]] = LOOKUPS,
        allowed: Mapping[str, Mapping[str, str]] | None = None,
    ) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_fixture_tree(root, files)
            return place_lookup_problems(root, lookups, allowed or {})


class PlaceLookupTests(PlaceLookupCase):
    def test_paths_that_treat_every_git_error_as_an_unknown_place_pass(self) -> None:
        self.assertEqual([], self.problems(_modules()))

    def test_each_refused_form_fails(self) -> None:
        for name, (path, old, new, expected) in REFUSED_PLACE_LOOKUPS.items():
            with self.subTest(name):
                problems = self.problems(_modules({path: (old, new)}))
                self.assertEqual(1, len(problems), problems)
                self.assertIn(expected, problems[0])
                self.assertIn("PLACE_LOOKUP_SANCTIONS", problems[0])

    def test_a_handler_that_catches_a_class_git_error_derives_from_or_every_error_passes(self) -> None:
        handlers = (
            "except RuntimeError:",
            "except Exception:",
            "except BaseException:",
            "except:",
            "except (OSError, GitError):",
        )
        for handler in handlers:
            with self.subTest(handler):
                files = _modules({GUARD: ("    except (GitError, OSError):", f"    {handler}")})
                self.assertEqual([], self.problems(files))

    def test_a_handler_that_raises_another_error_converts_every_kind_alike_unless_it_reads_the_kind(self) -> None:
        # The conforming entry point in trees.py raises UsageError from the GitError, which main catches; a conversion
        # without `from`, or of a message of its own, converts every kind alike too.
        old = "        raise UsageError(str(exc)) from exc"
        for new in (old, "        raise UsageError(str(exc))", '        raise UsageError("an unknown place")'):
            with self.subTest(new):
                self.assertEqual([], self.problems(_modules({TREES: (old, new)})))
        # One that reads the kind to choose what it raises treats the kinds apart.
        reading = '        raise UsageError("gone" if exc.kind == "not_repository" else str(exc)) from exc'
        problems = self.problems(_modules({TREES: (old, reading)}))
        self.assertEqual(1, len(problems), problems)
        self.assertIn(
            "current catches the GitError of a place lookup that find raises and treats its kinds apart (reads "
            "exc.kind)",
            problems[0],
        )

    def test_a_function_nested_in_another_is_followed_and_a_nested_name_shadows_a_top_level_one(self) -> None:
        end = "\n\nif __name__"
        # A helper nested in a function calls the lookup; the function that calls the helper treats the kinds apart.
        apart = (
            "\n\ndef wrap(path):\n    def inner():\n        return lookup(path)\n\n    try:\n        return inner()\n"
            f"    except GitError as exc:\n        return exc.kind{end}"
        )
        problems = self.problems(_modules({GUARD: (end, apart)}))
        self.assertEqual(1, len(problems), problems)
        self.assertIn(
            "wrap catches the GitError of a place lookup that wrap.inner raises and treats its kinds apart (reads "
            "exc.kind)",
            problems[0],
        )
        # A function nothing calls lets out the GitError of the helper nested in it.
        escaping = f"\n\ndef outer(path):\n    def inner():\n        return lookup(path)\n\n    return inner(){end}"
        problems = self.problems(_modules({GUARD: (end, escaping)}))
        self.assertEqual(1, len(problems), problems)
        self.assertIn(
            "outer, which nothing in tools/guard.py or tools/trees.py calls, lets the GitError of a place lookup that "
            "outer.inner raises escape",
            problems[0],
        )
        # A nested function named as a top-level lookup is the one its enclosing function calls.
        shadowing = f"\n\ndef safe(path):\n    def lookup(p):\n        return p\n\n    return lookup(path){end}"
        self.assertEqual([], self.problems(_modules({GUARD: (end, shadowing)})))

    def test_a_lookup_module_the_policy_cannot_read_or_a_lookup_no_call_reaches_is_named(self) -> None:
        self.assertEqual(
            [f"{POLICY}: PLACE_LOOKUPS names {GUARD}, which is missing, so no path to its lookups can be measured"],
            self.problems({TREES: CONFORMING_TREES}),
        )
        problems = self.problems({TREES: CONFORMING_TREES, GUARD: "def lookup(:\n"})
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith(f"{GUARD} does not parse"), problems)
        unreached = {**LOOKUPS, GUARD: frozenset({"lookup", "unused"})}
        files = _modules({GUARD: ("\n\nif __name__", "\n\ndef unused(path):\n    return path\n\n\nif __name__")})
        self.assertEqual(
            [
                f"{POLICY}: PLACE_LOOKUPS names {GUARD} unused, which no call in tools/guard.py or tools/trees.py "
                "reaches, so no path to it is measured; remove it, or name the lookup its callers call"
            ],
            self.problems(files, unreached),
        )

    def test_a_scope_allowed_with_its_reason_passes_and_a_stale_or_bare_entry_fails(self) -> None:
        name = "a handler that reads whether git answered"
        path, old, new, _ = REFUSED_PLACE_LOOKUPS[name]
        files = _modules({path: (old, new)})
        self.assertEqual([], self.problems(files, allowed={GUARD: {"view": "git's answer decides"}}))
        self.assertEqual(
            [
                "tests/validation/one_helper.py: PLACE_LOOKUP_SANCTIONS gives tools/guard.py view no reason; state why "
                "it is held to neither rule"
            ],
            self.problems(files, allowed={GUARD: {"view": " "}}),
        )
        self.assertEqual(
            [
                "tests/validation/one_helper.py: PLACE_LOOKUP_SANCTIONS names tools/guard.py reached, which neither "
                "treats a place lookup's GitError kinds apart nor lets one escape; remove the entry"
            ],
            self.problems(_modules(), allowed={GUARD: {"reached": "no longer needed"}}),
        )

    def test_a_lookup_its_module_does_not_define_fails(self) -> None:
        lookups = {**LOOKUPS, GUARD: frozenset({"lookup", "gone"})}
        self.assertEqual(
            [
                "tests/validation/one_helper.py: PLACE_LOOKUPS names tools/guard.py gone, which its module does not "
                "define; remove it or name the lookup by its scope"
            ],
            self.problems(_modules(), lookups),
        )

    def test_the_attributes_and_classes_read_from_git_error_include_those_that_tell_its_kinds(self) -> None:
        # Pinned against independent literals: the attributes a handler would read to tell one GitError from another,
        # and the names it is caught by.
        self.assertEqual({"kind", "answered", "returncode", "origin_failed"}, KIND_ATTRIBUTES)
        self.assertNotIn("args", KIND_ATTRIBUTES)
        self.assertLessEqual({"git_client.GitError", "RuntimeError", "Exception", "BaseException"}, CATCHING)

    def test_each_stated_limit_reads_as_its_docstring_says(self) -> None:
        limits: dict[str, tuple[str, str, list[str]]] = {
            # A lookup handed on as a value is not followed.
            "a lookup handed on as a value": (
                "def view(path):",
                "def many(paths):\n    return list(map(lookup, paths))\n\n\ndef view(path):",
                [],
            ),
            # A kind read through a function the handler hands the error to is not read.
            "a kind read through a function": (
                "    except (GitError, OSError):\n        return None",
                "    except GitError as exc:\n        return classify(exc)",
                [],
            ),
            # A class tested other than by isinstance is not read.
            "a class tested by type": (
                "isinstance(exc, (TimeoutError, GitError))",
                "type(exc) is GitError",
                [],
            ),
            # A kind read through an alias of the caught error is not read.
            "a kind read through an alias": (
                "    except (GitError, OSError):\n        return None",
                "    except GitError as exc:\n        error = exc\n        return error.kind",
                [],
            ),
            # A handler that raises a new GitError from the one it caught converts every kind alike.
            "a new GitError raised from the caught one": (
                "    except (GitError, OSError):\n        return None",
                "    except GitError as exc:\n        raise GitError('unknown place') from exc",
                [],
            ),
            # Whether a handler tells kinds apart is read, never what it makes of the place.
            "a place read as no hub": (
                "    except (GitError, OSError):\n        return None",
                "    except GitError:\n        hub = None\n        return hub",
                [],
            ),
            # A git call a scope makes itself, rather than through a lookup, is not followed.
            "a git call of a scope's own": (
                "def view(path):",
                "def own(path):\n    return CLIENT.output(path)\n\n\ndef view(path):",
                [],
            ),
            # A handler for a class derived from GitError alone is read as not catching, so the error is followed to the
            # next handler, here guard's catch-all.
            "a handler for a class derived from GitError": (
                "class Judge:\n    def judge(self, path):\n        return reached(path)",
                "class Gone(GitError):\n    pass\n\n\nclass Judge:\n    def judge(self, path):\n        try:\n"
                "            return reached(path)\n        except Gone:\n            return None",
                [],
            ),
            # An error kept and raised after its handler is read as caught.
            "an error raised after its handler": (
                "    except (GitError, OSError):\n        return None",
                "    except GitError as exc:\n        failure = exc\n    raise failure",
                [],
            ),
        }
        for name, (old, new, expected) in limits.items():
            with self.subTest(name):
                self.assertEqual(expected, self.problems(_modules({GUARD: (old, new)})))

    def test_a_suppressed_error_and_a_method_more_than_one_class_defines_are_not_followed(self) -> None:
        # contextlib.suppress is not read as catching, so the scope reads as letting the error out.
        suppressed = _modules(
            {
                GUARD: (
                    "    try:\n        return lookup(path)\n    except (GitError, OSError):\n        return None",
                    "    with contextlib.suppress(GitError):\n        return lookup(path)",
                )
            }
        )
        problems = self.problems(suppressed)
        self.assertEqual(1, len(problems), problems)
        self.assertIn("lets the GitError of a place lookup that view raises escape", problems[0])
        # A second class defining denied leaves judge.denied unfollowed, so Judge.denied reads as an entry point and
        # guard's handler is never reached.
        shared = _modules(
            {
                GUARD: (
                    "\n\ndef guard(path):",
                    "\n\nclass Other:\n    def denied(self, path):\n        return path\n\n\ndef guard(path):",
                )
            }
        )
        problems = self.problems(shared)
        self.assertEqual(1, len(problems), problems)
        self.assertIn("Judge.denied, which nothing in tools/guard.py or tools/trees.py calls", problems[0])

    def test_a_call_from_a_module_place_lookups_does_not_name_is_not_read(self) -> None:
        files = {**_modules(), "tools/other.py": "import guard\n\n\ndef run():\n    return guard.lookup('.')\n"}
        self.assertEqual([], self.problems(files))


# Each form the paragraph names that the closing-reference rule refuses, as a module's text, and what the refusal says.
OWN_PATTERN = "matches a closing keyword before an issue reference with its own pattern"
COMPARED = "compares text with a closing keyword before an issue reference"
ONE_TEXT = "reads one text's closing references"
NOT_BOTH = "no title or no body"
ONE_PATTERN_TEXT = "matches CLOSING_REFERENCE over one text"
READER_IMPORT = "from closing_references import pull_request_references\n\n\n"
REFUSED_CLOSING: dict[str, tuple[str, str]] = {
    "a compiled pattern": ('import re\nP = re.compile(r"(?i)\\bcloses?\\s+#(\\d+)")\n', OWN_PATTERN),
    "a search": (
        'import re\n\n\ndef f(body):\n    return re.search(r"fix(?:e[sd])?:?\\s*#(\\d+)", body)\n',
        OWN_PATTERN,
    ),
    "a pattern a module-level name holds": (
        'import re\nPATTERN = r"resolved #\\d+"\n\n\ndef f(text):\n    return re.findall(PATTERN, text)\n',
        OWN_PATTERN,
    ),
    "re imported under another name": ('import re as regex\nP = regex.compile(r"Closes #(\\d+)")\n', OWN_PATTERN),
    "a function imported from re": (
        'from re import finditer\n\n\ndef f(t):\n    return finditer(r"closes #(\\d+)", t)\n',
        OWN_PATTERN,
    ),
    "a reference in another repository": (
        'import re\nP = re.compile(r"closes [\\w.-]+/[\\w.-]+#(\\d+)")\n',
        OWN_PATTERN,
    ),
    "an autolinked reference": ('import re\nP = re.compile(r"fixes GH-(\\d+)")\n', OWN_PATTERN),
    "an issue's URL": ('import re\nP = re.compile(r"closes https://github\\.com/\\S+/issues/(\\d+)")\n', OWN_PATTERN),
    "a pull request's URL": (
        'import re\nP = re.compile(r"closes https://github\\.com/\\S+/pull/(\\d+)")\n',
        OWN_PATTERN,
    ),
    "a pattern re.escape reads": ('import re\nP = re.escape("Closes #")\n', OWN_PATTERN),
    "a str method": ('def f(line):\n    return line.startswith("Closes #")\n', COMPARED),
    "an in comparison": ('def f(text):\n    return "fixes #" in text.lower()\n', COMPARED),
    "an equality": ('def f(text):\n    return text == "Resolves #1"\n', COMPARED),
    "a member of a tuple compared": ('def f(text):\n    return text in ("closes #1", "other")\n', COMPARED),
    "a str method given a separator after the keyword": ('def f(text):\n    return text.find("closes: #")\n', COMPARED),
    "the one-text reader": (
        "from closing_references import closing_references\n\n\ndef f(body):\n    return closing_references(body)\n",
        ONE_TEXT,
    ),
    "the one-text reader through its module": (
        "import closing_references\n\n\ndef f(t):\n    return closing_references.closing_references(t)\n",
        ONE_TEXT,
    ),
    "a literal None for the title": (
        f"{READER_IMPORT}def f(b):\n    return pull_request_references(None, b)\n",
        NOT_BOTH,
    ),
    "a body alone": (
        f"{READER_IMPORT}def f(b):\n    return pull_request_references(b)\n",
        NOT_BOTH,
    ),
    "a title alone by name": (
        f"{READER_IMPORT}def f(t):\n    return pull_request_references(title=t)\n",
        NOT_BOTH,
    ),
    "unpacked parts": (
        f"{READER_IMPORT}def f(p):\n    return pull_request_references(*p)\n",
        NOT_BOTH,
    ),
    "a title beside unpacked parts": (
        f"{READER_IMPORT}def f(t, rest):\n    return pull_request_references(t, *rest)\n",
        NOT_BOTH,
    ),
    "a joined text with no body": (
        "from closing_references import pull_request_text\n\n\ndef f(t):\n    return pull_request_text(t, None)\n",
        NOT_BOTH,
    ),
    "the reference pattern over a body": (
        "from closing_references import CLOSING_REFERENCE\n\n\ndef f(b):\n    return CLOSING_REFERENCE.finditer(b)\n",
        ONE_PATTERN_TEXT,
    ),
    "the reference pattern over a title and a body joined by hand": (
        "from closing_references import CLOSING_REFERENCE\n\n\ndef f(t, b):\n"
        "    return CLOSING_REFERENCE.search(t + '\\n' + b)\n",
        ONE_PATTERN_TEXT,
    ),
}
# Modules the rule passes: readers through the helper, and text that names a keyword but reads no closing reference.
PASSED_CLOSING: dict[str, str] = {
    "both parts by position": "from closing_references import pull_request_references as r\n\n\ndef f(t, b):\n"
    "    return r(t, b)\n",
    "both parts by name": "import closing_references\n\n\ndef f(t, b):\n"
    "    return closing_references.pull_request_references(body=b, title=t)\n",
    "the reference pattern over a joined title and body": "from closing_references import CLOSING_REFERENCE, "
    "pull_request_text\n\n\ndef f(t, b):\n    return CLOSING_REFERENCE.finditer(pull_request_text(t, b))\n",
    "the helper's title and body checks": "from closing_references import stray_closing_lines, "
    "title_holds_closing_reference\n\n\ndef f(t, b, m):\n    return title_holds_closing_reference(t), "
    "stray_closing_lines(b, m)\n",
    "a writer of a closing line": 'def f(number):\n    return f"Closes #{number}\\n"\n',
    "a message naming a closing line": 'MESSAGE = "name the issue in a `Closes #<n>` line of its own"\n',
    "a title type": 'import re\nP = re.compile(r"fix(\\([^()]*\\))?: ")\n',
    "a keyword that stops before the reference": 'import re\nP = re.compile(r"resolve\\s+([\\"\']?)")\n',
    "a state compared": 'def f(state):\n    return state == "closed"\n',
    "a pattern that does not compile": 'import re\nP = re.compile(r"closes #(")\n',
    "a pattern that matches with the keyword replaced": 'import re\nP = re.compile(r"(?:closes|\\S+) #(\\d+)")\n',
}


class ClosingReferenceTests(unittest.TestCase):
    def test_each_refused_form_fails(self) -> None:
        for name, (text, expected) in REFUSED_CLOSING.items():
            with self.subTest(name):
                problems = module_closing_problems("m.py", ast.parse(text))
                self.assertEqual(1, len(problems), problems)
                self.assertIn(expected, problems[0])

    def test_each_passed_form_passes(self) -> None:
        for name, text in PASSED_CLOSING.items():
            with self.subTest(name):
                self.assertEqual([], module_closing_problems("m.py", ast.parse(text)))

    def test_a_refusal_names_the_module_line_and_scope(self) -> None:
        text = (
            'import re\n\n\nclass Reader:\n    def read(self, body):\n        return re.search(r"closes #(\\d+)", b)\n'
        )
        self.assertEqual(
            [f"m.py:6 Reader.read {OWN_PATTERN}; read closing references through {HELPER}"],
            module_closing_problems("m.py", ast.parse(text)),
        )

    def test_each_sample_reference_is_one_the_helper_reads_after_a_keyword(self) -> None:
        for reference in SAMPLE_REFERENCES:
            with self.subTest(reference):
                self.assertEqual(
                    [1], [found.number for found in closing_references.closing_references(f"Closes {reference}")]
                )

    def test_a_pattern_function_is_read_by_its_first_parameter_and_by_a_name_bound_to_one(self) -> None:
        source = (
            "def prefixmatch(pattern, string):\n    pass\n\n\nmatch = prefixmatch\nalias = other\n\n\n"
            "def purge():\n    pass\n\n\ndef other(text, pattern):\n    pass\n\n\ndef _hidden(pattern):\n    pass\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "module.py"
            path.write_text(source, encoding="utf-8")
            exported = ("prefixmatch", "match", "alias", "purge", "other")
            self.assertEqual({"prefixmatch", "match"}, pattern_functions(path, exported))

    def test_an_error_s_attributes_are_read_from_its_class_definition(self) -> None:
        source = (
            "class Other:\n    def kind(self):\n        pass\n\n\n"
            "class Failure(RuntimeError):\n    def __init__(self, message, kind):\n"
            "        super().__init__(message)\n        self.kind: str = kind\n        self.code = 1\n"
            "        other.ignored = 2\n\n    @property\n    def answered(self):\n        return self.kind\n\n"
            "    def _private(self):\n        pass\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "module.py"
            path.write_text(source, encoding="utf-8")
            self.assertEqual({"kind", "code", "answered"}, error_attributes(path, "Failure"))

    def test_the_reference_pattern_is_the_one_the_helper_binds(self) -> None:
        self.assertEqual("closing_references.CLOSING_REFERENCE", REFERENCE_PATTERN)
        self.assertIsInstance(closing_references.CLOSING_REFERENCE, re.Pattern)

    def test_the_functions_of_re_that_take_a_pattern_are_read_from_re(self) -> None:
        # Pinned against independent literals: the calls a module's own pattern is read at.
        self.assertLessEqual(
            {"re.compile", "re.search", "re.match", "re.fullmatch", "re.findall", "re.finditer", "re.sub"},
            PATTERN_FUNCTIONS,
            REFERENCE_PATTERN,
        )
        self.assertNotIn("re.purge", PATTERN_FUNCTIONS)

    def test_each_stated_limit_reads_as_its_docstring_says(self) -> None:
        limits = {
            # A pattern built from names or formatted values is not read.
            "a formatted pattern": 'import re\nK = "closes"\nP = re.compile(rf"{K}\\s+#(\\d+)")\n',
            # A pattern that needs text before the keyword is not read.
            "text before the keyword": 'import re\nP = re.compile(r"^- closes #(\\d+)")\n',
            # A keyword compared alone is not read.
            "a keyword compared alone": 'def f(word):\n    return word == "closes"\n',
            # A keyword written as a character class is not read.
            "a keyword as a character class": 'import re\nP = re.compile(r"[Cc]loses #(\\d+)")\n',
            # A pattern bound to a name inside a function, or given by keyword, is not read.
            "a pattern bound in a function": 'import re\n\n\ndef f(t):\n    p = r"closes #(\\d+)"\n'
            "    return re.search(p, t)\n",
            "a pattern given by keyword": 'import re\nP = re.compile(pattern=r"closes #(\\d+)")\n',
            # A pattern joined with + is not read.
            "a pattern joined with +": 'import re\nP = re.compile(r"closes " + r"#(\\d+)")\n',
            # A title or a body given as a literal empty string is read as given.
            "an empty literal for the body": f"{READER_IMPORT}def f(t):\n    return pull_request_references(t, '')\n",
            # The helper's own patterns are read through the helper.
            "the helper's own pattern": "from closing_references import BARE_CLOSING_LINE\n\n\ndef f(line):\n"
            "    return BARE_CLOSING_LINE.fullmatch(line)\n",
        }
        for name, text in limits.items():
            with self.subTest(name):
                self.assertEqual([], module_closing_problems("m.py", ast.parse(text)))

    def test_the_helper_suites_and_scripts_are_not_read(self) -> None:
        reader = 'import re\nP = re.compile(r"closes #(\\d+)")\n'
        files = {
            HELPER: reader,
            "skills/x/scripts/test_reader.py": reader,
            "skills/x/scripts/reader.sh": 'grep -i "closes #" "$1"\n',
            "docs/reader.md": f"```python\n{reader}```\n",
            "skills/x/scripts/reader.py": reader,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_fixture_tree(root, files)
            problems = closing_reference_problems(root, [root / name for name in files])
        self.assertEqual(
            [f"skills/x/scripts/reader.py:2 P {OWN_PATTERN}; read closing references through {HELPER}"], problems
        )

    def test_a_module_that_does_not_parse_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_fixture_tree(root, {"tools/broken.py": textwrap.dedent("def f(:\n")})
            problems = closing_reference_problems(root, [root / "tools/broken.py"])
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("tools/broken.py"), problems)


# A module that runs git status itself, in each form the status rule refuses.
REFUSED_STATUS = {
    "a list given to a client method": 'def f(git, d):\n    return git.names(["status", "--porcelain"], directory=d)\n',
    "a tuple given to a wrapper": "def f(git, d):\n    return run(git, d, ('status', '-z'))\n",
    "after git's own options": 'def f(git, d):\n    return git.output(["-c", "x=y", "-C", d, "status"])\n',
    "a name bound to the subcommand": 'S = "status"\n\n\ndef f(git):\n    return git.output([S, "-z"])\n',
    "at module level": 'LISTED = git.output(["--no-optional-locks", "status"])\n',
    "one at a time to a wrapper": 'def f(services, root):\n    return git(services, root, "status", "--porcelain")\n',
    "one at a time to a method": 'class F:\n    def f(self):\n        return self.git("status", "--porcelain")\n',
    "one at a time after git's options": 'def f(fixture):\n    return fixture.git("-c", "x=y", "status", "-z")\n',
    "before a starred element": 'def f(git, o):\n    return git.output(["status", "-z", *o])\n',
}
# Modules that read status through the helper, or give a call arguments whose subcommand is not status.
PASSED_STATUS = {
    "through the helper": "import git_worktrees\n\n\ndef f(git, d):\n    return git_worktrees.changes(git, d)\n",
    "another subcommand": 'def f(git):\n    return git.output(["rev-parse", "HEAD"])\n',
    "status as an option's value": 'def f(git):\n    return git.output(["-c", "status", "log"])\n',
    "status after the subcommand": 'def f(git):\n    return git.output(["submodule", "status"])\n',
    "a value in the subcommand's place": 'def f(git, word):\n    return git.output([word, "status"])\n',
    "a field read by its name": 'def f(entry):\n    return entry.get("status")\n',
    "a word given alone before a value": 'def f(s, d, read):\n    return run(s, d, "status", read, "--ignored")\n',
    "another subcommand one at a time": 'def f(fixture):\n    return fixture.git("submodule", "status", "-q")\n',
}
SANCTIONED_READER = 'STATUS_READ_ALLOWED = {"f": "a reason"}\n\n\ndef f(git):\n    return git.output(["status"])\n'


class StatusTests(unittest.TestCase):
    def problems(self, files: Mapping[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_fixture_tree(root, dict(files))
            return status_problems(root, [root / name for name in files])

    def test_each_refused_form_fails(self) -> None:
        for name, text in REFUSED_STATUS.items():
            with self.subTest(name):
                problems = self.problems({"tools/reader.py": text})
                self.assertEqual(1, len(problems), problems)
                self.assertIn(f"runs git status itself; read a checkout's status through {STATUS_HELPER}", problems[0])

    def test_each_passed_form_passes(self) -> None:
        for name, text in PASSED_STATUS.items():
            with self.subTest(name):
                self.assertEqual([], self.problems({"tools/reader.py": text}))

    def test_a_refusal_names_the_module_line_and_scope(self) -> None:
        text = 'class Hub:\n    def clean(self, git):\n        return git.names(["status"])\n'
        problems = self.problems({".claude/skills/x/scripts/hub.py": text})
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith(".claude/skills/x/scripts/hub.py:3 Hub.clean runs git status"), problems)

    def test_the_helper_suites_and_scripts_are_not_read(self) -> None:
        reader = 'def f(git):\n    return git.output(["status"])\n'
        files = {
            STATUS_HELPER: reader,
            "skills/x/scripts/test_reader.py": reader,
            "skills/x/scripts/reader.sh": "git status --porcelain\n",
            "docs/reader.md": f"```python\n{reader}```\n",
            "skills/x/scripts/reader.py": reader,
        }
        problems = self.problems(files)
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("skills/x/scripts/reader.py:2 f runs git status"), problems)

    def test_an_allowance_beside_the_code_sanctions_its_scope_and_one_that_holds_nothing_or_no_reason_fails(
        self,
    ) -> None:
        self.assertEqual([], self.problems({"tools/reader.py": SANCTIONED_READER}))
        stale = SANCTIONED_READER.replace('{"f": "a reason"}', '{"f": "a reason", "g": "a reason"}')
        empty = SANCTIONED_READER.replace('"a reason"', '" "')
        self.assertEqual(
            [
                "tools/stale.py: STATUS_READ_ALLOWED allows g, which holds nothing it sanctions; remove the entry",
                "tools/empty.py: STATUS_READ_ALLOWED must map each function, class, or assignment to its reason",
            ],
            [*self.problems({"tools/stale.py": stale}), *self.problems({"tools/empty.py": empty})[:1]],
        )

    def test_each_stated_limit_reads_as_its_docstring_says(self) -> None:
        limits = {
            # Arguments bound to a name first, joined with +, given by keyword, or led by a starred element: not read.
            "bound to a name first": 'def f(git):\n    arguments = ["status"]\n    return git.output(arguments)\n',
            "joined with +": 'def f(git):\n    return git.output(["status"] + ["-z"])\n',
            "given by keyword": 'def f(git):\n    return git.output(arguments=["status"])\n',
            "led by a starred element": 'def f(git, o):\n    return git.output([*o, "status"])\n',
            # A run of strings given one at a time ends at the first argument that is not one.
            "a value inside the run": 'def f(fixture, path):\n    return fixture.git("-C", path, "status", "-z")\n',
            # A run with no option after status is not read.
            "status given alone": 'def f(fixture):\n    return fixture.git("status")\n',
            "alone after git's options": 'def f(fixture):\n    return fixture.git("--no-pager", "status")\n',
            # An argument list that names the program first reads it as its subcommand.
            "the program named first": 'def f(run):\n    return run(["git", "status"])\n',
        }
        for name, text in limits.items():
            with self.subTest(name):
                self.assertEqual([], self.problems({"tools/reader.py": text}))

    def test_a_run_of_strings_given_to_another_call_is_refused_until_its_scope_states_why(self) -> None:
        # A stated limit: a call that is not git's, given `status` and then a word that starts with `-`, reads as git's.
        for name, call in {
            "a default": 'entry.get("status", "-")',
            "an option": 'parser.add_argument("status", "-s")',
        }.items():
            with self.subTest(name):
                text = f"def f(entry, parser):\n    return {call}\n"
                problems = self.problems({"tools/reader.py": text})
                self.assertEqual(1, len(problems), problems)
                self.assertIn("f runs git status itself", problems[0])
                sanctioned = 'STATUS_READ_ALLOWED = {"f": "not a git call"}\n\n\n' + text
                self.assertEqual([], self.problems({"tools/reader.py": sanctioned}))

    def test_the_subcommand_and_allowance_are_the_ones_the_rule_names(self) -> None:
        # Pinned against independent literals: the helper, the subcommand refused outside it, and the allowance.
        self.assertEqual("skills/ejryan89-skill-core/scripts/git_worktrees.py", STATUS_HELPER)
        self.assertEqual("status", one_helper.STATUS)
        self.assertEqual("STATUS_READ_ALLOWED", one_helper.STATUS_ALLOWANCE)

    def test_every_module_s_refusals_are_reported(self) -> None:
        reader = 'def f(git):\n    return git.output(["status"])\n'
        problems = self.problems({"tools/first.py": reader, "tools/second.py": reader})
        self.assertEqual(["tools/first.py:2", "tools/second.py:2"], sorted(problem.split()[0] for problem in problems))

    def test_with_no_files_given_the_rule_reads_what_a_commit_could_include(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            write_fixture_tree(root, {"tools/reader.py": 'def f(git):\n    return git.output(["status"])\n'})
            problems = status_problems(root)
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("tools/reader.py:2 f runs git status"), problems)

    def test_a_module_that_does_not_parse_is_named(self) -> None:
        problems = self.problems({"tools/broken.py": "def f(:\n"})
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("tools/broken.py"), problems)


# Modules that read a pull request's files only through the helper and check its listing as a caller must.
PASSED_PULL_FILES = {
    "both checks": (
        "def f(github, pull):\n"
        "    listing = github.list_pull_files(pull.repository, pull.number)\n"
        "    if listing.moved_from(pull.head) or listing.cut:\n"
        "        raise ValueError(pull)\n"
        "    return listing.files\n"
    ),
    "annotated binding": (
        "def f(github, head):\n"
        "    listing: object = github.list_pull_files('o/r', 1)\n"
        "    return listing.files if not listing.cut and not listing.moved_from(head) else None\n"
    ),
    "checked through an assignment expression": (
        "def f(github, head):\n"
        "    if (listing := github.list_pull_files('o/r', 1)).cut or listing.moved_from(head):\n"
        "        return None\n"
        "    return listing.files\n"
    ),
    "another endpoint": 'def f(github, n):\n    return github.api_json(f"repos/o/r/pulls/{n}/reviews")\n',
    "files of another kind": 'def f(github, n):\n    return github.api_json(f"repos/o/r/pulls/{n}/filesystem")\n',
}
# Modules that read a pull request's files themselves, or read the helper's listing without checking it.
REFUSED_PULL_FILES = {
    "f-string endpoint": (
        'def f(github, r, n):\n    return github.api_json(f"repos/{r}/pulls/{n}/files?per_page=100")\n',
        "spells GitHub's files endpoint of a pull request",
    ),
    "literal endpoint": (
        'ENDPOINT = "repos/o/r/pulls/7/files"\n',
        "spells GitHub's files endpoint of a pull request",
    ),
    "no head check": (
        "def f(github):\n"
        "    listing = github.list_pull_files('o/r', 1)\n"
        "    return [] if listing.cut else listing.files\n",
        "without checking moved_from",
    ),
    "no count check": (
        "def f(github, head):\n"
        "    listing = github.list_pull_files('o/r', 1)\n"
        "    return None if listing.moved_from(head) else listing.files\n",
        "without checking cut",
    ),
    "neither check": (
        "def f(github):\n    listing = github.list_pull_files('o/r', 1)\n    return listing.files\n",
        "without checking cut, moved_from",
    ),
    "handed on unbound": (
        "def f(github, read):\n    return read(github.list_pull_files('o/r', 1))\n",
        "without binding it to a name its checks read",
    ),
    "checks of another name": (
        "def f(github, other, head):\n"
        "    listing = github.list_pull_files('o/r', 1)\n"
        "    if other.cut or other.moved_from(head):\n"
        "        raise ValueError(head)\n"
        "    return listing.files\n",
        "without checking cut, moved_from",
    ),
    "checks in another scope": (
        "def check(listing, head):\n"
        "    return listing.cut or listing.moved_from(head)\n\n\n"
        "def f(github, head):\n"
        "    listing = github.list_pull_files('o/r', 1)\n"
        "    return None if check(listing, head) else listing.files\n",
        "without checking cut, moved_from",
    ),
}
SANCTIONED_PULL_FILES = 'def f(client, n):\n    return client.json(["api", f"repos/o/r/pulls/{n}/files"])\n'


class PullFilesTests(unittest.TestCase):
    def problems(self, files: Mapping[str, str], sanctions: Mapping[str, Mapping[str, str]] | None = None) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_fixture_tree(root, dict(files))
            return pull_files_problems(root, [root / name for name in files], {} if sanctions is None else sanctions)

    def test_each_refused_form_fails(self) -> None:
        for name, (text, refusal) in REFUSED_PULL_FILES.items():
            with self.subTest(name):
                problems = self.problems({"skills/x/scripts/reader.py": text})
                self.assertEqual(1, len(problems), problems)
                self.assertIn(refusal, problems[0])
                self.assertIn(f"read a pull request's files through {PULL_FILES_HELPER}'s list_pull_files", problems[0])

    def test_each_passed_form_passes(self) -> None:
        for name, text in PASSED_PULL_FILES.items():
            with self.subTest(name):
                self.assertEqual([], self.problems({"skills/x/scripts/reader.py": text}))

    def test_a_refusal_names_the_module_line_and_scope(self) -> None:
        text = 'class Reader:\n    def files(self, n):\n        return self.get(f"repos/o/r/pulls/{n}/files")\n'
        problems = self.problems({".claude/skills/x/scripts/reader.py": text})
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(
            problems[0].startswith(".claude/skills/x/scripts/reader.py:3 Reader.files spells GitHub's files endpoint"),
            problems,
        )

    def test_the_helper_and_suites_are_not_read(self) -> None:
        reader = 'def f(github, n):\n    return github.api_json(f"repos/o/r/pulls/{n}/files")\n'
        files = {
            PULL_FILES_HELPER: reader,
            "skills/x/scripts/test_reader.py": reader,
            "skills/x/scripts/reader.sh": "gh api repos/o/r/pulls/1/files\n",
            "skills/x/scripts/reader.ps1": "gh api repos/o/r/pulls/1/files\n",
            ".github/workflows/reader.yml": (
                "on: push\njobs:\n  r:\n    steps:\n      - run: gh api repos/o/r/pulls/1/files\n"
            ),
            "docs/reader.md": f"```python\n{reader}```\n",
            "skills/x/scripts/reader.py": reader,
        }
        problems = self.problems(files)
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("skills/x/scripts/reader.py:2 f spells"), problems)

    def test_a_sanctioned_scope_passes_and_a_stale_or_bare_entry_fails(self) -> None:
        files = {"tools/reader.py": SANCTIONED_PULL_FILES}
        self.assertEqual([], self.problems(files, {"tools/reader.py": {"f": "a reason"}}))
        self.assertEqual(
            [
                f"{POLICY}: PULL_FILES_SANCTIONS names tools/reader.py g, which reads no pull request's files itself; "
                "remove the entry",
                f"{POLICY}: PULL_FILES_SANCTIONS names tools/gone.py f, which reads no pull request's files itself; "
                "remove the entry",
            ],
            self.problems(files, {"tools/reader.py": {"f": "a reason", "g": "a reason"}, "tools/gone.py": {"f": "x"}}),
        )
        self.assertEqual(
            [
                f"{POLICY}: PULL_FILES_SANCTIONS gives tools/reader.py f no reason; state why it reads a pull "
                "request's files itself"
            ],
            self.problems(files, {"tools/reader.py": {"f": " "}}),
        )

    def test_the_repository_s_sanctions_each_hold_a_scope_with_a_reason(self) -> None:
        # The shepherd's every_file pages the endpoint through ejryan89-skill-core's gh client and checks both the
        # count and the head; the whole-repository check holds that it still reads the endpoint.
        self.assertEqual(
            {".claude/skills/shepherd-pull-requests/scripts/shepherd_pull_requests.py": ["every_file"]},
            {name: sorted(scopes) for name, scopes in one_helper.PULL_FILES_SANCTIONS.items()},
        )

    def test_each_stated_limit_reads_as_its_docstring_says(self) -> None:
        limits = {
            # An endpoint joined with + or built by format() is not read.
            "joined with +": 'def f(n):\n    return "repos/o/r/pulls/" + str(n) + "/files"\n',
            "built by format": 'def f(n):\n    return "repos/o/r/pulls/{}/{}".format(n, "files")\n',
            # A check anywhere in the scope counts, before the read or in a branch that never runs.
            "checked before the read": (
                "def f(github, listing, head):\n"
                "    if listing.cut or listing.moved_from(head):\n"
                "        return None\n"
                "    listing = github.list_pull_files('o/r', 1)\n"
                "    return listing.files\n"
            ),
        }
        for name, text in limits.items():
            with self.subTest(name):
                self.assertEqual([], self.problems({"tools/reader.py": text}))
        bound = "def f(self, github):\n    self.listing = github.list_pull_files('o/r', 1)\n"
        self.assertEqual(1, len(self.problems({"tools/reader.py": bound})), "an attribute is no name its checks read")

    def test_the_checks_are_the_helper_s_own_and_the_names_are_the_ones_the_rule_names(self) -> None:
        # Pinned against independent literals: the helper, its reader, and the checks of its answer.
        self.assertEqual("skills/ejryan89-code-review-core/scripts/review_github.py", PULL_FILES_HELPER)
        self.assertEqual("list_pull_files", one_helper.PULL_FILES_READER)
        self.assertEqual(frozenset({"cut", "moved_from"}), one_helper.LISTING_CHECKS)

    def test_each_further_stated_limit_reads_as_its_docstring_says(self) -> None:
        limits = {
            # /files after a value that holds the rest, an endpoint built with % or str.join, and gh's and GraphQL's
            # own readers of a pull request's files are not read.
            "after a value": 'def f(pull):\n    return f"{pull}/files"\n',
            "built with %": 'def f(n):\n    return "repos/o/r/pulls/%d/files" % n\n',
            "built with str.join": 'def f(n):\n    return "/".join(["repos/o/r/pulls", str(n), "files"])\n',
            "gh's json field": 'ARGUMENTS = ["pr", "view", "1", "--json", "files"]\n',
            "graphql's connection": 'QUERY = "pullRequest(number: 1) { files(first: 100) { nodes { path } } }"\n',
        }
        for name, text in limits.items():
            with self.subTest(name):
                self.assertEqual([], self.problems({"tools/reader.py": text}))

    def test_a_reader_called_by_its_bare_name_or_after_another_call_is_read_as_its_callee_names_it(self) -> None:
        bare = "def f():\n    listing = list_pull_files('o/r', 1)\n    return listing.files\n"
        problems = self.problems({"tools/reader.py": bare})
        self.assertEqual(1, len(problems), problems)
        self.assertIn("f reads list_pull_files's listing without checking cut, moved_from", problems[0])
        called = "def f(make, head):\n    return make()(head)\n"
        self.assertEqual([], self.problems({"tools/reader.py": called}))

    def test_a_check_of_an_attribute_s_attribute_is_no_check_of_a_name(self) -> None:
        text = (
            "def f(self, github, head):\n"
            "    listing = github.list_pull_files('o/r', 1)\n"
            "    return self.listing.cut or self.listing.moved_from(head) or listing.files\n"
        )
        problems = self.problems({"tools/reader.py": text})
        self.assertEqual(1, len(problems), problems)
        self.assertIn("without checking cut, moved_from", problems[0])

    def test_every_unchecked_read_in_a_module_is_reported_after_a_checked_one(self) -> None:
        checked = PASSED_PULL_FILES["both checks"]
        unchecked = "\n\ndef g(github):\n    listing = github.list_pull_files('o/r', 1)\n    return listing.files\n"
        problems = self.problems({"tools/reader.py": checked + unchecked})
        self.assertEqual(1, len(problems), problems)
        self.assertIn("g reads list_pull_files's listing without checking", problems[0])

    def test_with_no_files_given_the_rule_reads_what_a_commit_could_include(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            write_fixture_tree(root, {"tools/reader.py": REFUSED_PULL_FILES["literal endpoint"][0]})
            problems = pull_files_problems(root, sanctions={})
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("tools/reader.py:1 ENDPOINT spells"), problems)

    def test_the_shepherd_s_suite_holds_the_test_its_sanction_names(self) -> None:
        # The sanction's reason rests on the shepherd's own test of a short listing and a moved head.
        suite, owner, test = one_helper.SHEPHERD_FILES_TEST.split("::")
        tree = ast.parse((REPOSITORY_ROOT / suite).read_text(encoding="utf-8"))
        defined = {
            (node.name, member.name)
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            for member in node.body
            if isinstance(member, ast.FunctionDef)
        }
        self.assertIn((owner, test), defined)
        reason = one_helper.PULL_FILES_SANCTIONS[
            ".claude/skills/shepherd-pull-requests/scripts/shepherd_pull_requests.py"
        ]["every_file"]
        self.assertIn(one_helper.SHEPHERD_FILES_TEST, reason)

    def test_the_helper_spells_the_endpoint_the_rule_refuses_elsewhere(self) -> None:
        # The rule's endpoint is the one the helper reads: read alone, the helper's module would be refused.
        tree = ast.parse((REPOSITORY_ROOT / PULL_FILES_HELPER).read_text(encoding="utf-8"))
        problems = one_helper.module_pull_files_problems(tree)
        self.assertIn("GitHubClient.list_pull_files", [scope for _, scope, _ in problems])

    def test_a_module_that_does_not_parse_is_named(self) -> None:
        problems = self.problems({"tools/broken.py": "def f(:\n"})
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("tools/broken.py"), problems)


# Modules that read a snapshot's file by its repository path only through the helper.
PASSED_SNAPSHOT_PATHS = {
    "through the helper": (
        "from diff_paths import snapshot_path\n\n\n"
        "def f(root, relative):\n"
        "    if snapshot_path(root, relative) is None:\n"
        "        raise ValueError(relative)\n"
        "    return root.joinpath(relative)\n"
    ),
    "judged but never joined": (
        "from diff_paths import unsafe_snapshot_name\n\n\n"
        "def f(relative):\n    return not unsafe_snapshot_name(relative)\n"
    ),
    "another name joined": (
        "from diff_paths import unsafe_snapshot_name\n\n\n"
        "def f(root, relative, held):\n"
        "    if unsafe_snapshot_name(relative):\n"
        "        raise ValueError(relative)\n"
        "    return root / held\n"
    ),
    "another judge's name": (
        "from other import unsafe_snapshot_name\n\n\ndef f(root, relative):\n"
        "    return None if unsafe_snapshot_name(relative) else root / relative\n"
    ),
}
# Modules that judge a snapshot path and join it themselves.
REFUSED_SNAPSHOT_PATHS = {
    "joinpath": (
        "from pathlib import PurePosixPath\n\nfrom diff_paths import unsafe_snapshot_name\n\n\n"
        "def f(root, relative):\n"
        "    if unsafe_snapshot_name(relative):\n"
        "        raise ValueError(relative)\n"
        "    return root.joinpath(*PurePosixPath(relative).parts)\n"
    ),
    "slash": (
        "import diff_paths\n\n\ndef f(root, relative):\n"
        "    return None if diff_paths.unsafe_snapshot_name(relative) else root / relative\n"
    ),
    "os.path.join": (
        "import os\n\nfrom diff_paths import unsafe_snapshot_name as unsafe\n\n\n"
        "def f(root, relative):\n    return None if unsafe(relative) else os.path.join(root, relative)\n"
    ),
    "posixpath.join": (
        "import posixpath\n\nfrom diff_paths import unsafe_snapshot_name\n\n\n"
        "def f(root, relative):\n"
        "    return None if unsafe_snapshot_name(relative) else posixpath.join(root, relative)\n"
    ),
    "ntpath.join": (
        "import ntpath\n\nimport diff_paths\n\n\n"
        "def f(root, relative):\n"
        "    return None if diff_paths.unsafe_snapshot_name(relative) else ntpath.join(root, relative)\n"
    ),
}
SANCTIONED_SNAPSHOT_PATH = (
    'import diff_paths\n\nSNAPSHOT_PATH_READ_ALLOWED = {"f": "a reason"}\n\n\n'
    "def f(root, relative):\n"
    "    return None if diff_paths.unsafe_snapshot_name(relative) else root / relative\n"
)


class SnapshotPathTests(unittest.TestCase):
    def problems(self, files: Mapping[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_fixture_tree(root, dict(files))
            return snapshot_path_problems(root, [root / name for name in files])

    def test_each_refused_form_fails(self) -> None:
        for name, text in REFUSED_SNAPSHOT_PATHS.items():
            with self.subTest(name):
                problems = self.problems({"skills/x/scripts/reader.py": text})
                self.assertEqual(1, len(problems), problems)
                self.assertIn("f joins relative, a name it judges itself, onto a path", problems[0])
                self.assertIn("through diff_paths.snapshot_path", problems[0])

    def test_each_passed_form_passes(self) -> None:
        for name, text in PASSED_SNAPSHOT_PATHS.items():
            with self.subTest(name):
                self.assertEqual([], self.problems({"skills/x/scripts/reader.py": text}))

    def test_the_helper_and_suites_are_not_read(self) -> None:
        reader = REFUSED_SNAPSHOT_PATHS["slash"]
        files = {
            SNAPSHOT_HELPER: reader,
            "skills/x/scripts/test_reader.py": reader,
            "skills/x/scripts/reader.py": reader,
        }
        problems = self.problems(files)
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("skills/x/scripts/reader.py:5 f joins relative"), problems)

    def test_an_allowance_beside_the_code_sanctions_its_scope_and_one_that_holds_nothing_fails(self) -> None:
        self.assertEqual([], self.problems({"tools/reader.py": SANCTIONED_SNAPSHOT_PATH}))
        stale = SANCTIONED_SNAPSHOT_PATH.replace('{"f": "a reason"}', '{"f": "a reason", "g": "a reason"}')
        self.assertEqual(
            [
                "tools/reader.py: SNAPSHOT_PATH_READ_ALLOWED allows g, which holds nothing it sanctions; "
                "remove the entry"
            ],
            self.problems({"tools/reader.py": stale}),
        )
        blank = SANCTIONED_SNAPSHOT_PATH.replace('"a reason"', '" "')
        self.assertEqual(
            ["tools/reader.py: SNAPSHOT_PATH_READ_ALLOWED must map each function, class, or assignment to its reason"],
            self.problems({"tools/reader.py": blank})[:1],
        )

    def test_each_stated_limit_reads_as_its_docstring_says(self) -> None:
        limits = {
            # A name judged in one scope and joined in another, or joined under another name, is not followed.
            "joined in another scope": (
                "from diff_paths import unsafe_snapshot_name\n\n\n"
                "def judge(relative):\n    return not unsafe_snapshot_name(relative)\n\n\n"
                "def f(root, relative):\n    return root / relative if judge(relative) else None\n"
            ),
            "joined under another name": (
                "from diff_paths import unsafe_snapshot_name\n\n\n"
                "def f(root, relative):\n    path = relative\n"
                "    return None if unsafe_snapshot_name(relative) else root / path\n"
            ),
            # A judge given anything but a name, as an attribute, is not read.
            "an attribute judged": (
                "from diff_paths import unsafe_snapshot_name\n\n\n"
                "def f(root, entry):\n    return None if unsafe_snapshot_name(entry.path) else root / entry.path\n"
            ),
            # A join with no judge at all is not read.
            "a bare join": "def f(root, relative):\n    return root / relative\n",
        }
        for name, text in limits.items():
            with self.subTest(name):
                self.assertEqual([], self.problems({"tools/reader.py": text}))

    def test_the_names_are_the_ones_the_rule_names(self) -> None:
        # Pinned against independent literals: the helper, its judge and reader, and the allowance.
        self.assertEqual("skills/ejryan89-skill-core/scripts/diff_paths.py", SNAPSHOT_HELPER)
        self.assertEqual("diff_paths.unsafe_snapshot_name", one_helper.SNAPSHOT_JUDGE)
        self.assertEqual("diff_paths.snapshot_path", one_helper.SNAPSHOT_READER)
        self.assertEqual("SNAPSHOT_PATH_READ_ALLOWED", one_helper.SNAPSHOT_PATH_ALLOWANCE)

    def test_a_join_of_several_judged_names_names_each(self) -> None:
        text = (
            "from diff_paths import unsafe_snapshot_name\n\n\n"
            "def f(root, folder, name):\n"
            "    if unsafe_snapshot_name(folder) or unsafe_snapshot_name(name):\n"
            "        return None\n"
            "    return root.joinpath(folder, name)\n"
        )
        problems = self.problems({"tools/reader.py": text})
        self.assertEqual(1, len(problems), problems)
        self.assertIn("f joins folder, name, a name it judges itself", problems[0])

    def test_every_module_s_refusals_are_reported(self) -> None:
        reader = REFUSED_SNAPSHOT_PATHS["slash"]
        problems = self.problems({"tools/broken.py": "def f(:\n", "tools/first.py": reader, "tools/second.py": reader})
        self.assertEqual(
            ["tools/broken.py", "tools/first.py:5", "tools/second.py:5"],
            sorted(problem.split()[0] for problem in problems),
        )

    def test_with_no_files_given_the_rule_reads_what_a_commit_could_include(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            write_fixture_tree(root, {"tools/reader.py": REFUSED_SNAPSHOT_PATHS["slash"]})
            problems = snapshot_path_problems(root)
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("tools/reader.py:5 f joins relative"), problems)

    def test_each_further_stated_limit_reads_as_its_docstring_says(self) -> None:
        judged = "from pathlib import Path\n\nfrom diff_paths import unsafe_snapshot_name\n\n\ndef f(root, relative):\n"
        limits = {
            # A join by Path(), by /=, or in an f-string, and a judge given the name by keyword, are not read.
            "Path()": "    return None if unsafe_snapshot_name(relative) else Path(root, relative)\n",
            "/=": (
                "    if unsafe_snapshot_name(relative):\n        return None\n    root /= relative\n    return root\n"
            ),
            "an f-string": '    return None if unsafe_snapshot_name(relative) else f"{root}/{relative}"\n',
            "a keyword": "    return None if unsafe_snapshot_name(path=relative) else root / relative\n",
        }
        for name, body in limits.items():
            with self.subTest(name):
                self.assertEqual([], self.problems({"tools/reader.py": judged + body}))

    def test_a_module_that_does_not_parse_is_named(self) -> None:
        problems = self.problems({"tools/broken.py": "def f(:\n"})
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("tools/broken.py"), problems)


# Modules that build an owner/name pattern on the helper's class, or spell another pattern.
PASSED_REPOSITORIES = {
    "the helper's pattern": (
        "import re\n\nfrom github_remote import OWNER_AND_NAME\n\nPATTERN = re.compile(OWNER_AND_NAME)\n"
    ),
    "the helper's class": (
        'from github_remote import REPOSITORY_CHARACTERS as C\n\nPATTERN = rf"(?:[{C}]+)/[{C}]{{1,100}}"\n'
    ),
    "a name alone": 'NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]*"\n',
    "an owner before other text": 'INTERNAL = r"github\\.com/[A-Za-z0-9_.-]+-internal"\n',
    "another class": 'PAIR = r"[A-Za-z0-9]+/[A-Za-z0-9]+"\n',
    "a negated class": 'PAIR = r"[^A-Za-z0-9_.-]+/[^A-Za-z0-9_.-]+"\n',
}
# Modules that spell an owner/name pattern with their own copy of the class.
REFUSED_REPOSITORIES = {
    "compiled at module level": 'import re\n\nPATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")\n',
    "spelled in another order": 'PATTERN = r"[-.0-9A-Z_a-z]+/[A-Za-z0-9_.-]{1,100}"\n',
    "an escaped slash and a lazy name": 'REMOTE = r"github\\.com/([A-Za-z0-9_.-]+\\/[A-Za-z0-9_.-]+?)(?:\\.git)?"\n',
    "matched in a function": (
        'import re\n\n\ndef f(text):\n    return re.fullmatch("[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text)\n'
    ),
    "capturing groups": 'PATTERN = r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)"\n',
    "non-capturing groups": 'PATTERN = r"(?:[A-Za-z0-9_.-]+)/(?:[A-Za-z0-9_.-]+?)"\n',
    "named groups": 'PATTERN = r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+)"\n',
}
SANCTIONED_REPOSITORY = (
    'REPOSITORY_PATTERN_ALLOWED = {"PATTERN": "a reason"}\n\nPATTERN = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"\n'
)


class RepositoryTests(unittest.TestCase):
    def problems(self, files: Mapping[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_fixture_tree(root, dict(files))
            return repository_problems(root, [root / name for name in files])

    def test_each_refused_form_fails(self) -> None:
        for name, text in REFUSED_REPOSITORIES.items():
            with self.subTest(name):
                problems = self.problems({"skills/x/scripts/reader.py": text})
                self.assertEqual(1, len(problems), problems)
                self.assertIn("spells an owner/name pattern with its own character class", problems[0])
                self.assertIn(
                    f"build an owner/name pattern on {REPOSITORY_HELPER}'s REPOSITORY_CHARACTERS", problems[0]
                )

    def test_each_passed_form_passes(self) -> None:
        for name, text in PASSED_REPOSITORIES.items():
            with self.subTest(name):
                self.assertEqual([], self.problems({"skills/x/scripts/reader.py": text}))

    def test_a_refusal_names_the_module_line_and_scope(self) -> None:
        problems = self.problems({"tools/reader.py": REFUSED_REPOSITORIES["matched in a function"]})
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("tools/reader.py:5 f spells an owner/name pattern"), problems)

    def test_the_helper_and_suites_are_not_read(self) -> None:
        reader = REFUSED_REPOSITORIES["spelled in another order"]
        files = {
            REPOSITORY_HELPER: reader,
            "skills/x/scripts/test_reader.py": reader,
            "skills/x/scripts/reader.py": reader,
        }
        problems = self.problems(files)
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("skills/x/scripts/reader.py:1 PATTERN spells"), problems)

    def test_an_allowance_beside_the_code_sanctions_its_scope_and_one_that_holds_nothing_fails(self) -> None:
        self.assertEqual([], self.problems({"tools/reader.py": SANCTIONED_REPOSITORY}))
        stale = SANCTIONED_REPOSITORY.replace('{"PATTERN": "a reason"}', '{"PATTERN": "a reason", "g": "a reason"}')
        self.assertEqual(
            [
                "tools/reader.py: REPOSITORY_PATTERN_ALLOWED allows g, which holds nothing it sanctions; "
                "remove the entry"
            ],
            self.problems({"tools/reader.py": stale}),
        )
        blank = SANCTIONED_REPOSITORY.replace('"a reason"', '" "')
        self.assertEqual(
            ["tools/reader.py: REPOSITORY_PATTERN_ALLOWED must map each function, class, or assignment to its reason"],
            self.problems({"tools/reader.py": blank})[:1],
        )

    def test_each_stated_limit_reads_as_its_docstring_says(self) -> None:
        limits = {
            # A class that takes another set of characters, as \w takes letters outside ASCII, is another class.
            "a word class": 'PAIR = r"[\\w.-]+/[\\w.-]+"\n',
            # A pattern joined with +, or a class split across an f-string's values, is not read.
            "joined with +": 'PAIR = "[A-Za-z0-9_.-]+" + "/" + "[A-Za-z0-9_.-]+"\n',
            "split by a value": 'def f(name):\n    return rf"[A-Za-z0-9_.-]+/{name}"\n',
            # A class whose body holds what re reads as a set operation is not measured.
            "a set operation": 'PAIR = r"[A-Za-z0-9_.--]+/[A-Za-z0-9_.--]+"\n',
        }
        for name, text in limits.items():
            with self.subTest(name):
                self.assertEqual([], self.problems({"tools/reader.py": text}))

    def test_the_names_are_the_ones_the_rule_names(self) -> None:
        # Pinned against independent literals: the helper, its class, and the allowance.
        self.assertEqual("skills/ejryan89-skill-core/scripts/github_remote.py", REPOSITORY_HELPER)
        self.assertEqual("[A-Za-z0-9_.-]", one_helper.REPOSITORY_CLASS)
        self.assertEqual("REPOSITORY_PATTERN_ALLOWED", one_helper.REPOSITORY_ALLOWANCE)

    def test_a_class_that_adds_a_space_a_tab_or_a_control_character_is_another_class(self) -> None:
        for added in [" ", "\\t", "\\x00", "\\x7f"]:
            with self.subTest(repr(added)):
                pair = f"[A-Za-z0-9_.{added}-]+/[A-Za-z0-9_.{added}-]+"
                self.assertEqual([], self.problems({"tools/reader.py": f'PAIR = r"{pair}"\n'}))

    def test_a_class_holding_a_set_operation_anywhere_in_its_body_is_never_measured(self) -> None:
        for operation in ["[", "--", "&&", "~~", "||"]:
            for spelled in [f"[{operation}a]", f"[a{operation}b]", f"[a{operation}]"]:
                with self.subTest(spelled):
                    self.assertIsNone(one_helper.class_members(spelled))

    def test_every_module_s_refusals_are_reported(self) -> None:
        reader = REFUSED_REPOSITORIES["spelled in another order"]
        problems = self.problems({"tools/broken.py": "def f(:\n", "tools/first.py": reader, "tools/second.py": reader})
        self.assertEqual(
            ["tools/broken.py", "tools/first.py:1", "tools/second.py:1"],
            sorted(problem.split()[0] for problem in problems),
        )

    def test_with_no_files_given_the_rule_reads_what_a_commit_could_include(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            write_fixture_tree(root, {"tools/reader.py": REFUSED_REPOSITORIES["spelled in another order"]})
            problems = repository_problems(root)
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("tools/reader.py:1 PATTERN spells"), problems)

    def test_a_class_re_cannot_compile_is_never_read_as_the_helper_s(self) -> None:
        # A class re refuses, as `[\q]`, whose escape names no character, measures as nothing, so it neither raises
        # nor matches, on either side of a `|`.
        self.assertIsNone(one_helper.class_members(r"[\q]"))
        self.assertEqual([], self.problems({"tools/reader.py": 'PAIR = r"[\\q]+/[\\q]+|[A-Za-z0-9_.-]+/[\\q]+"\n'}))

    def test_each_further_stated_limit_reads_as_its_docstring_says(self) -> None:
        limits = {
            # A possessive quantifier, a verbose pattern's spaces around the /, and a bytes literal are not read.
            "possessive": 'PAIR = r"[A-Za-z0-9_.-]++/[A-Za-z0-9_.-]++"\n',
            "verbose": 'import re\n\nPAIR = re.compile(r"[A-Za-z0-9_.-]+ / [A-Za-z0-9_.-]+", re.VERBOSE)\n',
            "bytes": 'PAIR = rb"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"\n',
            # The flags a pattern is compiled with are not read: each class is measured as written, with none.
            "ASCII": 'import re\n\nPAIR = re.compile(r"[\\w.-]+/[\\w.-]+", re.ASCII)\n',
            "IGNORECASE": 'import re\n\nPAIR = re.compile(r"[a-z0-9_.-]+/[a-z0-9_.-]+", re.IGNORECASE)\n',
        }
        for name, text in limits.items():
            with self.subTest(name):
                self.assertEqual([], self.problems({"tools/reader.py": text}))

    def test_a_script_a_workflow_and_a_markdown_fence_are_not_read(self) -> None:
        pair = "[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
        files = {
            "skills/x/scripts/reader.sh": f"grep -E '{pair}' \"$1\"\n",
            ".github/workflows/reader.yml": f"on: push\njobs:\n  r:\n    steps:\n      - run: grep -E '{pair}'\n",
            "docs/reader.md": f'```python\nPAIR = r"{pair}"\n```\n',
        }
        self.assertEqual([], self.problems(files))

    def test_the_helper_s_own_pattern_is_the_form_the_rule_refuses_elsewhere(self) -> None:
        self.assertTrue(one_helper.spells_owner_and_name(github_remote.OWNER_AND_NAME))
        self.assertTrue(one_helper.spells_owner_and_name(github_remote.GITHUB_REMOTE.pattern))

    def test_a_module_that_does_not_parse_is_named(self) -> None:
        problems = self.problems({"tools/broken.py": "def f(:\n"})
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith("tools/broken.py"), problems)
