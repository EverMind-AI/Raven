"""A campaign's result has to outlive the message that announced it.

It lived in reports.jsonl and in one delivered line. Both are fine for reading
once; neither survives what happens next. The owner comes back a day later
wanting to ask about the run, and a wake turn is a cold start with no memory of
having written anything. A file is what both of them can open.

Nothing about the layout is decided by the tool. A tuning run reports one number,
a sweep of twenty blade angles reports a table, and a third thing nobody has
asked for yet reports something else -- so the prose passes through as written.
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
from oncall_flow.tools.ops_escalation import _write_report_md  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


def test_the_report_is_written_next_to_the_campaign(tmp_path):
    md = _write_report_md(tmp_path, "blade", "blade sweep", "done", {"points": 20}, None, "prose")
    assert md is not None and md.parent == tmp_path
    assert md.name.startswith("report-") and md.suffix == ".md"


def test_a_table_in_the_prose_survives_verbatim(tmp_path):
    """The sweep case: twenty rows are just what the narrative contains."""
    table = "| angle | cl |\n|---|---|\n| 0 | 0.40 |\n| 2 | 0.42 |"
    md = _write_report_md(tmp_path, "blade", "s", "done", {}, None, table)
    assert table in md.read_text()


def test_one_number_reads_as_one_number(tmp_path):
    md = _write_report_md(tmp_path, "tune", "s", "done", {"ndcg": 0.3477}, None, "")
    text = md.read_text()
    assert "ndcg: 0.3477" in text
    assert "Observed" in text


def test_the_outcome_and_the_campaign_are_both_on_it(tmp_path):
    """Read a day later, out of context, it has to say what it is."""
    md = _write_report_md(tmp_path, "legB2", "dam break", "failed", {}, None, "did not run to endTime")
    text = md.read_text()
    assert "legB2" in text and "failed" in text and "dam break" in text


def test_a_directory_that_cannot_be_written_does_not_lose_the_ending(tmp_path):
    """The report is already in the ledger and already delivered; the copy is a
    convenience and must not be able to fail the close."""
    missing = tmp_path / "gone"
    assert _write_report_md(missing, "c", "s", "done", {}, None, "x") is None


async def test_the_listing_says_where_a_finished_campaign_s_report_is(tmp_path, monkeypatch):
    import oncall_flow.tools.ops as ops

    home = tmp_path / "ops"
    d = home / "blade"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"backend": "process"}), encoding="utf-8")
    (d / "concluded.json").write_text(
        json.dumps({"concluded_at": "2026-08-18T21:41:00", "outcome": "done", "reason": "s"}), encoding="utf-8"
    )
    _write_report_md(d, "blade", "s", "done", {}, None, "x")
    tools_base.set_home(home)

    out = await ops.OpsCampaignsTool().execute()
    assert "report " in out and "report-" in out
