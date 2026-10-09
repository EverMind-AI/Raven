"""A fake EverOS Cloud, for the cloud memory plugin's tests and acceptance runs.

One request handler, two faces. :class:`FakeCloud` wraps it as an
``httpx.MockTransport`` for in-process tests; ``python -m tests._everos_cloud_fake``
serves the same handler over HTTP so a real ``raven`` process can be pointed at
it through the cloud slice's ``base_url``.

The handler speaks the four routes the plugin uses (``POST /api/v2/memory/{search,add,flush,get}``)
with the response shapes docs.evermind.ai documents, and, when asked, the OSS
``GET /health`` the local plugin probes. Every field the plugin can send is
either honoured or refused with a 400 naming it; nothing is read and ignored,
so a request shape the fake accepts is one the documentation accepts. A
catch-all records every request -- unknown routes included -- to a ledger, so
an assertion made from the ledger is about what the host process really sent.

Scripted answers come in modes (``ok``, ``empty``, ``401``, ``403``, ``429``,
``500``, ``418``, ``hang``) switchable at run time through ``POST /_fake/mode``,
so one server can serve a whole acceptance journey. The ``ok`` data carries
sentinel values nothing in Raven's configuration contains.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

EPISODE_SUBJECT = "ECM-EP-5c1d"
TOTALS = {"episode": 7, "profile": 1, "agent_case": 2, "agent_skill": 3}
REFUSED_MESSAGE = "ECM-REFUSED-401"

MODES = ("ok", "empty", "401", "403", "429", "500", "418", "hang")
MEMORY_ROUTES = (
    "/api/v2/memory/search",
    "/api/v2/memory/add",
    "/api/v2/memory/flush",
    "/api/v2/memory/get",
)
MODE_ROUTE = "/_fake/mode"
HANG_SECONDS = 3600.0

ADD_MAX_MESSAGES = 500
ADD_MAX_BYTES = 300_000

_USER_TYPES = {"episode": "episodes", "profile": "profiles"}
_AGENT_TYPES = {"agent_case": "agent_cases", "agent_skill": "agent_skills"}
_TYPE_FIELD = {**_USER_TYPES, **_AGENT_TYPES}

_SEARCH_FIELDS = {
    "query",
    "user_id",
    "agent_id",
    "top_k",
    "include_profile",
    "method",
    "min_score",
    "radius",
    "enable_llm_rerank",
    "with_readable_episode",
    "filters",
}
_GET_FIELDS = {"user_id", "agent_id", "memory_type", "page", "page_size", "sort_by", "sort_order", "filters"}
_ADD_FIELDS = {"session_id", "messages", "async_mode", "mode", "app_id", "project_id"}
_FLUSH_FIELDS = {"session_id", "app_id", "project_id"}
_MESSAGE_FIELDS = {"sender_id", "role", "timestamp", "content", "sender_name", "tool_calls", "tool_call_id"}
_ROLES = {"user", "assistant", "tool"}


def default_rows() -> dict[str, list[dict[str, Any]]]:
    """The ``ok`` data. ``session_id: None`` rows match any session filter and
    are reported under the session that was asked for."""
    return {
        "episodes": [
            {
                "id": "ep-5c1d",
                "subject": EPISODE_SUBJECT,
                "episode": "The reader prefers short answers and names the file they mean.",
                "summary": "Prefers short answers; names files.",
                "score": 0.91,
                "session_id": None,
                "timestamp": 1_760_000_000_000,
            }
        ],
        "profiles": [
            {
                "id": "pr-5c1d",
                "profile_data": {
                    "role": "developer",
                    "traits": [{"trait": "concise", "description": "prefers short replies"}],
                },
                "score": None,
            }
        ],
        "agent_cases": [
            {
                "id": "ac-5c1d",
                "task_intent": "ECM-CASE-5c1d: rename a module",
                "approach": "grep every importer first",
                "key_insight": "the registry re-exports it",
                "quality_score": 0.8,
                "score": 0.77,
                "session_id": None,
                "timestamp": 1_760_000_000_000,
            }
        ],
        "agent_skills": [
            {
                "id": "as-5c1d",
                "name": "ECM-SKILL-5c1d",
                "description": "rename safely",
                "content": "List importers before renaming.",
                "confidence": 0.6,
                "maturity_score": 0.5,
                "score": 0.7,
            }
        ],
    }


def _error(status: int, code: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"request_id": uuid.uuid4().hex, "error": {"code": code, "message": message}}


def _ok(status: int, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return status, {"request_id": uuid.uuid4().hex, "data": data}


def _unknown_fields(body: dict[str, Any], allowed: set[str]) -> list[str]:
    return sorted(k for k in body if k not in allowed)


def _owner(body: dict[str, Any]) -> tuple[str, str] | tuple[int, dict[str, Any]]:
    """``("user", id)`` / ``("agent", id)``, or the 422 the service answers."""
    has_user, has_agent = "user_id" in body, "agent_id" in body
    if has_user == has_agent:
        return _error(422, "invalid_parameter", "exactly one of user_id, agent_id is required")
    return ("user", str(body["user_id"])) if has_user else ("agent", str(body["agent_id"]))


def _session_filter(body: dict[str, Any]) -> str | None:
    filters = body.get("filters")
    if isinstance(filters, dict) and isinstance(filters.get("session_id"), str):
        return filters["session_id"]
    return None


@dataclass
class FakeCloud:
    """The handler and its state; one instance per fake server or transport."""

    mode: str = "ok"
    hang_on: set[str] = field(default_factory=set)
    rows: dict[str, list[dict[str, Any]]] = field(default_factory=default_rows)
    with_health: bool = False
    require_auth: bool = True
    ledger: list[dict[str, Any]] = field(default_factory=list)
    ledger_path: Path | None = None
    unflushed: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── control ──────────────────────────────────────────────────────

    def set_mode(self, mode: str, hang_on: set[str] | list[str] | None = None) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; one of {MODES}")
        self.mode = mode
        if hang_on is not None:
            self.hang_on = set(hang_on)
        elif mode != "hang":
            self.hang_on = set()

    def hangs(self, path: str) -> bool:
        # The control route never hangs: it is how a hung fake is told to stop hanging.
        return path != MODE_ROUTE and self.mode == "hang" and (not self.hang_on or path in self.hang_on)

    # ── the handler ─────────────────────────────────────────────────

    def handle(self, method: str, path: str, headers: dict[str, str], raw: bytes) -> tuple[int, dict[str, Any]]:
        """Answer one request; the faces record it and apply ``hang``."""
        body: Any = None
        if raw:
            try:
                body = json.loads(raw)
            except ValueError:
                return _error(400, "invalid_parameter", "body is not JSON")
        if path == MODE_ROUTE and method == "POST":
            return self._control(body)
        if path == "/health" and method == "GET" and self.with_health:
            return 200, {"status": "ok", "capabilities": {"embed": True, "rerank": True}}
        if path not in MEMORY_ROUTES or method != "POST":
            return _error(404, "not_found", f"no route {method} {path}")
        auth = headers.get("authorization", "")
        if not self.require_auth and not auth:
            # The OSS face: the local plugin sends no key, and the local server asks for none.
            auth = "Bearer oss"
        if not auth.startswith("Bearer ") or len(auth) <= len("Bearer "):
            return _error(401, "unauthorized", "missing or invalid token")
        if self.mode == "401":
            return _error(401, "unauthorized", REFUSED_MESSAGE)
        if self.mode == "403":
            return _error(403, "forbidden", "account not authorized for v2")
        if self.mode == "429":
            return _error(429, "rate_limited", "rate limit exceeded")
        if self.mode == "500":
            return _error(500, "internal", "ECM-INTERNAL-500")
        if self.mode == "418":
            return _error(418, "teapot", "ECM-TEAPOT-418")
        if not isinstance(body, dict):
            return _error(400, "invalid_parameter", "body must be a JSON object")
        if path.endswith("/search"):
            return self._search(body)
        if path.endswith("/get"):
            return self._get(body)
        if path.endswith("/add"):
            return self._add(body, len(raw))
        return self._flush(body)

    def _control(self, body: Any) -> tuple[int, dict[str, Any]]:
        if not isinstance(body, dict) or body.get("mode") not in MODES:
            return _error(400, "invalid_parameter", f"mode must be one of {MODES}")
        self.set_mode(str(body["mode"]), body.get("hang_on"))
        if "auth" in body:
            self.require_auth = bool(body["auth"])
        return _ok(200, {"mode": self.mode, "hang_on": sorted(self.hang_on), "auth": self.require_auth})

    def _rows(self, type_names: list[str], session: str | None, limit: int | None) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {f: [] for f in _TYPE_FIELD.values()}
        if self.mode == "empty":
            return out
        for name in type_names:
            fld = _TYPE_FIELD[name]
            rows = []
            for row in self.rows.get(fld, []):
                if session is not None and "session_id" in row:
                    sid = row["session_id"]
                    if sid is None:
                        row = {**row, "session_id": session}
                    elif sid != session:
                        continue
                rows.append(row)
            out[fld] = rows[:limit] if limit is not None else rows
        return out

    def _search(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        unknown = _unknown_fields(body, _SEARCH_FIELDS)
        if unknown:
            return _error(400, "invalid_parameter", f"unknown field(s): {', '.join(unknown)}")
        if not isinstance(body.get("query"), str):
            return _error(400, "invalid_parameter", "query is required")
        owner = _owner(body)
        if isinstance(owner[0], int):
            return owner  # type: ignore[return-value]
        track = owner[0]
        top_k = body.get("top_k", -1)
        limit = None if top_k in (None, -1) else int(top_k)
        if track == "user":
            types = ["episode"] + (["profile"] if body.get("include_profile") is True else [])
        else:
            types = ["agent_case", "agent_skill"]
        data = self._rows(types, _session_filter(body), limit)
        data["unprocessed_messages"] = []
        return _ok(200, data)

    def _get(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        unknown = _unknown_fields(body, _GET_FIELDS)
        if unknown:
            return _error(400, "invalid_parameter", f"unknown field(s): {', '.join(unknown)}")
        owner = _owner(body)
        if isinstance(owner[0], int):
            return owner  # type: ignore[return-value]
        track = owner[0]
        memory_type = body.get("memory_type")
        if memory_type not in _TYPE_FIELD:
            return _error(
                400, "invalid_parameter", "memory_type must be one of episode, profile, agent_case, agent_skill"
            )
        if (track == "user") != (memory_type in _USER_TYPES):
            return _error(422, "invalid_parameter", f"memory_type '{memory_type}' is not valid when {track}_id is set")
        page_size = int(body.get("page_size", 20))
        data = self._rows([memory_type], _session_filter(body), page_size)
        count = len(data[_TYPE_FIELD[memory_type]])
        data.update({"total_count": 0 if self.mode == "empty" else max(TOTALS[memory_type], count), "count": count})
        return _ok(200, data)

    def _add(self, body: dict[str, Any], raw_len: int) -> tuple[int, dict[str, Any]]:
        unknown = _unknown_fields(body, _ADD_FIELDS)
        if unknown:
            return _error(400, "invalid_parameter", f"unknown field(s): {', '.join(unknown)}")
        session = body.get("session_id")
        if not isinstance(session, str) or not 1 <= len(session) <= 128:
            return _error(400, "invalid_parameter", "session_id is required (1-128 chars)")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return _error(400, "invalid_parameter", "messages must hold at least one item")
        if len(messages) > ADD_MAX_MESSAGES or raw_len > ADD_MAX_BYTES:
            return _error(
                413, "payload_too_large", f"at most {ADD_MAX_MESSAGES} messages and {ADD_MAX_BYTES} bytes per add"
            )
        if body.get("mode", "chat") not in ("chat", "agent"):
            return _error(400, "invalid_parameter", "mode must be chat or agent")
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                return _error(400, "invalid_parameter", f"messages[{i}] must be an object")
            bad = _unknown_fields(msg, _MESSAGE_FIELDS)
            if bad:
                return _error(400, "invalid_parameter", f"messages[{i}]: unknown field(s): {', '.join(bad)}")
            if not isinstance(msg.get("sender_id"), str) or not msg["sender_id"]:
                return _error(400, "invalid_parameter", f"messages[{i}].sender_id is required")
            if msg.get("role") not in _ROLES:
                return _error(400, "invalid_parameter", f"messages[{i}].role must be user, assistant or tool")
            ts = msg.get("timestamp")
            if isinstance(ts, bool) or not isinstance(ts, int):
                return _error(400, "invalid_parameter", f"messages[{i}].timestamp must be an integer (unix ms)")
            if not isinstance(msg.get("content"), (str, list)):
                return _error(400, "invalid_parameter", f"messages[{i}].content must be a string or a list")
        with self._lock:
            self.unflushed.add(session)
        if body.get("async_mode", True) is False:
            return _ok(200, {"message_count": len(messages), "status": "accumulated"})
        return _ok(202, {"message_count": len(messages), "status": "queued"})

    def _flush(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        unknown = _unknown_fields(body, _FLUSH_FIELDS)
        if unknown:
            return _error(400, "invalid_parameter", f"unknown field(s): {', '.join(unknown)}")
        session = body.get("session_id")
        if not isinstance(session, str) or not session:
            return _error(400, "invalid_parameter", "session_id is required")
        with self._lock:
            pending = session in self.unflushed
            self.unflushed.discard(session)
        return _ok(200, {"status": "extracted" if pending else "no_extraction"})

    # ── recording ───────────────────────────────────────────────────

    def record(self, method: str, path: str, headers: dict[str, str], raw: bytes, status: int | None) -> None:
        try:
            body: Any = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode("utf-8", "replace")
        entry = {
            "ts": time.time(),
            "method": method,
            "path": path,
            "headers": dict(headers),
            "body": body,
            "status": status,
        }
        with self._lock:
            self.ledger.append(entry)
            if self.ledger_path is not None:
                with self.ledger_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ── the in-process face ─────────────────────────────────────────

    def transport(self) -> httpx.MockTransport:
        async def _handler(request: httpx.Request) -> httpx.Response:
            headers = {k.lower(): v for k, v in request.headers.items()}
            raw = request.content
            path = request.url.path
            if self.hangs(path):
                self.record(request.method, path, headers, raw, None)
                await asyncio.sleep(HANG_SECONDS)
            status, payload = self.handle(request.method, path, headers, raw)
            self.record(request.method, path, headers, raw, status)
            return httpx.Response(status, json=payload)

        return httpx.MockTransport(_handler)

    # ── the HTTP face ───────────────────────────────────────────────

    def serve(self, port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                if fake.hangs(self.path):
                    fake.record(self.command, self.path, headers, raw, None)
                    time.sleep(HANG_SECONDS)
                status, payload = fake.handle(self.command, self.path, headers, raw)
                fake.record(self.command, self.path, headers, raw, status)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                self._serve()

            def do_GET(self) -> None:
                self._serve()

            def log_message(self, fmt: str, *args: Any) -> None:
                return None

        server = ThreadingHTTPServer((host, port), Handler)
        server.daemon_threads = True
        return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve a fake EverOS Cloud for acceptance runs.")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--mode", choices=MODES, default="ok")
    parser.add_argument("--ledger", type=Path, default=None)
    parser.add_argument("--with-health", action="store_true")
    parser.add_argument("--hang-on", action="append", default=[], help="route path to hang on in hang mode; repeatable")
    args = parser.parse_args(argv)
    fake = FakeCloud(mode=args.mode, hang_on=set(args.hang_on), with_health=args.with_health, ledger_path=args.ledger)
    if args.ledger is not None:
        args.ledger.parent.mkdir(parents=True, exist_ok=True)
    server = fake.serve(args.port)
    print(f"fake everos cloud on http://127.0.0.1:{args.port} mode={args.mode}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
