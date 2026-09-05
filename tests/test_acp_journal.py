"""The ACP wire journal: what it records, what it refuses to grow into.

The journal is an audit record for a connection, so the properties worth pinning
are the ones a reader depends on and cannot check for themselves: one line per
frame in wire order, an offset that a call can slice with, a file no other user
can read, a stated ceiling that says so when it is reached, and a failure mode
that costs the record rather than the run.
"""

from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from raven.agent.acp.journal import CALL, FRAME, FrameJournal, enabled, open_journal, prune, redact_acp_frame


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_each_frame_is_one_line_and_the_offset_slices_it(tmp_path: Path) -> None:
    """The offset is a byte cursor into the file, which is what makes a call's
    ``[start, end)`` range readable by seeking rather than by re-parsing."""
    journal = FrameJournal(tmp_path / "a.jsonl")
    journal.note("out", frame={"method": "initialize"})
    mark = journal.offset
    journal.note("in", frame={"id": 1, "result": {}}, session="s1")
    journal.note("err", text="provider said no")
    journal.close()

    body = journal.path.read_bytes()
    assert len(body) == journal.offset
    written = _records(journal.path)
    assert [r["dir"] for r in written] == ["out", "in", "err"]
    # The session log's own grammar: a `_type` on every record and `timestamp`
    # as the time key, so one reader parses this file and the conversation's.
    assert {r["_type"] for r in written} == {FRAME}
    assert all(r["timestamp"] and "+" not in r["timestamp"] for r in written), "naive local, as the session log is"
    tail = [json.loads(line) for line in body[mark:].decode().splitlines()]
    assert tail[0]["session"] == "s1"
    assert tail[1]["text"] == "provider said no"


def test_a_frame_with_no_session_says_nothing_rather_than_null(tmp_path: Path) -> None:
    """Absent, not ``null``: ``initialize`` belongs to no session, and a reader
    filtering by session must not have to tell two spellings apart."""
    journal = FrameJournal(tmp_path / "a.jsonl")
    journal.note("out", frame={"method": "initialize"})
    journal.close()

    assert "session" not in _records(journal.path)[0]


def test_the_file_is_unreadable_to_anyone_else(tmp_path: Path) -> None:
    """It holds the whole prompt and every tool result, so the mode is set as the
    file is created rather than fixed up afterwards -- a world-readable window
    between the two would be exactly long enough to matter."""
    journal = FrameJournal(tmp_path / "a.jsonl")
    journal.note("out", frame={"method": "initialize"})
    journal.close()

    assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600


def test_mcp_secrets_are_redacted_without_mutating_the_wire_frame() -> None:
    frame = {
        "method": "session/load",
        "params": {
            "mcpServers": [
                {
                    "name": "private",
                    "env": [{"name": "TOKEN", "value": "stdio-secret"}],
                    "headers": [{"name": "Authorization", "value": "header-secret"}],
                }
            ]
        },
    }

    redacted = redact_acp_frame(frame)

    server = redacted["params"]["mcpServers"][0]
    assert server["env"][0]["value"] == "<redacted>"
    assert server["headers"][0]["value"] == "<redacted>"
    assert frame["params"]["mcpServers"][0]["env"][0]["value"] == "stdio-secret"
    assert frame["params"]["mcpServers"][0]["headers"][0]["value"] == "header-secret"


def test_reaching_the_ceiling_is_written_down(tmp_path: Path) -> None:
    """A journal that simply stopped would read as a connection that went quiet.

    The marker is the difference between "nothing more happened" and "we stopped
    recording", and only the second one is true.
    """
    journal = FrameJournal(tmp_path / "a.jsonl", max_bytes=200)
    for i in range(50):
        journal.note("in", frame={"method": "session/update", "params": {"i": i, "pad": "x" * 40}})
    journal.close()

    records = _records(journal.path)
    assert records[-1] == {"_type": "acp_marker", "truncated": 200}
    assert journal.offset <= 200, "the marker is budgeted first, so the file never exceeds its stated ceiling"
    assert sum(1 for r in records if r.get("dir") == "in") < 50, "the ceiling was not enforced"


def test_a_journal_that_cannot_open_its_file_costs_the_record_not_the_run(tmp_path: Path) -> None:
    """Nothing here may fail a run: an unwritable location downgrades to
    recording nothing, and the caller never learns."""
    blocked = tmp_path / "afile"
    blocked.write_text("not a directory", encoding="utf-8")
    journal = FrameJournal(blocked / "nested" / "a.jsonl")

    journal.note("out", frame={"method": "initialize"})
    journal.close()

    assert journal.offset == 0
    assert not journal.path.exists()


def test_the_env_switch_turns_it_off(monkeypatch, tmp_path: Path) -> None:
    """On unless asked otherwise, and asking yields no journal at all rather than
    an empty one -- a call with no journal records no frame range, instead of
    pointing at a file that does not exist."""
    monkeypatch.setenv("RAVEN_ACP_JOURNAL", "0")
    assert enabled() is False
    assert open_journal("a", root=tmp_path) is None

    monkeypatch.setenv("RAVEN_ACP_JOURNAL", "1")
    assert enabled() is True
    assert open_journal("a", root=tmp_path) is not None


def test_a_journal_is_named_for_its_agent_under_todays_directory(tmp_path: Path) -> None:
    journal = open_journal("My Agent/2", root=tmp_path)
    assert journal is not None
    journal.note("out", frame={})
    journal.close()

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert journal.path.parent == tmp_path / day
    assert journal.path.name.startswith("My-Agent-2-")
    assert journal.path.suffix == ".jsonl"


def test_prune_drops_only_directories_past_the_window(tmp_path: Path) -> None:
    """A reclaim policy, stated. The artifact store next door has none and had
    grown to gigabytes, which is the reason this one is not left implicit."""
    now = datetime.now(timezone.utc)
    old = tmp_path / (now - timedelta(days=30)).strftime("%Y-%m-%d")
    recent = tmp_path / (now - timedelta(days=1)).strftime("%Y-%m-%d")
    for day in (old, recent):
        day.mkdir(parents=True)
        (day / "a.jsonl").write_text("{}\n", encoding="utf-8")

    prune(tmp_path, keep_days=7)

    assert not old.exists()
    assert (recent / "a.jsonl").is_file()


def test_prune_survives_a_missing_root(tmp_path: Path) -> None:
    prune(tmp_path / "never-created")


def test_opening_a_journal_prunes_the_old_ones(tmp_path: Path) -> None:
    """The prune runs where a new connection is opened, because that is the one
    moment the code is guaranteed to reach on a long-running host."""
    stale = tmp_path / (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    stale.mkdir(parents=True)
    (stale / "a.jsonl").write_text("{}\n", encoding="utf-8")

    assert open_journal("a", root=tmp_path) is not None
    assert not stale.exists()


def test_notes_after_close_are_dropped(tmp_path: Path) -> None:
    """The connection's teardown closes it, and a late frame from a dying process
    must not reopen the file behind the close."""
    journal = FrameJournal(tmp_path / "a.jsonl")
    journal.note("out", frame={"method": "initialize"})
    settled = journal.offset
    journal.close()

    journal.note("in", frame={"method": "session/update"})

    assert journal.offset == settled
    assert len(_records(journal.path)) == 1


def test_a_value_json_cannot_encode_is_stringified_not_raised(tmp_path: Path) -> None:
    """``default=str`` is what keeps the read loop safe.

    A frame off the wire is always JSON, so this is the belt: something that
    reached ``note`` from anywhere else is written in whatever rendering Python
    can give it, rather than raising into the loop that was recording it.
    """
    journal = FrameJournal(tmp_path / "a.jsonl")
    journal.note("in", frame={"obj": object()})
    journal.close()

    written = _records(journal.path)
    assert len(written) == 1
    assert written[0]["frame"]["obj"].startswith("<object object at")


# ---- the bind line: whose call the frames that follow belong to --------------


def test_bind_names_the_instance_the_frames_belong_to(tmp_path: Path) -> None:
    """ACP has nowhere to carry raven's identity, so the dispatcher writes it.

    Without this a journal -- per connection, and therefore spanning
    conversations and instances -- can only be read by joining its session ids
    against spans or every meta.json on the host, and a stateless agent never
    registers an instance row for that join to land on.
    """
    journal = FrameJournal(tmp_path / "a.jsonl")
    journal.bind({"session": "s1", "agent": "Coder", "instance": "h1", "session_key": "web:abc", "resumed": False})
    journal.close()

    written = _records(journal.path)[0]
    assert written["_type"] == CALL, "a call boundary: not a frame, and not a message"
    assert written["timestamp"]
    assert {k: v for k, v in written.items() if k not in ("_type", "timestamp")} == {
        "session": "s1",
        "agent": "Coder",
        "instance": "h1",
        "session_key": "web:abc",
        "resumed": False,
    }


def test_an_empty_identity_writes_no_bind_line(tmp_path: Path) -> None:
    journal = FrameJournal(tmp_path / "a.jsonl")
    journal.bind({})
    journal.close()

    assert not journal.path.exists()
