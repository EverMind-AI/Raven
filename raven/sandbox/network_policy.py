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
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from raven.config.schema import NetworkPermissions, NetworkRule

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
# The name of the rule that holds the open set.
OPEN_RULE = "allow_open"


def _endpoint(host: str, ports: list[int]) -> dict:
    # tls: skip relays the connection without terminating TLS; these rules
    # carry no request inspection, and inspected egress measured an order of
    # magnitude slower.
    return {"host": host, "ports": ports, "tls": "NETWORK_TLS_MODE_SKIP"}


def _open_rule(extra_hosts: list[str], denied: frozenset[str] = frozenset()) -> dict:
    hosts = [host for host in dict.fromkeys([*_APEX_DOMAINS, *extra_hosts]) if host not in denied]
    endpoints = [_endpoint(_OPEN_WILDCARD, _ALL_PORTS), *(_endpoint(host, COMMON_PORTS) for host in hosts)]
    return {"name": OPEN_RULE, "endpoints": endpoints, "binaries": [{"path": "/**"}]}


logger = logging.getLogger(__name__)
_warned_unenforced: set[str] = set()
_warned_partial: set[tuple[str, str, str]] = set()


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


def _host_pattern(host: str) -> re.Pattern[str]:
    # OpenShell's host wildcards: "*" is one label, "**" one label or more.
    labels = [{"*": r"[^.]+", "**": r"[^.]+(?:\.[^.]+)*"}.get(label, re.escape(label)) for label in host.split(".")]
    return re.compile(r"\.".join(labels))


def refusing_rule(network: NetworkPermissions, binary: str, host: str, port: int | None) -> NetworkRule | None:
    """The user's deny rule that refuses ``binary`` reaching ``host:port``; None when no rule does.

    A deny rule with no ports refuses every port, and one with no program
    refuses every program: what it names is a host the user does not want
    reached, and the open set already drops a denied host on every port.
    """
    # The fully qualified spelling (a trailing dot) names the same host.
    host = host.lower().rstrip(".")
    for rule in network.rules:
        if rule.tier != "deny":
            continue
        if rule.ports and port not in rule.ports:
            continue
        if rule.binary and rule.binary != binary:
            continue
        if _host_pattern(rule.host).fullmatch(host):
            return rule
    return None


def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:8]


def compile_network_policy(
    network: NetworkPermissions | AllowNetNetwork, grants: Iterable[NetworkGrant] = ()
) -> dict[str, dict]:
    """The ``network_policies`` of a sandbox under ``network`` with ``grants`` added.

    A deny rule wins over what would open the same target: a grant it refuses
    is left out, and an allow rule loses the ports a deny rule takes from all of
    its hosts and programs. An allow rule a deny rule covers only in part stays
    whole, with a warning: OpenShell cannot carve an exception out of a rule.
    """
    policies: dict[str, dict] = {}
    denies = [rule for rule in network.rules if rule.tier == "deny"]
    if network.default == "open":
        denied = frozenset(rule.host for rule in network.rules if rule.tier == "deny")
        policies[OPEN_RULE] = _open_rule([], denied)
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
        ports = _ports_left(rule, denies)
        if not ports:
            continue
        name = f"raven_rule_{_digest(rule.host, rule.ports, rule.binary)}"
        policies[name] = {
            "name": name,
            "endpoints": [_endpoint(rule.host, ports)],
            "binaries": [{"path": rule.binary or "/**"}],
        }
    policies.update(
        grant_rules(grant for grant in grants if refusing_rule(network, grant.binary, grant.host, grant.port) is None)
    )
    return policies


def _ports_left(allow: Any, denies: list[Any]) -> list[int]:
    """The ports of ``allow`` no deny rule takes from it whole; warns once about a deny rule it cannot honour."""
    ports = list(allow.ports) or COMMON_PORTS
    for deny in denies:
        if deny.ports and not set(deny.ports) & set(ports):
            continue
        taken = _deny_covers(deny, allow)
        if taken is True:
            ports = [port for port in ports if deny.ports and port not in deny.ports]
        elif taken is None and (deny.host, allow.host, deny.binary) not in _warned_partial:
            _warned_partial.add((deny.host, allow.host, deny.binary))
            logger.warning(
                "permissions.network denies %s, but the allow rule for %s still lets sandboxed commands reach it; "
                "the deny rule only refuses approving it",
                f"{deny.binary} -> {deny.host}" if deny.binary else deny.host,
                allow.host,
            )
    return ports


def _deny_covers(deny: Any, allow: Any) -> bool | None:
    """True when ``deny`` names every host and program ``allow`` opens, None when only some, False when none."""
    if deny.binary and allow.binary and deny.binary != allow.binary:
        return False
    deny_pattern = _host_pattern(deny.host)
    # A "**" label is one label or more, so a deny rule covers it only when it
    # covers both of those spellings.
    spellings = {allow.host.replace("**", "*"), allow.host.replace("**", "*.*")}
    every_host = all(deny_pattern.fullmatch(spelling) for spelling in spellings)
    if every_host and (not deny.binary or deny.binary == allow.binary):
        return True
    some_host = every_host or _host_pattern(allow.host).fullmatch(re.sub(r"\*+", "x", deny.host)) is not None
    return None if some_host else False


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


class AllowNetRule(NamedTuple):
    """One rule an old ``allowNet`` list meant, shaped like a ``permissions.network`` rule."""

    host: str
    ports: list[int]
    binary: str = ""
    tier: str = "allow"


class AllowNetNetwork(NamedTuple):
    """What an old ``allowNet`` value meant, shaped like ``permissions.network``.

    Kept apart from the config model so the sandbox package reads no config:
    the config package imports this one, and the loader turns it into the
    ``permissions.network`` it writes.
    """

    default: str
    rules: list[AllowNetRule]


def network_from_allow_net(allow_net: bool | list[str]) -> AllowNetNetwork:
    """The ``permissions.network`` an old ``tools.sandbox.allowNet`` value meant.

    ``true`` is the open default and ``false`` no network. A host list opened
    those hosts on ports 80 and 443 only; with ``"*"`` in it, the open default
    plus the other hosts on the common ports.
    """
    if allow_net is True:
        return AllowNetNetwork("open", [])
    if not allow_net:
        return AllowNetNetwork("closed", [])
    if OPEN_ENTRY in allow_net:
        return AllowNetNetwork("open", [AllowNetRule(host, []) for host in allow_net if host != OPEN_ENTRY])
    return AllowNetNetwork("closed", [AllowNetRule(host, [80, 443]) for host in allow_net])
