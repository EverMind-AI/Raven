"""Proposers: decide which trial configs to run next.

A proposer turns the campaign's results so far into the next batch of trials.
``GridProposer`` is mechanical (a fixed grid, proposed once). ``LLMProposer``
delegates the judgment -- reading the score surface and proposing where to look
next -- to a language model through an injected ``complete(prompt) -> text``
callable, so the proposer is unit-testable with a fake model and, in production,
wired to Raven's configured provider. The LLM only *proposes* configs; scoring
stays deterministic in the trial, keeping the reward verifiable.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from raven.ops.campaign import Trial

Completion = Callable[[str], Awaitable[str]]


def config_key(config: dict[str, Any]) -> str:
    """Stable, id-safe key for a config dict (also the container/idempotency key).

    A config with no entries is a real round -- the change can already be in the
    case on the box, leaving nothing to pass -- but it used to join to the empty
    string, and the empty string is not a name. Measured 2026-08-12: ``jobs/{key}``
    collapsed to the jobs root, the ledger filed the record under ``""``, and the
    handle read ``ops-``. One job hid it; a second would have overwritten the
    first in place.

    Two empty configs still share this key on purpose. They ARE the same config,
    and re-submitting the same config must not start a second job.
    """
    if not config:
        # Not a key any config can also produce: a real entry always contributes
        # its name, and no name is empty.
        return "noparams"
    parts = [f"{k}{_short_value(config[k])}" for k in sorted(config)]
    body = "_".join(parts).replace(".", "p").replace("-", "m")
    if len(body) <= _KEY_CHARS:
        return body
    # Still too long even with every value folded. Keep the readable head and let
    # a digest of the whole carry the identity.
    return body[:_KEY_CHARS] + "__" + _digest(body)


# Room for the readable part while leaving most of NAME_MAX (255) free -- an
# apparatus digest is appended downstream, and a name with no headroom fails at
# mkdir rather than anywhere a reader would look.
_KEY_CHARS = 64
_VALUE_CHARS = 12


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def _short_value(value: Any) -> str:
    """A value as it appears in a name: itself when short, a digest when not.

    A path is the case that forced this. Spelled out, its separators turn one
    trial name into seven nested directories -- which hid a running job from a
    spend probe that globs one level -- and its length left the whole key at
    exactly NAME_MAX, so any config written slightly longer would not start.
    """
    text = str(value)
    if "/" in text or len(text) > _VALUE_CHARS:
        return "h" + _digest(text)
    return text


def legacy_config_key(config: dict[str, Any]) -> str:
    """The spelling used before values were folded.

    Campaigns started under it hold their records under these names, and a resume
    has to find them: a key that no longer matches reads as a trial that never
    ran, and re-running it spends the compute again.
    """
    parts = [f"{k}{config[k]}" for k in sorted(config)]
    if not parts:
        return "noparams"
    return "_".join(parts).replace(".", "p").replace("-", "m")


def _trial(config: dict[str, Any]) -> Trial:
    return Trial(config_key(config), dict(config))


class Proposer(ABC):
    @abstractmethod
    async def propose(self, history: list[dict[str, Any]], round_index: int) -> list[Trial]:
        """Given prior results (each ``{"config": ..., "score": float}``) and the
        round number, return the next batch of trials, or [] to stop."""


class GridProposer(Proposer):
    def __init__(self, grid: list[dict[str, Any]]) -> None:
        self._grid = [dict(c) for c in grid]

    async def propose(self, history: list[dict[str, Any]], round_index: int) -> list[Trial]:
        return [_trial(c) for c in self._grid] if round_index == 0 else []


class LLMProposer(Proposer):
    def __init__(
        self,
        complete: Completion,
        *,
        objective: str,
        seed: list[dict[str, Any]],
        batch_size: int = 3,
        max_rounds: int = 4,
    ) -> None:
        self._complete = complete
        self._objective = objective
        self._seed = [dict(c) for c in seed]
        self._batch_size = batch_size
        self._max_rounds = max_rounds

    async def propose(self, history: list[dict[str, Any]], round_index: int) -> list[Trial]:
        if round_index == 0:
            return [_trial(c) for c in self._seed]
        if round_index >= self._max_rounds:
            return []
        text = await self._complete(self._build_prompt(history))
        tried = {config_key(h["config"]) for h in history}
        fresh: list[Trial] = []
        seen: set[str] = set()
        for cfg in _extract_configs(text):
            key = config_key(cfg)
            if key in tried or key in seen:
                continue
            seen.add(key)
            fresh.append(_trial(cfg))
            if len(fresh) >= self._batch_size:
                break
        return fresh

    def _build_prompt(self, history: list[dict[str, Any]]) -> str:
        ranked = sorted(history, key=lambda h: h["score"], reverse=True)
        lines = [f"  {json.dumps(h['config'])} -> {h['score']}" for h in ranked]
        return (
            f"{self._objective}\n\n"
            f"Configs tried so far (best first):\n" + "\n".join(lines) + "\n\n"
            f"Propose up to {self._batch_size} new configs to try next, exploring promising "
            f"regions and not repeating tried ones. Reply with ONLY a JSON array of objects, "
            f'e.g. [{{"k1": 1.6, "b": 0.4}}]. Reply [] if further tuning is unlikely to help.'
        )


def make_raven_completer(
    provider: Any,
    *,
    model: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.2,
) -> Completion:
    """Adapt a Raven LLM provider (anything with ``async chat``) into a Completion.

    Keeps LLMProposer decoupled from the provider layer: the caller builds the
    configured provider once and passes it here. Low temperature by default since
    the model is proposing configs, not writing prose.
    """

    async def complete(prompt: str) -> str:
        resp = await provider.chat(
            [{"role": "user", "content": prompt}],
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return resp.content or ""

    return complete


def make_openai_completer(
    base_url: str,
    model: str,
    *,
    api_key: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.2,
    enable_thinking: bool | None = False,
    timeout: float = 90.0,
) -> Completion:
    """A Completion that calls an OpenAI-compatible ``/chat/completions`` endpoint.

    For self-deployed gateways (e.g. VolcEngine Qwen/GLM): bypasses the HTTP proxy
    (``trust_env=False``) since those hosts must be reached directly, and defaults
    ``enable_thinking=False`` because Qwen3.6 otherwise spends the token budget on
    hidden thinking and returns empty content. Set ``enable_thinking=None`` to omit
    the flag for models that don't support it.
    """
    import httpx

    async def complete(prompt: str) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if enable_thinking is not None:
            payload["chat_template_kwargs"] = {"enable_thinking": enable_thinking}
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
            resp = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"].get("content") or ""

    return complete


def _extract_configs(text: str) -> list[dict[str, Any]]:
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return [c for c in data if isinstance(c, dict) and c]
