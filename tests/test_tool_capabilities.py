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
from raven.agent.tools.capabilities import (
    CAPABILITIES,
    Need,
    borrowable_credential,
    configured_from,
    has_credential,
    is_configured,
    is_disabled,
    is_offered,
)
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
def _no_ambient_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every credential here also resolves from the environment, so a developer
    who exported one would otherwise see these pass for the wrong reason.

    ``OPENROUTER_API_KEY`` matters as much as the search key: it is a third
    source for the media family, behind the section and the borrow, and a case
    asserting that nothing was available to borrow silently stops testing that
    on any machine that exports it.
    """
    for var in ("SERPER_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def _config(tmp_path: Path):
    """A real Config with genuine defaults, independent of whoever runs this."""
    return load_config(tmp_path / "absent.json")


def _resolved(config, attr):
    return getattr(config.effective_media_config(), attr)


def _media_attrs() -> list[str]:
    """Every media tool the schema declares, asked of the schema.

    Listing them here instead would make the coverage test below blind to the
    one case it exists to catch: a media tool added to the schema and to
    registration but not to the table. A hardcoded list never configures the new
    tool, so it never appears among the gated ones, so the assertion holds while
    the table is already wrong.
    """
    from raven.config.schema import MediaGenConfig, MediaToolConfig

    return [n for n, f in MediaGenConfig.model_fields.items() if f.annotation is MediaToolConfig]


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
    for attr in _media_attrs():
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
    assert configured_from(cap, config) == "borrowed: providers.openrouter.apiKey"


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
            # The row that says "you already have this key" has to name where,
            # and the doctor prints both unguarded -- a blank one renders as an
            # instruction with the answer missing.
            assert cap.env_var, f"{cap.tool}: reuses a credential without naming the variable"
            assert cap.key_path != cap.config_path, f"{cap.tool}: model path doubling as the key path"


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
    # Registered, and unusable: no key resolves from the section, the borrow, or
    # the environment, so every call returns the tool's missing-key error. There
    # is no source to name, and naming the model path -- the only path this
    # capability has -- would tell the deployer a key sits somewhere it does not.
    assert configured_from(cap, config) == ""


@pytest.mark.parametrize(
    ("attr", "tool"),
    [("image", "image_generate"), ("speech", "text_to_speech"), ("video", "video_generate")],
)
def test_a_media_key_is_reported_at_its_own_path_not_the_model_path(attr, tool, workspace, tmp_path: Path) -> None:
    """``config_path`` names the model for this family, so reusing it as the
    credential source sends the deployer to edit a line holding no key."""
    config = _config(tmp_path)
    getattr(config.tools.media, attr).api_key = "sk-tool-own"
    loop = _loop(workspace, config)

    cap = next(c for c in CAPABILITIES if c.tool == tool)
    assert is_configured(cap, config) and loop.tools.has(tool)
    assert configured_from(cap, config) == f"tools.media.{attr}.apiKey"
    assert configured_from(cap, config) != cap.config_path


@pytest.mark.parametrize(
    ("attr", "tool"),
    [("image", "image_generate"), ("speech", "text_to_speech"), ("video", "video_generate")],
)
def test_a_media_key_from_the_environment_is_a_source_like_any_other(
    attr, tool, monkeypatch: pytest.MonkeyPatch, workspace, tmp_path: Path
) -> None:
    """The tool resolves ``OPENROUTER_API_KEY`` at call time, so a report that
    consults only config answers "no credential" for a working install."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")
    config = _config(tmp_path)
    getattr(config.tools.media, attr).model = "some/model"
    loop = _loop(workspace, config)

    cap = next(c for c in CAPABILITIES if c.tool == tool)
    assert is_configured(cap, config) and loop.tools.has(tool)
    assert configured_from(cap, config) == "OPENROUTER_API_KEY"
    # The half that decides whether the row carries a warning. This install
    # works, so calling it keyless would send a deployer to fix what is not
    # broken -- and only the tool's chain reaches the variable to know that.
    assert has_credential(cap, config)


@pytest.mark.parametrize(
    ("attr", "tool"),
    [("image", "image_generate"), ("speech", "text_to_speech"), ("video", "video_generate")],
)
def test_no_borrow_is_claimed_when_there_is_nothing_to_borrow(attr, tool, tmp_path: Path) -> None:
    """The instruction "reuse the key you already have" is wrong when no key
    exists, and wrong in the expensive direction: it is the reason a deployer
    sets a model, gets a registered tool, and sees every call fail."""
    config = _config(tmp_path)
    cap = next(c for c in CAPABILITIES if c.tool == tool)
    assert not config.providers.openrouter.api_key, "this case needs nothing to borrow"

    assert borrowable_credential(cap, config) == ""

    config.providers.openrouter.api_key = "sk-or-test"
    assert borrowable_credential(cap, config) == "providers.openrouter.apiKey"


@pytest.mark.parametrize(
    ("attr", "tool"),
    [("image", "image_generate"), ("speech", "text_to_speech"), ("video", "video_generate")],
)
def test_an_exported_key_is_reusable_too(attr, tool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A deployment that exports the key and configures nothing needs the model
    and nothing else, so telling it to go and set a key is the same misreport in
    the other direction."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")
    config = _config(tmp_path)
    cap = next(c for c in CAPABILITIES if c.tool == tool)
    assert not config.providers.openrouter.api_key, "the environment must be the only source"

    assert borrowable_credential(cap, config) == "OPENROUTER_API_KEY"


def test_only_the_media_family_borrows(tmp_path: Path) -> None:
    """web_search cannot reuse anything -- its row already names what to get --
    and web_fetch needs nothing at all."""
    config = _config(tmp_path)
    config.providers.openrouter.api_key = "sk-or-test"

    for cap in (c for c in CAPABILITIES if not c.media_attr):
        assert borrowable_credential(cap, config) == "", cap.tool


def test_a_switched_off_tool_is_configured_and_still_not_offered(workspace, tmp_path: Path) -> None:
    """The combination the report used to get wrong.

    A key is set, so the credential gate is satisfied and saying "unconfigured"
    would send the deployer to set it again. What decides whether the agent
    holds the tool is `tools.disabledTools`, applied after registration -- so
    the predicate that has to agree with the registry is `is_offered`, not
    `is_configured`.
    """
    config = _config(tmp_path)
    config.tools.web.search.api_key = "sk-serper"
    config.tools.disabled_tools = ["web_search"]
    # Passed in, the way the CLI entry points do: the loop takes the list as an
    # argument rather than reading the config.
    loop = _loop(workspace, config, brave_api_key="sk-serper", disabled_tools=config.tools.disabled_tools)
    cap = next(c for c in CAPABILITIES if c.tool == "web_search")

    assert loop.tools.has("web_search") is False
    assert is_configured(cap, config) is True
    assert is_disabled(cap, config) is True
    assert is_offered(cap, config) is loop.tools.has("web_search")


@pytest.mark.parametrize("cap", CAPABILITIES, ids=lambda c: c.tool)
def test_being_offered_matches_the_registry_for_every_capability(cap, workspace, tmp_path: Path) -> None:
    """Every entry, switched on and then off by name, against the real loop.

    Parametrised rather than written for web_search alone: the disabled list
    covers any tool, and a family that stops agreeing is exactly the drift the
    rest of this file exists to catch.
    """
    config = _config(tmp_path)
    config.tools.web.search.api_key = "sk-serper"
    for attr in _media_attrs():
        getattr(config.tools.media, attr).model = "some/model"
    config.providers.openrouter.api_key = "sk-or-test"

    on = _loop(workspace, config, brave_api_key="sk-serper")
    assert is_offered(cap, config) is on.tools.has(cap.tool)

    config.tools.disabled_tools = [cap.tool]
    off = _loop(workspace, config, brave_api_key="sk-serper", disabled_tools=config.tools.disabled_tools)
    assert is_offered(cap, config) is off.tools.has(cap.tool)
    assert off.tools.has(cap.tool) is False
