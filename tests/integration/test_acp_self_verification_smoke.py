"""Raven's ACP client verifies Raven's ACP agent, with no new code on either side.

``capabilities.verify_agent`` is the probe that decides whether a configured
agent is usable: it launches the executable, does two round trips
(``initialize``, then ``session/new`` in a throwaway directory), classifies the
result into one of four states, and never raises. It is duck-typed on the config
object, so pointing it at ``raven acp`` costs a five-field namespace -- and what
comes back is a conformance assertion written by the side that had to work with
real third-party agents rather than by the side being tested.

That asymmetry is the value. Every other test here was written against raven's
own reading of the specification; this one is checked by a reader that was built
from measurements of hermes and codex-acp, and it fails the same way it would
fail for them.

Marked ``integration`` because it launches the binary twice over.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.integration


def _cfg(home: Path):
    binary = Path(sys.executable).with_name("raven.exe" if sys.platform == "win32" else "raven")
    if not binary.exists():
        pytest.skip(f"raven console script not installed at {binary}")
    return SimpleNamespace(
        name="raven-self",
        # A string, not a list: the launcher shlex-parses it, and a list is
        # accepted by the type checker and then read as a file object.
        command=f"{binary} acp",
        cwd=None,
        # A throwaway home for the same reason every other smoke test uses one:
        # the agent starts cron and the memory backend against whatever this
        # names, and no provider is configured here, which keeps the run
        # reproducible.
        env={**os.environ, "RAVEN_HOME": str(home)},
        ready_timeout_ms=120000,
    )


async def test_ravens_own_probe_reports_the_agent_ready(tmp_path):
    from raven.agent.acp.capabilities import verify_agent

    snapshot = await verify_agent(_cfg(tmp_path / "home"))

    assert snapshot.status == "ready", f"{snapshot.status}: {snapshot.detail}"
    assert snapshot.protocol_version == 1
    assert snapshot.agent_name == "raven"
    assert snapshot.agent_version, "an agent that will not name its version cannot be diagnosed in the field"


async def test_the_probe_reads_back_exactly_what_the_agent_declared(tmp_path):
    """Both halves of the handshake, checked by the reader rather than the writer.

    ``prompt_modalities`` is the interesting one: it is assembled by the client
    from ``promptCapabilities``, so a flag the agent set and the client cannot
    find would show up here as a missing modality rather than as a passing test.
    """
    from raven.agent.acp.capabilities import verify_agent

    snapshot = await verify_agent(_cfg(tmp_path / "home"))

    assert set(snapshot.prompt_modalities) == {"text", "embeddedContext", "image"}
    assert "audio" not in snapshot.prompt_modalities, "declared false because there is no audio path"
    assert list(snapshot.auth_methods) == [], "an empty list is the statement that no authentication is needed"
    # Read back by the client that was built from measurements of hermes and
    # codex-acp, which is the point of this file: the flag is not merely set in
    # the agent's own dict, it is found where a third-party reader looks for it.
    assert snapshot.can_load is True, "the transcript replay exists, so the capability is declared"
    assert snapshot.can_resume is False, "session/resume is stable and unbuilt"
    assert snapshot.can_fork is False


async def test_the_probe_opens_a_real_session_not_just_a_handshake(tmp_path):
    """``session/new`` is the second round trip, and it is what separates "the
    executable exists" from "this agent is usable" -- the gap a ``which`` probe
    can never close. The detail line names the session capability it found."""
    from raven.agent.acp.capabilities import verify_agent

    snapshot = await verify_agent(_cfg(tmp_path / "home"))

    assert "over ACP v1" in snapshot.detail
    assert "sessions:" in snapshot.detail
    assert snapshot.elapsed_ms > 0
