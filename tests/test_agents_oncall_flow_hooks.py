"""The turn-frame hook: four axes on the v3 surface, driven through ctx objects.

Part 2c of the oncall-flow plugin. Each axis is exercised the way the loop
drives it -- ``AgentHookContext`` objects sharing ONE metadata dict per turn
(the trunk pins that shape) -- and the assertions pin the fork behaviours the
axes carry: the window context reaches the SAME tool instances the factories
adopted (the hook-to-tool bridge), the owner's words land in the watched
campaign's notes and pull a waited-on wake to now, the work-to-watch
judgement is bought once per turn and its line rides ``append_note``, a
successful ops_check_later ends the turn on its own note (and a refusal does
not), and the turn's summed usage is stamped through ``observers`` with every
freight key spent. The real CronService backs the wake grant, exactly as the
tools' own tests construct it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow import wakes, watched  # noqa: E402
from oncall_flow.escalation import NOTES_FILE  # noqa: E402
from oncall_flow.flow import (  # noqa: E402
    OncallFlowHook,
    TurnCloseHook,
    TurnContextHook,
    WatchedPathHook,
    make_flow_hook,
)
from oncall_flow.instrument import log_event, read_events, write_meta  # noqa: E402
from oncall_flow.tools import base as tools_base  # noqa: E402
from oncall_flow.tools.ops import OpsCheckLaterTool, OpsKillTool, OpsSubmitTool  # noqa: E402
from oncall_flow.window import bind_window, task_fingerprint  # noqa: E402

from raven.contracts.llm_provider import LLMResponse, ToolCallRequest  # noqa: E402
from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402
from raven.plugins.context import PluginContext, RuntimeHandles, ServiceLocator  # noqa: E402
from raven.plugins.manifest import PluginManifest  # noqa: E402
from raven.proactive_engine.schedulers.cron.grant import NamespacedWakeScheduler  # noqa: E402
from raven.proactive_engine.schedulers.cron.service import CronService  # noqa: E402
from raven.security.trust import wrap_untrusted  # noqa: E402
from raven.spine.message import ChatType, Source  # noqa: E402
from raven.spine.turn import Origin, TurnRequest  # noqa: E402

NS = "oncall-flow"


@pytest.fixture(autouse=True)
def _fresh_roster():
    tools_base.reset_faces()
    yield
    tools_base.reset_faces()


def _grant(tmp_path: Path) -> NamespacedWakeScheduler:
    return NamespacedWakeScheduler(CronService(tmp_path / "jobs.json", allowed_channels=None), NS)


def _campaign(tmp_path: Path, name: str = "camp-a", **meta) -> Path:
    tools_base.set_home(tmp_path / "state")
    cdir = tools_base.ops_home() / name
    write_meta(cdir, {"objective_words": "hold the line", "backend": "mock", **meta})
    return cdir


def _adopt_faces(grant: NamespacedWakeScheduler | None = None):
    check = tools_base.adopt(OpsCheckLaterTool())
    submit = tools_base.adopt(OpsSubmitTool())
    kill = tools_base.adopt(OpsKillTool())
    if grant is not None:
        check.bind_runtime(RuntimeHandles(wake_scheduler=grant))
    return check, submit, kill


def _inbound(
    meta: dict,
    text: str,
    *,
    session_key: str = "tui:w1",
    origin: Origin = Origin.USER,
    channel: str = "tui",
    chat_id: str = "default",
) -> AgentHookContext:
    request = TurnRequest(
        origin=origin,
        source=Source(channel=channel, chat_id=chat_id, sender_id="owner", chat_type=ChatType.DM),
        text=text,
    )
    return AgentHookContext(session_key=session_key, turn_request=request, inbound_content=text, metadata=meta)


def _iteration(
    meta: dict,
    *,
    response: LLMResponse,
    messages: list[dict] | None = None,
    iteration: int = 1,
    turn_question: str = "",
    session_key: str = "tui:w1",
) -> AgentHookContext:
    return AgentHookContext(
        session_key=session_key,
        iteration=iteration,
        messages=messages or [],
        response=response,
        turn_question=turn_question,
        metadata=meta,
    )


class _Judge:
    """A scripted provider: one canned reply, or an exception; calls recorded."""

    def __init__(self, reply):
        self._reply = reply
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._reply, BaseException):
            raise self._reply
        return SimpleNamespace(content=self._reply)


# ── The manifest row and the D6 gate ────────────────────────────────


def test_the_manifest_contributes_the_flow_hook_and_the_factory_gates_on_the_slice(tmp_path: Path) -> None:
    manifest = PluginManifest.from_toml_path(PLUGIN_DIR / "raven-plugin.toml")
    assert [h.name for h in manifest.contributes.hooks] == ["oncall_flow"]
    assert manifest.contributes.hooks[0].factory == "oncall_flow.flow:make_flow_hook"
    services = ServiceLocator(workspace=tmp_path / "ws", user_id="u", agent_id="a")
    on = PluginContext(config={"enabled": True, "stateRoot": str(tmp_path / "state")}, services=services)
    hook = make_flow_hook(on)
    assert isinstance(hook, OncallFlowHook) and hook.name == "OncallFlowHook"
    assert tools_base.ops_home() == tmp_path / "state", "the hook factory wires the same home the tool factories do"
    off = PluginContext(config={}, services=services)
    assert make_flow_hook(off) is None, "a disabled slice steers nothing (D6)"


# ── Axis 1: the window context reaches the adopted faces ────────────


def test_inbound_context_reaches_every_adopted_face(tmp_path: Path) -> None:
    tools_base.set_home(tmp_path / "state")
    check, submit, kill = _adopt_faces()
    text = "watch the sweep on the A800 box and tell me when it lands"
    asyncio.run(TurnContextHook().before_user_inbound(_inbound({}, text)))
    for face in (check, submit):
        assert (face._channel, face._chat_id, face._session_key) == ("tui", "default", "tui:w1")
        assert face._task == task_fingerprint(text)
    assert not hasattr(kill, "_session_key"), "a face without the seam is left as the fork left it"


def test_a_cold_turn_gets_the_empty_window_context_at_iteration_one(tmp_path: Path) -> None:
    tools_base.set_home(tmp_path / "state")
    check, _, _ = _adopt_faces()
    meta: dict = {}
    wake_text = "[Ops campaign 'camp-a' round 2 due] read the ledger"
    ctx = _iteration(meta, response=LLMResponse(content="x"), turn_question=wake_text, session_key="cron:abc")
    asyncio.run(TurnContextHook().before_iteration(ctx))
    assert (check._channel, check._chat_id) == ("", ""), "no window: the declared wake_route must serve"
    assert check._session_key == "cron:abc" and check._task == task_fingerprint(wake_text)


def test_the_iteration_fallback_never_overwrites_a_live_inbound_context(tmp_path: Path) -> None:
    tools_base.set_home(tmp_path / "state")
    check, _, _ = _adopt_faces()
    meta: dict = {}
    hook = TurnContextHook()
    asyncio.run(hook.before_user_inbound(_inbound(meta, "watch it")))
    asyncio.run(hook.before_iteration(_iteration(meta, response=LLMResponse(content="x"), turn_question="watch it")))
    assert check._channel == "tui", "one turn, one context: the freight key says the inbound fire already ran"


# ── Axis 1: the owner's words on the record ─────────────────────────


def test_owner_words_land_in_the_watched_campaigns_notes(tmp_path: Path) -> None:
    cdir = _campaign(tmp_path)
    bind_window(tools_base.ops_home(), "tui:w1", "camp-a", pid=os.getpid())
    asyncio.run(TurnContextHook().before_user_inbound(_inbound({}, "widen the angle range")))
    rows = [json.loads(line) for line in (cdir / NOTES_FILE).read_text().splitlines()]
    assert [(r["source"], r["note"]) for r in rows] == [("owner", "widen the angle range")]


def test_slash_commands_wake_prompts_and_unbound_windows_are_not_captured(tmp_path: Path) -> None:
    cdir = _campaign(tmp_path)
    bind_window(tools_base.ops_home(), "tui:w1", "camp-a", pid=os.getpid())
    hook = TurnContextHook()
    asyncio.run(hook.before_user_inbound(_inbound({}, "/status")))
    asyncio.run(hook.before_user_inbound(_inbound({}, "check it", origin=Origin.CRON)))
    asyncio.run(hook.before_user_inbound(_inbound({}, "how is it going", session_key="tui:w2")))
    assert not (cdir / NOTES_FILE).exists(), (
        "a command, a wake prompt and a stranger's window are not the owner speaking"
    )


def test_an_owner_answer_pulls_the_waited_on_wake_to_now(tmp_path: Path) -> None:
    grant = _grant(tmp_path)
    cdir = _campaign(tmp_path)
    bind_window(tools_base.ops_home(), "tui:w1", "camp-a", pid=os.getpid())
    _adopt_faces(grant)
    log_event(cdir, "ask_owner", allowed=True, question="keep the 3rd trial?", blocks_progress=False)
    wakes.schedule_next_look(
        grant, campaign="camp-a", eta_seconds=3600, message="re-check", channel="tui", to="default"
    )

    asyncio.run(TurnContextHook().before_user_inbound(_inbound({}, "yes, keep it")))

    events = {e["kind"]: e for e in read_events(cdir)}
    assert events["owner_answered"]["woke"] is True
    job = wakes.pending_look(grant, "camp-a")
    assert job is not None and job.state.next_run_at_ms <= int(time.time() * 1000) + 5_000, (
        "the owner is right here; the twenty-minute timer must not be sat out"
    )
    rows = [json.loads(line) for line in (cdir / NOTES_FILE).read_text().splitlines()]
    assert rows and rows[-1]["note"] == "yes, keep it", (
        "the question is read before the note is written -- the capture's own note is what closes it"
    )


def test_a_volunteered_direction_writes_the_note_but_wakes_nothing(tmp_path: Path) -> None:
    grant = _grant(tmp_path)
    cdir = _campaign(tmp_path)
    bind_window(tools_base.ops_home(), "tui:w1", "camp-a", pid=os.getpid())
    _adopt_faces(grant)
    wakes.schedule_next_look(
        grant, campaign="camp-a", eta_seconds=3600, message="re-check", channel="tui", to="default"
    )
    before = wakes.pending_look(grant, "camp-a").state.next_run_at_ms

    asyncio.run(TurnContextHook().before_user_inbound(_inbound({}, "also widen the range")))

    assert (cdir / NOTES_FILE).exists()
    assert "owner_answered" not in [e["kind"] for e in read_events(cdir)]
    assert wakes.pending_look(grant, "camp-a").state.next_run_at_ms == before, (
        "no question was waiting; a stray 'ok' must not turn into a round of work"
    )


# ── Axis 3: the work-to-watch judgement and its nudge ────────────────


def _look(meta: dict, command: str = "ls /srv/case", tool: str = "exec", question: str = "") -> AgentHookContext:
    response = LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(id="t1", name=tool, arguments={"command": command})],
    )
    return _iteration(meta, response=response, turn_question=question or "run the case at /srv/case and stay with it")


def test_one_judgement_per_turn_and_the_nudge_rides_the_note(tmp_path: Path) -> None:
    tools_base.set_home(tmp_path / "state")
    judge = _Judge('{"watched": true, "subjects": ["/srv/case"]}')
    hook = WatchedPathHook(judge)
    meta: dict = {}
    first = asyncio.run(hook.after_iteration(_look(meta)))
    second = asyncio.run(hook.after_iteration(_look(meta, command="cat /srv/case/log")))
    assert first.append_note == watched.provenance_line().strip()
    assert second.append_note == watched.provenance_line().strip(), "the fork re-added the line per matching look"
    assert len(judge.calls) == 1, "one judgement per turn, cached on the turn's own metadata dict"
    assert judge.calls[0]["messages"] == watched.build_prompt("run the case at /srv/case and stay with it")


def test_not_watched_or_an_unclaimed_subject_adds_no_line(tmp_path: Path) -> None:
    tools_base.set_home(tmp_path / "state")
    quiet = asyncio.run(WatchedPathHook(_Judge('{"watched": false, "subjects": []}')).after_iteration(_look({})))
    assert quiet.append_note is None
    miss = asyncio.run(
        WatchedPathHook(_Judge('{"watched": true, "subjects": ["/opt/elsewhere"]}')).after_iteration(_look({}))
    )
    assert miss.append_note is None, "a verdict only claims the subjects it named"


def test_a_bound_window_skips_the_judgement(tmp_path: Path) -> None:
    _campaign(tmp_path)
    bind_window(tools_base.ops_home(), "tui:w1", "camp-a", pid=os.getpid())
    judge = _Judge('{"watched": true, "subjects": ["/srv/case"]}')
    decision = asyncio.run(WatchedPathHook(judge).after_iteration(_look({})))
    assert decision.append_note is None and judge.calls == [], (
        "already recording something: asking again would tell a driving loop to declare a second campaign"
    )


def test_a_judge_failure_leaves_the_look_alone_and_is_not_cached(tmp_path: Path) -> None:
    tools_base.set_home(tmp_path / "state")
    judge = _Judge(RuntimeError("provider down"))
    hook = WatchedPathHook(judge)
    meta: dict = {}
    assert asyncio.run(hook.after_iteration(_look(meta))).append_note is None
    assert asyncio.run(hook.after_iteration(_look(meta))).append_note is None
    assert len(judge.calls) == 2, "a failed judgement is retried on the next look, never cached as an answer"


def test_ops_exec_is_judged_like_the_forks_machine_face(tmp_path: Path) -> None:
    tools_base.set_home(tmp_path / "state")
    judge = _Judge('{"watched": true, "subjects": ["/srv/case"]}')
    decision = asyncio.run(WatchedPathHook(judge).after_iteration(_look({}, tool="ops_exec")))
    assert decision.append_note == watched.provenance_line().strip(), (
        "the fork's exec(machine=...) landed as ops_exec; a look through it is still a look"
    )


# ── Axis 4: the turn close ──────────────────────────────────────────


def _check_later_turn(meta: dict, note: str) -> AgentHookContext:
    response = LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(id="t1", name="ops_check_later", arguments={"eta_seconds": 120})],
    )
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        {
            "role": "tool",
            "tool_call_id": "t1",
            "name": "ops_check_later",
            "content": wrap_untrusted(note, source="ops_check_later"),
        },
    ]
    return _iteration(meta, response=response, messages=messages)


def test_a_scheduled_wake_ends_the_turn_on_its_own_note() -> None:
    note = "Scheduled a wake at ~2026-09-01T09:00:00 (job ops:camp-a)."
    decision = asyncio.run(TurnCloseHook().after_iteration(_check_later_turn({}, note)))
    assert decision.short_circuit_result == note, "the note comes back out of the loop's untrusted fence intact"


def test_a_refused_check_later_does_not_end_the_turn() -> None:
    decision = asyncio.run(
        TurnCloseHook().after_iteration(_check_later_turn({}, "REFUSED: cite a fresh observation first."))
    )
    assert decision.short_circuit_result is None and decision.pass_through, (
        "a refusal arranged nothing, and closing on it would end the turn with nothing decided"
    )


# ── Axis 2 + the composed hook: accounting, order, freight hygiene ──


def test_turn_totals_sum_across_iterations_and_stamp_the_observers(tmp_path: Path) -> None:
    tools_base.set_home(tmp_path / "state")
    _adopt_faces()
    hook = OncallFlowHook()
    meta: dict = {}

    async def turn() -> None:
        await hook.before_user_inbound(_inbound(meta, "just a question"))
        await hook.after_iteration(
            _iteration(
                meta,
                response=LLMResponse(
                    content="a", usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
                ),
            )
        )
        await hook.after_iteration(_iteration(meta, response=LLMResponse(content="b"), iteration=2))
        await hook.after_send(AgentHookContext(session_key="tui:w1", outbound_content="done", metadata=meta))

    asyncio.run(turn())
    stamped = meta["observers"]["oncall_flow"]
    assert (stamped["calls"], stamped["calls_without_usage"]) == (2, 1)
    assert (stamped["prompt_tokens"], stamped["completion_tokens"], stamped["total_tokens"]) == (100, 20, 120)
    assert stamped["faces"] == 2, "the two context-taking faces, counted by the bridge"
    assert set(meta) == {"observers"}, "every freight key is spent at the send fire; nothing leaks into the next turn"


def test_the_composed_hook_counts_the_closing_call_before_it_halts(tmp_path: Path) -> None:
    tools_base.set_home(tmp_path / "state")
    _adopt_faces()
    hook = OncallFlowHook()
    meta: dict = {}
    note = "Scheduled a wake at ~2026-09-01T09:00:00 (job ops:camp-a)."

    async def turn():
        await hook.before_user_inbound(_inbound(meta, "keep watching"))
        ctx = _check_later_turn(meta, note)
        ctx.response.usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        closed = await hook.after_iteration(ctx)
        await hook.after_send(AgentHookContext(session_key="tui:w1", outbound_content=note, metadata=meta))
        return closed

    closed = asyncio.run(turn())
    assert closed.short_circuit_result == note
    stamped = meta["observers"]["oncall_flow"]
    assert stamped["closed_by"] == "ops_check_later"
    assert stamped["calls"] == 1, "accounting runs before the close in the axis order, so the closing call is counted"
