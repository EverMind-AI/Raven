"""Synchronous OpenAI-compatible ``call_fn`` for the semantic-node layer.

The driver models are served behind OpenAI-compatible ``/v1`` endpoints
(self-hosted Qwen / Kimi via vLLM). :func:`make_call_fn` returns the sync
``CallFn`` a :class:`~raven.evolver.orchestrator.nodes.semantic.SemanticNode`
expects — messages in, assistant text out.

Two behaviours matter for these specific models:

- **Reasoning models.** Qwen3.5/3.6 emit a large ``reasoning`` field before the
  real answer lands in ``message.content``. A small ``max_tokens`` gets consumed
  entirely by reasoning and returns ``content == null``. So the default token
  budget here is deliberately generous, and an empty/None content is retried
  rather than parsed.
- **No key required.** vLLM accepts any bearer token; ``api_key`` defaults to
  ``"EMPTY"`` and can be overridden from the environment.

This is a plain sync transport (httpx) so it composes with the synchronous
orchestrator FSM without threading an event loop through it.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from raven.evolver.orchestrator.nodes.semantic import CallFn, Messages


class EndpointError(RuntimeError):
    """Raised when the endpoint fails to return usable content after retries."""


def _extract_content(data: dict) -> Optional[str]:
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None


def make_call_fn(
    *,
    base_url: str,
    model: str,
    api_key: Optional[str] = None,
    api_key_env: str = "EVOLVER_DRIVER_API_KEY",
    max_tokens: int = 8192,
    temperature: float = 0.0,
    timeout: float = 180.0,
    retry_delays: tuple[float, ...] = (1.0, 2.0, 4.0),
) -> CallFn:
    """Build a sync ``call_fn`` bound to one OpenAI-compatible chat endpoint.

    ``base_url`` is the ``/v1`` root; ``/chat/completions`` is appended. Empty or
    missing content is retried with backoff (reasoning models occasionally emit
    no answer); after the final attempt an :class:`EndpointError` is raised so a
    node failure is loud rather than a silent empty string.
    """
    key = api_key or os.environ.get(api_key_env) or "EMPTY"
    url = base_url.rstrip("/") + "/chat/completions"

    def call_fn(messages: Messages) -> str:
        import httpx  # lazy: unit tests inject their own call_fn

        payload = {
            "model": model,
            "messages": list(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        last_exc: Exception | None = None
        for delay in retry_delays:
            try:
                with httpx.Client(timeout=timeout) as client:
                    resp = client.post(url, json=payload, headers=headers)
                    resp.raise_for_status()
                    content = _extract_content(resp.json())
                if content and content.strip():
                    return content
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
            time.sleep(delay)

        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            content = _extract_content(resp.json())
        if content and content.strip():
            return content
        raise EndpointError(
            f"endpoint {model!r} returned empty content after "
            f"{len(retry_delays) + 1} attempts" + (f"; last exc: {last_exc!r}" if last_exc else "")
        )

    return call_fn


__all__ = ["make_call_fn", "EndpointError"]
