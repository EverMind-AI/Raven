"""The plugin's config slice, owned here the way every flow plugin owns its own.

``plugins.config["ppt-engine"]`` carries the fork's ``tools.ppt`` keys in the
product config's camelCase (the w106 scaffold pre-named the slice and mirrors
the fork's five spelled keys; the last two fall back to the fork schema's own
defaults). ``config_schema`` in the manifest stays empty on purpose: admission
with an empty declaration is verbatim pass-through, and this module owns the
slice shape -- the same division oncall-flow, code-flow and research-flow use.

Parsing is strict, and a violation raises rather than mends: the fork said
these shapes with pydantic (wrong types refused loudly at config load), and a
hand parser that coerced -- ``bool("false")`` is True -- or silently fell back
to a default would invert the very knob the user set. The factories catch the
raise and cast the fail-closed sentinel (see ``raven_ppt.plugin``): the host's
stack builder skips a raising factory quietly, and closed-and-loud beats
open-and-quiet (the code-flow MisconfiguredGate doctrine).

One deliberate flip against the fork schema: the fork's ``enabled`` defaulted
True because the fork BUILD was the product -- every checkout of it was a deck
agent. A wheel is installed into any raven environment, so here an absent
slice means "this instance never asked for deck tools" and every factory
declines (the D6 admission shape). The shipped product slice spells
``enabled: true``, so the product face is unchanged.

Plugin-only keys, none of which the shipped product config carries:

  webProxy            forwarded to ppt_fetch and ppt_image_search, the seat the
                      fork's launcher-rendered tools.web.proxy fed
  imageSearch.apiKey  Serper key for ppt_image_search; falls back to
                      $SERPER_API_KEY (the research-flow spelling)
  deckPerSession      true by default: each session builds its deck in
                      <workdir>/decks/<session>/ rather than in <workdir>/deck,
                      so sessions sharing one channel directory do not share a deck
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
    # The fork schema's ranges, kept as refusals rather than silent clamps:
    # zero renders would publish a deck nobody looked at, and the unseen gate
    # reads the same number, so it would refuse forever.
    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low} and {high}, got {value}")
    return value


@dataclass(frozen=True)
class EngineConfig:
    """The slice, read once at activation."""

    enabled: bool = False
    profile: str = "script_author"
    composer_model: str = ""
    render_dpi: int = 144
    render_concurrency: int = 2
    views_per_call: int = 3
    deck_name: str = "deck.pptx"
    web_proxy: str | None = None
    # One deck directory per session under the bound working directory. The host hands
    # every session on a channel the same directory (a web page's channel is one folder),
    # and Project.root fences one deck per directory, so without this a second task in the
    # same channel inherits the first task's template, sources and plan. Measured on a
    # web gateway: three deck requests in one channel, the third built on the first's
    # template with the second's sources. The fork answered the same problem with one
    # engine per session; here the hook repoints the turn's working directory instead.
    deck_per_session: bool = True
    image_search_api_key: str | None = None
    # The second reader's reasoning effort, passed through to the provider as given
    # ("low", "none", ...). Empty means the provider's own default, which is the
    # author's setting -- sized for writing a deck, and on one live run 200 seconds
    # of thinking per page's picture.
    reader_effort: str = ""

    @classmethod
    def from_slice(cls, raw: dict[str, Any] | None) -> "EngineConfig":
        raw = raw or {}
        image_search = raw.get("imageSearch", {})
        if not isinstance(image_search, dict):
            raise ValueError(f"imageSearch must be an object, got {image_search!r}")
        key = image_search.get("apiKey", "")
        if not isinstance(key, str):
            raise ValueError(f"imageSearch.apiKey must be a string, got {key!r}")
        # The fork validated the route name at config load ("a typo in a
        # config file is a startup error naming the alternatives, not a
        # silent fallback" -- fork schema.py:872-880); without this check a
        # typo would ride to assembly's fallback branch and boot
        # script_author with only a process log to say so. The registry is
        # the one list of routes; a raise here walks the sentinel path like
        # every other malformed key (G2).
        from raven_ppt.profiles import registry

        profile = _text(raw, "profile", "script_author")
        if profile not in registry.names():
            raise ValueError(f"unknown ppt profile {profile!r}; known routes are {', '.join(sorted(registry.names()))}")
        return cls(
            enabled=_flag(raw, "enabled", False),
            profile=profile,
            composer_model=_text(raw, "composerModel", ""),
            render_dpi=_count(raw, "renderDpi", 144, 72, 300),
            render_concurrency=_count(raw, "renderConcurrency", 2, 1, 8),
            views_per_call=_count(raw, "viewsPerCall", 3, 1, 12),
            deck_name=_text(raw, "deckName", "deck.pptx") or "deck.pptx",
            web_proxy=_text(raw, "webProxy", "") or None,
            deck_per_session=_flag(raw, "deckPerSession", True),
            image_search_api_key=key or None,
            reader_effort=_text(raw, "readerEffort", ""),
        )
