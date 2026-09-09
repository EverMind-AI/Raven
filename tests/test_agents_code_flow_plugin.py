"""The code-flow plugin's wiring: the manifest, the hook factory, admission.

The plugin's one seat is the turn-frame hook. This file pins the manifest
rows, that the registry serves the real factory string, and the D6 admission
from the rendered config slice: no slice or a disabled slice casts nothing,
an enabled slice casts the hook, a malformed slice declines with a warning.
"""

import sys
from pathlib import Path

from raven.contracts.loop_hooks import AgentHook
from raven.contracts.session_events import SessionObserver
from raven.plugins import DiscoveredPlugin, ManifestOrigin, PluginManifest, PluginRegistry
from raven.plugins.context import PluginContext, ServiceLocator

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-code" / "plugins" / "code-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from code_flow.flow import CodeFlowHook, SessionForget, make_flow_hook, make_session_observer  # noqa: E402

MANIFEST_PATH = PLUGIN_DIR / "raven-plugin.toml"


def ctx_for(tmp_path: Path, config: dict) -> PluginContext:
    return PluginContext(
        config=config,
        services=ServiceLocator(workspace=tmp_path / "home", user_id="u", agent_id="a"),
    )


def test_the_manifest_declares_the_hook_and_the_observer():
    mf = PluginManifest.from_toml_path(MANIFEST_PATH)
    assert mf.id == "code-flow"
    assert [h.name for h in mf.contributes.hooks] == ["code_flow"]
    assert [o.name for o in mf.contributes.session_observers] == ["session_forget"]
    assert mf.contributes.tool_gates == [], "the write gate retired with worktree isolation"
    assert mf.contributes.tools == []


def test_the_registry_builds_the_hook_from_the_real_factory_string(tmp_path):
    """``location`` is the manifest path, the discovery convention
    (discover.py sets location=manifest_path; activation appends
    location.parent to sys.path) -- so the registry's own importability
    machinery is what serves the factory string here."""
    reg = PluginRegistry()
    mf = PluginManifest.from_toml_path(MANIFEST_PATH)
    reg.activate([DiscoveredPlugin(manifest=mf, source=ManifestOrigin.USER, location=MANIFEST_PATH)])
    assert reg.hook_names() == ["code_flow"]
    assert reg.tool_gate_names() == []
    assert reg.session_observer_names() == ["session_forget"]
    services = ServiceLocator(workspace=tmp_path / "home", user_id="u", agent_id="a")
    observer = reg.build_session_observer("session_forget", config={"enabled": True}, services=services)
    assert isinstance(observer, SessionForget)
    assert isinstance(observer, SessionObserver)
    built = reg.build_hook(
        "code_flow",
        config={"enabled": True},
        services=ServiceLocator(workspace=tmp_path / "home", user_id="u", agent_id="a"),
    )
    assert isinstance(built, CodeFlowHook)
    assert isinstance(built, AgentHook)
    assert built.name == "code_flow"


def test_an_absent_slice_casts_no_surface_at_all(tmp_path):
    """D6: no config means no hook and no observer."""
    assert make_flow_hook(ctx_for(tmp_path, {})) is None
    assert make_session_observer(ctx_for(tmp_path, {})) is None


def test_a_disabled_slice_casts_no_surface(tmp_path):
    assert make_flow_hook(ctx_for(tmp_path, {"enabled": False})) is None
    assert make_session_observer(ctx_for(tmp_path, {"enabled": False})) is None


def test_an_enabled_slice_casts_the_hook(tmp_path):
    assert isinstance(make_flow_hook(ctx_for(tmp_path, {"enabled": True})), CodeFlowHook)


def test_unknown_slice_keys_are_ignored_not_refused(tmp_path):
    """The launcher owns the slice spellings; a key this version does not know
    (an older render's ``workspaceGate``, a newer one's knob) must not turn the
    product off."""
    slice_ = {"enabled": True, "workspaceGate": {"allocBase": str(tmp_path)}, "laterKnob": 1}
    assert isinstance(make_flow_hook(ctx_for(tmp_path, slice_)), CodeFlowHook)


def test_a_malformed_slice_declines_the_hook_and_the_observer(tmp_path):
    assert make_flow_hook(ctx_for(tmp_path, {"enabled": "not-a-bool"})) is None
    assert make_session_observer(ctx_for(tmp_path, {"enabled": "not-a-bool"})) is None
