"""OpsDeclareTool: an experiment's declaration, written once, before any compute.

The cases that matter are the refusals. A declaration that is wrong costs nothing
here and everything later: every round is checked against it, and the apparatus
gate makes it unrewritable from the first submit on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.tools.ops_declare import OpsDeclareTool


@pytest.fixture
def home(tmp_path: Path, monkeypatch):
    import raven.agent.tools.ops as ops_mod

    d = tmp_path / "ops"
    d.mkdir()
    monkeypatch.setattr(ops_mod, "_ops_home", lambda: d)
    monkeypatch.setattr("raven.ops.connections.get",
                        lambda cid: {"host": "h", "port": 64106} if cid == "conn_ok" else None)
    monkeypatch.setattr("raven.ops.connections.display_name",
                        lambda cid: "the CPU box" if cid == "conn_ok" else "")
    return d


def _args(**over):
    base = dict(campaign="beam", objective="how much load the beam carries",
                metric="collapse_load", goal="max", connection="conn_ok",
                staged_case="/srv/case", command="bash {job_dir}/run.sh")
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_a_declaration_records_the_whole_setup_and_runs_nothing(home) -> None:
    out = await OpsDeclareTool().execute(**_args(
        seed_config={"nx": 80, "inc": 0.05}, budget_total=25, budget_unit="minute",
        budget_overlap="additive"))

    assert "no compute has been spent" in out
    meta = json.loads((home / "beam" / "meta.json").read_text(encoding="utf-8"))
    assert meta["connection"] == "conn_ok"
    assert meta["backend"] == "process", "a command is run by the process backend"
    assert meta["staged_case"] == "/srv/case"
    assert meta["objective"] == {"metric": "collapse_load", "direction": "max"}
    assert meta["seed_config"] == {"nx": 80, "inc": 0.05}
    assert meta["budget"] == {"unit": "minute", "total": 25.0, "overlap": "additive"}
    assert not (home / "beam" / "ledger.json").exists(), "declaring must not start anything"


@pytest.mark.asyncio
async def test_the_readback_names_what_the_owner_can_check_in_seconds(home) -> None:
    out = await OpsDeclareTool().execute(**_args(seed_config={"nx": 80}))

    assert "the CPU box" in out, "the owner's own name for the machine, not an id"
    assert "/srv/case" in out and "read, never written" in out
    assert "collapse_load, as max as possible" in out
    assert "nx=80" in out, "the starting point is the line most worth checking"


@pytest.mark.asyncio
async def test_no_budget_is_stated_rather_than_defaulted(home) -> None:
    # An unbudgeted experiment is ordinary -- run this once, reproduce that
    # failure. Inventing a number would put a limit nobody chose in front of a
    # loop that cannot tell it from one the owner set.
    out = await OpsDeclareTool().execute(**_args())

    meta = json.loads((home / "beam" / "meta.json").read_text(encoding="utf-8"))
    assert "budget" not in meta
    assert "nothing will stop this on the total" in out
    assert "Report what a round costs once you know it" in out, (
        "the owner cannot set a budget before anyone knows what a round costs"
    )


@pytest.mark.asyncio
async def test_declaring_over_an_existing_campaign_is_refused(home) -> None:
    await OpsDeclareTool().execute(**_args())
    before = (home / "beam" / "meta.json").read_text(encoding="utf-8")

    out = await OpsDeclareTool().execute(**_args(metric="something_else"))

    assert out.startswith("REFUSED") and "ops_ask_owner" in out
    assert (home / "beam" / "meta.json").read_text(encoding="utf-8") == before


@pytest.mark.asyncio
@pytest.mark.parametrize("over, expect", [
    ({"connection": ""}, "no machine to run on"),
    ({"connection": "conn_typo"}, "no connection with id"),
    ({"command": ""}, "does not say how a trial starts"),
    ({"command": "", "backend": "process"}, "nothing to run"),
    ({"goal": "biggest"}, "must be 'max' or 'min'"),
])
async def test_a_setup_that_cannot_run_is_refused_before_it_is_written(
    home, over, expect
) -> None:
    out = await OpsDeclareTool().execute(**_args(**over))

    assert out.startswith("REFUSED") and expect in out, out
    assert not (home / "beam").exists(), (
        "a campaign that cannot run must not be left on disk for a later round to find"
    )


@pytest.mark.asyncio
async def test_docker_is_allowed_but_only_when_asked_for(home) -> None:
    out = await OpsDeclareTool().execute(**_args(command="", backend="docker", image="py:3.12"))

    assert not out.startswith("REFUSED"), out
    meta = json.loads((home / "beam" / "meta.json").read_text(encoding="utf-8"))
    assert meta["backend"] == "docker" and meta["image"] == "py:3.12"
