"""A value handed over can be wrong, and running it anyway is a poor way to find out.

The round-0 gate used to refuse every departure from the declared start. That is
right for the four out of five departures measured on 2026-08-17 -- a coarser
mesh, six keys left to the job's defaults, six batches instead of the corpus, a
Courant ceiling lowered past the very behaviour the task was about. Each of those
changes the question rather than answering it.

It is wrong for the fifth kind. A water viscosity of 1e-3 is not water; a first
run that spends an hour on it produces a clean-looking number about a different
liquid. Nothing in the run says so, which is exactly why it must be corrected
before the hour is spent rather than after.

No check can tell those apart from the numbers -- the difference is why it was
done. So a departure is allowed when it is said out loud, and the reason is
recorded next to what changed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.tools import base as tools_base  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


@pytest.fixture
def campaign(tmp_path, monkeypatch):

    home = tmp_path / "ops"
    d = home / "beam"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(
        json.dumps(
            {
                "backend": "process",
                "remote_dir": "/tmp/x",
                "command": "true",
                "seed_config": {"nu": "1e-3", "deltaT": "5e-4"},
            }
        ),
        encoding="utf-8",
    )
    tools_base.set_home(home)
    monkeypatch.setattr("oncall_flow.docker_backend.make_ssh_runner", lambda h, p, k, **kw: lambda cmd: (1, "refused"))
    return d


async def _submit(campaign_dir, cfg, basis=""):
    from oncall_flow.tools.ops import OpsSubmitTool

    return await OpsSubmitTool().execute(
        campaign="beam", configs=[cfg], round=0, eta_seconds=60, objective="x", basis=basis
    )


def _events(d, kind):
    p = d / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if json.loads(l).get("kind") == kind]


async def test_a_silent_departure_is_refused_and_says_what_a_reason_would_be(campaign):
    out = await _submit(campaign, {"nu": "1e-6", "deltaT": "5e-4"})
    assert "REFUSED" in out and "gives no reason" in out.replace("\n", " ")
    assert "basis" in out
    assert _events(campaign, "seed_refused")


async def test_the_refusal_names_what_is_not_a_reason(campaign):
    """Cheapness is the one motive that must not pass, and it is the common one."""
    out = await _submit(campaign, {"nu": "1e-6"})
    text = out.replace("\n", " ")
    assert "expensive or awkward" in text
    assert "smaller mesh" in text or "fewer batches" in text


async def test_a_departure_with_a_reason_goes_through_and_is_recorded(campaign):
    out = await _submit(
        campaign, {"nu": "1e-6", "deltaT": "5e-4"}, basis="nu=1e-3 is not water; water is 1e-6 at 20C, three orders out"
    )
    assert "REFUSED" not in out
    dep = _events(campaign, "seed_departed")
    assert dep, "the departure has to be in the trail"
    assert any("nu" in c for c in dep[0]["changes"])
    assert "not water" in dep[0]["basis"], "the reason is recorded next to the change"


async def test_submitting_the_declared_start_needs_no_reason(campaign):
    out = await _submit(campaign, {"nu": "1e-3", "deltaT": "5e-4"})
    assert "REFUSED" not in out
    assert not _events(campaign, "seed_departed")
