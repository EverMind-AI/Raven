"""Every sandbox's network policy, compiled from permissions.network and the grants in scope."""

from __future__ import annotations

import pytest

from raven.config.schema import NetworkPermissions, NetworkRule
from raven.sandbox.network_policy import (
    COMMON_PORTS,
    NetworkGrant,
    compile_network_policy,
    network_from_allow_net,
)

SKIP = "NETWORK_TLS_MODE_SKIP"


def test_open_default_is_the_curated_open_rule():
    policies = compile_network_policy(NetworkPermissions())
    assert list(policies) == ["allow_open"]
    rule = policies["allow_open"]
    assert rule["endpoints"][0] == {"host": "**.*.*", "ports": list(range(1, 65536)), "tls": SKIP}
    assert {"host": "pypi.org", "ports": COMMON_PORTS, "tls": SKIP} in rule["endpoints"]
    assert rule["binaries"] == [{"path": "/**"}]


def test_closed_default_with_no_rules_has_no_network_policies():
    assert compile_network_policy(NetworkPermissions(default="closed")) == {}


def test_an_allow_rule_takes_the_common_ports_and_any_program_by_default():
    network = NetworkPermissions(default="closed", rules=[NetworkRule(host="example.com")])
    ((name, rule),) = compile_network_policy(network).items()
    assert name.startswith("raven_rule_")
    assert rule["endpoints"] == [{"host": "example.com", "ports": COMMON_PORTS, "tls": SKIP}]
    assert rule["binaries"] == [{"path": "/**"}]


def test_a_deny_rule_opens_nothing():
    network = NetworkPermissions(default="closed", rules=[NetworkRule(host="example.com", tier="deny")])
    assert compile_network_policy(network) == {}


def test_a_grant_becomes_a_named_rule_for_one_binary_host_and_port():
    policies = compile_network_policy(
        NetworkPermissions(default="closed"), [NetworkGrant("/usr/bin/curl", "example.com", 443)]
    )
    ((name, rule),) = policies.items()
    assert name.startswith("raven_grant_")
    assert rule == {
        "name": name,
        "endpoints": [{"host": "example.com", "ports": [443], "tls": SKIP}],
        "binaries": [{"path": "/usr/bin/curl"}],
    }


def test_every_compiled_rule_lists_a_binary():
    network = NetworkPermissions(
        rules=[NetworkRule(host="a.example.com"), NetworkRule(host="b.example", binary="/bin/x")]
    )
    policies = compile_network_policy(network, [NetworkGrant("", "c.example", 80)])
    assert all(rule["binaries"] for rule in policies.values())


def test_rule_names_are_stable_across_calls():
    network = NetworkPermissions(rules=[NetworkRule(host="example.com", ports=[443])])
    grants = [NetworkGrant("/usr/bin/curl", "example.org", 443)]
    first, second = compile_network_policy(network, grants), compile_network_policy(network, grants)
    assert list(first) == list(second)
    assert first == second
    assert first != compile_network_policy(network)


@pytest.mark.parametrize(
    ("allow_net", "default", "rules"),
    [
        (True, "open", []),
        (False, "closed", []),
        (
            ["pypi.org", "files.pythonhosted.org"],
            "closed",
            [("pypi.org", [80, 443]), ("files.pythonhosted.org", [80, 443])],
        ),
        (["*", "example.com"], "open", [("example.com", [])]),
    ],
)
def test_the_old_allow_net_maps_onto_permissions_network(allow_net, default, rules):
    network = network_from_allow_net(allow_net)
    assert network.default == default
    assert [(rule.host, rule.ports) for rule in network.rules] == rules


def test_the_open_policy_with_200_grants_fits_the_gateway_limit():
    pytest.importorskip("openshell")
    from raven.sandbox.openshell_executor import build_sandbox_spec, sandbox_policy

    grants = [NetworkGrant("/usr/bin/curl", f"host{i}.example.com", 443) for i in range(200)]
    policy = sandbox_policy(network_policies=compile_network_policy(NetworkPermissions(), grants), mounts=[], uid=1000)
    spec = build_sandbox_spec(image="i", policy=policy, driver_config={}, resources={})
    assert spec.policy.ByteSize() < 262144


def test_a_deny_rule_takes_an_apex_host_out_of_the_open_set():
    network = NetworkPermissions(rules=[NetworkRule(host="pypi.org", tier="deny")])
    hosts = [endpoint["host"] for endpoint in compile_network_policy(network)["allow_open"]["endpoints"]]
    assert "pypi.org" not in hosts and "github.com" in hosts


def test_a_deny_rule_the_wildcard_covers_is_reported_as_unenforced(caplog):
    import logging

    from raven.sandbox.network_policy import unenforced_denials

    network = NetworkPermissions(
        rules=[
            NetworkRule(host="evil.example.com", tier="deny"),
            NetworkRule(host="pypi.org", tier="deny"),
            NetworkRule(host="example.org", tier="deny"),
        ]
    )
    assert unenforced_denials(network) == ["evil.example.com"]
    assert unenforced_denials(NetworkPermissions(default="closed", rules=network.rules)) == []
    with caplog.at_level(logging.WARNING, logger="raven.sandbox.network_policy"):
        compile_network_policy(network)
        compile_network_policy(network)
    warnings = [r.getMessage() for r in caplog.records if "evil.example.com" in r.getMessage()]
    assert len(warnings) == 1


@pytest.fixture
def fresh_warnings(monkeypatch):
    from raven.sandbox import network_policy

    monkeypatch.setattr(network_policy, "_warned_unenforced", set())
    monkeypatch.setattr(network_policy, "_warned_partial", set())


def _closed(*rules: NetworkRule) -> NetworkPermissions:
    return NetworkPermissions(default="closed", rules=list(rules))


def test_a_deny_rule_takes_the_host_out_of_an_allow_rule_for_it(fresh_warnings):
    network = _closed(NetworkRule(host="example.com", ports=[443]), NetworkRule(host="example.com", tier="deny"))
    assert compile_network_policy(network) == {}


def test_a_deny_rule_takes_only_its_own_ports_out_of_an_allow_rule(fresh_warnings):
    network = _closed(
        NetworkRule(host="example.com", ports=[80, 443]), NetworkRule(host="example.com", ports=[443], tier="deny")
    )
    (rule,) = compile_network_policy(network).values()
    assert [endpoint["ports"] for endpoint in rule["endpoints"]] == [[80]]


def test_a_deny_rule_for_a_wildcard_covers_an_allow_rule_inside_it(fresh_warnings):
    network = _closed(NetworkRule(host="*.example.com"), NetworkRule(host="**.example.com", tier="deny"))
    assert compile_network_policy(network) == {}


def test_a_deny_rule_for_the_program_of_an_allow_rule_takes_it_out(fresh_warnings):
    network = _closed(
        NetworkRule(host="example.com", binary="/usr/bin/curl"),
        NetworkRule(host="example.com", binary="/usr/bin/curl", tier="deny"),
    )
    assert compile_network_policy(network) == {}


def test_a_deny_rule_for_another_program_leaves_an_allow_rule_alone(fresh_warnings, caplog):
    import logging

    network = _closed(
        NetworkRule(host="example.com", binary="/usr/bin/git"),
        NetworkRule(host="example.com", binary="/usr/bin/curl", tier="deny"),
    )
    with caplog.at_level(logging.WARNING, logger="raven.sandbox.network_policy"):
        assert len(compile_network_policy(network)) == 1
    assert caplog.records == []


def test_a_deny_rule_on_other_ports_leaves_a_wildcard_allow_rule_unwarned(fresh_warnings, caplog):
    import logging

    network = _closed(
        NetworkRule(host="*.example.com", ports=[443]), NetworkRule(host="bad.example.com", ports=[22], tier="deny")
    )
    with caplog.at_level(logging.WARNING, logger="raven.sandbox.network_policy"):
        (rule,) = compile_network_policy(network).values()
    assert rule["endpoints"][0]["ports"] == [443]
    assert caplog.records == []


@pytest.mark.parametrize(
    "allow, deny",
    [
        (NetworkRule(host="*.example.com"), NetworkRule(host="bad.example.com", tier="deny")),
        (NetworkRule(host="**.example.com"), NetworkRule(host="*.example.com", tier="deny")),
        (NetworkRule(host="example.com"), NetworkRule(host="example.com", binary="/usr/bin/curl", tier="deny")),
    ],
    ids=["host-inside-a-wildcard", "one-label-of-many", "one-program-of-any"],
)
def test_an_allow_rule_a_deny_rule_covers_only_in_part_stays_and_is_warned_about_once(
    fresh_warnings, caplog, allow, deny
):
    import logging

    network = _closed(allow, deny)
    with caplog.at_level(logging.WARNING, logger="raven.sandbox.network_policy"):
        first = compile_network_policy(network)
        compile_network_policy(network)
    (rule,) = first.values()
    assert rule["endpoints"][0]["host"] == allow.host
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1 and deny.host in warnings[0] and allow.host in warnings[0]


def test_a_grant_a_deny_rule_refuses_is_left_out(fresh_warnings):
    network = _closed(NetworkRule(host="example.com", tier="deny"))
    grants = [NetworkGrant("/usr/bin/curl", "example.com", 443), NetworkGrant("/usr/bin/curl", "example.org", 443)]
    (rule,) = compile_network_policy(network, grants).values()
    assert rule["endpoints"][0]["host"] == "example.org"


@pytest.mark.parametrize(
    ("rule", "target", "refuses"),
    [
        (NetworkRule(host="example.com", tier="deny"), ("/usr/bin/curl", "example.com", 443), True),
        (NetworkRule(host="example.com", tier="deny"), ("/usr/bin/curl", "example.com", 2222), True),
        (NetworkRule(host="example.com", ports=[22], tier="deny"), ("/usr/bin/curl", "example.com", 443), False),
        (
            NetworkRule(host="example.com", binary="/usr/bin/git", tier="deny"),
            ("/usr/bin/curl", "example.com", 443),
            False,
        ),
        (NetworkRule(host="*.example.com", tier="deny"), ("/usr/bin/curl", "api.example.com", 443), True),
        (NetworkRule(host="*.example.com", tier="deny"), ("/usr/bin/curl", "a.b.example.com", 443), False),
        (NetworkRule(host="**.example.com", tier="deny"), ("/usr/bin/curl", "a.b.example.com", 443), True),
        (NetworkRule(host="example.com"), ("/usr/bin/curl", "example.com", 443), False),
        (NetworkRule(host="example.com", tier="deny"), ("/usr/bin/curl", "EXAMPLE.com", 443), True),
        (NetworkRule(host="example.com", tier="deny"), ("/usr/bin/curl", "example.com.", 443), True),
    ],
)
def test_a_deny_rule_refuses_by_host_port_and_program(rule, target, refuses):
    from raven.sandbox.network_policy import refusing_rule

    assert (refusing_rule(NetworkPermissions(rules=[rule]), *target) is not None) is refuses
