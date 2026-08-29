"""The assembly door swaps a part by configuration alone.

``build_runtime`` derives the memory backend from ``memory.backend`` through
the plugin registry; a backend that ships as a plugin in a user directory
boards the loop with no code in the inner layers changed, and a turn reaches
it through the same seams the bundled one uses. This is the swap the
assembly container promises, exercised end to end on a fake provider.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.providers.base import LLMProvider, LLMResponse
from raven.spine import ChatType, Origin, Source, TurnRequest

DEMO_BACKEND = """
class DemoBackend:
    calls: list[str] = []

    def __init__(self, ctx):
        self.ctx = ctx

    async def recall(self, query, *, user_id=None, agent_id=None, top_k=5):
        DemoBackend.calls.append("recall")
        return []

    async def store(self, session_id, messages, *, metadata=None):
        DemoBackend.calls.append("store")

    async def feedback(self, signals):
        DemoBackend.calls.append("feedback")

    async def start(self):
        DemoBackend.calls.append("start")

    async def stop(self):
        DemoBackend.calls.append("stop")


def make(ctx):
    return DemoBackend(ctx)
"""


class _Provider(LLMProvider):
    """One fixed reply; the retry and classification machinery comes from the base."""

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


def _install_demo_plugin(root: Path) -> Path:
    plug = root / "plugins" / "swapdemo"
    plug.mkdir(parents=True)
    plug.joinpath("raven-plugin.toml").write_text(
        '[plugin]\nid = "swapdemo"\nversion = "1.0"\nenabled_by_default = true\n'
        "[[plugin.contributes.memory_backends]]\n"
        'name = "swapdemo"\nfactory = "swapdemo_backend:make"\n'
    )
    plug.joinpath("swapdemo_backend.py").write_text(DEMO_BACKEND)
    return root / "plugins"


@pytest.mark.asyncio
async def test_a_plugin_backend_boards_through_the_door_and_sees_the_turn(tmp_path: Path, monkeypatch) -> None:
    from raven.config.raven import RavenConfig
    from raven.config.schema import Config
    from raven.core import plugin_stack, runtime

    user_dir = _install_demo_plugin(tmp_path)
    monkeypatch.setattr(
        plugin_stack,
        "plugin_discovery_sources",
        lambda: {
            "bundled_dir": tmp_path / "none",
            "user_dir": user_dir,
            "project_dir": tmp_path / "none",
            "entry_points_group": None,
        },
    )
    monkeypatch.setattr(runtime.token_wise_stack, "install_from_config", lambda *a, **k: None)
    monkeypatch.setattr(runtime.token_wise_stack, "caching_probe", lambda *a, **k: False)

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "ws")
    ec_config = RavenConfig(memory={"backend": "swapdemo"})

    rt = runtime.build_runtime(config, ec_config, provider=_Provider())

    assert type(rt.backend).__name__ == "DemoBackend"
    assert rt.loop.backend is rt.backend
    calls = type(rt.backend).calls
    calls.clear()

    await rt.loop._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="remember that the sky is blue",
        ),
        session_key="s1",
    )
    drain = getattr(rt.loop, "drain_backend_stores", None)
    if drain is not None:
        await drain()

    assert "recall" in calls, calls
    assert "store" in calls, calls
