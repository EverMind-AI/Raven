"""Spine wiring for the one-shot ``agent -m`` path: the runner (an
AgentTurnRunner with stream=False, so the reply is one Text), the outlet that
renders a turn's text to the console, and the sink that feeds the delivery hub.

The turn runs through the spine (submit -> lane -> run_turn -> hub -> outlet);
the spine never imports cli, cli imports the spine.
"""

import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from raven.agent.spine_runner import AgentTurnRunner
from raven.contracts.token_strategy import UsageSnapshot
from raven.spine import (
    Deliverable,
    Notice,
    NoticeKind,
    OriginPools,
    Scheduler,
    Text,
    ToolEvent,
    ToolPhase,
    TurnEvent,
    TurnFailed,
    TurnRequest,
)
from raven.spine.delivery import Capabilities, DeliveryHub, make_hub_sink
from raven.spine.events import Reasoning


def _fmt_tokens(n: int) -> str:
    if n >= 1000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(n)


class TurnUsageSummary:
    """Per-turn usage accounting for the CLI summary line.

    Fed by an in-memory UsageTracker registered on the agent loop's TokenWise
    registry (same source the persistent telemetry tracker reads). The runner
    wrapper marks the turn start; the CliOutlet takes the delta after the
    turn's reply renders. ``take_line`` advances the baseline so a second
    deliverable in the same turn cannot double-report.

    That tracker sees only this process. A product sub-agent runs as an ACP
    process of its own and records its calls there, so ``delegated`` -- given
    the root session and the window since the last line -- adds what those
    processes billed to this conversation; without it a turn that delegated
    most of its work reported a fraction of what it cost."""

    def __init__(
        self,
        tracker: Any,
        *,
        delegated: Callable[[str, datetime, datetime], UsageSnapshot] | None = None,
    ) -> None:
        self._tracker = tracker
        self._delegated = delegated
        self._baseline = tracker.snapshot()
        self._started: float | None = None
        self._root = ""
        self._since: datetime | None = None

    def turn_started(self, root: str = "") -> None:
        self._baseline = self._tracker.snapshot()
        self._started = time.monotonic()
        if self._since is None or root != self._root:
            self._root = root
            self._since = datetime.now(timezone.utc)

    def _delegated_since_last_line(self) -> UsageSnapshot | None:
        if self._delegated is None or not self._root or self._since is None:
            return None
        until = datetime.now(timezone.utc)
        since, self._since = self._since, until
        try:
            return self._delegated(self._root, since, until)
        except Exception:  # noqa: BLE001 - a summary line must not fail the turn it reports
            logger.opt(exception=True).debug("one-shot summary: delegated usage unreadable")
            return None

    def take_line(self) -> str | None:
        total = self._tracker.snapshot()
        base = self._baseline
        self._baseline = total
        delegated = self._delegated_since_last_line()
        extra = delegated or UsageSnapshot(model="__none__", cache_read_tokens=0, cache_write_tokens=0, cost_usd=0.0)
        in_tokens = (
            ((total.input_tokens or 0) - (base.input_tokens or 0))
            + ((total.cache_read_tokens or 0) - (base.cache_read_tokens or 0))
            + ((total.cache_write_tokens or 0) - (base.cache_write_tokens or 0))
            + (extra.input_tokens or 0)
            + (extra.cache_read_tokens or 0)
            + (extra.cache_write_tokens or 0)
        )
        out_tokens = (total.output_tokens or 0) - (base.output_tokens or 0) + (extra.output_tokens or 0)
        calls = total.calls - base.calls + extra.calls
        if in_tokens <= 0 and out_tokens <= 0 and calls <= 0:
            return None
        input_missing = total.input_missing_calls - base.input_missing_calls + extra.input_missing_calls
        output_missing = total.output_missing_calls - base.output_missing_calls + extra.output_missing_calls
        input_text = "unknown" if calls and input_missing == calls else _fmt_tokens(in_tokens)
        output_text = "unknown" if calls and output_missing == calls else _fmt_tokens(out_tokens)
        parts = [f"{input_text} in / {output_text} out tokens"]
        if input_missing and input_missing < calls:
            parts.append(f"{input_missing} calls with unknown input tokens")
        if output_missing and output_missing < calls:
            parts.append(f"{output_missing} calls with unknown output tokens")
        missing = total.cost_missing_calls - base.cost_missing_calls + extra.cost_missing_calls
        cost = (total.cost_usd or 0.0) - (base.cost_usd or 0.0) + (extra.cost_usd or 0.0)
        priced = total.cost_usd is not None or (extra.calls and extra.cost_usd is not None)
        if priced and (calls == 0 or calls > missing):
            parts.append("<$0.0001" if 0 < cost < 0.0001 else "$" + f"{cost:.4f}".rstrip("0").rstrip("."))
        else:
            parts.append("cost unknown")
        if missing:
            parts.append(f"{missing} calls with unknown cost")
        if extra.calls:
            parts.append(f"incl. {extra.calls} sub-agent calls")
        if self._started is not None:
            parts.append(f"{time.monotonic() - self._started:.1f}s")
        return " · ".join(parts)


class _SummaryTurnRunner:
    """Marks the turn boundary for TurnUsageSummary; delegates the turn."""

    def __init__(self, inner: Any, summary: TurnUsageSummary) -> None:
        self._inner = inner
        self._summary = summary

    async def run(self, req: TurnRequest, emit: Any, drain: Any) -> Any:
        self._summary.turn_started(req.conversation or "")
        return await self._inner.run(req, emit, drain)


class _OneShotTurnRunner(AgentTurnRunner):
    """The one-shot runner: the plain agent runner plus its permission binding.

    Bound to None by decision, not omission: a ``-m`` turn prints one reply and
    exits, and it has no human round-trip at all -- ``ask_user`` is structurally
    unavailable here (no broker is ever wired), so an approval prompt would have
    nobody to reach either. The ask tier therefore refuses with a reason, the
    same answer a question gets on this surface, and the operator picks smart
    or full for one-shot work that must mutate.

    Every bound turn is kept: the gate appends refusals to it from inside the
    turn's own task, including background sub-agents that inherited the object.
    The caller reads all of them after background work and follow-up turns have
    settled.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.last_turn: Any = None
        self.turns: list[Any] = []

    async def run(self, req: TurnRequest, emit: Any, drain: Any) -> Any:
        from raven.permissions import start_permission_turn

        self.last_turn = start_permission_turn(
            None,
            conversation_id=req.conversation or "",
            turn_id=req.turn_id or "",
        )
        self.turns.append(self.last_turn)
        return await super().run(req, emit, drain)

    def refusals(self) -> list[Any]:
        """What this run's turn had refused, in the order it refused it."""
        return [refusal for turn in self.turns for refusal in (getattr(turn, "refusals", ()) or ())]


def _build_turn_summary(agent_loop: Any) -> TurnUsageSummary | None:
    """Wire the per-turn usage summary when the config switch is on.

    Returns None (feature fully off, zero output) when ``cli.turn_summary``
    is false or the loop has no TokenWise registry to observe (test stubs)."""
    strategies = getattr(agent_loop, "strategies", None)
    if strategies is None:
        return None
    from raven.config.loader import load_config

    if not load_config().cli.turn_summary:
        return None
    from raven.token_wise.usage_tracker import UsageTracker, delegated_usage

    tracker = UsageTracker(persist=False)
    strategies.register(tracker)
    return TurnUsageSummary(
        tracker,
        delegated=lambda root, since, until: delegated_usage(root, since, until=until),
    )


def _render_summary_line(line: str) -> None:
    from rich.console import Console

    # Two-space indent matches the render_notice progress lines in
    # agent_commands, so the summary sits in the same visual column.
    Console().print(f"  [dim]↳ {line}[/dim]")


class CliOutlet:
    """Renders a turn's deliverables to the terminal. Runs non-streaming (run_turn
    stream=False), so the reply arrives as one Text; MediaOut is eaten.

    ``render_notice`` is opt-in progress rendering: when set, a Notice (and the
    Reasoning a long tool like deep_research streams, see ``deliver``) renders as
    a progress line, gated by ``send_progress`` (PROGRESS) and ``send_tool_hints``
    (TOOL_HINT). The one-shot ``-m`` path wires it so deep_research progress is
    visible; note this also surfaces the model's per-tool progress hint on every
    tool call, gated by the same flags. A surface that omits it eats Notice /
    Reasoning as before."""

    def __init__(
        self,
        channel: str,
        render: Callable[[str], None],
        *,
        render_notice: Callable[[str], None] | None = None,
        send_progress: bool = False,
        send_tool_hints: bool = False,
        summary: TurnUsageSummary | None = None,
    ) -> None:
        self.name = channel
        self.capabilities = Capabilities()
        self._render = render
        self._render_notice = render_notice
        self._send_progress = send_progress
        self._send_tool_hints = send_tool_hints
        self._summary = summary

    async def deliver(self, out: Deliverable) -> None:
        if isinstance(out, Text):
            self._render(out.content)
            if self._summary is not None:
                line = self._summary.take_line()
                if line:
                    _render_summary_line(line)
        elif isinstance(out, Notice) and self._render_notice is not None:
            if out.kind is NoticeKind.PROGRESS and self._send_progress:
                self._render_notice(out.detail or "")
            elif out.kind is NoticeKind.TOOL_HINT and self._send_tool_hints:
                self._render_notice(out.detail or "")
        elif isinstance(out, Reasoning):
            # A long tool (deep_research) streams coarse progress as Reasoning; the
            # model itself never emits Reasoning here (this path runs non-streaming).
            if self._render_notice is not None and self._send_progress and out.content:
                self._render_notice(out.content)
        elif isinstance(out, ToolEvent) and out.phase is ToolPhase.COMPLETE:
            delivery = (out.metadata or {}).get("raven_delivery")
            if not isinstance(delivery, dict):
                return
            files = [item for item in delivery.get("files") or [] if isinstance(item, dict)]
            if not files:
                return
            message = str(delivery.get("message") or "").strip()
            lines = [f"- {item.get('name') or item.get('path') or 'file'}: {item.get('path') or ''}" for item in files]
            self._render("\n".join([part for part in [message, "Delivered files:", *lines] if part]))
        # Other Notice kinds / ToolEvent / MediaOut are eaten (render-can't path).


def build_one_shot_spine(
    agent_loop: Any,
    channel: str,
    render: Callable[[str], None],
    *,
    render_notice: Callable[[str], None] | None = None,
    render_error: Callable[[str], None] | None = None,
    send_progress: bool = False,
    send_tool_hints: bool = False,
    user_pool: int = 1,
    system_pool: int = 1,
    shutdown_grace: float = 0.0,
    on_refusals: Callable[[list[Any]], None] | None = None,
) -> tuple[Scheduler, DeliveryHub, Callable[[], Awaitable[None]]]:
    """Wire the spine pieces a one-shot ``-m`` turn flows through: a hub with the
    channel's CliOutlet registered, and a Scheduler whose runner bridges the agent
    loop and whose sink is that hub. Returns those plus a ``teardown`` the caller
    awaits on exit — stop the scheduler (no more events) then close the hub's
    outlet workers — shared with the test so the teardown sequence itself is
    covered.

    ``render_notice`` + the two config flags are threaded to the CliOutlet so
    progress lines render; a caller that omits them keeps Notice eaten.
    ``render_error`` draws a failed turn's own words; a caller that omits it
    gets them through ``render``.

    ``on_refusals`` receives what the run's permission gates refused during
    teardown. A one-shot run has no human on it, so without this a run whose
    mutations were all refused is indistinguishable from one that made them; a
    caller that omits it keeps the old silence.

    The per-turn usage summary (cli.turn_summary) is wired here, at the
    CliOutlet's deliver tail, so it renders once right after the reply."""
    summary = _build_turn_summary(agent_loop)
    hub = DeliveryHub()
    hub.register(
        CliOutlet(
            channel,
            render,
            render_notice=render_notice,
            send_progress=send_progress,
            send_tool_hints=send_tool_hints,
            summary=summary,
        )
    )
    inner: Any = _OneShotTurnRunner(agent_loop, stream=False, inline_tool_stream=True)
    runner: Any = inner
    if summary is not None:
        runner = _SummaryTurnRunner(runner, summary)
    hub_sink = make_hub_sink(hub)

    async def sink(event: TurnEvent) -> None:
        # The hub sink drops lifecycle events and nothing on this path reads the
        # turn's outcome, so a failed turn printed nothing and exited clean. Its
        # own words are the one report a one-shot reader gets, drawn after
        # whatever the turn had already delivered.
        if isinstance(event, TurnFailed) and not event.cancelled:
            await hub.wait_idle(channel)
            (render_error or render)(event.error)
            return
        await hub_sink(event)

    scheduler = Scheduler(runner, OriginPools(user=user_pool, system=system_pool), sink)

    async def teardown() -> None:
        await scheduler.shutdown(grace=shutdown_grace)
        if on_refusals is not None:
            on_refusals(inner.refusals())
        await hub.aclose()

    return scheduler, hub, teardown
