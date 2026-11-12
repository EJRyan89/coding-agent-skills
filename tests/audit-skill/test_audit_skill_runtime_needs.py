"""audit-skill's runtime needs, held to repository validation's for every shipped skill and for fixture skills.

Both derive a skill's needs through ejryan89-skill-core's runtime_support, each reading the skill's own grants,
metadata, agents, and scripts and the skills it invokes, and both read a Bash or PowerShell script's git subcommands
through ejryan89-skill-core's one reader, git_subcommands, each listing a PowerShell script's commands with
powershell_commands's parser script. The RUNTIME_NEEDS lines audit-skill prints for a skill in this source tree name
exactly the capabilities the runtime-support policy holds its declaration to, so neither derivation can drift from the
other unseen.

Each derivation reads a PowerShell script through a memo primed once for the module, from one batched read for all the
scripts the rows and the shipped skills hold and one for each level of the scripts those start, rather than starting
pwsh for each script; a script the prime did not reach falls through to the derivation's own reader. One PowerShell
row and the test that both read a script through one reader read it unprimed, so each side's own pwsh start stays
covered.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Callable, Iterator
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("audit-skill")))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "validation"))

import powershell_commands
import skill_inventory
import skill_layout
import skill_scripts
from skill_layout import derived_needs
from test_shell_readers import POWERSHELL_WRAPPED_GIT_PUSH_SOURCES, WRAPPED_GIT_PUSH_SCRIPTS

REPOSITORY = Path(__file__).resolve().parents[2]
SKILLS = sorted(path.parent.name for path in (REPOSITORY / "skills").glob("*/SKILL.md"))

# The suffixes of the scripts whose git commands a derivation reads by starting pwsh, on them or on a script they start.
SHELL_SUFFIXES = (".ps1", ".sh", ".bash")

# audit-skill's own reader of one PowerShell script's commands, which the memo below falls through to.
AUDIT_READER = skill_inventory.powershell_script_commands

# What audit-skill's reader gives each PowerShell script this module has read, and the scripts each prime_audit defers
# while it runs a read, as skill_scripts.prime holds them for validation's reader.
_AUDIT_COMMANDS: dict[str, list[list[str]]] = {}
_AUDIT_DEFERRED: list[list[str]] = []


def audit_script_commands(script: str) -> list[list[str]]:
    """audit-skill's powershell_script_commands through this module's memo: a script read before is not read again, one
    a prime_audit reaches is deferred, and any other falls through to AUDIT_READER."""
    if (commands := _AUDIT_COMMANDS.get(script)) is not None:
        return commands
    if _AUDIT_DEFERRED:
        _AUDIT_DEFERRED[-1].append(script)
        return []
    commands = _AUDIT_COMMANDS[script] = AUDIT_READER(script)
    return commands


def prime_audit(read: Callable[[], object]) -> None:
    """Run `read` until it reaches no PowerShell script the memo lacks, reading the scripts each run defers in one
    call of ejryan89-skill-core's powershell_commands.read, the reader audit-skill's wraps."""
    while True:
        _AUDIT_DEFERRED.append([])
        try:
            read()
        finally:
            deferred = _AUDIT_DEFERRED.pop()
        if not deferred:
            return
        texts = list(dict.fromkeys(deferred))
        _AUDIT_COMMANDS.update(zip(texts, powershell_commands.read(texts), strict=True))


@pytest.fixture(scope="module", autouse=True)
def primed_readers(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Route audit-skill's PowerShell reader through the memo, and prime both derivations' readers with every script
    the shipped skills and the fixture rows hold, but the unprimed row's, as each derivation reads them from a file."""
    root = tmp_path_factory.mktemp("primed")
    rows = [
        *((name, text) for name, text, _ in GIT_SUBCOMMAND_SCRIPTS),
        *UNNAMED_SUBCOMMAND_SCRIPTS,
        *((name, text + "\n") for name, text in WRAPPED_GIT_PUSHES),
    ]
    scripts = [path for path in (REPOSITORY / "skills").glob("*/scripts/*") if path.suffix.casefold() in SHELL_SUFFIXES]
    for index, (name, text) in enumerate(row for row in dict.fromkeys(rows) if row != UNPRIMED_ROW):
        script = root / str(index) / name
        script.parent.mkdir()
        script.write_text(text, encoding="utf-8")
        scripts.append(script)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(skill_inventory, "powershell_script_commands", audit_script_commands)
        skill_scripts.prime(lambda: [skill_layout.script_git_subcommands(script) for script in scripts])
        prime_audit(lambda: [skill_inventory.script_git_subcommands(script) for script in scripts])
        yield


def printed_needs(skill_md: Path) -> set[str]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = skill_inventory.main(["tools", str(skill_md)])
    assert code == 0, output.getvalue()
    return {line.split()[1] for line in output.getvalue().splitlines() if line.startswith("RUNTIME_NEEDS ")}


@pytest.mark.parametrize("name", SKILLS)
def test_audit_skill_derives_the_needs_validation_holds_each_skill_to(name: str) -> None:
    assert printed_needs(REPOSITORY / "skills" / name / "SKILL.md") == derived_needs(REPOSITORY, name)


def fixture_skill(root: Path, scripts: dict[str, str]) -> Path:
    """A source tree under `root` holding one skill, alpha, with these scripts, by file name, and full support on
    every runtime; the SKILL.md it returns."""
    skill = root / "skills" / "alpha"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: alpha\ndescription: Runs.\n---\nBody.\n", encoding="utf-8")
    for name, text in scripts.items():
        (skill / "scripts" / name).write_text(text, encoding="utf-8")
    (root / "deploy-meta").mkdir()
    (root / "deploy-meta" / "alpha.json").write_text(
        '{"runtime_support": {"claude-code": "full", "codex": "full", "copilot-cli": "full"}}\n', encoding="utf-8"
    )
    return skill / "SKILL.md"


def both_needs(root: Path, scripts: dict[str, str]) -> tuple[set[str], set[str]]:
    """The needs audit-skill prints for the fixture skill and the needs validation holds it to."""
    skill_md = fixture_skill(root, scripts)
    return printed_needs(skill_md), derived_needs(root, "alpha")


# The issue's forms: a git push in a Bash or PowerShell script, run directly, by Start-Process, behind a wrapper, or in
# the script a shell or PowerShell start-up runs, needs network access in both derivations; one in a regression suite
# or a comment needs none.
GIT_SUBCOMMAND_SCRIPTS: tuple[tuple[str, str, set[str]], ...] = (
    ("update.ps1", "git push origin\n", {"sandbox-network"}),
    ("update.ps1", "Start-Process git -ArgumentList 'push', 'origin'\n", {"sandbox-network"}),
    ("update.ps1", "& git.exe -C $root fetch\n", {"sandbox-network"}),
    ("update.ps1", "Git push\n", {"sandbox-network"}),
    ("update.ps1", "GIT.EXE fetch\n", {"sandbox-network"}),
    ("update.sh", "Git push\n", set()),
    ("update.ps1", "pwsh -NoProfile -Command 'git pull'\n", {"sandbox-network"}),
    ("update.ps1", "bash -c 'git ls-remote origin'\n", {"sandbox-network"}),
    ("update.ps1", "git status\n# git push\n", set()),
    ("update.PS1", "git clone $url\n", {"sandbox-network"}),
    ("test_update.ps1", "git push\n", set()),
    ("update.sh", "git push origin\n", {"sandbox-network"}),
    ("update.sh", "pwsh -c 'git push'\n", {"sandbox-network"}),
    ("update.sh", "git status\n", set()),
    # A case item's or a function body's command, and git or a shell named by a path or with .exe.
    ("update.sh", 'case "$x" in\n  up) git push ;;\nesac\n', {"sandbox-network"}),
    ("update.sh", "f() { git push; }\n", {"sandbox-network"}),
    ("update.sh", "/usr/bin/git push\n", {"sandbox-network"}),
    ("update.sh", "git.exe push origin\n", {"sandbox-network"}),
    ("update.sh", '/bin/bash -c "git push"\n', {"sandbox-network"}),
    ("update.ps1", "& 'C:/Program Files/Git/cmd/git' push\n", {"sandbox-network"}),
)

# The row each derivation reads with its own pwsh start: the prime leaves its script out.
UNPRIMED_ROW = ("update.ps1", "GIT.EXE fetch\n")


@pytest.mark.parametrize(("name", "text", "needs"), GIT_SUBCOMMAND_SCRIPTS)
def test_a_git_subcommand_a_script_runs_needs_the_same_capabilities_in_both(
    tmp_path: Path, name: str, text: str, needs: set[str]
) -> None:
    assert both_needs(tmp_path, {name: text}) == (needs, needs)


# A subcommand no literal names may be any, one that reaches a remote included, so both hold the script to network
# access: Bash's word, and PowerShell's word that is no literal.
UNNAMED_SUBCOMMAND_SCRIPTS = (
    ("update.sh", 'git "$sub" origin\n'),
    ("update.sh", "pwsh -c 'git $sub'\n"),
    ("update.ps1", "git $sub origin\n"),
    ("update.ps1", "Start-Process git -ArgumentList $arguments\n"),
)


@pytest.mark.parametrize(("name", "text"), UNNAMED_SUBCOMMAND_SCRIPTS)
def test_a_git_subcommand_no_literal_names_needs_network_access_in_both(tmp_path: Path, name: str, text: str) -> None:
    assert both_needs(tmp_path, {name: text}) == ({"sandbox-network"}, {"sandbox-network"})


# Every wrapper form both readers' suites hold, as a fixture script, gives both derivations the same needs.
WRAPPED_GIT_PUSHES = (
    *(("update.sh", script) for script in WRAPPED_GIT_PUSH_SCRIPTS),
    *(("update.ps1", source) for source in POWERSHELL_WRAPPED_GIT_PUSH_SOURCES),
)


@pytest.mark.parametrize(("name", "text"), WRAPPED_GIT_PUSHES)
def test_a_wrapped_git_push_needs_network_access_in_both(tmp_path: Path, name: str, text: str) -> None:
    assert both_needs(tmp_path, {name: text + "\n"}) == ({"sandbox-network"}, {"sandbox-network"})


def test_validation_and_audit_skill_read_a_script_through_one_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unprimed: each side starts pwsh on the script itself.
    monkeypatch.setattr(skill_scripts, "_SCRIPT_READINGS", {})
    monkeypatch.setattr(skill_inventory, "powershell_script_commands", AUDIT_READER)
    script = fixture_skill(tmp_path, {"update.ps1": "env git push\n"}).parent / "scripts" / "update.ps1"
    assert skill_layout.script_git_subcommands(script) == skill_inventory.script_git_subcommands(script) == {"push"}


def test_a_skill_that_invokes_another_needs_what_the_invoked_skill_does() -> None:
    # update-pr-dashboard declares only gh; the reviews it starts through review-prs need the rest.
    assert {"agent-delegation", "workflow", "fail-closed-agent-hooks"} <= printed_needs(
        REPOSITORY / "skills" / "update-pr-dashboard" / "SKILL.md"
    )
