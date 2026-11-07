"""Backfilling an external run's steps into the trace log."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from raven.trajectory import frames_backfill


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "logs").mkdir(parents=True)
    return state


def _journal(path: Path) -> None:
    lines = []

    def frame(direction: str, body: dict) -> None:
        lines.append(
            json.dumps({"_type": "acp_frame", "timestamp": "2026-10-27T09:00:01", "dir": direction, "frame": body})
        )

    frame(
        "out", {"method": "session/prompt", "params": {"sessionId": "s", "prompt": [{"type": "text", "text": "do it"}]}}
    )
    frame(
        "in",
        {
            "method": "session/update",
            "params": {
                "sessionId": "s",
                "update": {"sessionUpdate": "agent_message_chunk", "content": [{"type": "text", "text": "on it"}]},
            },
        },
    )
    frame(
        "in",
        {
            "method": "session/update",
            "params": {
                "sessionId": "s",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "c1",
                    "title": "read_file",
                    "kind": "read",
                    "status": "in_progress",
                    "rawInput": {"path": "a.py"},
                },
            },
        },
    )
    frame(
        "in",
        {
            "method": "session/update",
            "params": {
                "sessionId": "s",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "c1",
                    "status": "completed",
                    "rawOutput": "ok",
                },
            },
        },
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _spans(state: Path, journal: Path) -> None:
    start = (datetime.now() - timedelta(minutes=5)).isoformat()

    def span(span_id: str, name: str, parent: str | None, attrs: dict) -> dict:
        return {
            "schemaVersion": "audit.span.v1",
            "traceId": "t",
            "spanId": span_id,
            "parentSpanId": parent,
            "name": name,
            "startTime": start,
            "endTime": start,
            "status": {"code": "OK", "message": ""},
            "attributes": attrs,
            "events": [],
        }

    spans = [
        span("run", "subagent.run", None, {"session.key": "tui:x", "channel": "tui"}),
        span(
            "ext",
            "subagent.external",
            "run",
            {
                "session.key": "tui:x",
                "channel": "tui",
                "subagent.external.frames.path": str(journal),
                "subagent.external.frames.session_id": "s",
                "subagent.external.frames.start": 0,
                "subagent.external.frames.end": journal.stat().st_size if journal.exists() else 0,
            },
        ),
    ]
    (state / "logs" / "audit-spans.log").write_text("\n".join(json.dumps(s) for s in spans) + "\n", encoding="utf-8")


def test_backfill_writes_the_runs_steps_once(tmp_path: Path, monkeypatch):
    from raven.tracing import spans as tracing_spans

    state = _state(tmp_path)
    journal = tmp_path / "journal.jsonl"
    _journal(journal)
    _spans(state, journal)
    captured: list[dict] = []
    artifacts = state / "logs" / "audit-artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    log = state / "logs" / "audit-spans.log"

    def emit(record: dict) -> None:
        captured.append(record)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    monkeypatch.setattr(tracing_spans, "emit", emit)
    monkeypatch.setattr(
        tracing_spans,
        "persist_artifact",
        lambda kind, meta, payload, label=None: {"path": str(artifacts / f"{label}.json"), "sha1": "x", "bytes": 1},
    )

    dry = frames_backfill.backfill(state, dry_run=True)
    assert dry["runs"] == 1 and dry["written"] == 1 and dry["steps"] >= 3
    assert captured == []

    first = frames_backfill.backfill(state)
    assert first["written"] == 1 and first["steps"] == dry["steps"]
    ids = [s["spanId"] for s in captured]
    assert ids == sorted(ids) or len(set(ids)) == len(ids)
    assert {s["parentSpanId"] for s in captured} == {"ext"}
    assert {s["name"] for s in captured} == {"llm.call", "tool.call"}

    before = len(captured)
    again = frames_backfill.backfill(state)
    assert again["written"] == 0 and again["steps"] == 0 and again["already"] == 1
    assert len(captured) == before


def test_a_run_whose_journal_is_gone_is_left_alone(tmp_path: Path, monkeypatch):
    from raven.tracing import spans as tracing_spans

    state = _state(tmp_path)
    missing = tmp_path / "gone.jsonl"
    _spans(state, missing)
    captured: list[dict] = []
    monkeypatch.setattr(tracing_spans, "emit", captured.append)
    result = frames_backfill.backfill(state)
    assert result == {"runs": 0, "written": 0, "steps": 0, "already": 0, "unread": 0, "repaired": 0}
    assert captured == []


RUN_START = "2026-10-27T09:00:00+00:00"
RUN_END = "2026-10-27T09:00:40+00:00"


def _timed_journal(path: Path) -> None:
    """A run's frames, each at its own moment; the closing message is the last."""
    lines = []

    def frame(at: str, direction: str, body: dict) -> None:
        lines.append(json.dumps({"_type": "acp_frame", "timestamp": at, "dir": direction, "frame": body}))

    def update(body: dict) -> dict:
        return {"method": "session/update", "params": {"sessionId": "s", "update": body}}

    frame(
        "2026-10-27T09:00:01+00:00",
        "out",
        {"method": "session/prompt", "params": {"sessionId": "s", "prompt": [{"type": "text", "text": "do it"}]}},
    )
    frame(
        "2026-10-27T09:00:05+00:00",
        "in",
        update({"sessionUpdate": "agent_message_chunk", "content": [{"type": "text", "text": "on it"}]}),
    )
    frame(
        "2026-10-27T09:00:10+00:00",
        "in",
        update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "c1",
                "title": "read_file",
                "kind": "read",
                "status": "in_progress",
                "rawInput": {"path": "a.py"},
            }
        ),
    )
    frame(
        "2026-10-27T09:00:12+00:00",
        "in",
        update({"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed", "rawOutput": "ok"}),
    )
    frame(
        "2026-10-27T09:00:20+00:00",
        "in",
        update({"sessionUpdate": "agent_message_chunk", "content": [{"type": "text", "text": "done"}]}),
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _timed_run(state: Path, journal: Path) -> None:
    ext = {
        "schemaVersion": "audit.span.v1",
        "traceId": "t",
        "spanId": "ext",
        "parentSpanId": "run",
        "name": "subagent.external",
        "startTime": RUN_START,
        "endTime": RUN_END,
        "status": {"code": "OK", "message": ""},
        "events": [],
        "attributes": {
            "session.key": "tui:x",
            "channel": "tui",
            "subagent.external.frames.path": str(journal),
            "subagent.external.frames.session_id": "s",
            "subagent.external.frames.start": 0,
            "subagent.external.frames.end": journal.stat().st_size,
        },
    }
    (state / "logs" / "audit-spans.log").write_text(json.dumps(ext) + "\n", encoding="utf-8")


def _recording(state: Path, monkeypatch) -> list[dict]:
    """Every emitted span, also appended to the log the way the real store does."""
    from raven.tracing import spans as tracing_spans

    captured: list[dict] = []
    artifacts = state / "logs" / "audit-artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    log = state / "logs" / "audit-spans.log"

    def emit(record: dict) -> None:
        captured.append(record)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    monkeypatch.setattr(tracing_spans, "emit", emit)
    monkeypatch.setattr(
        tracing_spans,
        "persist_artifact",
        lambda kind, meta, payload, label=None: {"path": str(artifacts / f"{label}.json"), "sha1": "x", "bytes": 1},
    )
    return captured


def test_the_last_step_closes_at_the_runs_end_not_when_the_backfill_ran(tmp_path: Path, monkeypatch):
    state = _state(tmp_path)
    journal = tmp_path / "journal.jsonl"
    _timed_journal(journal)
    _timed_run(state, journal)
    captured = _recording(state, monkeypatch)

    frames_backfill.backfill(state)

    ends = [datetime.fromisoformat(s["endTime"]) for s in captured]
    assert ends and max(ends) == datetime.fromisoformat(RUN_END)


def test_a_step_an_earlier_pass_left_running_past_its_run_is_closed_at_the_runs_end(tmp_path: Path, monkeypatch):
    state = _state(tmp_path)
    journal = tmp_path / "journal.jsonl"
    _timed_journal(journal)
    _timed_run(state, journal)
    captured = _recording(state, monkeypatch)
    frames_backfill.backfill(state)
    last = max(captured, key=lambda s: s["startTime"])
    late = {**last, "endTime": "2026-10-29T08:08:43+00:00"}
    with (state / "logs" / "audit-spans.log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(late) + "\n")
    captured.clear()

    dry = frames_backfill.backfill(state, dry_run=True)
    assert dry["repaired"] == 1 and captured == []

    fixed = frames_backfill.backfill(state)
    assert fixed["repaired"] == 1 and fixed["written"] == 0
    assert [s["spanId"] for s in captured] == [last["spanId"]]
    assert datetime.fromisoformat(captured[0]["endTime"]) == datetime.fromisoformat(RUN_END)
    assert {k: v for k, v in captured[0].items() if k != "endTime"} == {k: v for k, v in late.items() if k != "endTime"}

    assert frames_backfill.backfill(state)["repaired"] == 0


def _late_step_journal(path: Path) -> None:
    """A run whose last step is stamped after the run closed: the journal keeps
    naive local times, and a reader in another zone, or across a clock step,
    reads one past the span's end."""
    lines = []

    def frame(at: str, body: dict) -> None:
        lines.append(json.dumps({"_type": "acp_frame", "timestamp": at, "dir": "in", "frame": body}))

    def update(body: dict) -> dict:
        return {"method": "session/update", "params": {"sessionId": "s", "update": body}}

    frame(
        "2026-10-27T09:00:01+00:00",
        {"method": "session/prompt", "params": {"sessionId": "s", "prompt": [{"type": "text", "text": "do it"}]}},
    )
    frame(
        "2026-10-27T09:00:05+00:00",
        update({"sessionUpdate": "agent_message_chunk", "content": [{"type": "text", "text": "on it"}]}),
    )
    frame(
        "2026-10-27T09:00:45+00:00",
        update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "c9",
                "title": "read_file",
                "kind": "read",
                "status": "in_progress",
            }
        ),
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_a_step_stamped_after_its_run_closed_is_drawn_inside_the_run(tmp_path: Path, monkeypatch):
    state = _state(tmp_path)
    journal = tmp_path / "journal.jsonl"
    _late_step_journal(journal)
    _timed_run(state, journal)
    captured = _recording(state, monkeypatch)

    frames_backfill.backfill(state)

    opened, closed = datetime.fromisoformat(RUN_START), datetime.fromisoformat(RUN_END)
    assert len(captured) == 3
    for step in captured:
        start, end = datetime.fromisoformat(step["startTime"]), datetime.fromisoformat(step["endTime"])
        assert opened <= start <= end <= closed, (step["spanId"], step["startTime"], step["endTime"])


def test_a_second_pass_over_a_late_step_writes_nothing(tmp_path: Path, monkeypatch):
    state = _state(tmp_path)
    journal = tmp_path / "journal.jsonl"
    _late_step_journal(journal)
    _timed_run(state, journal)
    captured = _recording(state, monkeypatch)
    frames_backfill.backfill(state)
    captured.clear()

    again = frames_backfill.backfill(state)

    assert (again["written"], again["repaired"], captured) == (0, 0, [])


def test_a_step_an_earlier_pass_wrote_wholly_after_its_run_is_moved_inside_it_once(tmp_path: Path, monkeypatch):
    state = _state(tmp_path)
    journal = tmp_path / "journal.jsonl"
    _late_step_journal(journal)
    _timed_run(state, journal)
    captured = _recording(state, monkeypatch)
    frames_backfill.backfill(state)
    last = max(captured, key=lambda s: s["spanId"])
    stray = {**last, "startTime": "2026-10-27T09:00:45+00:00", "endTime": "2026-10-27T09:00:45+00:00"}
    with (state / "logs" / "audit-spans.log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(stray) + "\n")
    captured.clear()

    fixed = frames_backfill.backfill(state)

    assert fixed["repaired"] == 1
    (moved,) = captured
    assert datetime.fromisoformat(moved["startTime"]) <= datetime.fromisoformat(moved["endTime"])
    assert datetime.fromisoformat(moved["endTime"]) == datetime.fromisoformat(RUN_END)
    captured.clear()
    assert frames_backfill.backfill(state)["repaired"] == 0
    assert captured == []


def test_a_run_whose_stretch_the_journal_no_longer_holds_is_not_counted_on_record(tmp_path: Path, monkeypatch):
    """A run whose recorded stretch reads back empty -- the journal shorter than
    it, or the stretch past the reader's cap -- has no steps written and none
    on record: it is reported as unread, not as already done."""
    state = _state(tmp_path)
    journal = tmp_path / "journal.jsonl"
    _timed_journal(journal)
    _timed_run(state, journal)
    log = state / "logs" / "audit-spans.log"
    run = json.loads(log.read_text().splitlines()[0])
    size = journal.stat().st_size
    run["attributes"]["subagent.external.frames.start"] = size
    run["attributes"]["subagent.external.frames.end"] = size + 4096
    log.write_text(json.dumps(run) + "\n", encoding="utf-8")
    captured = _recording(state, monkeypatch)

    result = frames_backfill.backfill(state)

    assert (result["runs"], result["written"], result["already"], result["unread"]) == (1, 0, 0, 1)
    assert captured == []


def test_a_result_stamped_after_the_run_closed_does_not_stretch_its_step(tmp_path: Path, monkeypatch):
    state = _state(tmp_path)
    journal = tmp_path / "journal.jsonl"
    lines = [
        {
            "_type": "acp_frame",
            "timestamp": "2026-10-27T09:00:01+00:00",
            "dir": "out",
            "frame": {
                "method": "session/prompt",
                "params": {"sessionId": "s", "prompt": [{"type": "text", "text": "do it"}]},
            },
        },
        {
            "_type": "acp_frame",
            "timestamp": "2026-10-27T09:00:10+00:00",
            "dir": "in",
            "frame": {
                "method": "session/update",
                "params": {
                    "sessionId": "s",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "c1",
                        "title": "read_file",
                        "kind": "read",
                        "status": "in_progress",
                        "rawInput": {"path": "a.py"},
                    },
                },
            },
        },
        {
            "_type": "acp_frame",
            "timestamp": "2026-10-27T09:00:55+00:00",
            "dir": "in",
            "frame": {
                "method": "session/update",
                "params": {
                    "sessionId": "s",
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "c1",
                        "status": "completed",
                        "rawOutput": "ok",
                    },
                },
            },
        },
    ]
    journal.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    _timed_run(state, journal)
    captured = _recording(state, monkeypatch)

    frames_backfill.backfill(state)

    tool = next(s for s in captured if s["name"] == "tool.call")
    assert datetime.fromisoformat(tool["endTime"]) == datetime.fromisoformat(RUN_END)


def test_a_dry_run_or_a_step_already_on_record_persists_no_artifact(monkeypatch):
    """Artifacts are part of writing a step: a dry run counts the steps, and a
    step already on record is skipped, so neither may leave a file behind."""
    from raven.agent.subagent.backends import observability
    from raven.tracing import spans as tracing_spans

    persisted: list[str] = []
    emitted: list[dict] = []

    def persist(kind, meta, payload, *, label=None):
        persisted.append(kind)
        return {"path": f"/artifacts/{kind}.json", "sha1": "x", "bytes": 1}

    monkeypatch.setattr(tracing_spans, "persist_artifact", persist)
    monkeypatch.setattr(tracing_spans, "emit", emitted.append)

    class Handle:
        def identity(self):
            return {
                "trace_id": "t",
                "span_id": "ext",
                "session_key": None,
                "channel": None,
                "chat_id": None,
                "surface": None,
            }

    events = [{"t": "say", "text": "done", "at": datetime.now().isoformat()}]

    def steps(**how):
        return observability.record_steps(
            Handle(), prompt="do it", events=events, elapsed_ms=10, id_seed="0123456789", **how
        )

    counted = steps(dry_run=True)
    assert len(counted) == 2
    assert (persisted, emitted) == ([], [])

    assert steps(skip_ids=set(counted)) == []
    assert (persisted, emitted) == ([], [])

    assert steps() == counted
    assert sorted(persisted) == ["llm.input", "llm.output"]
    assert len(emitted) == 2


def test_a_dry_run_and_a_second_pass_leave_the_store_as_they_found_it(tmp_path: Path, monkeypatch):
    from raven.tracing import spans as tracing_spans

    state = _state(tmp_path)
    journal = tmp_path / "journal.jsonl"
    _journal(journal)
    _spans(state, journal)
    monkeypatch.setenv("RAVEN_TRACING", "1")
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(state))
    tracing_spans._store = None

    def files() -> dict[str, bytes]:
        return {str(p.relative_to(state)): p.read_bytes() for p in state.rglob("*") if p.is_file()}

    try:
        before = files()
        dry = frames_backfill.backfill(state, dry_run=True)
        assert dry["written"] == 1 and dry["steps"] >= 3
        assert files() == before

        frames_backfill.backfill(state)
        written = files()
        assert any("artifact" in name for name in set(written) - set(before)), "the real pass writes artifacts"

        again = frames_backfill.backfill(state)
        assert again["written"] == 0
        assert files() == written
    finally:
        tracing_spans._store = None


def test_a_backfill_reads_a_run_with_the_dialect_its_connections_handshake_chose(tmp_path: Path, monkeypatch):
    from raven.tracing import spans as tracing_spans

    state = _state(tmp_path)
    journal = tmp_path / "journal.jsonl"

    def line(direction: str, frame: dict) -> str:
        return (
            json.dumps({"_type": "acp_frame", "timestamp": "2026-10-27T09:00:01", "dir": direction, "frame": frame})
            + "\n"
        )

    def update(body: dict) -> dict:
        return {"method": "session/update", "params": {"sessionId": "s", "update": body}}

    text = line("out", {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    text += line("in", {"jsonrpc": "2.0", "id": 0, "result": {"agentInfo": {"name": "codex-acp"}}})
    handshake_end = len(text.encode("utf-8"))
    text += line(
        "out",
        {"method": "session/prompt", "params": {"sessionId": "s", "prompt": [{"type": "text", "text": "lint it"}]}},
    )
    text += line(
        "in",
        update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "c1",
                "title": "lint",
                "kind": "execute",
                "status": "in_progress",
                "rawInput": {"command": ["pnpm", "lint"]},
            }
        ),
    )
    text += line(
        "in",
        update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "c1",
                "status": "completed",
                "rawOutput": {"formatted_output": "2 missing keys", "exit_code": 1},
            }
        ),
    )
    journal.write_text(text, encoding="utf-8")
    _spans(state, journal)
    log = state / "logs" / "audit-spans.log"
    spans = [json.loads(row) for row in log.read_text(encoding="utf-8").splitlines()]
    spans[-1]["attributes"]["subagent.external.frames.handshake_end"] = handshake_end
    log.write_text("\n".join(json.dumps(span) for span in spans) + "\n", encoding="utf-8")
    _recording(state, monkeypatch)
    payloads: list[object] = []
    artifacts = state / "logs" / "audit-artifacts"
    monkeypatch.setattr(
        tracing_spans,
        "persist_artifact",
        lambda kind, meta, payload, label=None: (
            payloads.append(payload) or {"path": str(artifacts / f"{label}.json"), "sha1": "x", "bytes": 1}
        ),
    )

    frames_backfill.backfill(state)

    results = [p for p in payloads if isinstance(p, dict) and "ok" in p]
    assert results == [{"result": "2 missing keys", "ok": False}]


@pytest.fixture(autouse=True)
def _tracing_on(monkeypatch):
    """A backfill writes only while tracing is on: pin it on, whatever the shell exports."""
    monkeypatch.setenv("RAVEN_TRACING", "1")


def test_a_backfill_with_tracing_turned_off_writes_nothing_unless_forced(tmp_path: Path, monkeypatch):
    state = _state(tmp_path)
    journal = tmp_path / "journal.jsonl"
    _journal(journal)
    _spans(state, journal)
    captured = _recording(state, monkeypatch)
    monkeypatch.setenv("RAVEN_TRACING", "0")

    off = frames_backfill.backfill(state)
    assert captured == []
    assert off["tracing_off"] == 1 and off["written"] == 0

    dry = frames_backfill.backfill(state, dry_run=True)
    assert dry["written"] == 1 and captured == []

    forced = frames_backfill.backfill(state, force=True)
    assert forced["written"] == 1 and captured
