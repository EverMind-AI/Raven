"""Recovering tool calls a model wrote as text in the MCP ``<use_mcp_tool>`` form.

Some models are trained on an agent framework that calls tools by writing a block
into the reply instead of using the wire's tool-call field::

    <use_mcp_tool>
    <server_name>search_and_scrape_webpage</server_name>
    <tool_name>web_search</tool_name>
    <arguments>
    {"query": "...", "count": 10}
    </arguments>
    </use_mcp_tool>

The inference server's tool parser does not know that form, so the call arrives as
words and the loop reads a step that does something as an answer. MiroThinker-1.7-mini
behind vLLM does this under a long agent prompt while it answers a short one with
proper tool calls: on one research turn its first search came back as this text, a
report gate took it for a draft, and the turn spent its budget re-sampling it.

This module turns such a reply back into the calls it meant. A block whose closing
tag is missing ends where the ``<toolbox>`` wrapper closes, where the next block opens,
or at the end of the reply: the same model, on another turn, left it out every time
and the turn ended on the text. Its ``<arguments>`` must still be closed. Only names
among the tools the step offered become calls, and the arguments must be a JSON
object; a reply holding anything else is left as it came. The server
name is dropped: this loop's tools are not grouped by server. Like the reasoning split
(``raven.providers.reasoning``) this needs the whole text, so it runs once the reply is
in hand, and words of the block that already streamed to a reader stay shown.
"""

from __future__ import annotations

import json
import re
import secrets
import string
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

import json_repair

from raven.contracts.llm_provider import LLMResponse, RunMeta, ToolCallRequest
from raven.providers.tool_names import normalized_tool_name

_BLOCK_RE = re.compile(
    r"<use_mcp_tool>(?P<body>.*?)(?:</use_mcp_tool>|(?=</toolbox>)|(?=<use_mcp_tool>)|\Z)", re.DOTALL
)
_TOOL_RE = re.compile(r"<tool_name>\s*(?P<name>.*?)\s*</tool_name>", re.DOTALL)
_ARGS_RE = re.compile(r"<arguments>(?P<args>.*?)</arguments>", re.DOTALL)
_WRAPPER_RE = re.compile(r"</?toolbox>")
_ALNUM = string.ascii_letters + string.digits


def _offered_names(tools: Iterable[dict[str, Any]] | None) -> set[str]:
    names: set[str] = set()
    for tool in tools or ():
        name = (tool.get("function") or {}).get("name") if isinstance(tool, dict) else None
        if isinstance(name, str):
            names.add(name)
    return names


def _arguments(text: str) -> tuple[dict[str, Any], bool] | None:
    """The block's arguments as an object, and whether they needed repair; None when they are not one."""
    text = text.strip()
    if not text:
        return {}, False
    try:
        value, repaired = json.loads(text), False
    except json.JSONDecodeError:
        value, repaired = json_repair.loads(text), True
    return (value, repaired) if isinstance(value, dict) else None


def _call(body: str, offered: set[str]) -> ToolCallRequest | None:
    name_hit = _TOOL_RE.search(body)
    if name_hit is None:
        return None
    name = normalized_tool_name(name_hit.group("name"))
    if name not in offered:
        return None
    args_hit = _ARGS_RE.search(body)
    if args_hit is None and "<arguments>" in body:
        return None
    parsed = _arguments(args_hit.group("args") if args_hit else "")
    if parsed is None:
        return None
    args, repaired = parsed
    return ToolCallRequest(
        id="".join(secrets.choice(_ALNUM) for _ in range(9)),
        name=name,
        arguments=args,
        run_meta=RunMeta(arguments_repaired=True) if repaired else None,
    )


def recover_text_tool_calls(response: LLMResponse, tools: Iterable[dict[str, Any]] | None) -> LLMResponse | None:
    """``response`` with the ``<use_mcp_tool>`` blocks of its reply as tool calls, or None to keep it.

    None unless the response carries no tool call of its own, did not fail, and every
    block in its reply names an offered tool with object arguments: a reply that is
    partly a call the loop cannot run is not guessed at. The reply keeps the words
    outside the blocks.
    """
    content = response.content
    if response.has_tool_calls or response.finish_reason == "error" or not content or "<use_mcp_tool>" not in content:
        return None
    offered = _offered_names(tools)
    if not offered:
        return None
    calls: list[ToolCallRequest] = []
    for hit in _BLOCK_RE.finditer(content):
        call = _call(hit.group("body"), offered)
        if call is None:
            return None
        calls.append(call)
    if not calls:
        return None
    rest = _WRAPPER_RE.sub("", _BLOCK_RE.sub("", content)).strip()
    return replace(response, content=rest or None, tool_calls=calls, finish_reason="tool_calls")


__all__ = ["recover_text_tool_calls"]
