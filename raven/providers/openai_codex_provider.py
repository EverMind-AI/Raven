"""OpenAI Codex Responses Provider.

LiteLLM can reach this backend -- its ChatGPT driver owns the credential raven
signs in with, and a chat-completions call for one of these models is bridged to
the Responses API rather than posted to a chat endpoint the backend does not
serve. What it cannot do yet is send the whole request: its Responses
transformation filters the body through an allow-list of eleven keys, dropping
``parallel_tool_calls`` and ``text`` among them. An upstream fix exists but has
not reached the released code.

So the request stays here while the credential does not. ``test_openai_codex_provider``
holds that allow-list from the installed LiteLLM: when it stops dropping what
this provider sends, this file is what should be deleted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, AsyncGenerator

import httpx
from loguru import logger

from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest

DEFAULT_CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_ORIGINATOR = "raven"


class OpenAICodexProvider(LLMProvider):
    """Use Codex OAuth to call the Responses API."""

    def __init__(self, default_model: str = "openai-codex/gpt-5.1-codex"):
        super().__init__(api_key=None, api_base=None)
        self.default_model = default_model

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
        model = model or self.default_model
        system_prompt, input_items = _convert_messages(messages)

        from raven.providers.chatgpt_token import access_token_and_account

        # Refreshing can block, and the credential belongs to LiteLLM's driver.
        access, account_id = await asyncio.to_thread(access_token_and_account)
        headers = _build_headers(account_id, access)

        body: dict[str, Any] = {
            "model": _strip_model_prefix(model),
            "store": False,
            "stream": True,
            "instructions": system_prompt,
            "input": input_items,
            "text": {"verbosity": "medium"},
            "include": ["reasoning.encrypted_content"],
            "tool_choice": tool_choice or "auto",
            "parallel_tool_calls": True,
        }

        # Nothing to group without instructions: every such request would share
        # one key while sharing no prefix.
        if system_prompt:
            body["prompt_cache_key"] = _prompt_cache_key(system_prompt)

        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}

        if tools:
            body["tools"] = _convert_tools(tools)

        url = DEFAULT_CODEX_URL

        timeout = self.generation.timeout
        try:
            try:
                content, tool_calls, finish_reason = await _request_codex(
                    url, headers, body, verify=True, timeout=timeout
                )
            except Exception as e:
                if "CERTIFICATE_VERIFY_FAILED" not in str(e):
                    raise
                logger.warning("SSL certificate verification failed for Codex API; retrying with verify=False")
                content, tool_calls, finish_reason = await _request_codex(
                    url, headers, body, verify=False, timeout=timeout
                )
            return LLMResponse(
                content=content,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
            )
        except Exception as e:
            return LLMResponse(
                content=f"Error calling Codex: {str(e)}",
                finish_reason="error",
                error_classification=self.classify_error(e),
            )

    def get_default_model(self) -> str:
        return self.default_model


def _strip_model_prefix(model: str) -> str:
    if model.startswith("openai-codex/") or model.startswith("openai_codex/"):
        return model.split("/", 1)[1]
    return model


def _build_headers(account_id: str, token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": DEFAULT_ORIGINATOR,
        "User-Agent": "raven (python)",
        "accept": "text/event-stream",
        "content-type": "application/json",
    }


async def _request_codex(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    verify: bool,
    timeout: float,
) -> tuple[str, list[ToolCallRequest], str]:
    async with httpx.AsyncClient(timeout=timeout, verify=verify) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            if response.status_code != 200:
                text = await response.aread()
                raise RuntimeError(_friendly_error(response.status_code, text.decode("utf-8", "ignore")))
            return await _consume_sse(response, timeout)


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI function-calling schema to Codex flat format."""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        fn = (tool.get("function") or {}) if tool.get("type") == "function" else tool
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or {}
        converted.append(
            {
                "type": "function",
                "name": name,
                "description": fn.get("description") or "",
                "parameters": params if isinstance(params, dict) else {},
            }
        )
    return converted


def _convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system_prompt = ""
    input_items: list[dict[str, Any]] = []

    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            system_prompt = content if isinstance(content, str) else ""
            continue

        if role == "user":
            input_items.append(_convert_user_message(content))
            continue

        if role == "assistant":
            # Handle text first.
            if isinstance(content, str) and content:
                input_items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                        "status": "completed",
                        "id": f"msg_{idx}",
                    }
                )
            # Then handle tool calls.
            for tool_call in msg.get("tool_calls", []) or []:
                fn = tool_call.get("function") or {}
                call_id, item_id = _split_tool_call_id(tool_call.get("id"))
                call_id = call_id or f"call_{idx}"
                item_id = item_id or f"fc_{idx}"
                input_items.append(
                    {
                        "type": "function_call",
                        "id": item_id,
                        "call_id": call_id,
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments") or "{}",
                    }
                )
            continue

        if role == "tool":
            call_id, _ = _split_tool_call_id(msg.get("tool_call_id"))
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _convert_tool_output(content),
                }
            )
            continue

    return system_prompt, input_items


def _convert_tool_output(content: Any) -> Any:
    """Tool result -> Responses ``function_call_output.output``.

    A plain string passes through. A multimodal block list becomes the array
    form (``input_text`` / ``input_image``), which the Responses API accepts for
    tool output -- unlike Chat Completions, whose ``role:"tool"`` content is
    typed ``string | ChatCompletionContentPartText[]`` and so cannot carry an
    image at all.

    The important part is what this does NOT do: ``json.dumps`` a block list.
    That used to serialize an image's whole base64 payload into the output as
    prose -- the model saw megabytes of gibberish instead of a picture, and
    nothing errored.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)

    texts: list[str] = []
    parts: list[dict[str, Any]] = []
    has_image = False
    for item in content:
        if not isinstance(item, dict):
            texts.append(str(item))
            parts.append({"type": "input_text", "text": str(item)})
            continue
        if item.get("type") == "text":
            text = item.get("text", "")
            texts.append(text)
            parts.append({"type": "input_text", "text": text})
        elif item.get("type") == "image_url":
            url = (item.get("image_url") or {}).get("url")
            if url:
                has_image = True
                parts.append({"type": "input_image", "image_url": url, "detail": "auto"})
        else:
            # Unknown block: serializing it is fine (it carries no base64), but
            # it must still reach the model rather than being dropped.
            blob = json.dumps(item, ensure_ascii=False)
            texts.append(blob)
            parts.append({"type": "input_text", "text": blob})

    if has_image:
        return parts
    # No image to preserve, so keep the simpler string form the API has always
    # accepted rather than gratuitously switching shape.
    return "\n".join(texts)


def _convert_user_message(content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        return {"role": "user", "content": [{"type": "input_text", "text": content}]}
    if isinstance(content, list):
        converted: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                converted.append({"type": "input_text", "text": item.get("text", "")})
            elif item.get("type") == "image_url":
                url = (item.get("image_url") or {}).get("url")
                if url:
                    converted.append({"type": "input_image", "image_url": url, "detail": "auto"})
        if converted:
            return {"role": "user", "content": converted}
    return {"role": "user", "content": [{"type": "input_text", "text": ""}]}


def _split_tool_call_id(tool_call_id: Any) -> tuple[str, str | None]:
    if isinstance(tool_call_id, str) and tool_call_id:
        if "|" in tool_call_id:
            call_id, item_id = tool_call_id.split("|", 1)
            return call_id, item_id or None
        return tool_call_id, None
    return "call_0", None


def _prompt_cache_key(system_prompt: str) -> str:
    """Group requests that share a cached prefix -- which is the instructions.

    Keyed on the whole transcript before, which grows every turn: the key was
    different on every request, so the one thing it exists for -- landing
    requests with a common prefix on the same cache -- never happened.
    """
    return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()


async def _iter_sse(response: httpx.Response, timeout: float) -> AsyncGenerator[dict[str, Any], None]:
    buffer: list[str] = []
    # Per-event idle cap: aiter_lines resets httpx's read timer on every byte,
    # so a trickle/keepalive stall never trips it. wait_for on each line bounds
    # the silence between SSE lines without penalizing a long, progressing run.
    lines = response.aiter_lines()
    while True:
        try:
            line = await asyncio.wait_for(lines.__anext__(), timeout)
        except StopAsyncIteration:
            break
        if line == "":
            if buffer:
                data_lines = [ln[5:].strip() for ln in buffer if ln.startswith("data:")]
                buffer = []
                if not data_lines:
                    continue
                data = "\n".join(data_lines).strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    yield json.loads(data)
                except Exception:
                    continue
            continue
        buffer.append(line)


async def _consume_sse(response: httpx.Response, timeout: float) -> tuple[str, list[ToolCallRequest], str]:
    content = ""
    tool_calls: list[ToolCallRequest] = []
    tool_call_buffers: dict[str, dict[str, Any]] = {}
    finish_reason = "stop"

    async for event in _iter_sse(response, timeout):
        event_type = event.get("type")
        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if not call_id:
                    continue
                tool_call_buffers[call_id] = {
                    "id": item.get("id") or "fc_0",
                    "name": item.get("name"),
                    "arguments": item.get("arguments") or "",
                }
        elif event_type == "response.output_text.delta":
            content += event.get("delta") or ""
        elif event_type == "response.function_call_arguments.delta":
            call_id = event.get("call_id")
            if call_id and call_id in tool_call_buffers:
                tool_call_buffers[call_id]["arguments"] += event.get("delta") or ""
        elif event_type == "response.function_call_arguments.done":
            call_id = event.get("call_id")
            if call_id and call_id in tool_call_buffers:
                tool_call_buffers[call_id]["arguments"] = event.get("arguments") or ""
        elif event_type == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if not call_id:
                    continue
                buf = tool_call_buffers.get(call_id) or {}
                args_raw = buf.get("arguments") or item.get("arguments") or "{}"
                try:
                    args = json.loads(args_raw)
                except Exception:
                    args = {"raw": args_raw}
                tool_calls.append(
                    ToolCallRequest(
                        id=f"{call_id}|{buf.get('id') or item.get('id') or 'fc_0'}",
                        name=buf.get("name") or item.get("name"),
                        arguments=args,
                    )
                )
        elif event_type == "response.completed":
            status = (event.get("response") or {}).get("status")
            finish_reason = _map_finish_reason(status)
        elif event_type in {"error", "response.failed"}:
            raise RuntimeError("Codex response failed")

    return content, tool_calls, finish_reason


_FINISH_REASON_MAP = {"completed": "stop", "incomplete": "length", "failed": "error", "cancelled": "error"}


def _map_finish_reason(status: str | None) -> str:
    return _FINISH_REASON_MAP.get(status or "completed", "stop")


def _friendly_error(status_code: int, raw: str) -> str:
    if status_code == 429:
        return "ChatGPT usage quota exceeded or rate limit triggered. Please try again later."
    return f"HTTP {status_code}: {raw}"
