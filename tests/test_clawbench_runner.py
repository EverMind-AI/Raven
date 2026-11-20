"""The checked-in ClawBench runner speaks the compacting-only context API.

The runner predates the unified context engine: it used to choose a context
engine and a curator model. Both are gone, so the runner must expose neither
and let the one engine run its default history strategy.
"""

from __future__ import annotations

import importlib.util
import inspect
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
