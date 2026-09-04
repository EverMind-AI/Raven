"""Low-level file, image, hashing, and command helpers for rendering."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_record(path: Path, root: Path) -> dict[str, Any]:
    with Image.open(path) as image:
        width, height = image.size
    return {
        "path": path.relative_to(root).as_posix(),
        "width": width,
        "height": height,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def json_write(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def safe_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._").lower()
    return (stem or "artifact")[:64]


def command_version(command: str | None) -> str | None:
    if not command:
        return None
    try:
        result = subprocess.run(
            [command, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError:
        return None
    line = (result.stdout or result.stderr).strip().splitlines()
    return line[0][:200] if line else None


def make_preview_image(source: Path, target: Path, max_edge: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        if image.format == "PNG" and max(image.size) <= max_edge:
            copy_source = True
        else:
            copy_source = False
            has_alpha = image.mode in {"RGBA", "LA"} or "transparency" in image.info
            image = image.convert("RGBA" if has_alpha else "RGB")
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            image.save(target, "PNG", optimize=True)
    if copy_source:
        shutil.copy2(source, target)


def crop_white_margin(
    source: Image.Image,
    *,
    threshold: int = 8,
) -> tuple[Image.Image, tuple[int, int, int, int]]:
    image = source.convert("RGB")
    difference = ImageChops.difference(
        image,
        Image.new("RGB", image.size, "white"),
    ).convert("L")
    mask = difference.point(lambda value: 255 if value > threshold else 0)
    content_bounds = mask.getbbox()
    if content_bounds is None:
        return image, (0, 0, image.width, image.height)
    padding = max(12, round(max(image.size) * 0.015))
    bounds = (
        max(0, content_bounds[0] - padding),
        max(0, content_bounds[1] - padding),
        min(image.width, content_bounds[2] + padding),
        min(image.height, content_bounds[3] + padding),
    )
    return image.crop(bounds), bounds


def evenly_spaced(items: list[Any], limit: int) -> list[Any]:
    if len(items) <= limit:
        return items
    if limit == 1:
        return [items[0]]
    indexes = {round(index * (len(items) - 1) / (limit - 1)) for index in range(limit)}
    return [items[index] for index in sorted(indexes)]
