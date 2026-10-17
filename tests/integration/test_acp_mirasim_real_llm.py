"""The Mirasim adapter against the Mirasim host running on this machine, with real model turns.

Run by hand: ``pytest tests/integration/test_acp_mirasim_real_llm.py -m integration -n 0``. Every
turn names ``glm-5.3-flash``, the platform model, and checks the snapshot ran on it: the agents'
own defaults are premium models or the user's own keys. Only claude, codex, pi and zcode offer it,
so those are the agents covered; they span Mirasim's three kinds of driver.

The host is the user's own, so every conversation is opened in one directory of its own and
deleted when its test ends, and a sweep before and after deletes whatever an earlier run left
there -- matched on that exact directory, never a prefix. The turns show in the Mirasim app
while they run.
"""

from __future__ import annotations

import asyncio
import struct
import tempfile
import time
import zlib
from pathlib import Path
from typing import Any

import pytest

from raven.acp.mirasim.host import HostConnection, HostUnavailableError, locate_host
from raven.acp.mirasim.server import MirasimAgent
from raven.acp.mirasim.store import BindingStore

pytestmark = [pytest.mark.integration, pytest.mark.slow]

MODEL = "glm-5.3-flash"
WORKDIR = Path(tempfile.gettempdir()) / "raven-mirasim-it"
TURN_S = 240.0
HOST = locate_host()
"""Read once at import: every test runs under a HOME of its own (``tests/conftest.py``), where the
host's socket is not."""


def _host_env() -> dict[str, str]:
    assert HOST is not None
    return {"RAVEN_MIRASIM_HOST": f"unix:{HOST.socket}" if HOST.socket else str(HOST.port)}


async def _host_is_up() -> bool:
    if HOST is None:
        return False
    conn = HostConnection(on_frame=lambda frame: None, locate=lambda: HOST)
    try:
        await conn.connect()
        return True
    except HostUnavailableError:
        return False
    finally:
        await conn.close()


async def _sweep() -> list[str]:
    """Delete every conversation in the test directory; the keys deleted."""
    conn = HostConnection(on_frame=lambda frame: None, locate=lambda: HOST)
    deleted: list[str] = []
    try:
        listed = await conn.request(
            {"type": "listSessions", "reqId": "raven-it-sweep", "workdir": str(WORKDIR)},
            lambda f: f.get("type") == "sessions" and f.get("reqId") == "raven-it-sweep",
            timeout=30,
        )
        for row in listed.get("sessions") or []:
            if row.get("workdir") != str(WORKDIR):
                continue
            ref = f"raven-it-delete-{row['sessionKey']}"
            await conn.request(
                {"type": "deleteSession", "sessionKey": row["sessionKey"], "clientRef": ref},
                lambda f, r=ref: f.get("type") == "sessionDeleteResult" and f.get("clientRef") == r,
                timeout=30,
            )
            deleted.append(row["sessionKey"])
    finally:
        await conn.close()
    return deleted


@pytest.fixture(scope="module", autouse=True)
def _live_host() -> Any:
    if not asyncio.run(_host_is_up()):
        pytest.skip("no Mirasim host is running on this machine")
    WORKDIR.mkdir(parents=True, exist_ok=True)
    asyncio.run(_sweep())
    yield
    asyncio.run(_sweep())


class Live:
    """One adapter on the real host, and what it has said."""

    def __init__(self, agent: str, store: Path) -> None:
        self.frames: list[dict[str, Any]] = []
        self.agent = MirasimAgent(agent=agent, store=BindingStore(store), emit=self.frames.append, locate=lambda: HOST)
        self.ids = 0
        self.opened: list[str] = []

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        self.ids += 1
        response = await self.agent.handle({"jsonrpc": "2.0", "id": self.ids, "method": method, "params": params})
        assert response is not None
        return response

    async def ok(self, method: str, **params: Any) -> Any:
        response = await asyncio.wait_for(self.call(method, **params), TURN_S)
        assert "result" in response, response
        return response["result"]

    async def session(self) -> str:
        await self.ok("initialize", protocolVersion=1)
        sid = (await self.ok("session/new", cwd=str(WORKDIR), mcpServers=[]))["sessionId"]
        await self.ok("session/set_config_option", sessionId=sid, configId="model", value=MODEL)
        self.opened.append(sid)
        return sid

    def updates(self, sid: str) -> list[dict[str, Any]]:
        return [
            frame["params"]["update"]
            for frame in self.frames
            if frame.get("method") == "session/update" and frame["params"]["sessionId"] == sid
        ]

    def said(self, sid: str, kind: str = "agent_message_chunk") -> str:
        return "".join(
            update["content"].get("text", "") for update in self.updates(sid) if update["sessionUpdate"] == kind
        )

    def ran_on(self, sid: str) -> Any:
        return self.agent.sessions[sid].view.snapshot.get("model")

    async def close(self) -> None:
        for sid in self.opened:
            if sid in self.agent.sessions:
                await self.call("session/delete", sessionId=sid)
        await self.agent.shutdown()
        await self.agent.close()


@pytest.fixture
async def live(tmp_path: Path) -> Any:
    opened: list[Live] = []

    def make(agent: str = "claude") -> Live:
        adapter = Live(agent, tmp_path / f"{agent}.json")
        opened.append(adapter)
        return adapter

    yield make
    for adapter in opened:
        await adapter.close()


def text(words: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": words}]


def red_png(path: Path, size: int = 64) -> None:
    row = b"\x00" + b"\xff\x00\x00" * size
    data = zlib.compress(row * size)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", data) + chunk(b"IEND", b""))


@pytest.mark.parametrize("agent", ["claude", "codex", "pi", "zcode"])
async def test_a_turn_streams_and_ends_with_its_usage(live: Any, agent: str) -> None:
    adapter = live(agent)
    sid = await adapter.session()
    result = await adapter.ok(
        "session/prompt",
        sessionId=sid,
        prompt=text("Count from 1 to 12, one number per line, nothing else. Do not use tools."),
    )
    assert result == {"stopReason": "end_turn"}
    assert adapter.ran_on(sid) == MODEL
    chunks = [u for u in adapter.updates(sid) if u["sessionUpdate"] == "agent_message_chunk"]
    assert len(chunks) >= 2, "the reply arrived whole rather than streamed"
    assert "12" in adapter.said(sid)
    usage = [u for u in adapter.updates(sid) if u["sessionUpdate"] == "usage_update"]
    assert usage and usage[-1]["used"] > 0 and usage[-1]["size"] > usage[-1]["used"]


async def test_a_conversation_continues_is_listed_replays_and_is_deleted(live: Any) -> None:
    adapter = live()
    sid = await adapter.session()
    await adapter.ok("session/prompt", sessionId=sid, prompt=text("Remember the code word PELICAN-7. Reply OK only."))
    await adapter.ok("session/prompt", sessionId=sid, prompt=text("What was the code word? Reply with it only."))
    assert "PELICAN-7" in adapter.said(sid)
    key = adapter.agent.sessions[sid].mirasim_key
    listed = await adapter.ok("session/list", cwd=str(WORKDIR))
    assert sid in [row["sessionId"] for row in listed["sessions"]]
    other = live()
    await other.ok("initialize", protocolVersion=1)
    await other.ok("session/load", sessionId=sid, cwd=str(WORKDIR), mcpServers=[])
    asked = other.said(sid, "user_message_chunk")
    assert asked.index("Remember the code word") < asked.index("What was the code word?")
    assert "PELICAN-7" in other.said(sid)
    assert await other.ok("session/delete", sessionId=sid) == {}
    after = await other.ok("session/list", cwd=str(WORKDIR))
    assert key not in [row["sessionId"] for row in after["sessions"]] and sid not in [
        row["sessionId"] for row in after["sessions"]
    ]


async def test_a_cancel_stops_a_long_turn_at_once(live: Any) -> None:
    adapter = live()
    sid = await adapter.session()
    turn = asyncio.ensure_future(
        adapter.ok("session/prompt", sessionId=sid, prompt=text("Count from 1 to 400, one number per line. No tools."))
    )
    deadline = time.monotonic() + TURN_S
    while len(adapter.said(sid)) < 20 and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
    started = time.monotonic()
    await adapter.ok("session/cancel", sessionId=sid)
    assert await turn == {"stopReason": "cancelled"}
    assert time.monotonic() - started < 5
    snapshot = adapter.agent.sessions[sid].view.snapshot
    assert snapshot.get("phase") == "done"


async def test_steering_lands_in_the_running_turn(live: Any) -> None:
    adapter = live()
    sid = await adapter.session()
    turn = asyncio.ensure_future(
        adapter.ok("session/prompt", sessionId=sid, prompt=text("Count from 1 to 300, one number per line. No tools."))
    )
    deadline = time.monotonic() + TURN_S
    while len(adapter.said(sid)) < 20 and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
    status = await adapter.ok(
        "_raven/session/steer", sessionId=sid, text="Stop counting now and reply with the single word STEERED."
    )
    assert status == {"status": "injected"}
    await turn
    assert "STEERED" in adapter.said(sid)
    assert "STEERED" in adapter.said(sid, "user_message_chunk")


async def test_an_image_reaches_claude(live: Any, tmp_path: Path) -> None:
    picture = tmp_path / "picture.png"
    red_png(picture)
    adapter = live()
    sid = await adapter.session()
    prompt = [
        {
            "type": "text",
            "text": "What single colour fills the attached image? Answer in English: the colour name only, one word.",
        },
        {"type": "resource_link", "uri": picture.as_uri(), "name": "picture.png", "mimeType": "image/png"},
    ]
    await adapter.ok("session/prompt", sessionId=sid, prompt=prompt)
    assert "red" in adapter.said(sid).lower()


async def test_a_tool_call_shows_as_it_runs(live: Any) -> None:
    adapter = live()
    sid = await adapter.session()
    await adapter.ok("session/prompt", sessionId=sid, prompt=text("Use your shell tool to run `ls`, then reply DONE."))
    calls = [u for u in adapter.updates(sid) if u["sessionUpdate"] == "tool_call"]
    assert calls, [u["sessionUpdate"] for u in adapter.updates(sid)]
    settled = [
        u for u in adapter.updates(sid) if u["sessionUpdate"] == "tool_call_update" and u.get("status") == "completed"
    ]
    assert settled or any(call.get("status") == "completed" for call in calls)


async def test_raven_dispatches_to_a_mirasim_preset(tmp_path: Path) -> None:
    from raven.acp_client.acp_agent import AcpAgentBackend
    from raven.acp_client.pool import close_pool
    from raven.agent.subagent.presets import third_party_subagent_preset
    from raven.config.schema import ThirdPartyAcpSubagentConfig

    row = third_party_subagent_preset("mirasim_claude")
    cfg = ThirdPartyAcpSubagentConfig.model_validate(
        {**row, "env": {"RAVEN_HOME": str(tmp_path / "home"), **_host_env()}}
    )
    try:
        answer = await asyncio.wait_for(
            AcpAgentBackend(name=cfg.name, command=cfg.command, env=dict(cfg.env)).run(
                "Reply with exactly: PONG",
                task_id="t1",
                workspace=WORKDIR,
                executor=None,
                session_model=MODEL,
            ),
            TURN_S,
        )
    finally:
        await close_pool()
    assert "PONG" in answer
