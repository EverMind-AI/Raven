"""Deep research tool: delegate a question to the MiroThinker API.

Two transports share one tool. Local interactive surfaces (CLI/TUI) stream the
answer inline over ``/chat/completions`` SSE. Channels (weixin, ...) run it
async: the tool hands the query to :class:`DeepResearchManager`, which
submits it to the ``/v1/responses`` background endpoint, polls, and delivers the
finished answer back to the conversation verbatim via a ``deliver_text`` turn.
"""

import asyncio
import datetime
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from raven.agent.tools.base import Tool
from raven.config.schema import DeepResearchToolConfig

DEFAULT_BASE_URL = "https://api.miromind.ai/v1"
DEFAULT_MODEL = "mirothinker-1-7-deepresearch-mini"
# Hardcoded (not a config field), like the skill-hub cache / checkpoint subdirs.
_OUTPUT_SUBDIR = "deep_research"


@dataclass(frozen=True)
class _Routing:
    """Per-turn delivery target. Frozen and stored in a ContextVar so a
    concurrent turn cannot clobber this turn's routing (the tool is shared)."""

    channel: str
    chat_id: str
    conversation: str


# Coarse progress shown while the engine works, keyed by the reasoning-step type
# the stream reports. One line per step-type change (not per token) keeps it
# readable on both a terminal and the TUI.
_PROGRESS_LABELS = {
    "thinking": "thinking...",
    "web_search": "searching the web...",
    "fetch_url_content": "reading a page...",
    "execute_python": "running analysis...",
    "execute_command": "running a command...",
}

# The callback signature the loop wires per-turn on streaming surfaces:
# ``cb("progress", line)`` while researching, ``cb("answer", content)`` once done.
StreamCallback = Callable[[str, str], Awaitable[None]]


class DeepResearchTool(Tool):
    """Delegate a research question to MiroThinker and return its finished answer.

    The API is OpenAI-compatible but the answer is minute-scale; it is consumed
    over SSE (``stream: true``) so the connection keeps flowing and is not
    dropped as idle. On a streaming surface (CLI/TUI) the loop wires a callback
    via ``set_stream_callback``: progress streams live and the finished answer is
    delivered to the user directly, so the tool returns only a compact receipt
    and the main model does not re-emit (and thus cannot rewrite) the answer.
    Without a callback (e.g. a channel) it returns the full structured result.
    """

    name = "deep_research"
    description = (
        "Delegate a question that needs broad web search and multi-source "
        "cross-checking to an external deep-research engine (MiroThinker). "
        "Blocks for minutes. Returns a FINISHED, self-contained answer with its "
        "own inline citations and a References section. Relay it to the user "
        "as-is; do NOT rewrite, re-summarize, or run extra web_search after it "
        "(that wastes tokens and can corrupt its citations). If it reports a "
        "non-ok status, re-run with a sharper query or report the failure. Use "
        "for open-ended research; for a single quick lookup use web_search."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The research question"},
        },
        "required": ["query"],
    }

    # Registry ceiling: raise well above the default so a legitimate minute-scale
    # run isn't timer-killed. The inner httpx read timeout (below) stays under
    # this so the tool, not the ceiling, owns the timeout path.
    timeout_seconds = 900.0
    # Idle read timeout between SSE chunks: research streams chunks continuously,
    # so a gap this long means the stream is dead. Well under timeout_seconds.
    _READ_TIMEOUT_S = 180.0

    def __init__(
        self,
        config: DeepResearchToolConfig,
        workspace: Path,
        proxy: str | None = None,
        manager: "DeepResearchManager | None" = None,
    ):
        self._config = config
        self._workspace = Path(workspace)
        self._proxy = proxy
        # Background manager for the async (channel) transport; None on local
        # surfaces. Even when present it only routes async once its submit handle
        # is wired (gateway-only) — see ``DeepResearchManager.can_deliver``.
        self._manager = manager
        # Turn-local so a user turn and a concurrent proactive turn cannot clobber
        # each other's routing (same reason MessageTool keeps its callback here).
        self._stream_cb: ContextVar[StreamCallback | None] = ContextVar("deep_research_stream_cb", default=None)
        self._routing: ContextVar[_Routing | None] = ContextVar("deep_research_routing", default=None)

    @staticmethod
    def is_configured(config: DeepResearchToolConfig) -> bool:
        """Whether a key is reachable, so the loop can register the tool opt-in."""
        return bool(config.api_key or os.environ.get("MIROTHINKER_API_KEY"))

    def set_stream_callback(self, cb: StreamCallback | None) -> None:
        """Wire the per-turn stream callback (turn-local). Set only on surfaces
        that render the answer inline (CLI/TUI); left unset elsewhere."""
        self._stream_cb.set(cb)

    def set_context(self, channel: str, chat_id: str, session_key: str) -> None:
        """Set the per-turn delivery routing (turn-local), so the async transport
        knows which conversation to push the finished answer back to."""
        self._routing.set(_Routing(channel=channel, chat_id=chat_id, conversation=session_key))

    def _api_key(self) -> str:
        return self._config.api_key or os.environ.get("MIROTHINKER_API_KEY", "")

    async def execute(self, query: str, **kwargs: Any) -> str:
        key = self._api_key()
        if not key:
            return self._result(
                "error",
                content=(
                    "deep_research: no API key configured. Set it under "
                    "tools.deep_research.apiKey or export MIROTHINKER_API_KEY."
                ),
            )

        cb = self._stream_cb.get()
        # Async (channel) transport: no inline stream callback but a wired
        # manager (gateway-only). Hand off to the background poller, which later
        # delivers the finished answer verbatim via a deliver_text turn.
        if cb is None and self._manager is not None and self._manager.can_deliver():
            if (routing := self._routing.get()) is not None:
                return await self._manager.start(query, routing)

        base = (self._config.api_base or DEFAULT_BASE_URL).rstrip("/")
        model = self._config.model or DEFAULT_MODEL
        payload = {"model": model, "messages": [{"role": "user", "content": query}], "stream": True}

        try:
            logger.debug("deep_research: {} via {}", model, "proxy" if self._proxy else "direct")
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._READ_TIMEOUT_S, connect=10.0), proxy=self._proxy
            ) as client:
                async with client.stream(
                    "POST",
                    f"{base}/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json=payload,
                ) as r:
                    if r.status_code != 200:
                        await r.aread()
                        return self._result("error", content=f"deep_research HTTP {r.status_code}: {_error_message(r)}")
                    content, finish, usage = await self._consume(r, cb)
        except httpx.TimeoutException as e:
            detail = str(e) or "the research engine went quiet"
            return self._result("timeout", content=f"deep_research timed out: {detail}")
        except Exception as e:
            logger.error("deep_research error: {}", e)
            return self._result("error", content=f"deep_research error: {e}")

        status = {"stop": "ok", "cancelled": "timeout"}.get(finish, "error")
        report_ref = self._write_report(content, query) if content else None
        # Streaming surface: deliver the finished answer to the user directly and
        # hand the model a compact receipt, so it relays (a short ack) instead of
        # re-emitting the whole answer. Errors stay on the plain path (they are
        # short, and letting the model relay them is fine).
        if cb is not None and status == "ok" and content:
            await cb("answer", content)
            return self._receipt(status, report_ref)
        return self._result(status, content=content or "(empty response)", report_ref=report_ref, usage=usage)

    @staticmethod
    async def _consume(
        response: httpx.Response, cb: StreamCallback | None
    ) -> tuple[str, str | None, dict[str, Any] | None]:
        """Accumulate delta.content; grab finish_reason/usage from the tail; emit a
        coarse progress line whenever the reasoning-step type changes (if wired)."""
        parts: list[str] = []
        finish: str | None = None
        usage: dict[str, Any] | None = None
        last_step: str | None = None
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                parts.append(delta["content"])
            if cb is not None:
                for step in delta.get("reasoning_steps") or []:
                    kind = step.get("type")
                    # Skip "thinking" (the between-action default) and de-dup
                    # consecutive same-action steps, so progress shows a few
                    # meaningful milestones (search/fetch/exec) instead of the
                    # thinking<->action churn flooding the terminal.
                    if not kind or kind == "thinking" or kind == last_step:
                        continue
                    last_step = kind
                    await cb("progress", _PROGRESS_LABELS.get(kind, kind))
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
            if chunk.get("usage"):
                usage = chunk["usage"]
        return "".join(parts), finish, usage

    def _write_report(self, content: str, query: str) -> str:
        return _write_report_file(self._workspace, content, query)

    @staticmethod
    def _result(
        status: str,
        *,
        content: str,
        report_ref: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> str:
        return json.dumps(
            {"status": status, "content": content, "report_ref": report_ref, "usage": usage},
            ensure_ascii=False,
        )

    @staticmethod
    def _receipt(status: str, report_ref: str | None) -> str:
        return json.dumps(
            {
                "status": status,
                "report_ref": report_ref,
                "delivered": True,
                "note": (
                    "The full answer, with its citations, is ALREADY shown to the user. "
                    "Reply with at most a one-line acknowledgement. Do NOT restate it, and do "
                    "NOT add any facts, figures, or your own summary -- your numbers may "
                    "contradict the researched answer. If it looks off-target, say so and offer to re-run."
                ),
            },
            ensure_ascii=False,
        )


def _error_message(response: httpx.Response) -> str:
    try:
        return (response.json().get("error") or {}).get("message") or response.text[:200]
    except Exception:
        return response.text[:200]


def _write_report_file(workspace: Path, content: str, query: str) -> str:
    out_dir = Path(workspace) / _OUTPUT_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"deep_research-{stamp}-{uuid.uuid4().hex[:8]}.md"
    path.write_text(f"# {query}\n\n{content}\n", encoding="utf-8")
    return str(path)


def _extract_output_text(body: dict[str, Any]) -> str:
    """Pull the answer from a completed Responses payload: concatenate the
    ``output_text`` parts of the ``message`` items. Research runs carry tool
    calls too, so ``output`` can hold several items — never assume index 0."""
    parts: list[str] = []
    for item in body.get("output") or []:
        if item.get("type") != "message":
            continue
        for c in item.get("content") or []:
            if c.get("type") == "output_text" and c.get("text"):
                parts.append(c["text"])
    return "".join(parts)


class DeepResearchManager:
    """Runs a deep-research query in the background and delivers the finished
    answer back to its conversation verbatim.

    Mirrors :class:`SubagentManager`'s shape — a fire-and-forget asyncio task per
    query, an in-flight guard (one research per conversation), and a late-bound
    submit handle (wired gateway-only) used to re-enter the spine. Unlike
    SubagentManager it does NOT re-inject a model turn: the answer is delivered
    as-is through a ``deliver_text`` turn, so the model cannot rewrite it.
    """

    _POLL_INTERVAL_S = 5.0
    # Overall ceiling for one background research, matching the flagship's
    # observed worst case with headroom; the poll loop gives up past this.
    _MAX_POLL_S = 3600.0

    def __init__(self, config: DeepResearchToolConfig, workspace: Path, proxy: str | None = None):
        self._config = config
        self._workspace = Path(workspace)
        self._proxy = proxy
        # Late-bound (gateway wires it after building the scheduler). Until then
        # can_deliver() is False and the tool falls back to a synchronous path.
        self._submit: Callable[[Any], Any] | None = None
        # In-flight guard: conversation -> poll task. One research per
        # conversation; released by the task's done-callback (fires on success,
        # error, OR cancellation), so a crash can never wedge the guard.
        self._active: dict[str, asyncio.Task[None]] = {}

    def set_submit(self, submit: Callable[[Any], Any]) -> None:
        self._submit = submit

    def can_deliver(self) -> bool:
        """Whether async delivery is wired (gateway only). The tool checks this
        to decide between the async transport and the synchronous fallback."""
        return self._submit is not None

    async def start(self, query: str, routing: _Routing) -> str:
        """Submit the research, spawn the background poller, return an ack the
        model relays. Refuses if the conversation already has one running; on a
        submit failure returns an error receipt and starts nothing."""
        if (existing := self._active.get(routing.conversation)) is not None and not existing.done():
            return json.dumps(
                {
                    "status": "busy",
                    "note": (
                        "A deep research is already running for this conversation. Tell the user to "
                        "wait for it to finish before starting another; do not start a second one."
                    ),
                },
                ensure_ascii=False,
            )
        try:
            resp_id = await self._submit_research(query)
        except Exception as e:
            logger.error("deep_research async submit failed: {}", e)
            return json.dumps(
                {"status": "error", "note": f"deep_research could not start: {e}. Tell the user it failed to start."},
                ensure_ascii=False,
            )

        task = asyncio.create_task(self._run(resp_id, query, routing))
        self._active[routing.conversation] = task
        task.add_done_callback(lambda _: self._active.pop(routing.conversation, None))
        logger.info("deep_research async started [{}] for {}", resp_id, routing.conversation)
        return json.dumps(
            {
                "status": "started",
                "note": (
                    "Deep research has started in the background. Tell the user it is underway and the "
                    "result will arrive on its own when ready. Do NOT attempt the research yourself and "
                    "do NOT call deep_research again for this."
                ),
            },
            ensure_ascii=False,
        )

    async def _run(self, resp_id: str, query: str, routing: _Routing) -> None:
        """Poll to completion, then deliver the answer verbatim. Any failure
        becomes an error delivery; the guard is released by the done-callback
        regardless, and submit errors here are logged, never propagated."""
        try:
            answer = await self._poll(resp_id)
        except asyncio.CancelledError:
            raise  # teardown (e.g. session cancel) — no delivery; guard freed by callback
        except Exception as e:
            logger.error("deep_research [{}] failed: {}", resp_id, e)
            text = f"Deep research failed: {e}"
        else:
            text = answer or "Deep research finished but returned no content."
            if answer:
                # Saving the report is best-effort: a write failure (disk/perms)
                # must not lose the answer we already have, so log, don't raise.
                try:
                    logger.info(
                        "deep_research [{}] report saved: {}",
                        resp_id,
                        _write_report_file(self._workspace, answer, query),
                    )
                except Exception as e:
                    logger.error("deep_research [{}] report save failed: {}", resp_id, e)
        try:
            self._deliver(routing, text)
        except Exception as e:
            logger.error("deep_research [{}] delivery failed: {}", resp_id, e)

    def _deliver(self, routing: _Routing, text: str) -> None:
        from raven.spine import ChatType, Origin, Source, TurnRequest

        # _submit is guaranteed set: _deliver is only reached from a task that
        # start() spawned, and start() runs only when can_deliver() was true.
        self._submit(
            TurnRequest(
                origin=Origin.SUBAGENT,
                source=Source(
                    channel=routing.channel,
                    chat_id=routing.chat_id,
                    sender_id="deep_research",
                    chat_type=ChatType.DM,
                ),
                text="",
                conversation=routing.conversation,
                deliver_text=text,
            )
        )

    def _headers(self) -> dict[str, str]:
        key = self._config.api_key or os.environ.get("MIROTHINKER_API_KEY", "")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _base(self) -> str:
        return (self._config.api_base or DEFAULT_BASE_URL).rstrip("/")

    async def _submit_research(self, query: str) -> str:
        model = self._config.model or DEFAULT_MODEL
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), proxy=self._proxy) as client:
            r = await client.post(
                f"{self._base()}/responses",
                headers=self._headers(),
                json={"model": model, "input": query, "background": True, "stream": False},
            )
            if r.status_code not in (200, 202):
                raise RuntimeError(f"HTTP {r.status_code}: {_error_message(r)}")
            resp_id = r.json().get("id")
            if not resp_id:
                raise RuntimeError("no response id returned")
            return resp_id

    async def _poll(self, resp_id: str) -> str:
        waited = 0.0
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), proxy=self._proxy) as client:
            while waited < self._MAX_POLL_S:
                r = await client.get(f"{self._base()}/responses/{resp_id}", headers=self._headers())
                r.raise_for_status()
                body = r.json()
                status = body.get("status")
                if status == "completed":
                    return _extract_output_text(body)
                if status in ("failed", "cancelled"):
                    raise RuntimeError(f"research {status}")
                await asyncio.sleep(self._POLL_INTERVAL_S)
                waited += self._POLL_INTERVAL_S
        raise RuntimeError("research timed out")
