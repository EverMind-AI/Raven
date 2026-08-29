"""The Eval Engine boards through the assembly door by configuration alone.

``evalEngine.enabled`` is the only switch: off (the default) mounts nothing on
the loop, on mounts the engine's three hooks after whatever the host supplied.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.agent.loop.bundles import HostWiring
from raven.contracts.loop_hooks import AgentHook
from raven.eval_engine import AfterIterationHook, BeforeIterationHook, ToolAuditHook
from raven.providers.base import LLMProvider, LLMResponse

EVAL_HOOKS = (BeforeIterationHook, ToolAuditHook, AfterIterationHook)


class _Provider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        return LLMResponse(content="ok", finish_reason="stop")

    def get_default_model(self) -> str:
        return "fake/default"


class _HostHook(AgentHook):
    @property
    def name(self) -> str:
        return "host"


@pytest.fixture
def door(tmp_path: Path, monkeypatch):
    from raven.config.schema import Config
    from raven.core import runtime

    monkeypatch.setattr(runtime.token_wise_stack, "install_from_config", lambda *a, **k: None)
    monkeypatch.setattr(runtime.token_wise_stack, "caching_probe", lambda *a, **k: False)
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "ws")

    def build(ec_config, **kw):
        return runtime.build_runtime(config, ec_config, provider=_Provider(), **kw)

    return build


def _mounted(rt):
    return [h for h in rt.loop.hooks if isinstance(h, EVAL_HOOKS)]


def test_off_by_default_mounts_no_eval_hook(door) -> None:
    from raven.config.raven import RavenConfig

    rt = door(RavenConfig())
    assert _mounted(rt) == []


def test_enabled_mounts_the_three_hooks_after_the_hosts(door) -> None:
    from raven.config.raven import RavenConfig

    ec = RavenConfig(evalEngine={"enabled": True, "onTaskCompletion": False})
    host_hook = _HostHook()
    rt = door(ec, host=HostWiring(hooks=[host_hook]))

    mounted = _mounted(rt)
    assert [type(h) for h in mounted] == list(EVAL_HOOKS)
    chain = list(rt.loop.hooks)
    assert chain.index(host_hook) > max(chain.index(h) for h in mounted)
