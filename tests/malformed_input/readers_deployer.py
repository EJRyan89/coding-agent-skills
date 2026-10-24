"""The deployer's readers: its own state files, read on every later run, the source's JSON, and the runtimes'
listings.

Every refusal is a DeployError, which deploy.py prints as its ERROR lines; a journal line it cannot read is returned
as the entry recovery stops at, which the adapter raises as Refused.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from reader_table import REPOSITORY, Reader, Refused, text_of

sys.path.insert(0, str(REPOSITORY))
sys.path.insert(0, str(REPOSITORY / "skills" / "ejryan89-skill-core" / "scripts"))

from process_identity import ProcessStatus

from deployer import config, discovery, journal, lock, manifest, source
from deployer.errors import DeployError
from deployer.names import transient_name
from deployer.paths import Paths

SOURCE_ID = "example/source"


def _paths(path: Path) -> Paths:
    """Paths whose source tree and home are the case's folder."""
    return Paths(source_dir=path.parent, home=path.parent)


def _read_config(path: Path) -> object:
    if not config.is_saved(path):
        raise AssertionError("the case placed nothing at the configuration's path")
    return config.read(path, SOURCE_ID)


# A journal line recovery reads: the skills root's preserve entry for one item of the run the journal's folder names.
JOURNAL_LINE = json.dumps(
    {"op": "preserve", "item": "example", "from": f"skills/{transient_name('example')}", "to": ".backups/run/example"}
).encode()


def _read_journal(path: Path) -> object:
    """The journal as the dry run's survey reads it: a run it cannot reconcile without the user is refused."""
    paths = Paths(source_dir=path.parents[4], home=path.parents[4])
    pending = journal.pending_runs(paths)
    if any(not unattended for _, unattended in pending):
        raise Refused(f"the run needs the user: {pending}")
    return pending


def _read_lock(path: Path) -> object:
    # The holder is not running, so a readable PID is judged stale and returned; an unreadable one is refused.
    paths = Paths(source_dir=path.parents[3], home=path.parents[3])
    return lock._existing_holder(paths, lambda pid: ProcessStatus(alive=False, start_time=None))


def _read_source_id(path: Path) -> object:
    return source.load_source_id(_paths(path))


def _frontmatter_name(path: Path) -> object:
    return source.frontmatter_name(path, "Skill 'example'")


def _copilot_listing(path: Path) -> object:
    return discovery.parse_copilot(text_of(path))


READERS = [
    Reader(
        "deployer.config.read",
        "key-value",
        f"_source_id={SOURCE_ID}\n".encode(),
        _read_config,
        (DeployError,),
    ),
    Reader(
        "deployer.discovery.parse_copilot",
        "json",
        b'[{"name": "example", "source": "personal-agents", "path": "skills/example", "enabled": true}]',
        _copilot_listing,
        (discovery.ListingError,),
        accepts={"top_level_list": "an empty array is Copilot's listing of no skills, which verify reports as missing"},
    ),
    Reader(
        "deployer.journal.pending_runs",
        "json-lines",
        JOURNAL_LINE + b"\n",
        _read_journal,
        (Refused,),
        filename="home/.claude/deployer/staging/run/journal.jsonl",
        accepts={
            "empty": "an empty journal records no change, so recovery has nothing to undo",
            "torn_line": "a last line with no newline was cut off before its change began, so it is set aside",
            "blank_lines": "blank lines hold no entry and are passed over",
        },
    ),
    Reader(
        "deployer.lock._existing_holder",
        "json",
        b'{"pid": 4242, "start_time": 1, "token": "4242-1-ab12"}',
        _read_lock,
        (DeployError,),
        filename="home/.claude/deployer/.deploy.lock.d/info.json",
    ),
    Reader(
        "deployer.manifest.load",
        "json",
        b'{"manifest_version": 8, "sources": {}}',
        manifest.load,
        (DeployError,),
    ),
    Reader(
        "deployer.source.frontmatter_name",
        "frontmatter",
        b"---\nname: example\ndescription: An example.\n---\n",
        _frontmatter_name,
        (DeployError,),
        accepts={
            "bom": "frontmatter.read decodes UTF-8 with an optional byte-order mark, as editors on Windows save it",
            "nul_bytes": "the body after the frontmatter's closing line is never read",
        },
    ),
    Reader(
        "deployer.source.load_source_id",
        "json",
        f'{{"id": "{SOURCE_ID}"}}'.encode(),
        _read_source_id,
        (DeployError,),
        filename="source.json",
        accepts={"bom": "the source's JSON is decoded as UTF-8 with an optional byte-order mark"},
    ),
]
