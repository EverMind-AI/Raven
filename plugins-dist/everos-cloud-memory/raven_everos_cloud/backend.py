"""EverosCloudBackend -- the host's :class:`MemoryBackend` over EverOS Cloud.

The same four routes the local plugin speaks (``POST /api/v2/memory/{search,add,flush,get}``),
with three differences that are the whole of this module's reason to exist:

- **A Bearer key instead of a server.** The key comes from the plugin's own
  slice first and ``EVEROS_CLOUD_API_KEY`` second; without one nothing is sent
  (``recall`` is ``[]``, ``store`` is ``False``, ``health`` says what to set).
  There is no process to probe, spawn or stop, so there is no service state
  machine either: every call stands on its own.
- **Every add is ``mode: "agent"``.** The local server runs both extraction
  pipelines on every add; the cloud makes that a per-request field that
  defaults to ``chat``, and ``chat`` would silently stop agent-track memories.
- **Flush only when the caller says the conversation is over, and at stop.**
  ``metadata["flush"]`` (the sub-agent handoff) and ``metadata["is_final"]``
  (the importer's last batch) flush right after their adds; ordinary turns
  never do, because the cloud extracts on its own. ``stop()`` flushes whatever
  this process added to and never flushed, within a small budget.

Identity is the host's: ``ctx.services.user_id`` / ``agent_id`` name the owner
on every request, and a per-call override arrives in ``store``'s metadata in
either spelling (``user_id`` or ``userId``). The translation layer --
message conversion, result flattening, the profile cap -- is copied from
``raven_everos.backend`` so the two plugins file and read memories the same
way.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

import httpx

from raven.contracts.memory import BackendHealth, HealthCheck, Memory
from raven.plugins import PluginContext

logger = logging.getLogger("raven_everos_cloud")

DEFAULT_BASE_URL = "https://api.evermind.ai"
ENV_KEY = "EVEROS_CLOUD_API_KEY"
KEYS_URL = "https://everos.evermind.ai"
PLUGIN_ID = "everos-cloud-memory"
BACKEND_NAME = "everos-cloud"

# The host abandons a per-turn recall at 5.0 s (raven/context_engine/segments/memory.py);
# the skill router has no budget of its own, so this bound is the only one it gets.
RECALL_TIMEOUT_S = 4.0
# The sub-agent read-back allows 30 s (raven/agent/subagent_memory.py); a
# read right after extraction is the slow case.
SESSION_TIMEOUT_S = 10.0
# The cloud queues an add and answers 202 at once.
ADD_TIMEOUT_S = 30.0
# Extraction runs inside a flush. The variable exists so an acceptance run can
# make a hung flush give up in seconds; it is a test hook, not a feature.
FLUSH_TIMEOUT_ENV = "RAVEN_EVEROS_CLOUD_FLUSH_TIMEOUT_S"
DEFAULT_FLUSH_TIMEOUT_S = 360.0
HEALTH_TIMEOUT_S = 5.0
# One budget for the whole sweep at stop, as the local plugin's.
SHUTDOWN_FLUSH_BUDGET_S = 5.0
# Documented cloud limits are 500 messages and 300 KB; the byte ceiling keeps
# headroom for the envelope.
ADD_MAX_MESSAGES = 500
ADD_MAX_BYTES = 250_000
_SESSION_PAGE_SIZE = 100

_OwnerType = Literal["user", "agent"]

_STALE_IDENTITY_KEYS = ("user_id", "agent_id", "userId", "agentId")


def flush_timeout_s() -> float:
    """Read at call time so the environment hook works in a running process."""
    return float(os.environ.get(FLUSH_TIMEOUT_ENV) or DEFAULT_FLUSH_TIMEOUT_S)


def resolve_api_key(config: Mapping[str, Any]) -> tuple[str, str | None]:
    """``(key, source)``: the slice first, then the environment; ``("", None)`` when neither.

    File before environment is the order the web tool vendor keys use, and the
    one the settings page edits; a key that came from the environment is never
    written back, so the file records only what a person typed.
    """
    key = str(config.get("api_key") or "")
    if key:
        return key, "file"
    key = os.environ.get(ENV_KEY, "")
    return (key, "env") if key else ("", None)


# ---------------------------------------------------------------------------
# Translation layer, copied from raven_everos.backend
# ---------------------------------------------------------------------------


def as_ms_epoch(value: Any) -> int | None:
    """A message's timestamp as the service wants it (unix ms), or ``None``.

    Accepts a ms-epoch int, a seconds-epoch int and an ISO 8601 string. The
    seconds/ms split is by magnitude: a seconds value stays below the threshold
    until the year 5138.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            return None
        return int(number * 1000) if number < 100_000_000_000 else int(number)
    if isinstance(value, str):
        try:
            ms = int(datetime.fromisoformat(value).timestamp() * 1000)
        except ValueError:
            return None
        return ms if ms > 0 else None
    return None


def convert_messages(
    messages: list[dict[str, Any]],
    *,
    agent_id: str,
    user_id: str = "default",
) -> list[dict[str, Any]]:
    """AgentLoop ``{"role", "content", ...}`` rows as the service's message items.

    ``assistant`` / ``tool`` messages get ``sender_id = agent_id`` so the agent
    track accrues under the stable agent identity; ``user`` messages keep their
    own ``sender_id`` or get ``user_id``. ``system`` is dropped; multimodal
    parts collapse to their text; a missing timestamp becomes now.
    """
    now_ms = int(time.time() * 1000)
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant", "tool"):
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text", "")).strip()
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
        if not isinstance(content, str):
            content = str(content)
        tool_calls = m.get("tool_calls") if role == "assistant" else None
        if not content and not tool_calls:
            continue
        entry: dict[str, Any] = {
            "sender_id": agent_id if role in ("assistant", "tool") else (m.get("sender_id") or user_id),
            "role": role,
            "timestamp": as_ms_epoch(m.get("timestamp")) or now_ms,
            "content": content,
        }
        if tool_calls:
            entry["tool_calls"] = tool_calls
        if role == "tool" and m.get("tool_call_id"):
            entry["tool_call_id"] = m["tool_call_id"]
        out.append(entry)
    return out


def _owner_override(metadata: dict[str, Any] | None, key: str) -> str:
    """One per-call owner id from ``store``'s metadata, in either spelling.

    The block reaches here as an agent wrote it in raven's config, which spells
    its keys in camelCase; the contract documents them in snake_case. Reading
    only one of the two made the override silently never fire for a real
    config, filing a sub-agent's memories under the host's own identity.
    """
    if not metadata:
        return ""
    return str(metadata.get(key) or metadata.get(key.replace("_id", "Id")) or "")


# The profile grows for the life of an install with no server-side cap; this
# keeps it from outweighing every episode in the recalled block.
_PROFILE_MAX_CHARS = 1200


def _cap_profile_text(text: str) -> str:
    if len(text) <= _PROFILE_MAX_CHARS:
        return text
    head, _, _ = text[:_PROFILE_MAX_CHARS].rpartition("\n")
    kept = head or text[:_PROFILE_MAX_CHARS]
    omitted = len(text) - len(kept)
    return f"{kept}\n[profile truncated, {omitted} chars omitted]"


def _flatten_profile_list(items: list[Any]) -> list[str]:
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            lines.append(f"- {item}")
            continue
        label = item.get("category") or item.get("trait")
        body = item.get("description")
        if label and body:
            lines.append(f"- {label}: {body}")
        elif label or body:
            lines.append(f"- {label or body}")
    return lines


def _flatten_profile(profile_data: Any) -> str:
    """Profile dict as prompt lines; the ``*_ms`` bookkeeping keys are skipped."""
    if not isinstance(profile_data, dict):
        return _cap_profile_text(str(profile_data))
    lines: list[str] = []
    for key, value in profile_data.items():
        if key.endswith("_ms"):
            continue
        if isinstance(value, list):
            lines.extend(_flatten_profile_list(value))
        else:
            lines.append(f"{key}: {value}")
    return _cap_profile_text("\n".join(lines))


def _score(row: dict[str, Any], default: float = 0.0) -> float:
    try:
        value = row.get("score")
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def search_data_to_memories(data: Any, owner_type: _OwnerType) -> list[Memory]:
    """Flatten the service's ``data`` envelope into ``list[Memory]``, sorted by score."""
    out: list[Memory] = []
    if not isinstance(data, dict):
        return out
    if owner_type == "user":
        for ep in data.get("episodes") or []:
            if not isinstance(ep, dict):
                continue
            text = ep.get("summary") or ep.get("episode") or ""
            out.append(
                Memory(
                    text=str(text),
                    score=_score(ep),
                    metadata={
                        "id": ep.get("id"),
                        "session_id": ep.get("session_id"),
                        "type": "episode",
                        "owner_type": "user",
                    },
                )
            )
        for prof in data.get("profiles") or []:
            if not isinstance(prof, dict):
                continue
            out.append(
                Memory(
                    text=_flatten_profile(prof.get("profile_data")),
                    score=_score(prof, 1.0) if prof.get("score") is not None else 1.0,
                    metadata={"id": prof.get("id"), "type": "profile", "owner_type": "user"},
                )
            )
    else:
        for skill in data.get("agent_skills") or []:
            if not isinstance(skill, dict):
                continue
            out.append(
                Memory(
                    text=str(skill.get("content") or ""),
                    score=_score(skill),
                    metadata={
                        "id": skill.get("id"),
                        "name": skill.get("name", ""),
                        "type": "skill",
                        "owner_type": "agent",
                        "confidence": skill.get("confidence"),
                    },
                )
            )
        for case in data.get("agent_cases") or []:
            if not isinstance(case, dict):
                continue
            text = "\n\n".join(
                part
                for field in ("task_intent", "approach", "key_insight")
                if (part := str(case.get(field) or "").strip())
            )
            out.append(
                Memory(
                    text=text,
                    score=_score(case),
                    metadata={"id": case.get("id"), "type": "case", "owner_type": "agent"},
                )
            )
    out.sort(key=lambda m: m.score, reverse=True)
    return out


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------


class EverosCloudBackend:
    """raven_everos_cloud's :class:`MemoryBackend` implementation."""

    def __init__(self, ctx: PluginContext, *, client: httpx.AsyncClient | None = None) -> None:
        self._config: dict[str, Any] = dict(ctx.config or {})
        self._logger = ctx.logger
        self._user_id: str = ctx.services.user_id
        self._agent_id: str = ctx.services.agent_id
        self._warn_stale_identity_keys()
        self._api_key, self._key_source = resolve_api_key(self._config)
        self._base_url: str = str(self._config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self._client: httpx.AsyncClient | None = client
        self._owns_client = client is None
        self._unflushed: set[str] = set()
        self._stopping = False
        self._no_key_logged = False

    # ── identity ────────────────────────────────────────────────────

    def _warn_stale_identity_keys(self) -> None:
        for key in _STALE_IDENTITY_KEYS:
            stale = self._config.get(key)
            if stale is None:
                continue
            current = self._user_id if key in ("user_id", "userId") else self._agent_id
            if stale != current:
                self._logger.warning(
                    "plugins.config['%s'].%s=%r is ignored; identity comes from memory.%s=%r. Remove the key.",
                    PLUGIN_ID,
                    key,
                    stale,
                    "userId" if key in ("user_id", "userId") else "agentId",
                    current,
                )

    # ── transport ───────────────────────────────────────────────────

    def _client_or_open(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(ADD_TIMEOUT_S))
        return self._client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _has_key(self, what: str) -> bool:
        if self._api_key:
            return True
        if not self._no_key_logged:
            self._no_key_logged = True
            self._logger.warning("EverOS Cloud %s skipped: no API key; set %s or run raven onboard", what, ENV_KEY)
        return False

    async def _post(self, path: str, body: dict[str, Any], timeout: float) -> httpx.Response:
        client = self._client_or_open()
        return await asyncio.wait_for(
            client.post(f"{self._base_url}{path}", json=body, headers=self._headers()),
            timeout=timeout,
        )

    # ── lifecycle ───────────────────────────────────────────────────

    async def start(self) -> None:
        self._client_or_open()

    async def stop(self) -> None:
        self._stopping = True
        try:
            await self._sweep_unflushed()
        finally:
            if self._owns_client and self._client is not None:
                await self._client.aclose()
                self._client = None

    async def _sweep_unflushed(self) -> None:
        """One flush per session this process added to and never flushed.

        Only an explicit end flushes during a run, so the sweep is the only
        flush most sessions ever get from Raven; the cloud's own extraction is
        the fallback for whatever the budget leaves behind.
        """
        pending = sorted(self._unflushed)
        if not pending or not self._api_key:
            return
        deadline = time.monotonic() + SHUTDOWN_FLUSH_BUDGET_S
        flushed = 0
        for session_id in list(pending):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                r = await self._post("/api/v2/memory/flush", {"session_id": session_id}, timeout=remaining)
                r.raise_for_status()
            except Exception as exc:  # noqa: BLE001 - a sweep must not fail the shutdown
                self._logger.warning("EverOS Cloud stop: flush of session %s failed (%s)", session_id, exc)
                break
            self._unflushed.discard(session_id)
            flushed += 1
        self._logger.info("flushed %d session(s) at stop", flushed)
        if self._unflushed:
            self._logger.warning(
                "EverOS Cloud stop: flush budget %ss exhausted with %d session(s) unflushed: %s",
                SHUTDOWN_FLUSH_BUDGET_S,
                len(self._unflushed),
                sorted(self._unflushed),
            )

    # ── MemoryBackend ───────────────────────────────────────────────

    async def recall(
        self,
        query: str,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        top_k: int,
    ) -> list[Memory]:
        if (user_id is None) == (agent_id is None):
            self._logger.warning(
                "EverosCloudBackend.recall: expected exactly one of user_id / agent_id (got user_id=%r, agent_id=%r); returning empty",
                user_id,
                agent_id,
            )
            return []
        if not self._has_key("recall"):
            return []
        owner_type: _OwnerType = "user" if user_id is not None else "agent"
        body: dict[str, Any] = {"query": query, "top_k": top_k}
        if user_id is not None:
            body["user_id"] = user_id
            body["include_profile"] = True
        else:
            body["agent_id"] = agent_id
        try:
            r = await self._post("/api/v2/memory/search", body, timeout=RECALL_TIMEOUT_S)
            r.raise_for_status()
            payload = r.json() or {}
        except asyncio.TimeoutError:
            self._logger.warning("recall timed out after %.1f s; returning empty", RECALL_TIMEOUT_S)
            return []
        except Exception as exc:  # noqa: BLE001 - a recall must never cost the turn
            self._logger.warning("EverosCloudBackend.recall failed (%s); returning empty", exc)
            return []
        return search_data_to_memories(payload.get("data"), owner_type)[:top_k]

    async def store(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        if not messages:
            return True
        payload = convert_messages(
            messages,
            agent_id=_owner_override(metadata, "agent_id") or self._agent_id,
            user_id=_owner_override(metadata, "user_id") or self._user_id,
        )
        if not payload:
            return True
        if not self._has_key("store"):
            return False
        batches, dropped = self._batches(session_id, payload)
        explicit_end = bool(metadata and (metadata.get("flush") or metadata.get("is_final")))
        self._unflushed.add(session_id)
        for batch in batches:
            body = {"session_id": session_id, "mode": "agent", "messages": batch}
            if not await self._send("add", "/api/v2/memory/add", body, ADD_TIMEOUT_S):
                return False
        if explicit_end:
            if not await self._send("flush", "/api/v2/memory/flush", {"session_id": session_id}, flush_timeout_s()):
                return False
            self._unflushed.discard(session_id)
        return not dropped

    def _batches(self, session_id: str, payload: list[dict[str, Any]]) -> tuple[list[list[dict[str, Any]]], bool]:
        """Split a slice at the service's limits; a single oversize message is dropped and named.

        A message over the byte ceiling cannot be made to fit without storing
        words the user never said, so it is left out and the rest goes on; the
        caller hears ``False`` for the slice.
        """
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        size = 0
        dropped = False
        for index, message in enumerate(payload):
            item_size = len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
            if item_size > ADD_MAX_BYTES:
                # ponytail: a single message over the ceiling is dropped whole; chunk one
                # message across adds if real turns ever hit this.
                dropped = True
                self._logger.warning(
                    "EverosCloudBackend.store: message %d of session %s is %d bytes, over the %d-byte ceiling; not stored",
                    index,
                    session_id,
                    item_size,
                    ADD_MAX_BYTES,
                )
                continue
            if current and (len(current) >= ADD_MAX_MESSAGES or size + item_size > ADD_MAX_BYTES):
                batches.append(current)
                current, size = [], 0
            current.append(message)
            size += item_size
        if current:
            batches.append(current)
        return batches, dropped

    async def _send(self, what: str, path: str, body: dict[str, Any], timeout: float) -> bool:
        try:
            r = await self._post(path, body, timeout=timeout)
            r.raise_for_status()
        except asyncio.TimeoutError:
            self._logger.warning(
                "EverosCloudBackend.store: %s timed out after %ss; this slice was not indexed", what, timeout
            )
            return False
        except Exception as exc:  # noqa: BLE001 - a write failure is reported, never raised
            level = self._logger.info if self._stopping else self._logger.warning
            level("EverosCloudBackend.store: %s failed (%s); this slice was not indexed", what, exc)
            return False
        return True

    async def recall_session(
        self,
        session_id: str,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[Memory]:
        if (user_id is None) == (agent_id is None) or not self._has_key("recall_session"):
            return []
        owner_type: _OwnerType = "user" if user_id is not None else "agent"
        body: dict[str, Any] = {
            "memory_type": "episode" if owner_type == "user" else "agent_case",
            "filters": {"session_id": session_id},
            "page_size": _SESSION_PAGE_SIZE,
        }
        body["user_id" if owner_type == "user" else "agent_id"] = user_id if owner_type == "user" else agent_id
        try:
            r = await self._post("/api/v2/memory/get", body, timeout=SESSION_TIMEOUT_S)
            r.raise_for_status()
            payload = r.json() or {}
        except Exception as exc:  # noqa: BLE001 - a read-back must not fail the caller
            self._logger.warning("EverosCloudBackend.recall_session failed (%s); returning empty", exc)
            return []
        return search_data_to_memories(payload.get("data"), owner_type)

    async def delete(self, memory_id: str, *, kind: str | None = None) -> bool:
        # The cloud deletes by scope, not by memory id, and no caller remains.
        return False

    async def feedback(self, signals: dict[str, Any]) -> None:
        return None

    async def health(self) -> BackendHealth:
        if not self._api_key:
            return self._missing(f"no API key; set {ENV_KEY} or run raven onboard")
        body = {"memory_type": "episode", "user_id": self._user_id, "page_size": 1}
        try:
            r = await self._post("/api/v2/memory/get", body, timeout=HEALTH_TIMEOUT_S)
        except asyncio.TimeoutError:
            return self._missing(f"cannot reach {self._base_url}: no answer within {HEALTH_TIMEOUT_S:g}s")
        except Exception as exc:  # noqa: BLE001 - a probe reads as "not reachable"
            return self._missing(f"cannot reach {self._base_url}: {exc}")
        status = r.status_code
        if 200 <= status < 300:
            return BackendHealth(ready=True, checks=[HealthCheck(BACKEND_NAME, "ok", self._base_url)])
        if status == 401:
            return self._missing(f"API key rejected (401); check the key in plugins.config.{PLUGIN_ID} or {ENV_KEY}")
        if status == 403:
            return self._missing(
                "refused (403); this may mean the account is not authorized for the v2 memory API; see " + KEYS_URL
            )
        if status == 429:
            return BackendHealth(
                ready=True, checks=[HealthCheck(BACKEND_NAME, "degraded", f"rate limited by {self._base_url}")]
            )
        return self._missing(f"{self._base_url} answered {status}")

    def _missing(self, hint: str) -> BackendHealth:
        return BackendHealth(ready=False, checks=[HealthCheck(BACKEND_NAME, "missing", hint)])


def make_backend(ctx: PluginContext) -> EverosCloudBackend:
    """Sync and read-only: ``raven doctor`` constructs without starting."""
    return EverosCloudBackend(ctx)
