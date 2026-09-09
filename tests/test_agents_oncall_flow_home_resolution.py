"""Where campaign state lands, and why it has to be resolved per call.

The measured incident (2026-08-05). A run launched with
``raven tui --config ~/.raven-r5/config.json`` kept its ledger, its decisions and
its trial events under that config's directory, because every tool call passed an
explicit ledger path. ``ops_finish`` was the one call that omitted it, so the
report resolved against a module-level constant bound from ``Path.home()`` at
import time and was written to ``~/.raven/ops/<campaign>/`` instead -- a
directory created on the spot, holding no ``meta.json``.

That is worse than a misplaced file. ``_expected_baseline()`` reads the starting
value from ``meta.json``, so with no ``meta.json`` it returned ``{}``, the report
gate had nothing to compare the reported baseline against, and it accepted on the
first try. "The gate found nothing wrong" and "the gate had nothing to check"
came out as the same word: ``Accepted``.

Same shape for the wake accounting, which went to the old home through a second
constant in ``oncall_flow.instrument``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.tools import base as tools_base  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


def _use_config(monkeypatch, cfg: Path) -> None:
    """The fork re-resolved the home from ``cfg`` per call; the plugin's
    root is installed once, so pointing at an instance is one set_home."""
    tools_base.set_home(cfg.parent / "ops")


def test_campaign_dir_resolves_per_call_not_at_import(tmp_path: Path, monkeypatch) -> None:
    """A constant bound at import cannot follow ``--config``, which is known only
    at startup. Rebinding after the fact is what split one campaign in two."""
    from oncall_flow import instrument

    first = tmp_path / "one" / "config.json"
    second = tmp_path / "two" / "config.json"
    for cfg in (first, second):
        cfg.parent.mkdir(parents=True)

    _use_config(monkeypatch, first)
    assert instrument.campaign_dir("c", home=tools_base.ops_home()) == tmp_path / "one" / "ops" / "c"

    _use_config(monkeypatch, second)
    assert instrument.campaign_dir("c", home=tools_base.ops_home()) == tmp_path / "two" / "ops" / "c"


def test_campaign_dir_still_honours_an_explicit_home(tmp_path: Path, monkeypatch) -> None:
    from oncall_flow import instrument

    cfg = tmp_path / "instance" / "config.json"
    cfg.parent.mkdir(parents=True)
    _use_config(monkeypatch, cfg)

    assert instrument.campaign_dir("c", home=tmp_path / "elsewhere") == tmp_path / "elsewhere" / "c"
    del monkeypatch


def test_report_without_a_ledger_lands_beside_the_campaign_meta(tmp_path: Path, monkeypatch) -> None:
    """The end-to-end shape of the incident: omitting ``ledger`` must resolve to
    the same directory ``ops_submit`` anchored to, so the gate finds ``meta.json``
    and can refuse. Before the fix this resolved to the default home."""
    from oncall_flow.tools import ops as ops_tools

    cfg = tmp_path / "instance" / "config.json"
    cfg.parent.mkdir(parents=True)
    _use_config(monkeypatch, cfg)

    resolved = ops_tools._resolve_campaign_dir("armb embed r5e", None)

    assert resolved.parent == cfg.parent / "ops"
    assert resolved == ops_tools._resolve_campaign_dir(
        "armb embed r5e", str(cfg.parent / "ops" / resolved.name / "ledger.json")
    )
