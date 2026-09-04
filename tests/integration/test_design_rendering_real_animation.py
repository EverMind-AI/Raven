"""Integration coverage for real animated-image rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PIL")
pytest.importorskip("fitz")
from PIL import Image, ImageColor, features

from raven_design.rendering.models import RenderConfig
from raven_design.rendering.paths import RenderPathPolicy
from raven_design.rendering.pdf import image_difference
from raven_design.rendering.service import RenderService


def _service(tmp_path: Path) -> RenderService:
    config = RenderConfig.discover(
        ffmpeg_path=None,
        ffprobe_path=None,
        max_motion_seconds=10,
    )
    return RenderService(
        config,
        path_policy=RenderPathPolicy(
            workspace=tmp_path,
            media_root=tmp_path,
            runtime_root=tmp_path / "runtime",
            restrict_to_workspace=True,
        ),
        backend="direct",
        max_concurrency=1,
    )


def _frames() -> list[Image.Image]:
    frames = []
    for offset, color in ((0, "red"), (16, "green"), (32, "blue")):
        frame = Image.new("RGBA", (96, 64), (255, 255, 255, 0))
        for x in range(offset, offset + 32):
            for y in range(16, 48):
                frame.putpixel((x, y), ImageColor.getrgb(color) + (255,))
        frames.append(frame)
    return frames


def _write_animation(path: Path, format: str) -> None:
    frames = _frames()
    options = {
        "format": format,
        "save_all": True,
        "append_images": frames[1:],
        "duration": [100, 300, 600],
        "loop": 0,
    }
    if format == "GIF":
        options["disposal"] = 2
    frames[0].save(path, **options)


@pytest.mark.asyncio
async def test_nonuniform_gif_timeline_and_primary_snapshot_match(tmp_path: Path) -> None:
    source = tmp_path / "animation.gif"
    _write_animation(source, "GIF")
    service = _service(tmp_path)

    parts = await service.preview(
        path=source,
        output_dir=tmp_path / "output",
        max_previews=3,
        capture_duration_seconds=1,
    )

    bundle = next((tmp_path / "output").iterdir())
    metadata = json.loads(parts[0]["text"])
    assert metadata["views"][0] == {"image": 1, "at_ms": 400}
    assert {view["at_ms"] for view in metadata["views"]} == {0, 100, 400}
    assert {item.name for item in bundle.iterdir()} == {
        "document.pdf",
        "pages",
        "preview",
    }
    primary = min((bundle / "preview").iterdir())
    assert image_difference(primary, bundle / "pages" / "page-0001.png") == {
        "changed_pixel_ratio": 0.0,
        "mean_absolute_channel_delta": 0.0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffix", "format", "feature"),
    [
        (".png", "PNG", None),
        (".webp", "WEBP", "webp"),
    ],
)
async def test_apng_and_webp_publish_keyframes_only(
    tmp_path: Path,
    suffix: str,
    format: str,
    feature: str | None,
) -> None:
    if feature and not features.check(feature):
        pytest.skip(f"Pillow lacks {feature}")
    source = tmp_path / f"animation{suffix}"
    _write_animation(source, format)
    service = _service(tmp_path)

    parts = await service.preview(
        path=source,
        output_dir=tmp_path / "output",
        max_previews=2,
        capture_duration_seconds=1,
    )

    bundle = next((tmp_path / "output").iterdir())
    metadata = json.loads(parts[0]["text"])
    assert len(metadata["views"]) == len(parts) - 1
    assert all("at_ms" in view for view in metadata["views"])
    assert {item.name for item in bundle.iterdir()} == {
        "document.pdf",
        "pages",
        "preview",
    }
