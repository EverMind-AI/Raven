"""The headless research driver: the environment the launched agent receives.

The launch starts from the login shell's environment, which holds what the
user's profile exports and nothing a caller exported in the process that
started the driver. So the driver has to hand the launcher the settings it
honours, and the launcher's own tables say which ones those are: the selected
vendor's key, the host model's key, the provider selection, and the host home.
This file pins that map - for several vendors, for the settings that are not a
vendor's key, and through the driver's own entry point, which is driven with
its launch stubbed so no model is called.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DRIVER = REPO / "agents" / "raven-research" / "run_research.py"


@pytest.fixture()
def driver():
    spec = importlib.util.spec_from_file_location("agents_research_run_research", DRIVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    ("vendor", "key_var"),
    [
        ("tavily", "RESEARCH_TAVILY_API_KEY"),
        ("brave", "RESEARCH_BRAVE_API_KEY"),
        ("exa", "RESEARCH_EXA_API_KEY"),
        ("serper", "RESEARCH_SERPER_API_KEY"),
        ("tinyfish", "RESEARCH_TINYFISH_API_KEY"),
    ],
)
def test_the_selected_vendors_key_reaches_the_agent(driver, monkeypatch, vendor, key_var):
    """Selecting a vendor must bring that vendor's key with it."""
    monkeypatch.setenv("RESEARCH_WEB_SEARCH_PROVIDER", vendor)
    monkeypatch.setenv(key_var, f"{vendor}-key")

    env = driver.agent_env()

    assert env["RESEARCH_WEB_SEARCH_PROVIDER"] == vendor
    assert env[key_var] == f"{vendor}-key"


def test_every_vendor_the_launcher_names_reaches_the_agent(driver, monkeypatch):
    """No vendor is special: the launcher's own table says which keys exist."""
    table = driver.launcher().WEB_VENDOR_ENV_VARS
    assert table, "the launcher names no vendor"

    for vendor, env_var in table.items():
        monkeypatch.setenv(f"RESEARCH_{env_var}", f"{vendor}-key")

    env = driver.agent_env()

    for vendor, env_var in table.items():
        assert env[f"RESEARCH_{env_var}"] == f"{vendor}-key"


def test_the_host_model_key_and_the_host_home_reach_the_agent(driver, monkeypatch, tmp_path):
    """The launcher's documented host-model and OAuth inheritance needs both."""
    home = tmp_path / "raven-home"
    monkeypatch.setenv("RESEARCH_API_KEY", "own-model-key")
    monkeypatch.setenv("RAVEN_HOME", str(home))

    env = driver.agent_env()

    assert env["RESEARCH_API_KEY"] == "own-model-key"
    assert env["RAVEN_HOME"] == str(home)
    assert env["MINIMAX_OAUTH_TOKEN_DIR"] == str(home / "oauth")


def test_the_driver_hands_the_agent_the_selected_vendors_key(driver, monkeypatch, tmp_path):
    """The driver's own entry point, with the launch stubbed out."""
    seen: dict[str, str] = {}

    async def fake_drive(task, env, traces, mode):
        seen.update(env)
        return "the answer"

    monkeypatch.setattr(driver, "_drive", fake_drive)
    monkeypatch.setenv("RAVEN_TRACING_DIR", "seed")
    monkeypatch.setenv("RESEARCH_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("RESEARCH_TAVILY_API_KEY", "tavily-key")
    monkeypatch.setenv("RESEARCH_API_KEY", "own-model-key")
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_research.py", "--task", "why", "--traces", str(tmp_path / "traces")],
    )

    assert driver.main() == 0

    assert seen["RESEARCH_WEB_SEARCH_PROVIDER"] == "tavily"
    assert seen["RESEARCH_TAVILY_API_KEY"] == "tavily-key"
    assert seen["RESEARCH_API_KEY"] == "own-model-key"
    assert seen["RAVEN_TRACING_DIR"] == str(tmp_path / "traces")


def test_the_launcher_starts_on_the_key_the_driver_hands_it(driver, monkeypatch, tmp_path):
    """The launcher's own refusal, with the driver's environment in place.

    The launcher refuses to start when the SELECTED search vendor has no key,
    and it reads the key from the environment the driver hands it. So the
    driver's map, applied to the environment, has to carry the selected
    vendor's key - otherwise the run the driver asks for never happens.
    """
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RESEARCH_NG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("RESEARCH_API_KEY", "own-model-key")
    monkeypatch.setenv("RESEARCH_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("RESEARCH_TAVILY_API_KEY", "tavily-key")
    for name, value in driver.agent_env().items():
        monkeypatch.setenv(name, value)

    rendered = driver.launcher().render_config(DRIVER.parent / "config.json")

    config = json.loads(Path(rendered).read_text(encoding="utf-8"))
    assert config["plugins"]["config"]["research-flow"]["search"]["provider"] == "tavily"
    assert config["plugins"]["config"]["research-flow"]["search"]["apiKey"] == "tavily-key"

