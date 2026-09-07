"""The machine is one machine, however many campaigns are on it.

Measured 2026-08-31 on the A800 box: two campaigns (baseline-a800,
baseline-train-a800) each submitted a trial within two minutes, both landed on
device 0, and the second died of CUDA OOM inside the first one's memory. The
registry row said ``concurrency: 1``; each campaign read only its own ledger,
where everything looked fine. The same evening the two campaigns carried the
same idem_key -- one measurement, bought twice.

Both gates read across every sibling campaign under the same ops home, and both
follow the refuse-on-contradiction rule: no connection in the meta, no
``concurrency`` on the row, an unreadable ledger -- each leaves the submit
exactly as it was.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.backend import JobHandle, JobResult, JobStatus  # noqa: E402
from oncall_flow.ledger import Ledger  # noqa: E402
from oncall_flow.proposer import config_key  # noqa: E402
from oncall_flow.tools import base as tools_base  # noqa: E402
from oncall_flow.tools.ops import OpsSubmitTool  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


class _FakeCron:
    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def schedule_wake(self, key, at_ms, message, **route):
        self.jobs = [j for j in self.jobs if j["key"] != key]
        self.jobs.append({"key": key, "at_ms": at_ms, "message": message, **route})
        return SimpleNamespace(id=f"oncall-flow:{key}")

    def advance_wake_to_now(self, key):
        return any(j["key"] == key for j in self.jobs)

    def pending_wakes(self, prefix=""):
        return [SimpleNamespace(id=f"oncall-flow:{j['key']}") for j in self.jobs if j["key"].startswith(prefix)]

    def cancel_wake(self, key):
        before = len(self.jobs)
        self.jobs = [j for j in self.jobs if j["key"] != key]
        return len(self.jobs) < before


def _campaign(tmp_path: Path, name: str, *, connection: str = "m1") -> Path:
    cdir = tmp_path / name
    cdir.mkdir(exist_ok=True)
    meta = {
        "backend": "process",
        "host": "h",
        "port": 22,
        "key": "~/.ssh/id_rsa",
        "command": "run {config} {job_dir}",
        "budget": {"unit": "core-minute", "total": 150, "overlap": "additive"},
    }
    if connection:
        meta["connection"] = connection
    (cdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return cdir


def _running_job(
    cdir: Path, idem_key: str, *, campaign: str | None = None, terminal: bool = False, held: dict | None = None
) -> None:
    """A sibling campaign's ledger with one job, running or finished."""
    led = Ledger(cdir / "ledger.json")
    led.record(idem_key, campaign=campaign or cdir.name, config={"k": idem_key}, resources_held=held)
    led.set_handle(idem_key, JobHandle(backend="process", job_id=f"ops-{idem_key}"))
    if terminal:
        led.set_result(idem_key, JobResult(JobStatus.SUCCEEDED))
    else:
        led.set_status(idem_key, JobStatus.RUNNING)


def _observed(cdir: Path) -> None:
    """A recorded probe, so round >= 1 can cite one -- exactly as a real turn does."""
    from oncall_flow.state_claims import StateFacts, write_facts

    write_facts(cdir, StateFacts(metric_readings={"metric": (0.5,)}, probe_seq=1))


def _install(monkeypatch, *, concurrency=1, row_extra: dict | None = None, labels: list | None = None):
    submitted: list[dict] = []

    class _Backend:
        _run = staticmethod(lambda cmd: (0, ""))

        async def spent_minutes(self):
            return 0.0

        async def remaining_minutes(self):
            return 150.0

        def unmeasured_spend(self):
            return {}

        async def submit(self, spec):
            submitted.append(dict(spec.payload))
            if labels is not None:
                labels.append(dict(spec.labels))
            return SimpleNamespace(backend="process", job_id=f"ops-{spec.idem_key}")

        async def poll(self, handle):
            return JobStatus.RUNNING

    monkeypatch.setattr("oncall_flow.backends.backend_from_meta", lambda meta: _Backend())
    monkeypatch.setattr("oncall_flow.backends.prepare_from_meta", lambda *a, **k: None, raising=False)
    row = {"id": "m1", "display_name": "GPU box"}
    if concurrency is not None:
        row["concurrency"] = concurrency
    row.update(row_extra or {})
    monkeypatch.setattr("oncall_flow.connections.get", lambda cid: row if cid == "m1" else None)
    monkeypatch.setattr("oncall_flow.connections.display_name", lambda cid: "GPU box")
    return submitted


def _tool():
    t = OpsSubmitTool()
    t.bind_runtime(SimpleNamespace(wake_scheduler=_FakeCron()))
    t.set_context("cli", "direct")
    return t


async def _submit(cdir: Path, **kw):
    kw.setdefault("host", "h")
    kw.setdefault("objective", "o")
    kw.setdefault("eta_seconds", 60)
    kw.setdefault("campaign", cdir.name)
    return await _tool().execute(ledger=str(cdir / "ledger.json"), **kw)


@pytest.mark.asyncio
async def test_a_sibling_campaigns_running_job_fills_the_machine(tmp_path, monkeypatch):
    """The incident: concurrency 1, a sibling's trial running, a new submit."""
    other = _campaign(tmp_path, "other")
    _running_job(other, "trialA")
    mine = _campaign(tmp_path, "mine")
    sent = _install(monkeypatch, concurrency=1)

    out = await _submit(mine, configs=[{"lr": 1}], round=0)

    assert "REFUSED" in out
    assert "other" in out, "the occupant is named, so the agent can go read it"
    assert "trialA" in out
    assert sent == [], "nothing may land on the busy machine"
    kinds = [json.loads(line).get("kind") for line in (mine / "events.jsonl").read_text().splitlines()]
    assert "capacity_refused" in kinds


@pytest.mark.asyncio
async def test_a_finished_sibling_job_frees_the_machine(tmp_path, monkeypatch):
    other = _campaign(tmp_path, "other")
    _running_job(other, "trialA", terminal=True)
    mine = _campaign(tmp_path, "mine")
    sent = _install(monkeypatch, concurrency=1)

    out = await _submit(mine, configs=[{"lr": 1}], round=0)

    assert "Submitted" in out
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_no_concurrency_on_the_row_gates_nothing(tmp_path, monkeypatch):
    """Refuse on contradiction, never on uncertainty."""
    other = _campaign(tmp_path, "other")
    _running_job(other, "trialA")
    mine = _campaign(tmp_path, "mine")
    sent = _install(monkeypatch, concurrency=None)

    out = await _submit(mine, configs=[{"lr": 1}], round=0)

    assert "Submitted" in out
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_campaign_with_no_connection_gates_nothing(tmp_path, monkeypatch):
    other = _campaign(tmp_path, "other", connection="m1")
    _running_job(other, "trialA")
    mine = _campaign(tmp_path, "mine", connection="")
    sent = _install(monkeypatch, concurrency=1)

    out = await _submit(mine, configs=[{"lr": 1}], round=0)

    assert "Submitted" in out
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_the_same_trial_in_a_live_sibling_is_not_bought_twice(tmp_path, monkeypatch):
    """The other half of the incident: one idem_key under two campaign names."""
    cfg = {"lr": 1}
    other = _campaign(tmp_path, "other")
    _running_job(other, config_key(cfg), terminal=True)
    mine = _campaign(tmp_path, "mine")
    sent = _install(monkeypatch, concurrency=4)

    out = await _submit(mine, configs=[cfg], round=0)

    assert "other" in out, "the owning campaign is named"
    assert "ops_tune_status" in out, "refusing without naming the way out just blocks it"
    assert sent == [], "the measurement exists; it is not bought again"
    assert Ledger(mine / "ledger.json").all() == [], "a refused duplicate leaves no orphan record"


@pytest.mark.asyncio
async def test_a_concluded_siblings_keys_are_free_to_reproduce(tmp_path, monkeypatch):
    cfg = {"lr": 1}
    other = _campaign(tmp_path, "other")
    _running_job(other, config_key(cfg), terminal=True)
    (other / "concluded.json").write_text("{}", encoding="utf-8")
    mine = _campaign(tmp_path, "mine")
    sent = _install(monkeypatch, concurrency=4)

    out = await _submit(mine, configs=[cfg], round=0)

    assert "Submitted" in out
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_retry_inside_the_same_campaign_is_not_a_duplicate(tmp_path, monkeypatch):
    """The ledger's own idempotency handles a same-campaign retry; the gate must not."""
    cfg = {"lr": 1}
    mine = _campaign(tmp_path, "mine")
    _running_job(mine, config_key(cfg), terminal=True)
    _observed(mine)
    sent = _install(monkeypatch, concurrency=4)

    out = await _submit(mine, configs=[cfg], round=1, basis="metric 0.5 on the latest probe; retrying")

    assert "duplicate" not in out.lower()
    assert "bought" not in out.lower()


# --- check-and-reserve is one step -------------------------------------------


def _reserved_job(cdir: Path, idem_key: str, *, age_s: float = 0.0) -> None:
    """A record written by a submit whose backend call has not returned a handle."""
    led = Ledger(cdir / "ledger.json")
    led.record(idem_key, campaign=cdir.name, config={"k": idem_key})
    if age_s:
        rec = led.get(idem_key)
        rec.reserved_at -= age_s
        led._persist()


def test_a_fresh_handle_less_record_holds_the_machine(tmp_path):
    """Between record() and set_handle() the record IS the reservation."""
    from oncall_flow.occupancy import running_on

    _reserved_job(_campaign(tmp_path, "a"), "k1")
    occupants = running_on(tmp_path, "m1")
    assert [(o.campaign, o.idem_key, o.status) for o in occupants] == [("a", "k1", "reserved")]


def test_a_stale_handle_less_record_is_an_orphan_and_does_not(tmp_path):
    from oncall_flow.occupancy import RESERVATION_GRACE_S, running_on

    _reserved_job(_campaign(tmp_path, "a"), "k1", age_s=RESERVATION_GRACE_S + 1)
    assert running_on(tmp_path, "m1") == []


def test_a_record_from_before_reservations_is_skipped_as_before(tmp_path):
    from oncall_flow.occupancy import running_on

    cdir = _campaign(tmp_path, "a")
    led = Ledger(cdir / "ledger.json")
    led.record("k1", campaign="a", config={})
    led.get("k1").reserved_at = None
    led._persist()
    assert Ledger(cdir / "ledger.json").get("k1").reserved_at is None
    assert running_on(tmp_path, "m1") == []


def test_the_reservation_lock_is_exclusive_across_the_ops_home(tmp_path):
    """Taken through the host's portable lock (portalocker), so a Windows-hosted
    agent can submit at all; a direct fcntl import refused every submit there."""
    from oncall_flow.occupancy import _LOCK_NAME, reservation_lock

    from raven.utils.portable_lock import LockTimeoutError, file_lock

    with reservation_lock(tmp_path):
        with pytest.raises(LockTimeoutError):
            with file_lock(tmp_path / _LOCK_NAME, blocking=False):
                pass
    with file_lock(tmp_path / _LOCK_NAME, blocking=False):
        pass


@pytest.mark.asyncio
async def test_a_submit_in_flight_already_counts_against_the_machine(tmp_path, monkeypatch):
    """The window the reviewer reproduced: one campaign has recorded and is
    awaiting the backend; a sibling reads the gate. It must see the machine
    taken, not free."""
    import asyncio

    from oncall_flow.occupancy import capacity_refusal

    _install(monkeypatch, concurrency=1)
    a = _campaign(tmp_path, "a")
    seen: dict = {}
    gate = asyncio.Event()

    class _SlowBackend:
        _run = staticmethod(lambda cmd: (0, ""))

        async def spent_minutes(self):
            return 0.0

        async def remaining_minutes(self):
            return 150.0

        def unmeasured_spend(self):
            return {}

        async def submit(self, spec):
            # The sibling's view while this submit is in flight.
            seen["occupants"] = [
                o.status for o in __import__("oncall_flow.occupancy", fromlist=["x"]).running_on(tmp_path, "m1")
            ]
            seen["refusal"] = capacity_refusal(tmp_path, "m1", incoming=1, concurrency=1, display="GPU box")
            gate.set()
            return SimpleNamespace(backend="process", job_id=f"ops-{spec.idem_key}")

        async def poll(self, handle):
            return JobStatus.RUNNING

    monkeypatch.setattr("oncall_flow.backends.backend_from_meta", lambda meta: _SlowBackend())
    out = await _submit(a, round=0, configs=[{"x": 1}])
    assert gate.is_set()
    assert seen["occupants"] == ["reserved"], out
    assert seen["refusal"] is not None and "reserved" in seen["refusal"]
    # Once the handle lands the same record counts as the running job it is.
    rec = Ledger(a / "ledger.json").all()[0]
    assert rec.handle is not None


# ---- admission by what a job holds (owner's rulings, 2026-09-03) ----


@pytest.mark.asyncio
async def test_two_single_device_jobs_fit_a_two_device_machine_and_get_distinct_cards(tmp_path, monkeypatch):
    """The run11 refusal: concurrency 1 on a two-card box sent the second card's
    jobs around the ledger. Admitted by devices, both fit, and each is handed its
    own card -- the model never picks one."""
    mine = _campaign(tmp_path, "mine")
    labels: list[dict] = []
    sent = _install(monkeypatch, concurrency=1, row_extra={"kind": "gpu", "gpus": 2}, labels=labels)

    out = await _submit(mine, configs=[{"lr": 1}, {"lr": 2}], round=0)

    assert "Submitted 2" in out, out
    assert len(sent) == 2
    assert [lab["device_ids"] for lab in labels] == ["0", "1"]
    assert [lab["width"] for lab in labels] == ["1", "1"]
    recs = sorted(Ledger(mine / "ledger.json").all(), key=lambda r: r.config["lr"])
    assert recs[0].resources_held == {"gpus": 1, "device_ids": ["0"]}
    assert recs[1].resources_held == {"gpus": 1, "device_ids": ["1"]}


@pytest.mark.asyncio
async def test_a_job_that_needs_the_whole_machine_waits_for_a_held_card(tmp_path, monkeypatch):
    other = _campaign(tmp_path, "other")
    _running_job(other, "trialA", held={"gpus": 1, "device_ids": ["0"]})
    mine = _campaign(tmp_path, "mine")
    sent = _install(monkeypatch, concurrency=1, row_extra={"kind": "gpu", "gpus": 2})

    out = await _submit(mine, configs=[{"lr": 1, "gpus_needed": 2}], round=0)

    assert "REFUSED" in out and "2 device(s)" in out and "1 is/are held" in out, out
    assert "other: trialA" in out and "ids 0" in out, "the holder and its card are named"
    assert sent == []


@pytest.mark.asyncio
async def test_a_held_card_is_skipped_when_the_next_job_is_assigned(tmp_path, monkeypatch):
    other = _campaign(tmp_path, "other")
    _running_job(other, "trialA", held={"gpus": 1, "device_ids": ["0"]})
    mine = _campaign(tmp_path, "mine")
    labels: list[dict] = []
    _install(monkeypatch, concurrency=1, row_extra={"kind": "gpu", "gpus": 2}, labels=labels)

    out = await _submit(mine, configs=[{"lr": 1}], round=0)

    assert "Submitted 1" in out, out
    assert labels[0]["device_ids"] == "1"


@pytest.mark.asyncio
async def test_a_request_above_the_machine_is_refused_outright_not_queued(tmp_path, monkeypatch):
    mine = _campaign(tmp_path, "mine")
    sent = _install(monkeypatch, concurrency=1, row_extra={"kind": "gpu", "gpus": 2})

    out = await _submit(mine, configs=[{"lr": 1, "gpus_needed": 8}], round=0)

    assert "REFUSED" in out and "can never start here" in out, out
    assert sent == []


@pytest.mark.asyncio
async def test_a_record_from_before_resources_holds_one_unit(tmp_path, monkeypatch):
    """What it was billed as. A campaign in flight when this shipped keeps running."""
    other = _campaign(tmp_path, "other")
    _running_job(other, "trialA")
    mine = _campaign(tmp_path, "mine")
    sent = _install(monkeypatch, concurrency=1, row_extra={"kind": "gpu", "gpus": 2})

    out = await _submit(mine, configs=[{"lr": 1}, {"lr": 2}], round=0)

    assert "REFUSED" in out and "1 is/are held" in out, out
    assert sent == []


@pytest.mark.asyncio
async def test_cores_are_counted_the_same_way_on_a_cpu_machine(tmp_path, monkeypatch):
    other = _campaign(tmp_path, "other")
    _running_job(other, "solveA", held={"cores": 24})
    mine = _campaign(tmp_path, "mine")
    labels: list[dict] = []
    sent = _install(monkeypatch, concurrency=1, row_extra={"kind": "cpu", "cores": 32}, labels=labels)

    out = await _submit(mine, configs=[{"nx": 1, "cores_needed": 8}], round=0)
    assert "Submitted 1" in out, out
    assert labels[0] == {"campaign": "mine", "width": "8"}, "cores are counted, not pinned to ids"

    again = _campaign(tmp_path, "again")
    out = await _submit(again, configs=[{"nx": 2, "cores_needed": 8}], round=0)
    assert "REFUSED" in out and "32 core(s) and 32 is/are held" in out, out
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_memory_is_checked_only_when_both_sides_declared_it(tmp_path, monkeypatch):
    other = _campaign(tmp_path, "other")
    _running_job(other, "solveA", held={"cores": 8, "memory_gb": 200})
    mine = _campaign(tmp_path, "mine")
    sent = _install(monkeypatch, concurrency=1, row_extra={"kind": "cpu", "cores": 32, "memory": "232 GB"})

    out = await _submit(mine, configs=[{"nx": 1, "cores_needed": 8, "memory_needed_gb": 64}], round=0)
    assert "REFUSED" in out and "232 GB of memory and 200 GB is/are held" in out, out

    out = await _submit(mine, configs=[{"nx": 1, "cores_needed": 8}], round=0)
    assert "Submitted 1" in out, "an undeclared memory need is not checked"
    assert len(sent) == 1


def test_free_device_ids_are_lowest_first_and_skip_what_is_held(tmp_path):
    from oncall_flow.occupancy import free_device_ids

    other = _campaign(tmp_path, "other")
    _running_job(other, "trialA", held={"gpus": 2, "device_ids": ["1", "2"]})

    assert free_device_ids(tmp_path, "m1", 4) == ["0", "3"]


def test_a_live_job_from_before_device_ids_reserves_the_lowest_free_ids(tmp_path):
    """The upgrade case: a record written before resources were recorded holds
    one device somewhere and does not say which. Counting it for admission but
    not for placement handed a newcomer device 0 while the legacy job sat on it
    -- the collision this layer exists to prevent. It takes the lowest free ids,
    as many as it is billed for; two such jobs on a two-card box leave nothing."""
    from oncall_flow.occupancy import admission_refusal, free_device_ids

    other = _campaign(tmp_path, "other")
    _running_job(other, "legacyA", held=None)

    assert admission_refusal(tmp_path, "m1", unit="gpus", capacity=2, requests=[1]) is None, "one card is free"
    assert free_device_ids(tmp_path, "m1", 2) == ["1"], "and it is not the one the legacy job most likely holds"

    _running_job(other, "legacyB", held={"gpus": 1})
    assert free_device_ids(tmp_path, "m1", 2) == []
    assert admission_refusal(tmp_path, "m1", unit="gpus", capacity=2, requests=[1]) is not None

    pinned = _campaign(tmp_path, "pinned")
    _running_job(pinned, "trialC", held={"gpus": 1, "device_ids": ["0"]})
    assert free_device_ids(tmp_path, "m1", 4) == ["3"], "explicit ids are skipped; the two unplaced jobs take 1 and 2"
