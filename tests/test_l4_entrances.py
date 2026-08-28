"""The L4 constitution, pinned.

L4 = the entrance layer: whoever calls the kernel. Three runnable clauses:
1. The kernel has zero knowledge of its callers -- no inner package imports
   any surface package, ever.
2. A NEW entrance needs nothing but inward imports: a ~25-line surface builds
   a Scheduler, submits a TurnRequest, and receives deliverables -- without
   touching or knowing cli/gateway/rpc.
3. Every turn produces EXACTLY one terminal event (the L0 handover: consumers
   key their per-turn release on it; zero would leak the slot, two would test
   release idempotence in anger).
"""

from __future__ import annotations

import ast
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INNER_DIRS = ["spine", "contracts", "agent", "memory_engine", "context_engine",
              "providers", "session", "sandbox", "routing", "token_wise",
              "plugins", "channels", "gateway", "market", "ops"]
SURFACES = ("raven.cli", "raven.rpc", "raven.web_rpc", "raven.proactive_engine")


def test_kernel_and_organs_know_no_surface():
    offenders = []
    for d in INNER_DIRS:
        for p in (REPO / "raven" / d).rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            try:
                tree = ast.parse(p.read_text(errors="replace"))
            except SyntaxError:
                continue
            for node in tree.body:
                mods = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module]
                elif isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                for m in mods:
                    if any(m == s or m.startswith(s + ".") for s in SURFACES):
                        offenders.append(f"{p.relative_to(REPO)}:{node.lineno} -> {m}")
    # Debt allowlist: EMPTY, and may only shrink — it emptied when ask_user /
    # deep_research were re-typed against the QuestionResponder paper (tool
    # side), so no inner module names the concrete broker machine at rpc.
    assert offenders == [], (
        "an inner layer imports a surface (callers must stay unknown to the "
        f"called): {offenders}"
    )


class _Resp:
    content = "fifth entrance says hi"
    tool_calls: list = []
    reasoning_content = None
    thinking_blocks = None
    usage: dict = {}
    finish_reason = "stop"
    error_classification = None

    has_tool_calls = False


class _Provider:
    async def chat(self, messages, **kw):
        return _Resp()

    async def chat_with_retry(self, messages, **kw):
        return _Resp()

    async def chat_stream(self, messages, **kw):
        return _Resp()


@pytest.mark.asyncio
async def test_a_new_entrance_needs_only_inward_imports():
    """The whole fifth entrance, inline. It imports spine + agent and nothing
    from any existing surface; if this ever needs a cli/rpc import to work,
    the entrance layer has grown a hidden dependency."""
    from raven.agent.loop.main import AgentLoop
    from raven.agent.spine_runner import AgentTurnRunner
    from raven.spine.message import ChatType, Source
    from raven.spine.scheduler import OriginPools, Scheduler
    from raven.spine.turn import Origin, TurnRequest

    loop = AgentLoop(provider=_Provider(), workspace=Path(tempfile.mkdtemp()),
                     model="f", interactive=False)
    received: list = []

    async def sink(ev):
        received.append(type(ev).__name__)

    sched = Scheduler(runner=AgentTurnRunner(loop, stream=False),
                      pools=OriginPools(user=2, system=2), sink=sink)
    req = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="fifth", chat_id="c1", sender_id="u", chat_type=ChatType.DM),
        text="hello from the fifth entrance",
    )
    outcome = await sched.submit(req).result()

    assert outcome is not None
    assert "Text" in received, "the entrance must receive the answer"
    terminals = [e for e in received if e in ("TurnEnded", "TurnFailed")]
    assert len(terminals) == 1, (
        f"exactly one terminal event per turn, got {terminals} "
        "(zero leaks the consumer's slot; two would double-release)"
    )
