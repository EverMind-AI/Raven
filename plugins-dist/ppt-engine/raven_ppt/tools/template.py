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
    needed_imports,
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
# How much source one reply carries. The host cuts a tool result at 16,000 characters
# and marks the cut, and a five-page reference measured 16,016: the author saw two and
# a half pages and asked again for the rest. Pages that do not fit are named instead.
SOURCE_BUDGET_CHARS = 13_000


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

    @staticmethod
    def _layout_pictures(source: Path) -> list[str]:
        """The layouts of this template that carry a picture, each with its example pages.

        Said here because nothing else says it: the page renders show the photograph, the
        page's own shapes do not carry it, and an author told to replace the template's
        pictures looked for them on the page and found nothing to replace.
        """
        from raven_ppt.services.measure.adherence import layouts_with_photographs

        return [
            f"layout '{layout}' carries {len(sizes)} picture(s) ({', '.join(sizes)}), under example page(s) "
            + ", ".join(str(page) for page in pages)
            + " -- on the page you build from one of those: `replace_picture(layout_pictures(slide)[0], "
            + f"FIGURES / 'x.png', 'cover'{', alpha=0.25' if _page_sized(sizes) else ''})`"
            for layout, (pages, sizes) in layouts_with_photographs(source).items()
        ]

    @staticmethod
    def _borrowable(source: Path) -> list[str]:
        """The reference pages of the other bundled templates, each named by its arrangement.

        Measured before this existed: an author whose template had no timeline drew one
        from `stack` and `plane`, while three other bundled templates shipped one. A
        page named by template, number and arrangement is an offer the way the bound
        template's own pages are; "the other templates have pages too" is not.
        """
        from raven_ppt.services.template.defaults import bundled_path, reference_pages

        said: list[str] = []
        menus: dict[str, dict[int, Any]] = {}
        for stem, number in reference_pages(except_stem=Path(source).stem):
            if stem not in menus:
                path = bundled_path(stem)
                menus[stem] = {entry.number: entry for entry in menu(path)} if path else {}
            entry = menus[stem].get(number)
            if entry is None or entry.role or entry.hidden:
                continue
            what = entry.arrangement or f"{entry.text_blocks} text, {entry.shapes} drawn"
            slots = f" ({entry.slots} slots)" if entry.slots else ""
            said.append(f"{stem} page {number} is {what}{slots}")
        return said

    async def _as_renders(self, deck: Project, template: Any, payload: dict[str, Any]) -> str | ToolResult:
        """Every example page as pictures, plus the measured house style."""
        folder = deck.review_dir / "template"
        listing = menu(template.source, _unwritable_pages(template.source, template.example_pages))
        named = roles(listing)
        pdf = await self.thumbnails.pdf(template.source, folder)
        shown = {entry.number for entry in listing if not entry.hidden}
        wanted = [number for number in range(1, template.example_pages + 1) if number in shown]
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
        examples_named: list[str] = []
        if listing:
            payload["structural_pages_are"] = [entry.line() for entry in listing if entry.role]
            payload["template_pages"] = [entry.line() for entry in listing]
            # Named page by page, and by the arrangement each one is, because the reply
            # that named only the structural pages got exactly the structural pages
            # cloned. Measured on one live run against an eighteen-page template: five
            # `prototype` calls, all five of them the pages this reply named by role,
            # and twelve content pages drawn from `stack`/`plane`/`write` instead --
            # which is how a deck comes out with three tables and no timeline. The run
            # before it, whose request happened to list the arrangements in prose,
            # cloned eleven of twelve. Prose about "adaptable prototypes" is not the
            # same offer as a page number beside the shape it holds.
            examples = [entry for entry in listing if not entry.role and not entry.hidden]
            examples_named = [
                f"page {entry.number} is {entry.arrangement}" + (f" ({entry.slots} slots)" if entry.slots else "")
                for entry in examples
                if entry.arrangement
            ]
            shapes = "; ".join(examples_named)
            payload["content_pages"] = (
                f"{len(examples)} of the template's pages are content examples"
                # A page too sparse to read a signature off has no arrangement to name,
                # and the sentence still has to end somewhere: without this the reply
                # came out as "already draws: . Clone the one ...".
                + (f", each an arrangement this template already draws: {shapes}" if shapes else "")
                + ". Clone the one whose arrangement matches the page's information shape -- "
                "`adapt(prs, prototype(tpl, N), title='...', texts={...})` -- and prefer that to drawing "
                "the page yourself: a page composed from `plane`, `write` and `stack` has those and "
                "nothing else, while whatever this template draws that they cannot -- a timeline, a ring "
                "of badges, a numbered pill, a figure card -- exists on these pages and nowhere else. "
                "Use `adapt(items=...)` to fill the repeated units and remove the spares. Compose inside "
                "the measured house style only for a page no example can carry."
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
        # Named page by page in the *ask*, because the ask is what becomes `next_step` and
        # `next_step` is what the program acts on. The same list went into the payload as
        # `content_pages` first and moved a live run from five prototypes out of fifteen
        # pages to six: the reply that names the structural pages concretely, and leaves
        # the content pages to a sentence about "any listed example page", gets the
        # structural pages cloned and the rest drawn from primitives. A page number
        # beside the shape it holds is a different offer from a category.
        if examples_named:
            asks.append(
                "give every content page a prototype from this list, by the arrangement it holds: "
                + "; ".join(examples_named)
                + " -- record the page number you picked in the plan's `prototype` field and clone it with "
                "`adapt(prs, prototype(tpl, N), ...)`. A page you compose instead has `plane`, `write` and "
                "`stack` and nothing else, and whatever this template draws that those cannot -- a timeline, "
                "a ring of badges, a numbered pill, a figure card -- is on these pages and nowhere else. "
                "Compose from scratch only for a page no arrangement here can carry, and say which"
            )
        carried = self._layout_pictures(template.source)
        if carried:
            payload["layout_pictures"] = carried
            asks.append(
                "this template keeps some of its photographs on its layouts, where every page on the layout "
                "inherits them and `pictures={...}` on a cloned page never reaches them: "
                + "; ".join(carried)
                + ". Change one for every page at once with `replace_picture(layout_pictures(slide)[0], "
                "FIGURES/'x.png', 'cover', alpha=0.25)` after `from ppt_template import layout_pictures` -- a "
                "picture generated in the deck's own style (`ppt_generate_image`) is the usual replacement, and "
                "one the size of the page is the page's background, so it takes the `alpha` or every title on "
                "that layout drowns; `backdrop` does the same for a page whose layout carries none -- or keep it "
                "if it is the design rather than a stock photograph"
            )
        borrowable = self._borrowable(template.source)
        if borrowable:
            payload["borrowable_pages"] = borrowable
            asks.append(
                "for a page no example above can carry, borrow one of these pages from another bundled "
                "template -- its colours and master become this deck's, only the arrangement comes across: "
                + "; ".join(borrowable)
                + ". Clone it with `adapt(prs, prototype(bundled('<template>'), N), ...)` after "
                "`from ppt_template import bundled`, and record both `borrowed: '<template>'` and "
                "`prototype: N` on that page of the plan"
            )
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
            "from `from ppt_template import adapt, prototype, fill, units, shape_at, drop_shape`. To adjust one "
            "shape after `adapt`, take a handle on it -- `shape_at(slide, 5)` by the number printed above, "
            "`shape_near(slide, 1.56, 2.47)` by where the page shows it, `shape_saying(slide, 'Method')` by the "
            "copy it starts with -- and never by `shape.left`, which inside a group is the group's own "
            "coordinate and not a position on the page. `raise_type(slide)` lifts copy the template states "
            "under the readability floor. A prototype "
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
        sources = []
        texts: list[str] = []
        unread: list[int] = []
        cut: dict[str, dict[str, int]] = {}
        carried = 0
        for number in asked:
            page = decompile(template.source, number - 1, images_dir=deck.build_dir)
            if page is None:
                continue
            block = page.summary(imports=False)
            if sources and (len(sources) >= MAX_SOURCE_PAGES or carried + len(block) > SOURCE_BUDGET_CHARS):
                unread.append(number)
                continue
            if len(block) > SOURCE_BUDGET_CHARS:
                # One page larger than the whole budget is still bounded here, not by
                # the host: cut at the host's mark, the reply ended mid-line and read as
                # a complete page. A page this size is a page to clone, and the note
                # says so where the code stops.
                note = (
                    f"\n# -- cut: {{withheld}} more line(s) of page {number} did not fit the "
                    f"{SOURCE_BUDGET_CHARS}-character reply. A page too large to read as code is one to clone: "
                    "`from ppt_template import clone_page, replace_text, replace_picture, drop_shape`"
                )
                block, withheld = _cut(block, SOURCE_BUDGET_CHARS - len(note) - 4)
                cut[str(number)] = withheld
                block += note.format(withheld=withheld["lines_withheld"])
            sources.append(page)
            texts.append(block)
            carried += len(block)
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
        if cut:
            payload["pages_cut"] = cut
            asks.append(
                "page(s) " + ", ".join(cut) + " were larger than one reply and stop where the `# -- cut` line "
                "says; clone those pages rather than reading the rest"
            )
        if unread:
            # Said rather than silently applied: a truncated list that reads as the
            # whole list is how an author concludes a page has nothing on it.
            payload["pages_not_read"] = unread
            asks.append(
                "pages " + ", ".join(str(number) for number in unread) + " did not fit this reply; ask for them "
                "in a second call"
            )
        body = _return.done(asks=asks, **payload)
        # The imports once, ahead of the first page, rather than once per page.
        imports = needed_imports(page.source for page in sources)
        if imports:
            texts[0] = "\n".join(f"# {line}" for line in imports) + "\n" + texts[0]
        return _return.with_images(body, [text_block(text) for text in texts])


def _cut(block: str, budget: int) -> tuple[str, dict[str, int]]:
    """The first whole lines of ``block`` that fit ``budget``, and how many were withheld."""
    lines = block.splitlines(keepends=True)
    kept: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) > budget:
            break
        kept.append(line)
        size += len(line)
    if not kept:
        kept = [lines[0][:budget]]
    return "".join(kept).rstrip("\n"), {"lines_shown": len(kept), "lines_withheld": len(lines) - len(kept)}


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


def _page_sized(sizes: list[str]) -> bool:
    """Whether any of these "WxHin" pictures is most of the page, which is a background."""
    for size in sizes:
        try:
            width, height = (float(part) for part in size.rstrip("in").split("x"))
        except ValueError:
            continue
        if width * height >= 0.55 * 13.333 * 7.5:
            return True
    return False
