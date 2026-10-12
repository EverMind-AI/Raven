"""raven.ops.transport: the one spelling of the ssh command line.

The one-shot runner and an ACP agent on a machine both build on ``ssh_argv``,
so these pin what the runner sent before the two shared it.
"""

from __future__ import annotations

import subprocess

import pytest

from raven.ops import transport
from raven.ops.transport import ISOLATE_IDENTITY, TransportError, make_ssh_runner, ssh_argv, ssh_target

BASE = [
    "ssh",
    "-i",
    "/k",
    "-p",
    "2222",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=15",
    "-o",
    "StrictHostKeyChecking=accept-new",
]


def test_the_runner_sends_what_it_always_sent(monkeypatch):
    sent: list[list[str]] = []

    def run(argv, **kwargs):
        sent.append(argv)
        return subprocess.CompletedProcess(argv, 0, "out", "")

    monkeypatch.setattr(transport.subprocess, "run", run)

    assert make_ssh_runner("h", 2222, "/k", user="u")("true") == (0, "out")
    assert make_ssh_runner("h", 2222, "/k", user="u", identities_only=True)("true") == (0, "out")

    assert sent[0] == [*BASE, "u@h", "true"]
    assert sent[1] == [*BASE, *ISOLATE_IDENTITY, "u@h", "true"]


def test_extra_options_go_before_the_destination_where_ssh_reads_them():
    argv = ssh_argv("h", 2222, "/k", user="u", extra=("-T", "-o", "ServerAliveInterval=15"))

    assert argv == [*BASE, "-T", "-o", "ServerAliveInterval=15", "u@h"]


def test_a_row_fills_in_the_registry_s_defaults():
    host, port, key, user = ssh_target({"id": "m", "host": " h "})

    assert (host, port, user) == ("h", 22, "root")
    assert key.endswith("/.ssh/id_rsa") and not key.startswith("~")


def test_a_row_with_no_host_is_refused_by_name():
    with pytest.raises(TransportError, match="'m' has no address"):
        ssh_target({"id": "m", "port": 22})
