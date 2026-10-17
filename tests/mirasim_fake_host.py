"""A stand-in Mirasim host: ``/api/health`` and the ``/ws`` frames the adapter speaks, scripted per test.

It answers in the shapes measured against Mirasim 0.0.465 and 0.0.537: a turn is
a ``taskId``; ``prompt`` answers ``accepted`` (``taskId: ""`` when the session is
busy and the prompt queued, then ``outbox.dispatched`` when it runs); a
subscriber gets a ``snapshot`` and then ``session`` patches that name the
sequence they start from; ``listClis`` answers progressively; ``catalog`` and
``commands`` carry no correlation key, ``sessions`` and ``history`` echo
``reqId``.

A turn runs a script of steps, ``respond(prompt_frame, session) -> steps``:

- ``("text", s)`` / ``("reasoning", s)``: appended;
- ``("tool", call)``: a tool call upserted by ``id``;
- ``("set", keys)``: snapshot keys replaced;
- ``("ask", interaction)``: the turn waits for its ``interact``, recorded in
  ``session.answers``;
- ``("hold",)``: the turn waits for a ``stop`` or a ``steer``;
- ``("error", message)``: the turn ends with that error.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from raven.acp.mirasim.host import HostAddress

Step = tuple[Any, ...]

ROWS: list[dict[str, Any]] = [
    {"id": "claude", "label": "Claude Code", "kind": "cli", "installed": True, "version": "2.1.302"},
    {"id": "codex", "label": "Codex", "kind": "cli", "installed": True, "version": "0.163.0"},
    {"id": "zcode", "label": "ZCode CLI", "kind": "cli", "installed": False, "version": None},
    {"id": "qwen", "label": "Qwen Code", "kind": "cli", "installed": True, "version": "0.25.2"},
]

RUNGS = [{"id": "low", "label": "Low"}, {"id": "high", "label": "High"}, {"id": "max", "label": "Max"}]

CATALOGS: dict[str, dict[str, Any]] = {
    "claude": {
        "defaultModel": "sonnet",
        "defaultEffort": "high",
        "models": [
            {"id": "sonnet", "label": "Sonnet", "contextWindow": 200000},
            {"id": "opus", "label": "Opus", "contextWindow": 1000000},
            {"id": "glm-5.3-flash", "label": "GLM 5.3 Flash", "contextWindow": 128000},
            {"id": "profile:abc", "label": "My key", "contextWindow": 64000},
        ],
        "effort": RUNGS,
        "effortByModel": {"sonnet": RUNGS, "opus": RUNGS, "glm-5.3-flash": []},
        "routeByModel": {"glm-5.3-flash": "relay", "profile:abc": "direct"},
    },
    "codex": {"defaultModel": "gpt-6", "models": [{"id": "gpt-6", "label": "GPT-6"}], "effort": []},
    "qwen": {"defaultModel": "qwen-max", "models": [{"id": "qwen-max", "label": "Qwen Max"}], "effort": []},
}

COMMANDS = [
    {"name": "branch", "description": "Fork this conversation into a new branch session", "source": "host"},
    {"name": "compact", "source": "builtin"},
    {"name": "review", "description": "Review the changes", "source": "skill"},
]


def echo(prompt: dict[str, Any], session: FakeSession) -> list[Step]:
    """The default script: one reply naming the turn, the model and the effort it ran on."""
    said = f"turn {len(session.turns) + 1} of {session.key} on {prompt.get('model')} effort {prompt.get('effort')}"
    return [("reasoning", "thinking it over"), ("text", f"{said}: {prompt.get('prompt')}")]


@dataclass(eq=False)
class FakeSession:
    key: str
    agent: str
    workdir: str
    snapshot: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    turns: list[dict[str, Any]] = field(default_factory=list)
    """The settled turns, oldest first, as ``getHistory`` reports them."""
    queue: list[dict[str, Any]] = field(default_factory=list)
    running: asyncio.Task[None] | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    stopped: bool = False
    steered: list[str] = field(default_factory=list)
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    route: Any = "unset"
    prompts: list[dict[str, Any]] = field(default_factory=list)
    title: str = ""
    updated_at: int = 1792224900000


@dataclass(eq=False)
class Client:
    ws: web.WebSocketResponse
    subs: set[str] = field(default_factory=set)
    cadence: bool = False


class FakeHost:
    """One stand-in host, serving until :meth:`stop`."""

    def __init__(
        self,
        *,
        token: str | None = None,
        version: str = "0.0.537",
        respond: Callable[[dict[str, Any], FakeSession], list[Step]] = echo,
    ) -> None:
        self.token = token
        self.version = version
        self.respond = respond
        self.rows: list[dict[str, Any]] = [dict(row) for row in ROWS]
        self.catalogs: dict[str, dict[str, Any]] = json.loads(json.dumps(CATALOGS))
        self.commands = list(COMMANDS)
        self.progressive: dict[str, int] = {}
        """Agent id -> how many ``clis`` frames show it uninstalled before the one that shows it installed."""
        self.sessions: dict[str, FakeSession] = {}
        self.deleted: set[str] = set()
        self.received: list[dict[str, Any]] = []
        self.clients: list[Client] = []
        self.refuse: dict[str, str] = {}
        """Frame type -> the ``error`` message a frame of that type is answered with instead."""
        self.silent: set[str] = set()
        """Frame types that are swallowed without an answer."""
        self.health: bool | str = True
        """``/api/health``: its JSON when true, a 503 when false, and a string is the 200 body sent instead."""
        self.accepting: asyncio.Event | None = None
        """When given, a ``prompt`` is answered only once it is set."""
        self.address: HostAddress | None = None
        self._runner: web.AppRunner | None = None
        self._tasks = itertools.count(1)

    async def start(self, *, socket: Path | None = None) -> HostAddress:
        app = web.Application()
        app.router.add_get("/api/health", self._health)
        app.router.add_get("/ws", self._ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        if socket is not None:
            site: web.BaseSite = web.UnixSite(self._runner, str(socket))
            await site.start()
            self.address = HostAddress(socket=str(socket))
        else:
            site = web.TCPSite(self._runner, "127.0.0.1", 0)
            await site.start()
            server = site._server
            assert server is not None
            port = server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
            self.address = HostAddress(port=port, token=self.token)
        return self.address

    async def stop(self) -> None:
        for session in self.sessions.values():
            if session.running is not None:
                session.running.cancel()
        await self.drop()
        if self._runner is not None:
            await self._runner.cleanup()

    async def drop(self) -> None:
        """Close every client connection, as a host that went away would."""
        for client in list(self.clients):
            await client.ws.close()

    def frames(self, kind: str) -> list[dict[str, Any]]:
        return [frame for frame in self.received if frame.get("type") == kind]

    def session(self, key: str | None = None) -> FakeSession:
        if key is None:
            assert len(self.sessions) == 1, self.sessions
            return next(iter(self.sessions.values()))
        return self.sessions[key]

    def add_session(self, agent: str, workdir: str, turns: list[dict[str, Any]], *, title: str = "") -> FakeSession:
        """A conversation the host already has, as if begun in Mirasim."""
        key = f"{agent}:{uuid.uuid4()}"
        session = FakeSession(key=key, agent=agent, workdir=workdir, turns=turns, title=title)
        if turns:
            session.snapshot = {"phase": "done", "taskId": turns[-1].get("taskId"), "text": turns[-1].get("text", "")}
        self.sessions[key] = session
        return session

    async def wait_for(self, predicate: Callable[[], Any], *, limit: float = 10.0) -> Any:
        deadline = asyncio.get_running_loop().time() + limit
        while asyncio.get_running_loop().time() < deadline:
            if got := predicate():
                return got
            await asyncio.sleep(0.01)
        raise AssertionError("the stand-in host never saw what the test waited for")

    # -- HTTP -------------------------------------------------------------------

    async def _health(self, request: web.Request) -> web.Response:
        if isinstance(self.health, str):
            return web.Response(text=self.health)
        if not self.health:
            return web.Response(status=503)
        return web.json_response({"ok": True, "name": "mirasim", "version": self.version, "pid": 1})

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        if self.token is not None and request.headers.get("x-mirasim-token") != self.token:
            return web.Response(status=401, text="unauthorized")
        ws = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024)
        await ws.prepare(request)
        client = Client(ws)
        self.clients.append(client)
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                frame = json.loads(message.data)
                self.received.append(frame)
                await self._handle(client, frame)
        finally:
            self.clients.remove(client)
        return ws

    # -- frames -----------------------------------------------------------------

    async def _send(self, client: Client, frame: dict[str, Any]) -> None:
        if not client.ws.closed:
            with contextlib.suppress(ConnectionError, RuntimeError):
                await client.ws.send_str(json.dumps(frame))

    async def _broadcast(self, key: str, frame: dict[str, Any]) -> None:
        for client in list(self.clients):
            if key in client.subs:
                await self._send(client, frame)

    async def _handle(self, client: Client, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind in self.silent:
            return
        if kind in self.refuse:
            await self._send(
                client, {"type": "error", "message": self.refuse[kind], "clientRef": frame.get("clientRef")}
            )
            return
        if kind == "setStreamCadence":
            client.cadence = True
        elif kind == "listClis":
            asyncio.ensure_future(self._clis(client))
        elif kind == "getCatalog":
            doc = self.catalogs.get(frame.get("agent"), {"models": [], "effort": []})
            await self._send(client, {"type": "catalog", "agent": frame.get("agent"), "agents": [], **doc})
        elif kind == "getCommands":
            await self._send(
                client,
                {
                    "type": "commands",
                    "agent": frame.get("agent"),
                    "workdir": frame.get("workdir"),
                    "items": self.commands,
                },
            )
        elif kind == "listSessions":
            await self._send(client, self._sessions(frame))
        elif kind == "getHistory":
            await self._send(client, self._history(frame))
        elif kind == "subscribe":
            await self._subscribe(client, frame.get("sessionKey"))
        elif kind == "prompt":
            await self._prompt(client, frame)
        elif kind == "stop":
            await self._stop(client, frame)
        elif kind == "recall":
            await self._recall(frame)
        elif kind == "steer":
            await self._steer(frame)
        elif kind == "interact":
            self._interact(frame)
        elif kind == "deleteSession":
            key = frame.get("sessionKey")
            self.sessions.pop(key, None)
            self.deleted.add(key)
            await self._send(
                client,
                {
                    "type": "sessionDeleteResult",
                    "clientRef": frame.get("clientRef"),
                    "sessionKey": key,
                    "ok": True,
                    "worktreeRemoved": False,
                },
            )

    async def _clis(self, client: Client) -> None:
        late = dict(self.progressive)
        while True:
            rows = [
                {**row, "installed": False, "version": None} if late.get(row["id"], 0) > 0 else row for row in self.rows
            ]
            await self._send(client, {"type": "clis", "clis": rows, "autoUpdate": True, "agentList": {}})
            if not any(count > 0 for count in late.values()):
                return
            late = {agent: count - 1 for agent, count in late.items()}
            await asyncio.sleep(0.05)

    def _sessions(self, frame: dict[str, Any]) -> dict[str, Any]:
        workdir = frame.get("workdir")
        rows = [
            {
                "sessionKey": session.key,
                "agent": session.agent,
                "workdir": session.workdir,
                "title": session.title,
                "preview": session.turns[-1].get("prompt", "") if session.turns else "",
                "updatedAt": session.updated_at,
                "runState": "running" if session.running else "done",
                "numTurns": len(session.turns),
            }
            for session in self.sessions.values()
            if frame.get("scope") == "global" or session.workdir == workdir
        ]
        return {"type": "sessions", "reqId": frame.get("reqId"), "workdir": workdir, "sessions": rows, "hasMore": False}

    def _history(self, frame: dict[str, Any]) -> dict[str, Any]:
        session = self.sessions.get(frame.get("sessionKey"))
        turns = list(session.turns) if session is not None else []
        end = len(turns)
        before = frame.get("before")
        if before is not None:
            end = next((i for i, turn in enumerate(turns) if turn.get("taskId") == before), end)
        limit = frame.get("limit") or end
        start = max(0, end - limit)
        page = turns[start:end]
        return {
            "type": "history",
            "sessionKey": frame.get("sessionKey"),
            "reqId": frame.get("reqId"),
            "turns": page,
            "total": len(turns),
            "older": start,
        }

    async def _subscribe(self, client: Client, key: Any) -> None:
        session = self.sessions.get(key)
        if session is None or not session.snapshot:
            await self._send(client, {"type": "outbox", "sessionKey": key, "items": [], "noRun": True})
            return
        client.subs.add(key)
        await self._send(
            client, {"type": "snapshot", "sessionKey": key, "seq": session.seq, "snapshot": session.snapshot}
        )

    async def _patch(self, session: FakeSession, patch: dict[str, Any]) -> None:
        start = session.seq
        session.seq += 1
        snapshot = dict(session.snapshot)
        for key, added in (("text", patch.get("appendText")), ("reasoning", patch.get("appendReasoning"))):
            if added:
                snapshot[key] = (snapshot.get(key) or "") + added
        snapshot.update(patch.get("set") or {})
        session.snapshot = snapshot
        await self._broadcast(
            session.key,
            {"type": "session", "sessionKey": session.key, "seq": session.seq, "fromSeq": start, "patch": patch},
        )

    async def _prompt(self, client: Client, frame: dict[str, Any]) -> None:
        if self.accepting is not None:
            await self.accepting.wait()
        key = frame.get("sessionKey")
        if key in self.deleted:
            await self._send(
                client,
                {
                    "type": "error",
                    "clientRef": frame.get("clientRef"),
                    "sessionKey": key,
                    "message": "This conversation was deleted, so it cannot be continued.",
                },
            )
            return
        if key is None:
            key = f"{frame.get('agent')}:{uuid.uuid4()}"
            self.sessions[key] = FakeSession(key=key, agent=frame.get("agent"), workdir=frame.get("workdir"))
        session = self.sessions.setdefault(key, FakeSession(key=key, agent=key.split(":")[0], workdir=""))
        if "route" in frame:
            session.route = frame["route"]
        session.prompts.append(frame)
        client.subs.add(key)
        if session.running is not None and not session.running.done():
            session.queue.append(frame)
            await self._send(
                client, {"type": "accepted", "clientRef": frame.get("clientRef"), "sessionKey": key, "taskId": ""}
            )
            await self._outbox(session)
            return
        task_id = f"task-{next(self._tasks)}"
        await self._send(
            client, {"type": "accepted", "clientRef": frame.get("clientRef"), "sessionKey": key, "taskId": task_id}
        )
        await self._send(
            client, {"type": "snapshot", "sessionKey": key, "seq": session.seq, "snapshot": session.snapshot}
        )
        session.running = asyncio.ensure_future(self._run(session, frame, task_id))

    async def _outbox(self, session: FakeSession, **extra: Any) -> None:
        items = [{"ref": item.get("clientRef"), "prompt": item.get("prompt")} for item in session.queue]
        await self._broadcast(session.key, {"type": "outbox", "sessionKey": session.key, "items": items, **extra})

    async def _run(self, session: FakeSession, frame: dict[str, Any], task_id: str) -> None:
        session.stopped = False
        session.steered = []
        await self._patch(
            session,
            {
                "set": {
                    "phase": "streaming",
                    "taskId": task_id,
                    "text": "",
                    "reasoning": "",
                    "toolCalls": [],
                    "interactions": [],
                    "steers": None,
                    "prompt": frame.get("prompt"),
                    "model": frame.get("model"),
                    "error": None,
                    "incomplete": None,
                }
            },
        )
        error: str | None = None
        try:
            for step in self.respond(frame, session):
                if session.stopped:
                    break
                error = await self._step(session, step)
                if error is not None:
                    break
        except asyncio.CancelledError:
            return
        if session.stopped:
            error = "Interrupted by user."
        usage = {"contextUsedTokens": 1234, "turnOutputTokens": 56}
        await self._patch(
            session,
            {
                "set": {
                    "phase": "done",
                    "error": error,
                    "incomplete": bool(session.stopped),
                    "interactions": [],
                    "usage": usage,
                }
            },
        )
        snapshot = session.snapshot
        session.turns.append(
            {
                "taskId": task_id,
                "prompt": frame.get("prompt"),
                "text": snapshot.get("text", ""),
                "reasoning": snapshot.get("reasoning", ""),
                "model": frame.get("model"),
                "phase": "done",
            }
        )
        session.running = None
        if session.queue:
            nxt = session.queue.pop(0)
            next_id = f"task-{next(self._tasks)}"
            await self._outbox(session, dispatched={"ref": nxt.get("clientRef"), "taskId": next_id})
            session.running = asyncio.ensure_future(self._run(session, nxt, next_id))

    async def _step(self, session: FakeSession, step: Step) -> str | None:
        kind = step[0]
        if kind == "text":
            await self._patch(session, {"appendText": step[1]})
        elif kind == "reasoning":
            await self._patch(session, {"appendReasoning": step[1]})
        elif kind == "tool":
            calls = [call for call in session.snapshot.get("toolCalls") or [] if call.get("id") != step[1]["id"]]
            held = next(
                (call for call in session.snapshot.get("toolCalls") or [] if call.get("id") == step[1]["id"]), None
            )
            merged = {**(held or {}), **step[1]}
            await self._patch(session, {"set": {"toolCalls": [*calls, merged]}})
        elif kind == "set":
            await self._patch(session, {"set": step[1]})
        elif kind == "ask":
            interaction = step[1]
            await self._patch(session, {"set": {"interactions": [interaction]}})
            session.wake.clear()
            while interaction["promptId"] not in session.answers and not session.stopped:
                await session.wake.wait()
                session.wake.clear()
            await self._patch(session, {"set": {"interactions": []}})
        elif kind == "hold":
            session.wake.clear()
            while not session.stopped and not session.steered:
                await session.wake.wait()
                session.wake.clear()
            if session.steered:
                await self._patch(session, {"appendText": f"steered: {session.steered[-1]}"})
        elif kind == "error":
            return str(step[1])
        return None

    async def _stop(self, client: Client, frame: dict[str, Any]) -> None:
        session = self.sessions.get(frame.get("sessionKey"))
        if session is None or session.running is None:
            return
        if frame.get("taskId") and frame.get("taskId") != session.snapshot.get("taskId"):
            await self._send(
                client,
                {
                    "type": "error",
                    "code": "stale",
                    "sessionKey": session.key,
                    "message": f"stale stop: run {frame.get('taskId')} is not running",
                },
            )
            return
        session.stopped = True
        session.wake.set()

    async def _recall(self, frame: dict[str, Any]) -> None:
        session = self.sessions.get(frame.get("sessionKey"))
        if session is None:
            return
        session.queue = [item for item in session.queue if item.get("clientRef") != frame.get("ref")]
        await self._outbox(session)

    async def _steer(self, frame: dict[str, Any]) -> None:
        session = self.sessions.get(frame.get("sessionKey"))
        if session is None or session.running is None:
            return
        item = next((item for item in session.queue if item.get("clientRef") == frame.get("ref")), None)
        if item is None:
            return
        session.queue.remove(item)
        steers = [*(session.snapshot.get("steers") or []), {"text": item.get("prompt"), "at": 1}]
        session.steered.append(item.get("prompt"))
        await self._outbox(session, steered={"ref": frame.get("ref"), "taskId": session.snapshot.get("taskId")})
        await self._patch(session, {"set": {"steers": steers}})
        session.wake.set()

    def _interact(self, frame: dict[str, Any]) -> None:
        for session in self.sessions.values():
            if any(
                item.get("promptId") == frame.get("promptId") for item in session.snapshot.get("interactions") or []
            ):
                session.answers[frame["promptId"]] = frame
                session.wake.set()


__all__ = ["CATALOGS", "COMMANDS", "ROWS", "RUNGS", "FakeHost", "FakeSession", "echo"]
