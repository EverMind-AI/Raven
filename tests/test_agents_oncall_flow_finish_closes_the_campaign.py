"""ops_finish: filing the result and closing the campaign are one call.

They used to be two. ops_report filed the result and ops_cancel wrote the
concluded marker and cleared the pending wakes. Measured 2026-08-12 across two
arms of the same task: both arms reported, and their reports were complete and
correct -- but only one went on to make the second call. The other campaign
stayed open, and its wake re-arm went on waking a loop that had already finished
and said so, four times, each a full round of re-reading and re-reporting, until
the re-arm cap stopped it.

Nothing in that second call is a judgement. A step with no judgement in it should
not depend on being remembered.
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
from oncall_flow.tools.ops_escalation import OpsFinishTool  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


def _bound(tool, scheduler):
    tool.bind_runtime(SimpleNamespace(wake_scheduler=scheduler))
    return tool


class _Cron:
    def __init__(self, keys: list[str]) -> None:
        self.names = list(keys)

    def schedule_wake(self, key, at_ms, message, **route):
        self.names.append(key)
        return SimpleNamespace(id=f"oncall-flow:{key}")

    def pending_wakes(self, prefix=""):
        return [SimpleNamespace(id=f"oncall-flow:{k}") for k in self.names if k.startswith(prefix)]

    def advance_wake_to_now(self, key):
        return False

    def cancel_wake(self, key):
        if key in self.names:
            self.names.remove(key)
            return True
        return False


def _campaign(tmp_path: Path) -> Path:
    cdir = tmp_path / "c"
    cdir.mkdir()
    (cdir / "meta.json").write_text(json.dumps({"backend": "process"}), encoding="utf-8")
    return cdir


def _ledger(cdir: Path) -> str:
    return str(cdir / "ledger.json")


@pytest.mark.asyncio
async def test_one_call_files_the_report_and_closes_the_campaign(tmp_path):
    cdir = _campaign(tmp_path)
    cron = _Cron(["c", "other"])

    out = await _bound(OpsFinishTool(), cron).execute(
        campaign="c",
        subject="bm25 sweep",
        outcome="done",
        dedupe_key="k1",
        observed={"ndcg": 0.3606},
        condition_type="absolute",
        ledger=_ledger(cdir),
    )

    assert "Accepted" in out
    assert (cdir / "reports.jsonl").exists(), "the report half still happens"
    concluded = json.loads((cdir / "concluded.json").read_text())
    assert concluded["outcome"] == "done"
    assert cron.names == ["other"], "only this campaign's wakes stand down"
    assert "1 pending wake" in out, "the reply says what it stood down"


@pytest.mark.asyncio
async def test_a_refused_report_leaves_the_campaign_open(tmp_path):
    """The close rides on the report being accepted, not on the call being made.
    A campaign closed by a refused report would be closed with nothing filed."""
    cdir = _campaign(tmp_path)
    cron = _Cron(["c"])

    out = await _bound(OpsFinishTool(), cron).execute(
        campaign="c",
        subject="bm25 sweep",
        outcome="done",
        dedupe_key="k1",
        observed={"ndcg": 0.3606},
        condition_type="relative",
        ledger=_ledger(cdir),
    )

    assert "REFUSED" in out
    assert not (cdir / "concluded.json").exists()
    assert cron.names == ["c"], "the wake stays, so the loop gets another turn"


@pytest.mark.asyncio
async def test_a_failure_with_nothing_measured_must_say_why_there_is_none(tmp_path):
    """A failure often has no numbers -- the job died before it wrote one, or died
    because it could not. Requiring a reading there leaves the one honest report
    unsendable, and the loop goes silent instead."""
    cdir = _campaign(tmp_path)

    refused = await OpsFinishTool().execute(
        campaign="c",
        subject="training",
        outcome="failed",
        dedupe_key="k1",
        condition_type="absolute",
        ledger=_ledger(cdir),
    )
    assert "REFUSED" in refused and "no_data_reason" in refused
    assert not (cdir / "concluded.json").exists()

    accepted = await OpsFinishTool().execute(
        campaign="c",
        subject="training",
        outcome="failed",
        dedupe_key="k1",
        condition_type="absolute",
        ledger=_ledger(cdir),
        no_data_reason="every round OOMed at step 0; the 80GB card is shared and never had room",
    )
    assert "Accepted" in accepted
    row = json.loads((cdir / "reports.jsonl").read_text().splitlines()[0])
    assert "OOMed" in row["observed"]["no_data_reason"]
    assert json.loads((cdir / "concluded.json").read_text())["outcome"] == "failed"


@pytest.mark.asyncio
async def test_a_failure_that_did_measure_something_reports_it(tmp_path):
    cdir = _campaign(tmp_path)
    out = await OpsFinishTool().execute(
        campaign="c",
        subject="training",
        outcome="failed",
        dedupe_key="k1",
        observed={"ndcg": 0.3047},
        condition_type="absolute",
        ledger=_ledger(cdir),
    )
    assert "Accepted" in out
    row = json.loads((cdir / "reports.jsonl").read_text().splitlines()[0])
    assert row["observed"] == {"ndcg": 0.3047}


@pytest.mark.asyncio
async def test_closing_without_a_cron_service_still_marks_the_campaign(tmp_path):
    """The marker is what every later turn reads. Without it a wake that fires
    from some other scheduler has nothing telling it to stand down."""
    cdir = _campaign(tmp_path)
    out = await OpsFinishTool().execute(
        campaign="c",
        subject="s",
        outcome="stopped",
        dedupe_key="k1",
        observed={"ndcg": 0.36},
        condition_type="absolute",
        ledger=_ledger(cdir),
    )
    assert "Accepted" in out
    assert (cdir / "concluded.json").exists()
