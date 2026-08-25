"""The routes, by name.

A route used to be a boolean. `freeCompositionEnabled` answered three separate
questions at once -- which stages run, which backend composes the pages, and
what the model may write -- so the assembly function grew ten branches on it,
and two of the branches contradicted each other well enough that a whole
sub-route (1500 lines of designer tooling) could not be reached under any
configuration at all. Nobody noticed, because a test had frozen the
unreachability as the expected behaviour.

Routes are values here, and a value can be enumerated, asserted about, and
listed in a table. `available()` answers "can this route actually run", which is
the question the assembly point should be asking instead of recomputing a
predicate.
"""

from __future__ import annotations

from raven.ppt.contracts import Capabilities, Profile, StageSpec

# Fail-closed kind. A claim about the source material that the file itself
# settles: a page citing one figure while showing another. Everything else warns
# -- see docs/ppt-raven-design.md D2 for why measurements of the rendered page
# must not refuse publication.
_PROVENANCE = frozenset({"citation"})

# What the deck was agreed to be. Refusals rather than warnings on every route: a
# deck of the wrong length or in the wrong language is not a deck the audience
# asked for, and neither is answerable by rearranging a page.
# What the deck and the plan agreed between them, which a built deck either honours
# or does not. `unplaced_figure` joins them because the plan only ever names figures
# the catalogue holds -- `figure` refuses one it does not -- so a page planned with a
# picture is a page that can have one, and shipping it without is the deck quietly
# dropping the evidence it said it would show. Answering it is one line either way:
# place it, or plan again without it.
_AGREED = frozenset({"page_budget", "language", "unplaced_figure"})

SCRIPT_AUTHOR = Profile(
    name="script_author",
    backend="script",
    stages=(
        # One stage for the whole front of the route, because once the task has
        # been read every step it implies is mechanical: where the materials are,
        # whether there is a template, which of the brief the request already
        # states. Splitting it into four asks left the sequencing to whoever was
        # reading four tool descriptions, which is the arrangement the design pass
        # below already argued against.
        StageSpec(name="prepare", tool="ppt_prepare"),
        # The primitives `prepare` drives, still registered on their own: it is
        # worth being able to ingest one more paper, bind a template or fetch a URL
        # without reading the task again. Optional because a prepared project has
        # had them run already, and required stages are what `available()` checks a
        # route against.
        StageSpec(name="brief", tool="ppt_brief", required=False),
        StageSpec(name="template", tool="ppt_template", required=False),
        StageSpec(name="gather", tool="ppt_fetch", required=False),
        StageSpec(name="generate", tool="ppt_generate_image", required=False),
        StageSpec(name="ingest", tool="ppt_ingest", required=False),
        StageSpec(name="inspect", tool="ppt_figure_inspect", required=False),
        # What the deck argues, page by page, before any of it is drawn. The route
        # went without one and the decks showed it: what a page said was decided
        # while its geometry was being typed, so they came out thin -- a title and
        # three short lines, with nothing having asked what the audience must
        # believe by the end. It is also where gathering belongs, because this is
        # the first moment anything knows which picture is missing.
        StageSpec(name="plan", tool="ppt_outline"),
        StageSpec(name="build", tool="ppt_build"),
        # No tool: asked for in prose it did not happen. By the time an
        # instruction says "now review the layout", the author has been looking
        # at these pages while getting them to build, so the review it would run
        # is the one it just ran, with the intent it already holds. It runs from
        # code, on an empty context.
        StageSpec(name="design_pass", tool=None),
        StageSpec(name="publish", tool=None),
    ),
    # The one route that hands the model a whole program. It has no ceiling and
    # no house style but the one its author writes, which is the point: the
    # schema route that preceded it bounded the design at what the schema could
    # say and filled in the rest itself, so seventeen different compositions
    # still read as one template.
    capabilities=Capabilities(raw_script=True),
    blocking_kinds=_PROVENANCE | _AGREED | {"band", "unmapped_page", "house_style"},
    skill="ppt-script-authoring",
)

SLOT_AUTHOR = Profile(
    name="slot_author",
    backend="slots",
    stages=(
        StageSpec(name="brief", tool="ppt_brief"),
        StageSpec(name="ingest", tool="ppt_ingest"),
        StageSpec(name="inspect", tool="ppt_figure_inspect"),
        StageSpec(name="plan", tool="ppt_outline"),
        StageSpec(name="fill", tool="ppt_fill"),
        StageSpec(name="compile", tool="ppt_compile"),
        StageSpec(name="review", tool="ppt_review"),
        StageSpec(name="publish", tool=None),
    ),
    # Semantic regions on a 0..1000 grid, which is composition intent rather
    # than finished geometry: the engine still owns the physical mapping, the
    # safe-area inset, the type scale, text fitting and the overlap check.
    capabilities=Capabilities(normalized_regions=True),
    blocking_kinds=_PROVENANCE | _AGREED | {"band", "overlap", "outside_safe_area"},
    skill="ppt-slot-authoring",
)

IMAGE_TEXT = Profile(
    name="image_text",
    backend="imagetext",
    stages=(
        StageSpec(name="brief", tool="ppt_brief"),
        StageSpec(name="ingest", tool="ppt_ingest"),
        StageSpec(name="plan", tool="ppt_outline"),
        # Two stages, not one, and the split is the whole design. The
        # background stage writes an image prompt *and* declares the regions it
        # is leaving clear; the text stage may only write into those regions.
        # A single stage that generated an image with words baked into it would
        # produce a page nobody can edit, retranslate or fix a typo in -- and
        # the words would be a raster no measurement here can read, so every
        # check on the copy would pass by having nothing to look at.
        StageSpec(name="background", tool="ppt_background"),
        StageSpec(name="place_text", tool="ppt_place_text"),
        StageSpec(name="build", tool=None),
        StageSpec(name="design_pass", tool=None),
        StageSpec(name="publish", tool=None),
    ),
    capabilities=Capabilities(background_prompt=True, normalized_regions=True),
    # `text_over_art` is this route's own fail-closed condition: type that has
    # landed on a busy part of the generated image is unreadable, and unlike a
    # collision it cannot be fixed by the reader squinting.
    blocking_kinds=_PROVENANCE | _AGREED | {"text_in_background", "text_over_art"},
    skill="ppt-image-text-authoring",
)

_PROFILES: dict[str, Profile] = {p.name: p for p in (SCRIPT_AUTHOR, SLOT_AUTHOR, IMAGE_TEXT)}

DEFAULT = SCRIPT_AUTHOR.name


def names() -> tuple[str, ...]:
    return tuple(_PROFILES)


def get(name: str) -> Profile:
    try:
        return _PROFILES[name]
    except KeyError:
        raise ValueError(f"no such deck route: {name!r}. Registered: {', '.join(sorted(_PROFILES))}") from None


def available(name: str, registered_tools: set[str]) -> tuple[bool, tuple[str, ...]]:
    """Whether a route can run here, and which of its tools are missing.

    Asked at assembly rather than recomputed from flags. The predecessor
    recomputed it -- `expert_outline and not free_composition`, where
    `expert_outline` already implied `free_composition` -- and the route it
    described silently ceased to exist.
    """
    stages = get(name).stages
    absent = tuple(s.tool for s in stages if s.tool and s.tool not in registered_tools)
    blocking = tuple(s.tool for s in stages if s.required and s.tool and s.tool not in registered_tools)
    # Every absent tool is reported and only a required one makes the route
    # unrunnable. Reporting only the required ones hid a stage of the default route
    # whose tool did not exist anywhere in the codebase: `available()` answered
    # "(True, ())" and the startup warning never fired, which is the same silence
    # the docstring above congratulates itself on having fixed.
    return (not blocking, absent)


def profile_stages(profile: Profile) -> tuple[StageSpec, ...]:
    return tuple(s for s in profile.stages if s.required)
