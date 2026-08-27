"""Entry point for an isolated render worker process."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from raven.rendering.models import RenderConfig, RenderError, RenderRequest
from raven.rendering.paths import RenderPathPolicy

_REQUEST_KEYS = {
    "version",
    "source",
    "output_dir",
    "asset_root",
    "preview_limit",
    "options",
    "config",
}
_OPTION_KEYS = {
    "motion_mode",
    "capture_duration_seconds",
    "capture_at_seconds",
    "page_range",
    "viewport_width",
    "viewport_height",
    "actions",
}


def run(request_path: Path) -> dict[str, Any]:
    payload = _read_request(request_path)
    config_values = dict(payload["config"])
    for name in (
        "chrome_path",
        "libreoffice_path",
        "ffmpeg_path",
        "ffprobe_path",
        "qpdf_path",
    ):
        config_values.pop(name, None)
    config = RenderConfig.discover(**config_values)
    options = payload["options"]
    workspace = Path("/workspace")
    policy = RenderPathPolicy(
        workspace=workspace,
        media_root=workspace / "input",
        runtime_root=workspace,
        restrict_to_workspace=True,
    )
    from raven.rendering.direct import DirectRenderEngine

    outcome = DirectRenderEngine(config, policy).run(
        RenderRequest(
            path=Path(payload["source"]),
            output_dir=Path(payload["output_dir"]),
            motion_mode=options["motion_mode"],
            capture_duration_seconds=options["capture_duration_seconds"],
            capture_at_seconds=options["capture_at_seconds"],
            page_range=options["page_range"],
            viewport_width=options["viewport_width"],
            viewport_height=options["viewport_height"],
            asset_root=Path(payload["asset_root"]) if payload["asset_root"] else None,
            actions=tuple(options["actions"]) if options.get("actions") else None,
        ),
        preview_limit=payload["preview_limit"],
    )
    return {
        "version": 1,
        "status": "ok",
        "bundle": outcome.bundle_dir.name,
        "detection": asdict(outcome.detection),
        "page_records": outcome.page_records,
        "preview_records": outcome.preview_records,
        "preview_candidate_count": outcome.preview_candidate_count,
        "warnings": outcome.warnings,
    }


def _read_request(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderError("invalid_parameters", "The worker request is invalid.") from exc
    if not isinstance(payload, dict) or set(payload) != _REQUEST_KEYS or payload.get("version") != 1:
        raise RenderError("invalid_parameters", "The worker request schema is invalid.")
    options = payload.get("options")
    config = payload.get("config")
    if (
        not isinstance(options, dict)
        or set(options) != _OPTION_KEYS
        or not isinstance(config, dict)
        or set(config) != {field.name for field in fields(RenderConfig)}
    ):
        raise RenderError("invalid_parameters", "The worker request options are invalid.")
    for key in ("source", "output_dir"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.startswith("/workspace/"):
            raise RenderError("path_not_allowed", f"{key} must be inside the worker mount.")
    asset_root = payload.get("asset_root")
    if asset_root is not None and (not isinstance(asset_root, str) or not asset_root.startswith("/workspace/")):
        raise RenderError("path_not_allowed", "asset_root must be inside the worker mount.")
    preview_limit = payload.get("preview_limit")
    if not isinstance(preview_limit, int) or isinstance(preview_limit, bool):
        raise RenderError("invalid_parameters", "preview_limit must be an integer.")
    return payload


def main() -> int:
    response_path = Path("/workspace/response.json")
    try:
        if len(sys.argv) != 2:
            raise RenderError("invalid_parameters", "Expected one request path.")
        response = run(Path(sys.argv[1]))
    except RenderError as exc:
        response = {
            "version": 1,
            "status": "error",
            "error": exc.as_dict(),
        }
    except Exception:
        response = {
            "version": 1,
            "status": "error",
            "error": {
                "code": "conversion_failed",
                "message": "The renderer worker failed unexpectedly.",
                "retryable": False,
                "details": {},
            },
        }
    response_path.write_text(
        json.dumps(response, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
