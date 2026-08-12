"""AgentLoop <-> LazyProvider wiring (SF11).

``LazyProvider`` exists so building the real provider -- which imports
litellm, ~2-7s -- happens on a background prewarm thread instead of stalling
``AgentLoop.__init__``. Construction still needs an answer for the model's
context window, and used to get one by asking LiteLLM's static table, which
imports LiteLLM right there on the main thread if it is not loaded yet --
defeating the whole point of the lazy provider. ``resolve_context_window``'s
``allow_fetch=False`` construction-time tier now also means "and don't import
LiteLLM to answer this" (see ``rates._try_litellm_context_window``'s
``allow_import``), and ``AgentLoop.__init__`` wires ``LazyProvider.on_built``
to ``refresh_context_window`` so the window self-corrects once prewarm
finishes the import in the background.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from raven.agent.loop import AgentLoop
from raven.providers.base import GenerationSettings, LLMProvider
from raven.providers.lazy import LazyProvider


class _StubProvider(LLMProvider):
    api_key = "test"

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, *args, **kwargs):
        raise NotImplementedError

    async def chat_with_retry(self, *args, **kwargs):
        raise NotImplementedError


def _make_lazy(factory=None) -> LazyProvider:
    if factory is None:

        def factory():
            raise AssertionError("the factory must not run during AgentLoop construction")

    return LazyProvider(factory, default_model="stub", generation=GenerationSettings())


def _make_loop(tmp_path: Path, provider) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
    )


def test_construction_wires_on_built_to_refresh_context_window(tmp_path: Path) -> None:
    lazy = _make_lazy()
    agent = _make_loop(tmp_path, lazy)

    assert lazy.on_built == agent.refresh_context_window


def test_construction_does_not_build_the_lazy_provider(tmp_path: Path) -> None:
    """The factory raises if called -- construction must never invoke it."""
    lazy = _make_lazy()
    _make_loop(tmp_path, lazy)  # must not raise


def test_construction_with_a_plain_provider_skips_the_wiring(tmp_path: Path) -> None:
    """A provider with no ``on_built`` attribute (every provider but
    LazyProvider) must not make construction raise trying to set one."""
    _make_loop(tmp_path, _StubProvider())  # must not raise


def test_on_built_firing_after_construction_updates_the_window(tmp_path: Path, monkeypatch) -> None:
    """End to end: once the real provider is built, the callback re-walks the
    ladder and the corrected window lands on the loop."""
    import raven.agent.loop.main as agent_loop_main

    lazy = _make_lazy(factory=lambda: _StubProvider())
    agent = _make_loop(tmp_path, lazy)

    monkeypatch.setattr(agent_loop_main, "effective_context_window", lambda *a, **k: 4096)
    lazy._built()  # simulates prewarm's background build completing

    assert agent.context_window_tokens == 4096


def test_agentloop_construction_with_a_lazy_provider_never_imports_litellm() -> None:
    """The regression this fixes: resolving the construction-time window used
    to import litellm inline (see module docstring). Run in a subprocess for
    a guaranteed-clean ``sys.modules`` -- other test files in this session
    force-import litellm at collection time (see test_provider_rates.py),
    so an in-process check would pass or fail depending on test order.
    """
    script = """
import sys
import tempfile
from pathlib import Path

from raven.agent.loop import AgentLoop
from raven.providers import model_catalog_cache
from raven.providers.base import GenerationSettings, LLMProvider
from raven.providers.lazy import LazyProvider


class _StubProvider(LLMProvider):
    def get_default_model(self):
        return "openrouter/deepseek/deepseek-v4-pro"


assert "litellm" not in sys.modules, "litellm already imported before construction -- test is not isolated"

with tempfile.TemporaryDirectory() as td:
    model_catalog_cache._CACHE_PATH = Path(td) / "model-catalog.json"
    lazy = LazyProvider(
        factory=lambda: _StubProvider(),
        default_model="openrouter/deepseek/deepseek-v4-pro",
        generation=GenerationSettings(),
    )
    AgentLoop(
        provider=lazy,
        workspace=Path(td),
        model="openrouter/deepseek/deepseek-v4-pro",
        max_iterations=2,
        restrict_to_workspace=True,
    )

ok = "litellm" not in sys.modules
print("LITELLM_NOT_IMPORTED" if ok else "LITELLM_IMPORTED", flush=True)

# The claim under test is the mid-process sys.modules state above; interpreter
# teardown is not part of it, and AgentLoop's construction drags in native
# libraries whose atexit hooks segfault the teardown on Linux (observed as
# returncode -11 in CI while the same script exits 0 on macOS). Skip teardown.
import os

os._exit(0 if ok else 2)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "LITELLM_NOT_IMPORTED" in result.stdout, result.stdout + result.stderr
