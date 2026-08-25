from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.tools.base import ToolResult
from raven.ppt.contracts import Project
from raven.ppt.services.ingest import CATALOGUE_FILE
from raven.ppt.tools.inspect import PptFigureInspectTool


class _Views:
    def data_uri(self, path: Path) -> str:
        return "data:image/png;base64,AAAA"


class _Composer:
    def __init__(self) -> None:
        self.calls = 0

    async def ask(self, system, parts, *, max_tokens):
        self.calls += 1
        return json.dumps({"visual_caption": "A three-stage memory lifecycle architecture."})


def _catalogue(project: Project, *, caption: str | None = None) -> None:
    project.ingest_dir.mkdir(parents=True, exist_ok=True)
    project.figures_dir.mkdir(parents=True, exist_ok=True)
    (project.figures_dir / "arch.png").write_bytes(b"PNG")
    (project.ingest_dir / CATALOGUE_FILE).write_text(
        json.dumps(
            {
                "schema": "raven.ppt.assets.v1",
                "assets": {
                    "arch-1": {
                        "file": "arch.png",
                        "kind": "image",
                        "width_px": 1200,
                        "height_px": 800,
                        "caption": caption,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_inspection_adds_a_visual_caption_when_the_source_has_none(tmp_path: Path) -> None:
    project = Project(workspace=tmp_path, slug="deck")
    _catalogue(project)
    composer = _Composer()

    result = await PptFigureInspectTool(tmp_path, _Views(), composer=composer).execute(
        project="deck", figures=["arch-1"]
    )

    assert isinstance(result, ToolResult)
    saved = json.loads((project.ingest_dir / CATALOGUE_FILE).read_text(encoding="utf-8"))
    assert saved["assets"]["arch-1"]["caption"] is None
    assert saved["assets"]["arch-1"]["visual_caption"] == "A three-stage memory lifecycle architecture."
    assert "visual_caption" in result.model_text


@pytest.mark.asyncio
async def test_source_caption_is_never_replaced_by_visual_inspection(tmp_path: Path) -> None:
    project = Project(workspace=tmp_path, slug="deck")
    _catalogue(project, caption="Figure 3. Source-authored caption.")
    composer = _Composer()

    await PptFigureInspectTool(tmp_path, _Views(), composer=composer).execute(
        project="deck", figures=["arch-1"]
    )

    saved = json.loads((project.ingest_dir / CATALOGUE_FILE).read_text(encoding="utf-8"))
    assert saved["assets"]["arch-1"]["caption"] == "Figure 3. Source-authored caption."
    assert "visual_caption" not in saved["assets"]["arch-1"]
    assert composer.calls == 0
