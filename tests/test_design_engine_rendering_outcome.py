"""Unit tests for normalized render outcomes and tool payloads."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from raven_design.rendering.models import AdapterResult, Detection, RenderOutcome
from raven_design.rendering.pipeline import RenderPipeline
from raven_design.rendering.result import preview_tool_result, render_tool_result


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
    from raven_design.rendering.pipeline import _validated_actions

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

    from raven_design.rendering.models import RenderError
    from raven_design.rendering.pipeline import _validated_actions

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
    from raven_design.rendering.result import preview_view

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


def test_a_cropped_image_becomes_a_content_warning() -> None:
    from raven_design.rendering.browser import _append_image_crop_warning

    warnings: list[dict] = []
    _append_image_crop_warning(warnings, {"image_crops": []})
    assert warnings == []
    _append_image_crop_warning(
        warnings,
        {
            "image_crops": [
                {
                    "src": "demo.png",
                    "natural": [960, 720],
                    "box": [480, 720],
                    "object_fit": "cover",
                    "cropped_fraction": 0.5,
                }
            ]
        },
    )
    assert warnings[0]["code"] == "image_cropped"
    assert warnings[0]["details"] == ["demo.png: 960x720 shown in a 480x720 box with object-fit cover, 50% cropped"]


@pytest.fixture
def _browsers_from_the_real_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite redirects HOME to a temp dir; playwright keeps its browsers
    under the real one. Point discovery there unless the caller already did."""
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        import pwd

        real_home = pwd.getpwuid(os.getuid()).pw_dir
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", os.path.join(real_home, "Library", "Caches", "ms-playwright"))


@pytest.mark.usefixtures("_browsers_from_the_real_home")
def test_the_browser_reports_images_that_object_fit_crops() -> None:
    """A height attribute overriding a CSS aspect-ratio turned a 4:3 frame into a
    2:3 box and object-fit: cover cut half the frame away; the runtime metadata
    must expose that, and leave a fully shown image alone."""
    playwright = pytest.importorskip("playwright.sync_api")
    from raven_design.rendering.browser_runtime import runtime_metadata
    from raven_design.rendering.models import _discover_chromium

    chrome = _discover_chromium()
    if not chrome:
        pytest.skip("no system chromium")

    pixel = (
        "data:image/svg+xml;utf8,"
        "<svg xmlns='http://www.w3.org/2000/svg' width='960' height='720'><rect width='960' height='720' fill='red'/></svg>"
    )
    html = (
        "<body style='margin:0'>"
        "<img id='bg' src=\"{p}\" style='position:fixed;inset:0;width:100vw;height:100vh;object-fit:cover'>"
        "<img id='cut' src=\"{p}\" width='960' height='720' style='position:relative;width:480px;height:720px;object-fit:cover'>"
        "<img id='whole' src=\"{p}\" style='position:relative;width:480px;height:360px;object-fit:cover'>"
        "</body>"
    ).format(p=pixel)
    with playwright.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=chrome, args=["--no-sandbox"])
        except Exception as exc:  # pragma: no cover - environment without a browser
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page()
        page.set_content(html)
        page.wait_for_function("() => [...document.images].every(i => i.complete && i.naturalWidth > 0)")
        crops = runtime_metadata(page)["image_crops"]
        browser.close()
    # the full-viewport background is cropped by design and stays out of the list
    assert [c["box"] for c in crops] == [[480, 720]]
    assert crops[0]["cropped_fraction"] == 0.5


def test_the_opening_visual_facts_become_one_detail_line() -> None:
    from raven_design.rendering.browser import _append_opening_visual_warning

    warnings: list[dict] = []
    _append_opening_visual_warning(warnings, {"opening_visual": None})
    assert warnings == []
    _append_opening_visual_warning(
        warnings,
        {
            "opening_visual": {
                "src": "hero.png",
                "viewport_fraction": 48,
                "headline_overlap": 0,
                "region_under_headline": None,
            }
        },
    )
    assert warnings[0]["code"] == "opening_visual"
    assert warnings[0]["details"] == ["opening visual: hero.png — 48% of viewport; headline overlap 0%"]


@pytest.mark.usefixtures("_browsers_from_the_real_home")
def test_the_browser_measures_where_the_headline_sits_on_the_opening_visual() -> None:
    """Text over a full-bleed image reads as overlap 100 with a uniform backdrop
    for a flat fill; text beside an image reads as overlap 0."""
    playwright = pytest.importorskip("playwright.sync_api")
    from raven_design.rendering.browser_runtime import runtime_metadata
    from raven_design.rendering.models import _discover_chromium

    chrome = _discover_chromium()
    if not chrome:
        pytest.skip("no system chromium")
    fill = (
        "data:image/svg+xml;utf8,"
        "<svg xmlns='http://www.w3.org/2000/svg' width='800' height='600'><rect width='800' height='600' fill='red'/></svg>"
    )
    over = (
        "<body style='margin:0'><img src=\"{p}\" style='position:fixed;inset:0;width:100vw;height:100vh;object-fit:cover'>"
        "<h1 style='position:fixed;left:10vw;top:20vh;margin:0;font-size:48px'>Hello</h1></body>"
    ).format(p=fill)
    beside = (
        "<body style='margin:0;display:grid;grid-template-columns:1fr 1fr;height:100vh'>"
        "<h1 style='margin:0;font-size:48px'>Hello</h1><img src=\"{p}\" style='width:100%;height:100%;object-fit:cover'></body>"
    ).format(p=fill)
    ready = "() => [...document.images].every(i => i.complete && i.naturalWidth > 0)"
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chrome, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1000, "height": 700})
        page.set_content(over)
        page.wait_for_function(ready)
        on_image = runtime_metadata(page)["opening_visual"]
        page.set_content(beside)
        page.wait_for_function(ready)
        next_to = runtime_metadata(page)["opening_visual"]
        browser.close()
    assert (on_image["viewport_fraction"], on_image["headline_overlap"], on_image["region_under_headline"]) == (
        100,
        100,
        "uniform",
    )
    assert (next_to["viewport_fraction"], next_to["headline_overlap"]) == (50, 0)


def test_render_measurements_travel_as_facts_not_errors(tmp_path: Path) -> None:
    """opening_visual and image_cropped are readings the author checks a
    declared intent against; a healthy render that carries them must not
    present as erroneous through the public result."""
    outcome = _outcome(
        tmp_path,
        warnings=[
            {
                "code": "opening_visual",
                "details": ["opening visual: hero.png - 100% of viewport; headline overlap 100%"],
            },
            {
                "code": "image_cropped",
                "details": ["demo.png: 960x720 shown in a 480x720 box with object-fit cover, 50% cropped"],
            },
        ],
    )
    result = render_tool_result(outcome)
    assert "errors" not in result
    assert result["warnings"] == ["opening_visual", "image_cropped"]
    assert result["facts"] == [
        "opening visual: hero.png - 100% of viewport; headline overlap 100%",
        "demo.png: 960x720 shown in a 480x720 box with object-fit cover, 50% cropped",
    ]
    metadata = json.loads(preview_tool_result(outcome, max_inline_bytes=10)[0]["text"])
    assert "errors" not in metadata and metadata["facts"] == result["facts"]


_CFT_MAC_ARM64 = "chromium-1234/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"


def _stage_chromium(root: Path, relative: str) -> Path:
    executable = root / relative
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"")
    return executable


def test_discovery_reaches_the_playwright_env_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven_design.rendering.models import _discover_chromium

    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    env_root = tmp_path / "browsers"
    executable = _stage_chromium(env_root, _CFT_MAC_ARM64)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(env_root))

    assert _discover_chromium() == str(executable)


@pytest.mark.parametrize("cache", ["Library/Caches/ms-playwright", ".cache/ms-playwright"])
def test_unset_or_zero_env_root_falls_through_to_the_home_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cache: str
) -> None:
    from raven_design.rendering.models import _discover_chromium

    monkeypatch.setattr("shutil.which", lambda name: None)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    executable = _stage_chromium(home / cache, _CFT_MAC_ARM64)

    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    assert _discover_chromium() == str(executable)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")
    assert _discover_chromium() == str(executable)


def test_env_zero_resolves_the_package_local_browsers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven_design.rendering import models

    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    pkg_root = tmp_path / "pkg" / ".local-browsers"
    executable = _stage_chromium(pkg_root, _CFT_MAC_ARM64)
    monkeypatch.setattr(models, "_package_local_root", lambda: pkg_root)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")

    assert models._discover_chromium() == str(executable)


def test_a_set_env_root_wins_over_the_home_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven_design.rendering.models import _discover_chromium

    monkeypatch.setattr("shutil.which", lambda name: None)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _stage_chromium(home / "Library" / "Caches" / "ms-playwright", _CFT_MAC_ARM64)
    env_root = tmp_path / "browsers"
    from_env = _stage_chromium(env_root, "chromium-1234/chrome-linux/chrome")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(env_root))

    assert _discover_chromium() == str(from_env)


def test_the_vm_root_outranks_the_user_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven_design.rendering.models import _CACHE_PATTERN_GROUPS, _VM_PATTERN_GROUPS, _discover_chromium

    monkeypatch.setattr("shutil.which", lambda name: None)
    vm_root = tmp_path / "ms-playwright"
    from_vm = _stage_chromium(vm_root, "chromium-1234/chrome-linux/chrome")
    cache_root = tmp_path / "cache"
    _stage_chromium(cache_root, _CFT_MAC_ARM64)
    roots = ((vm_root, _VM_PATTERN_GROUPS), (cache_root, _CACHE_PATTERN_GROUPS))

    assert _discover_chromium(roots) == str(from_vm)


def test_the_legacy_chromium_app_layout_is_still_discovered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven_design.rendering.models import _discover_chromium

    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    env_root = tmp_path / "browsers"
    executable = _stage_chromium(env_root, "chromium-1105/chrome-mac/Chromium.app/Contents/MacOS/Chromium")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(env_root))

    assert _discover_chromium() == str(executable)


def test_the_missing_browser_refusal_says_how_to_fix_it(tmp_path: Path) -> None:
    from raven_design.rendering.browser import BrowserAdapter
    from raven_design.rendering.models import RenderConfig, RenderError, RenderRequest

    config = RenderConfig(chrome_path="", libreoffice_path=None)
    detection = Detection(
        format="html",
        family="browser",
        mime="text/html",
        declared_extension=".html",
        support_level="guaranteed",
        metadata={},
    )
    request = RenderRequest(path=tmp_path / "page.html", output_dir=tmp_path)

    with pytest.raises(RenderError) as raised:
        BrowserAdapter(config).render(tmp_path / "page.html", tmp_path, detection, request)

    assert raised.value.code == "renderer_unavailable"
    assert "playwright install chromium" in raised.value.message
    assert "chromePath" in raised.value.message
