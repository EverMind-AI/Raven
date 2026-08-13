"""The capability table must describe what AgentLoop actually does.

A second description of an existing rule is only worth having while it stays
true. These drive a real ``AgentLoop`` and compare what it registered against
what the table predicts, for every combination that changes an answer -- so the
table cannot quietly become a third opinion about which tools are available.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.tools.capabilities import CAPABILITIES, Need, configured_from, is_configured
from raven.config.loader import load_config
from raven.providers.base import LLMProvider, LLMResponse


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
        return LLMResponse(content="stub", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture(autouse=True)
def _no_ambient_serper_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """web_search resolves its key from the environment too, so a developer who
    exported one would otherwise see these pass for the wrong reason."""
    monkeypatch.delenv("SERPER_API_KEY", raising=False)


def _config(tmp_path: Path):
    """A real Config with genuine defaults, independent of whoever runs this."""
    return load_config(tmp_path / "absent.json")


def _resolved(config, attr):
    return getattr(config.effective_media_config(), attr)


def _loop(workspace: Path, config, **kw) -> AgentLoop:
    return AgentLoop(
        provider=_StubProvider(),
        workspace=workspace,
        model="stub",
        media_config=config.effective_media_config(),
        **kw,
    )


def test_the_table_names_exactly_the_credential_gated_tools(workspace, tmp_path: Path) -> None:
    """Derived rather than listed: the tools that appear only once credentials
    are supplied *are* the credential-gated ones, so this catches both halves --
    a table entry for a tool that no longer exists, and a newly gated tool whose
    author forgot the table. The second is the one that puts a deployer back to
    guessing what is missing."""
    bare = _config(tmp_path)
    loop_bare = _loop(workspace, bare)

    full = _config(tmp_path)
    full.tools.web.search.api_key = "sk-serper"
    for attr in ("image", "speech", "video"):
        getattr(full.tools.media, attr).model = "some/model"
    full.providers.openrouter.api_key = "sk-or-test"
    loop_full = _loop(workspace, full, brave_api_key="sk-serper")

    gated = set(loop_full.tools.tool_names) - set(loop_bare.tools.tool_names)
    declared = {c.tool for c in CAPABILITIES if c.need is not Need.NOTHING}

    assert gated == declared, (
        f"gated but undeclared: {sorted(gated - declared)}; declared but not gated: {sorted(declared - gated)}"
    )


@pytest.mark.parametrize("cap", CAPABILITIES, ids=lambda c: c.tool)
def test_the_table_agrees_with_the_loop_when_unconfigured(cap, workspace, tmp_path: Path) -> None:
    config = _config(tmp_path)
    loop = _loop(workspace, config)

    assert is_configured(cap, config) is loop.tools.has(cap.tool), (
        f"{cap.tool}: table says configured={is_configured(cap, config)}, loop registered={loop.tools.has(cap.tool)}"
    )


def test_a_configured_search_key_agrees_on_both_sides(workspace, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.tools.web.search.api_key = "sk-serper"
    loop = _loop(workspace, config, brave_api_key="sk-serper")

    cap = next(c for c in CAPABILITIES if c.tool == "web_search")
    assert is_configured(cap, config) and loop.tools.has("web_search")
    assert configured_from(cap, config) == "tools.web.search.apiKey"


def test_the_env_var_alone_agrees_on_both_sides(workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The half of the deploys a config-only reading would answer wrong."""
    monkeypatch.setenv("SERPER_API_KEY", "sk-from-env")
    config = _config(tmp_path)
    loop = _loop(workspace, config)

    cap = next(c for c in CAPABILITIES if c.tool == "web_search")
    assert is_configured(cap, config) and loop.tools.has("web_search")
    assert configured_from(cap, config) == "SERPER_API_KEY"


@pytest.mark.parametrize(
    ("attr", "tool"),
    [("image", "image_generate"), ("speech", "text_to_speech"), ("video", "video_generate")],
)
def test_a_media_model_alone_agrees_on_both_sides(attr, tool, workspace, tmp_path: Path) -> None:
    """A model with no key is the switch-on case: the key is borrowed."""
    config = _config(tmp_path)
    getattr(config.tools.media, attr).model = "some/model"
    config.providers.openrouter.api_key = "sk-or-test"
    loop = _loop(workspace, config)

    cap = next(c for c in CAPABILITIES if c.tool == tool)
    assert is_configured(cap, config) and loop.tools.has(tool)
    assert configured_from(cap, config) == "providers.openrouter.apiKey (borrowed)"


def test_an_openrouter_key_alone_switches_nothing_on(workspace, tmp_path: Path) -> None:
    """The property that makes the media rule "model *or* key" rather than
    "resolved key": a chat credential must not silently enable three paid tools."""
    config = _config(tmp_path)
    config.providers.openrouter.api_key = "sk-or-test"
    loop = _loop(workspace, config)

    for cap in (c for c in CAPABILITIES if c.media_attr):
        assert not is_configured(cap, config), cap.tool
        assert not loop.tools.has(cap.tool), cap.tool


def test_the_free_capability_needs_no_credential(workspace, tmp_path: Path) -> None:
    config = _config(tmp_path)
    loop = _loop(workspace, config)

    cap = next(c for c in CAPABILITIES if c.need is Need.NOTHING)
    assert is_configured(cap, config) and loop.tools.has(cap.tool)
    assert configured_from(cap, config) == "", "nothing was required, so nothing supplied it"


def test_every_entry_carries_what_a_deployer_has_to_act_on() -> None:
    """The table is read by someone deciding what to go and do, so the fields
    that tell them are not optional for the needs that require action."""
    for cap in CAPABILITIES:
        assert cap.summary and cap.tool
        if cap.need is Need.NEW_ACCOUNT:
            assert cap.obtain_from, f"{cap.tool}: no url for an account the deployer must create"
            assert cap.config_path, f"{cap.tool}: no config path to put the key in"
        if cap.need is Need.OWN_CREDENTIAL:
            assert cap.cost_note, f"{cap.tool}: switched on without saying it bills per call"


@pytest.mark.parametrize(
    ("attr", "tool"),
    [("image", "image_generate"), ("speech", "text_to_speech"), ("video", "video_generate")],
)
def test_a_media_model_with_no_key_to_borrow_still_counts(attr, tool, workspace, tmp_path: Path) -> None:
    """The half of the media rule the borrow hides.

    With an OpenRouter key present, `effective_media_config` fills `api_key` in,
    so a rule reading only the key still answers correctly and a test that sets
    both proves nothing about the `or model` half. With no key to borrow, the
    model is the only thing making this configured -- and the loop registers it,
    so the table must agree.
    """
    config = _config(tmp_path)
    getattr(config.tools.media, attr).model = "some/model"
    assert not config.providers.openrouter.api_key, "this case needs nothing to borrow"
    loop = _loop(workspace, config)

    cap = next(c for c in CAPABILITIES if c.tool == tool)
    assert not _resolved(config, attr).api_key, "nothing should have been borrowed"
    assert is_configured(cap, config) and loop.tools.has(tool)
    assert configured_from(cap, config) == cap.config_path
