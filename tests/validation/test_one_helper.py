"""Fixture tests for tests/validation/one_helper.py.

The place-lookup rule fails on a handler on a path to a place lookup that treats a GitError's kinds apart, and on a
scope nothing calls that lets one escape, and passes on paths that treat every GitError as an unknown place. The
closing-reference rule fails on a module other than the helper that matches a closing keyword before an issue reference
itself, or reads a title or a body alone, and passes on one that reads both through the helper.
"""

from __future__ import annotations

import ast
import re
import sys
import tempfile
import textwrap
import unittest
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import closing_references
from one_helper import (
    CATCHING,
    HELPER,
    KIND_ATTRIBUTES,
    PATTERN_FUNCTIONS,
    POLICY,
    REFERENCE_PATTERN,
    SAMPLE_REFERENCES,
    closing_reference_problems,
    error_attributes,
    module_closing_problems,
    pattern_functions,
    place_lookup_problems,
)
from validation_support import write_fixture_tree

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
        for handler in ("except RuntimeError:", "except Exception:", "except BaseException:", "except:"):
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
