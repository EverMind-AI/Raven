"""A reply that writes its tool calls as ``<use_mcp_tool>`` blocks is read as those calls."""

from __future__ import annotations

from typing import Any

from raven.agent.loop import AgentLoop
from raven.contracts.llm_provider import LLMResponse
from raven.contracts.tool import Tool
from raven.providers.text_tool_calls import recover_text_tool_calls
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest

_OFFERED = [
    {"type": "function", "function": {"name": "web_search"}},
    {"type": "function", "function": {"name": "web_fetch"}},
]

_BLOCK = (
    "<use_mcp_tool>\n<server_name>search_and_scrape_webpage</server_name>\n"
    "<tool_name>web_search</tool_name>\n<arguments>\n"
    '{"query": "personal agent new system July 2026", "count": 10}\n'
    "</arguments>\n</use_mcp_tool>"
)


def _reply(content: str | None, **extra: Any) -> LLMResponse:
    return LLMResponse(content=content, finish_reason=extra.pop("finish_reason", "stop"), **extra)


def test_a_block_naming_an_offered_tool_becomes_its_call():
    recovered = recover_text_tool_calls(_reply(f"Searching first.\n<toolbox> {_BLOCK} </toolbox>"), _OFFERED)

    assert recovered is not None
    assert [(c.name, c.arguments) for c in recovered.tool_calls] == [
        ("web_search", {"query": "personal agent new system July 2026", "count": 10})
    ]
    assert recovered.finish_reason == "tool_calls"
    assert recovered.content == "Searching first."
    assert recovered.tool_calls[0].id


def test_every_block_is_a_call_in_order():
    fetch = _BLOCK.replace("web_search", "web_fetch").replace('"count": 10', '"url": "https://a.example"')
    recovered = recover_text_tool_calls(_reply(_BLOCK + "\n" + fetch), _OFFERED)

    assert [c.name for c in recovered.tool_calls] == ["web_search", "web_fetch"]
    assert recovered.content is None
    assert len({c.id for c in recovered.tool_calls}) == 2


def test_repaired_arguments_are_flagged_on_the_call():
    broken = _BLOCK.replace('"count": 10}', '"count": 10')
    call = recover_text_tool_calls(_reply(broken), _OFFERED).tool_calls[0]

    assert call.arguments["count"] == 10
    assert call.run_meta is not None and call.run_meta.arguments_repaired


def test_what_cannot_be_read_as_calls_is_left_as_it_came():
    unoffered = _BLOCK.replace("web_search", "exec")
    not_an_object = _BLOCK.replace('{"query": "personal agent new system July 2026", "count": 10}', '["a"]')
    unclosed = _BLOCK.removesuffix("</use_mcp_tool>")
    for content in (unoffered, not_an_object, unclosed, _BLOCK + "\n" + unoffered, "no blocks here", None):
        assert recover_text_tool_calls(_reply(content), _OFFERED) is None, content
    # No tools on offer, a failed call, or a reply that already carries calls.
    assert recover_text_tool_calls(_reply(_BLOCK), None) is None
    assert recover_text_tool_calls(_reply(_BLOCK, finish_reason="error"), _OFFERED) is None
    native = recover_text_tool_calls(_reply(_BLOCK), _OFFERED)
    assert recover_text_tool_calls(native, _OFFERED) is None


class _Search(Tool):
    def __init__(self) -> None:
        self.queries: list[str] = []

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return "search"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"query": {"type": "string"}, "count": {"type": "integer"}}}

    async def execute(self, **kwargs: Any) -> str:
        self.queries.append(kwargs["query"])
        return "results"


class _TextCaller:
    def __init__(self) -> None:
        self.responses = [_reply(_BLOCK), _reply("done")]
        self.seen: list[list[dict[str, Any]]] = []

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.seen.append(list(kwargs["messages"]))
        return self.responses.pop(0)

    def get_default_model(self) -> str:
        return "fake/model"


async def test_the_loop_runs_the_call_a_reply_wrote_as_text(tmp_path):
    provider = _TextCaller()
    agent = AgentLoop(provider=provider, workspace=tmp_path, model="fake/model")
    search = _Search()
    agent.tools.register(search)

    result, _media = await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="tui", chat_id="default", sender_id="user", chat_type=ChatType.DM),
            text="go",
            conversation="session-a",
        ),
        session_key="session-a",
    )

    assert search.queries == ["personal agent new system July 2026"]
    assert result == "done"
    # The model is shown its own call in the wire's form, not the text it wrote.
    assistant = [m for m in provider.seen[-1] if m.get("role") == "assistant"][-1]
    assert assistant["tool_calls"][0]["function"]["name"] == "web_search"
    assert "<use_mcp_tool>" not in (assistant.get("content") or "")
