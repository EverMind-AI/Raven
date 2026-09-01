"""The contribution surface's ledger: the plugin-facing vocabulary, pinned.

``ServiceLocator`` and ``RuntimeHandles`` are papers now
(``raven.contracts.plugin_surface``, under the contract tier's version and
ledger); ``PluginManifest`` stays plugin-side because paper-izing a pydantic
model would pull pydantic into the kernel closure. This file keeps what the
tier ledger does not pin: the exact field rosters, the frozenness, the
manifest vocabulary, and the re-export address plugin authors import from.
"""

from __future__ import annotations

import dataclasses


def test_the_service_locator_grants_are_the_ledgered_five() -> None:
    from raven.plugins.context import ServiceLocator

    assert sorted(f.name for f in dataclasses.fields(ServiceLocator)) == [
        "agent_id",
        "notify",
        "provider",
        "user_id",
        "workspace",
    ], "a new field here is a new grant to every plugin factory: ledger it and say why"
    assert ServiceLocator.__dataclass_params__.frozen, "grants are handed over, never handed back"


def test_the_runtime_handles_grants_are_ledgered() -> None:
    from raven.plugins.context import RuntimeHandles

    assert sorted(f.name for f in dataclasses.fields(RuntimeHandles)) == [
        # The loop's own user-question face, lent to a bound holder. A BIG
        # grant: whoever holds it can interrupt the user.
        "direct_ask",
        "playbook_runtime",
        # Repoints one session's working root, durable truth first. A BIG
        # grant: whoever holds it moves where every subsequent write lands.
        "rebind_workdir",
        "session_dir",
        "subagent_registry",
        "subagents_paused",
        # Keyed one-shot wakes on the host scheduler, namespaced to the
        # contributing plugin (paper: contracts/scheduling.py).
        "wake_scheduler",
    ], "a new field here is a new late-bound grant: ledger it and name the power it hands over"
    assert RuntimeHandles.__dataclass_params__.frozen, "grants are handed over, never handed back"


def test_the_manifest_kinds_are_ledgered() -> None:
    from raven.plugins.manifest import Contributes, PluginManifest

    assert sorted(Contributes.model_fields) == [
        "hooks",
        "memory_backends",
        "services",
        "tool_gates",
        "tools",
    ], (
        "a new contribution kind changes what every raven-plugin.toml can say: "
        "ledger it here in the change that teaches the registry to consume it"
    )
    assert sorted(PluginManifest.model_fields) == [
        "bundled",
        "config_schema",
        "contributes",
        "display_name",
        "enabled_by_default",
        "id",
        "raven",
        "version",
    ], "the manifest header is the plugin file format: a new key is a format change, ledger it"


def test_the_decline_vocabulary_is_two_shapes() -> None:
    """A factory declines by returning None (pinned by the plugin-tools
    tests); a binder declines by raising the one sanctioned exception. Both
    are configuration facts, not failures -- the ledger pins that the
    exception exists on the contribution surface and is an Exception a loop
    can catch narrowly.
    """
    from raven.plugins.context import BindDeclinedError

    assert issubclass(BindDeclinedError, Exception)
    assert not issubclass(BindDeclinedError, (ValueError, TypeError, RuntimeError)), (
        "the decline must stay its own class: a loop catches it narrowly, and riding a "
        "builtin would catch real bugs as declines"
    )


def test_the_documented_import_address_serves_the_papers_objects() -> None:
    """Plugin authors import from ``raven.plugins.context``; the definitions
    live in the papers. Both spellings must hand out the same objects, or two
    half-surfaces drift apart under one name."""
    from raven.contracts import plugin_surface
    from raven.plugins import context

    assert context.ServiceLocator is plugin_surface.ServiceLocator
    assert context.RuntimeHandles is plugin_surface.RuntimeHandles
    assert context.BindDeclinedError is plugin_surface.BindDeclinedError


def test_a_service_contribution_parses_registers_and_builds(tmp_path) -> None:
    """[seam-2] The fourth kind travels the same road as the other three:
    manifest row -> activation (duplicate names refused across plugins) ->
    factory call with a fresh PluginContext -> an inert object the host may
    start. The paper's face is what the built object satisfies."""
    import textwrap
    from types import SimpleNamespace

    from raven.contracts.services import PluginService
    from raven.plugins.manifest import PluginManifest

    toml = textwrap.dedent("""
        [plugin]
        id = "watcher-plug"
        version = "0.1.0"
        [[plugin.contributes.services]]
        name = "event_watcher"
        factory = "raven.plugins.manifest:PluginManifest"
    """)
    mf = PluginManifest.from_toml_str(toml)
    assert [s.name for s in mf.contributes.services] == ["event_watcher"]
    assert mf.contributes.services[0].factory.endswith(":PluginManifest")

    class _Svc:
        def __init__(self) -> None:
            self.started = []

        async def start(self, handles) -> None:
            self.started.append(handles)

        async def stop(self) -> None:
            pass

    assert isinstance(_Svc(), PluginService), "the paper face is start/stop"

    class _Reg:
        def service_names(self):
            return ["event_watcher"]

        def service_plugin_id(self, name):
            return "watcher-plug"

        def build_service(self, name, config, services):
            return _Svc()

    from raven.core.plugin_stack import build_plugin_services

    cfg = SimpleNamespace(
        memory=SimpleNamespace(user_id="u", agent_id="a"),
        plugins=SimpleNamespace(config={}),
    )
    built = build_plugin_services(tmp_path, cfg, registry=_Reg(), provider=None)
    assert len(built) == 1
    assert built[0].contributed_by == "watcher-plug"
