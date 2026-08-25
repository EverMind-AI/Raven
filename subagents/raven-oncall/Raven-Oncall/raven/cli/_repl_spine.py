"""Spine wiring for the REPL: the runner (an AgentTurnRunner with stream=False,
so the reply is one Text), the outlet that renders a turn's text to the console,
and the sink that feeds the delivery hub.

The REPL runs turns through spine (submit -> lane -> run_turn -> hub -> outlet).
spine never imports cli; cli imports spine.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from raven.agent.spine_runner import AgentTurnRunner
from raven.spine import (
    ChatType,
    Deliverable,
    Notice,
    NoticeKind,
    Origin,
    OriginPools,
    Scheduler,
    Source,
    Text,
    TurnHandle,
    TurnRequest,
)
from raven.spine.delivery import Capabilities, DeliveryHub, make_hub_sink
from raven.spine.events import Reasoning


class CliOutlet:
    """Renders a turn's deliverables to the terminal. Runs non-streaming (run_turn
    stream=False), so the reply arrives as one Text; ToolEvent/MediaOut are eaten.

    ``render_notice`` is opt-in progress rendering: when set, a Notice (and the
    Reasoning a long tool like deep_research streams, see ``deliver``) renders as
    a progress line, gated by ``send_progress`` (PROGRESS) and ``send_tool_hints``
    (TOOL_HINT). Both the one-shot ``-m`` path and the interactive REPL wire it so
    deep_research progress is visible; note this also surfaces the model's
    per-tool progress hint on every tool call, gated by the same flags. A surface
    that omits it eats Notice / Reasoning as before.

    ``render_marker`` is opt-in: a Text whose ``source.extras._sentinel_origin``
    is set renders this proactive marker before its content (the interactive
    REPL passes the 🐦‍⬛ marker the old bus consumer printed). Left None elsewhere,
    so a normal turn reply renders unchanged."""

    def __init__(
        self,
        channel: str,
        render: Callable[[str], None],
        *,
        render_notice: Callable[[str], None] | None = None,
        render_marker: Callable[[], None] | None = None,
        send_progress: bool = False,
        send_tool_hints: bool = False,
    ) -> None:
        self.name = channel
        self.capabilities = Capabilities()
        self._render = render
        self._render_notice = render_notice
        self._render_marker = render_marker
        self._send_progress = send_progress
        self._send_tool_hints = send_tool_hints

    async def deliver(self, out: Deliverable) -> None:
        if isinstance(out, Text):
            if self._render_marker is not None and out.source is not None and out.source.extras.get("_sentinel_origin"):
                self._render_marker()
            self._render(out.content)
        elif isinstance(out, Notice) and self._render_notice is not None:
            if out.kind is NoticeKind.PROGRESS and self._send_progress:
                self._render_notice(out.detail or "")
            elif out.kind is NoticeKind.TOOL_HINT and self._send_tool_hints:
                self._render_notice(out.detail or "")
        elif isinstance(out, Reasoning):
            # A long tool (deep_research) streams coarse progress as Reasoning; the
            # model itself never emits Reasoning here (REPL runs non-streaming).
            if self._render_notice is not None and self._send_progress and out.content:
                self._render_notice(out.content)
        # Other Notice kinds / ToolEvent / MediaOut are eaten (render-can't path).


def build_repl(
    agent_loop: Any,
    channel: str,
    render: Callable[[str], None],
    *,
    render_notice: Callable[[str], None] | None = None,
    render_marker: Callable[[], None] | None = None,
    send_progress: bool = False,
    send_tool_hints: bool = False,
    user_pool: int = 1,
    system_pool: int = 1,
) -> tuple[Scheduler, DeliveryHub, Callable[[], Awaitable[None]]]:
    """Wire the spine pieces a REPL turn flows through: a hub with the channel's
    CliOutlet registered, and a Scheduler whose runner bridges the agent loop and
    whose sink is that hub. Returns those plus a ``teardown`` the caller awaits on
    exit — stop the scheduler (no more events) then close the hub's outlet workers
    — shared with the test so the teardown sequence itself is covered.

    ``render_notice`` + the two config flags are threaded to the CliOutlet for the
    one-shot ``-m`` path; the interactive REPL omits them (Notice stays eaten)."""
    hub = DeliveryHub()
    hub.register(
        CliOutlet(
            channel,
            render,
            render_notice=render_notice,
            render_marker=render_marker,
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


async def run_repl_loop(
    read_input: Callable[[], Awaitable[str]],
    submit: Callable[[TurnRequest], TurnHandle],
    wait_idle: Callable[[str], Awaitable[None]],
    *,
    channel: str,
    chat_id: str,
    is_exit: Callable[[str], bool],
    handle_slash: Callable[[str], bool],
    thinking: Callable[[], Any],
    on_exit: Callable[[], None],
) -> None:
    """Read a line, submit it as a turn, wait for the turn to finish AND its
    output to render, then prompt again — so a reply always lands before the next
    prompt. result() means the turn stopped emitting; wait_idle is the render
    barrier that the async outlet has caught up. tty/console and exit/slash are
    injected so this runs against the real scheduler and hub under test."""
    while True:
        # Wrap the whole iteration (read + turn) so Ctrl-C / EOF at any point —
        # including mid-turn — exits cleanly, as the bus loop did.
        try:
            user_input = await read_input()
            command = user_input.strip()
            if not command:
                continue
            if is_exit(command):
                on_exit()
                return
            if command.startswith("/") and handle_slash(command):
                continue
            handle = submit(
                TurnRequest(
                    origin=Origin.USER,
                    source=Source(channel=channel, chat_id=chat_id, sender_id="user", chat_type=ChatType.DM),
                    text=user_input,
                    conversation=f"{channel}:{chat_id}",
                )
            )
            with thinking():
                await handle.result()
            await wait_idle(channel)
        except (EOFError, KeyboardInterrupt):
            on_exit()
            return
