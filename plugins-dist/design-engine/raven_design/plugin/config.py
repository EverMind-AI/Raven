"""The plugin's config slice, owned here the way every product plugin owns its own.

``plugins.config["design-engine"]`` carries the fork's product knobs in the
product config's camelCase: the ``visualDomainSelector`` section (the fork's
``skillForge.visualDomainSelector``), the ``render`` section (the fork's
``tools.render``, every field of the fork schema accepted at its fork default),
and the ``taskState`` section (the resident Task State surface, C2's tools+
hooks lean). ``config_schema`` in the manifest stays empty on purpose:
admission with an empty declaration is verbatim pass-through, and this module
owns the slice shape -- the division the sibling product plugins use.

Parsing is strict, and a violation raises rather than mends: the fork said
these shapes with pydantic (wrong types refused loudly at config load), and a
hand parser that coerced -- ``bool("false")`` is True -- or silently fell back
to a default would invert the very knob the user set. The factories catch the
raise and cast the fail-closed sentinel (see ``raven_design.plugin``): the
host's stack builder skips a raising factory quietly, and closed-and-loud
beats open-and-quiet (the code-flow MisconfiguredGate doctrine, w101).

One deliberate flip against the fork schema, the ppt-engine precedent kept:
the fork's top ``enabled`` defaulted True because the fork BUILD was the
product. A wheel is installed into any raven environment, so here an absent
slice means "this instance never asked for the design engine" and every
factory declines (the D6 admission shape). The shipped product slice spells
``enabled: true``, so the product face is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _flag(raw: dict[str, Any], key: str, fallback: bool) -> bool:
    value = raw.get(key, fallback)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false, got {value!r}")
    return value


def _text(raw: dict[str, Any], key: str, fallback: str) -> str:
    value = raw.get(key, fallback)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string, got {value!r}")
    return value


def _count(raw: dict[str, Any], key: str, fallback: int, low: int, high: int) -> int:
    value = raw.get(key, fallback)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer, got {value!r}")
    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low} and {high}, got {value}")
    return value


def _number(raw: dict[str, Any], key: str, fallback: float, low: float, high: float) -> float:
    value = raw.get(key, fallback)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number, got {value!r}")
    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low} and {high}, got {value}")
    return float(value)


def _texts(raw: dict[str, Any], key: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    value = raw.get(key)
    if value is None:
        return fallback
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of strings, got {value!r}")
    return tuple(value)


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object, got {value!r}")
    return value


@dataclass(frozen=True)
class SelectorConfig:
    """The fork's ``skillForge.visualDomainSelector`` leaves, fork defaults kept.

    The fork's ``model`` and ``provider`` pinning leaves are NOT carried: the
    fork built a credentialed pin through its provider pool (fork
    factory.py:192-206), a seat this host does not grant a plugin, and a knob
    the code cannot honour is a phantom by this wave's own D4 floor. The
    selector permanently follows the conversation's binding -- the fork's own
    no-credentials fallback, now the only semantics (ledgered loss line).
    """

    enabled: bool = True
    preferred_max: int = 2
    alternatives_max: int = 3
    max_tokens: int = 8192
    temperature: float = 0.0

    @classmethod
    def from_section(cls, raw: dict[str, Any]) -> "SelectorConfig":
        known = {"enabled", "preferredMax", "alternativesMax", "maxTokens", "temperature"}
        if unknown := set(raw) - known:
            raise ValueError(f"unknown visualDomainSelector keys: {sorted(unknown)}")
        return cls(
            enabled=_flag(raw, "enabled", True),
            preferred_max=_count(raw, "preferredMax", 2, 0, 5),
            alternatives_max=_count(raw, "alternativesMax", 3, 0, 15),
            max_tokens=_count(raw, "maxTokens", 8192, 1, 200_000),
            temperature=_number(raw, "temperature", 0.0, 0.0, 2.0),
        )


#: The fork RenderToolConfig's fields (fork schema.py:883-934), one row per
#: camelCase knob: (slice key, attr, parser, default, bounds). Every field
#: keeps its fork default so ``RenderService.from_tool_config`` finds the
#: exact attribute set it has always read; the shipped slice spells five.
_OFFICE_BACKENDS = ("onlyoffice", "libreoffice", "graph")

_RENDER_SPEC: tuple[tuple[str, str, str, Any, tuple[Any, Any] | None], ...] = (
    ("enabled", "enabled", "flag", True, None),
    # Fail-closed by default: the fork seat passed the HOST's
    # tools.restrictToWorkspace through to the render path policy (fork
    # loop main.py:900), but a plugin is granted no read of that host knob
    # today -- so the wheel restricts unless the slice says otherwise. The
    # shipped product slice spells false (fork parity for this product);
    # any other installer gets a fenced render lane until they choose.
    ("restrictToWorkspace", "restrict_to_workspace", "flag", True, None),
    ("backend", "backend", "choice", "auto", ("auto", "direct", "boxlite")),
    ("workerImage", "worker_image", "text", "", None),
    ("workerCpus", "worker_cpus", "count", 2, (1, 64)),
    ("workerMemoryMib", "worker_memory_mib", "count", 4096, (512, 1_048_576)),
    ("workerCreateTimeoutSeconds", "worker_create_timeout_seconds", "count", 300, (1, 86_400)),
    ("allowNetwork", "allow_network", "flag", False, None),
    ("networkAllowlist", "network_allowlist", "texts", (), None),
    ("officeBackendOrder", "office_backend_order", "texts", ("onlyoffice", "libreoffice"), None),
    ("allowCloudOffice", "allow_cloud_office", "flag", False, None),
    ("maxInputBytes", "max_input_bytes", "count", 100 * 1024 * 1024, (1, 1 << 40)),
    ("maxAssetBytes", "max_asset_bytes", "count", 200 * 1024 * 1024, (1, 1 << 40)),
    ("maxOutputBytes", "max_output_bytes", "count", 1024 * 1024 * 1024, (1, 1 << 42)),
    ("maxPages", "max_pages", "count", 200, (1, 200)),
    ("rasterDpi", "raster_dpi", "count", 144, (72, 600)),
    ("previewMaxEdge", "preview_max_edge", "count", 2048, (256, 8192)),
    ("defaultPreviewCount", "default_preview_count", "count", 6, (1, 12)),
    ("maxPreviewCount", "max_preview_count", "count", 12, (1, 12)),
    ("maxInlinePreviewBytes", "max_inline_preview_bytes", "count", 8 * 1024 * 1024, (1, 1 << 32)),
    ("defaultCaptureDurationSeconds", "default_capture_duration_seconds", "number", 3.0, (0.25, 10.0)),
    ("maxCaptureDurationSeconds", "max_capture_duration_seconds", "number", 10.0, (0.25, 10.0)),
    ("motionFps", "motion_fps", "count", 8, (1, 30)),
    ("timeoutSeconds", "timeout_seconds", "count", 180, (1, 86_400)),
    ("maxConcurrency", "max_concurrency", "count", 2, (1, 32)),
    ("locale", "locale", "text", "en-US", None),
    ("timezone", "timezone", "text", "UTC", None),
    ("chromePath", "chrome_path", "text", "", None),
    ("libreofficePath", "libreoffice_path", "text", "", None),
    ("ffmpegPath", "ffmpeg_path", "text", "", None),
    ("ffprobePath", "ffprobe_path", "text", "", None),
    ("qpdfPath", "qpdf_path", "text", "", None),
    ("maxSidePixels", "max_side_pixels", "count", 8192, (256, 1 << 20)),
    ("maxViewportWidth", "max_viewport_width", "count", 8192, (320, 1 << 20)),
    ("maxViewportHeight", "max_viewport_height", "count", 8192, (240, 1 << 20)),
    ("maxTotalPixels", "max_total_pixels", "count", 500_000_000, (1, 1 << 42)),
    ("maxAnimationFrames", "max_animation_frames", "count", 1000, (2, 100_000)),
    ("browserReadySeconds", "browser_ready_seconds", "number", 10.0, (0.1, 3600.0)),
    ("spreadsheetViewportWidth", "spreadsheet_viewport_width", "count", 1600, (320, 100_000)),
    ("spreadsheetViewportHeight", "spreadsheet_viewport_height", "count", 1000, (240, 100_000)),
    ("spreadsheetMaxViewports", "spreadsheet_max_viewports", "count", 4, (1, 24)),
)


@dataclass(frozen=True)
class RenderSettings:
    """The fork ``tools.render`` schema said as a plain frozen dataclass.

    ``RenderService.from_tool_config`` reads these attributes by name; the
    field set and every default are the fork's own, so the service sees the
    schema it was written against.
    """

    enabled: bool = True
    restrict_to_workspace: bool = True
    backend: str = "auto"
    worker_image: str = ""
    worker_cpus: int = 2
    worker_memory_mib: int = 4096
    worker_create_timeout_seconds: int = 300
    allow_network: bool = False
    network_allowlist: tuple[str, ...] = ()
    office_backend_order: tuple[str, ...] = ("onlyoffice", "libreoffice")
    allow_cloud_office: bool = False
    max_input_bytes: int = 100 * 1024 * 1024
    max_asset_bytes: int = 200 * 1024 * 1024
    max_output_bytes: int = 1024 * 1024 * 1024
    max_pages: int = 200
    raster_dpi: int = 144
    preview_max_edge: int = 2048
    default_preview_count: int = 6
    max_preview_count: int = 12
    max_inline_preview_bytes: int = 8 * 1024 * 1024
    default_capture_duration_seconds: float = 3.0
    max_capture_duration_seconds: float = 10.0
    motion_fps: int = 8
    timeout_seconds: int = 180
    max_concurrency: int = 2
    locale: str = "en-US"
    timezone: str = "UTC"
    chrome_path: str = ""
    libreoffice_path: str = ""
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    qpdf_path: str = ""
    max_side_pixels: int = 8192
    max_viewport_width: int = 8192
    max_viewport_height: int = 8192
    max_total_pixels: int = 500_000_000
    max_animation_frames: int = 1000
    browser_ready_seconds: float = 10.0
    spreadsheet_viewport_width: int = 1600
    spreadsheet_viewport_height: int = 1000
    spreadsheet_max_viewports: int = 4

    @classmethod
    def from_section(cls, raw: dict[str, Any]) -> "RenderSettings":
        known = {row[0] for row in _RENDER_SPEC}
        if unknown := set(raw) - known:
            raise ValueError(f"unknown render keys: {sorted(unknown)}")
        values: dict[str, Any] = {}
        for key, attr, kind, default, bounds in _RENDER_SPEC:
            if kind == "flag":
                values[attr] = _flag(raw, key, default)
            elif kind == "text":
                values[attr] = _text(raw, key, default)
            elif kind == "texts":
                values[attr] = _texts(raw, key, default)
                if key == "officeBackendOrder":
                    if unknown := [name for name in values[attr] if name not in _OFFICE_BACKENDS]:
                        raise ValueError(
                            f"officeBackendOrder must name only {', '.join(_OFFICE_BACKENDS)}; got {unknown}"
                        )
            elif kind == "count":
                values[attr] = _count(raw, key, default, *bounds)
            elif kind == "number":
                values[attr] = _number(raw, key, default, *bounds)
            elif kind == "choice":
                value = _text(raw, key, default)
                if value not in bounds:
                    raise ValueError(f"{key} must be one of {', '.join(bounds)}, got {value!r}")
                values[attr] = value
        settings = cls(**values)
        # The fork schema's two cross-field refusals, kept as refusals.
        if settings.default_preview_count > settings.max_preview_count:
            raise ValueError("defaultPreviewCount cannot exceed maxPreviewCount")
        if settings.default_capture_duration_seconds > settings.max_capture_duration_seconds:
            raise ValueError("defaultCaptureDurationSeconds cannot exceed maxCaptureDurationSeconds")
        return settings


@dataclass(frozen=True)
class TaskStateConfig:
    """The resident Task State surface (verdict C2, tools + hooks).

    ``state_root`` names the directory the sidecar store writes under; the
    launcher renders it beneath the product state root at the exec swap. An
    empty value declines the surface rather than inventing a location: state
    written to a guessed path is state a reinstall silently abandons.
    """

    enabled: bool = True
    state_root: str = ""

    @classmethod
    def from_section(cls, raw: dict[str, Any]) -> "TaskStateConfig":
        known = {"enabled", "stateRoot"}
        if unknown := set(raw) - known:
            raise ValueError(f"unknown taskState keys: {sorted(unknown)}")
        return cls(enabled=_flag(raw, "enabled", True), state_root=_text(raw, "stateRoot", ""))


@dataclass(frozen=True)
class EngineConfig:
    """The slice, read once at activation."""

    enabled: bool = False
    selector: SelectorConfig = SelectorConfig()
    render: RenderSettings = RenderSettings()
    task_state: TaskStateConfig = TaskStateConfig()

    @classmethod
    def from_slice(cls, raw: dict[str, Any] | None) -> "EngineConfig":
        raw = raw or {}
        known = {"enabled", "visualDomainSelector", "render", "taskState"}
        if unknown := set(raw) - known:
            raise ValueError(f"unknown design-engine keys: {sorted(unknown)}")
        return cls(
            enabled=_flag(raw, "enabled", False),
            selector=SelectorConfig.from_section(_section(raw, "visualDomainSelector")),
            render=RenderSettings.from_section(_section(raw, "render")),
            task_state=TaskStateConfig.from_section(_section(raw, "taskState")),
        )


__all__ = ["EngineConfig", "RenderSettings", "SelectorConfig", "TaskStateConfig"]
