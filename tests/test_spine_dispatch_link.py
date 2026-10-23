"""Which span a turn says dispatched it, end to end.

A real scheduler runs the turns, a real sub-agent manager dispatches and reports
back, the real tracer records them, and the trajectory projection and turn
selection read the result. Each report turn must name the run that sent it,
whenever and in whatever order it arrives; every other turn names nothing --
also when the host built its scheduler inside a span.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from raven.agent.subagent import manager as manager_mod
from raven.agent.subagent.manager import SubagentManager
from raven.observability import semconv
from raven.spine import ChatType, Origin, OriginPools, Scheduler, Source, TurnOutcome, TurnRequest, Usage
from raven.tracing import spans as _spans
from raven.tracing import trace
from raven.trajectory.entries import project_entries
from raven.trajectory.selection import session_turns

SESSION = "web:s1"
_ADDRESS = {"channel": "web", "chat_id": "s1", "session_key": SESSION}


def _user(text: str) -> TurnRequest:
    src = Source(channel="web", chat_id="s1", sender_id="u", chat_type=ChatType.DM)
    return TurnRequest(origin=Origin.USER, source=src, text=text, conversation=SESSION)


def _label(req: TurnRequest) -> str:
    """A report is named by what it reports on; anything else by its text."""
    return req.delegated["label"] if req.delegated else req.text


class _Executor:
    async def __aenter__(self) -> _Executor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _GatedBackend:
    """Answers a task once the test opens that task's gate."""

    def __init__(self) -> None:
        self.gates: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)

    async def run(self, task: str, **kwargs: Any) -> str:
        await self.gates[task].wait()
        return f"done {task}"


class _Runner:
    """Opens each turn the way ``AgentLoop._process_message`` does, and acts out the few turns the scenarios need."""

    def __init__(self) -> None:
        self.manager: SubagentManager | None = None
        self.scheduler: Scheduler | None = None
        self.task_ids: dict[str, str] = {}
        self.turn_spans: dict[str, str] = {}
        self.started: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self.done: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self.hold: dict[str, asyncio.Event] = {}

    async def run(self, req, emit, drain) -> TurnOutcome:
        await self._process_message(req, session_key=req.conversation)
        self.done[_label(req)].set()
        return TurnOutcome(usage=Usage(0, 0, 0), explicit_reply=False)

    @trace.instrument(
        "session.turn", root=True, seed=semconv.turn_seed, on_open=semconv.turn_open, extract=semconv.turn
    )
    async def _process_message(self, req: TurnRequest, session_key: str | None = None) -> None:
        label = _label(req)
        self.turn_spans[label] = trace.current_span().span_id
        self.started[label].set()
        words = req.text.split()
        if words[0] == "spawn":
            assert self.manager is not None
            with trace.span("tool.call", {"tool.name": "spawn"}):
                for name in words[1:]:
                    receipt = await self.manager.spawn(
                        task=name,
                        task_summary=name,
                        origin_channel="web",
                        origin_chat_id="s1",
                        session_key=SESSION,
                        agent="raven",
                    )
                    found = re.search(r"\(id: ([^)]+)\)", receipt)
                    assert found is not None, receipt
                    self.task_ids[name] = found.group(1)
        elif req.text == "pick":
            # A menu pick: a hook inside this turn submits the user's next one.
            assert self.scheduler is not None
            with trace.span("tool.call", {"tool.name": "pick"}):
                self.scheduler.submit(_user("picked"))
        if (gate := self.hold.get(label)) is not None:
            await gate.wait()


class _World:
    def __init__(self, trace_dir: Path, runner: _Runner, manager: SubagentManager, backend: _GatedBackend) -> None:
        self.trace_dir = trace_dir
        self.runner = runner
        self.manager = manager
        self.backend = backend

    def records(self) -> list[dict[str, Any]]:
        log = self.trace_dir / "logs" / "audit-spans.log"
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def link_of(self, label: str) -> tuple[str, str] | None:
        """The dispatch link the finished ``session.turn`` of ``label`` carries."""
        span_id = self.runner.turn_spans[label]
        (final,) = [
            r for r in self.records() if r["spanId"] == span_id and r["attributes"].get("turn.in_progress") is False
        ]
        return trace.dispatch_link_of(final["attributes"])

    def run_of(self, name: str) -> tuple[str, str]:
        task_id = self.runner.task_ids[name]
        (record,) = [
            r
            for r in self.records()
            if r["name"] == "subagent.run" and r["attributes"].get("subagent.task_id") == task_id
        ]
        return (record["traceId"], record["spanId"])

    def origins(self) -> dict[str, set[str]]:
        """Each turn's trajectory origins, keyed by the turn's label."""
        trace_of = {r["spanId"]: r["traceId"] for r in self.records() if r["name"] == "session.turn"}
        projection = project_entries(self.records(), state=self.trace_dir)
        by_trace: defaultdict[str, set[str]] = defaultdict(set)
        for entry in projection.entries:
            by_trace[entry.trace_id].add(entry.origin)
        return {label: by_trace[trace_of[span_id]] for label, span_id in self.runner.turn_spans.items()}

    def main_line(self) -> list[str]:
        """The labels of the session's main-line turns, in order."""
        label_of = {span_id: label for label, span_id in self.runner.turn_spans.items()}
        return [label_of[turn.span_id] for turn in session_turns(SESSION, answering=False, state_dir=self.trace_dir)]

    async def runs_reported(self) -> None:
        await asyncio.gather(*self.manager._running_tasks.values(), return_exceptions=True)


@pytest.fixture(params=["built-untraced", "built-in-a-span"])
async def world(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    trace_dir = tmp_path / "traces"
    monkeypatch.setenv("RAVEN_TRACING", "1")
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(trace_dir))
    _spans._store = None
    monkeypatch.setattr(manager_mod, "build_executor", lambda *a, **k: _Executor())

    backend = _GatedBackend()
    manager = SubagentManager(provider=SimpleNamespace(get_default_model=lambda: "stub"), workspace=tmp_path)
    manager._resolve_backend = lambda agent: backend
    runner = _Runner()

    async def _sink(_event) -> None:
        return None

    # A scheduler keeps the context it was built in for every turn, so one built
    # inside a span must still give each turn no link but the one it states.
    building = trace.span("startup.build") if request.param == "built-in-a-span" else contextlib.nullcontext()
    with building:
        scheduler = Scheduler(runner, OriginPools(user=4, system=4), _sink)
    manager.set_submit(scheduler.submit)
    runner.manager, runner.scheduler = manager, scheduler
    try:
        yield _World(trace_dir, runner, manager, backend), scheduler
    finally:
        for gate in list(backend.gates.values()) + list(runner.hold.values()):
            gate.set()
        await manager.cancel_all()
        await scheduler.shutdown(grace=0)
        _spans._store = None


async def test_reports_arriving_while_idle_each_name_their_own_run_and_the_next_user_turn_names_none(world) -> None:
    """A reports into an idle session, B queues behind A's report, and the user speaks after both."""
    w, scheduler = world
    await scheduler.submit(_user("spawn A B")).result()

    w.runner.hold["A"] = asyncio.Event()
    w.backend.gates["A"].set()
    await w.runner.started["A"].wait()
    w.backend.gates["B"].set()
    await w.runs_reported()
    after = scheduler.submit(_user("user after"))
    w.runner.hold["A"].set()
    await after.result()
    await w.runner.done["B"].wait()

    assert w.link_of("A") == w.run_of("A")
    assert w.link_of("B") == w.run_of("B")
    assert w.link_of("spawn A B") is None
    assert w.link_of("user after") is None
    origins = w.origins()
    assert origins["user after"] == {"main"}
    assert origins["A"] == {"subagent"} and origins["B"] == {"subagent"}
    assert w.main_line() == ["spawn A B", "user after"]


async def test_a_report_arriving_while_the_session_is_busy_names_its_run(world) -> None:
    w, scheduler = world
    await scheduler.submit(_user("spawn C")).result()

    w.runner.hold["long"] = asyncio.Event()
    long_turn = scheduler.submit(_user("long"))
    await w.runner.started["long"].wait()
    w.backend.gates["C"].set()
    await w.runs_reported()
    w.runner.hold["long"].set()
    await long_turn.result()
    await w.runner.done["C"].wait()

    assert w.link_of("C") == w.run_of("C")
    assert w.link_of("long") is None
    assert w.main_line() == ["spawn C", "long"]


async def test_a_user_turn_submitted_inside_a_running_turn_names_nothing(world) -> None:
    w, scheduler = world
    await scheduler.submit(_user("pick")).result()
    await w.runner.done["picked"].wait()

    assert w.link_of("picked") is None
    assert w.main_line() == ["pick", "picked"]


async def test_an_unprompted_report_names_nothing_whatever_span_it_comes_from(world) -> None:
    w, _scheduler = world
    w.manager.remember_origin(dict(_ADDRESS))

    with trace.span("acp.read"):
        await w.manager.announce_unprompted_turn(SESSION, "watch", "h1", "the job finished")
    await w.runner.done["watch/h1"].wait()

    assert w.link_of("watch/h1") is None
    assert w.main_line() == ["watch/h1"]
