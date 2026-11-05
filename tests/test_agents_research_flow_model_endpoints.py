"""``modelEndpoints``: a flow gate pinned to a model the session's provider does not serve.

The plugin is lent one provider, the session's. A digest or a reviewer pinned to a
fixed model on another endpoint therefore needs that model routed by name, while
every other call - and every call naming no model - stays on the session's model.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow import plugin as flow_plugin  # noqa: E402
from research_flow.config import FlowConfig  # noqa: E402
from research_flow.support.ledger import set_ledger_dir  # noqa: E402

from raven.plugins.context import PluginContext, ServiceLocator  # noqa: E402
from raven.providers.base import LLMProvider, LLMResponse  # noqa: E402

_ENDPOINT = {"model": "fixed-flash", "apiBase": "https://llm.example/v1", "apiKeyEnv": "FLOW_TEST_FLASH_KEY"}


class _Recorder(LLMProvider):
    def __init__(self, name: str, *, api_base: str = "", api_key: str = ""):
        super().__init__(api_key="test")
        self.name = name
        self.api_base = api_base
        self.endpoint_key = api_key
        self.calls: list[dict] = []

    async def chat(self, messages, tools=None, model=None, **kwargs):
        self.calls.append({"model": model, "messages": messages})
        return LLMResponse(content=f"from {self.name}", finish_reason="stop")

    def get_default_model(self) -> str:
        return self.name


@pytest.fixture
def endpoints(monkeypatch):
    """Endpoint providers stand in for the wire, recording what each was built with."""
    built: dict[str, _Recorder] = {}

    def fake_endpoint(ep):
        built[ep.model] = _Recorder(ep.model, api_base=ep.api_base, api_key=ep.api_key)
        return built[ep.model]

    monkeypatch.setattr("raven.providers.per_model_provider._endpoint_provider", fake_endpoint)
    return built


def _shared(tmp_path, slice_: dict, lent):
    ctx = PluginContext(
        config={"enabled": True, **slice_},
        services=ServiceLocator(workspace=tmp_path, user_id="u", agent_id="a", provider=lent),
    )
    return flow_plugin._shared_for(ctx)


@pytest.fixture(autouse=True)
def _reset(tmp_path):
    yield
    flow_plugin._SHARED.pop(str(tmp_path), None)
    set_ledger_dir(None)


def test_no_endpoints_lends_the_sessions_provider_unchanged(tmp_path):
    lent = _Recorder("student")
    assert _shared(tmp_path, {}, lent).provider is lent


def test_a_named_model_reaches_its_endpoint_and_the_rest_stay_on_the_session(tmp_path, monkeypatch, endpoints):
    monkeypatch.setenv("FLOW_TEST_FLASH_KEY", "sk-test")
    lent = _Recorder("student")
    provider = _shared(tmp_path, {"modelEndpoints": [_ENDPOINT]}, lent).provider

    async def scenario():
        await provider.chat_with_retry(messages=[{"role": "user", "content": "q"}], model="fixed-flash")
        await provider.chat_with_retry(messages=[{"role": "user", "content": "q"}])
        await provider.chat_with_retry(messages=[{"role": "user", "content": "q"}], model="student")

    asyncio.run(scenario())
    routed = endpoints["fixed-flash"]
    assert (routed.api_base, routed.endpoint_key) == ("https://llm.example/v1", "sk-test")
    assert [c["model"] for c in routed.calls] == ["fixed-flash"]
    assert len(lent.calls) == 2
    # The ledger's reviewer column reads this for a gate whose model is unset.
    assert provider.get_default_model() == "student"


def test_the_digest_pinned_to_an_endpoint_model_is_served_there(tmp_path, monkeypatch, endpoints):
    monkeypatch.setenv("FLOW_TEST_FLASH_KEY", "sk-test")
    lent = _Recorder("student")
    provider = _shared(tmp_path, {"modelEndpoints": [_ENDPOINT]}, lent).provider
    digest = flow_plugin._make_digest_fn(provider, "fixed-flash")

    out = asyncio.run(digest("page body " * 50, "the founding year"))

    assert out == "from fixed-flash"
    assert lent.calls == []
    assert "Information needed: the founding year" in endpoints["fixed-flash"].calls[0]["messages"][1]["content"]


def test_a_key_variable_that_is_unset_refuses_instead_of_falling_back(tmp_path, monkeypatch, endpoints):
    monkeypatch.delenv("FLOW_TEST_FLASH_KEY", raising=False)
    with pytest.raises(ValueError, match="FLOW_TEST_FLASH_KEY"):
        _shared(tmp_path, {"modelEndpoints": [_ENDPOINT]}, _Recorder("student"))


def test_an_endpoint_without_a_key_variable_is_key_free(tmp_path, endpoints):
    shared = _shared(tmp_path, {"modelEndpoints": [{"model": "local", "apiBase": "http://h:1/v1"}]}, _Recorder("s"))
    assert shared.provider is not None
    assert endpoints["local"].endpoint_key == "EMPTY"


@pytest.mark.parametrize(
    "entry",
    [
        {"model": "m", "apiBase": "http://h/v1", "apiKeyENV": "K"},
        {"model": "", "apiBase": "http://h/v1"},
        {"model": "m"},
    ],
)
def test_a_malformed_entry_is_refused(entry):
    with pytest.raises(ValueError):
        FlowConfig.from_slice({"modelEndpoints": [entry]})


def test_a_mode_overlay_cannot_move_the_endpoints():
    base = FlowConfig.from_slice({"modelEndpoints": [{"model": "m", "apiBase": "http://h/v1"}]})
    with pytest.raises(ValueError, match="modelEndpoints"):
        base.with_overlay({"modelEndpoints": []})
    assert base.with_overlay({"verify": {"maxRevisions": 3}}).model_endpoints == base.model_endpoints
