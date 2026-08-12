"""Third-party OpenAI-compatible HTTP agent backend (mirothinker, …) as a
spawned sub-agent (req5).

v1 is stateless: one Chat Completions call per spawn (no client-side history
replay yet). The API key lives in config; the raw JSON is read directly so
provider extension fields survive.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from loguru import logger

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider


class OpenAIApiBackend:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        model: str,
        api_key: str = "",
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: int | None = None,
        max_output_chars: int = 128000,
    ) -> None:
        self.name = name
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_output_chars = max_output_chars

    async def run(
        self,
        task: str,
        *,
        task_id: str,
        workspace: Path,
        executor: Any,
        session_key: str | None = None,
        instance: str | None = None,
        provider: LLMProvider | None = None,
        model: str | None = None,
    ) -> str:
        # The parent's provider/model are accepted and ignored: this backend
        # posts to its own configured endpoint under its own ``self.model``.
        url = self.base_url.rstrip("/") + "/chat/completions"
        messages: list[dict[str, Any]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": task})
        # Explicit opt-out: a provider whose default is SSE (mirothinker) answers an
        # omitted `stream` with text/event-stream, which `resp.json()` cannot read.
        body: dict[str, Any] = {"model": self.model, "messages": messages, "stream": False}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        logger.info("Subagent [{}] OpenAI-API agent {!r}: {}", task_id, self.name, self.model)
        # aiohttp treats both a bare `timeout=None` and an omitted `timeout` as
        # "use aiohttp's own 5-minute default", so an explicit ClientTimeout
        # instance with total=None is the only way to mean "no automatic cap".
        # `total` stays unbounded as intended, but `connect` is still capped:
        # it covers both DNS resolution and the socket connect (`sock_connect`
        # alone would leave a hung resolver unbounded), so a stall before the
        # request is even sent can't hang forever -- a different failure than
        # a slow response body, which `total=None` still allows.
        client_timeout = aiohttp.ClientTimeout(total=self.timeout, connect=30)
        # `trust_env` also reads HTTP_PROXY/HTTPS_PROXY/NO_PROXY, which aiohttp
        # otherwise ignores -- unlike httpx (the rest of the codebase), which honors
        # them by default. On a host that can only reach the provider through a
        # proxy, a direct connection is not a connection error but whatever the
        # provider says to an unexpected origin (mirothinker: HTTP 451).
        async with aiohttp.ClientSession(timeout=client_timeout, trust_env=True) as session:
            async with session.post(url, json=body, headers=headers) as resp:
                if resp.status != 200:
                    text = (await resp.text())[:2000]
                    raise RuntimeError(f"OpenAI-API agent {self.name!r} HTTP {resp.status}: {text}")
                data = await resp.json()

        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"OpenAI-API agent {self.name!r}: unexpected response shape") from exc
        content = message.get("content")
        if not content:
            # Some reasoning models put the answer only in a reasoning field.
            content = message.get("reasoning_content") or message.get("reasoning") or ""
        return str(content).strip()[: self.max_output_chars]
