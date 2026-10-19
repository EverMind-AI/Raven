"""The Mirasim adapter against the Mirasim host running on this machine, with real model turns.

Run by hand: ``pytest tests/integration/test_acp_mirasim_real_llm.py -m integration -n 0``. Every
turn names ``glm-5.3-flash``, the platform model, and checks that it ran on it: the agents' own
defaults are premium models or the user's own keys. Only claude, codex, pi and zcode offer it, so
those are the agents covered; they span Mirasim's three kinds of driver.

The host is the user's own. A run claims a directory of its own, ``slot-<n>`` under
``<tmp>/raven-mirasim-it``, by holding that slot's lock for as long as it runs, so runs side by
side -- two terminals, or xdist workers -- never share one. Its conversations are deleted when
their test ends, and a sweep before and after deletes whatever an earlier run left in its slot --
matched on that exact directory, never a prefix -- so nothing outside it, a conversation another
run holds included, is touched. The turns show in the Mirasim app while they run.

What a run leaves behind: the slot directories themselves, and the agents' own records of the
turns, which deleting a Mirasim conversation does not remove -- Claude Code keeps its transcripts
and a ``memory/`` directory under ``~/.claude/projects/`` in a folder named after the slot's path.
A run interrupted mid-test leaves its conversations until the next run that claims the slot.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import struct
import tempfile
import time
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

import pytest

from raven.acp.mirasim.host import HostConnection, HostUnavailableError, locate_host, mirasim_home
from raven.acp.mirasim.server import MirasimAgent
from raven.acp.mirasim.store import BindingStore

pytestmark = [pytest.mark.integration, pytest.mark.slow]

MODEL = "glm-5.3-flash"
SLOTS = Path(tempfile.gettempdir()) / "raven-mirasim-it"
TURN_S = 240.0
HOST_HOME = mirasim_home().resolve()
HOST = locate_host()
"""Read the address and token directory at import, before each test isolates its home."""

_RUN: dict[str, Path] = {}


def workdir() -> Path:
    """The slot this run holds: every conversation it opens works there."""
    return _RUN["dir"]


def _host_env() -> dict[str, str]:
    assert HOST is not None
    return {
        "RAVEN_MIRASIM_HOST": f"unix:{HOST.socket}" if HOST.socket else str(HOST.port),
        "MIRASIM_HOME": str(HOST_HOME),
    }


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


async def _listed(directory: Path) -> list[dict[str, Any]]:
    """The host's rows for the conversations working in ``directory`` exactly."""
    conn = HostConnection(on_frame=lambda frame: None, locate=lambda: HOST)
    try:
        listed = await conn.request(
            {"type": "listSessions", "reqId": "raven-it-list", "workdir": str(directory)},
            lambda f: f.get("type") == "sessions" and f.get("reqId") == "raven-it-list",
            timeout=30,
        )
    finally:
        await conn.close()
    return [row for row in listed.get("sessions") or [] if row.get("workdir") == str(directory)]


async def _sweep(directory: Path) -> list[str]:
    """Delete every conversation in ``directory``, a slot this run holds; the keys deleted."""
    rows = await _listed(directory)
    conn = HostConnection(on_frame=lambda frame: None, locate=lambda: HOST)
    deleted: list[str] = []
    try:
        for row in rows:
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


def _locked(handle: IO[bytes]) -> bool:
    """Take ``handle``'s file for this process alone, or say another holds it."""
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: IO[bytes]) -> None:
    # Windows frees a lock on close only when it gets round to it; the next run may come sooner.
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def _slot() -> Iterator[Path]:
    """The first slot no other run holds, held until the block ends."""
    SLOTS.mkdir(parents=True, exist_ok=True)
    for index in itertools.count():
        handle = (SLOTS / f"slot-{index}.lock").open("a+b")
        if not _locked(handle):
            handle.close()
            continue
        try:
            directory = SLOTS / f"slot-{index}"
            directory.mkdir(exist_ok=True)
            yield directory
        finally:
            _unlock(handle)
            handle.close()
        return


@pytest.fixture(scope="module", autouse=True)
def _live_host() -> Any:
    if not asyncio.run(_host_is_up()):
        pytest.skip("no Mirasim host is running on this machine")
    with _slot() as directory:
        _RUN["dir"] = directory
        asyncio.run(_sweep(directory))
        yield
        asyncio.run(_sweep(directory))


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
        sid = (await self.ok("session/new", cwd=str(workdir()), mcpServers=[]))["sessionId"]
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
    listed = await adapter.ok("session/list", cwd=str(workdir()))
    assert sid in [row["sessionId"] for row in listed["sessions"]]
    other = live()
    await other.ok("initialize", protocolVersion=1)
    await other.ok("session/load", sessionId=sid, cwd=str(workdir()), mcpServers=[])
    asked = other.said(sid, "user_message_chunk")
    assert asked.index("Remember the code word") < asked.index("What was the code word?")
    assert "PELICAN-7" in other.said(sid)
    assert await other.ok("session/delete", sessionId=sid) == {}
    after = await other.ok("session/list", cwd=str(workdir()))
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

    # A model the session refuses leaves the dispatch on the agent's default, a premium one, with
    # nothing failing: so the menu is read before any turn runs, and the turn's model after.
    menus = MirasimAgent(
        agent="claude", store=BindingStore(tmp_path / "menus.json"), emit=lambda frame: None, locate=lambda: HOST
    )
    try:
        offered = (await menus.catalog()).offers_model(MODEL)
    finally:
        await menus.close()
    assert offered, f"Mirasim's Claude Code offers no {MODEL}: the dispatch would run on its default"
    row = third_party_subagent_preset("mirasim_claude")
    cfg = ThirdPartyAcpSubagentConfig.model_validate(
        {**row, "env": {"RAVEN_HOME": str(tmp_path / "home"), **_host_env()}}
    )
    try:
        answer = await asyncio.wait_for(
            AcpAgentBackend(name=cfg.name, command=cfg.command, env=dict(cfg.env)).run(
                "Reply with exactly: PONG",
                task_id="t1",
                workspace=workdir(),
                executor=None,
                session_model=MODEL,
            ),
            TURN_S,
        )
    finally:
        await close_pool()
    assert "PONG" in answer
    ran = [row.get("model") for row in await _listed(workdir()) if row.get("agent") == "claude"]
    assert ran and all(model == MODEL for model in ran), ran
