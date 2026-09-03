"""What ppt_ingest hands back, which is what everything downstream stands on."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven_ppt.contracts import Project
from raven_ppt.tools.ingest import PptIngestTool

pytest.importorskip("fitz")


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "materials").mkdir()
    return tmp_path


def _body(reply: str) -> dict:
    return json.loads(reply)


@pytest.mark.asyncio
async def test_a_text_source_is_read_and_recorded(workspace: Path) -> None:
    (workspace / "materials" / "notes.md").write_text(
        "# TarViS\n\nThe M4 chip delivers 30,972 million units and 14% growth.\n", encoding="utf-8"
    )
    body = _body(await PptIngestTool(workspace).execute(project="tarvis", materials_dir="materials"))

    assert body["ok"] is True
    # A path, not a name: every path in the reply reads back with `read_file`, which
    # cost a live run two calls guessing where "notes.md" was.
    assert body["sources"] == ["deck/sources/notes.md"]
    assert (workspace / body["sources"][0]).is_file()
    assert (workspace / body["materials"]).is_file()
    assert body["characters"] > 0
    project = Project(workspace=workspace, slug="tarvis")
    assert (project.ingest_dir / "materials.md").is_file()
    assert (project.ingest_dir / "read.json").is_file()


@pytest.mark.asyncio
async def test_the_reply_says_what_to_read_next(workspace: Path) -> None:
    (workspace / "materials" / "notes.md").write_text("Some text.\n", encoding="utf-8")
    body = _body(await PptIngestTool(workspace).execute(project="tarvis", materials_dir="materials"))
    assert "materials.md" in body["next_step"]


@pytest.mark.asyncio
async def test_a_deck_with_no_visual_evidence_is_told_so(workspace: Path) -> None:
    """Otherwise a page waits for a figure that was never extracted."""
    (workspace / "materials" / "notes.md").write_text("Prose only.\n", encoding="utf-8")
    body = _body(await PptIngestTool(workspace).execute(project="tarvis", materials_dir="materials"))
    assert body["figure_count"] == 0
    assert "drawn rather than placed" in body["next_step"]


@pytest.mark.asyncio
async def test_an_empty_materials_directory_names_what_is_supported(workspace: Path) -> None:
    body = _body(await PptIngestTool(workspace).execute(project="tarvis", materials_dir="materials"))
    assert body["ok"] is False
    assert "holds no sources to read" in body["error"]


@pytest.mark.asyncio
async def test_a_directory_outside_the_workspace_is_refused(workspace: Path) -> None:
    body = _body(await PptIngestTool(workspace).execute(project="tarvis", materials_dir="../elsewhere"))
    assert body["ok"] is False and "outside the workspace" in body["error"]


@pytest.mark.asyncio
async def test_a_missing_directory_says_where_it_looked(workspace: Path) -> None:
    body = _body(await PptIngestTool(workspace).execute(project="tarvis", materials_dir="nope"))
    assert body["ok"] is False and "no directory at nope" in body["error"]


@pytest.mark.asyncio
async def test_a_bad_project_name_is_refused_before_anything_is_read(workspace: Path) -> None:
    body = _body(await PptIngestTool(workspace).execute(project="../etc", materials_dir="materials"))
    assert body["ok"] is False and "usable project name" in body["error"]
