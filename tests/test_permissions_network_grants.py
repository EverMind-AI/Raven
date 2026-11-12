"""Run-time network grants, the scopes they apply to, and the sandboxes they must reach."""

from __future__ import annotations

from raven.config.schema import NetworkPermissions, PermissionsConfig
from raven.permissions.network_grants import GrantRegistry, network_source_for
from raven.sandbox.network_policy import NetworkGrant

CURL = NetworkGrant("/usr/bin/curl", "example.com", 443)
GIT = NetworkGrant("/usr/bin/git", "gitlab.example.org", 22)


def test_a_grant_stays_in_its_conversations_scope():
    registry = GrantRegistry()
    registry.grant("conv-a", CURL)
    assert registry.grants_for("conv-a") == frozenset({CURL})
    assert registry.grants_for("conv-b") == frozenset()


def test_the_shared_sandbox_sees_every_scopes_grants():
    registry = GrantRegistry()
    registry.grant("conv-a", CURL)
    registry.grant("conv-b", GIT)
    assert registry.grants_for(None) == frozenset({CURL, GIT})


def test_a_scope_reaches_its_own_sandboxes_and_the_shared_one():
    registry = GrantRegistry()
    shared, own, other = object(), object(), object()
    registry.register(shared, None)
    registry.register(own, "conv-a")
    registry.register(other, "conv-b")
    assert set(map(id, registry.executors_for("conv-a"))) == {id(shared), id(own)}


def test_an_unregistered_sandbox_is_no_longer_reached():
    registry = GrantRegistry()
    own = object()
    registry.register(own, "conv-a")
    registry.unregister(own)
    registry.unregister(own)
    assert registry.executors_for("conv-a") == []


def test_the_network_source_reads_the_live_permissions_and_the_scopes_grants():
    registry = GrantRegistry()
    current = {"config": PermissionsConfig(network=NetworkPermissions(default="closed"))}
    source = network_source_for(lambda: current["config"], "conv-a", registry=registry)
    assert source() == {}
    registry.grant("conv-a", CURL)
    assert [name.startswith("raven_grant_") for name in source()] == [True]
    current["config"] = PermissionsConfig()
    assert "allow_open" in source()


def test_a_grant_without_a_scope_is_ignored():
    registry = GrantRegistry()
    registry.grant("", CURL)
    assert registry.grants_for(None) == frozenset()
