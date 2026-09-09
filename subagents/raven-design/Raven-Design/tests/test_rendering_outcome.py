"""Unit tests for normalized render outcomes and tool payloads."""

from __future__ import annotations

import json
from pathlib import Path

from raven.rendering.models import AdapterResult, Detection, RenderOutcome
from raven.rendering.pipeline import RenderPipeline
from raven.rendering.result import preview_tool_result, render_tool_result


def _outcome(
    tmp_path: Path,
    *,
    format: str = "docx",
    previews: list[dict] | None = None,
    candidate_count: int | None = None,
    warnings: list[dict] | None = None,
    hidden_sheets: int = 0,
) -> RenderOutcome:
    bundle = tmp_path / "bundle"
    (bundle / "preview").mkdir(parents=True)
    records = previews or []
    for index, record in enumerate(records):
        path = bundle / record["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes([index + 1]) * int(record.pop("_bytes", 8)))
    return RenderOutcome(
        bundle_dir=bundle,
        detection=Detection(
            format=format,
            family="office",
            mime="application/octet-stream",
            declared_extension=f".{format}",
            support_level="guaranteed",
            metadata={"hidden_sheet_count": hidden_sheets},
        ),
        page_records=[],
        preview_records=records,
        preview_candidate_count=candidate_count if candidate_count is not None else len(records),
        warnings=warnings or [],
    )


def _metadata(parts: list[dict]) -> dict:
    return json.loads(parts[0]["text"])


def test_render_result_only_contains_dir_and_content_warnings(tmp_path: Path) -> None:
    outcome = _outcome(
        tmp_path,
        warnings=[
            {"code": "prototype_office_backend"},
            {"code": "macros_disabled"},
            {"code": "macros_disabled"},
        ],
    )

    assert render_tool_result(outcome) == {
        "dir": str(tmp_path / "bundle"),
        "warnings": ["macros_disabled"],
    }


def test_preview_views_map_document_slide_sheet_and_motion(tmp_path: Path) -> None:
    cases = [
        ("docx", {"path": "preview/page.png", "mime": "image/png", "page": 3}, {"page": 3}),
        (
            "pptx",
            {
                "path": "preview/slide.png",
                "mime": "image/png",
                "page": 2,
                "slide": 3,
            },
            {"slide": 3},
        ),
        (
            "xlsx",
            {
                "path": "preview/sheet.png",
                "mime": "image/png",
                "sheet_name": "Data",
                "visible_range": "A1:Q62",
            },
            {"sheet": "Data", "range": "A1:Q62"},
        ),
        (
            "animated_gif",
            {"path": "preview/frame.png", "mime": "image/png", "at_ms": 1000},
            {"at_ms": 1000},
        ),
    ]
    for folder, (format, record, expected) in enumerate(cases):
        outcome = _outcome(tmp_path / str(folder), format=format, previews=[record])
        view = _metadata(preview_tool_result(outcome, 1024))["views"][0]
        assert view == {"image": 1, **expected}


def test_inline_byte_limit_is_applied_before_view_numbering(tmp_path: Path) -> None:
    outcome = _outcome(
        tmp_path,
        previews=[
            {"path": "preview/first.png", "mime": "image/png", "page": 1, "_bytes": 4},
            {"path": "preview/second.png", "mime": "image/png", "page": 2, "_bytes": 8},
        ],
        candidate_count=3,
    )

    parts = preview_tool_result(outcome, 6)

    assert len(parts) == 2
    assert _metadata(parts) == {
        "views": [{"image": 1, "page": 1}],
        "omitted": {"images": 2},
    }


def test_empty_optional_metadata_is_omitted_and_hidden_sheets_are_counted(
    tmp_path: Path,
) -> None:
    outcome = _outcome(
        tmp_path,
        format="xlsx",
        previews=[
            {
                "path": "preview/data.png",
                "mime": "image/png",
                "sheet_name": "Data",
                "used_range": "A1:C5",
            }
        ],
        hidden_sheets=1,
    )

    metadata = _metadata(preview_tool_result(outcome, 1024))

    assert metadata == {
        "views": [{"image": 1, "sheet": "Data", "range": "A1:C5"}],
        "omitted": {"hidden_sheets": 1},
    }


def test_presentation_pages_map_back_to_source_slide_numbers() -> None:
    detection = Detection(
        format="pptx",
        family="office",
        mime="application/octet-stream",
        declared_extension=".pptx",
        support_level="guaranteed",
        metadata={
            "slides": [
                {"index": 0, "hidden": True},
                {"index": 1, "hidden": False},
            ]
        },
    )
    records = [{"page": 1}]
    adapter_result = AdapterResult({}, {}, {}, {}, [], [])

    RenderPipeline._enrich_presentation_pages(
        detection,
        {"page_count": 1},
        records,
        adapter_result,
    )

    assert records == [{"page": 1, "slide": 2}]
    assert adapter_result.warnings == []


def test_validated_actions_accepts_each_step_kind() -> None:
    from raven.rendering.pipeline import _validated_actions

    actions = _validated_actions(
        (
            {"click": "#start"},
            {"hover": ".bar"},
            {"fill": {"selector": "#name", "value": "abc"}},
            {"wait_ms": 500},
        )
    )

    assert actions is not None
    assert [next(iter(item)) for item in actions] == ["click", "hover", "fill", "wait_ms"]


def test_validated_actions_rejects_bad_steps() -> None:
    import pytest

    from raven.rendering.models import RenderError
    from raven.rendering.pipeline import _validated_actions

    assert _validated_actions(None) is None
    for bad in (
        (),
        ({"click": "#a"},) * 6,
        ({"click": ""},),
        ({"click": "#a", "hover": "#b"},),
        ({"drag": "#a"},),
        ({"wait_ms": 10},),
        ({"wait_ms": True},),
        ({"fill": {"selector": "#a"}},),
    ):
        with pytest.raises(RenderError):
            _validated_actions(bad)


def test_action_preview_view_reports_status_and_change() -> None:
    from raven.rendering.result import preview_view

    detection = Detection(
        format="html",
        family="browser",
        mime="text/html",
        declared_extension="html",
        support_level="guaranteed",
        metadata={},
    )
    view = preview_view(
        {"action": "click #start", "status": "failed: TimeoutError", "changed_pixel_ratio": 0.0},
        2,
        detection,
    )

    assert view == {
        "image": 2,
        "action": "click #start",
        "status": "failed: TimeoutError",
        "changed_pixel_ratio": 0.0,
    }
