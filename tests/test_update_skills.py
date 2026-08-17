from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.config.update_skills import get_skillforge, set_skillforge


def _write(tmp: Path, cfg: dict) -> Path:
    p = tmp / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def test_get_returns_whitelist_camelcase(tmp_path: Path) -> None:
    p = _write(
        tmp_path, {"skillForge": {"enabled": True, "router": {"weights": {"local": 1.0, "everos": 0.9, "hub": 0.85}}}}
    )
    out = get_skillforge(config_path=p)
    assert out["enabled"] is True
    assert out["router"]["weights"] == {"local": 1.0, "everos": 0.9, "hub": 0.85}
    assert "hub" in out["router"] and "localDirs" in out


def test_set_patches_only_whitelist_and_preserves_other_keys(tmp_path: Path) -> None:
    p = _write(tmp_path, {"skillForge": {"enabled": True, "embeddingModel": "keepme", "router": {"topK": 7}}})
    set_skillforge({"enabled": False, "router": {"weights": {"hub": 0.5}}}, config_path=p)
    raw = json.loads(p.read_text(encoding="utf-8"))["skillForge"]
    assert raw["enabled"] is False
    assert raw["embeddingModel"] == "keepme"  # advanced key preserved
    assert raw["router"]["topK"] == 7  # advanced router key preserved
    assert raw["router"]["weights"]["hub"] == 0.5


def test_set_rejects_out_of_range_weight(tmp_path: Path) -> None:
    p = _write(tmp_path, {"skillForge": {}})
    with pytest.raises(ValueError):
        set_skillforge({"router": {"weights": {"hub": 5.0}}}, config_path=p)
    assert json.loads(p.read_text(encoding="utf-8")) == {"skillForge": {}}  # nothing written


def test_set_rejects_bad_hub_url_and_empty_localdir_path(tmp_path: Path) -> None:
    p = _write(tmp_path, {"skillForge": {}})
    with pytest.raises(ValueError):
        set_skillforge({"router": {"hub": {"endpoint": "not-a-url"}}}, config_path=p)
    with pytest.raises(ValueError):
        set_skillforge({"localDirs": [{"path": "  "}]}, config_path=p)


def test_set_localdirs_normalizes_entry(tmp_path: Path) -> None:
    p = _write(tmp_path, {"skillForge": {}})
    set_skillforge({"localDirs": [{"path": "~/skills"}]}, config_path=p)
    raw = json.loads(p.read_text(encoding="utf-8"))["skillForge"]["localDirs"]
    assert raw == [{"path": "~/skills", "enabled": True, "name": None, "alwaysEnabled": True}]


def test_get_hub_has_only_whitelisted_keys(tmp_path: Path) -> None:
    # get_skillforge must not leak un-whitelisted hub keys (e.g. HubSourceConfig.source).
    p = _write(tmp_path, {"skillForge": {"router": {"hub": {"endpoint": "https://x"}}}})
    out = get_skillforge(config_path=p)
    assert set(out["router"]["hub"]) == {"endpoint", "apiKey", "minSafety", "timeoutS"}
    assert "source" not in out["router"]["hub"]
