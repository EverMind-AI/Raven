"""Session modes: composing the profiles, and switching a session between them."""

from types import SimpleNamespace

import pytest

from raven.acp import protocol
from raven.acp.methods import AcpMethods
from raven.acp.modes import _deep_merge, build_session_modes, resolve_profiles
from raven.acp.spine import AcpSessions
from raven.config.raven import DRFlowConfig
from raven.config.schema import AcpConfig


def _config(modes: dict | None = None, default: str | None = None, max_tool_iterations: int = 40):
    return SimpleNamespace(
        acp=AcpConfig.model_validate({"modes": modes or {}, "defaultMode": default}),
        agents=SimpleNamespace(defaults=SimpleNamespace(max_tool_iterations=max_tool_iterations)),
    )


def _ec_config(**dr_flow):
    return SimpleNamespace(dr_flow=DRFlowConfig.model_validate({"enabled": True, **dr_flow}))


_CATALOGUE = {
    "fast": {"name": "Fast", "description": "converges early", "drFlow": {}},
    "deep": {
        "name": "Deep",
        "description": "searches longer",
        "drFlow": {"maxIterations": 30, "sufficiency": {"minSearches": 5}},
        "maxToolIterations": 60,
    },
    "ultra": {
        "name": "Ultra",
        "description": "exhaustive",
        "drFlow": {"maxIterations": None, "sufficiency": {"enabled": False}},
        "maxToolIterations": 150,
    },
}


# -- the merge ---------------------------------------------------------------


def test_dicts_recurse_and_siblings_the_overlay_never_names_survive():
    base = {"sufficiency": {"enabled": True, "minSearches": 1, "minFetches": 2}, "identityOverride": "the one prompt"}

    merged = _deep_merge(base, {"sufficiency": {"minSearches": 5}})

    assert merged["sufficiency"] == {"enabled": True, "minSearches": 5, "minFetches": 2}
    assert merged["identityOverride"] == "the one prompt"


def test_an_explicit_null_lands_as_null():
    """Ultra clears the iteration cap by writing null, which the schema reads as
    "back to the built-in default" - so it must survive rather than read as absent."""
    merged = _deep_merge({"maxIterations": 20}, {"maxIterations": None})

    assert "maxIterations" in merged and merged["maxIterations"] is None


def test_a_list_the_overlay_touches_is_replaced_whole():
    merged = _deep_merge({"toolsAllowlist": ["a", "b", "c"]}, {"toolsAllowlist": ["a"]})

    assert merged["toolsAllowlist"] == ["a"]


def test_the_baseline_is_never_mutated():
    """One dump is merged against once per mode, so an in-place merge would
    compose every overlay onto the one before it."""
    base = {"sufficiency": {"minSearches": 1}}

    _deep_merge(base, {"sufficiency": {"minSearches": 5}})
    second = _deep_merge(base, {"sufficiency": {"minSearches": 9}})

    assert base["sufficiency"]["minSearches"] == 1
    assert second["sufficiency"]["minSearches"] == 9


# -- resolution --------------------------------------------------------------


def test_every_mode_inherits_the_baseline_it_does_not_name():
    """The point of carrying diffs: the identity prompt exists once, in the
    config, and reaches every profile from there."""
    ec_config = _ec_config(identityOverride="the one prompt", maxIterations=20)

    profiles = resolve_profiles(_config(_CATALOGUE), ec_config)

    assert set(profiles) == {"fast", "deep", "ultra"}
    assert all(p.dr_flow.identity_override == "the one prompt" for p in profiles.values())
    assert profiles["fast"].dr_flow.max_iterations == 20
    assert profiles["deep"].dr_flow.max_iterations == 30
    assert profiles["ultra"].dr_flow.max_iterations is None
    assert profiles["ultra"].dr_flow.sufficiency.enabled is False


def test_a_mode_that_names_no_tool_ceiling_inherits_the_config_s():
    profiles = resolve_profiles(_config(_CATALOGUE, max_tool_iterations=40), _ec_config())

    assert profiles["fast"].max_iterations == 40
    assert profiles["deep"].max_iterations == 60


def test_an_overlay_the_schema_rejects_fails_at_startup_naming_its_mode():
    """``extra='forbid'`` on the DR schema is what turns a typo into this. It has
    to happen here rather than at the session/set_mode that first reaches it: by
    then a client has already been shown the mode as available."""
    broken = {"deep": {"name": "Deep", "drFlow": {"maxIteratons": 30}}}

    with pytest.raises(ValueError, match="acp.modes.deep.drFlow"):
        resolve_profiles(_config(broken), _ec_config())


def test_declaring_no_modes_resolves_to_none():
    assert resolve_profiles(_config(), _ec_config()) == {}


def test_a_default_naming_no_declared_mode_is_refused_by_the_schema():
    with pytest.raises(ValueError, match="defaultMode"):
        AcpConfig.model_validate({"modes": {"fast": {"name": "Fast"}}, "defaultMode": "deep"})


# -- the table ---------------------------------------------------------------


def test_a_session_starts_in_the_declared_default():
    modes = build_session_modes(_config(_CATALOGUE, default="deep"), _ec_config())

    assert modes.enabled and modes.default == "deep"
    assert modes.current("acp:new") == "deep"
    assert modes.profile("acp:new").max_iterations == 60


def test_no_declared_default_takes_the_first_entry():
    modes = build_session_modes(_config(_CATALOGUE), _ec_config())

    assert modes.default == "fast"


def test_a_switch_moves_only_the_session_that_asked():
    # The gate is off in the upstream schema and on in this product's config, so
    # the baseline has to say so or "ultra turned it off" proves nothing.
    modes = build_session_modes(_config(_CATALOGUE, default="fast"), _ec_config(sufficiency={"enabled": True}))

    modes.set("acp:a", "ultra")

    assert modes.current("acp:a") == "ultra"
    assert modes.current("acp:b") == "fast"
    assert modes.profile("acp:a").dr_flow.sufficiency.enabled is False
    assert modes.profile("acp:b").dr_flow.sufficiency.enabled is True


def test_an_unknown_mode_is_refused_and_leaves_the_session_where_it_was():
    modes = build_session_modes(_config(_CATALOGUE, default="fast"), _ec_config())

    with pytest.raises(KeyError):
        modes.set("acp:a", "turbo")

    assert modes.current("acp:a") == "fast"


def test_with_nothing_declared_the_baseline_is_what_a_session_runs_on():
    """The degradation path: a deployment that declares no modes gets the
    config's own drFlow and iteration cap, which is what build_loop passed
    before this surface existed."""
    ec_config = _ec_config(maxIterations=7)
    modes = build_session_modes(_config(max_tool_iterations=11), ec_config)

    assert not modes.enabled
    assert modes.state("acp:a") is None
    assert modes.profile("acp:a").dr_flow is ec_config.dr_flow
    assert modes.profile("acp:a").max_iterations == 11


def test_the_wire_state_carries_the_catalogue_and_the_current_id():
    modes = build_session_modes(_config(_CATALOGUE, default="fast"), _ec_config())
    modes.set("acp:a", "deep")

    state = modes.state("acp:a")

    assert state["currentModeId"] == "deep"
    assert [m["id"] for m in state["availableModes"]] == ["fast", "deep", "ultra"]
    assert state["availableModes"][1] == {"id": "deep", "name": "Deep", "description": "searches longer"}


# -- the protocol ------------------------------------------------------------


class _Harness:
    def __init__(self, modes=None):
        self.frames: list[dict] = []
        self.sessions = AcpSessions()
        self.methods = AcpMethods(
            submit=lambda req: None,
            sessions=self.sessions,
            emit=self.frames.append,
            on_session_open=self._open,
            modes=modes,
        )
        self.opened: list[str] = []

    async def _open(self, session_id):
        self.opened.append(session_id)

    async def call(self, method, params=None, *, request_id=1):
        frame = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        return await self.methods.handle(frame)

    async def new_session(self):
        await self.call("initialize", {"protocolVersion": 1})
        return await self.call("session/new", {"cwd": "/tmp", "mcpServers": []})


async def test_session_new_advertises_the_catalogue():
    modes = build_session_modes(_config(_CATALOGUE, default="deep"), _ec_config())
    harness = _Harness(modes)

    result = (await harness.new_session())["result"]

    assert result["modes"]["currentModeId"] == "deep"
    assert [m["id"] for m in result["modes"]["availableModes"]] == ["fast", "deep", "ultra"]


async def test_set_mode_moves_the_session_and_shows_in_the_next_session_response():
    modes = build_session_modes(_config(_CATALOGUE, default="fast"), _ec_config())
    harness = _Harness(modes)
    session_id = (await harness.new_session())["result"]["sessionId"]

    response = await harness.call("session/set_mode", {"sessionId": session_id, "modeId": "ultra"})

    assert response["result"] == {}
    assert modes.current(session_id) == "ultra"


async def test_an_unknown_mode_names_what_is_available():
    modes = build_session_modes(_config(_CATALOGUE, default="fast"), _ec_config())
    harness = _Harness(modes)
    session_id = (await harness.new_session())["result"]["sessionId"]

    error = (await harness.call("session/set_mode", {"sessionId": session_id, "modeId": "turbo"}))["error"]

    assert error["code"] == protocol.INVALID_PARAMS
    assert error["data"]["availableModes"] == ["fast", "deep", "ultra"]
    assert modes.current(session_id) == "fast"


async def test_a_missing_mode_id_is_an_invalid_param_not_a_crash():
    harness = _Harness(build_session_modes(_config(_CATALOGUE), _ec_config()))
    session_id = (await harness.new_session())["result"]["sessionId"]

    error = (await harness.call("session/set_mode", {"sessionId": session_id}))["error"]

    assert error["code"] == protocol.INVALID_PARAMS
    assert error["data"]["field"] == "modeId"


async def test_an_unknown_session_is_answered_before_the_mode_is_read():
    """-32002 rather than a mode error about a session that does not exist."""
    harness = _Harness(build_session_modes(_config(_CATALOGUE), _ec_config()))
    await harness.call("initialize", {"protocolVersion": 1})

    error = (await harness.call("session/set_mode", {"sessionId": "acp:gone", "modeId": "deep"}))["error"]

    assert error["code"] == protocol.RESOURCE_NOT_FOUND


async def test_a_build_declaring_no_modes_keeps_the_pre_modes_wire():
    """No ``modes`` object on the session response, and set_mode stays
    not-implemented -- which reads as a surface this deployment did not turn on,
    where "unknown method" would read as a version mismatch."""
    harness = _Harness(build_session_modes(_config(), _ec_config()))
    result = (await harness.new_session())["result"]

    assert "modes" not in result

    error = (await harness.call("session/set_mode", {"sessionId": result["sessionId"], "modeId": "deep"}))["error"]

    assert error["code"] == protocol.METHOD_NOT_FOUND
    assert "not implemented" in error["message"]
