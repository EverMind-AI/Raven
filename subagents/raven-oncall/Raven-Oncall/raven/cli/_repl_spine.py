"""Spine wiring for the one-shot ``agent -m`` path: the runner (an
AgentTurnRunner with stream=False, so the reply is one Text), the outlet that
renders a turn's text to the console, and the sink that feeds the delivery hub.

The one-shot turn runs through spine (submit -> lane -> run_turn -> hub ->
outlet). spine never imports cli; cli imports spine.
"""

import time
from collections.abc import Awaitable, Callable
from typing import Any

from raven.agent.spine_runner import AgentTurnRunner
from raven.spine import (
    Deliverable,
    Notice,
    NoticeKind,
    OriginPools,
    Scheduler,
    Text,
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
    deliverable in the same turn cannot double-report."""

    def __init__(self, tracker: Any) -> None:
        self._tracker = tracker
        self._baseline = tracker.snapshot()
        self._started: float | None = None

    def turn_started(self) -> None:
        self._baseline = self._tracker.snapshot()
        self._started = time.monotonic()

    def take_line(self) -> str | None:
        total = self._tracker.snapshot()
        base = self._baseline
        self._baseline = total
        in_tokens = (
            (total.input_tokens - base.input_tokens)
            + (total.cache_read_tokens - base.cache_read_tokens)
            + (total.cache_write_tokens - base.cache_write_tokens)
        )
        out_tokens = total.output_tokens - base.output_tokens
        if in_tokens <= 0 and out_tokens <= 0:
            return None
        parts = [f"{_fmt_tokens(in_tokens)} in / {_fmt_tokens(out_tokens)} out tokens"]
        cost = (total.estimated_cost_usd or 0.0) - (base.estimated_cost_usd or 0.0)
        if cost > 0:
            # Below the 4-decimal render floor a real cost would round to "$0"
            # and read as a free call; show the floor instead of a lie.
            parts.append("<$0.0001" if cost < 0.0001 else "$" + f"{cost:.4f}".rstrip("0").rstrip("."))
        if self._started is not None:
            parts.append(f"{time.monotonic() - self._started:.1f}s")
        return " · ".join(parts)


class _SummaryTurnRunner:
    """Marks the turn boundary for TurnUsageSummary; delegates the turn."""

    def __init__(self, inner: Any, summary: TurnUsageSummary) -> None:
        self._inner = inner
        self._summary = summary

    async def run(self, req: TurnRequest, emit: Any, drain: Any) -> Any:
        self._summary.turn_started()
        return await self._inner.run(req, emit, drain)


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
    from raven.token_wise.usage_tracker import UsageTracker

    tracker = UsageTracker(persist=False)
    strategies.register(tracker)
    return TurnUsageSummary(tracker)


def _render_summary_line(line: str) -> None:
    from rich.console import Console

    # Two-space indent matches the render_notice progress lines in
    # agent_commands, so the summary sits in the same visual column.
    Console().print(f"  [dim]↳ {line}[/dim]")


class CliOutlet:
    """Renders a turn's deliverables to the terminal. Runs non-streaming (run_turn
    stream=False), so the reply arrives as one Text; ToolEvent/MediaOut are eaten.

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
        # Other Notice kinds / ToolEvent / MediaOut are eaten (render-can't path).


def build_repl(
    agent_loop: Any,
    channel: str,
    render: Callable[[str], None],
    *,
    render_notice: Callable[[str], None] | None = None,
    send_progress: bool = False,
    send_tool_hints: bool = False,
    user_pool: int = 1,
    system_pool: int = 1,
) -> tuple[Scheduler, DeliveryHub, Callable[[], Awaitable[None]]]:
    """Wire the spine pieces a one-shot ``-m`` turn flows through: a hub with the
    channel's CliOutlet registered, and a Scheduler whose runner bridges the agent
    loop and whose sink is that hub. Returns those plus a ``teardown`` the caller
    awaits on exit — stop the scheduler (no more events) then close the hub's
    outlet workers — shared with the test so the teardown sequence itself is
    covered.

    ``render_notice`` + the two config flags are threaded to the CliOutlet so
    progress lines render; a caller that omits them keeps Notice eaten.

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
    runner: Any = AgentTurnRunner(agent_loop, stream=False, inline_tool_stream=True)
    if summary is not None:
        runner = _SummaryTurnRunner(runner, summary)
    scheduler = Scheduler(
        runner,
        OriginPools(user=user_pool, system=system_pool),
        make_hub_sink(hub),
    )

    async def teardown() -> None:
        await scheduler.shutdown(grace=0.0)
        await hub.aclose()

    return scheduler, hub, teardown
