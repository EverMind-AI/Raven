"""Network denials an OpenShell sandbox reported, read from the gateway's OCSF log lines.

The sandbox supervisor logs every refused connection as an OCSF line that the
gateway serves through ``GetSandboxLogs``. Two shapes matter: the TCP denial,
which names the program, the host and the port, and the DNS refusal, which
names only the host and precedes the TCP line for the same name.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_TCP = re.compile(
    r"^NET:OPEN \[[A-Z]+\] DENIED (?P<binary>.+?)\(\d+\) -> (?P<target>\S+) \[reason:(?P<reason>[^\]]+)\]"
)
_DNS = re.compile(r"^NET:REFUSE \[[A-Z]+\] DENIED (?P<host>\S+) \[reason:(?P<reason>[^\]]+)\]")
_BRACKETED = re.compile(r"^\[(?P<host>[^\]]+)\]:(?P<port>\d+)$")


@dataclass(frozen=True)
class NetworkDenial:
    """One connection the sandbox's network policy refused."""

    binary: str
    host: str
    port: int | None
    reason: str


def _target(text: str) -> tuple[str, int | None]:
    bracketed = _BRACKETED.match(text)
    if bracketed:
        return bracketed["host"], int(bracketed["port"])
    host, _, port = text.rpartition(":")
    return (host, int(port)) if host and port.isdigit() else (text, None)


def parse_denials(messages: Iterable[str]) -> list[NetworkDenial]:
    """The distinct denials in ``messages``, in the order first seen.

    A DNS refusal is kept only when no TCP denial names the same host: the TCP
    line says more about the same attempt.
    """
    found: list[NetworkDenial] = []
    for message in messages:
        tcp = _TCP.match(message)
        if tcp:
            host, port = _target(tcp["target"])
            found.append(NetworkDenial(tcp["binary"], host, port, tcp["reason"]))
            continue
        dns = _DNS.match(message)
        if dns:
            found.append(NetworkDenial("", dns["host"], None, dns["reason"]))
    tcp_hosts = {denial.host for denial in found if denial.port is not None}
    seen: set[tuple[str, str, int | None]] = set()
    result: list[NetworkDenial] = []
    for denial in found:
        key = (denial.binary, denial.host, denial.port)
        if key in seen or (denial.port is None and denial.host in tcp_hosts):
            continue
        seen.add(key)
        result.append(denial)
    return result


def denial_line(denial: NetworkDenial) -> str:
    """``/usr/bin/curl -> example.com:443``, or the bare host a DNS refusal names."""
    if denial.port is None:
        target = denial.host
    elif ":" in denial.host:
        target = f"[{denial.host}]:{denial.port}"
    else:
        target = f"{denial.host}:{denial.port}"
    return f"{denial.binary} -> {target}" if denial.binary else target
