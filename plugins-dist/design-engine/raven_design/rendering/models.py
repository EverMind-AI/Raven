"""Shared configuration, request, outcome, and error models for rendering."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NotRequired, Protocol, TypedDict

DEFAULT_CAPTURE_DURATION_SECONDS = 3.0
DEFAULT_PREVIEW_COUNT = 6
DEFAULT_VIEWPORT = (1440, 900)
MOTION_MODES = frozenset({"auto", "static", "dynamic"})

_VM_BROWSER_ROOT = Path("/ms-playwright")
_VM_PATTERN_GROUPS = (
    (
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-linux64/chrome",
    ),
    (
        "chromium_headless_shell-*/chrome-headless-shell-linux/headless_shell",
        "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
    ),
)
# playwright >= 1.58 installs macOS Chromium as a Chrome for Testing app
# bundle; older caches still carry the pre-CfT Chromium.app layout, so
# every pattern must end at the executable the config will exec.
_CACHE_PATTERN_GROUPS = (
    ("chromium-*/chrome-linux/chrome", "chromium-*/chrome-linux64/chrome"),
    ("chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",),
    ("chromium-*/chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",),
    ("chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",),
)

DiscoveryRoots = Sequence[tuple[Path, tuple[tuple[str, ...], ...]]]


def _package_local_root() -> Path | None:
    # PLAYWRIGHT_BROWSERS_PATH="0" is playwright's spelling for "keep browsers
    # inside the package", not a path: resolve the package the way doctor does,
    # so the two detectors cannot disagree about the same environment.
    spec = importlib.util.find_spec("playwright")
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).parent / ".local-browsers"


def _playwright_roots() -> DiscoveryRoots:
    # /ms-playwright is the boxlite VM image layout and worker.py re-runs
    # discovery inside that VM, so it outranks the PLAYWRIGHT_BROWSERS_PATH
    # override, which in turn outranks the per-user host caches.
    roots: list[tuple[Path, tuple[tuple[str, ...], ...]]] = [(_VM_BROWSER_ROOT, _VM_PATTERN_GROUPS)]
    env_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if env_root == "0":
        local = _package_local_root()
        if local is not None:
            roots.append((local, _CACHE_PATTERN_GROUPS))
    elif env_root:
        roots.append((Path(env_root), _CACHE_PATTERN_GROUPS))
    home = Path.home()
    roots.append((home / "Library" / "Caches" / "ms-playwright", _CACHE_PATTERN_GROUPS))
    roots.append((home / ".cache" / "ms-playwright", _CACHE_PATTERN_GROUPS))
    return roots


def _discover_chromium(roots: DiscoveryRoots | None = None) -> str | None:
    executable = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if executable:
        return executable
    for browser_root, pattern_groups in _playwright_roots() if roots is None else roots:
        if not browser_root.is_dir():
            continue
        for patterns in pattern_groups:
            candidates = sorted(candidate for pattern in patterns for candidate in browser_root.glob(pattern))
            if candidates:
                return str(candidates[-1])
    return None


class RenderError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }

    def as_tool_error(self) -> str:
        return f"Error: {json.dumps(self.as_dict(), separators=(',', ':'))}"


@dataclass(frozen=True)
class Detection:
    format: str
    family: str
    mime: str
    declared_extension: str
    support_level: str
    metadata: dict[str, Any]
    motion_signals: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RenderRequest:
    path: Path
    output_dir: Path
    motion_mode: str = "auto"
    capture_duration_seconds: float = DEFAULT_CAPTURE_DURATION_SECONDS
    capture_at_seconds: float = 0.0
    page_range: str | None = None
    viewport_width: int = DEFAULT_VIEWPORT[0]
    viewport_height: int = DEFAULT_VIEWPORT[1]
    asset_root: Path | None = None
    internal_output: bool = False
    actions: tuple[dict[str, Any], ...] | None = None
    scale: float = 1.0


@dataclass(frozen=True)
class RenderConfig:
    chrome_path: str | None
    libreoffice_path: str | None
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    qpdf_path: str | None = None
    allow_network: bool = False
    network_allowlist: tuple[str, ...] = ()
    office_backend_order: tuple[str, ...] = ("onlyoffice", "libreoffice")
    allow_cloud_office: bool = False
    raster_dpi: int = 144
    preview_max_edge: int = 2048
    max_input_bytes: int = 100 * 1024 * 1024
    max_asset_bytes: int = 200 * 1024 * 1024
    max_output_bytes: int = 1024 * 1024 * 1024
    max_pages: int = 200
    max_side_pixels: int = 8192
    max_total_pixels: int = 500_000_000
    max_inline_preview_bytes: int = 8 * 1024 * 1024
    default_preview_count: int = DEFAULT_PREVIEW_COUNT
    max_preview_count: int = 12
    min_motion_seconds: float = 0.25
    max_motion_seconds: float = 10.0
    max_animation_frames: int = 1000
    motion_fps: int = 8
    timeout_seconds: int = 180
    browser_ready_seconds: float = 10.0
    min_viewport_width: int = 320
    max_viewport_width: int = 8192
    min_viewport_height: int = 240
    max_viewport_height: int = 8192
    browser_changed_pixel_limit: float = 0.01
    browser_channel_delta_limit: float = 0.005
    spreadsheet_viewport_width: int = 1600
    spreadsheet_viewport_height: int = 1000
    spreadsheet_viewport_depth: int = 24
    spreadsheet_max_viewports: int = 4
    locale: str = "en-US"
    timezone: str = "UTC"

    @classmethod
    def discover(cls, **overrides: Any) -> RenderConfig:
        values: dict[str, Any] = {
            "chrome_path": _discover_chromium(),
            "libreoffice_path": shutil.which("libreoffice") or shutil.which("soffice"),
            "ffmpeg_path": shutil.which("ffmpeg"),
            "ffprobe_path": shutil.which("ffprobe"),
            "qpdf_path": shutil.which("qpdf"),
        }
        values.update(overrides)
        return cls(**values)


@dataclass
class AdapterResult:
    renderer: dict[str, Any]
    rendered: dict[str, Any]
    motion: dict[str, Any]
    artifacts: dict[str, Any]
    preview_candidates: list[str | dict[str, Any]]
    warnings: list[dict[str, Any]]


@dataclass
class RenderOutcome:
    bundle_dir: Path
    detection: Detection
    page_records: list[dict[str, Any]]
    preview_records: list[dict[str, Any]]
    preview_candidate_count: int
    warnings: list[dict[str, Any]]


class RenderAdapter(Protocol):
    def render(
        self,
        source: Path,
        bundle_root: Path,
        detection: Detection,
        request: RenderRequest,
    ) -> AdapterResult: ...


class RenderToolOutput(TypedDict):
    dir: str
    warnings: NotRequired[list[str]]
    errors: NotRequired[list[str]]
    facts: NotRequired[list[str]]
