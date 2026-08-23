"""A turn the runtime opened is marked as such in the transcript.

Reopening a session drew the announce that ends a sub-agent run as the user's
own words -- untrusted fence, instance handle, and the instruction saying not to
repeat either of them to the user, all on screen. The text is what the model
reads; a reader needs to know a person did not type it.
"""

from __future__ import annotations

import json

import pytest

from raven.agent.loop.main import _ORIGIN_KEY
from raven.rpc.methods.session import _map_to_wire
from raven.spine.turn import Origin


def _stored(**over):
    entry = {"role": "user", "content": "hi", "timestamp": "2026-08-21T15:00:00"}
    entry.update(over)
    return entry


# ── the wire ──────────────────────────────────────────────────────


def test_a_stored_origin_reaches_the_wire() -> None:
    """`session.resume` copies a whitelist onto the wire entry; a field absent
    from it is dropped however carefully it was written."""
    [entry] = _map_to_wire([_stored(origin="subagent")], "tui:default")
    assert entry["origin"] == "subagent"


def test_an_unmarked_entry_carries_no_origin() -> None:
    [entry] = _map_to_wire([_stored()], "tui:default")
    assert "origin" not in entry


@pytest.mark.parametrize("origin", ["subagent", "cron", "sentinel", "heartbeat"])
def test_every_runtime_origin_survives_the_wire(origin) -> None:
    """Not only sub-agents: a cron reminder's text carries "when you reply,
    mention when the reminder was originally set", which is as much an
    instruction to the model as an announce is."""
    [entry] = _map_to_wire([_stored(origin=origin)], "tui:default")
    assert entry["origin"] == origin


def test_the_text_still_rides() -> None:
    """The model reads it. Only the reader is meant to treat it differently."""
    [entry] = _map_to_wire([_stored(origin="subagent", content="[Subagent 'x' completed]")], "tui:default")
    assert entry["text"] == "[Subagent 'x' completed]"


# ── the storage rename ────────────────────────────────────────────


def test_the_private_key_is_renamed_for_storage(tmp_path) -> None:
    """Same underscore-then-rename convention as ``_notice``: the underscore
    keeps it out of the live provider payload, storage gets the plain name."""
    from raven.agent.loop.main import AgentLoop

    class _Session:
        def __init__(self):
            self.entries = []

        def record(self, entry):
            self.entries.append(entry)

    loop = object.__new__(AgentLoop)
    loop._now_fn = lambda: __import__("datetime").datetime(2026, 8, 21, 15, 0)  # _save_turn stamps entries
    session = _Session()
    messages = [{"role": "user", "content": "hi", _ORIGIN_KEY: "subagent"}]
    AgentLoop._save_turn(loop, session, messages, 0)

    [stored] = session.entries
    assert stored["origin"] == "subagent"
    assert _ORIGIN_KEY not in stored


def test_an_unmarked_turn_stores_no_origin() -> None:
    from raven.agent.loop.main import AgentLoop

    class _Session:
        def __init__(self):
            self.entries = []

        def record(self, entry):
            self.entries.append(entry)

    loop = object.__new__(AgentLoop)
    loop._now_fn = lambda: __import__("datetime").datetime(2026, 8, 21, 15, 0)
    session = _Session()
    AgentLoop._save_turn(loop, session, [{"role": "user", "content": "hi"}], 0)
    assert "origin" not in session.entries[0]


# ── the contract ──────────────────────────────────────────────────


def test_the_schema_declares_the_field() -> None:
    """The page's client is generated from this file, so a field the schema does
    not name cannot be read by the reader that needs it."""
    from pathlib import Path

    schema = json.loads((Path(__file__).resolve().parent.parent / "rpc-schema" / "openrpc.json").read_text())
    props = schema["components"]["schemas"]["TranscriptMessage"]["properties"]
    assert props["origin"]["type"] == "string"


def test_the_origins_named_in_the_description_are_the_real_ones() -> None:
    """The description lists them for a reader of the contract; a rename in the
    enum would otherwise leave the contract quietly wrong."""
    from pathlib import Path

    schema = json.loads((Path(__file__).resolve().parent.parent / "rpc-schema" / "openrpc.json").read_text())
    described = schema["components"]["schemas"]["TranscriptMessage"]["properties"]["origin"]["description"]
    for origin in (Origin.SUBAGENT, Origin.CRON, Origin.SENTINEL, Origin.HEARTBEAT):
        assert f"`{origin}`" in described


# ── which turns are marked ────────────────────────────────────────


@pytest.mark.parametrize("origin", [Origin.SUBAGENT, Origin.CRON, Origin.SENTINEL, Origin.HEARTBEAT])
def test_every_origin_but_the_user_is_marked(origin) -> None:
    """Marking only sub-agents would leave cron, sentinel and heartbeat drawing
    their runtime prose as typed words -- the same bug, three more ways in."""
    from raven.agent.loop.main import _runtime_origin

    assert _runtime_origin(origin) == str(origin)


def test_a_person_typing_is_not_marked() -> None:
    from raven.agent.loop.main import _runtime_origin

    assert _runtime_origin(Origin.USER) is None
    assert _runtime_origin(None) is None


# ── the path that answers before _save_turn runs ──────────────────
# With personalization on, a runtime-origin turn can be met with a clarifying
# question and returned, so the envelope the assembler would have marked is
# never built and the entry reaches disk from `_record_inbound` instead.


class _Recorder:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def record(self, entry: dict) -> None:
        self.entries.append(entry)


@pytest.mark.parametrize("origin", [Origin.SENTINEL, Origin.CRON, Origin.HEARTBEAT])
def test_the_clarification_path_marks_what_it_stores(origin) -> None:
    from raven.agent.loop.main import AgentLoop

    session = _Recorder()
    AgentLoop._record_inbound(object.__new__(AgentLoop), session, "runtime notice", origin, "2026-08-22T15:00:00")

    [stored] = session.entries
    assert stored["role"] == "user"
    assert stored["origin"] == str(origin)


def test_the_clarification_path_leaves_a_persons_message_unmarked() -> None:
    from raven.agent.loop.main import AgentLoop

    session = _Recorder()
    AgentLoop._record_inbound(object.__new__(AgentLoop), session, "hi", Origin.USER, "2026-08-22T15:00:00")

    assert "origin" not in session.entries[0]


def test_no_path_writes_the_inbound_entry_around_the_writer() -> None:
    """Two copies of this write is how one came to be marked and the other not,
    so the invariant is that the raw form is gone rather than that some function
    contains a call."""
    import inspect

    from raven.agent.loop import main as loop_main

    source = inspect.getsource(loop_main)
    # Exactly one: the writer's own line. A second is a path that skipped it.
    assert source.count('{"role": "user", "content": content') == 1
    assert '{"role": "user", "content": content' in inspect.getsource(loop_main.AgentLoop._record_inbound)
    assert source.count("self._record_inbound(session, content, origin") == 2


# ── the other readers of the same contract ────────────────────────


def test_the_markdown_export_does_not_sign_it_as_the_user() -> None:
    """`session.export` is read by a person too. Under "User" it asserts that
    someone typed an announce carrying an untrusted fence and an instruction not
    to repeat it."""
    from raven.session.export import _render_message

    said = _render_message({"role": "user", "content": "[BEGIN UNTRUSTED ...] handle x", "origin": "subagent"})

    assert "🧑 User" not in said
    assert "Runtime (subagent)" in said
    # The text is kept: this file is the audit trail, and a record that drops
    # what it cannot attribute is worse than one that attributes it correctly.
    assert "handle x" in said


def test_the_markdown_export_still_signs_a_person() -> None:
    from raven.session.export import _render_message

    assert "🧑 User" in _render_message({"role": "user", "content": "hi"})
