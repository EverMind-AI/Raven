"""`ppt_template`: build inside the template the user gave us.

What a template is for turned out to be two different things, and treating them as
one produced the decks that got sent back.

A template's **structural pages** -- its cover, its contents list, its section
divider, its closing -- are the pages a reader recognises the house by, and they
are cloned. A deck that draws its own cover announces itself as not the user's
before a word of it is read.

Its **content pages** are examples, not immutable forms. A review deck may clone the
nearest one, replace its words and pictures, delete unused repeated units, and move
or resize the surviving regions when the actual content needs it. A six-card grid
filled with four must remove the two spare units; a portrait picture frame receiving
a landscape figure must be replaced, reshaped, or abandoned. If the page cannot
carry the argument after those edits, the author composes inside the measured house
style instead. The template supplies continuity; the content decides the final
composition.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import is_dataclass, replace
from pathlib import Path
from typing import Any

from raven.contracts.tool import Tool, ToolResult
from raven.utils.images import image_block, text_block
from raven_ppt.contracts import Project
from raven_ppt.services.template import (
    PaletteError,
    as_palette,
    bind,
    bound,
    decompile,
    write_palette,
)
from raven_ppt.services.template.house import house_style
from raven_ppt.services.template.inventory import template_dir, write_ground
from raven_ppt.services.template.menu import menu, roles
from raven_ppt.services.template.theme import ground_of
from raven_ppt.tools import _return
from raven_ppt.tools._args import ArgumentError, as_ints

# Every example page comes back in one reply. A cap was the first answer and it
# was indefensible: the templates measured ship thirteen pages, the cap was twelve,
# and the thirteenth was simply never shown -- an author choosing a page to adapt
# could not choose the one it could not see.
#
# These renders answer "which page is nearest what I have to say", not "is this
# type legible in a room", so they are cheap: at 96 dpi a page is a quarter of the
# bytes it is at 144.
BATCH_PAGES = 8
RENDER_DPI = 96

# How many pages come back as source in one call. Bounded because a decompiled
# page runs to fifty lines and this is read, not scrolled -- and the count is
# stated in the reply rather than silently applied.
MAX_SOURCE_PAGES = 6


class PptTemplateTool(Tool):
    name = "ppt_template"
    description = (
        "Build this deck inside a .pptx template. Call it with the file's path to bind it: the deck is "
        "then built in a copy of that file, so the template's master, theme, layouts, fonts and canvas "
        "are this deck's house style. Called with just the project it returns every visible example page "
        "with its role and capacity, a render of each, the measured house style -- title row, body area, "
        "type ladder -- and a palette derived from the file. Called with `pages` it returns those "
        "example pages as python-pptx source to adapt. Called with `palette` it keeps the colours you "
        "read off the renders as this deck's own, for every page: what a template declares and what its "
        "pages paint are not the same colours, and only the renders show the second."
    )
    timeout_seconds = 300.0

    def __init__(
        self,
        workspace: Path,
        views: Any,
        provision: Callable[[Project], Path] | None = None,
    ) -> None:
        self.workspace = workspace
        self.views = views
        self.thumbnails = replace(views, dpi=RENDER_DPI) if is_dataclass(views) else views
        self.provision = provision

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "description": "the deck project, as given to ppt_prepare"},
                "path": {
                    "type": "string",
                    "description": (
                        "path to the user's .pptx, relative to the workspace. Give it once to bind the "
                        "template; leave it out afterwards, the deck stays bound to it"
                    ),
                },
                "palette": {
                    "type": "object",
                    "additionalProperties": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ]
                    },
                    "description": (
                        "the colours you read off the renders above, as role: #RRGGBB. These become this "
                        "deck's theme for every page, so state them once here rather than per page. "
                        "background, foreground, accent, surface, muted, accent_soft, accent_ink and grid "
                        "replace what would have been derived; chart_series is a list in the order the "
                        "charts read it; any other name you give is a colour this deck's pages can then "
                        "reach as a tint. State the roles you are sure of -- the rest follow from those. "
                        "What a template declares and what its pages paint are not the same colours, so "
                        "this reading is the renders' and not the file's"
                    ),
                },
                "pages": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "maxItems": MAX_SOURCE_PAGES,
                    "description": (
                        "which example pages to read as python-pptx source, by the numbers the reply names. "
                        "The source comes back with the page's pictures written into the build directory, so "
                        "an add_picture line in it runs as pasted"
                    ),
                },
            },
            "required": ["project"],
        }

    async def execute(
        self,
        project: str,
        path: str | None = None,
        pages: list[int] | None = None,
        palette: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str | ToolResult:
        try:
            deck = Project(workspace=self.workspace, slug=project)
        except ValueError as exc:
            return _return.failed(str(exc))

        try:
            pages = as_ints(pages, "pages") or None
        except ArgumentError as exc:
            return _return.failed(str(exc), hint="pages: [2, 4, 5]")

        already = bound(deck)
        rebound = self._bind(deck, path) if path else None
        # After the bind, because binding a different template drops the palette read
        # off the last one -- so a call that does both states this template's colours.
        stated: dict[str, Any] | None = None
        if palette is not None:
            try:
                stated = write_palette(deck, as_palette(palette))
            except PaletteError as exc:
                return _return.failed(str(exc), hint='palette: {"accent": "#155FFD"}')
        template = rebound or already
        if template is None:
            error, detail = self._nothing(path)
            return _return.failed(error, **detail)
        if self.provision is not None:
            self.provision(deck)

        payload: dict[str, Any] = {
            "project": project,
            "template": template.source.name,
            "canvas_in": f"{template.inventory.width_in:g}x{template.inventory.height_in:g}",
            "layouts": [layout.summary() for layout in template.inventory.layouts],
            "example_pages": template.example_pages,
            "build_from": "os.environ['PPT_TEMPLATE']",
        }
        if path and rebound is None:
            # Said, not swallowed: an author whose path was ignored would otherwise
            # believe it had bound the file it named.
            payload["note"] = (
                f"{path} was not usable as a template, so this is the one already bound to the deck. "
                "A file the user attached goes through ppt_prepare's `files`"
            )
        if template.inventory.theme_colours:
            payload["theme_colours"] = dict(template.inventory.theme_colours)
        if template.inventory.fonts:
            payload["fonts"] = list(template.inventory.fonts)
        if stated is not None:
            # Echoed, because a palette is merged over what the deck already stated and
            # an author correcting one role should see the seven it is now sitting with.
            payload["palette"] = stated
            payload["palette_note"] = (
                "kept for this deck; every page's ppt_theme carries these, and the roles you did "
                "not name are derived from the ones you did"
            )

        if pages:
            return await self._as_code(deck, template, pages, payload)
        return await self._as_renders(deck, template, payload)

    def _bind(self, deck: Project, path: str):
        """Bind the path, or None -- in which case the caller falls back to what is
        already bound.

        The fall-back is not laxity. A template usually arrives as an attachment,
        which `ppt_prepare` binds from a path under the media cache, outside the
        workspace; the task text then names that same absolute path, so an author
        asking to see the template it just bound passes it and this check refuses --
        which is right for a path a model produced and wrong as an answer to "show
        me the template". Refusing outright is only correct when there is no
        template at all.
        """
        source = (self.workspace / path).resolve()
        if not _inside(self.workspace, source):
            return None
        return bind(source, deck)

    def _nothing(self, path: str | None) -> tuple[str, dict[str, Any]]:
        if path:
            return (
                f"{path} could not be used as a template and this deck has none bound",
                {
                    "hint": (
                        "it has to be a .pptx inside the workspace that opens as a presentation. A file the "
                        "user attached goes through ppt_prepare's `files` instead -- an attachment lands "
                        "outside the workspace, where a path given here is not allowed to point"
                    )
                },
            )
        return (
            "no template is bound to this deck",
            {"hint": "call ppt_template with the path to the user's .pptx first, or build without one"},
        )

    # How many of the template's own content pages are sampled for their ground. Two
    # is not enough: one template runs a single white page between black ones, and a
    # sample of two that caught it read the whole deck as white. The pages come from a
    # PDF that has already been converted, so each extra one is a pdftoppm call.
    _GROUND_PAGES = 4

    def _palette(self, template: Any, measured: str | None) -> dict[str, Any]:
        """The colours the script will be handed, and where each came from.

        Handed over rather than imposed. The ground is the one field a file can be
        wrong about and often is -- of 119 templates, 46 declared a ground their own
        pages do not use -- so a reading off the render is the default and the pages
        it was read from are in this same reply. A model that looks at them and
        disagrees is better placed than any of this: it can see the template.
        """
        from raven_ppt.services.template.theme import theme_name, theme_of

        entry = theme_of(template.inventory)
        held = [value for _, value in template.inventory.theme_colours if str(value).startswith("#")]
        return {
            "name": theme_name(template.inventory),
            "ground": str(entry["background"]),
            "ground_read_from": "the renders above" if measured else "what the file declares",
            "ink": str(entry["foreground"]),
            "accent": str(entry["accent"]),
            "the_template_also_holds": held[:10],
            "yours_to_change": (
                "THEMES gives you these as a starting point, not a rule. Look at the pages above: if the "
                "ground you see is not the one named here, set it -- `T['background'] = '#RRGGBB'` in your "
                "program -- and the planes and muted copy derived from it follow. The same goes for any "
                "field. A colour you can see beats a colour something measured"
            ),
        }

    async def _record_ground(self, deck: Project, pdf: Any, folder: Path, house: Any) -> str | None:
        """Measure the ground off the template's own content pages and keep it.

        The declared ground is wrong often enough to matter: across 119 templates it
        agreed with the render 73 times, and eleven declared its inverse -- white over
        a blue page, black over a white one -- because what a reader sees is painted by
        a shape on the layout, not by the theme under it. Content pages rather than the
        cover, since a content page is what a build script mostly draws on.
        """
        pages = list(getattr(house, "content_pages", ()) or ())[: self._GROUND_PAGES]
        if pdf is None or not pages:
            return None
        shots = await self.thumbnails.pages_of(pdf, folder, pages)
        colour = ground_of([path for _, path in sorted(shots.items())])
        if colour:
            write_ground(template_dir(deck), colour)
        return colour

    async def _as_renders(self, deck: Project, template: Any, payload: dict[str, Any]) -> str | ToolResult:
        """Every example page as pictures, plus the measured house style."""
        folder = deck.review_dir / "template"
        listing = menu(template.source, _unwritable_pages(template.source, template.example_pages))
        named = roles(listing)
        pdf = await self.thumbnails.pdf(template.source, folder)
        wanted = list(range(1, template.example_pages + 1))
        renders = await self.thumbnails.pages_of(pdf, folder, wanted) if pdf is not None and wanted else {}
        # The render is read twice: once as pictures for the author, once as type sizes
        # for the house style. A template's title placeholder usually declares no size
        # at all, so without the render the ladder is a guess -- and the conversion has
        # already been paid for here.
        house = house_style(template.source, listing, pdf)
        measured = await self._record_ground(deck, pdf, folder, house)
        payload["palette"] = self._palette(template, measured)
        if named:
            payload["house_pages"] = {role: number for role, number in named.items()}
        if house is not None:
            payload["house_style"] = house.brief()
        if listing:
            payload["structural_pages_are"] = [entry.line() for entry in listing if entry.role]
            payload["template_pages"] = [entry.line() for entry in listing]
            payload["content_pages"] = (
                f"{len(house.content_pages) if house else 0} of the template's pages are content examples. "
                "They are adaptable prototypes: replace example text and pictures, use adapt(items=...) "
                "to fill the actual repeated units and remove spares, or compose inside the measured house "
                "style when the example cannot carry the argument."
            )
        asks = []
        if named:
            asks.append(
                "clone the template's structural pages -- "
                + ", ".join(f"its {role} is page {number}" for role, number in named.items())
                + " -- with `from ppt_template import adapt, prototype` then "
                "`adapt(prs, prototype(tpl, N), title='...', texts={'the words there now': 'yours'})`, where "
                "`tpl = Presentation(os.environ['PPT_TEMPLATE_SOURCE'])` -- the user's own file, with these "
                "example pages still in it. That is a different file from the one you build into: "
                "`PPT_TEMPLATE` has had them removed, so handing that one to `prototype` answers `this "
                "template ships 0 pages`. Those pages are the deck's house frame and are not redrawn"
            )
        # Every argument gets a literal call here, and that is the whole point of the
        # length. A live run called `adapt` fourteen times, passed `texts=` in thirteen
        # of them and `pictures=` and `drop=` in none -- the reply had spelled `texts={...}`
        # out and left the other two as prose, so the template's placeholder photograph and
        # its icons shipped untouched. The same run imported `drop_shape` and never called
        # it. A capability described is a capability declined.
        asks.append(
            "any listed example page may be a content prototype when its composition is close to the "
            "page's information shape: `adapt(prs, prototype(tpl, N), title='page title', "
            "subtitle='its second line', texts={'the words there now': 'yours'}, "
            "pictures={4: FIGURES/'fig2.png'}, drop=[9], keep=['20XX.XX.XX'])`, where "
            "`FIGURES = Path(os.environ['PPT_FIGURES_DIR'])` -- a bare 'figures/...' resolves against the "
            "build directory the program runs in, which is not where the ingest put them. A key is the "
            "shape's current text or its 1-based place on the page -- a shape's *name* matches nothing "
            "and raises, listing what the page does hold. `title` and `subtitle` reach the page's own "
            "heading rows without your having to know which shape they are, and on a content page they "
            "are the form to prefer. A closing page is where `subtitle` runs out: a heading row is the topmost "
            "line in the page's top third, and a cover that centres its title mid-page or a section "
            "divider carrying one word has no second one, so there `adapt` raises and you name that "
            "line in `texts` off the render instead"
        )
        asks.append(
            "the two defaults are opposites, and this is the one that catches authors out: **text you "
            "name in none of those arguments is emptied**, because the words on a template page are its "
            "example copy -- so name every line you mean to keep, or list it in `keep=[...]` to leave it "
            "exactly as the template wrote it. **A picture or icon you name in neither `pictures` nor "
            "`drop` stays as it is**, so the template's stock photograph and its decorative icons ship in "
            "your deck unless you replace them or drop them. `pictures={4: FIGURES/'fig2.png'}` "
            "keeps the frame's size, crop and rounding; `pictures={4: (FIGURES/'fig2.png', "
            "(0.8, 1.6, 7.4, 4.2))}` reshapes the frame first, where those four numbers are "
            "(left, top, width, height) in inches -- a size, not the two corners a `ppt_layout` Box "
            "holds, though a Box may be handed over whole and is converted"
        )
        asks.append(
            "a content page is usually one small group repeated, and `items` is what fills it: "
            "`adapt(prs, prototype(tpl, N), title='...', items=[['01', 'first heading', 'its body'], "
            "['02', 'second heading', 'its body']])`. One entry per unit, each a list positional over "
            "that unit's text shapes -- `None` keeps one as it is, and `''` over a number restates it "
            "for its new position -- or a dict keyed by the text a shape holds now. Give one value per "
            "text shape in the unit and not one per line you have to say: a list shorter than the unit "
            "leaves the rest exactly as the template wrote it, so a two-value entry against a "
            "three-shape card ships the template's own placeholder copy once per surviving card. "
            "Passing more values than the unit holds raises, naming each shape, which is how to learn "
            "the count. The units left over "
            "are deleted rather than emptied, so a six-slot page carrying four points loses two slots "
            "instead of shipping two empty bubbles. `items` fills the page's longest run only: if a "
            "render shows more repeated cards than you passed items for, the page holds a second run, "
            "and `fill(units(slide)[1], [['03', 'third heading', 'its body']])` after `adapt` returns "
            "fills that one -- positionally, because `adapt` has already emptied its words; both come "
            "from `from ppt_template import adapt, prototype, fill, units, shape_at, drop_shape`. A prototype "
            "is a starting composition, not an immutable form; if its capacity or picture geometry is "
            "wrong, choose another page or compose inside the house style"
        )
        if house is not None and house.layout:
            asks.append(
                f"for a page without a suitable prototype, draw it yourself: "
                f"`layout = prs.slide_layouts.get_by_name({house.layout!r})` then "
                f"`prs.slides.add_slide(layout)`, so the page inherits the template's background, put the page title "
                "in the title row exactly as given above, and lay the content out inside body_area_in with "
                "`ppt_layout` -- Box.columns, .rows, .grid, plane(), write(), table(). The arrangement is "
                "yours to decide from what the page has to say"
            )
        if house is not None and house.scale:
            asks.append(
                "one scale for the whole deck: every page title at "
                f"{house.scale.get('title', 28):g}pt, body copy at {house.scale.get('body', 18):g}pt, and "
                "copy at or above 14pt, with a caption or a source line free to reach 10.8pt. Same size for the same role on every page -- a reader reads each page "
                "against the one before it, and a deck whose body size moves page to page reads as unfinished"
            )
        asks.append(
            "open the deck with Presentation(os.environ['PPT_TEMPLATE']) -- the template with its example "
            "pages removed, so a page you add inherits its master, theme and canvas"
        )
        if named and not renders:
            # What is known is that this call produced none, and that is all this
            # may say. It used to say "unavailable on this machine", which is a
            # fact about the host rather than about the call -- and the author acts
            # on it for the rest of the deck: one live run read every example page
            # as code and never asked for a render again, on a host where the pdf
            # and four page renders had just been written to disk. The failure is
            # in the log now; the sentence no longer states its cause.
            payload["renders"] = (
                "none came back from this call; ask again for a few pages by number, "
                "or read selected example pages as code"
            )
        body = _return.done(asks=asks, **payload)
        blocks: list[Any] = []
        by_number = {number: role for role, number in named.items()}
        for number in sorted(renders):
            blocks.append(text_block(f"Template page {number} -- the template's {by_number.get(number, 'page')}"))
            blocks.append(image_block(self.views.data_uri(renders[number])))
        return _return.with_images(body, blocks)

    async def _as_code(
        self, deck: Project, template: Any, pages: list[int], payload: dict[str, Any]
    ) -> str | ToolResult:
        """The chosen pages as source, with their pictures written where it can find them.

        The pictures go into the build directory rather than beside the template,
        so the `add_picture("template_00.png", ...)` in the reference is a line the
        author can paste and run: that directory is where the program runs.
        """
        deck.build_dir.mkdir(parents=True, exist_ok=True)
        asked = sorted(set(pages))
        # Out of range first: "page 9 of a 2-page template" is a different mistake from
        # "page 5 is a content page", and answering the second for the first sends an
        # author looking for a house style that has nothing to do with it.
        beyond = [number for number in asked if number > template.example_pages]
        if beyond and len(beyond) == len(asked):
            return _return.failed(
                f"none of pages {asked} could be read out of {template.source.name}",
                hint=f"the template ships {template.example_pages} example pages, numbered from 1",
            )
        asked = [number for number in asked if number not in beyond]
        sources = [
            page
            for number in asked[:MAX_SOURCE_PAGES]
            if (page := decompile(template.source, number - 1, images_dir=deck.build_dir))
        ]
        if not sources:
            return _return.failed(
                f"none of pages {sorted(set(pages))} could be read out of {template.source.name}",
                hint=f"the template ships {template.example_pages} example pages, numbered from 1",
            )

        asks = [
            "adapt what you read rather than reproducing it: the counts, the words and the pictures are "
            "this deck's, and only the design language is the template's"
        ]
        unwritable = {page.index + 1: page.unredrawable for page in sources if page.unredrawable}
        if unwritable:
            payload["cannot_be_redrawn"] = {str(number): list(what) for number, what in unwritable.items()}
            asks.append(
                "pages "
                + ", ".join(str(number) for number in sorted(unwritable))
                + " hold shapes python-pptx cannot write, so code alone will not reproduce them -- clone "
                "those pages with `from ppt_template import clone_page, replace_text, replace_picture, "
                "drop_shape` and edit the copy instead"
            )
        payload["pages_read"] = [page.index + 1 for page in sources]
        if len(asked) > MAX_SOURCE_PAGES:
            # Said rather than silently applied: a truncated list that reads as the
            # whole list is how an author concludes a page has nothing on it.
            payload["pages_not_read"] = asked[MAX_SOURCE_PAGES:]
        body = _return.done(asks=asks, **payload)
        return _return.with_images(body, [text_block(page.summary()) for page in sources])


def _unwritable_pages(source: Path, count: int) -> dict[int, tuple[str, ...]]:
    """Which example pages hold something python-pptx cannot write, page -> what.

    Every page, not the ones asked for: the verdict an author needs at the moment of
    choosing is "9 of 13", and a per-request answer cannot say that. Decompiling all
    of them costs well under a second on the templates measured, and no images are
    written because none are wanted here.
    """
    found: dict[int, tuple[str, ...]] = {}
    for number in range(1, count + 1):
        page = decompile(source, number - 1)
        if page is not None and page.unredrawable:
            found[number] = page.unredrawable
    return found


def _inside(workspace: Path, path: Path) -> bool:
    root = workspace.resolve()
    return path == root or root in path.parents
