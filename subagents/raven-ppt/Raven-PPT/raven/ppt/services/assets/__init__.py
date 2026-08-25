"""Reviewed visual assets: palettes, outline icons, measurement fonts.

Three kinds of thing, one service, because they answer one question -- "what may
this deck draw with?" -- and they answer it the same way: a closed, reviewed set
rather than a free choice. A palette whose contrast was verified, a font whose
metrics the renderer will not widen, an icon that stays vector after export.
Whoever draws picks from the set; nothing here draws.

Nothing here is physical geometry either. Colours are paints, icons are strokes
on a unit grid, and a theme carries no type scale at all -- the sizes a page ends
up set at are measured against that page by `services/measure`, not chosen
alongside the palette. That keeps the hard invariant intact by construction:
there is no font-size field in this package for a model to reach.

The one filesystem-shaped thing is `script_helpers`, and even that only produces
text. It projects the themes and icons into two importable modules for the script
route; writing them into a build directory is the script backend's job, since the
backend owns that directory.
"""

from raven.ppt.services.assets.color import (
    contrast_ratio,
    ensure_contrast,
    is_canonical_hex,
    mix,
    relative_luminance,
)
from raven.ppt.services.assets.fonts import (
    MEASURED_SAFE_FONTS,
    FontError,
    MeasurementFonts,
    measurement_fonts,
)
from raven.ppt.services.assets.icons import (
    ICON_GRID,
    IconDataError,
    UnknownIconError,
    icon_candidates,
    icon_names,
    icon_paths,
    icon_provenance,
    resolve_icon_name,
)
from raven.ppt.services.assets.themes import (
    ACCENT_CONTRAST_TARGET,
    DECOR_LANGUAGE_GUIDE,
    DECOR_LANGUAGES,
    DEFAULT_THEME_ID,
    FOREGROUND_CONTRAST_TARGET,
    STRUCTURAL_TONES,
    THEME_GUIDE,
    THEMES,
    TITLE_GEOMETRIES,
    TITLE_GEOMETRY_GUIDE,
    Theme,
    ThemeError,
    auto_theme_id,
    derive_theme,
    get_theme,
    theme_guide_lines,
)

__all__ = [
    "ACCENT_CONTRAST_TARGET",
    "DECOR_LANGUAGES",
    "DECOR_LANGUAGE_GUIDE",
    "DEFAULT_THEME_ID",
    "FOREGROUND_CONTRAST_TARGET",
    "ICON_GRID",
    "MEASURED_SAFE_FONTS",
    "STRUCTURAL_TONES",
    "THEMES",
    "THEME_GUIDE",
    "TITLE_GEOMETRIES",
    "TITLE_GEOMETRY_GUIDE",
    "FontError",
    "IconDataError",
    "UnknownIconError",
    "MeasurementFonts",
    "Theme",
    "ThemeError",
    "auto_theme_id",
    "contrast_ratio",
    "derive_theme",
    "ensure_contrast",
    "get_theme",
    "icon_candidates",
    "icon_names",
    "icon_paths",
    "icon_provenance",
    "is_canonical_hex",
    "measurement_fonts",
    "mix",
    "relative_luminance",
    "resolve_icon_name",
    "theme_guide_lines",
]
