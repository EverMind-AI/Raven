"""Real packages of the three kinds an Imported Conversation is read from, built by their real writers.

Every package here comes out of the code that ships it -- ``write_export`` for a
Trajectory Export, ``collect_bundle`` + ``redact_bundle`` + ``pack_report`` for a
Trajectory Report, the bug report pipeline for a Bug Report Package -- so a
change to any writer that the reader does not follow fails the reader's tests.

The source store holds two main-line turns of one session: the first calls the
model with a v2 shell (optionally over a conversation whose messages together
are longer than the per-artifact read cap, each one under it), calls a tool and
dispatches a sub-agent; the second calls the model once more.
"""

from __future__ import annotations

import json
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

from raven.session.manager import SessionManager
from raven.tracing import artifact_v2
from raven.trajectory import bugreport as breport
from raven.trajectory import export as ex
from raven.trajectory import review as treview
from raven.trajectory import selection as sel
from raven.trajectory.bundle import collect_bundle
from raven.trajectory.redact import redact_bundle
from raven.trajectory.report import pack_report

SESSION = "tui:trip"
TITLE = "Trip planning"
EXPORT_ID = "ex-20261001-abc123"
LONG_ROUNDS = 60
LONG_MESSAGE_CHARS = 10 * 1024
SYSTEM_TEXT = "you are raven"
FIRST_INPUT = "plan a trip to the coast"
SECOND_INPUT = "and book the hotel"


@dataclass(frozen=True)
class Source:
    state: Path
    workspace: Path
    session_file: Path
    messages: list[dict[str, Any]]
    first_call: list[dict[str, Any]]


def _artifact(state: Path, kind: str, name: str, payload: Any) -> Path:
    path = state / "logs" / "audit-artifacts" / kind / "2026-10-01" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def _shell(state: Path, name: str, messages: list[dict[str, Any]]) -> Path:
    refs = []
    for message in messages:
        sha1 = artifact_v2.message_sha1(message)
        blob = artifact_v2.message_path(state / "logs" / "audit-artifacts", sha1)
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_text(artifact_v2.message_text(message), encoding="utf-8")
        refs.append(artifact_v2.make_ref(sha1))
    system = next(i for i, m in enumerate(messages) if m["role"] == "system")
    user = max(i for i, m in enumerate(messages) if m["role"] == "user")
    return _artifact(
        state,
        "llm.input",
        name,
        {
            "artifactFormat": artifact_v2.ARTIFACT_FORMAT,
            "provider": "test",
            "model": "test-model",
            "systemPrompt": refs[system],
            "prompt": refs[user],
            "messages": refs,
            "tools": [],
        },
    )


def _span(trace: str, span: str, name: str, minute: int, *, parent: str | None = None, **attrs: Any) -> dict:
    record: dict[str, Any] = {
        "schemaVersion": "audit.span.v1",
        "traceId": trace,
        "spanId": span,
        "name": name,
        "startTime": f"2026-10-01T10:{minute:02d}:00+00:00",
        "endTime": f"2026-10-01T10:{minute:02d}:30+00:00",
        "status": {"code": "OK"},
        "attributes": {"session.key": SESSION, **attrs},
    }
    if parent:
        record["parentSpanId"] = parent
    return record


def write_source(base: Path, *, long_input: bool = False) -> Source:
    """A trace store and a session record under ``base`` holding the two-turn conversation."""
    state = base / "traces"
    workspace = base / "workspace"
    (state / "logs" / "audit-artifacts").mkdir(parents=True)
    workspace.mkdir(parents=True)
    history: list[dict[str, Any]] = []
    for round_ in range(LONG_ROUNDS if long_input else 1):
        filler = (f"round {round_} of the coast road " * LONG_MESSAGE_CHARS)[:LONG_MESSAGE_CHARS]
        history += [{"role": "user", "content": f"question {round_}"}, {"role": "assistant", "content": filler}]
    first_call = [{"role": "system", "content": SYSTEM_TEXT}, *history, {"role": "user", "content": FIRST_INPUT}]
    second_call = [
        {"role": "system", "content": SYSTEM_TEXT},
        {"role": "user", "content": FIRST_INPUT},
        {"role": "assistant", "content": "found three hotels"},
        {"role": "user", "content": SECOND_INPUT},
    ]
    first_shell = _shell(state, "a-llm.json", first_call)
    second_shell = _shell(state, "b-llm.json", second_call)
    tool_out = _artifact(state, "tool.output", "a-tool.txt", "weather: sunny")
    outside = base / "outside-the-store.json"
    outside.write_text("{}", encoding="utf-8")
    sub_out = _artifact(state, "turn.output", "sub-out.json", {"content": "found 3 hotels"})
    records = [
        _span(
            "trace-a",
            "span-a",
            "session.turn",
            1,
            **{"turn.input_preview": FIRST_INPUT, "turn.in_progress": False},
        ),
        _span("trace-a", "span-a1", "llm.call", 1, parent="span-a", **{"llm.input.artifact_path": str(first_shell)}),
        _span(
            "trace-a",
            "span-a2",
            "tool.call",
            1,
            parent="span-a",
            **{"tool.output.artifact_path": str(tool_out), "tool.input.artifact_path": str(outside)},
        ),
        _span(
            "trace-sub",
            "span-s",
            "session.turn",
            2,
            **{
                "session.key": "sub:x",
                "trace.dispatched_in_trace_id": "trace-a",
                "trace.dispatched_by_span_id": "span-a2",
                "turn.output.artifact_path": str(sub_out),
                "turn.in_progress": False,
            },
        ),
        _span(
            "trace-b",
            "span-b",
            "session.turn",
            5,
            **{"turn.input_preview": SECOND_INPUT, "turn.in_progress": False},
        ),
        _span("trace-b", "span-b1", "llm.call", 5, parent="span-b", **{"llm.input.artifact_path": str(second_shell)}),
    ]
    log = state / "logs" / "audit-spans.log"
    log.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    manager = SessionManager(workspace)
    session = manager.get_or_create(SESSION)
    session.add_message("user", FIRST_INPUT, trace_id="trace-a")
    session.add_message("assistant", "found three hotels")
    session.add_message("user", SECOND_INPUT, trace_id="trace-b")
    session.add_message("assistant", "booked")
    session.set_title(TITLE)
    manager.save(session)
    session_file = manager.session_path(SESSION)
    return Source(state, workspace, session_file, list(session.messages), first_call)


def build_export(base: Path, *, long_input: bool = False) -> tuple[Path, Source]:
    source = write_source(base, long_input=long_input)
    plan = sel.plan_selection(SESSION, ["trace-a", "trace-b"], answering=False, state_dir=source.state)
    selection = sel.collect_selection(plan, state_dir=source.state)
    cut = sel.cut_session_record(source.session_file, plan, state_dir=source.state)
    out = base / "out" / "export.tar.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    ex.write_export(selection, cut, out, export_id=EXPORT_ID, state_dir=source.state)
    return out, source


def build_report(base: Path, *, long_input: bool = False) -> tuple[Path, Source]:
    source = write_source(base, long_input=long_input)
    bundle = collect_bundle("trace-a", base / "bundles", source.state, workspace=source.workspace)
    redacted = base / "redacted" / bundle.name
    redact_bundle(bundle, redacted, secrets=[])
    out = base / "out" / f"{bundle.name}.tar.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    return pack_report(redacted, out), source


def _decide_all(items: list[treview.ReviewItem], _reasons: list[str]) -> list[treview.ReviewDecision]:
    return [
        treview.ReviewDecision(
            item.id,
            treview.ACTION_ACKNOWLEDGED if item.kind == treview.KIND_CONFIRMED else treview.ACTION_KEPT,
        )
        for item in items
    ]


def build_bug_report(
    base: Path, *, description: str = "The hotel was never booked\nsteps follow", long_input: bool = False
) -> tuple[Path, Source]:
    source = write_source(base, long_input=long_input)
    with mock.patch.object(breport, "collect_known_secrets", lambda _p: ([], True)):
        prep = breport.prepare_trajectory("trace-a", workspace=source.workspace, state_dir=source.state)
        prep = breport.freeze_export(prep, description=description, severity="high", decide=_decide_all)
        _, record = breport.confirm_and_package(prep, state_dir=source.state)
    return Path(record["package"]["path"]), source


def members(path: Path) -> list[str]:
    with tarfile.open(path, "r:gz") as tar:
        return tar.getnames()
