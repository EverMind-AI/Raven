"""The checked-in ClawBench runner speaks the compacting-only context API.

The runner predates the unified context engine: it used to choose a context
engine and a curator model. Both are gone, so the runner must expose neither
and let the one engine run its default history strategy.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from pathlib import Path

from raven.config.raven import ContextConfig

STREAM = Path(__file__).resolve().parent.parent / "benchmarks" / "clawbench" / "stream.py"


def _runner():
    spec = importlib.util.spec_from_file_location("clawbench_stream", STREAM)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_args_exposes_no_retired_context_engine_flags(monkeypatch) -> None:
    module = _runner()
    monkeypatch.setattr(sys, "argv", ["stream.py"])
    args = module.parse_args()
    assert not hasattr(args, "context_engine")
    assert not hasattr(args, "curator_model")


def test_the_session_takes_no_context_engine_or_curator_model() -> None:
    module = _runner()
    params = inspect.signature(module.RavenSession.__init__).parameters
    assert "context_engine" not in params
    assert "curator_model" not in params


def test_the_default_history_strategy_is_compacting() -> None:
    assert ContextConfig().history.strategy == "compacting"


def test_the_session_builds_a_loop_from_the_current_wiring_bundles(tmp_path, monkeypatch) -> None:
    """Constructing the session is the contract; ``--help`` never reaches it.

    The runner imports its provider and builds ``AgentLoop`` inside
    ``__init__``, so a help run exits before either line and proves nothing.
    This builds the object: the import path, the five wiring bundles and the
    keyword set all have to match the loop this tree ships.
    """
    from raven.providers.base import LLMProvider, LLMResponse

    class _Provider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")

        async def chat(self, *args, **kwargs) -> LLMResponse:
            return LLMResponse(content="", finish_reason="stop")

        def get_default_model(self) -> str:
            return "bench/model"

    monkeypatch.setattr("raven.providers.factory.make_provider", lambda config, model=None: _Provider())
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"agents": {"defaults": {"model": "bench/model", "provider": "openai"}}}),
        encoding="utf-8",
    )

    module = _runner()
    session = module.RavenSession(
        workspace=tmp_path / "ws",
        session_id="clawbench:test",
        model=None,
        provider_name=None,
        api_key=None,
        api_base=None,
        config_path=config_path,
        max_iterations=7,
        context_window=None,
        restrict_to_workspace=True,
    )

    assert session.agent._default_max_iterations == 7
    assert session.agent.interactive is False
    assert session.context_window > 0
    assert session.agent.context_engine.mints_source_refs is True
