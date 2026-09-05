"""Asking what experiments there are.

Until now there was no way to ask. A campaign's name reached the loop in exactly
one place -- the error raised when an unnamed call could not tell which campaign
was meant -- so the only way to see what existed was to trip over the ambiguity
on purpose. For the shape this is actually used in (several windows, several
experiments, some finished) that is not a listing, it is an accident.

This lists and does not judge. Which campaign is "current" is a per-window fact
the resolver answers, and answering it here in a different way is how two sources
of one truth start disagreeing -- the mistake that produced the concluded-campaign
handling twice over.

Spend is measured on the host, so it is probed only for campaigns still live
(usually one or two) and never for finished ones, whose number is already in their
record. A probe that fails prints a dash: a listing has to stay cheap enough to
ask casually, and must never be the call that fails.
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
from oncall_flow.tools.ops import OpsCampaignsTool  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


def _home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "ops"
    home.mkdir(exist_ok=True)
    tools_base.set_home(home)
    return home


def _campaign(
    home: Path,
    name: str,
    *,
    host="14.103.100.27",
    case="/home/cfd/work/case_a",
    total=150,
    unit="core-minute",
    concluded=None,
    trials=0,
) -> Path:
    d = home / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(
        json.dumps(
            {
                "backend": "process",
                "host": host,
                "port": 22,
                "key": "~/.ssh/id_rsa",
                "command": "run {config} {job_dir}",
                "staged_case": case,
                "budget": {"unit": unit, "total": total, "overlap": "additive"},
            }
        ),
        encoding="utf-8",
    )
    if trials:
        (d / "ledger.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "records": {
                        f"t{i}": {
                            "idem_key": f"t{i}",
                            "status": "succeeded",
                            "campaign": name,
                            "handle": None,
                            "attempts": 1,
                            "escalated": False,
                            "metrics": {},
                        }
                        for i in range(trials)
                    },
                }
            ),
            encoding="utf-8",
        )
    if concluded:
        (d / "concluded.json").write_text(
            json.dumps({"concluded_at": "2026-08-14T01:00:00", "outcome": concluded, "reason": f"{name} done"}),
            encoding="utf-8",
        )
    return d


def _spend(monkeypatch, minutes=115.47):
    class _Backend:
        async def spent_minutes(self):
            return minutes

        async def remaining_minutes(self):
            return 150.0 - minutes

    monkeypatch.setattr("oncall_flow.backends.backend_from_meta", lambda meta: _Backend())


@pytest.mark.asyncio
async def test_it_lists_every_campaign_with_what_it_is(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    _campaign(home, "dambreak-legA", case="/home/cfd/work/case_legA", trials=2)
    _campaign(home, "m9-handover", case="", concluded="done", trials=3)
    _spend(monkeypatch)

    out = await OpsCampaignsTool().execute()
    assert "dambreak-legA" in out and "m9-handover" in out
    assert "/home/cfd/work/case_legA" in out, "so a task statement can be matched against it"
    assert "14.103.100.27" in out
    assert "150" in out


@pytest.mark.asyncio
async def test_a_finished_campaign_is_marked_and_carries_its_outcome(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    _campaign(home, "m9-handover", concluded="failed")
    _spend(monkeypatch)
    out = await OpsCampaignsTool().execute()
    assert "failed" in out
    assert "2026-08-14" in out


@pytest.mark.asyncio
async def test_spend_is_probed_for_live_campaigns_only(monkeypatch, tmp_path):
    """A finished campaign's number is already recorded; probing it costs a remote
    round trip for something nobody is deciding on."""
    home = _home(monkeypatch, tmp_path)
    _campaign(home, "live-one")
    _campaign(home, "done-one", concluded="done")

    probed: list[str] = []

    class _Backend:
        def __init__(self, host):
            self.host = host

        async def spent_minutes(self):
            probed.append(self.host)
            return 12.5

        async def remaining_minutes(self):
            return 137.5

    monkeypatch.setattr("oncall_flow.backends.backend_from_meta", lambda meta: _Backend(meta.get("host", "?")))
    await OpsCampaignsTool().execute()
    assert len(probed) == 1, f"probed {len(probed)} times"


@pytest.mark.asyncio
async def test_a_host_that_cannot_answer_still_lists(monkeypatch, tmp_path):
    """Never the call that fails -- it exists to be asked casually."""
    home = _home(monkeypatch, tmp_path)
    _campaign(home, "live-one")

    class _Dead:
        async def spent_minutes(self):
            raise OSError("connection refused")

        async def remaining_minutes(self):
            raise OSError("connection refused")

    monkeypatch.setattr("oncall_flow.backends.backend_from_meta", lambda meta: _Dead())
    out = await OpsCampaignsTool().execute()
    assert "live-one" in out


@pytest.mark.asyncio
async def test_it_does_not_say_which_one_is_current(monkeypatch, tmp_path):
    """That is the resolver's answer and it is per window. A second source for one
    truth is how the two disagreed twice already."""
    home = _home(monkeypatch, tmp_path)
    _campaign(home, "a")
    _campaign(home, "b")
    _spend(monkeypatch)
    out = await OpsCampaignsTool().execute()
    assert "current" not in out.lower()


@pytest.mark.asyncio
async def test_an_empty_ops_home_says_so_plainly(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    out = await OpsCampaignsTool().execute()
    assert "no campaign" in out.lower()


def test_the_listing_says_what_each_campaign_is_optimising(tmp_path, monkeypatch):
    """Two campaigns can share a machine, a case directory and a budget.

    Measured 2026-08-17 with six live at once: two FEA campaigns rendered as the
    same line but for their names, so a window could only tell them apart by
    guessing which name meant which task. What separated them -- one maximising a
    collapse load, one minimising a penetration -- was in the meta and not on the
    line.
    """
    import asyncio
    import json as _j

    import oncall_flow.tools.ops as ops

    home = tmp_path / "ops"
    for name, obj in (
        ("beam", {"metric": "collapse_load", "direction": "max"}),
        ("block", {"metric": "max_penetration", "direction": "min"}),
    ):
        d = home / name
        d.mkdir(parents=True)
        (d / "meta.json").write_text(
            _j.dumps(
                {
                    "backend": "process",
                    "staged_case": "/same/case",
                    "remote_dir": "/same",
                    "budget": {"unit": "minute", "total": 150},
                    "objective": obj,
                }
            ),
            encoding="utf-8",
        )
    tools_base.set_home(home)

    out = asyncio.run(ops.OpsCampaignsTool().execute())
    assert "max collapse_load" in out and "min max_penetration" in out


def test_the_ambiguity_prompt_says_what_to_do_when_none_of_them_fits(tmp_path, monkeypatch):
    """Naming a campaign binds the window to it, so taking the nearest row sends
    the work to another experiment's ledger and budget. The cheapest move must not
    be the wrong one."""
    import json as _j

    import oncall_flow.tools.ops as ops

    home = tmp_path / "ops"
    for name in ("one", "two"):
        d = home / name
        d.mkdir(parents=True)
        (d / "meta.json").write_text(_j.dumps({"backend": "process"}), encoding="utf-8")
    tools_base.set_home(home)

    try:
        ops._resolve_campaign_dir("", None)
    except ValueError as exc:
        text = str(exc)
    else:
        raise AssertionError("two live campaigns and no name should be ambiguous")

    assert "ops_ask_owner" in text
    assert "nearest" in text
