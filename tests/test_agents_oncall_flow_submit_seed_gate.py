"""Round 0 runs the starting point the campaign declared.

``seed_config`` is the owner's declaration of what the first run is: the code and
its defaults as they were handed over. The submit path has said so in a comment
since it was written -- "round zero's config belongs to whoever set the campaign
up" -- but it only acted on that when ``configs`` was empty, and the status line
told the agent "round 0 runs this unless you change it". So the declaration was
a default, not a declaration.

Measured 2026-08-13, both CFD legs, each having read that line:

  * leg A   seed ``{"deltaT": "5e-4", "run": 1}``  submitted ``{"run": 1}``
  * leg B2  seed ``{"maxCo": 20, "run": 1}``       submitted ``{"maxCo": "1", "run": 1}``

Neither run measured what it was set up to measure, and nothing in either
trail said the starting point had been dropped -- the trial names (``run1``,
``maxCo1_run1``) were the only trace, and they read as ordinary.

The rule is the basis rule moved back one round. From round 1 on, changing the
config requires citing a reading; at round 0 no reading exists yet, so there is
nothing a change could be grounded in. An agent that believes the declared
starting point is wrong has ``ops_ask_owner`` -- the same escape hatch the
meta.json refusal names, for the same reason.

This carries no domain knowledge: it compares the submitted config against the
campaign's own declaration, whatever the keys mean. A campaign that declares no
seed_config is unaffected.
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

from oncall_flow.tools import base as tools_base  # noqa: E402
from oncall_flow.tools.ops import OpsSubmitTool  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


def _bound(tool, scheduler):
    tool.bind_runtime(SimpleNamespace(wake_scheduler=scheduler))
    return tool


class _Job:
    def __init__(self, i):
        self.id = str(i)


class _FakeCron:
    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.messages: list[str] = []
        self.advanced: list[str] = []
        self.cancelled: list[str] = []

    def schedule_wake(self, key, at_ms, message, **route):
        self.jobs = [j for j in self.jobs if j["key"] != key]
        self.jobs.append({"key": key, "at_ms": at_ms, "message": message, **route})
        self.messages.append(message)
        return SimpleNamespace(id=f"oncall-flow:{key}")

    def advance_wake_to_now(self, key):
        self.advanced.append(key)
        return any(j["key"] == key for j in self.jobs)

    def pending_wakes(self, prefix=""):
        return [SimpleNamespace(id=f"oncall-flow:{j['key']}") for j in self.jobs if j["key"].startswith(prefix)]

    def cancel_wake(self, key):
        before = len(self.jobs)
        self.jobs = [j for j in self.jobs if j["key"] != key]
        self.cancelled.append(key)
        return len(self.jobs) < before


def _campaign(tmp_path: Path, seed) -> Path:
    cdir = tmp_path / "c"
    cdir.mkdir(exist_ok=True)
    meta = {
        "backend": "process",
        "host": "h",
        "port": 22,
        "key": "~/.ssh/id_rsa",
        "command": "run {config} {job_dir}",
        "budget": {"unit": "core-minute", "total": 150, "overlap": "additive"},
    }
    if seed is not None:
        meta["seed_config"] = seed
    (cdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return cdir


def _install(monkeypatch):
    from types import SimpleNamespace

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
            return SimpleNamespace(backend="process", job_id=f"ops-{spec.idem_key}")

        async def poll(self, handle):
            from oncall_flow.backend import JobStatus

            return JobStatus.RUNNING

    monkeypatch.setattr("oncall_flow.backends.backend_from_meta", lambda meta: _Backend())
    monkeypatch.setattr("oncall_flow.backends.prepare_from_meta", lambda *a, **k: None, raising=False)
    return submitted


def _observed(cdir: Path) -> None:
    from oncall_flow.state_claims import StateFacts, write_facts

    write_facts(cdir, StateFacts(metric_readings={"ndcg": (0.29,)}, probe_seq=1))


def _tool():
    t = _bound(OpsSubmitTool(), _FakeCron())
    t.set_context("cli", "direct")
    return t


async def _submit(cdir: Path, **kw):
    kw.setdefault("host", "h")
    kw.setdefault("objective", "o")
    kw.setdefault("eta_seconds", 60)
    kw.setdefault("campaign", "c")
    return await _tool().execute(ledger=str(cdir / "ledger.json"), **kw)


@pytest.mark.asyncio
async def test_round_zero_runs_the_declared_start(tmp_path, monkeypatch):
    cdir = _campaign(tmp_path, {"deltaT": "5e-4", "run": 1})
    sent = _install(monkeypatch)
    out = await _submit(cdir, configs=[{"deltaT": "5e-4", "run": 1}], round=0)
    assert "Submitted" in out
    assert sent and sent[0]["deltaT"] == "5e-4"


@pytest.mark.asyncio
async def test_an_empty_round_zero_still_takes_the_seed(tmp_path, monkeypatch):
    """The behaviour that already worked: no configs means run the declaration."""
    cdir = _campaign(tmp_path, {"deltaT": "5e-4", "run": 1})
    sent = _install(monkeypatch)
    out = await _submit(cdir, configs=[], round=0)
    assert "Submitted" in out
    assert sent[0]["deltaT"] == "5e-4"


@pytest.mark.asyncio
async def test_a_dropped_key_refuses(tmp_path, monkeypatch):
    """Leg A: the declared key simply was not passed on, so the job ran defaults."""
    cdir = _campaign(tmp_path, {"deltaT": "5e-4", "run": 1})
    sent = _install(monkeypatch)
    out = await _submit(cdir, configs=[{"run": 1}], round=0)

    assert "REFUSED" in out
    assert "deltaT" in out, "naming the key is the whole content of the refusal"
    assert "ops_ask_owner" in out, "refusing without naming the way out just blocks it"
    assert sent == [], "nothing may reach the host"
    kinds = [json.loads(l).get("kind") for l in (cdir / "events.jsonl").read_text().splitlines()]
    assert "seed_refused" in kinds


@pytest.mark.asyncio
async def test_a_changed_value_refuses(tmp_path, monkeypatch):
    """Leg B2: maxCo 20 was replaced by 1 before it had ever been run."""
    cdir = _campaign(tmp_path, {"maxCo": 20, "run": 1})
    sent = _install(monkeypatch)
    out = await _submit(cdir, configs=[{"maxCo": "1", "run": 1}], round=0)
    assert "REFUSED" in out and "maxCo" in out
    assert sent == []


@pytest.mark.asyncio
async def test_the_same_value_spelled_differently_is_the_same_start(tmp_path, monkeypatch):
    """5e-4 and 0.0005 are one number. Refusing that would be a false refusal."""
    cdir = _campaign(tmp_path, {"deltaT": "5e-4", "run": 1})
    sent = _install(monkeypatch)
    out = await _submit(cdir, configs=[{"deltaT": 0.0005, "run": "1"}], round=0)
    assert "Submitted" in out, out
    assert sent


@pytest.mark.asyncio
async def test_an_extra_key_refuses(tmp_path, monkeypatch):
    """Adding a knob changes the starting point as surely as removing one."""
    cdir = _campaign(tmp_path, {"maxCo": 20, "run": 1})
    sent = _install(monkeypatch)
    out = await _submit(cdir, configs=[{"maxCo": 20, "run": 1, "pTol": "1e-9"}], round=0)
    assert "REFUSED" in out and "pTol" in out
    assert sent == []


@pytest.mark.asyncio
async def test_a_batch_at_round_zero_refuses(tmp_path, monkeypatch):
    """A sweep alongside the declared start is not the declared start being run
    on its own -- it spends the budget on configs nobody asked for yet."""
    cdir = _campaign(tmp_path, {"maxCo": 20, "run": 1})
    sent = _install(monkeypatch)
    out = await _submit(cdir, configs=[{"maxCo": 20, "run": 1}, {"maxCo": 5, "run": 2}], round=0)
    assert "REFUSED" in out
    assert sent == []


@pytest.mark.asyncio
async def test_a_declared_batch_is_matched_as_a_batch(tmp_path, monkeypatch):
    """seed_config may be a list; then that list is what round 0 runs."""
    cdir = _campaign(tmp_path, [{"maxCo": 20, "run": 1}, {"maxCo": 5, "run": 2}])
    sent = _install(monkeypatch)
    out = await _submit(cdir, configs=[{"maxCo": 20, "run": 1}, {"maxCo": 5, "run": 2}], round=0)
    assert "Submitted" in out, out
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_round_one_may_change_anything(tmp_path, monkeypatch):
    """The point of the campaign. Once there is a reading, the config is the
    agent's to move -- the basis gate governs that, not this one."""
    cdir = _campaign(tmp_path, {"maxCo": 20, "run": 1})
    sent = _install(monkeypatch)
    await _submit(cdir, configs=[{"maxCo": 20, "run": 1}], round=0)
    _observed(cdir)
    out = await _submit(
        cdir, configs=[{"maxCo": 1, "run": 2}], round=1, basis="ndcg 0.29 on the latest sample; maxCo 20 stalled"
    )
    assert "Submitted" in out, out
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_a_campaign_that_already_has_records_is_past_its_start(tmp_path, monkeypatch):
    """A handover is round 0 for this shift, not for the campaign.

    The gate holds because at round 0 no reading exists, so a change has nothing
    to be grounded in. When trials are already in the ledger that premise is
    simply false -- their scores, curves and spend are what the incoming shift is
    supposed to read and act on.

    Enforcing it there would be worse than useless. The M9 handover fixture
    declares the seed its previous shift already ran; refusing the newcomer's
    config would send it to run a configuration whose result is sitting in the
    ledger -- spending a wake cycle on a submit that idempotency will not even
    start.
    """
    cdir = _campaign(tmp_path, {"lr": "5e-06", "epochs": 8})
    sent = _install(monkeypatch)
    (cdir / "ledger.json").write_text(
        json.dumps(
            {
                "version": 1,
                "records": {
                    "lr5em06_epochs8": {
                        "idem_key": "lr5em06_epochs8",
                        "status": "succeeded",
                        "campaign": "c",
                        "handle": None,
                        "attempts": 1,
                        "escalated": False,
                        "metrics": {"ndcg": 0.3255},
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    out = await _submit(cdir, configs=[{"lr": "1e-05", "epochs": 4}], round=0)
    assert "Submitted" in out, out
    assert sent and sent[0]["lr"] == "1e-05"


@pytest.mark.asyncio
async def test_a_campaign_with_no_declaration_is_unaffected(tmp_path, monkeypatch):
    cdir = _campaign(tmp_path, None)
    sent = _install(monkeypatch)
    out = await _submit(cdir, configs=[{"whatever": 3}], round=0)
    assert "Submitted" in out
    assert sent
