# text-read-policy

The change adds a validation policy whose docstring says it reads each module through `ast` and resolves each name
through its imports, while the code matches `print(` in each line's text. The scenario expects a SHOULD_FIX of any
category on lines 10, 17, and 18 of `console_output.py`: the pattern, the text read, and the match.

It names no category because the defect fits several the validation-policy brief lists: `Unheld invariant`, since
the policy holds less than its sentence, and `Code-document drift`, since it holds something else. Reviewers chose
among them from run to run, so a scenario that named one passed inconsistently on unchanged code (#2083). Line 16 is
left out because it carries another defect, a missing or empty `tools/` folder read as no problems.

Limit: the judge reads a finding's path, line, and severity, never what it is about, so a SHOULD_FIX on these lines
about another defect passes too. Line 17 carries one: `read_text(encoding="utf-8")` stops the policy with an
exception on a module under `tools/` that is not UTF-8. #3319, a finding expectation that requires text in the
finding's title, closes it.
