"""Unit tests for animation capture and representative frame selection."""

from __future__ import annotations

from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw

from raven.rendering.animated_image import _number
from raven.rendering.browser import _validate_capture_budget
from raven.rendering.browser_runtime import classify_motion, timeline_frame_times
from raven.rendering.keyframes import select_keyframes
from raven.rendering.models import RenderConfig, RenderError, RenderRequest


def _candidate(
    root: Path,
    name: str,
    color: str,
    at_ms: int,
    duration: float,
    *,
    primary: bool = False,
) -> dict[str, object]:
    Image.new("RGB", (160, 90), color).save(root / name, "PNG")
    return {
        "path": name,
        "at_ms": at_ms,
        "duration_seconds": duration,
        "primary": primary,
    }


def test_runtime_animation_activity_is_classified_as_dynamic() -> None:
    classification, confidence, _ = classify_motion(
        "auto",
        ["request_animation_frame"],
        [],
        {
            "playing_animation_count": 0,
            "video_count": 0,
            "audio_count": 0,
            "probe": {"raf": 1},
        },
    )

    assert classification == "autoplay_dynamic"
    assert confidence == "runtime_activity"


def test_non_finite_ffprobe_timestamps_are_ignored() -> None:
    assert _number("inf") is None
    assert _number("nan") is None


def test_browser_timeline_always_includes_exact_capture_end() -> None:
    assert timeline_frame_times(0.3, 8) == [0, 0.125, 0.25, 0.3]


def test_script_page_gets_timeline_even_when_probes_see_nothing() -> None:
    # Probes miss slow/small motion and the deterministic clock defeats
    # the rAF counter, so a page that can run code must default to the
    # full timeline rather than collapse to a single static frame.
    classification, confidence, _ = classify_motion(
        "auto",
        ["script", "canvas", "user_interaction"],
        [],
        {
            "animation_count": 0,
            "playing_animation_count": 0,
            "video_count": 0,
            "audio_count": 0,
            "canvas_count": 1,
            "event_attribute_count": 0,
            "probe": {"raf": 0, "timers": 0, "canvas": 1},
        },
    )

    assert classification == "autoplay_dynamic"
    assert confidence == "source_signals"


def test_hover_only_page_is_classified_as_interaction_required() -> None:
    classification, confidence, _ = classify_motion(
        "auto",
        ["hover", "user_interaction"],
        [],
        {
            "animation_count": 0,
            "playing_animation_count": 0,
            "video_count": 0,
            "audio_count": 0,
            "probe": {"raf": 0, "timers": 0},
        },
    )

    assert classification == "interaction_required"
    assert confidence == "source_and_runtime_signals"


def test_inert_page_stays_static() -> None:
    classification, confidence, _ = classify_motion(
        "auto",
        [],
        [],
        {
            "animation_count": 0,
            "playing_animation_count": 0,
            "video_count": 0,
            "audio_count": 0,
            "probe": {"raf": 0, "timers": 0},
        },
    )

    assert classification == "static_observed"
    assert confidence == "observed"


def test_browser_capture_budget_rejects_extreme_dynamic_timeline() -> None:
    config = RenderConfig(
        chrome_path=None,
        libreoffice_path=None,
        max_total_pixels=1_000_000,
        motion_fps=30,
    )
    request = RenderRequest(
        path=Path("page.html"),
        output_dir=Path("output"),
        motion_mode="dynamic",
        capture_duration_seconds=10,
        viewport_width=3840,
        viewport_height=2160,
    )

    with pytest.raises(RenderError) as raised:
        _validate_capture_budget(
            (request.viewport_width, request.viewport_height),
            request,
            config,
            include_timeline=True,
        )

    assert raised.value.code == "resource_limit_exceeded"


def test_keyframes_put_primary_first_and_remove_visual_duplicates(
    tmp_path: Path,
) -> None:
    candidates = [
        _candidate(tmp_path, "start.png", "white", 0, 0.1),
        _candidate(tmp_path, "middle-a.png", "gray", 100, 0.1),
        _candidate(tmp_path, "middle-b.png", "gray", 200, 0.1),
        _candidate(tmp_path, "final.png", "black", 300, 0.1, primary=True),
    ]

    selected = select_keyframes(candidates, tmp_path, 3)

    assert selected[0]["at_ms"] == 300
    assert selected[1]["at_ms"] == 0
    assert selected[2]["at_ms"] in {100, 200}


def test_keyframes_weight_long_lived_states_over_flashes(tmp_path: Path) -> None:
    candidates = [
        _candidate(tmp_path, "primary.png", "black", 0, 1.0, primary=True),
        _candidate(tmp_path, "long.png", "blue", 1000, 8.0),
        _candidate(tmp_path, "flash.png", "red", 9000, 0.1),
    ]

    selected = select_keyframes(candidates, tmp_path, 2)

    assert [item["at_ms"] for item in selected] == [0, 1000]


def test_keyframes_keep_small_visible_motion(tmp_path: Path) -> None:
    candidates = []
    for index, left in enumerate((8, 72, 136)):
        image = Image.new("RGB", (160, 90), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((left, 40, left + 7, 47), fill=(0, 80, 255))
        name = f"motion-{index}.png"
        image.save(tmp_path / name, "PNG")
        candidates.append(
            {
                "path": name,
                "at_ms": index * 500,
                "duration_seconds": 0.5,
                "primary": index == 2,
            }
        )

    assert len(select_keyframes(candidates, tmp_path, 3)) == 3


def test_visible_text_state_can_distinguish_identical_rasters(tmp_path: Path) -> None:
    candidates = [
        _candidate(tmp_path, "before.png", "white", 0, 1.0, primary=True),
        _candidate(tmp_path, "after.png", "white", 1000, 1.0),
    ]
    candidates[0]["_state"] = {"visible_text_tokens": ["before"]}
    candidates[1]["_state"] = {"visible_text_tokens": ["after"]}

    selected = select_keyframes(candidates, tmp_path, 2)

    assert [item["at_ms"] for item in selected] == [0, 1000]
