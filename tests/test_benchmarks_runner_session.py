"""The benchmark runners build their session on the loop API this tree ships.

AppWorld, PinchBench direct and PinchBench bot each construct ``AgentLoop``
from inside a function body, so ``--help`` exits before the import and the
constructor and proves nothing. These tests drive each runner's own build path
and hold the real ``AgentLoop`` constructor to it: a retired keyword, a stale
import, or a wiring field moved to the wrong bundle fails here.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PINCHBENCH = REPO_ROOT / "benchmarks" / "pinchbench"
PINCH_DIRECT = PINCHBENCH / "direct"
PINCH_BOT = PINCHBENCH / "bot_runner"


def _load(directory: Path, name: str):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    return importlib.import_module(name)


def _task():
    task_loader = importlib.import_module("task_loader")
    return task_loader.Task(
        task_id="probe_1",
        name="probe",
        category="probe",
        grading_type="automated",
        timeout_seconds=5,
        workspace_files=[],
        prompt="say hi",
        expected_behavior="",
        grading_criteria=[],
    )


def _failing_provider():
    from raven.providers.base import LLMProvider, LLMResponse

    class _Provider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")

        async def chat(self, *args, **kwargs) -> LLMResponse:
            return LLMResponse(content="", finish_reason="stop")

        def get_default_model(self) -> str:
            return "bench/model"

    return _Provider()


class _Built(Exception):
    """The runner reached its AgentLoop construction and the capture stopped it."""


def _capture_construction(monkeypatch) -> dict:
    """Call the real constructor, keep the loop, then stop the runner there.

    ``execute_task`` continues into a live turn after building the loop; the
    contract under test is the construction, so the capture raises as soon as
    the real ``AgentLoop`` has accepted the runner's keywords.
    """
    loop_module = importlib.import_module("raven.agent.loop")
    real = loop_module.AgentLoop
    built: dict = {}

    def capture(*args, **kwargs):
        built["agent"] = real(*args, **kwargs)
        raise _Built

    monkeypatch.setattr(loop_module, "AgentLoop", capture)
    return built


def _write_config(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "model": "bench/model",
                        "provider": "custom",
                        "workspace": str(workspace),
                        "max_tool_iterations": 7,
                        "context_window_tokens": 32_768,
                    }
                },
                "providers": {"custom": {"api_key": "sk-test", "api_base": "http://127.0.0.1:1/v1"}},
                "tools": {"restrict_to_workspace": True},
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_appworld_builds_the_loop_from_the_current_wiring_bundles(tmp_path, monkeypatch) -> None:
    from benchmarks.appworld import agent_cli

    monkeypatch.setattr("raven.providers.factory.make_provider", lambda config, model=None: _failing_provider())
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    config_path = _write_config(tmp_path)

    args = type(
        "Args",
        (),
        {
            "config": str(config_path),
            "workspace": str(tmp_path / "ws"),
            "model": None,
            "env_url": "http://127.0.0.1:1",
            "task_id": "probe_1",
        },
    )()

    agent, exec_tool = agent_cli._build_agent(args)

    assert agent._default_max_iterations == 7
    assert agent.interactive is False
    assert agent.context_window_tokens == 32_768
    assert agent.restrict_to_workspace is True
    assert "execute" in agent.tools
    assert agent.tools.get("execute") is exec_tool
    withheld = agent.tools.withheld_names()
    for retired in ("exec", "read_file", "write_file", "web_search", "spawn"):
        assert retired in withheld


def test_pinchbench_direct_builds_the_loop_from_the_current_wiring_bundles(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    module = _load(PINCH_DIRECT, "raven_executor")
    built = _capture_construction(monkeypatch)

    with pytest.raises(_Built):
        asyncio.run(
            module.execute_task(
                _task(),
                tmp_path / "ws",
                tmp_path / "assets",
                model="bench/model",
                api_key="sk-test",
                api_base="http://127.0.0.1:1/v1",
                provider_name="custom",
            )
        )

    agent = built["agent"]
    assert agent._default_max_iterations == 40
    assert agent.interactive is False
    assert agent.context_window_tokens == 65_536
    assert agent.restrict_to_workspace is True


def test_pinchbench_bot_builds_the_loop_from_the_current_wiring_bundles(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    module = _load(PINCH_BOT, "bot_executor")
    built = _capture_construction(monkeypatch)

    with pytest.raises(_Built):
        asyncio.run(
            module.execute_task(
                _task(),
                tmp_path / "ws",
                tmp_path / "assets",
                model="bench/model",
                api_key="sk-test",
            )
        )

    agent = built["agent"]
    assert agent._default_max_iterations == 40
    assert agent.interactive is False
    assert agent.context_window_tokens == 65_536
    assert agent.restrict_to_workspace is True
