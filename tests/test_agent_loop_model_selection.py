"""Per-session model: Session.metadata["model"] outranks the router."""

from __future__ import annotations

from raven.agent.loop import AgentLoop
from raven.contracts.llm_provider import LLMResponse
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


class _ModelRecordingProvider:
    """Records the model each call was made with. Non-streaming path."""

    def __init__(self) -> None:
        self.seen: list[str | None] = []
        self.seen_fallbacks: list[list[str] | None] = []

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.seen.append(kwargs.get("model"))
        self.seen_fallbacks.append(kwargs.get("fallback_models"))
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "fake/default"


def _stub_edges(loop: AgentLoop) -> None:
    """No-op the sandbox/MCP bring-up so a text-only turn runs without a VM."""

    async def _noop(**_kw) -> None:
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop


def _req(text: str, *, conversation: str) -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="web", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text=text,
        conversation=conversation,
    )


async def test_session_model_reaches_the_llm_call(tmp_path):
    provider = _ModelRecordingProvider()
    loop = AgentLoop(provider=provider, workspace=tmp_path, model="fake/default")
    _stub_edges(loop)
    loop.sessions.get_or_create("web:abc").metadata["model"] = "anthropic/claude-opus-4-5"

    await loop._process_message(_req("hi", conversation="web:abc"))

    assert provider.seen == ["anthropic/claude-opus-4-5"]


async def test_a_session_without_a_model_uses_the_agent_default(tmp_path):
    provider = _ModelRecordingProvider()
    loop = AgentLoop(provider=provider, workspace=tmp_path, model="fake/default")
    _stub_edges(loop)

    await loop._process_message(_req("hi", conversation="web:plain"))

    assert provider.seen == ["fake/default"]


class _CountingRouter:
    """Records whether the router was consulted at all."""

    def __init__(self, model: str, fallbacks: list[str]) -> None:
        self._model = model
        self._fallbacks = list(fallbacks)
        self.calls = 0

    async def select_model_chain(self, prompt: str) -> tuple[str | None, list[str]]:
        self.calls += 1
        return self._model, list(self._fallbacks)


async def test_a_session_model_suppresses_the_router_entirely(tmp_path):
    """Without this, the if/elif could regress into two independent ifs and the
    other tests would not notice, because they run with no router configured."""
    provider = _ModelRecordingProvider()
    router = _CountingRouter("router/picked", ["router/fallback"])
    loop = AgentLoop(provider=provider, workspace=tmp_path, model="fake/default", router=router)
    _stub_edges(loop)
    loop.sessions.get_or_create("web:abc").metadata["model"] = "anthropic/claude-opus-4-5"

    await loop._process_message(_req("hi", conversation="web:abc"))

    assert provider.seen == ["anthropic/claude-opus-4-5"]
    assert router.calls == 0
    assert provider.seen_fallbacks == [[]]


async def test_the_router_still_runs_when_the_session_has_no_model(tmp_path):
    """Guards the other direction: the elif branch must stay reachable."""
    provider = _ModelRecordingProvider()
    router = _CountingRouter("router/picked", [])
    loop = AgentLoop(provider=provider, workspace=tmp_path, model="fake/default", router=router)
    _stub_edges(loop)

    await loop._process_message(_req("hi", conversation="web:plain"))

    assert router.calls == 1
    assert provider.seen == ["router/picked"]


async def test_two_sessions_keep_their_own_models(tmp_path):
    """The isolation guarantee: one loop, two sessions, two models."""
    provider = _ModelRecordingProvider()
    loop = AgentLoop(provider=provider, workspace=tmp_path, model="fake/default")
    _stub_edges(loop)
    loop.sessions.get_or_create("web:a").metadata["model"] = "anthropic/claude-opus-4-5"
    loop.sessions.get_or_create("web:b").metadata["model"] = "deepseek/deepseek-v3"

    await loop._process_message(_req("hi", conversation="web:a"))
    await loop._process_message(_req("hi", conversation="web:b"))

    assert provider.seen == ["anthropic/claude-opus-4-5", "deepseek/deepseek-v3"]
