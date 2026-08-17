"""DeepSeek-V4 official-encoding provider (raw text completions).

DeepSeek-V4 ships no chat template: its official harness encodes the
conversation to a raw prompt string (DSML tool calling, ``<think>`` blocks,
reasoning-effort text prefix) and parses the completion text back. A generic
OpenAI chat gateway cannot reproduce that encoding — tool schemas and effort
never reach the model in the format it was trained on. This provider bypasses
the chat endpoint entirely: encode via the vendored official reference
implementation, POST to ``{api_base}/completions``, parse the raw text.
"""

import asyncio
import json
import uuid
from typing import Any

from loguru import logger

from raven.providers import _dsv4_encoding as enc
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest

# Prompt-level effort accepts only low/high/max; map the OpenAI-style enum.
_EFFORT_MAP = {
    "none": "low",
    "low": "low",
    "minimal": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "max",
    "max": "max",
}


def build_dsv4_messages(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Normalize an OpenAI-style history into encode_messages() input.

    Tools ride on the first system message (the official encoding renders the
    DSML schema block there); a synthetic system message is created when the
    history has none. Content that arrived as block lists is flattened to
    text, since the raw prompt has no notion of typed blocks.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        m = dict(msg)
        content = m.get("content")
        if isinstance(content, list):
            m["content"] = "\n\n".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") in ("text", "input_text")
            )
        if m.get("role") == "assistant" and m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                fn = dict(tc.get("function") or {})
                args = fn.get("arguments")
                if not isinstance(args, str):
                    args = enc.to_json(args or {})
                calls.append({"id": tc.get("id", ""), "function": {"name": fn.get("name", ""), "arguments": args}})
            m["tool_calls"] = calls
        out.append(m)

    if tools:
        if out and out[0].get("role") == "system":
            out[0] = {**out[0], "tools": tools}
        else:
            out.insert(0, {"role": "system", "content": "", "tools": tools})
    return out


def parse_completion(text: str, reasoning_pre_split: bool = False) -> dict[str, Any]:
    """Parse a completion, degrading gracefully on malformed output.

    ``reasoning_pre_split=True`` means the gateway already extracted the
    ``<think>`` block into a separate field (OpenRouter does this on
    /completions), so ``text`` starts directly at the content — parse it in
    chat mode. The official parser raises on any deviation; a benchmark turn
    must never die on that, so fall back to splitting off whatever reasoning
    is present and, when the DSML region itself is broken, return the text as
    plain content so the agent loop can continue.
    """
    t = text
    if enc.eos_token not in t:
        t += enc.eos_token
    try:
        return enc.parse_message_from_completion_text(t, thinking_mode="chat" if reasoning_pre_split else "thinking")
    except (ValueError, AssertionError) as err:
        logger.warning("dsv4 strict parse failed ({}), using lenient fallback", err)

    reasoning, sep, rest = t.partition(enc.thinking_end_token)
    if not sep:
        reasoning, rest = "", t
    rest = rest.replace(enc.eos_token, "")
    marker = f"<{enc.dsml_token}{enc.tool_calls_block_name}"
    content, matched, dsml_part = rest.partition(marker)
    tool_calls: list[dict[str, Any]] = []
    if matched:
        # dsml_part starts with the block opener's ">\n" — exactly where
        # parse_tool_calls expects to begin scanning.
        try:
            _, _, tool_calls = enc.parse_tool_calls(0, dsml_part)
        except Exception:
            tool_calls = []
    return {
        "role": "assistant",
        "content": content.strip("\n"),
        "reasoning_content": reasoning,
        "tool_calls": enc.tool_calls_to_openai_format(tool_calls),
    }


class DeepSeekV4RawProvider(LLMProvider):
    """Serve DeepSeek-V4 through its official prompt encoding."""

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "deepseek/deepseek-v4-flash-0731",
        extra_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        # Body extras merged into every /completions request (top_p, provider
        # routing pins, ...). "reasoning" is excluded: effort is realized as a
        # text prefix inside the encoded prompt, and sending the chat-mode
        # param too would double-apply it.
        self.extra_body = {k: v for k, v in (extra_body or {}).items() if k != "reasoning"}

    def get_default_model(self) -> str:
        return self.default_model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        effort = _EFFORT_MAP.get((reasoning_effort or "low").lower(), "low")
        dsv4_messages = build_dsv4_messages(self._sanitize_empty_content(messages), tools)
        try:
            prompt = enc.encode_messages(dsv4_messages, thinking_mode="thinking", reasoning_effort=effort)
        except Exception as e:
            return LLMResponse(
                content=f"Error calling LLM: dsv4 encoding failed: {e}",
                finish_reason="error",
                error_classification=self.classify_error(e),
            )

        body: dict[str, Any] = {
            "model": model or self.default_model,
            "prompt": prompt,
            "max_tokens": max(1, max_tokens),
            "temperature": temperature,
            "stream": False,
            **self.extra_body,
        }
        try:
            data = await asyncio.wait_for(self._post_completions(body), self.generation.timeout)
        except Exception as e:
            return LLMResponse(
                content=f"Error calling LLM: {str(e) or type(e).__name__}",
                finish_reason="error",
                error_classification=self.classify_error(e),
            )

        try:
            choice = data["choices"][0]
            completion_text = choice.get("text") or ""
            gateway_reasoning = choice.get("reasoning") or None
            finish_reason = choice.get("finish_reason") or "stop"
        except (KeyError, IndexError, TypeError) as e:
            err = data.get("error") if isinstance(data, dict) else None
            msg = f"Error calling LLM: malformed completions response: {err or data}"
            return LLMResponse(
                content=msg,
                finish_reason="error",
                error_classification=self.classify_error(e, msg),
            )

        parsed = parse_completion(completion_text, reasoning_pre_split=gateway_reasoning is not None)
        tool_calls: list[ToolCallRequest] = []
        for tc in parsed["tool_calls"]:
            raw_args = tc["function"]["arguments"]
            try:
                args = json.loads(raw_args)
                if not isinstance(args, dict):
                    args = {"value": args}
            except (json.JSONDecodeError, TypeError):
                args = {"arguments": raw_args}
            tool_calls.append(
                ToolCallRequest(
                    id=f"dsml{uuid.uuid4().hex[:9]}",
                    name=tc["function"]["name"],
                    arguments=args,
                )
            )

        return LLMResponse(
            content=parsed["content"] or None,
            tool_calls=tool_calls,
            finish_reason="length" if finish_reason == "length" else "stop",
            usage=data.get("usage") or {},
            reasoning_content=gateway_reasoning or parsed["reasoning_content"] or None,
        )

    async def _post_completions(self, body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        url = (self.api_base or "https://openrouter.ai/api/v1").rstrip("/") + "/completions"
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient(timeout=self.generation.timeout) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
