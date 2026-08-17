"""Spine wiring for the one-shot ``agent -m`` path: the runner (an
AgentTurnRunner with stream=False, so the reply is one Text), the outlet that
renders a turn's text to the console, and the sink that feeds the delivery hub.

The one-shot turn runs through spine (submit -> lane -> run_turn -> hub ->
outlet). spine never imports cli; cli imports spine.
"""

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
)
from raven.spine.delivery import Capabilities, DeliveryHub, make_hub_sink
from raven.spine.events import Reasoning


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
    ) -> None:
        self.name = channel
        self.capabilities = Capabilities()
        self._render = render
        self._render_notice = render_notice
        self._send_progress = send_progress
        self._send_tool_hints = send_tool_hints

    async def deliver(self, out: Deliverable) -> None:
        if isinstance(out, Text):
            self._render(out.content)
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
    progress lines render; a caller that omits them keeps Notice eaten."""
    hub = DeliveryHub()
    hub.register(
        CliOutlet(
            channel,
            render,
            render_notice=render_notice,
            send_progress=send_progress,
            send_tool_hints=send_tool_hints,
        )
    )
    scheduler = Scheduler(
        AgentTurnRunner(agent_loop, stream=False, inline_tool_stream=True),
        OriginPools(user=user_pool, system=system_pool),
        make_hub_sink(hub),
    )

    async def teardown() -> None:
        await scheduler.shutdown(grace=0.0)
        await hub.aclose()

    return scheduler, hub, teardown
