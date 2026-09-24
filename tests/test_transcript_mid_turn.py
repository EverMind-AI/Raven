"""A message merged into a running turn is marked as such in the transcript.

A live client draws it inside the turn it joined, off ``message.injected``. A
reload has only the stored entry to go on, and an unmarked user entry is a
question: the fold closed over the narration above it, the turn number moved,
and the sentence the model had just written was promoted to the answer of a turn
that had not finished. The mark is what keeps the two views the same.
"""

from __future__ import annotations

import json
from pathlib import Path

from raven.agent.loop._shared import _MID_TURN_USER_KEY
from raven.rpc.methods.session import _map_to_wire


def _stored(**over):
    entry = {"role": "user", "content": "only the last quarter", "timestamp": "2026-09-22T09:00:30"}
    entry.update(over)
    return entry


# ── the wire ──────────────────────────────────────────────────────


def test_a_stored_mark_reaches_the_wire() -> None:
    """`session.resume` copies a whitelist onto the wire entry; a field absent
    from it is dropped however carefully it was written."""
    [entry] = _map_to_wire([_stored(mid_turn=True)], "tui:default")
    assert entry["mid_turn"] is True


def test_an_unmarked_entry_carries_no_mark() -> None:
    [entry] = _map_to_wire([_stored()], "tui:default")
    assert "mid_turn" not in entry


def test_the_text_still_rides() -> None:
    """It is a real user message; only where the reader draws it changes."""
    [entry] = _map_to_wire([_stored(mid_turn=True)], "tui:default")
    assert entry["text"] == "only the last quarter"


# ── the storage rename ────────────────────────────────────────────


def _loop():
    from raven.agent.loop.main import AgentLoop

    loop = object.__new__(AgentLoop)
    loop._now_fn = lambda: __import__("datetime").datetime(2026, 9, 22, 10, 0)
    return loop


class _Session:
    def __init__(self):
        self.entries = []

    def record(self, entry):
        self.entries.append(entry)


def test_the_private_key_is_renamed_for_storage() -> None:
    """Same underscore-then-rename convention as ``_origin``: the underscore
    keeps it out of the live provider payload, storage gets the plain name."""
    from raven.agent.loop.main import AgentLoop

    session = _Session()
    messages = [{"role": "user", "content": "only the last quarter", _MID_TURN_USER_KEY: True}]
    AgentLoop._save_turn(_loop(), session, messages, 0)

    [stored] = session.entries
    assert stored["mid_turn"] is True
    assert _MID_TURN_USER_KEY not in stored


def test_an_ordinary_question_stores_no_mark() -> None:
    from raven.agent.loop.main import AgentLoop

    session = _Session()
    AgentLoop._save_turn(_loop(), session, [{"role": "user", "content": "summarise the report"}], 0)
    assert "mid_turn" not in session.entries[0]


# ── the contract ──────────────────────────────────────────────────


def test_the_schema_declares_the_field() -> None:
    """The page's client is generated from this file, so a field the schema does
    not name cannot be read by the reader that needs it."""
    schema = json.loads((Path(__file__).resolve().parent.parent / "rpc-schema" / "openrpc.json").read_text())
    props = schema["components"]["schemas"]["TranscriptMessage"]["properties"]
    assert props["mid_turn"]["type"] == "boolean"
