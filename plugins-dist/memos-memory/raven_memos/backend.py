"""MemOS Cloud as a memory backend.

REST surface used (``https://memos.memtensor.cn/api/openmem/v1``, the paths the
official ``MemOSClient`` calls): ``POST /add/message`` and ``POST
/search/memory`` for the turn path, ``POST /get/message`` to read one
conversation back, ``POST /delete/memory`` with ``memory_ids`` alone for the
browser. A search row is ``{"id", "memory_key", "memory_value", "memory_type",
"relativity", "conversation_id", ...}`` (observed 2026-09-15).

A search takes its row budget as ``memory_limit_number``; ``top_k`` is not a
field of this route and is ignored rather than refused, which caps a caller
that sends it at the service default of 6 rows.

A search answers only from the views named in ``include_memory_view``; an
unknown name is a 400, so the field is read rather than tolerated. The default
is the factual view alone, which left the rest of the pool unreachable. Two
of those views are the agent's own side of the same conversations and have no
counterpart among the factual rows: a ``tool_memory`` row is one task's
trajectory -- ``tool_value`` gives task, action and result, ``experience``
gives the rule drawn from it -- and a ``skill`` is the ``name``/
``description``/``procedure`` triple several trajectories collapsed into.
``USER_VIEWS`` and ``AGENT_VIEWS`` put each family behind the track that asks
for it.

``memory_limit_number`` is one budget shared across the views in a request, so
naming a view that loses on relevance costs nothing: measured 2026-09-17,
asking for skills beside the factual rows returned no skills at all, because
nine of them topped out at 0.5097 relativity against a 0.5773 floor among the
twenty factual rows that filled the budget. That is the service ranking them,
which is a finding; a caller that never named the view could not tell it apart
from a pool with no skills in it.

MemOS reports failure inside a 200: ``code`` is ``0`` on success, ``40309`` on
rate limiting, and ``message`` is ``"ok"``. ``_effective_status`` folds those
onto the HTTP statuses the base class already handles. A search answers with
several lists; this backend keeps the two that are memory about the user
(``memory_detail_list`` and ``preference_detail_list``) and ignores the tool,
skill, profile and event lists.
"""

from __future__ import annotations

from typing import Any

from raven.contracts.memory import Memory
from raven.memory_engine import Call, HttpMemoryBackend, Reply, clamp_score
from raven.plugins import PluginContext

RATE_LIMITED_CODE = 40309
#: MemOS keeps one identity and sorts what it knows into views, so raven's two
#: tracks are two sets of views against the same ``user_id`` rather than two ids.
#:
#: ``detail_factual`` is the narrative of what happened, which is what the user
#: track asks for. ``tool_memory`` and ``skill`` are the agent's own side: a
#: ``ToolTrajectoryMemory`` records the task, the actions taken, the result and
#: the rule drawn from it, and a skill is the procedure several of those
#: collapsed into. ``preference`` stays out of both -- standing likes and
#: dislikes about a person are not what a coding task needs -- and so do
#: ``event`` and ``profile``.
USER_VIEWS = ("detail_factual",)
AGENT_VIEWS = ("tool_memory", "skill")


def _tool_text(row: dict[str, Any]) -> str:
    """A trajectory row: what was done, then what it taught.

    ``tool_value`` is the run itself (task, actions, result) and ``experience``
    is the rule drawn out of it. The rule alone reads as advice from nowhere,
    and the run alone leaves the reader to re-derive the point, so both go.
    """
    lines = [str(row.get("tool_value") or "").strip(), str(row.get("experience") or "").strip()]
    return "\n".join(line for line in lines if line)


def _skill_text(value: Any) -> str:
    """Flatten a skill into the lines a model can act on.

    A skill is not a sentence like the factual rows are: it arrives as
    ``{"name", "description", "procedure": [...]}``. The procedure is the part
    worth having -- it names files, functions and commands -- so it is kept
    whole under its own title rather than summarised away.
    """
    if not isinstance(value, dict):
        return str(value or "")
    lines = [str(value.get("name") or "").strip(), str(value.get("description") or "").strip()]
    procedure = value.get("procedure")
    if isinstance(procedure, list):
        lines.extend(str(step).strip() for step in procedure)
    elif procedure:
        lines.append(str(procedure).strip())
    return "\n".join(line for line in lines if line)


def _body_code(body: Any) -> int | None:
    if isinstance(body, dict) and isinstance(body.get("code"), int):
        return body["code"]
    return None


class MemosBackend(HttpMemoryBackend):
    NAME = "memos"
    DEFAULT_BASE_URL = "https://memos.memtensor.cn/api/openmem/v1"
    ENV_KEY = "MEMOS_API_KEY"
    SIGNUP_URL = "https://memos-dashboard.openmem.net"

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self._api_key}"}

    def _effective_status(self, reply: Reply) -> int:
        code = _body_code(reply.body)
        if reply.status == 200 and code is not None and code != 0:
            return 429 if code == RATE_LIMITED_CODE else 502
        return reply.status

    def _store_accepted(self, reply: Reply) -> bool:
        return self._effective_status(reply) == 200 and (reply.body or {}).get("message") == "ok"

    def _recall_call(self, query: str, top_k: int, owner: str) -> Call:
        # The row budget is ``memory_limit_number``. ``top_k`` is not a field
        # of this route: it is accepted and ignored, so the service answers with
        # its own default (6 rows, measured 2026-09-17) whatever was asked for.
        return Call(
            "POST",
            "/search/memory",
            json=self._search_body(query, top_k, owner, USER_VIEWS),
        )

    def _agent_recall_call(self, query: str, top_k: int, owner: str) -> Call | None:
        return Call("POST", "/search/memory", json=self._search_body(query, top_k, owner, AGENT_VIEWS))

    @staticmethod
    def _search_body(query: str, top_k: int, owner: str, views: tuple[str, ...]) -> dict[str, Any]:
        return {
            "query": query,
            "user_id": owner,
            "memory_limit_number": top_k,
            "include_memory_view": list(views),
        }

    def _parse_hits(self, body: Any) -> list[Memory]:
        data = (body or {}).get("data") or {}
        out: list[Memory] = []
        rows: list[tuple[str, dict[str, Any]]] = []
        for kind, key in (
            ("memory", "memory_detail_list"),
            ("preference", "preference_detail_list"),
            ("skill", "skill_detail_list"),
            ("tool", "tool_memory_detail_list"),
        ):
            rows.extend((kind, row) for row in data.get(key) or [])
        for kind, row in rows:
            if kind == "skill":
                text = _skill_text(row.get("skill_value"))
            elif kind == "tool":
                text = _tool_text(row)
            else:
                text = str(row.get("memory_value") or row.get("preference") or "")
            if not text:
                continue
            out.append(
                Memory(
                    text=text,
                    score=clamp_score(row.get("relativity", row.get("score"))),
                    metadata={
                        "id": row.get("memory_id") or row.get("id"),
                        "kind": kind,
                        "backend": self.NAME,
                    },
                )
            )
        # The caller keeps the first ``top_k``. Each view arrives sorted within
        # itself, so without this a weaker row from an earlier view would
        # outrank a better one purely by which list it happened to sit in.
        out.sort(key=lambda m: m.score, reverse=True)
        return out

    def _store_call(self, session_id: str, messages: list[dict[str, Any]], owner: str) -> Call:
        rows = [{"role": m["role"], "content": m["content"]} for m in messages]
        return Call(
            "POST",
            "/add/message",
            json={
                "messages": rows,
                "user_id": owner,
                "conversation_id": session_id,
                "async_mode": True,
            },
        )

    def _delete_call(self, memory_id: str, kind: str | None) -> Call | None:
        # The route takes exactly one selector; ``user_id`` beside ``memory_ids``
        # is refused (code 40071), and ``user_id`` alone deletes the whole user.
        return Call("POST", "/delete/memory", json={"memory_ids": [memory_id]})

    def _session_call(self, session_id: str, owner: str) -> Call:
        return Call("POST", "/get/message", json={"user_id": owner, "conversation_id": session_id})

    def _parse_session(self, body: Any) -> list[Memory]:
        data = (body or {}).get("data") or {}
        rows = data.get("message_detail_list") or data.get("messages") or []
        out: list[Memory] = []
        for m in rows:
            content = str(m.get("content") or "")
            if not content:
                continue
            out.append(
                Memory(
                    text=content,
                    score=0.5,
                    metadata={"id": m.get("message_id") or m.get("id"), "kind": "message", "backend": self.NAME},
                )
            )
        return out

    def _health_call(self) -> Call:
        return Call(
            "POST",
            "/search/memory",
            json={
                "query": "health",
                "user_id": self._user_id,
                "memory_limit_number": 1,
                "include_memory_view": list(USER_VIEWS),
            },
        )


def make_backend(ctx: PluginContext) -> MemosBackend:
    """Sync and read-only: ``raven doctor`` constructs without starting."""
    return MemosBackend(ctx)


__all__ = ["AGENT_VIEWS", "USER_VIEWS", "MemosBackend", "make_backend"]
