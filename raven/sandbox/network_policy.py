"""The network half of every sandbox's OpenShell policy, compiled from ``permissions.network``.

One function turns the permission set and the grants in a sandbox's scope into
the ``network_policies`` an OpenShell sandbox runs under, so every sandbox --
the main loop's, a spawn's, a DAG node's -- reads the same rules. Every rule it
writes names its programs: OpenShell matches a rule with no binaries to none.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from raven.config.schema import NetworkPermissions

# OpenShell has no allow-any-host selector (NVIDIA/OpenShell#3856): "*", "**"
# and two-label wildcards are rejected, hostless allowed_ips endpoints do not
# match names under the Docker driver's policy DNS, and an "a*.*"-style
# pattern passes the gateway but stops the sandbox from activating. "Open"
# is therefore every host of three or more labels on every port, plus the
# two-label names developer tooling reaches directly. One all-ports endpoint
# already takes most of the gateway's 256 KiB policy budget, so the apex names
# get the common ports only.
_OPEN_WILDCARD = "**.*.*"
_ALL_PORTS = list(range(1, 65536))
COMMON_PORTS = [22, 80, 443, 8080, 8443, 9418]
_APEX_DOMAINS = (
    "pypi.org",
    "python.org",
    "anaconda.org",
    "npmjs.org",
    "npmjs.com",
    "yarnpkg.com",
    "nodejs.org",
    "jsdelivr.net",
    "unpkg.com",
    "github.com",
    "githubusercontent.com",
    "gitlab.com",
    "bitbucket.org",
    "gitee.com",
    "crates.io",
    "rustup.rs",
    "rust-lang.org",
    "golang.org",
    "go.dev",
    "docker.io",
    "docker.com",
    "quay.io",
    "ghcr.io",
    "gcr.io",
    "maven.org",
    "gradle.org",
    "rubygems.org",
    "nuget.org",
    "ubuntu.com",
    "debian.org",
    "alpinelinux.org",
    "huggingface.co",
    "openai.com",
    "anthropic.com",
    "openrouter.ai",
    "deepseek.com",
)
OPEN_ENTRY = "*"


def _endpoint(host: str, ports: list[int]) -> dict:
    # tls: skip relays the connection without terminating TLS; these rules
    # carry no request inspection, and inspected egress measured an order of
    # magnitude slower.
    return {"host": host, "ports": ports, "tls": "NETWORK_TLS_MODE_SKIP"}


def _open_rule(extra_hosts: list[str], denied: frozenset[str] = frozenset()) -> dict:
    hosts = [host for host in dict.fromkeys([*_APEX_DOMAINS, *extra_hosts]) if host not in denied]
    endpoints = [_endpoint(_OPEN_WILDCARD, _ALL_PORTS), *(_endpoint(host, COMMON_PORTS) for host in hosts)]
    return {"name": "allow_open", "endpoints": endpoints, "binaries": [{"path": "/**"}]}


logger = logging.getLogger(__name__)
_warned_unenforced: set[str] = set()


def unenforced_denials(network: NetworkPermissions) -> list[str]:
    """Denied hosts the open default's ``**.*.*`` wildcard still lets a sandbox reach.

    A two-label host leaves the open set when denied; a host of three labels or
    more is matched by the wildcard, which OpenShell cannot carve an exception
    out of at this level.
    """
    if network.default != "open":
        return []
    return [rule.host for rule in network.rules if rule.tier == "deny" and len(rule.host.split(".")) >= 3]


class NetworkGrant(NamedTuple):
    """One program allowed to one host and port while it runs, granted at run time."""

    binary: str
    host: str
    port: int


def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:8]


def compile_network_policy(network: NetworkPermissions, grants: Iterable[NetworkGrant] = ()) -> dict[str, dict]:
    """The ``network_policies`` of a sandbox under ``network`` with ``grants`` added.

    Deny rules open nothing here; they refuse at approval time.
    """
    policies: dict[str, dict] = {}
    if network.default == "open":
        denied = frozenset(rule.host for rule in network.rules if rule.tier == "deny")
        policies["allow_open"] = _open_rule([], denied)
        for host in unenforced_denials(network):
            if host not in _warned_unenforced:
                _warned_unenforced.add(host)
                logger.warning(
                    "permissions.network denies %s, but the open default's wildcard still lets sandboxed "
                    "commands reach it; the rule only refuses approving it",
                    host,
                )
    for rule in network.rules:
        if rule.tier != "allow":
            continue
        name = f"raven_rule_{_digest(rule.host, rule.ports, rule.binary)}"
        policies[name] = {
            "name": name,
            "endpoints": [_endpoint(rule.host, list(rule.ports) or COMMON_PORTS)],
            "binaries": [{"path": rule.binary or "/**"}],
        }
    policies.update(grant_rules(grants))
    return policies


def grant_rules(grants: Iterable[NetworkGrant]) -> dict[str, dict]:
    """One named rule per grant, for exactly its program, host and port."""
    rules: dict[str, dict] = {}
    for grant in grants:
        name = f"raven_grant_{_digest(grant.binary, grant.host, grant.port)}"
        rules[name] = {
            "name": name,
            "endpoints": [_endpoint(grant.host, [grant.port])],
            "binaries": [{"path": grant.binary or "/**"}],
        }
    return rules


def policy_digest(policies: dict[str, dict]) -> str:
    """A stable fingerprint of compiled policies, to tell whether a running sandbox is behind."""
    return hashlib.sha256(json.dumps(policies, sort_keys=True).encode()).hexdigest()


def network_from_allow_net(allow_net: bool | list[str]) -> NetworkPermissions:
    """The ``permissions.network`` an old ``tools.sandbox.allowNet`` value meant.

    ``true`` is the open default and ``false`` no network. A host list opened
    those hosts on ports 80 and 443 only; with ``"*"`` in it, the open default
    plus the other hosts on the common ports.
    """
    from raven.config.schema import NetworkPermissions, NetworkRule

    if allow_net is True:
        return NetworkPermissions(default="open")
    if not allow_net:
        return NetworkPermissions(default="closed")
    if OPEN_ENTRY in allow_net:
        return NetworkPermissions(
            default="open", rules=[NetworkRule(host=host) for host in allow_net if host != OPEN_ENTRY]
        )
    return NetworkPermissions(default="closed", rules=[NetworkRule(host=host, ports=[80, 443]) for host in allow_net])
