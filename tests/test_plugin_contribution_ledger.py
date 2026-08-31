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


def test_the_runtime_handles_grants_are_the_ledgered_four() -> None:
    from raven.plugins.context import RuntimeHandles

    assert sorted(f.name for f in dataclasses.fields(RuntimeHandles)) == [
        "playbook_runtime",
        "session_dir",
        "subagent_registry",
        "subagents_paused",
    ], "a new field here is a new late-bound grant: ledger it and name the power it hands over"
    assert RuntimeHandles.__dataclass_params__.frozen, "grants are handed over, never handed back"


def test_the_manifest_kinds_are_the_ledgered_three() -> None:
    from raven.plugins.manifest import Contributes, PluginManifest

    assert sorted(Contributes.model_fields) == ["hooks", "memory_backends", "tools"], (
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
