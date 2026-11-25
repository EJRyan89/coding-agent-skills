# branch-name-command

The change passes the pull request's author-controlled branch name to `git fetch` as a bare argument, against the
threat-model row that the name is never an argument of a command. The scenario expects a MUST_FIX on that argument
line, line 16 of `review_github.py`, of any category.

It names no category because the defect fits several the trust-boundary brief lists: `Code-document drift`, since
the row exists and the code breaks it, `Unstated trust boundary`, and `Correctness`. Reviewers chose among them from
run to run, so a scenario that named one passed inconsistently on unchanged code (#2083). It expects line 16 alone
because every model's MUST_FIX on the defect sits there, while the only MUST_FIX findings on the line that reads
the name, line 14, were about a fork's head. The fixture states that only a pull request from a branch of the repository reaches
`fetch_head`, so a fork's head fetched from `origin`, a second MUST_FIX on these lines, is not one the change makes.

Limit: the judge reads a finding's path, line, and severity, never what it is about, so a MUST_FIX on line 16 about
another defect passes too. #3319, a finding expectation that requires text in the finding's title, closes it.
