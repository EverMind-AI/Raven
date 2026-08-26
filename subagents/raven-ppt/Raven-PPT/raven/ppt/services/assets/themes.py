"""Ten reviewed palettes, and the arithmetic to derive an eleventh.

A theme is the one deck-wide token set every page of a deck draws from, and it
exists so a deck is structurally unable to mix styles. What it carries is
colour, a font family, and three non-colour design choices (how the title sits,
what the decoration language is, how a structural page differs from a content
page). What it deliberately does not carry is a single number of points or
pixels: type size is measured against the page it lands on, not chosen with the
palette, and it is never a field a model can write.

Two ground rules the presets all obey, both learned from decks that read as
generated:

- Every page is white. The tinted plane a theme used to put behind the whole
  slide moved to ``surface``, where it separates cards and structural pages
  while the reading ground stays white. A deck whose every page carries a wash
  announces itself before a word is read.
- One accent, used for meaning -- markers, highlights, key numbers -- never for
  decorative bars or stripes. Cards separate through the ``surface`` tint plus
  a radius, never an edge bar.

``accent_soft`` and ``accent_soft_dark`` are pre-mixed low-contrast fills for
oversized background numerals and shapes. They are solid rather than a translucent
accent so that the fill is the same colour wherever it is used; the export does
carry alpha, and a drawing that wants a translucent layer asks `ppt_charts` for one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import NamedTuple

from raven.ppt.services.assets.color import (
    WHITE,
    contrast_ratio,
    ensure_contrast,
    hue,
    hue_distance,
    is_canonical_hex,
    mix,
)
from raven.ppt.services.assets.fonts import CJK_SAFE_FONTS, MEASURED_SAFE_FONTS, SERIF_FACES, cjk_face


class ThemeError(ValueError):
    """A theme names something outside the reviewed vocabulary."""


@dataclass(frozen=True)
class TitleGeometrySpec:
    geometry_id: str
    description: str
    rail: bool = False


TITLE_GEOMETRIES: dict[str, TitleGeometrySpec] = {
    "plain": TitleGeometrySpec("plain", "open title with no title furniture"),
    "rail": TitleGeometrySpec("rail", "left title rail with body beside it", rail=True),
    "editorial": TitleGeometrySpec("editorial", "offset editorial title with a compact body start"),
}


@dataclass(frozen=True)
class DecorLanguageSpec:
    language_id: str
    description: str


DECOR_LANGUAGES: dict[str, DecorLanguageSpec] = {
    "geometric": DecorLanguageSpec("geometric", "large circles, soft numerals, and rounded cards"),
    "editorial": DecorLanguageSpec("editorial", "quiet panels, rules, and asymmetric corner blocks"),
    "corner": DecorLanguageSpec("corner", "corner brackets, framed arcs, and compact markers"),
    "grid": DecorLanguageSpec("grid", "technical grid, ticks, and modular markers"),
}

STRUCTURAL_TONES = ("light", "soft")

MIN_CHART_SERIES = 6


class StructuralPaints(NamedTuple):
    """What a section divider or a cover paints with.

    A structural page is the same palette read differently, not a second
    palette: a ``light`` theme leaves it on the white ground, a ``soft`` theme
    lifts it onto ``surface`` and re-clears every text contrast against that
    ground rather than assuming the white one still applies.
    """

    background: str
    foreground: str
    muted: str
    accent: str
    accent_soft: str


@dataclass(frozen=True)
class Theme:
    """One deck-wide token set; every slide of a deck uses exactly one.

    The ``*_on_dark`` tokens are for the minority of surfaces that are not the
    page -- a dark cover panel, an inverted callout -- and they are part of the
    reviewed set rather than computed at use time so their contrast was checked
    once, here, against ``background_dark``.
    """

    theme_id: str
    background: str
    background_dark: str
    surface: str
    foreground: str
    muted: str
    accent: str
    accent_soft: str
    foreground_on_dark: str
    muted_on_dark: str
    accent_on_dark: str
    accent_soft_dark: str
    grid: str
    chart_series: tuple[str, ...]
    font_family: str
    # The CJK companion. A deck in Chinese set in a Latin face renders its Han
    # through whatever the viewer falls back to -- a different design, often a
    # different weight, and on this renderer no glyphs at all. Naming both faces
    # means "TarViS" stays in the Latin face and 目标查询 is set on purpose.
    cjk_font_family: str = ""
    title_geometry: str = "plain"
    decor_language: str = "geometric"
    structural_tone: str = "light"

    @property
    def cjk_family(self) -> str:
        """The CJK face to name beside `font_family`, matching its class."""
        return self.cjk_font_family or cjk_face(serif=self.font_family in SERIF_FACES)

    def __post_init__(self) -> None:
        # Vocabulary is checked here rather than only on the registry so a theme
        # built by hand -- a test, a future route -- fails at the typo instead of
        # at the first property access that looks it up.
        if self.title_geometry not in TITLE_GEOMETRIES:
            raise ThemeError(f"unknown title geometry: {self.title_geometry!r}")
        if self.decor_language not in DECOR_LANGUAGES:
            raise ThemeError(f"unknown decoration language: {self.decor_language!r}")
        if self.structural_tone not in STRUCTURAL_TONES:
            raise ThemeError(f"unknown structural tone: {self.structural_tone!r}")

    @property
    def title_geometry_spec(self) -> TitleGeometrySpec:
        return TITLE_GEOMETRIES[self.title_geometry]

    @property
    def title_rail(self) -> bool:
        return self.title_geometry_spec.rail

    def series_color(self, index: int) -> str:
        """Chart series colour, wrapping so an extra series cannot crash a page."""
        return self.chart_series[index % len(self.chart_series)]

    def structural_paints(self) -> StructuralPaints:
        if self.structural_tone == "soft":
            return StructuralPaints(
                self.surface,
                ensure_contrast(self.foreground, self.surface, FOREGROUND_CONTRAST_TARGET),
                ensure_contrast(self.muted, self.surface, ACCENT_CONTRAST_TARGET),
                ensure_contrast(self.accent, self.surface, ACCENT_CONTRAST_TARGET),
                self.accent_soft,
            )
        return StructuralPaints(self.background, self.foreground, self.muted, self.accent, self.accent_soft)


# Body-text contrast targets (WCAG): foreground aims AAA, accent only has to
# hold AA because the copy it colours -- kickers, trackers, key numbers -- is
# set larger than body text.
FOREGROUND_CONTRAST_TARGET = 7.0
ACCENT_CONTRAST_TARGET = 4.5

# 3:1 is WCAG's target for a graphic rather than for text, and what it governs here
# is an accent mark on its own soft tile -- an icon, a bullet, a rule. It was named
# for chart marks and claimed to apply to the data series, which nothing checked and
# no preset meets: every one of the ten falls to about 1.5:1 by its third series.
# Six colours that all clear 3:1 on white *and* hold apart from each other is not a
# palette anyone ships, which is why real ones order their series by prominence and
# let the tail go quiet. That ordering is the series contract instead --
# `chart_series[0]` is the series a page is about -- and it belongs in the authoring
# guidance rather than in a threshold.
GRAPHIC_CONTRAST_TARGET = 3.0


def _validate_theme(theme: Theme) -> Theme:
    """The checks a reviewed or derived theme has to pass before anything draws.

    Construction already refused an unknown geometry, decor or tone; what is
    left is the part that only matters once the tokens are real paints: every
    colour canonical, the face measurable, and enough series for a chart.
    """
    if theme.cjk_font_family and theme.cjk_font_family not in CJK_SAFE_FONTS:
        raise ThemeError(
            f"theme {theme.theme_id!r} names unmeasured CJK font {theme.cjk_font_family!r}; "
            f"pick one of {sorted(CJK_SAFE_FONTS)} -- a name that resolves on only one side of the "
            "render makes every review render lie"
        )
    if theme.font_family not in MEASURED_SAFE_FONTS:
        raise ThemeError(
            f"theme {theme.theme_id!r} names unmeasured font {theme.font_family!r}; "
            f"pick one of {sorted(MEASURED_SAFE_FONTS)} or measure the new face against "
            "the fitting baseline first"
        )
    paints = (
        theme.background,
        theme.background_dark,
        theme.surface,
        theme.foreground,
        theme.muted,
        theme.accent,
        theme.accent_soft,
        theme.foreground_on_dark,
        theme.muted_on_dark,
        theme.accent_on_dark,
        theme.accent_soft_dark,
        theme.grid,
        *theme.chart_series,
    )
    for paint in paints:
        if not is_canonical_hex(paint):
            raise ThemeError(f"theme {theme.theme_id!r} paint {paint!r} must be uppercase #RRGGBB")
    if len(theme.chart_series) < MIN_CHART_SERIES:
        raise ThemeError(f"theme {theme.theme_id!r} needs >={MIN_CHART_SERIES} chart series colors")
    return theme


# Palette sources: the anthropic pptx-skill "Color Palettes" table, plus
# Tailwind v3 / IBM Carbon / metropolis / Okabe-Ito values from the
# design-research token sheet (all text-role contrasts WCAG-AA verified there;
# chart orders are the CVD-validated sequences). ``accent`` has to stay >= 4.5:1
# on ``background`` because trackers and kickers set regular text in it.
# ``accent_soft*`` are pre-mixed tints against their respective backgrounds.
THEMES: dict[str, Theme] = {
    theme.theme_id: _validate_theme(theme)
    for theme in (
        Theme(
            theme_id="warm-paper",
            background="#FFFFFF",
            background_dark="#4A3B30",
            surface="#F3ECE7",
            foreground="#2B2724",
            muted="#756A5F",
            accent="#8A5A38",
            accent_soft="#EDE1D5",
            foreground_on_dark="#FFFFFF",
            muted_on_dark="#D6D2CC",
            accent_on_dark="#EDE1D5",
            accent_soft_dark="#4A3B30",
            grid="#DFD5C9",
            chart_series=("#4B311E", "#916444", "#CAB5A5", "#565048", "#9A948C", "#D8D5D2"),
            font_family="Cambria",
            title_geometry="editorial",
            decor_language="corner",
            structural_tone="soft",
        ),
        Theme(
            theme_id="ink-graphite",
            background="#FFFFFF",
            background_dark="#1A1D21",
            surface="#F1F3F6",
            foreground="#1A1D21",
            muted="#5C636B",
            accent="#0B5FA5",
            accent_soft="#DCE8F4",
            foreground_on_dark="#FFFFFF",
            muted_on_dark="#D6D2CC",
            accent_on_dark="#DCE8F4",
            accent_soft_dark="#1A1D21",
            grid="#E3E5E8",
            chart_series=("#063760", "#0B5FA5", "#8AB2D4", "#40454B", "#93989D", "#D1D3D6"),
            font_family="Arial",
            title_geometry="plain",
            decor_language="grid",
            structural_tone="light",
        ),
        Theme(
            theme_id="archive-sepia",
            background="#FFFFFF",
            background_dark="#3B2E22",
            surface="#F5EFE5",
            foreground="#33291E",
            muted="#7C6A56",
            accent="#8C5A2B",
            accent_soft="#EFE2D2",
            foreground_on_dark="#FFFFFF",
            muted_on_dark="#D6D2CC",
            accent_on_dark="#EFE2D2",
            accent_soft_dark="#3B2E22",
            grid="#E4DACB",
            chart_series=("#4C3118", "#8C5A2B", "#CBB59F", "#615548", "#9F9486", "#DAD5D0"),
            font_family="Century Schoolbook",
            title_geometry="plain",
            decor_language="corner",
            structural_tone="soft",
        ),
        Theme(
            theme_id="sage-clinical",
            background="#FFFFFF",
            background_dark="#22352B",
            surface="#EBEFEB",
            foreground="#1F2D24",
            muted="#5F6F64",
            accent="#2F6B52",
            accent_soft="#DDEAE1",
            foreground_on_dark="#FFFFFF",
            muted_on_dark="#D6D2CC",
            accent_on_dark="#DDEAE1",
            accent_soft_dark="#22352B",
            grid="#DDE6DE",
            chart_series=("#193A2D", "#477C66", "#A1BCB1", "#464F48", "#8C9690", "#D2D7D4"),
            font_family="Helvetica",
            title_geometry="rail",
            decor_language="corner",
            structural_tone="light",
        ),
        Theme(
            theme_id="plum-editorial",
            background="#FFFFFF",
            background_dark="#33253A",
            surface="#F1E9F1",
            foreground="#2A2130",
            muted="#6E6274",
            accent="#7A3E6B",
            accent_soft="#EBDDE9",
            foreground_on_dark="#FFFFFF",
            muted_on_dark="#D6D2CC",
            accent_on_dark="#EBDDE9",
            accent_soft_dark="#33253A",
            grid="#E6DEE7",
            chart_series=("#43223A", "#824A74", "#C3A8BC", "#534B56", "#958E99", "#D6D3D8"),
            font_family="Bookman Old Style",
            title_geometry="editorial",
            decor_language="grid",
            structural_tone="soft",
        ),
        Theme(
            theme_id="steel-engineering",
            background="#FFFFFF",
            background_dark="#1B3247",
            surface="#E7EDF3",
            foreground="#14202B",
            muted="#556575",
            accent="#2A5D8C",
            accent_soft="#DBE6F0",
            foreground_on_dark="#FFFFFF",
            muted_on_dark="#D6D2CC",
            accent_on_dark="#DBE6F0",
            accent_soft_dark="#1B3247",
            grid="#DFE6ED",
            chart_series=("#17334C", "#2A5D8C", "#9FB6CB", "#48525C", "#86909A", "#CFD4D8"),
            font_family="Arial",
            title_geometry="plain",
            decor_language="geometric",
            structural_tone="light",
        ),
        Theme(
            theme_id="terracotta-craft",
            background="#FFFFFF",
            background_dark="#4A2A1F",
            surface="#F6EDE4",
            foreground="#2E241E",
            muted="#7B6960",
            accent="#A8503A",
            accent_soft="#F3DFD6",
            foreground_on_dark="#FFFFFF",
            muted_on_dark="#D6D2CC",
            accent_on_dark="#F3DFD6",
            accent_soft_dark="#4A2A1F",
            grid="#E8DBD2",
            chart_series=("#5B2B20", "#A8503A", "#D8B0A6", "#60554E", "#9E938D", "#DAD5D2"),
            font_family="Cambria",
            title_geometry="plain",
            decor_language="editorial",
            structural_tone="soft",
        ),
        Theme(
            theme_id="indigo-scholar",
            background="#FFFFFF",
            background_dark="#252A5E",
            surface="#E7E7F3",
            foreground="#1D1F35",
            muted="#5E6178",
            accent="#3A3F8F",
            accent_soft="#DEE0F2",
            foreground_on_dark="#FFFFFF",
            muted_on_dark="#D6D2CC",
            accent_on_dark="#DEE0F2",
            accent_soft_dark="#252A5E",
            grid="#E1E2EC",
            chart_series=("#20234E", "#3A3F8F", "#A6A9CC", "#4D4F5E", "#8C8E9C", "#D2D3D9"),
            font_family="Times New Roman",
            title_geometry="rail",
            decor_language="grid",
            structural_tone="light",
        ),
        Theme(
            theme_id="moss-field",
            background="#FFFFFF",
            background_dark="#33421C",
            surface="#EEF2E8",
            foreground="#242B1E",
            muted="#66705A",
            accent="#4F6B2A",
            accent_soft="#E3EAD5",
            foreground_on_dark="#FFFFFF",
            muted_on_dark="#D6D2CC",
            accent_on_dark="#E3EAD5",
            accent_soft_dark="#33421C",
            grid="#E0E6D8",
            chart_series=("#2B3A17", "#647C43", "#B0BC9F", "#494F43", "#919789", "#D4D7D1"),
            font_family="Helvetica",
            title_geometry="rail",
            decor_language="geometric",
            structural_tone="soft",
        ),
        Theme(
            theme_id="oxblood-press",
            background="#FFFFFF",
            background_dark="#4A1F1F",
            surface="#F4E9E6",
            foreground="#2C1E1E",
            muted="#75625F",
            accent="#8C3A3A",
            accent_soft="#F0DCDA",
            foreground_on_dark="#FFFFFF",
            muted_on_dark="#D6D2CC",
            accent_on_dark="#F0DCDA",
            accent_soft_dark="#4A1F1F",
            grid="#E7DAD8",
            chart_series=("#4C2020", "#8C3A3A", "#CBA6A6", "#5C504D", "#9A8E8C", "#D8D3D2"),
            font_family="Century Schoolbook",
            title_geometry="editorial",
            decor_language="geometric",
            structural_tone="light",
        ),
    )
}

DEFAULT_THEME_ID = "ink-graphite"

# Theme-picking guidance surfaced to whoever chooses one. Every preset must
# appear here: a palette the guide omits is one the model never learns exists,
# so it picks by name from whatever the guide does list.
THEME_GUIDE: dict[str, str] = {
    "auto": "stable title-based rotation across curated palette, title, and decoration presets",
    "warm-paper": "white page, warm sand planes, walnut accent; editorial title + corner decor, soft structure",
    "ink-graphite": "white page, cool grey planes, near-black ink, cobalt accent; plain title + grid decor, light structure",
    "archive-sepia": "white page, cream archival planes, sepia accent; plain title + corner decor, soft structure",
    "sage-clinical": "white page, pale green planes, pine accent; rail title + corner decor, light structure",
    "plum-editorial": "white page, mauve planes, plum accent; editorial title + grid decor, soft structure",
    "steel-engineering": "white page, cool blue-grey planes, steel-blue accent; plain title + geometric decor, light structure",
    "terracotta-craft": "white page, warm clay planes, terracotta accent; plain title + editorial decor, soft structure",
    "indigo-scholar": "white page, pale indigo planes, indigo accent; title rail + grid decor, light structure",
    "moss-field": "white page, pale green-grey planes, moss accent; rail title + geometric decor, soft structure",
    "oxblood-press": "white page, warm blush planes, oxblood accent; editorial title + geometric decor, light structure",
}

_unguided = sorted(set(THEMES) - set(THEME_GUIDE))
if _unguided:
    raise ThemeError(f"themes missing from THEME_GUIDE (the model would never see them): {_unguided}")

TITLE_GEOMETRY_GUIDE = {key: spec.description for key, spec in TITLE_GEOMETRIES.items()}
DECOR_LANGUAGE_GUIDE = {key: spec.description for key, spec in DECOR_LANGUAGES.items()}

_AUTO_THEME_IDS = tuple(THEMES)


def get_theme(theme_id: str) -> Theme:
    try:
        return THEMES[theme_id]
    except KeyError as exc:
        raise ThemeError(f"unknown theme: {theme_id!r} (available: {sorted(THEMES)})") from exc


def auto_theme_id(title: str) -> str:
    """Pick a preset from the deck title, stably.

    Hashed rather than random so a rebuild of the same deck gets the same
    palette: a theme that moves between builds makes every re-render a visual
    diff and defeats the review loop.
    """
    digest = hashlib.sha256(title.strip().encode()).digest()
    return _AUTO_THEME_IDS[int.from_bytes(digest[:4], "big") % len(_AUTO_THEME_IDS)]


def theme_guide_lines(theme_ids: list[str] | None = None) -> str:
    """One line per theme, for whoever has to choose without reading the code."""
    ids = sorted(theme_ids if theme_ids is not None else THEMES)
    return "\n".join(f"- {theme_id}: {THEME_GUIDE[theme_id]}" for theme_id in ids)


# ---------------------------------------------------------------------------
# Derived themes. The model picks a topic-informed seed (a base and a primary
# colour); the engine derives the full token set and *computes* the safety the
# presets got from manual review. The model makes the creative call, the
# arithmetic it always gets wrong -- contrast, tint mixing, chart separation --
# stays deterministic engine work.
# ---------------------------------------------------------------------------

_NEUTRAL_MUTED = {"cool": "#475569", "warm": "#57534E", "gray": "#525252"}
_NEUTRAL_GRID = {"cool": "#E2E8F0", "warm": "#E7E5E4", "gray": "#E5E5E5"}
# The neutral choice has to reach the page ground and the body ink too. Muted
# and grid were already picked per family (slate / stone / neutral) while these
# two stayed pinned to slate and pure white, so a "warm" deck read as warm in
# its secondary text and cold in its body copy -- and never warm at all in the
# field behind them. Same families, same source (Tailwind), one step off pure
# white so the page has paper in it rather than glare.
_NEUTRAL_FOREGROUND = {"cool": "#0F172A", "warm": "#1C1917", "gray": "#171717"}
_NEUTRAL_BACKGROUND = {"cool": "#FFFFFF", "warm": "#F5F5F4", "gray": "#FAFAFA"}

# CVD-validated pools from the design-research sheet; a derived series starts
# with the theme accent and greedily maximizes hue separation from there.
_CHART_POOL_LIGHT = ("#0891B2", "#E11D48", "#4F46E5", "#D97706", "#7C3AED", "#059669", "#0072B2", "#E69F00")

NEUTRAL_FAMILIES = tuple(_NEUTRAL_MUTED)


def _choose_design_dimension(
    value: str | None, registry: dict[str, object] | tuple[str, ...], seed: str, salt: str
) -> str:
    if value in (None, "auto"):
        digest = hashlib.sha256(f"{seed}|{salt}".encode()).digest()
        keys = tuple(registry)
        return keys[int.from_bytes(digest[:4], "big") % len(keys)]
    if value not in registry:
        raise ThemeError(f"unknown {salt.replace('_', ' ')}: {value!r} (available: {sorted(registry)})")
    return value


def _derive_chart_series(lead: str, pool: tuple[str, ...]) -> tuple[str, ...]:
    series = [lead]
    candidates = [color for color in pool if color.upper() != lead.upper()]
    while len(series) < MIN_CHART_SERIES and candidates:
        best = max(candidates, key=lambda color: min(hue_distance(hue(color), hue(picked)) for picked in series))
        series.append(best)
        candidates.remove(best)
    return tuple(series[:MIN_CHART_SERIES])


def derive_theme(
    base: str,
    primary: str,
    accent: str | None = None,
    neutral: str = "cool",
    title_geometry: str | None = "auto",
    decor_language: str | None = "auto",
    structural_tone: str | None = "auto",
) -> Theme:
    """Derive a full validated Theme from a seed.

    Deterministic: the same seed always yields the same tokens, so a rebuild
    produces the same fingerprints and a re-render is comparable to the last one.
    """
    primary = primary.upper()
    accent = (accent or primary).upper()
    if base != "light":
        raise ThemeError(f"theme base must be 'light'; dark backgrounds are disabled, got {base!r}")
    if neutral not in _NEUTRAL_MUTED:
        raise ThemeError(f"neutral must be one of {sorted(_NEUTRAL_MUTED)}, got {neutral!r}")
    seed = f"{base}|{primary}|{accent}|{neutral}"
    selected_geometry = _choose_design_dimension(title_geometry, TITLE_GEOMETRIES, seed, "title_geometry")
    selected_decor = _choose_design_dimension(decor_language, DECOR_LANGUAGES, seed, "decor_language")
    selected_tone = _choose_design_dimension(structural_tone, STRUCTURAL_TONES, seed, "structural_tone")
    identity = f"{seed}|{selected_geometry}|{selected_decor}|{selected_tone}"
    theme_id = f"custom-{hashlib.sha256(identity.encode()).hexdigest()[:8]}"

    background = _NEUTRAL_BACKGROUND[neutral]
    accent_text = ensure_contrast(accent, background, ACCENT_CONTRAST_TARGET)
    background_dark = ensure_contrast(primary, WHITE, FOREGROUND_CONTRAST_TARGET)
    theme = Theme(
        theme_id=theme_id,
        background=background,
        background_dark=background_dark,
        surface=mix(primary, WHITE, 0.92),
        foreground=_NEUTRAL_FOREGROUND[neutral],
        muted=_NEUTRAL_MUTED[neutral],
        accent=accent_text,
        accent_soft=mix(accent, WHITE, 0.82),
        foreground_on_dark=WHITE,
        muted_on_dark=ensure_contrast(mix(WHITE, background_dark, 0.26), background_dark, ACCENT_CONTRAST_TARGET),
        accent_on_dark=ensure_contrast(mix(accent, WHITE, 0.45), background_dark, ACCENT_CONTRAST_TARGET),
        accent_soft_dark=mix(WHITE, background_dark, 0.90),
        grid=_NEUTRAL_GRID[neutral],
        chart_series=_derive_chart_series(accent_text, _CHART_POOL_LIGHT),
        # A derived theme carries a palette the model seeded but no typographic
        # intent, so it takes the neutral grotesque rather than the measurement
        # font: DejaVu Sans is what fitting measures with, but it is absent from
        # the viewer's machine, where the name would resolve to an unknown
        # substitute. Arial resolves to a metric-compatible face on both sides.
        font_family="Arial",
        title_geometry=selected_geometry,
        decor_language=selected_decor,
        structural_tone=selected_tone,
    )

    _validate_theme(theme)
    for label, ink, ground, target in (
        ("foreground/background", theme.foreground, theme.background, FOREGROUND_CONTRAST_TARGET),
        ("accent/background", theme.accent, theme.background, ACCENT_CONTRAST_TARGET),
        (
            "foreground_on_dark/background_dark",
            theme.foreground_on_dark,
            theme.background_dark,
            FOREGROUND_CONTRAST_TARGET,
        ),
        ("accent_on_dark/background_dark", theme.accent_on_dark, theme.background_dark, ACCENT_CONTRAST_TARGET),
        ("muted/background", theme.muted, theme.background, ACCENT_CONTRAST_TARGET),
        ("muted_on_dark/background_dark", theme.muted_on_dark, theme.background_dark, ACCENT_CONTRAST_TARGET),
    ):
        ratio = contrast_ratio(ink, ground)
        if ratio < target - 1e-6:
            raise ThemeError(f"derived theme {theme_id} breaks contrast: {label} = {ratio:.2f} < {target}")
    return theme
