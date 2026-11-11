"""The one reader of Claude Code's stream-json output that the canaries and the skill evaluations share."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from tools import claude_stream

# Each character other than a newline and a carriage return that str.splitlines() breaks a line on, which a JSON
# string Claude Code writes as it is can hold.
BREAKS = [chr(code) for code in range(0x110000) if chr(code) not in "\n\r" and len(f"a{chr(code)}b".splitlines()) == 2]


def test_an_event_holding_a_line_break_other_than_newline_is_one_event() -> None:
    assert {chr(0x85), chr(0x2028), chr(0x2029)} <= set(BREAKS)
    for separator in BREAKS:
        event = {"type": "assistant", "message": {"content": [{"type": "text", "text": f"a{separator}b"}]}}
        stream = json.dumps(event, ensure_ascii=False) + "\r\n" + json.dumps({"type": "result"}) + "\n"
        assert list(claude_stream.events(stream)) == [event, {"type": "result"}], hex(ord(separator))


@pytest.mark.parametrize(
    ("line", "kind"),
    [("[]", "array"), ('"text"', "string"), ("3", "number"), ("2.5", "number"), ("true", "boolean"), ("null", "null")],
)
def test_a_json_line_that_is_no_object_is_refused_by_its_line_and_kind(line: str, kind: str) -> None:
    stream = '{"type": "system"}\n' + line + '\n{"type": "result"}\n'
    events = claude_stream.events(stream)
    assert next(events) == {"type": "system"}
    with pytest.raises(claude_stream.StreamError) as raised:
        next(events)
    assert str(raised.value) == f"line 2 of the session's stream is a JSON {kind}, not an event object"


def test_a_line_that_is_not_json_is_passed_over_and_the_events_around_it_read() -> None:
    stream = '\n{"type": "system"}\nwarning: not json\n{"type": "result", "type": "x"}\n{"type": "result"}\n{"ty'
    assert list(claude_stream.events(stream)) == [{"type": "system"}, {"type": "result"}]
    assert list(claude_stream.events("")) == []
