"""The contribution surface's ledger: the plugin-facing vocabulary, pinned.

raven/plugins is not under raven/contracts, but ``ServiceLocator``,
``RuntimeHandles`` and ``PluginManifest`` are promises all the same: a plugin
built against them outlives any one refactor of the host (everos-memory is
its own distribution now, and the bundled shelf rides the same shapes). Same
discipline as tests/test_contracts_two_tier_ledger.py -- the roster lives
beside the assertion, so growing the surface is a reviewed change to this
file with a reason in the diff, never a drift.
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
