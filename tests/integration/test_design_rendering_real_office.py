"""Integration coverage for real Office document rendering."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("PIL")
pytest.importorskip("fitz")

from raven_design.rendering.models import RenderConfig, RenderRequest
from raven_design.rendering.paths import RenderPathPolicy
from raven_design.rendering.service import RenderService

_CORPUS_ENV = "RAVEN_RENDER_REGRESSION_DIR"


def _corpus_file(extension: str) -> Path:
    root_value = os.environ.get(_CORPUS_ENV)
    if not root_value:
        pytest.skip(f"{_CORPUS_ENV} is not configured")
    root = Path(root_value).expanduser().resolve()
    candidates = sorted(root.rglob(f"*{extension}"))
    if not candidates:
        pytest.skip(f"No {extension} sample exists under {_CORPUS_ENV}")
    return candidates[0]


def _service(source: Path, tmp_path: Path) -> RenderService:
    config = RenderConfig.discover(timeout_seconds=180)
    if not config.libreoffice_path:
        pytest.skip("LibreOffice is unavailable")
    return RenderService(
        config,
        path_policy=RenderPathPolicy(
            workspace=source.parent,
            media_root=source.parent,
            runtime_root=tmp_path / "runtime",
            restrict_to_workspace=False,
        ),
        backend="direct",
        max_concurrency=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", [".docx", ".pptx", ".xlsx"])
async def test_real_office_bundle_and_preview_contract(
    tmp_path: Path,
    extension: str,
) -> None:
    source = _corpus_file(extension)
    service = _service(source, tmp_path)

    render = await service.render(
        RenderRequest(
            path=source,
            output_dir=tmp_path / "render",
            capture_duration_seconds=0.25,
        ),
        preview_limit=6,
    )
    preview = await service.preview(
        path=source,
        output_dir=tmp_path / "preview",
        max_previews=6,
        capture_duration_seconds=0.25,
    )

    bundle = Path(render["dir"])
    assert {item.name for item in bundle.iterdir()} == {
        "document.pdf",
        "pages",
        "preview",
    }
    assert (bundle / "document.pdf").stat().st_size > 0
    assert list((bundle / "pages").glob("*.png"))
    metadata = json.loads(preview[0]["text"])
    assert len(metadata["views"]) == len(preview) - 1
    if extension == ".xlsx":
        assert all("sheet" in view and "range" in view for view in metadata["views"])
    if extension == ".pptx":
        assert all("slide" in view for view in metadata["views"])
