"""Atomic write path for the ``skillForge`` config block (P4 skills page).

Whitelist-patch only: reads the raw block, patches the exposed keys, validates
the merged block against ``SkillForgeConfig``, and writes the RAW patched dict
back (never ``model_dump`` — that would reset advanced keys to defaults). Every
non-whitelisted key is preserved verbatim.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

from raven.config.loader import get_config_path, load_config, read_raw_or_raise
from raven.config.raven import SkillForgeConfig

_WEIGHTS = ("local", "everos", "hub")


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _raw_config(path: Path) -> dict[str, Any]:
    return read_raw_or_raise(path) if path.exists() else {}


def _raw_block(cfg: dict) -> dict:
    return dict(cfg.get("skillForge") or cfg.get("skill_forge") or {})


def get_skillforge(*, config_path: Path | None = None) -> dict:
    path = config_path or get_config_path()
    sf = _raw_block(_raw_config(path))
    d = SkillForgeConfig.model_validate(sf).model_dump(by_alias=True)  # raises if malformed
    router = d.get("router") or {}
    hub = router.get("hub") or {}
    return {
        "enabled": d.get("enabled", True),
        "router": {
            "weights": router.get("weights") or {},
            "hub": {
                "endpoint": hub.get("endpoint"),
                "apiKey": hub.get("apiKey"),
                "minSafety": hub.get("minSafety"),
                "timeoutS": hub.get("timeoutS"),
            },
        },
        "everos": {"enabled": (d.get("everos") or {}).get("enabled", True)},
        "localDirs": d.get("localDirs") or [],
    }


def set_skillforge(fields: dict, *, config_path: Path | None = None) -> None:
    path = config_path or get_config_path()
    data = _raw_config(path)
    sf = _raw_block(data)

    if "enabled" in fields:
        sf["enabled"] = bool(fields["enabled"])

    rin = fields.get("router") or {}
    if rin:
        router = dict(sf.get("router") or {})
        if "weights" in rin:
            w = dict(router.get("weights") or {})
            for k in _WEIGHTS:
                if k in (rin["weights"] or {}):
                    v = float(rin["weights"][k])
                    if not 0.0 <= v <= 1.0:
                        raise ValueError(f"weight {k}={v} out of range [0, 1]")
                    w[k] = v
            router["weights"] = w
        if "hub" in rin:
            h = dict(router.get("hub") or {})
            hin = rin["hub"] or {}
            if "endpoint" in hin:
                ep = hin["endpoint"]
                if ep and not str(ep).startswith(("http://", "https://")):
                    raise ValueError(f"hub endpoint must be an http(s) URL: {ep!r}")
                h["endpoint"] = ep
            for k in ("apiKey", "minSafety", "timeoutS"):
                if k in hin:
                    h[k] = hin[k]
            router["hub"] = h
        sf["router"] = router

    if "everos" in fields and "enabled" in (fields["everos"] or {}):
        ev = dict(sf.get("everos") or {})
        ev["enabled"] = bool(fields["everos"]["enabled"])
        sf["everos"] = ev

    if "localDirs" in fields:
        out = []
        for d in fields["localDirs"] or []:
            p = str((d or {}).get("path") or "").strip()
            if not p:
                raise ValueError("localDirs entry needs a non-empty path")
            out.append(
                {
                    "path": p,
                    "enabled": bool(d.get("enabled", True)),
                    "name": d.get("name"),
                    "alwaysEnabled": bool(d.get("alwaysEnabled", True)),
                }
            )
        sf["localDirs"] = out

    SkillForgeConfig.model_validate(sf)  # raises ValidationError if illegal; nothing written yet

    data.pop("skill_forge", None)
    data["skillForge"] = sf
    _write_atomic(path, data)
    logger.info("update_skills: wrote skillForge block")


def list_skills() -> list[dict]:
    from raven.memory_engine.skill_forge import LocalSkillCatalog

    config = load_config()
    svc = LocalSkillCatalog(config.workspace_path, config=getattr(config, "skill_forge", None), start_watcher=False)
    return [
        {
            "id": m.id,
            "name": m.name,
            "source": m.source,
            "description": m.description or "",
            "path": str(m.path),
        }
        for m in svc.gather_all_skills()
    ]


__all__ = ["get_skillforge", "set_skillforge", "list_skills"]
