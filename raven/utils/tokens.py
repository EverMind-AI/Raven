"""Prompt token estimation: a provider's own counter first, tiktoken as the fallback.

``tiktoken`` is imported here and nowhere else in ``raven.utils``, so a leaf that
wants a safe filename does not pay for an encoding table.
"""

import contextlib
import json
import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import tiktoken

from raven.utils.images import estimate_content_part_tokens

# A cl100k regex piece cannot cross a newline followed by non-whitespace.
# These boundaries preserve full-prompt tokenization, unlike message boundaries.
_ENCODING_NAME = "cl100k_base"
_TOKEN_BOUNDARY = re.compile(r"(?<=[\r\n])(?=\S)")

# tiktoken's merge loop is quadratic in the length of one regex piece, and an
# unbroken run of one character (padding, a row of ``=``, base64 of zeros, a
# page of blank lines) is one piece: 200,000 of them hold the event loop for
# about twenty seconds. A run of space or non-space longer than this is counted
# in slices of it, which moves the count by a token or two a slice, either way.
# Space is cl100k's: Python's ``\s`` also takes U+001C..U+001F, which cl100k
# glues to the text around them, so those four go with the non-space runs.
_RUN_SLICE = 1_000
_RUN = re.compile(r"[^\S\x1c-\x1f]+|[\S\x1c-\x1f]+")


def encoding() -> tiktoken.Encoding:
    """The cl100k encoding the estimator counts with, without the Python copy of its ranks.

    tiktoken's registered encoding keeps that copy beside the one its core
    encodes with and reads it only to pickle an encoding outside the registry --
    a registered one pickles by name -- so it is dropped once loaded, about
    20 MiB. LiteLLM's import drops the same copy (``litellm_setup``); this drops
    it in a process that never imports LiteLLM.
    """
    enc = tiktoken.get_encoding(_ENCODING_NAME)
    if hasattr(enc, "_mergeable_ranks"):
        with contextlib.suppress(AttributeError):
            del enc._mergeable_ranks
    return enc


def _encoded_len(encoding: tiktoken.Encoding, text: str) -> int:
    """``len(encoding.encode(text))``, with any run past ``_RUN_SLICE`` counted in slices."""
    if len(text) <= _RUN_SLICE:
        return len(encoding.encode(text))
    long_runs = [m for m in _RUN.finditer(text) if m.end() - m.start() > _RUN_SLICE]
    if not long_runs:
        return len(encoding.encode(text))
    total = 0
    pos = 0
    for m in long_runs:
        if m.start() > pos:
            total += len(encoding.encode(text[pos : m.start()]))
        total += sum(
            len(encoding.encode(text[i : min(i + _RUN_SLICE, m.end())])) for i in range(m.start(), m.end(), _RUN_SLICE)
        )
        pos = m.end()
    if pos < len(text):
        total += len(encoding.encode(text[pos:]))
    return total


class _PromptTokenCache:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.costs: dict[str, int] = {}
        self.total = 0
        self.initial_payload: str | None = None

    def clear(self) -> None:
        self.counts.clear()
        self.costs.clear()
        self.total = 0
        self.initial_payload = None

    def count(self, payload: str, encoding: tiktoken.Encoding) -> int:
        if not self.counts:
            if self.initial_payload is None:
                self.total = _encoded_len(encoding, payload)
                self.initial_payload = payload
                return self.total
            if payload == self.initial_payload:
                return self.total
            self.initial_payload = None
            self.total = 0
        counts = Counter(_TOKEN_BOUNDARY.split(payload))
        costs: dict[str, int] = {}
        total = self.total
        for part, count in self.counts.items():
            total -= max(0, count - counts[part]) * self.costs[part]
        for part, count in counts.items():
            cost = self.costs.get(part)
            if cost is None:
                cost = _encoded_len(encoding, part)
            total += max(0, count - self.counts[part]) * cost
            costs[part] = cost
        self.counts, self.costs, self.total = counts, costs, total
        return total


_prompt_cache: ContextVar[_PromptTokenCache | None] = ContextVar("prompt_token_cache", default=None)


@contextmanager
def reuse_prompt_tokenization() -> Iterator[None]:
    """Count the first prompt whole; index fragments only when a trim changes it."""
    token = _prompt_cache.set(_PromptTokenCache())
    try:
        yield
    finally:
        _prompt_cache.reset(token)


def estimate_prompt_tokens(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> int:
    """Estimate prompt tokens with tiktoken."""
    parts: list[str] = []
    extra_tokens = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    txt = part.get("text", "")
                    if txt:
                        parts.append(txt)
                elif (image_tokens := estimate_content_part_tokens(part)) is not None:
                    extra_tokens += image_tokens
                else:
                    parts.append(json.dumps(part, ensure_ascii=False))
        elif content is not None:
            parts.append(json.dumps(content, ensure_ascii=False))

        for key in ("name", "tool_call_id"):
            value = msg.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
        if msg.get("tool_calls"):
            parts.append(json.dumps(msg["tool_calls"], ensure_ascii=False))
        reasoning = msg.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            parts.append(reasoning)
        if msg.get("thinking_blocks"):
            parts.append(json.dumps(msg["thinking_blocks"], ensure_ascii=False))

    if tools:
        parts.append(json.dumps(tools, ensure_ascii=False))

    payload = "\n".join(parts)
    cache = _prompt_cache.get()
    if not payload:
        if cache is not None:
            cache.clear()
        return extra_tokens
    try:
        enc = encoding()
        text_tokens = _encoded_len(enc, payload) if cache is None else cache.count(payload, enc)
    except Exception:
        if cache is not None:
            cache.clear()
        text_tokens = len(payload) // 4
    return max(1, text_tokens + extra_tokens)


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate prompt tokens contributed by one persisted message."""
    content = message.get("content")
    parts: list[str] = []
    extra_tokens = 0
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text", "")
                if text:
                    parts.append(text)
            elif (image_tokens := estimate_content_part_tokens(part)) is not None:
                extra_tokens += image_tokens
            else:
                parts.append(json.dumps(part, ensure_ascii=False))
    elif content is not None:
        parts.append(json.dumps(content, ensure_ascii=False))

    for key in ("name", "tool_call_id"):
        value = message.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    if message.get("tool_calls"):
        parts.append(json.dumps(message["tool_calls"], ensure_ascii=False))
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        parts.append(reasoning)
    if message.get("thinking_blocks"):
        parts.append(json.dumps(message["thinking_blocks"], ensure_ascii=False))

    payload = "\n".join(parts)
    if not payload:
        return max(1, extra_tokens)
    try:
        enc = encoding()
        text_tokens = _encoded_len(enc, payload)
    except Exception:
        text_tokens = len(payload) // 4
    return max(1, text_tokens + extra_tokens)


def estimate_prompt_tokens_chain(
    provider: Any,
    model: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> tuple[int, str]:
    """Estimate prompt tokens via provider counter first, then tiktoken fallback."""
    provider_counter = getattr(provider, "estimate_prompt_tokens", None)
    if callable(provider_counter):
        try:
            tokens, source = provider_counter(messages, tools, model)
            if isinstance(tokens, (int, float)) and tokens > 0:
                return int(tokens), str(source or "provider_counter")
        except Exception:
            pass

    estimated = estimate_prompt_tokens(messages, tools)
    if estimated > 0:
        return int(estimated), "tiktoken"
    return 0, "none"


__all__ = [
    "estimate_message_tokens",
    "estimate_prompt_tokens",
    "estimate_prompt_tokens_chain",
    "reuse_prompt_tokenization",
]
