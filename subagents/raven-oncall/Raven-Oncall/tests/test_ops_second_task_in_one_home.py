"""A second task in the same instance, which is how anyone actually uses this.

Asked on 2026-08-13: "will a user just run `raven tui` once and then do task A
and task B in a row?" The answer was no, in two places, and neither had been
noticed for a month because every experiment passes ``--config`` -- so each
instance held exactly one campaign and neither path was ever walked. The
apparatus was shaped unlike the product, so it could not measure the product's
faults.

**Locating with no name.** ``_resolve_campaign_dir`` counted every directory
under the ops home, concluded or not, and refused as ambiguous from the second
task onward. The cron service, deciding the same "which campaign" question,
already skips concluded ones; the two disagreed and this was the side that was
wrong. When every campaign is finished the answer is not a list of names either:
the previous piece of work is over, and this is new work.

**Naming a finished campaign.** Task statements no longer mention a campaign
name, so the agent invents one, and two similar tasks colliding is not unlikely.
The collision landed on the wrong message: "CONCLUDED by the user; not
submitting" reads as a state or permission problem rather than "you picked a name
that is taken". An agent that reads it that way reaches for ``concluded.json``
with ``edit_file``, or edits meta.json -- the exact apparatus-editing behaviour
measured on 2026-08-12, with the trigger once again on our side.

Refusing is kept (a) because a finished campaign's record is what a report was
filed against, and (b) because nothing here can tell "continue that work" from
"different work, same word". What changes is that the refusal names the real
cause and says which files not to touch.

Reading a concluded campaign stays allowed: that is how a report gets checked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.tools.ops import OpsSubmitTool, _resolve_campaign_dir


class _Job:
    def __init__(self, i): self.id = str(i)


class _FakeCron:
    def __init__(self): self.jobs = []
    def add_job(self, **kw): self.jobs.append(kw); return _Job(len(self.jobs))
    def list_jobs(self):
        from types import SimpleNamespace
        return [SimpleNamespace(id=str(i), name=j["name"]) for i, j in enumerate(self.jobs)]
    def remove_job(self, job_id): del self.jobs[int(job_id)]; return True


def _home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "ops"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr("raven.agent.tools.ops._ops_home", lambda: home)
    return home


def _campaign(home: Path, name: str, *, concluded: bool = False) -> Path:
    d = home / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({
        "backend": "process", "host": "h", "port": 22, "key": "~/.ssh/id_rsa",
        "command": "run {config} {job_dir}",
        "budget": {"unit": "gpu-minute", "total": 60, "overlap": "additive"},
    }), encoding="utf-8")
    if concluded:
        (d / "concluded.json").write_text(
            json.dumps({"concluded_at": "2026-08-13T17:25:26", "outcome": "done",
                        "reason": "task A"}), encoding="utf-8")
    return d


# ---------------------------------------------------------------- locating

def test_the_finished_task_does_not_make_the_live_one_ambiguous(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    _campaign(home, "task-a", concluded=True)
    live = _campaign(home, "task-b")
    assert _resolve_campaign_dir("", None) == live


def test_two_live_campaigns_are_still_ambiguous(monkeypatch, tmp_path):
    """Silently picking one would drive somebody else's experiment."""
    home = _home(monkeypatch, tmp_path)
    _campaign(home, "task-a")
    _campaign(home, "task-b")
    with pytest.raises(ValueError) as exc:
        _resolve_campaign_dir("", None)
    assert "task-a" in str(exc.value) and "task-b" in str(exc.value)


def test_all_finished_says_the_work_is_over_not_which_one(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    _campaign(home, "task-a", concluded=True)
    _campaign(home, "task-b", concluded=True)
    with pytest.raises(ValueError) as exc:
        _resolve_campaign_dir("", None)
    msg = str(exc.value)
    assert "say which one" not in msg, "listing finished names points at the wrong action"
    assert "finished" in msg or "concluded" in msg
    assert "new" in msg, "the right action is to start one, and it has to be said"


def test_a_named_campaign_still_resolves_even_when_concluded(monkeypatch, tmp_path):
    """Reading a finished campaign is how its report gets checked."""
    home = _home(monkeypatch, tmp_path)
    d = _campaign(home, "task-a", concluded=True)
    assert _resolve_campaign_dir("task-a", None) == d


def test_an_explicit_ledger_path_always_wins(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    _campaign(home, "task-a", concluded=True)
    d = _campaign(home, "task-b")
    assert _resolve_campaign_dir("", str(d / "ledger.json")) == d


# ---------------------------------------------------------------- naming

@pytest.mark.asyncio
async def test_reusing_a_finished_name_says_it_is_the_name(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    _campaign(home, "dambreak", concluded=True)

    sub = OpsSubmitTool(cron_service=_FakeCron())
    sub.set_context("cli", "direct")
    out = await sub.execute(host="h", configs=[{"a": 1}], objective="o",
                            eta_seconds=60, round=0, campaign="dambreak")

    assert "dambreak" in out
    assert "name" in out.lower(), "the cause is the name, and it has to be named"
    assert "concluded.json" in out and "meta.json" in out, (
        "an agent told only 'refused' reaches for the files that look like the block"
    )
    assert "2026-08-13T17:25:26" in out, "when it finished says whose work this record is"


@pytest.mark.asyncio
async def test_it_does_not_read_as_a_permission_problem(monkeypatch, tmp_path):
    """The old wording blamed the user's conclusion, which invited editing it."""
    home = _home(monkeypatch, tmp_path)
    _campaign(home, "dambreak", concluded=True)
    sub = OpsSubmitTool(cron_service=_FakeCron())
    sub.set_context("cli", "direct")
    out = await sub.execute(host="h", configs=[{"a": 1}], objective="o",
                            eta_seconds=60, round=0, campaign="dambreak")
    assert "different name" in out.lower() or "another name" in out.lower(), (
        "the way forward is a new name, not a permission appeal"
    )
