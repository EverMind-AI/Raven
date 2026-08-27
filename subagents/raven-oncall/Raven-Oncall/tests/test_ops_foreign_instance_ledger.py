"""An explicit ledger path may point anywhere except INTO another raven instance.

Measured 2026-08-25: a woken subagent turn, handed the default instance's path
for a same-named campaign by a misconfigured shell's wake message, read the
wrong experiment's ledger and then concluded it. The explicit-ledger branch
stays open for everything that is nobody's instance -- scratch dirs, archive
copies -- and for this instance's own tree; it closes only on a path whose
ancestry says "a different raven lives here" (a config.json beside the ops/
tree the ledger sits in).
"""

import json
from pathlib import Path

import pytest

from raven.agent.tools import ops as ops_mod


def _mk_instance(root: Path, campaign: str) -> Path:
    (root / "ops" / campaign).mkdir(parents=True)
    (root / "config.json").write_text("{}", encoding="utf-8")
    led = root / "ops" / campaign / "ledger.json"
    led.write_text(json.dumps({"version": 1, "records": {}}), encoding="utf-8")
    return led


def test_foreign_instance_ledger_is_refused(tmp_path, monkeypatch):
    foreign = _mk_instance(tmp_path / "other-raven", "beam")
    with pytest.raises(ValueError, match="different raven instance"):
        ops_mod._resolve_campaign_dir(None, ledger=str(foreign))


def test_own_instance_ledger_is_allowed(tmp_path, monkeypatch):
    own_root = tmp_path / "me"
    led = _mk_instance(own_root, "beam")
    monkeypatch.setattr(ops_mod, "_ops_home", lambda: own_root / "ops")
    assert ops_mod._resolve_campaign_dir(None, ledger=str(led)) == led.parent


def test_scratch_ledger_stays_reachable(tmp_path):
    led = tmp_path / "anywhere" / "ledger.json"
    led.parent.mkdir(parents=True)
    led.write_text("{}", encoding="utf-8")
    assert ops_mod._resolve_campaign_dir(None, ledger=str(led)) == led.parent


def test_archive_copy_stays_reachable(tmp_path):
    # an archived campaign dir: ops/<c>/ledger.json shape but NO config.json root
    led = tmp_path / "archive" / "ops" / "beam" / "ledger.json"
    led.parent.mkdir(parents=True)
    led.write_text("{}", encoding="utf-8")
    assert ops_mod._resolve_campaign_dir(None, ledger=str(led)) == led.parent
