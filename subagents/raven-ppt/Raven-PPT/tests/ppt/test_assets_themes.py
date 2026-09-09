"""What a palette has to be true of before a deck is allowed to use it.

Two families of assertion live here. The presets are checked as data -- ten of
them, every page white, every text contrast cleared -- because they were
verified by hand once and this is what stops a later edit undoing that quietly.
The derived themes are checked as a function over random seeds, because there is
no hand review at all on a palette the model invents at runtime: the guarantee
has to hold for every seed or it holds for none.
"""

from __future__ import annotations

import itertools
import random

import pytest

from raven.ppt.services.assets.color import contrast_ratio, ensure_contrast, hex_to_rgb, mix, relative_luminance
from raven.ppt.services.assets.fonts import MEASURED_SAFE_FONTS
from raven.ppt.services.assets.themes import (
    ACCENT_CONTRAST_TARGET,
    DECOR_LANGUAGE_GUIDE,
    DECOR_LANGUAGES,
    DEFAULT_THEME_ID,
    FOREGROUND_CONTRAST_TARGET,
    GRAPHIC_CONTRAST_TARGET,
    MIN_CHART_SERIES,
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

# Every colour token on a Theme, so a field added later has to be named here
# rather than silently escaping the paint checks.
PAINT_FIELDS = (
    "background",
    "background_dark",
    "surface",
    "foreground",
    "muted",
    "accent",
    "accent_soft",
    "foreground_on_dark",
    "muted_on_dark",
    "accent_on_dark",
    "accent_soft_dark",
    "grid",
)


def _random_hex(rng: random.Random) -> str:
    return f"#{rng.randrange(0x1000000):06X}"


def _rgb_distance(left: str, right: str) -> float:
    a, b = hex_to_rgb(left), hex_to_rgb(right)
    return sum((x - y) ** 2 for x, y in zip(a, b, strict=True)) ** 0.5


def test_ten_reviewed_presets_are_present_and_guided() -> None:
    assert len(THEMES) == 10
    assert DEFAULT_THEME_ID in THEMES
    # A palette the guide omits is one the model never learns exists.
    assert set(THEMES) <= set(THEME_GUIDE)
    assert "auto" in THEME_GUIDE
    assert set(THEME_GUIDE) - {"auto"} == set(THEMES)
    for theme_id in THEMES:
        assert theme_id in theme_guide_lines()


@pytest.mark.parametrize("theme_id", sorted(THEMES))
def test_every_preset_page_is_white(theme_id: str) -> None:
    """The reading ground is white on all ten; the tint lives on `surface`.

    This is a recent and deliberate change -- the tinted plane used to be the
    page -- and it is exactly the kind of thing a later palette edit reverts by
    habit, so it is pinned. `surface` still has to be a tint rather than white,
    or cards and structural pages have nothing to separate against.
    """
    theme = THEMES[theme_id]
    assert theme.background == "#FFFFFF"
    assert theme.surface != "#FFFFFF"
    assert relative_luminance(theme.surface) >= 0.75


def test_ink_graphite_keeps_its_reviewed_surface() -> None:
    assert THEMES["ink-graphite"].surface == "#F1F3F6"


@pytest.mark.parametrize("theme_id", sorted(THEMES))
def test_every_preset_is_complete_and_contrast_clean(theme_id: str) -> None:
    theme = THEMES[theme_id]
    assert theme.theme_id == theme_id
    for field in PAINT_FIELDS:
        paint = getattr(theme, field)
        assert paint == paint.upper() and len(paint) == 7 and paint.startswith("#"), f"{field}={paint}"
    assert len(theme.chart_series) >= MIN_CHART_SERIES
    assert len(set(theme.chart_series)) == len(theme.chart_series)
    assert theme.font_family in MEASURED_SAFE_FONTS
    assert theme.title_geometry in TITLE_GEOMETRIES
    assert theme.decor_language in DECOR_LANGUAGES
    assert theme.structural_tone in STRUCTURAL_TONES

    assert contrast_ratio(theme.foreground, theme.background) >= FOREGROUND_CONTRAST_TARGET
    assert contrast_ratio(theme.muted, theme.background) >= ACCENT_CONTRAST_TARGET
    assert contrast_ratio(theme.accent, theme.background) >= ACCENT_CONTRAST_TARGET
    assert contrast_ratio(theme.foreground_on_dark, theme.background_dark) >= FOREGROUND_CONTRAST_TARGET
    assert contrast_ratio(theme.muted_on_dark, theme.background_dark) >= ACCENT_CONTRAST_TARGET
    assert contrast_ratio(theme.accent_on_dark, theme.background_dark) >= ACCENT_CONTRAST_TARGET
    # An icon or a marker drawn in the accent has to read on its own soft tile --
    # which is what this target governs, and the only thing it ever governed.
    assert contrast_ratio(theme.accent, theme.accent_soft) >= GRAPHIC_CONTRAST_TARGET


def test_no_theme_field_carries_a_type_size() -> None:
    """The hard invariant, as an assertion rather than a review habit.

    Font size is never exposed to the model, and the way that guarantee decayed
    in the predecessor was a `ladders` / `floors` pair riding along on the theme
    -- one dataclass field away from the schema. There is no size on a theme at
    all now, and every value on one is a paint or a vocabulary word.
    """
    banned = {
        "size",
        "sizes",
        "ladder",
        "ladders",
        "floor",
        "floors",
        "pt",
        "px",
        "em",
        "ems",
        "scale",
        "leading",
        "tracking",
        "weight",
    }
    for theme in THEMES.values():
        for name, value in vars(theme).items():
            assert not banned & set(name.lower().split("_")), f"{name} smells like a type scale"
            flat = value if isinstance(value, tuple) else (value,)
            for item in flat:
                assert isinstance(item, str), f"{name} carries a number: {item!r}"


def test_design_registries_form_distinct_reviewed_presets() -> None:
    """Ported from the predecessor's derived-theme suite.

    The point is that the ten are ten *designs*, not one design in ten colour
    ways: no two share a (title geometry, decoration) pair, and both structural
    tones are actually exercised.
    """
    presets = [theme for theme_id, theme in THEMES.items() if theme_id != DEFAULT_THEME_ID]
    assert set(TITLE_GEOMETRIES) == {"plain", "rail", "editorial"}
    assert set(DECOR_LANGUAGES) == {"geometric", "editorial", "corner", "grid"}
    assert set(STRUCTURAL_TONES) == {"light", "soft"}
    assert len({(theme.title_geometry, theme.decor_language) for theme in presets}) == len(presets)
    assert {theme.structural_tone for theme in presets} == set(STRUCTURAL_TONES)
    assert set(TITLE_GEOMETRY_GUIDE) == set(TITLE_GEOMETRIES)
    assert set(DECOR_LANGUAGE_GUIDE) == set(DECOR_LANGUAGES)


@pytest.mark.parametrize("theme_id", sorted(THEMES))
def test_structural_pages_clear_contrast_on_whichever_ground_they_use(theme_id: str) -> None:
    theme = THEMES[theme_id]
    paints = theme.structural_paints()
    assert relative_luminance(paints.background) >= 0.75
    assert contrast_ratio(paints.foreground, paints.background) >= FOREGROUND_CONTRAST_TARGET
    assert contrast_ratio(paints.muted, paints.background) >= ACCENT_CONTRAST_TARGET
    assert contrast_ratio(paints.accent, paints.background) >= ACCENT_CONTRAST_TARGET
    expected = theme.surface if theme.structural_tone == "soft" else theme.background
    assert paints.background == expected


def test_series_color_wraps_rather_than_raising() -> None:
    theme = THEMES[DEFAULT_THEME_ID]
    assert theme.series_color(0) == theme.chart_series[0]
    assert theme.series_color(len(theme.chart_series)) == theme.chart_series[0]


def test_title_rail_is_the_only_geometry_that_reserves_one() -> None:
    assert THEMES["sage-clinical"].title_rail
    assert not THEMES["ink-graphite"].title_rail
    assert not THEMES["warm-paper"].title_rail


def test_get_theme_names_the_alternatives_when_it_misses() -> None:
    assert get_theme(DEFAULT_THEME_ID) is THEMES[DEFAULT_THEME_ID]
    with pytest.raises(ThemeError, match="unknown theme.*available"):
        get_theme("neon-vaporwave")


def test_auto_theme_is_a_stable_rotation_over_every_preset() -> None:
    """Hashed, not random: a rebuild of the same deck must not change palette."""
    assert auto_theme_id("Quarterly Review") == auto_theme_id("  Quarterly Review  ")
    assert auto_theme_id("Quarterly Review") in THEMES
    picked = {auto_theme_id(f"deck {index}") for index in range(400)}
    assert picked == set(THEMES)


def test_constructing_a_theme_refuses_vocabulary_it_does_not_know() -> None:
    base = THEMES[DEFAULT_THEME_ID]
    fields = {name: getattr(base, name) for name in vars(base)}
    with pytest.raises(ThemeError, match="title geometry"):
        Theme(**{**fields, "title_geometry": "diagonal"})
    with pytest.raises(ThemeError, match="decoration language"):
        Theme(**{**fields, "decor_language": "confetti"})
    with pytest.raises(ThemeError, match="structural tone"):
        Theme(**{**fields, "structural_tone": "neon"})


def test_derived_theme_contrast_holds_for_any_seed() -> None:
    """Ported: the whole reason derivation is arithmetic and not a lookup."""
    rng = random.Random(20260731)
    for _ in range(120):
        theme = derive_theme("light", _random_hex(rng), _random_hex(rng) if rng.random() < 0.5 else None)
        assert contrast_ratio(theme.foreground, theme.background) >= FOREGROUND_CONTRAST_TARGET
        assert contrast_ratio(theme.accent, theme.background) >= ACCENT_CONTRAST_TARGET
        assert contrast_ratio(theme.muted, theme.background) >= ACCENT_CONTRAST_TARGET
        assert contrast_ratio(theme.foreground_on_dark, theme.background_dark) >= FOREGROUND_CONTRAST_TARGET
        assert contrast_ratio(theme.accent_on_dark, theme.background_dark) >= ACCENT_CONTRAST_TARGET
        assert contrast_ratio(theme.muted_on_dark, theme.background_dark) >= ACCENT_CONTRAST_TARGET
        paints = theme.structural_paints()
        assert contrast_ratio(paints.foreground, paints.background) >= FOREGROUND_CONTRAST_TARGET
        assert contrast_ratio(paints.muted, paints.background) >= ACCENT_CONTRAST_TARGET
        assert contrast_ratio(paints.accent, paints.background) >= ACCENT_CONTRAST_TARGET
        assert len(theme.chart_series) == MIN_CHART_SERIES
        assert len(set(theme.chart_series)) == MIN_CHART_SERIES


def test_derived_chart_marks_stay_distinguishable_from_each_other() -> None:
    """Two marks closer than this in RGB are one mark to a reader."""
    rng = random.Random(4242)
    for _ in range(40):
        series = derive_theme("light", _random_hex(rng), neutral=rng.choice(["cool", "warm", "gray"])).chart_series
        closest = min(_rgb_distance(left, right) for left, right in itertools.combinations(series, 2))
        assert closest >= 0.12


def test_derived_theme_is_deterministic() -> None:
    a = derive_theme("light", "#B85042", "#2C5F2D", "warm")
    b = derive_theme("light", "#b85042", "#2c5f2d", "warm")
    assert a == b
    assert a.theme_id.startswith("custom-")


def test_derived_theme_takes_a_face_that_resolves_on_both_sides() -> None:
    """A seed carries no typographic intent, so it gets the deck's own face.

    Not the measurement font: DejaVu Sans is what fitting measures with, and it is
    absent from the viewer's machine, where the name would resolve to an unknown
    substitute. A deck built on the user's own template never reaches here -- it
    reads its face off the file.
    """
    theme = derive_theme("light", "#B85042")
    assert theme.font_family in MEASURED_SAFE_FONTS
    assert theme.font_family == THEMES[DEFAULT_THEME_ID].font_family


def test_custom_theme_can_select_non_color_design_dimensions() -> None:
    theme = derive_theme(
        "light",
        "#B85042",
        neutral="warm",
        title_geometry="rail",
        decor_language="grid",
        structural_tone="soft",
    )
    assert theme.title_geometry == "rail"
    assert theme.title_rail
    assert theme.decor_language == "grid"
    assert theme.structural_tone == "soft"
    with pytest.raises(ThemeError, match="title geometry"):
        derive_theme("light", "#B85042", title_geometry="diagonal")
    with pytest.raises(ThemeError, match="decor language"):
        derive_theme("light", "#B85042", decor_language="confetti")
    with pytest.raises(ThemeError, match="structural tone"):
        derive_theme("light", "#B85042", structural_tone="neon")


def test_custom_theme_identity_includes_design_dimensions() -> None:
    editorial = derive_theme(
        "light",
        "#B85042",
        title_geometry="editorial",
        decor_language="corner",
        structural_tone="light",
    )
    geometric = derive_theme(
        "light",
        "#B85042",
        title_geometry="rail",
        decor_language="geometric",
        structural_tone="soft",
    )
    assert editorial.theme_id != geometric.theme_id


def test_derived_theme_rejects_bad_inputs() -> None:
    with pytest.raises(ThemeError, match="dark backgrounds are disabled"):
        derive_theme("dark", "#B85042")
    with pytest.raises(ThemeError, match="neutral must be one of"):
        derive_theme("light", "#B85042", neutral="pastel")


def test_ensure_contrast_keeps_the_hue_it_was_given() -> None:
    """It darkens toward a pole rather than clamping: a met ratio that threw the
    palette away would pass every contrast check and ruin every deck."""
    lifted = ensure_contrast("#9CC3E5", "#FFFFFF", 7.0)
    assert contrast_ratio(lifted, "#FFFFFF") >= 7.0
    r, g, b = hex_to_rgb(lifted)
    assert b > r and b > g
    # Already clear: returned untouched rather than pushed further.
    assert ensure_contrast("#111111", "#FFFFFF", 7.0) == "#111111"
    # Unreachable target falls back to the pole instead of looping forever.
    assert ensure_contrast("#808080", "#808080", 21.0) in {"#000000", "#FFFFFF"}


def test_mix_is_a_weighted_blend_between_two_opaque_paints() -> None:
    assert mix("#000000", "#FFFFFF", 0.0) == "#000000"
    assert mix("#000000", "#FFFFFF", 1.0) == "#FFFFFF"
    assert mix("#000000", "#FFFFFF", 0.5) == "#808080"


def test_every_theme_names_a_cjk_companion_matched_to_its_class() -> None:
    """A deck in Chinese set in a Latin face renders Han through the viewer's fallback.

    Serif Latin gets the CJK serif and sans gets the CJK sans, so a Chinese line and
    the Latin line above it belong to the same design.
    """
    from raven.ppt.services.assets.fonts import CJK_SANS, CJK_SERIF, SERIF_FACES

    for theme_id, theme in THEMES.items():
        expected = CJK_SERIF if theme.font_family in SERIF_FACES else CJK_SANS
        assert theme.cjk_family == expected, theme_id


def test_an_unmeasured_cjk_face_is_refused() -> None:
    """微软雅黑 resolves to a face with no Han glyphs on this renderer, so a deck
    naming it would be corrected against review renders showing tofu."""
    import dataclasses

    from raven.ppt.services.assets.themes import _validate_theme

    with pytest.raises(ThemeError, match="unmeasured CJK font"):
        _validate_theme(dataclasses.replace(THEMES["ink-graphite"], cjk_font_family="Microsoft YaHei"))
