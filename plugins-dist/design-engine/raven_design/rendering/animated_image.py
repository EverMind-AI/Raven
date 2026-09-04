"""Decode animated images into normalized render bundles."""

from __future__ import annotations

import json
import subprocess
from math import isfinite
from pathlib import Path
from typing import Any

import PIL
from PIL import Image

from raven_design.rendering.bundle import BundlePaths
from raven_design.rendering.models import (
    AdapterResult,
    Detection,
    RenderConfig,
    RenderError,
    RenderRequest,
)
from raven_design.rendering.pdf import PdfBackend, PyMuPdfBackend
from raven_design.rendering.util import command_version

_LOG_TAIL_LENGTH = 1000


class AnimatedImageAdapter:
    def __init__(
        self,
        config: RenderConfig,
        pdf_backend: PdfBackend | None = None,
    ) -> None:
        self.config = config
        self.pdf = pdf_backend or PyMuPdfBackend()

    def render(
        self,
        source: Path,
        bundle_root: Path,
        detection: Detection,
        request: RenderRequest,
    ) -> AdapterResult:
        self._validate(detection)
        if self.config.ffmpeg_path and self.config.ffprobe_path:
            try:
                timeline = self._decode_ffmpeg(source, bundle_root, detection)
                return self._build_result(
                    bundle_root,
                    detection,
                    request,
                    timeline,
                    backend="ffmpeg",
                    version=command_version(self.config.ffmpeg_path),
                )
            except RenderError:
                result = self._build_result(
                    bundle_root,
                    detection,
                    request,
                    self._decode_pillow(source, bundle_root),
                    backend="pillow",
                    version=PIL.__version__,
                )
                result.warnings.append(
                    {
                        "code": "animation_decode_fallback",
                        "message": "FFmpeg decoding failed; the animation was composited with Pillow.",
                    }
                )
                return result
        return self._build_result(
            bundle_root,
            detection,
            request,
            self._decode_pillow(source, bundle_root),
            backend="pillow",
            version=PIL.__version__,
        )

    def _validate(self, detection: Detection) -> None:
        metadata = detection.metadata
        duration = float(metadata["duration_seconds"])
        if duration > self.config.max_motion_seconds:
            raise RenderError(
                "resource_limit_exceeded",
                "The animated image exceeds the configured duration limit.",
                details={
                    "duration_seconds": duration,
                    "limit": self.config.max_motion_seconds,
                },
            )
        if metadata["frame_count"] > self.config.max_animation_frames:
            raise RenderError(
                "resource_limit_exceeded",
                "The animated image exceeds the frame-count safety limit.",
                details={
                    "frames": metadata["frame_count"],
                    "limit": self.config.max_animation_frames,
                },
            )

    def _decode_ffmpeg(
        self,
        source: Path,
        bundle_root: Path,
        detection: Detection,
    ) -> list[dict[str, Any]]:
        frames = self._probe_frames(source)
        frames_dir = bundle_root / "motion" / "frames"
        frames_dir.mkdir(parents=True)
        command = [
            str(self.config.ffmpeg_path),
            "-v",
            "error",
            "-i",
            str(source),
            "-frames:v",
            str(detection.metadata["frame_count"]),
            "-vsync",
            "0",
            str(frames_dir / "frame-%06d.png"),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RenderError(
                "conversion_failed",
                "FFmpeg could not decode the animated image.",
            ) from exc
        paths = sorted(frames_dir.glob("frame-*.png"))
        if result.returncode != 0 or len(paths) != detection.metadata["frame_count"]:
            raise RenderError(
                "conversion_failed",
                "FFmpeg returned an incomplete animation.",
                details={
                    "exit_code": result.returncode,
                    "stderr": result.stderr[-_LOG_TAIL_LENGTH:],
                },
            )
        fallback_durations = detection.metadata.get("frame_durations_ms") or []
        timeline: list[dict[str, Any]] = []
        elapsed = 0.0
        total_pixels = 0
        for index, path in enumerate(paths):
            probed = frames[index] if index < len(frames) else {}
            timestamp = _number(probed.get("best_effort_timestamp_time"))
            duration = (
                _number(probed.get("pkt_duration_time"))
                or _number(probed.get("duration_time"))
                or (float(fallback_durations[index]) / 1000 if index < len(fallback_durations) else 0.001)
            )
            with Image.open(path) as image:
                total_pixels += image.width * image.height
            if total_pixels > self.config.max_total_pixels:
                raise RenderError(
                    "resource_limit_exceeded",
                    "Decoded animation frames exceed the total pixel limit.",
                )
            at = elapsed if timestamp is None else max(elapsed, timestamp)
            timeline.append(
                {
                    "index": index + 1,
                    "timestamp_seconds": round(at, 6),
                    "duration_seconds": round(max(0.001, duration), 6),
                    "path": path.name,
                }
            )
            elapsed = at + max(0.001, duration)
        return timeline

    def _probe_frames(self, source: Path) -> list[dict[str, Any]]:
        command = [
            str(self.config.ffprobe_path),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=best_effort_timestamp_time,pkt_duration_time,duration_time",
            "-of",
            "json",
            str(source),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )
            payload = json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            raise RenderError(
                "conversion_failed",
                "ffprobe could not inspect the animation timeline.",
            ) from exc
        frames = payload.get("frames")
        if result.returncode != 0 or not isinstance(frames, list):
            raise RenderError(
                "conversion_failed",
                "ffprobe returned no usable animation timeline.",
            )
        return [frame for frame in frames if isinstance(frame, dict)]

    def _decode_pillow(
        self,
        source: Path,
        bundle_root: Path,
    ) -> list[dict[str, Any]]:
        frames_dir = bundle_root / "motion" / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        timeline: list[dict[str, Any]] = []
        elapsed = 0.0
        total_pixels = 0
        with Image.open(source) as image:
            for index in range(image.n_frames):
                image.seek(index)
                frame = image.convert("RGBA").copy()
                total_pixels += frame.width * frame.height
                if total_pixels > self.config.max_total_pixels:
                    raise RenderError(
                        "resource_limit_exceeded",
                        "Decoded animation frames exceed the total pixel limit.",
                    )
                duration = max(0.001, float(image.info.get("duration", 0)) / 1000)
                target = frames_dir / f"frame-{index + 1:06d}.png"
                frame.save(target, "PNG")
                timeline.append(
                    {
                        "index": index + 1,
                        "timestamp_seconds": round(elapsed, 6),
                        "duration_seconds": round(duration, 6),
                        "path": target.name,
                    }
                )
                elapsed += duration
        return timeline

    def _build_result(
        self,
        bundle_root: Path,
        detection: Detection,
        request: RenderRequest,
        timeline: list[dict[str, Any]],
        *,
        backend: str,
        version: str | None,
    ) -> AdapterResult:
        snapshot = timeline[0]
        for frame in timeline:
            if frame["timestamp_seconds"] <= request.capture_at_seconds:
                snapshot = frame
            else:
                break
        frames_dir = bundle_root / "motion" / "frames"
        self.pdf.image_to_pdf(
            frames_dir / snapshot["path"],
            BundlePaths(bundle_root).document,
        )
        elapsed = sum(float(frame["duration_seconds"]) for frame in timeline)
        return AdapterResult(
            renderer={
                "adapter": "animated_image",
                "backend": backend,
                "engine_version": version,
                "isolated": False,
                "network_policy": "none",
            },
            rendered={
                "frame_count": len(timeline),
                "duration_seconds": round(elapsed, 6),
                "width": detection.metadata["width"],
                "height": detection.metadata["height"],
            },
            motion={
                "classification": "autoplay_dynamic",
                "confidence": "container_semantics",
                "observed_ms": round(elapsed * 1000),
                "signals": ["container_animation"],
                "changed_frame_pairs": None,
            },
            artifacts={},
            preview_candidates=[
                {
                    "path": f"motion/frames/{frame['path']}",
                    "kind": "frame",
                    "at_ms": round(frame["timestamp_seconds"] * 1000),
                    "duration_seconds": frame["duration_seconds"],
                    "primary": frame["path"] == snapshot["path"],
                }
                for frame in timeline
            ],
            warnings=[],
        )


def _number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) and result >= 0 else None
