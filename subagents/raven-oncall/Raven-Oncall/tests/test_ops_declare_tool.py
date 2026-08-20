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


@pytest.mark.asyncio
async def test_the_case_and_the_round_directory_can_be_named_in_the_command(home) -> None:
    # Naming the case is still ordinary -- running a script that lives in it,
    # reading a file out of it -- it is only copying it in that is refused.
    out = await OpsDeclareTool().execute(**_args(
        command="cd {job_dir} && python3 {staged_case}/gen.py {config} > in && bash run.sh"))

    assert not out.startswith("REFUSED"), out


@pytest.mark.asyncio
@pytest.mark.parametrize("copying", [
    "cd {job_dir} && cp {staged_case}/run.sh . && bash run.sh",
    "cp -r {staged_case}/* {job_dir}/ && cd {job_dir} && bash run.sh",
    "rsync -a {staged_case}/ {job_dir}/ && bash {job_dir}/run.sh",
    "cd {job_dir} && cp /srv/case/run.sh . && bash run.sh",
])
async def test_copying_the_case_in_is_refused(home, copying) -> None:
    """Measured 2026-08-19, twice, and the second time cost the campaign.

    The round's directory is filled with a link to every file in the case before
    the command runs, so copying the case in has the same file as source and
    target and cp stops with "are the same file". Round 0 of beam-limit-load
    failed on exactly that, and the arm's answer was not to fix the declaration
    but to abandon the ledger and run the sweep by hand on the shared box.

    The two descriptions that say the case is already there were in place when it
    was declared. Saying so was not enough; this refuses it.
    """
    out = await OpsDeclareTool().execute(**_args(command=copying))

    assert out.startswith("REFUSED"), out
    assert "already there" in out
    assert not (home / "beam").exists(), "a campaign that cannot run is not left behind"


@pytest.mark.asyncio
async def test_copying_within_the_round_directory_is_its_own_business(home) -> None:
    # Only copying FROM the case is refused. What a trial does with its own
    # directory -- a backup before overwriting, staging an input -- is not ours.
    out = await OpsDeclareTool().execute(**_args(
        command="cd {job_dir} && cp {config} in.json && bash run.sh"))

    assert not out.startswith("REFUSED"), out


@pytest.mark.asyncio
async def test_a_placeholder_nothing_fills_in_is_refused_at_declaration(home) -> None:
    """Caught here rather than at the first submit, where it was a bare KeyError.

    Measured 2026-08-19: a command using {staged_case} -- which nothing expanded
    at the time -- came back as "KeyError 'staged_case'. This is a fault inside
    the tool itself, do not work around it". True, and it left the loop with
    nothing it was permitted to do: it repeated the identical call three times and
    the campaign never ran a round.
    """
    out = await OpsDeclareTool().execute(**_args(
        command="bash {arena}/run.sh --cores {cores}"))

    assert out.startswith("REFUSED")
    assert "arena" in out and "cores" in out
    for known in ("{job_dir}", "{config}", "{staged_case}", "{remote_dir}"):
        assert known in out, "a refusal has to say what IS available"
    assert not (home / "beam").exists(), "a campaign that cannot run is not left behind"


@pytest.mark.asyncio
@pytest.mark.parametrize("rounds", ["/srv/case/runs", "/srv/case", "/srv/case/a/b"])
async def test_rounds_inside_the_case_are_refused(home, rounds) -> None:
    """Measured 2026-08-19: an arm put rounds at "<staged_case>/runs".

    Two things go wrong and the second is worse. The case stops being read-only --
    that run left job.inp and config.json inside it -- and each round is built from
    a tree of links to the case, so a round directory inside the case gets linked
    into the round after it.

    The write-set probe does not catch this: it asks which FILES in the case
    changed, and a new directory beside them changes none.
    """
    out = await OpsDeclareTool().execute(**_args(remote_dir=rounds))

    assert out.startswith("REFUSED") and "inside the owner's case" in out
    assert "/srv/case" in out and rounds in out, "both paths have to be visible"
    assert not (home / "beam").exists()


@pytest.mark.asyncio
async def test_rounds_beside_the_case_are_fine(home) -> None:
    out = await OpsDeclareTool().execute(**_args(remote_dir="/srv/runs"))

    assert not out.startswith("REFUSED"), out


@pytest.mark.asyncio
async def test_a_name_that_merely_starts_the_same_is_not_inside(home) -> None:
    # "/srv/case-old" begins with "/srv/case" as text and is a different directory.
    out = await OpsDeclareTool().execute(**_args(remote_dir="/srv/case-old/runs"))

    assert not out.startswith("REFUSED"), out
