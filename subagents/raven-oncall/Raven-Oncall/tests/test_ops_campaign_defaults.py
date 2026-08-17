"""A task statement should not have to carry plumbing the harness already holds.

Two things were still being typed into every task text on 2026-08-06, and neither
is a fact the person asking for the work has any reason to know:

  - the starting config, which belongs to whoever set the campaign up (the code and
    its defaults are the upstream deliverable);
  - the campaign name and its ledger path, which is an API handle -- "state lives
    over there" -- that leaked into a prompt.

Both are resolvable from the campaign itself. The rule for both is the same: fill
in from state only when there is exactly one answer, and say so plainly when there
is not. A default that silently picks one of several campaigns would be the worst
of the three outcomes, because the wrong campaign looks exactly like the right one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.tools.ops import OpsCheckLaterTool, OpsSubmitTool, OpsTuneStatusTool, _resolve_campaign_dir
from raven.config import paths as config_paths

SEED = {"lr": 2e-5, "queries_per_step": 16, "negatives": 3, "max_len": 256, "epochs": 8}


def _home(monkeypatch, tmp_path: Path):
    root = tmp_path / "ops"
    monkeypatch.setattr(config_paths, "get_ops_home", lambda: root)
    return root


def _campaign(root: Path, name: str, *, seed: dict | None = None) -> Path:
    cdir = root / name
    cdir.mkdir(parents=True, exist_ok=True)
    meta = {"backend": "process", "host": "h", "remote_dir": "/r", "command": "c", "budget_minutes_total": 140}
    if seed is not None:
        meta["seed_config"] = seed
    (cdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (cdir / "ledger.json").write_text(json.dumps({"version": 1, "records": {}}), encoding="utf-8")
    return cdir


def test_the_only_campaign_is_found_without_being_named(tmp_path: Path, monkeypatch) -> None:
    root = _home(monkeypatch, tmp_path)
    cdir = _campaign(root, "armb-embed-r14e")

    assert _resolve_campaign_dir("", None) == cdir
    assert _resolve_campaign_dir(None, None) == cdir


def test_two_campaigns_are_not_guessed_between(tmp_path: Path, monkeypatch) -> None:
    """Picking one silently is worse than refusing: the wrong campaign reads exactly
    like the right one, and the loop would drive somebody else's experiment."""
    root = _home(monkeypatch, tmp_path)
    _campaign(root, "armb-embed-r14e")
    _campaign(root, "cfd-transient")

    with pytest.raises(ValueError) as exc:
        _resolve_campaign_dir("", None)
    assert "cfd-transient" in str(exc.value) and "armb-embed-r14e" in str(exc.value)


def test_an_explicit_name_still_wins(tmp_path: Path, monkeypatch) -> None:
    root = _home(monkeypatch, tmp_path)
    _campaign(root, "armb-embed-r14e")
    other = _campaign(root, "cfd-transient")

    assert _resolve_campaign_dir("cfd-transient", None) == other


@pytest.mark.asyncio
async def test_status_needs_no_ledger_when_there_is_one_campaign(tmp_path: Path, monkeypatch) -> None:
    root = _home(monkeypatch, tmp_path)
    _campaign(root, "armb-embed-r14e")

    out = await OpsTuneStatusTool().execute(metric="ndcg")

    assert "No campaign state" not in out
    assert "Campaign" in out


@pytest.mark.asyncio
async def test_submit_uses_the_campaigns_seed_config_when_none_is_given(tmp_path: Path, monkeypatch) -> None:
    """Round zero's config comes from the campaign, not from the person asking."""
    root = _home(monkeypatch, tmp_path)
    cdir = _campaign(root, "armb-embed-r14e", seed=SEED)

    submitted: list[dict] = []

    class _Backend:
        name = "process"

        async def submit(self, spec):
            submitted.append(dict(spec.payload))
            from raven.ops.backend import JobHandle

            return JobHandle("process", f"ops-{spec.idem_key}")

    from raven.ops import backends as ops_backends

    monkeypatch.setattr(ops_backends, "backend_from_meta", lambda meta: _Backend())
    monkeypatch.setattr(ops_backends, "prepare_from_meta", lambda meta, app_dir: None)

    tool = OpsSubmitTool(None)
    tool.set_context("tui", "default")
    out = await tool.execute(objective="maximize ndcg", eta_seconds=600)

    assert "REFUSED" not in out and "no config" not in out.lower(), out
    assert submitted and submitted[0]["lr"] == pytest.approx(2e-5)


@pytest.mark.asyncio
async def test_submit_says_so_when_there_is_no_seed_and_no_config(tmp_path: Path, monkeypatch) -> None:
    """Absent must read as absent. A submit with nothing to run has to say that,
    not launch an empty config and look like it worked."""
    root = _home(monkeypatch, tmp_path)
    _campaign(root, "armb-embed-r14e")  # no seed_config

    tool = OpsSubmitTool(None)
    tool.set_context("tui", "default")
    out = await tool.execute(objective="maximize ndcg", eta_seconds=600)

    assert "no config" in out.lower() or "seed_config" in out


@pytest.mark.asyncio
async def test_check_later_needs_no_campaign_name(tmp_path: Path, monkeypatch) -> None:
    root = _home(monkeypatch, tmp_path)
    cdir = _campaign(root, "armb-embed-r14e")
    import dataclasses

    from raven.ops.state_claims import read_facts, write_facts

    facts = read_facts(cdir)
    write_facts(
        cdir, dataclasses.replace(facts, metric_readings={"ndcg": (0.3046,)}, probe_seq=1, shown_values=(0.3046,))
    )

    tool = OpsCheckLaterTool(None)
    tool.set_context("tui", "default")
    out = await tool.execute(eta_seconds=600, basis="ndcg 0.3046, below the starting value")

    assert "REFUSED" not in out, out


def test_the_tools_say_a_campaign_is_already_there() -> None:
    """Making the handle optional is not enough on its own.

    Measured 2026-08-06: with the campaign name and ledger path removed from the
    task text, two arms on different models both went straight to raw ssh -- one
    scanned ports 22/2222/8022/10022 and then asked the user for the port -- and
    neither called a single ops tool. The schema allowed omitting the handle;
    nothing told them a campaign existed at all, so there was nothing to omit it
    from. A capability the loop cannot know about is a capability it does not have.
    """
    from raven.agent.tools.ops import OpsSubmitTool, OpsTuneStatusTool

    status = OpsTuneStatusTool().description
    assert "NO ARGUMENTS" in status
    # what the campaign holds, so reading it first is obviously worth doing
    for held in ("budget", "operating policy", "starting"):
        assert held in status.lower()

    submit = OpsSubmitTool(None).description
    assert "already set up" in submit
    # and the alternative is named as wrong, not merely unmentioned
    assert "ssh" in submit.lower()


def test_the_resident_tool_notes_point_at_the_campaign_first() -> None:
    """TOOLS.md is in BOOTSTRAP_FILES, so unlike a skill it is in the system prompt
    on every turn -- measured on the same day, skills reached 0 of 2 wake turns
    while this file was present throughout. So the instruction to read the campaign
    before hunting for the machine belongs here."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "raven" / "templates" / "TOOLS.md").read_text(encoding="utf-8")

    assert "ops_tune_status` with no arguments" in text
    assert "do not ssh to the host to hunt for paths or ports" in text


async def test_the_wake_message_does_not_pin_a_metric_the_campaign_declares(tmp_path: Path, monkeypatch) -> None:
    """A wake message that spells out metric='ndcg' overrides whatever the campaign
    declared, and the wake turn has no history to notice with. The status tool reads
    the campaign's objective itself now, so naming a metric here can only be wrong --
    it was hardcoded to an embedding benchmark's metric while the same tools run CFD.
    """
    root = _home(monkeypatch, tmp_path)
    cdir = _campaign(root, "cfd-transient")
    meta = json.loads((cdir / "meta.json").read_text(encoding="utf-8"))
    meta["objective"] = {"metric": "residual", "direction": "min"}
    (cdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    import dataclasses

    from raven.ops.state_claims import read_facts, write_facts

    facts = read_facts(cdir)
    write_facts(
        cdir, dataclasses.replace(facts, metric_readings={"residual": (0.004,)}, probe_seq=1, shown_values=(0.004,))
    )

    from raven.proactive_engine.schedulers.cron.service import CronService

    svc = CronService(tmp_path / "jobs.json", allowed_channels={"tui"})
    tool = OpsCheckLaterTool(svc)
    tool.set_context("tui", "default")
    out = await tool.execute(eta_seconds=600, basis="residual 0.004, still falling")

    assert "REFUSED" not in out, out
    wakes = [j for j in svc.list_jobs() if j.name.startswith("ops:cfd-transient:")]
    assert wakes, "the re-check has to actually be scheduled or this proves nothing"
    message = wakes[0].payload.message
    assert "metric=" not in message, f"the campaign already says what it optimises; message was: {message}"


async def test_an_omitted_campaign_still_names_the_wake_and_the_ledger(tmp_path: Path, monkeypatch) -> None:
    """Making the handle optional was only half the change, and the other half was
    missing here. ops_submit backfills the resolved name; the rest of the tools kept
    using the empty string they were called with, so:

      - the wake was named ``ops::recheck``, which is outside the ``ops:<campaign>:``
        prefix every dedup and re-arm check keys on -- the "one pending wake per
        campaign" invariant simply did not apply to it;
      - the wake message read ``[Ops campaign '' re-check] Call
        ops_tune_status(ledger='')`` -- and a wake turn starts with no history, so
        that message is the entire context it gets.

    r13 and r14 both passed the name explicitly, so this stayed latent -- while
    TOOLS.md was already telling the loop it could omit it. ops_finish and ops_note
    still require the name, so they cannot reach this; ops_kill takes the resolved
    directory and never composes a prefix.
    """
    root = _home(monkeypatch, tmp_path)
    cdir = _campaign(root, "cfd-transient")
    import dataclasses

    from raven.ops.state_claims import read_facts, write_facts
    from raven.proactive_engine.schedulers.cron.service import CronService

    write_facts(
        cdir,
        dataclasses.replace(
            read_facts(cdir), metric_readings={"residual": (0.004,)}, probe_seq=1, shown_values=(0.004,)
        ),
    )
    svc = CronService(tmp_path / "jobs.json", allowed_channels={"tui"})
    tool = OpsCheckLaterTool(svc)
    tool.set_context("tui", "default")

    out = await tool.execute(eta_seconds=600, basis="residual 0.004, still falling")

    assert "REFUSED" not in out, out
    wakes = [j for j in svc.list_jobs() if j.name.startswith("ops:cfd-transient:")]
    assert wakes, f"wake fell outside the campaign prefix; jobs were {[j.name for j in svc.list_jobs()]}"
    message = wakes[0].payload.message
    assert "cfd-transient" in message, message
    assert str(cdir / "ledger.json") in message, message


async def test_round_zero_records_the_objective_it_was_given(tmp_path: Path, monkeypatch) -> None:
    """Nothing in the product wrote the campaign's objective, so every reader of it
    was inert. Five call sites were changed to read ``objective.metric`` instead of
    a hardcoded "ndcg", and the deliverable is computed only when a direction is
    declared -- with no writer, all of it fell back to the old behaviour and the new
    code did nothing at all. Found 2026-08-07 while writing the handoff, which is too
    late: the readers and the writer are one change.

    ops_submit is where it belongs, because ``metric`` was already a parameter here
    and, after the wake message stopped naming it, an unused one -- declared,
    documented, and referenced nowhere. A required-but-unused parameter is a
    standing invitation to fill it in wrong.
    """
    root = _home(monkeypatch, tmp_path)
    ledger = root / "c1" / "ledger.json"

    tool = OpsSubmitTool(None)
    tool.set_context("tui", "default")
    await tool.execute(
        objective="minimise the residual",
        eta_seconds=600,
        host="h",
        port=2222,
        remote_dir="/r",
        ledger=str(ledger),
        campaign="c1",
        round=0,
        configs=[{"nuWater": "1e-3"}],
        metric="residual",
        goal="min",
    )

    meta = json.loads((root / "c1" / "meta.json").read_text(encoding="utf-8"))
    assert meta["objective"] == {"metric": "residual", "direction": "min"}


async def test_an_undeclared_direction_records_no_objective(tmp_path: Path, monkeypatch) -> None:
    """Direction cannot be inferred from the metric name: for a loss the best value
    is the smallest. Recording a guessed direction would make the backend hand over
    the worst checkpoint with exactly the confidence of the best one."""
    root = _home(monkeypatch, tmp_path)
    ledger = root / "c2" / "ledger.json"

    tool = OpsSubmitTool(None)
    tool.set_context("tui", "default")
    await tool.execute(
        objective="tune it",
        eta_seconds=600,
        host="h",
        port=2222,
        remote_dir="/r",
        ledger=str(ledger),
        campaign="c2",
        round=0,
        configs=[{"lr": 1e-5}],
        metric="ndcg",
    )

    meta = json.loads((root / "c2" / "meta.json").read_text(encoding="utf-8"))
    assert "objective" not in meta


async def test_a_hand_written_meta_gets_the_objective_written_into_it(tmp_path: Path, monkeypatch) -> None:
    """Every experiment here is set up by writing meta.json before the first submit,
    which took the branch that merges the stored file over this round's arguments
    and never wrote back. The objective assembled from metric= and goal= therefore
    lived in memory for one call: the campaign on disk still declared none, and all
    five readers of it went on falling back."""
    root = _home(monkeypatch, tmp_path)
    cdir = _campaign(root, "c1")
    (cdir / "meta.json").write_text(
        json.dumps({"host": "h", "port": 2222, "remote_dir": "/r", "budget_minutes_total": 140}),
        encoding="utf-8",
    )

    tool = OpsSubmitTool(None)
    tool.set_context("tui", "default")
    await tool.execute(
        objective="tune it",
        eta_seconds=600,
        host="h",
        port=2222,
        remote_dir="/r",
        ledger=str(cdir / "ledger.json"),
        campaign="c1",
        round=0,
        configs=[{"lr": 1e-5}],
        metric="f1",
        goal="max",
    )

    meta = json.loads((cdir / "meta.json").read_text(encoding="utf-8"))
    assert meta["objective"] == {"metric": "f1", "direction": "max"}
    # The hand-written fields are the campaign's fixed setup and outrank this
    # round's arguments; writing the merged dict back would quietly replace them.
    assert meta["budget_minutes_total"] == 140
