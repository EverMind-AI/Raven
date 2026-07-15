"""Deep research tool: delegate a question to the MiroThinker API over SSE."""

import datetime
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
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

    def __init__(self, config: DeepResearchToolConfig, workspace: Path, proxy: str | None = None):
        self._config = config
        self._workspace = Path(workspace)
        self._proxy = proxy
        # Turn-local so a user turn and a concurrent proactive turn cannot clobber
        # each other's routing (same reason MessageTool keeps its callback here).
        self._stream_cb: ContextVar[StreamCallback | None] = ContextVar("deep_research_stream_cb", default=None)

    @staticmethod
    def is_configured(config: DeepResearchToolConfig) -> bool:
        """Whether a key is reachable, so the loop can register the tool opt-in."""
        return bool(config.api_key or os.environ.get("MIROTHINKER_API_KEY"))

    def set_stream_callback(self, cb: StreamCallback | None) -> None:
        """Wire the per-turn stream callback (turn-local). Set only on surfaces
        that render the answer inline (CLI/TUI); left unset elsewhere."""
        self._stream_cb.set(cb)

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
                        return self._result(
                            "error", content=f"deep_research HTTP {r.status_code}: {_error_message(r)}"
                        )
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
        out_dir = self._workspace / _OUTPUT_SUBDIR
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = out_dir / f"deep_research-{stamp}-{uuid.uuid4().hex[:8]}.md"
        path.write_text(f"# {query}\n\n{content}\n", encoding="utf-8")
        return str(path)

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
