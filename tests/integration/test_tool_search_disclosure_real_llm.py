"""Real-LLM e2e for progressive tool disclosure (search -> call, no describe).

Drives a real ``AgentLoop`` against OpenRouter (deepseek-v3.2). With the
catalog compacted, a sentinel domain tool (``convert_currency``) is hidden;
the model must ``tool_search`` to find it and ``tool_call`` to invoke it. Since
``tool_search`` now returns each hit's parameter schema and ``tool_describe``
no longer exists, a correct call proves the model used the schema carried in
the search result directly.

Skips unless an OpenRouter key is configured in ``~/.raven/config.json`` or
``~/.everclaw/config.json`` (providers.openrouter.apiKey).

    uv run pytest tests/integration/test_tool_search_disclosure_real_llm.py -m real_llm -s
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.tools.base import Tool
from raven.config.schema import ToolSearchConfig
from raven.providers.litellm_provider import LiteLLMProvider

MODEL = "openrouter/deepseek/deepseek-v3.2-exp"


def _load_openrouter_key() -> str | None:
    for name in ("~/.raven/config.json", "~/.everclaw/config.json"):
        p = Path(name).expanduser()
        if not p.exists():
            continue
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        key = (cfg.get("providers", {}).get("openrouter", {}) or {}).get("apiKey")
        if key and str(key).startswith("sk-or-"):
            return key
    return None


class _FakeDomainTool(Tool):
    """A no-op cataloged tool used to pad the catalog past the threshold."""

    def __init__(self, name: str, description: str) -> None:
        self._name = name
        self._description = description

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return f"{self._name} ok"


class _ConvertCurrencyTool(Tool):
    """Sentinel: records the args it was invoked with."""

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    @property
    def name(self) -> str:
        return "convert_currency"

    @property
    def description(self) -> str:
        return "Convert a monetary amount from one currency to another."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Amount to convert."},
                "from_currency": {"type": "string", "description": "ISO code to convert from, e.g. USD."},
                "to_currency": {"type": "string", "description": "ISO code to convert to, e.g. JPY."},
            },
            "required": ["amount", "from_currency", "to_currency"],
        }

    async def execute(self, **kwargs: Any) -> str:
        self._calls.append(kwargs)
        return "15000 JPY"


DISTRACTORS = [
    ("send_slack_message", "Post a message to a Slack channel."),
    ("create_github_issue", "Open a new issue in a GitHub repository."),
    ("generate_image", "Generate an image from a text prompt."),
    ("query_database", "Run a read-only SQL query against the warehouse."),
    ("book_flight", "Book a flight given origin, destination and date."),
    ("translate_text", "Translate text between languages."),
    ("summarize_document", "Summarize a long document."),
    ("fetch_weather", "Get the weather forecast for a city."),
    ("schedule_meeting", "Schedule a calendar meeting."),
    ("transcribe_audio", "Transcribe an audio file to text."),
    ("resize_photo", "Resize a photo to given dimensions."),
    ("send_email", "Send an email to a recipient."),
    ("crawl_webpage", "Fetch and extract text from a web page."),
    ("plot_chart", "Render a chart from a data series."),
    ("detect_language", "Detect the language of a text."),
]


@pytest.mark.real_llm
@pytest.mark.asyncio
async def test_search_then_call_with_returned_schema_real_llm() -> None:
    key = _load_openrouter_key()
    if not key:
        pytest.skip("OpenRouter key not configured (providers.openrouter.apiKey)")

    provider = LiteLLMProvider(
        api_key=key,
        api_base="https://openrouter.ai/api/v1",
        default_model=MODEL,
        provider_name="openrouter",
    )

    calls: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as td:
        loop = AgentLoop(
            provider=provider,
            workspace=Path(td),
            model=MODEL,
            max_iterations=6,
            restrict_to_workspace=True,
            tool_search_config=ToolSearchConfig(enabled=True, compaction_threshold=10),
        )
        for nm, desc in DISTRACTORS:
            loop.tools.register(_FakeDomainTool(nm, desc))
        loop.tools.register(_ConvertCurrencyTool(calls))

        events: list[tuple[str, str]] = []

        async def on_tool_event(phase: str, payload: dict) -> None:
            events.append((phase, payload.get("name", "")))

        task = (
            "I need to convert 100 USD to Japanese yen. You do not have a currency "
            "tool loaded — search the tool catalog for one, then use it. Amount is 100, "
            "from USD, to JPY."
        )
        final, tools_used, _messages, _outcome = await loop._run_agent_loop(
            [{"role": "user", "content": task}],
            on_tool_event=on_tool_event,
        )

    print("\ntools_used:", tools_used)
    print("tool events:", events)
    print("convert_currency calls:", calls)
    print("final:", final)

    assert "tool_search" in tools_used, "model should search the hidden catalog"
    assert "tool_call" in tools_used, "model should invoke the found tool via tool_call"
    assert "tool_describe" not in tools_used, "tool_describe was removed"
    assert calls, "the sentinel convert_currency tool must have been executed"
    args = calls[0]
    assert str(args.get("from_currency", "")).upper() == "USD"
    assert str(args.get("to_currency", "")).upper() == "JPY"
    assert float(args.get("amount")) == 100
