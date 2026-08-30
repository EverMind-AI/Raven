"""The agents/ pilot launcher: rendering, refusals, identity, and the exec.

The B-side product must hold the same launch contract as its vendored twin
while consuming installed raven: secrets merge into a rendered 0600 config
whose parent decides the data dir, the workspace is pinned and seeded with
the identity, refusals fire before a server that could not work starts, and
the exec targets ``python -m raven acp``. The strongest pin is the loader
round-trip: what the launcher renders, trunk raven's own loader loads.
"""

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

#: The vendored twin's visible tool face -- its config disables everything
#: else its fork registers. The B side must show exactly this face: trunk
#: grew tools the fork never had (the deep_research offer stub above all,
#: which would have a research agent offering to outsource research), and
#: every one of them must be disabled by the product config, not by luck.
VENDORED_TOOL_FACE = {
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "grep",
    "find",
    "web_search",
    "web_fetch",
    "ask_user",
}

REPO = Path(__file__).resolve().parent.parent
RUN_PY = REPO / "agents" / "raven-research" / "run.py"


@pytest.fixture()
def launcher():
    spec = importlib.util.spec_from_file_location("agents_research_run", RUN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def grounded(launcher, tmp_path, monkeypatch):
    """A launcher pointed at a scratch home and state root, secrets set."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RESEARCH_NG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("RESEARCH_API_KEY", "sk-own")
    monkeypatch.setenv("RESEARCH_SERPER_API_KEY", "serper-key")
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.delenv("RESEARCH_JINA_API_KEY", raising=False)
    return launcher


def test_the_render_merges_secrets_and_pins_the_workspace(grounded, tmp_path):
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    data = json.loads(rendered.read_text())
    assert data["providers"]["openrouter"]["apiKey"] == "sk-own"
    assert data["tools"]["web"]["search"]["apiKey"] == "serper-key"
    assert data["agents"]["defaults"]["workspace"] == str(tmp_path / "state" / "workspace")
    assert rendered.parent == tmp_path / "state"


def test_the_rendered_file_is_owner_only(grounded):
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    assert stat.S_IMODE(rendered.stat().st_mode) == 0o600


def test_the_render_loads_through_trunks_own_loader(grounded):
    from raven.config.loader import load_config

    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    config = load_config(rendered)
    assert config.agents.defaults.model == "openai/gpt-5.6-sol-pro"
    assert config.tools.web.search.api_key == "serper-key"


def test_the_identity_is_seeded_once_and_never_overwritten(grounded, tmp_path):
    grounded.render_config(RUN_PY.parent / "config.json")
    soul = tmp_path / "state" / "workspace" / "agent_memory" / "profile" / "soul.md"
    assert "research agent" in soul.read_text()
    soul.write_text("operator tuned")
    grounded.render_config(RUN_PY.parent / "config.json")
    assert soul.read_text() == "operator tuned"


def test_no_llm_key_anywhere_refuses_before_serving(grounded, monkeypatch):
    monkeypatch.delenv("RESEARCH_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        grounded.render_config(RUN_PY.parent / "config.json")


def test_the_llm_inherits_from_the_host_config(grounded, tmp_path, monkeypatch):
    monkeypatch.delenv("RESEARCH_API_KEY", raising=False)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps(
            {
                "providers": {"anthropic": {"apiKey": "sk-host"}},
                "agents": {"defaults": {"provider": "anthropic", "model": "claude-sonnet-5"}},
            }
        )
    )
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    data = json.loads(rendered.read_text())
    assert data["providers"]["anthropic"]["apiKey"] == "sk-host"
    assert data["agents"]["defaults"]["model"] == "claude-sonnet-5"
    assert data["agents"]["defaults"]["maxToolIterations"] == 40


def test_missing_search_key_refuses(grounded, monkeypatch):
    monkeypatch.delenv("RESEARCH_SERPER_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        grounded.render_config(RUN_PY.parent / "config.json")


def test_the_search_env_var_is_the_tools_own(launcher):
    import inspect

    import raven.agent.tools.web as web

    assert launcher.SEARCH_ENV_VAR in inspect.getsource(web)


def test_the_exec_targets_installed_raven(grounded, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "execv", lambda *a: calls.append(a))
    monkeypatch.setattr(sys, "argv", ["run.py"])
    with pytest.raises(AssertionError):
        grounded.main()
    ((binary, argv),) = [(c[0], c[1]) for c in calls]
    assert binary == sys.executable
    assert argv[:4] == [sys.executable, "-m", "raven", "acp"]
    assert argv[4] == "--config"


def _quiet_plugins(tmp_path, monkeypatch):
    from raven.core import plugin_stack

    monkeypatch.setattr(
        plugin_stack,
        "plugin_discovery_sources",
        lambda: {
            "bundled_dir": tmp_path / "none",
            "user_dir": tmp_path / "none",
            "project_dir": tmp_path / "none",
            "entry_points_group": None,
        },
    )


def test_the_pilots_tool_face_equals_the_vendored_twins(grounded, tmp_path, monkeypatch):
    """Build the loop from the rendered config; the model-visible tool set is
    the fork's nine and nothing more."""
    from raven.config.loader import load_config
    from raven.config.raven import RavenConfig
    from raven.contracts.llm_provider import LLMResponse
    from raven.core import runtime
    from raven.providers.base import LLMProvider

    class _StubProvider(LLMProvider):
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

    _quiet_plugins(tmp_path, monkeypatch)
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    # The acp entrance's first act is set_config_path(rendered) -- the live
    # config (which serves disabledTools) reads that path, so the test does
    # what the transport does.
    import raven.home as home

    monkeypatch.setattr(home, "_current_config_path", rendered)
    config = load_config(rendered)
    rt = runtime.build_runtime(config, RavenConfig(), provider=_StubProvider())
    try:
        visible = {d["function"]["name"] for d in rt.loop.tools.get_definitions()}
    finally:
        rt.discard()
    assert visible == VENDORED_TOOL_FACE
