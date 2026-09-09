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
constant in ``raven.ops.instrument``.
"""

from __future__ import annotations

import json
from pathlib import Path

from raven.config import paths as config_paths
from raven.config.paths import get_ops_home as _real_get_ops_home


def _use_config(monkeypatch, cfg: Path) -> None:
    """Point resolution at ``cfg`` and restore the real resolver.

    ``tests/conftest.py`` patches ``get_ops_home`` itself to keep the suite out of
    the real home, which is the function under test here; ``_real_get_ops_home``
    is bound at import, before that fixture runs, so it is the genuine one."""
    monkeypatch.setattr(config_paths, "get_ops_home", _real_get_ops_home)
    monkeypatch.setattr(config_paths, "get_config_path", lambda: cfg)


def test_ops_home_follows_the_config_directory(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "instance" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({}), encoding="utf-8")
    _use_config(monkeypatch, cfg)

    assert config_paths.get_ops_home() == cfg.parent / "ops"


def test_ops_home_is_unchanged_when_no_config_is_bound(tmp_path: Path, monkeypatch) -> None:
    """The default has to stay ``<home>/.raven/ops`` exactly, or an existing
    single-instance install would find its campaigns moved after an upgrade."""
    default_cfg = tmp_path / ".raven" / "config.json"
    default_cfg.parent.mkdir(parents=True)
    _use_config(monkeypatch, default_cfg)

    assert config_paths.get_ops_home() == tmp_path / ".raven" / "ops"


def test_campaign_dir_resolves_per_call_not_at_import(tmp_path: Path, monkeypatch) -> None:
    """A constant bound at import cannot follow ``--config``, which is known only
    at startup. Rebinding after the fact is what split one campaign in two."""
    from raven.ops import instrument

    first = tmp_path / "one" / "config.json"
    second = tmp_path / "two" / "config.json"
    for cfg in (first, second):
        cfg.parent.mkdir(parents=True)

    _use_config(monkeypatch, first)
    assert instrument.campaign_dir("c") == tmp_path / "one" / "ops" / "c"

    _use_config(monkeypatch, second)
    assert instrument.campaign_dir("c") == tmp_path / "two" / "ops" / "c"


def test_campaign_dir_still_honours_an_explicit_home(tmp_path: Path, monkeypatch) -> None:
    from raven.ops import instrument

    cfg = tmp_path / "instance" / "config.json"
    cfg.parent.mkdir(parents=True)
    _use_config(monkeypatch, cfg)

    assert instrument.campaign_dir("c", home=tmp_path / "elsewhere") == tmp_path / "elsewhere" / "c"


def test_report_without_a_ledger_lands_beside_the_campaign_meta(tmp_path: Path, monkeypatch) -> None:
    """The end-to-end shape of the incident: omitting ``ledger`` must resolve to
    the same directory ``ops_submit`` anchored to, so the gate finds ``meta.json``
    and can refuse. Before the fix this resolved to the default home."""
    from raven.agent.tools import ops as ops_tools

    cfg = tmp_path / "instance" / "config.json"
    cfg.parent.mkdir(parents=True)
    _use_config(monkeypatch, cfg)

    resolved = ops_tools._resolve_campaign_dir("armb embed r5e", None)

    assert resolved.parent == cfg.parent / "ops"
    assert resolved == ops_tools._resolve_campaign_dir(
        "armb embed r5e", str(cfg.parent / "ops" / resolved.name / "ledger.json")
    )
