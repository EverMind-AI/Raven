"""One whole turn, with the worker table on and off, compared call for call.

The claim this file exists to hold is the switch's own promise: with
``playbooks.agentHarness`` at its default, a turn is what it was before this
feature existed. Asserted the only way that can be asserted -- by running a
real turn through ``run_turn`` (where the per-turn scopes are opened, which a
capture calling ``_process_message`` would skip) and diffing everything that
crosses the provider boundary.

The one difference the feature is allowed to make is also pinned: ``spawn``
offers this turn's workers instead of the bare roster, and each carries the
brief the task written for it has to match.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.config.raven import CheckpointConfig, RuntimeConfig
from raven.config.schema import PlaybookConfig
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest

QUERY = "List what is in the workspace, then tell me how many entries you saw."
EMIT = "emit_worker_table"

WORKERS = {
    "description": "two researchers, one per competitor",
    "workers": [
        {
            "as": "research-a",
            "name": "Raven",
            "brief": "only A's pricing",
            "systemPrompt": "Only look at A. Leave B alone.",
            "tools": ["web_fetch"],
        },
        {"as": "research-b", "name": "Raven", "brief": "only B's pricing", "systemPrompt": "Only look at B."},
    ],
}


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content)


def _spawn_target(tools: Any) -> dict[str, Any] | None:
    """What ``spawn`` offers as its target -- the one observable a table moves."""
    for tool in tools or []:
        fn = tool.get("function", tool) or {}
        if fn.get("name") != "spawn":
            continue
        prop = ((fn.get("parameters") or {}).get("properties") or {}).get("subagent") or {}
        return {"enum": prop.get("enum"), "description": prop.get("description")}
    return None


def _scrub(value: Any) -> Any:
    """Drop the only thing that legitimately moves between two runs.

    Two: ``wrap_untrusted`` mints a fresh eight-hex nonce per invocation, and
    each run gets its own temp workspace whose path the identity segment
    embeds. Neither is behaviour, and both differ between any two runs.
    """
    if isinstance(value, str):
        value = re.sub(r"/tmp/[^\s\"']+", "<TMP>", value)
        value = re.sub(r"\b[0-9a-f]{8}\b", "NONCE", value)
        return value
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    return value


class _Scripted(LLMProvider):
    """Answers the setup call with a table, then drives a two-iteration turn."""

    def __init__(self, *, emit_table: bool) -> None:
        super().__init__(api_key="test")
        self._emit_table = emit_table
        self.calls: list[dict[str, Any]] = []

    def get_default_model(self) -> str:
        return "stub"

    def _record(self, messages, tools, model, kind) -> None:
        self.calls.append(
            {
                "kind": kind,
                "model": model,
                "tool_names": sorted((t.get("function") or {}).get("name") or t.get("name") for t in (tools or [])),
                "spawn_target": _spawn_target(tools),
                "messages": [{"role": m.get("role"), "content": _text(m.get("content"))} for m in messages],
            }
        )

    def _turn_reply(self) -> LLMResponse:
        turns = len([c for c in self.calls if c["kind"] == "turn"])
        if turns == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="call_1", name="list_dir", arguments={"path": "."})],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="I saw the workspace listing above.", finish_reason="stop")

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        self._record(messages, tools, model, "turn")
        return self._turn_reply()

    async def chat_with_retry(self, messages, tools=None, model=None, fallback_models=None, **kwargs) -> LLMResponse:
        names = {(t.get("function", t) or {}).get("name") for t in (tools or [])}
        if EMIT in names:
            self._record(messages, tools, model, "setup")
            if not self._emit_table:
                return LLMResponse(content="no", finish_reason="stop")
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="gen_1", name=EMIT, arguments=WORKERS)],
                finish_reason="tool_calls",
            )
        self._record(messages, tools, model, "turn")
        return self._turn_reply()


async def _run(workspace, harness: str, *, emit_table: bool = True) -> tuple[_Scripted, list[str]]:
    (workspace / "alpha.txt").write_text("a")
    (workspace / "beta.txt").write_text("b")
    provider = _Scripted(emit_table=emit_table)
    loop = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=4),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(
            runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
            playbook_config=PlaybookConfig(agentHarness=harness),
        ),
    )
    emitted: list[str] = []

    async def _emit(*args, **kwargs):
        for item in list(args) + list(kwargs.values()):
            text = getattr(item, "content", None) or getattr(item, "text", None)
            if isinstance(text, str) and text.strip():
                emitted.append(text)

    def _drain():
        return []

    await loop.run_turn(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text=QUERY,
            conversation="test:c1",
        ),
        _emit,
        _drain,
        stream=False,
    )
    return provider, emitted


def _turn_calls(provider: _Scripted) -> list[dict[str, Any]]:
    return [_scrub(call) for call in provider.calls if call["kind"] == "turn"]


@pytest.mark.asyncio
async def test_the_default_turn_is_deterministic(tmp_path_factory) -> None:
    """The baseline this file compares against has to be stable itself."""
    first, _ = await _run(tmp_path_factory.mktemp("a"), "default")
    second, _ = await _run(tmp_path_factory.mktemp("b"), "default")
    assert _turn_calls(first) == _turn_calls(second)


@pytest.mark.asyncio
async def test_a_configured_turn_differs_only_in_what_spawn_offers(tmp_path_factory) -> None:
    """The switch's promise, measured rather than asserted.

    Everything the loop hands the provider -- the model, the tool array, every
    message -- is identical with the feature on. The single exception is the
    target ``spawn`` offers, which is the whole of what a worker table is for.
    """
    off, off_emitted = await _run(tmp_path_factory.mktemp("off"), "default")
    on, on_emitted = await _run(tmp_path_factory.mktemp("on"), "generate")

    off_turns, on_turns = _turn_calls(off), _turn_calls(on)
    assert len(off_turns) == len(on_turns), "the feature must not add or drop a turn's model calls"
    assert off_emitted == on_emitted, "the answer must not change"

    for index, (a, b) in enumerate(zip(off_turns, on_turns, strict=True), 1):
        assert a["model"] == b["model"], f"call {index}: model moved"
        assert a["tool_names"] == b["tool_names"], f"call {index}: the tool array moved"
        assert a["messages"] == b["messages"], f"call {index}: the prompt moved"


@pytest.mark.asyncio
async def test_the_setup_call_is_the_only_extra_one(tmp_path_factory) -> None:
    on, _ = await _run(tmp_path_factory.mktemp("on"), "generate")
    setup = [call for call in on.calls if call["kind"] == "setup"]
    assert len(setup) == 1
    assert setup[0]["tool_names"] == [EMIT], "the setup call is offered one tool and no others"


@pytest.mark.asyncio
async def test_spawn_offers_the_workers_and_their_briefs(tmp_path_factory) -> None:
    on, _ = await _run(tmp_path_factory.mktemp("on"), "generate")
    target = _turn_calls(on)[0]["spawn_target"]
    assert target["enum"] == ["research-a", "research-b"]
    assert "only A's pricing" in target["description"]
    assert "only B's pricing" in target["description"]
    assert "Raven" in target["description"], "the label says which agent is behind it"


@pytest.mark.asyncio
async def test_the_default_turn_offers_the_bare_roster(tmp_path_factory) -> None:
    off, _ = await _run(tmp_path_factory.mktemp("off"), "default")
    target = _turn_calls(off)[0]["spawn_target"]
    assert "Raven" in target["enum"], "the roster's own names, whatever this install has"
    assert "research-a" not in target["enum"]


@pytest.mark.asyncio
async def test_a_setup_call_that_emits_nothing_leaves_the_turn_alone(tmp_path_factory) -> None:
    """The fallback, end to end: a generation that produces no table must give
    a turn that is byte-identical to the default one."""
    off, _ = await _run(tmp_path_factory.mktemp("off"), "default")
    on, _ = await _run(tmp_path_factory.mktemp("on"), "generate", emit_table=False)
    assert _turn_calls(off) == _turn_calls(on)


@pytest.mark.asyncio
async def test_the_setup_call_runs_on_the_session_s_own_model(tmp_path_factory) -> None:
    """The table is written before ``use_binding`` opens, so the binding has to
    be resolved by hand -- reading ``self.provider`` there answers with the
    loop's default, and a session that switched model would have its setup call
    go out on the model, and the credential, it switched away from.
    """
    workspace = tmp_path_factory.mktemp("switched")
    (workspace / "alpha.txt").write_text("a")
    provider = _Scripted(emit_table=True)
    loop = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=4),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(
            runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
            playbook_config=PlaybookConfig(agentHarness="generate"),
        ),
    )
    switched = replace(loop.binding_for_session("test:c1"), model="switched-model")
    loop.set_session_binding("test:c1", switched)

    async def _emit(*args, **kwargs):
        return None

    await loop.run_turn(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text=QUERY,
            conversation="test:c1",
        ),
        _emit,
        lambda: [],
        stream=False,
    )
    setup = [call for call in provider.calls if call["kind"] == "setup"]
    assert setup and setup[0]["model"] == "switched-model"
