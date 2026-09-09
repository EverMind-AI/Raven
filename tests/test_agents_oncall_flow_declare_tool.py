"""OpsDeclareTool: an experiment's declaration, written once, before any compute.

The cases that matter are the refusals. A declaration that is wrong costs nothing
here and everything later: every round is checked against it, and the apparatus
gate makes it unrewritable from the first submit on.
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
from oncall_flow.tools.ops_declare import OpsDeclareTool  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


@pytest.fixture
def home(tmp_path: Path, monkeypatch):

    d = tmp_path / "ops"
    d.mkdir()
    tools_base.set_home(d)
    monkeypatch.setattr(
        "oncall_flow.connections.get", lambda cid: {"host": "h", "port": 64106} if cid == "conn_ok" else None
    )
    monkeypatch.setattr("oncall_flow.connections.display_name", lambda cid: "the CPU box" if cid == "conn_ok" else "")
    return d


def _args(**over):
    base = dict(
        campaign="beam",
        objective="how much load the beam carries",
        objective_kind="optimize",
        metric="collapse_load",
        goal="max",
        connection="conn_ok",
        staged_case="/srv/case",
        command="bash {job_dir}/run.sh",
    )
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_a_declaration_records_the_whole_setup_and_runs_nothing(home) -> None:
    out = await OpsDeclareTool().execute(
        **_args(seed_config={"nx": 80, "inc": 0.05}, budget_total=25, budget_unit="minute", budget_overlap="additive")
    )

    assert "no compute has been spent" in out
    meta = json.loads((home / "beam" / "meta.json").read_text(encoding="utf-8"))
    assert meta["connection"] == "conn_ok"
    assert meta["backend"] == "process", "a command is run by the process backend"
    assert meta["staged_case"] == "/srv/case"
    assert meta["objective"] == {
        "kind": "optimize",
        "metric": "collapse_load",
        "direction": "max",
    }
    assert meta["seed_config"] == {"nx": 80, "inc": 0.05}
    assert meta["budget"] == {
        "unit": "minute",
        "total": 25.0,
        "overlap": "additive",
        "meter": "compute",
    }, "the meter says who reads the spend, and machine time is the default"
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
def _ledger(cdir, *statuses) -> None:
    """A ledger holding one record per status given."""
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "ledger.json").write_text(
        json.dumps(
            {
                "version": 1,
                "records": {
                    f"t{i}": {"idem_key": f"t{i}", "status": s, "campaign": cdir.name} for i, s in enumerate(statuses)
                },
            }
        ),
        encoding="utf-8",
    )


def _events(cdir) -> list[dict]:
    p = cdir / "events.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


@pytest.mark.asyncio
async def test_a_declaration_nothing_depends_on_yet_can_be_corrected(home) -> None:
    """Measured 2026-08-21: an OpenFOAM command used ``source``, round 0 died on it
    (``sh: source: not found``), and the task was unrecoverable -- with nothing
    recorded and 150 core-minutes unspent -- because a second declaration was
    refused for moving a target under results that did not exist.
    """
    await OpsDeclareTool().execute(**_args(command="source /opt/env && bash run.sh"))
    _ledger(home / "beam", "failed")

    out = await OpsDeclareTool().execute(**_args(command="bash -c '. /opt/env && bash run.sh'"))

    assert not out.startswith("REFUSED"), out
    meta = json.loads((home / "beam" / "meta.json").read_text(encoding="utf-8"))
    assert meta["command"] == "bash -c '. /opt/env && bash run.sh'"


@pytest.mark.asyncio
async def test_correcting_it_leaves_a_line_between_the_two_setups(home) -> None:
    """The failed rounds stay in the ledger and were run under the old command, so
    the change itself has to be readable months later."""
    await OpsDeclareTool().execute(**_args(command="bash old.sh"))
    _ledger(home / "beam", "failed")

    await OpsDeclareTool().execute(**_args(command="bash new.sh"))

    amended = [e for e in _events(home / "beam") if e.get("kind") == "declaration_amended"]
    assert len(amended) == 1
    assert amended[0]["changed"]["command"] == {"was": "bash old.sh", "now": "bash new.sh"}


@pytest.mark.asyncio
async def test_a_declaration_with_results_under_it_is_refused(home) -> None:
    await OpsDeclareTool().execute(**_args())
    _ledger(home / "beam", "succeeded", "failed")
    before = (home / "beam" / "meta.json").read_text(encoding="utf-8")

    out = await OpsDeclareTool().execute(**_args(metric="something_else"))

    assert out.startswith("REFUSED") and "already has results" in out
    assert (home / "beam" / "meta.json").read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_a_declaration_with_a_round_in_flight_is_refused(home) -> None:
    """The running round is using the current setup; amending now would leave the
    ledger half under one and half under another."""
    await OpsDeclareTool().execute(**_args())
    _ledger(home / "beam", "running")

    out = await OpsDeclareTool().execute(**_args(command="bash other.sh"))

    assert out.startswith("REFUSED") and "running against the current" in out
    assert "ops_kill" in out


@pytest.mark.asyncio
async def test_a_concluded_campaign_is_refused(home) -> None:
    """Its report states the conditions; changing them behind it makes the two
    disagree."""
    await OpsDeclareTool().execute(**_args())
    (home / "beam" / "concluded.json").write_text("{}", encoding="utf-8")

    out = await OpsDeclareTool().execute(**_args(command="bash other.sh"))

    assert out.startswith("REFUSED") and "concluded" in out


@pytest.mark.asyncio
async def test_an_unreadable_ledger_is_not_permission_to_rewrite(home) -> None:
    await OpsDeclareTool().execute(**_args())
    (home / "beam" / "ledger.json").write_text("{not json", encoding="utf-8")

    out = await OpsDeclareTool().execute(**_args(command="bash other.sh"))

    assert out.startswith("REFUSED") and "could not be read" in out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "over, expect",
    [
        ({"connection": ""}, "no machine to run on"),
        ({"connection": "conn_typo"}, "no connection with id"),
        ({"command": ""}, "does not say how a trial starts"),
        ({"command": "", "backend": "process"}, "nothing to run"),
        ({"goal": "biggest"}, "must be 'max' or 'min'"),
    ],
)
async def test_a_setup_that_cannot_run_is_refused_before_it_is_written(home, over, expect) -> None:
    out = await OpsDeclareTool().execute(**_args(**over))

    assert out.startswith("REFUSED") and expect in out, out
    assert not (home / "beam").exists(), "a campaign that cannot run must not be left on disk for a later round to find"


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
    out = await OpsDeclareTool().execute(
        **_args(command="cd {job_dir} && python3 {staged_case}/gen.py {config} > in && bash run.sh")
    )

    assert not out.startswith("REFUSED"), out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "copying",
    [
        "cd {job_dir} && cp {staged_case}/run.sh . && bash run.sh",
        "cp -r {staged_case}/* {job_dir}/ && cd {job_dir} && bash run.sh",
        "rsync -a {staged_case}/ {job_dir}/ && bash {job_dir}/run.sh",
        "cd {job_dir} && cp /srv/case/run.sh . && bash run.sh",
    ],
)
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
    out = await OpsDeclareTool().execute(**_args(command="cd {job_dir} && cp {config} in.json && bash run.sh"))

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
    out = await OpsDeclareTool().execute(**_args(command="bash {arena}/run.sh --cores {cores}"))

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


# ---- what one job holds is said at declare (owner's rulings, 2026-09-03) ----


@pytest.fixture
def gpu_home(tmp_path: Path, monkeypatch):
    d = tmp_path / "ops"
    d.mkdir()
    tools_base.set_home(d)
    rows = {
        "gpu2": {"host": "h", "port": 1, "kind": "gpu", "gpus": 2},
        "cpu32": {"host": "h", "port": 1, "kind": "cpu", "cores": 32, "memory": "232 GB"},
    }
    monkeypatch.setattr("oncall_flow.connections.get", lambda cid: rows.get(cid))
    monkeypatch.setattr(
        "oncall_flow.connections.display_name", lambda cid: {"gpu2": "GPU box", "cpu32": "CPU box"}.get(cid, "")
    )
    return d


@pytest.mark.asyncio
async def test_a_declared_gpus_per_job_on_an_uncounted_gpu_row_is_refused_not_dropped(gpu_home, monkeypatch) -> None:
    """Reviewer's case (2026-09-07): a kind: gpu row with no gpus and no 'N x' device
    line used to take gpus_per_job=2 and write no resources at all. The number is
    refused and the row named as the thing to fix; it is not silently forgotten."""
    monkeypatch.setattr(
        "oncall_flow.connections.get",
        lambda cid: {"host": "h", "port": 1, "kind": "gpu", "device": "NVIDIA A800 + NVIDIA A800", "cores": 128},
    )
    out = await OpsDeclareTool().execute(
        **_args(connection="gpu2", gpus_per_job=2, command="torchrun --nproc_per_node=2 train.py {config}")
    )
    assert out.startswith("REFUSED") and "gpus: N" in out and "does not say how many devices" in out
    assert "cores_per_job" not in out, "a GPU box is never advised to count cores"
    assert not (gpu_home / "beam" / "meta.json").exists()
    out = await OpsDeclareTool().execute(
        **_args(connection="gpu2", command="torchrun --nproc_per_node=2 train.py {config}")
    )
    assert not out.startswith("REFUSED"), "with no per-job number said, the row keeps its job-count gate"
    meta = json.loads((gpu_home / "beam" / "meta.json").read_text())
    assert meta.get("resources", {}) == {}


@pytest.mark.asyncio
async def test_one_device_per_job_and_a_trialless_campaign_pass_an_uncounted_gpu_row(gpu_home, monkeypatch) -> None:
    """The migration table keeps the job-count gate on such a row and blocks only
    gpus_per_job > 1 (reviewer, 2026-09-07): one card per job is what that gate
    already stands for, and a campaign that starts no trial holds nothing."""
    monkeypatch.setattr(
        "oncall_flow.connections.get",
        lambda cid: {"host": "h", "port": 1, "kind": "gpu", "device": "NVIDIA A800 + NVIDIA A800", "cores": 128},
    )
    out = await OpsDeclareTool().execute(**_args(connection="gpu2", gpus_per_job=1, command="python train.py {config}"))
    assert not out.startswith("REFUSED"), out
    assert json.loads((gpu_home / "beam" / "meta.json").read_text()).get("resources", {}) == {}
    out = await OpsDeclareTool().execute(
        **_args(
            campaign="watch",
            connection="gpu2",
            gpus_per_job=2,
            command="",
            objective_kind="condition",
            condition="the queue on the GPU box is empty",
            metric="",
            goal="",
        )
    )
    assert not out.startswith("REFUSED"), out


@pytest.mark.asyncio
async def test_a_gpu_machine_asks_how_many_devices_one_job_holds(gpu_home) -> None:
    out = await OpsDeclareTool().execute(**_args(connection="gpu2"))
    assert out.startswith("REFUSED") and "gpus_per_job" in out and "2 of them" in out
    assert not (gpu_home / "beam" / "meta.json").exists()


@pytest.mark.asyncio
async def test_a_job_larger_than_the_machine_is_refused_at_declare(gpu_home) -> None:
    out = await OpsDeclareTool().execute(**_args(connection="gpu2", gpus_per_job=8))
    assert out.startswith("REFUSED") and "can never start here" in out


@pytest.mark.asyncio
async def test_a_command_that_picks_its_own_card_is_refused(gpu_home) -> None:
    out = await OpsDeclareTool().execute(
        **_args(
            connection="gpu2", gpus_per_job=1, command="cd {staged_case} && CUDA_VISIBLE_DEVICES=0 ./run.sh {config}"
        )
    )
    assert out.startswith("REFUSED") and "CUDA_VISIBLE_DEVICES" in out and "Take it out of the command" in out


@pytest.mark.asyncio
async def test_nproc_per_node_has_to_agree_with_the_declaration(gpu_home) -> None:
    disagree = await OpsDeclareTool().execute(
        **_args(connection="gpu2", gpus_per_job=2, command="torchrun --nproc_per_node=1 train.py {config}")
    )
    assert disagree.startswith("REFUSED") and "have to agree" in disagree
    agree = await OpsDeclareTool().execute(
        **_args(connection="gpu2", gpus_per_job=2, command="torchrun --nproc_per_node=2 train.py {config}")
    )
    assert agree.startswith("Declared"), agree
    meta = json.loads((gpu_home / "beam" / "meta.json").read_text())
    assert meta["resources"] == {"gpus_per_job": 2}
    assert "holds        2 device(s) per job" in agree


@pytest.mark.asyncio
async def test_a_seed_config_naming_a_card_earns_a_note_not_a_refusal(gpu_home) -> None:
    out = await OpsDeclareTool().execute(**_args(connection="gpu2", gpus_per_job=1, seed_config={"gpu": "0", "lr": 1}))
    assert out.startswith("Declared"), out
    assert "the system does not read it" in out


@pytest.mark.asyncio
async def test_a_cpu_machine_asks_for_cores_and_checks_memory(gpu_home) -> None:
    out = await OpsDeclareTool().execute(**_args(connection="cpu32"))
    assert out.startswith("REFUSED") and "cores_per_job" in out and "32 of them" in out

    out = await OpsDeclareTool().execute(**_args(connection="cpu32", cores_per_job=8, memory_per_job_gb=500))
    assert out.startswith("REFUSED") and "232 GB of memory" in out

    out = await OpsDeclareTool().execute(**_args(connection="cpu32", cores_per_job=8, memory_per_job_gb=64))
    assert out.startswith("Declared"), out
    meta = json.loads((gpu_home / "beam" / "meta.json").read_text())
    assert meta["resources"] == {"cores_per_job": 8, "memory_per_job_gb": 64}
    assert "holds        8 core(s), 64 GB per job" in out


@pytest.mark.asyncio
async def test_a_machine_that_hands_out_nothing_countable_is_asked_for_nothing(home) -> None:
    """The legacy row: job-count gate, no new field required."""
    out = await OpsDeclareTool().execute(**_args())
    assert out.startswith("Declared"), out
    assert "resources" not in json.loads((home / "beam" / "meta.json").read_text())
