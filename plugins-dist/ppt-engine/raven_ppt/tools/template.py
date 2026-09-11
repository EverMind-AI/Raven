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

import asyncio
import hashlib
import logging
from collections.abc import Callable
from dataclasses import is_dataclass, replace
from pathlib import Path
from typing import Any

from raven.contracts.tool import Tool, ToolResult
from raven.utils.images import image_block, text_block
from raven_ppt.contracts import Project
from raven_ppt.services.render import RenderError
from raven_ppt.services.template import (
    PaletteError,
    as_palette,
    bind,
    bound,
    decompile,
    needed_imports,
    write_palette,
)
from raven_ppt.services.template.capacity import LEGEND
from raven_ppt.services.template.house import house_style
from raven_ppt.services.template.inventory import template_dir, write_ground
from raven_ppt.services.template.menu import ICON_SLOT_NOTE, menu, roles
from raven_ppt.services.template.theme import borrow_ink_note, ground_of
from raven_ppt.tools import _return
from raven_ppt.tools._args import ArgumentError, as_ints

_log = logging.getLogger(__name__)

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

# How the example pages travel: one tiled sheet, four cells to a row, rather than one
# picture each. Measured over 459 vision calls on the model this engine runs, asking it
# to name the page whose arrangement fits a stated need: the sheet scores 90% top-1 over
# 150 answers and so do 20 separate renders, at 3.27MB against 8.71MB, a 7.0s median
# against 11.0s, and -- the cost that actually killed a recorded run -- one image pinned
# in the request afterwards instead of twenty. That run put 23 template renders into a
# request, `ppt_figure_inspect` took it to 58 images and 16.8MB, and the provider
# answered 200 with empty content five times on a byte-identical body.
#
# The renders are not replaceable by the text beside them. The same measurement sent
# this reply's own `template_pages` lines with no pictures at all: 49% overall, 10% on a
# row of headline figures, 30% on a chart page, 43% on a timeline. `page_signature` has
# no word for those shapes and says so in its own docstring, so what the pictures carry
# is exactly the part the sentences cannot.
SHEET_COLUMNS = 4
# The borrowable pages, six to a row: 36 of them at four columns would be nine rows,
# where the ceiling starts costing cell width, while six columns is six rows and comes
# out 4664x2642 with the same 768px cells. Measured on the same model: this sheet
# answers 100% of the same need list, where the text-only offer that ships today
# answers 27% -- the largest single gain in the whole measurement, and a picture the
# author has never been shown at any price.
BORROW_SHEET_COLUMNS = 6

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
        "example pages, and only those, as python-pptx source to adapt. Called with `palette` it keeps the colours you "
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

        payload: dict[str, Any] = {"project": project, "template": template.source.name}
        if path and rebound is None:
            # Said, not swallowed: an author whose path was ignored would otherwise
            # believe it had bound the file it named.
            payload["note"] = (
                f"{path} was not usable as a template, so this is the one already bound to the deck. "
                "A file the user attached goes through ppt_prepare's `files`"
            )
        # The roster rides on the calls that change or first show what it describes -- a
        # bind, or a look at the renders -- and not on a page read: an author reading a
        # deck's pages two or three at a time saw it on the bind, and one run carried
        # the same roster seven times over.
        if not pages or rebound is not None:
            payload.update(_roster(template))
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
            + f"FIGURES / 'x.png', 'cover'{', alpha=0.1' if _page_sized(sizes) else ''})`"
            + (
                " -- page-sized, so it is the page's background: alpha=0.1 keeps it a texture, and a photograph "
                "meant to be seen goes in at full strength under a plane of ink with light type, as `backdrop` lays them"
                if _page_sized(sizes)
                else ""
            )
            for layout, (pages, sizes) in layouts_with_photographs(source).items()
        ]

    @staticmethod
    def _borrowable(source: Path) -> list[tuple[str, int, str]]:
        """The reference pages of the other bundled templates, each named by its arrangement.

        Measured before this existed: an author whose template had no timeline drew one
        from `stack` and `plane`, while three other bundled templates shipped one. A
        page named by template, number and arrangement is an offer the way the bound
        template's own pages are; "the other templates have pages too" is not.

        (stem, page, sentence) rather than the sentence alone, so the sheet of these
        pages and the sentences about them cannot come apart: one walk over the offer
        list decides both what is said and what is shown.
        """
        from raven_ppt.services.template.defaults import bundled_path, reference_artwork, reference_pages

        said: list[tuple[str, int, str]] = []
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
            # The one thing on a reference page that does not follow the deck. Said
            # against the four pages it is true of and no others: a note on a page that
            # comes across clean is what teaches an author to read past the notes.
            drawings = reference_artwork(stem, number)
            carried = (
                f" -- replace its {drawings} drawing(s), painted in {stem.split('_')[0]}'s own colours"
                if drawings
                else ""
            )
            said.append((stem, number, f"{_cell(stem, number)} = {stem} page {number} is {what}{slots}{carried}"))
        return said

    async def _borrow_sheet(
        self, deck: Project, offers: list[tuple[str, int, str]]
    ) -> tuple[Path, list[tuple[str, int]]] | None:
        """The offered reference pages as one picture, and which of them are on it.

        None when none of them can be rendered.

        Their own sheet rather than cells appended to the bound template's, because the
        two answer different questions -- which of my template's pages is nearest this,
        against which page of another template can carry what mine cannot -- and a cell
        the author cannot tell apart from its neighbour by provenance is worse than no
        cell. The pages of every offered template are rendered once into the deck and
        kept, so the second call through here pays for the tiling only.

        The pictured pages come back with the sheet because the sentences beside it
        count them. One lender that fails to convert leaves the other lenders' cells
        intact, and a sheet of those cells is worth showing -- but named after the whole
        offer it claimed to picture pages it did not, the reply said so in two places,
        and every later call read that sheet out of the cache and never asked the failed
        lender again. Named after what rendered it is honest and still a cache: a later
        call with that lender back in service renders one page more, names a different
        sheet and composes it, while the per-template conversions beside it are already
        on disk, so the retry costs the tiling and not the LibreOffice run.
        """
        from raven_ppt.services.template.defaults import bundled_path

        folder = deck.review_dir / "borrowable"
        wanted = {stem: pages for stem, pages in _by_stem(offers).items() if bundled_path(stem) is not None}
        # The complete sheet's own name is known before anything is rendered, and it is
        # what the ordinary follow-up call wants. Only the tiling is downstream of the
        # render: `pages_of` rasterises every page it is handed whether or not a sheet
        # already holds them (9.9s of the 41.3s over the seven templates), so looking
        # for the file after the render pays that on every call. A sheet short of the
        # offer has no fast path on purpose -- falling through is how the lender that
        # failed is asked again.
        offered = [(stem, number) for stem, pages in wanted.items() for number in pages]
        whole = folder / f"sheet_{len(offered)}_{_pictured_key(offered)}.png"
        if whole.is_file() and whole.stat().st_size > 0:
            return whole, offered

        async def render(stem: str, pages: list[int]) -> dict[int, Path]:
            pdf = await self.thumbnails.pdf(bundled_path(stem), folder / stem)
            return await self.thumbnails.pages_of(pdf, folder / stem, pages) if pdf is not None else {}

        # Together, because seven templates is seven LibreOffice conversions and they
        # are what this costs: 31.5s of the 41.3s measured over the seven, against 9.9s
        # of rasterising. The views' own gate still holds the conversions to two at a
        # time, which is what keeps this from being the reason a build machine stalls.
        done = await asyncio.gather(*(render(stem, pages) for stem, pages in wanted.items()))
        shots: list[Path] = []
        labels: list[str] = []
        pictured: list[tuple[str, int]] = []
        for (stem, pages), rendered in zip(wanted.items(), done, strict=True):
            for number in pages:
                if number in rendered:
                    shots.append(rendered[number])
                    labels.append(_cell(stem, number))
                    pictured.append((stem, number))
        if not shots:
            return None
        # Named after which pages are on it, not how many. A deck rebound from one
        # bundled template to another is offered a different 34 pages, and a cache
        # keyed on the count would hand it the first template's sheet -- showing the
        # pages of the template it is now built in and hiding the ones it may borrow.
        made = whole if pictured == offered else folder / f"sheet_{len(pictured)}_{_pictured_key(pictured)}.png"
        if made.is_file() and made.stat().st_size > 0:
            return made, pictured
        try:
            sheet = await asyncio.to_thread(
                self.thumbnails.contact_sheet, shots, made, BORROW_SHEET_COLUMNS, labels=labels
            )
        except RenderError:
            # One picture short of an offer the text still makes in full. The sentences
            # are the capability; the sheet is what makes them choosable.
            _log.warning("template: the borrowable reference sheet did not compose", exc_info=True)
            return None
        return sheet, pictured

    async def _as_renders(self, deck: Project, template: Any, payload: dict[str, Any]) -> str | ToolResult:
        """Every example page as pictures, plus the measured house style."""
        folder = deck.review_dir / "template"
        listing = menu(template.source, *_page_notes(template.source, template.example_pages))
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
                "`s = clone_page(prs, prototype(tpl, N))` then `replace_text(s, 'the words there now', 'yours')` -- prefer that to drawing "
                "the page yourself: a page composed from `plane`, `write` and `stack` has those and "
                "nothing else, while whatever this template draws that they cannot -- a timeline, a ring "
                "of badges, a numbered pill, a figure card -- exists on these pages and nowhere else. "
                "Write each repeated unit with `replace_text` and take the spares out with `remove_unit`. Compose inside "
                "the measured house style only for a page no example can carry. " + LEGEND
            )
        asks = []
        if named:
            asks.append(
                "clone the template's structural pages -- "
                + ", ".join(f"its {role} is page {number}" for role, number in named.items())
                + " -- with `from ppt_template import clone_page, prototype, replace_text` then "
                "`s = clone_page(prs, prototype(tpl, N))` and one `replace_text(s, old, new)` per line, where "
                "`tpl = Presentation(os.environ['PPT_TEMPLATE_SOURCE'])` -- the user's own file, with these "
                "example pages still in it. That is a different file from the one you build into: "
                "`PPT_TEMPLATE` has had them removed, so handing that one to `prototype` answers `this "
                "template ships 0 pages`. Those pages are the deck's house frame and are not redrawn"
            )
        # Every argument gets a literal call here, and that is the whole point of the
        # length. A live run cloned fourteen pages, wrote copy into thirteen of them and
        # reached for a picture or a leftover shape in none -- the reply had spelled the
        # copy call out and left the other two as prose, so the template's placeholder
        # photograph and its icons shipped untouched. The same run imported `drop_shape`
        # and never called it. A capability described is a capability declined.
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
                "`clone_page(prs, prototype(tpl, N))` and `replace_text`. A page you compose instead has `plane`, `write` and "
                "`stack` and nothing else, and whatever this template draws that those cannot -- a timeline, "
                "a ring of badges, a numbered pill, a figure card -- is on these pages and nowhere else. "
                "Compose from scratch only for a page no arrangement here can carry, and say which"
            )
        carried = self._layout_pictures(template.source)
        if carried:
            payload["layout_pictures"] = carried
            asks.append(
                "this template keeps some of its photographs on its layouts, where every page on the layout "
                "inherits them and a `replace_picture` on a cloned page never reaches them: "
                + "; ".join(carried)
                + ". Change one for every page at once with `replace_picture(layout_pictures(slide)[0], "
                "FIGURES/'x.png', 'cover')` after `from ppt_template import layout_pictures`, and keep the rest "
                "of that layout's art: the chips, marks and rules beside the picture are the cover's design, and "
                "a cover that dropped them for a full-bleed photograph came out as type on fog. A cut-out slot "
                "on the page's own ground takes a cut-out in this template's manner (`ppt_generate_image(..., "
                "transparent=true)`, its palette and outline named in the prompt), even for a real place; a "
                "photograph slot takes a photograph; one the size of the page is the page's background and wants "
                "a plane of ink and light type over it, the way `backdrop` lays a photograph for a page whose "
                "layout carries none -- or keep it if it is the design rather than a stock photograph"
            )
        borrowable = self._borrowable(template.source)
        borrowed = await self._borrow_sheet(deck, borrowable) if borrowable else None
        borrow_sheet, pictured = borrowed if borrowed is not None else (None, [])
        whole_offer = len(pictured) == len(borrowable)
        if borrowable:
            payload["borrowable_pages"] = [said for _, _, said in borrowable]
            asks.append(
                "for a page no example above can carry, borrow one of these pages from another bundled "
                "template -- its colours and master become this deck's, only the arrangement comes across: "
                + "; ".join(said for _, _, said in borrowable)
                + ". Clone it with `clone_page(prs, prototype(bundled('<template>'), N))` after "
                "`from ppt_template import bundled`, and record both `borrowed: '<template>'` and "
                "`prototype: N` on that page of the plan"
                + (
                    (
                        f". The picture below shows all {len(pictured)} of them, one cell each, the plate on "
                        "every cell reading as the key above"
                        if whole_offer
                        else f". The picture below shows {len(pictured)} of them, one cell each, the plate on "
                        f"every cell reading as the key above; the other {len(borrowable) - len(pictured)} "
                        "did not render on this host and are offered by the text above alone"
                    )
                    if borrow_sheet is not None
                    else ""
                )
                + (f". {ink}" if (ink := borrow_ink_note(template.inventory, template.source)) else "")
            )
        asks.append(
            "any listed example page may be a content prototype when its composition is close to the "
            "page's information shape. The whole route is four calls:\n"
            "```python\n"
            "from ppt_template import clone_page, drop_shape, prototype, remove_unit, replace_picture, "
            "replace_text, shape_at, units\n"
            "tpl = Presentation(os.environ['PPT_TEMPLATE_SOURCE'])   # the original, example pages intact\n"
            "s = clone_page(prs, prototype(tpl, 7))                  # arrives holding the template's words\n"
            "replace_text(s, 'the words there now', 'yours')         # once per line this page says\n"
            "replace_picture(shape_at(s, 4), FIGURES / 'fig2.png', 'cover')\n"
            "drop_shape(shape_at(s, 9))                              # a shape this page does not use\n"
            "```\n"
            "where `FIGURES = Path(os.environ['PPT_FIGURES_DIR'])` -- a bare 'figures/...' resolves against "
            "the build directory the program runs in, which is not where the ingest put them. "
            "**`replace_text`'s first argument is the text that shape is holding right now, and "
            "`ppt_template(pages=[N])` prints exactly that string above every shape on the page**, so "
            "read the page back and each printed line is a key you can paste. It matches on the words in "
            "the box and never on a shape's name; a string that matches nothing raises, listing what the "
            "page does hold"
        )
        asks.append(
            "**a cloned page arrives carrying the template's own words, and every line you do not "
            "replace is still saying them.** That is this route's safety rather than a nuisance: a line "
            "you missed is visible on the page, and the build refuses to publish it and quotes the line "
            "back (`placeholder_copy`); a numeral or a glyph of the template's left behind is one warning "
            "for the page rather than a refusal (`placeholder_marks`). So every block is either replaced "
            "or taken off the page with `drop_shape`, bar one: a structural page's own label -- the `目录` "
            "or `Agenda` an index page is called by -- stays, and nothing asks you for it. "
            "The same holds for pictures and icons -- the template's stock photograph "
            "ships in your deck unless you replace or drop it. `replace_picture(shape, image, 'cover')` "
            "keeps the frame's size, crop and rounding; `box=(0.8, 1.6, 7.4, 4.2)` reshapes the frame "
            "first, those four numbers being (left, top, width, height) in inches -- a size, not the two "
            "corners a `ppt_layout` Box holds, though a Box may be handed over whole and is converted -- "
            "and `anchor`, `trim`, `zoom` and `alpha` are there for the rest"
        )
        asks.append(
            "a content page is usually one small group repeated, and each repeat is written the same "
            "way -- one `replace_text` per line, keyed on the words that unit is holding now. **A page "
            "with more slots than the deck has points needs the spares taken out, and that is "
            "`remove_unit`:**\n"
            "```python\n"
            "s = clone_page(prs, prototype(tpl, 2))          # an agenda of eight slots\n"
            "for old, new in pairs:                          # six of them take this deck's sections\n"
            "    replace_text(s, old, new)\n"
            "for spare in max(units(s), key=len)[6:]:        # the seventh and eighth go, and the\n"
            "    remove_unit(spare)                          # survivors are laid out again\n"
            "```\n"
            "`remove_unit` closes the row up; `drop_shape` removes a shape and leaves the hole, which is "
            "what you want for one shape and not for a slot. Writing '' into a slot you meant to delete "
            "empties its text and ships its numbered bubble anyway, so it is not a spelling of delete. "
            "`units(s)` says how many runs the page has and how long each is -- check it whenever the "
            "render shows more repeated cards than you wrote. To reach one shape: `shape_at(s, 5)` by "
            "the number the read-back prints, `shape_near(s, 1.56, 2.47)` by where the page shows it, "
            "`shape_saying(s, 'Method')` by the copy it starts with -- and never by `shape.left`, which "
            "inside a group is the group's own coordinate and not a position on the page. "
            "`raise_type(s)` lifts copy the template states under the readability floor. A prototype is "
            "a starting composition, not an immutable form; if its capacity or picture geometry is "
            "wrong, choose another page or compose inside the house style"
        )
        if house is not None and house.layout:
            asks.append(
                f"for a page without a suitable prototype, draw it yourself: "
                f"`layout = prs.slide_layouts.get_by_name({house.layout!r})` then "
                f"`prs.slides.add_slide(layout)`, so the page inherits the template's background, put the page title "
                "in the title row exactly as given above, and lay the content out inside the box "
                "`body_area_as_code` hands you -- paste that line, then divide the box with `ppt_layout`: "
                "Box.columns, .rows, .grid, plane(), write(), table(). Every one of those takes a box of two "
                "corners, which is what `body_area_corners_in` and that line both are. The arrangement is "
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
        entries = {entry.number: entry for entry in listing}
        shown_pages = sorted(renders)
        legend = [_render_label(number, by_number.get(number), entries.get(number)) for number in shown_pages]
        pages_sheet = await self._pages_sheet(folder, renders) if renders else None
        if pages_sheet is not None:
            blocks.append(
                text_block(
                    f"The {len(shown_pages)} example pages are one picture below, {SHEET_COLUMNS} to a row in "
                    "page order, each cell carrying its page number on a dark plate at its top-left. What each "
                    "one is:\n\n" + "\n\n".join(legend)
                )
            )
            blocks.append(image_block(self.views.sheet_uri(pages_sheet)))
        else:
            for number, said in zip(shown_pages, legend, strict=True):
                blocks.append(text_block(said))
                blocks.append(image_block(self.views.data_uri(renders[number])))
        if borrow_sheet is not None:
            blocks.append(
                text_block(
                    (
                        f"And the {len(pictured)} pages offered from the other bundled templates, "
                        if whole_offer
                        else f"And {len(pictured)} of the {len(borrowable)} pages offered from the other bundled "
                        "templates -- the rest did not render here -- "
                    )
                    + f"{BORROW_SHEET_COLUMNS} to a row, each cell's plate naming the template and the page the "
                    "way the key above does. Only their arrangement comes across when you clone one; the "
                    "colours you see are their own template's and become this deck's."
                )
            )
            blocks.append(image_block(self.views.sheet_uri(borrow_sheet)))
        return _return.with_images(body, blocks)

    async def _pages_sheet(self, folder: Path, renders: dict[int, Path]) -> Path | None:
        """The template's example pages as one picture, or None when tiling is unavailable.

        The cell's plate carries the page number this reply names, handed over rather
        than parsed back out of the file name: the numbering an author answers in is
        this listing's, and a subset or a renumbered render would make the two disagree.
        """
        numbers = sorted(renders)
        made = folder / f"pages_sheet_{len(numbers)}.png"
        try:
            return await asyncio.to_thread(
                self.thumbnails.contact_sheet,
                [renders[number] for number in numbers],
                made,
                SHEET_COLUMNS,
                labels=[str(number) for number in numbers],
            )
        except RenderError:
            # Back to one picture per page, which is what this reply carried before the
            # sheet: costlier and measurably no better, but a reply with no renders in
            # it sends an author to read every example page as code.
            _log.warning("template: the example-page sheet did not compose", exc_info=True)
            return None

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
                    "`clone_page(prs, prototype(tpl, N))` then `replace_text` per line"
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
                "those pages with `clone_page(prs, prototype(tpl, N))` and `replace_text` per line "
                "instead. Drawing anything of your own into it -- a chart, a panel, "
                "a figure -- means emptying that space first -- `clear_region(slide, page_box(shape_at(slide, n)))` "
                "for where one shape is, or `clear_region(slide, Box.corners(x0, y0, x1, y1))` for a space of "
                "your own, since four bare numbers cannot say which reading they are and are refused -- which "
                "reports every shape it took out and every one still lying over your box. A run that swept the "
                "page by hand instead kept the arrows, the number labels and every connector, and drew two "
                "charts on top of them"
            )
        # Apart from `cannot_be_redrawn`, because the answer is the opposite one. A run
        # was told to clone a chart page and replace_text it, got a chart holding the
        # template's own categories, and spent 73 minutes in a shell measuring the
        # template's axis type so it could rebuild the chart by hand.
        charts = {page.index + 1: page.redraw_yourself for page in sources if page.redraw_yourself}
        if charts:
            payload["charts_to_draw"] = {str(number): list(boxes) for number, boxes in charts.items()}
            asks.append(
                "pages "
                + ", ".join(str(number) for number in sorted(charts))
                + " carry the template's own chart. The layout may be cloned, but the chart must be redrawn "
                "with ppt_charts from your own data and placed in the same position -- `charts_to_draw` is "
                "the box each one occupies, and the page's code below names the two calls. Cloning a chart "
                "keeps the template's numbers and nothing here rewrites them, replace_text included: a "
                "chart's labels are in its own part, not in a text frame, and no gate reads inside a chart "
                "either, so a page shipped that way is not refused"
            )
        if unwritable or charts:
            # The route, where the instruction to write code is. Nine ppt_template
            # replies in one run told an author to clone and to redraw and named no
            # tool for either, and the run wrote 288 identical exec calls instead of
            # a program. `ppt_template.clone_page` is also a module path that reads
            # like this tool's name, which is the other half of the same mistake.
            asks.append(
                "the deck's program is where those lines go, not a call on this tool: write it to "
                "`deck/build/build.py` with write_file, then ppt_build runs it and returns every page it drew"
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


def _cell(stem: str, number: int) -> str:
    """What a borrowed page's cell says, and the key its sentence is written against.

    The template's first word and the page, not a running index: the measured harness
    numbered the cells 1..35 and spent a legend teaching the model to dereference them,
    and the cell can simply say it. Short because it is painted over the page's own
    top-left corner -- eight characters at a 768px cell, against thirty for the stem.
    The first word is what tells the eight bundled templates apart.
    """
    return f"{str(stem).split('_')[0]} {int(number)}"


def _pictured_key(pictured: list[tuple[str, int]]) -> str:
    """A short stable name for exactly this set of pages on a sheet."""
    said = ";".join(f"{stem}:{number}" for stem, number in pictured)
    return hashlib.sha256(said.encode("utf-8")).hexdigest()[:8]


def _by_stem(offers: list[tuple[str, int, str]]) -> dict[str, list[int]]:
    """The offered pages grouped by template, each template's pages in offer order.

    Grouped because rendering is per file: a template converted once answers every page
    of it that is offered, and the offer list walks templates several pages at a time.
    """
    grouped: dict[str, list[int]] = {}
    for stem, number, _ in offers:
        grouped.setdefault(stem, []).append(number)
    return grouped


def _roster(template: Any) -> dict[str, Any]:
    """What the bound template offers a build: canvas, layouts, example pages, colours, fonts."""
    roster: dict[str, Any] = {
        "canvas_in": f"{template.inventory.width_in:g}x{template.inventory.height_in:g}",
        "layouts": [layout.summary() for layout in template.inventory.layouts],
        "example_pages": template.example_pages,
        "build_from": "os.environ['PPT_TEMPLATE']",
    }
    if template.inventory.theme_colours:
        roster["theme_colours"] = dict(template.inventory.theme_colours)
    if template.inventory.fonts:
        roster["fonts"] = list(template.inventory.fonts)
    return roster


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


# The page stays on offer -- its arrangement is exactly what an author wants a chart
# page for -- and the offer carries what the clone does not deliver. Said beside the
# render because that is what a prototype is chosen from: the run that cloned a chart
# page had picked it here and read this line, which said nothing about the chart.
_CHART_IS_YOURS = (
    ". The {noun} on it {verb} the template's own, at {where}: the layout may be cloned, but the chart must "
    "be redrawn with `ppt_charts` from your own data and placed in the same position. A cloned chart arrives "
    "holding the template's numbers and nothing here rewrites them -- `replace_text` reaches text frames, and "
    "a chart's labels are not in one"
)


def _chart_clause(charts: tuple[str, ...]) -> str:
    """What the page owes the author, or "" where the page draws no chart."""
    if not charts:
        return ""
    one = len(charts) == 1
    return _CHART_IS_YOURS.format(
        noun="chart" if one else f"{len(charts)} charts",
        verb="is" if one else "are",
        where=" and ".join(charts),
    )


def _render_label(number: int, role: str | None, entry: Any) -> str:
    """The line above a template page's render: what the page is, and how to start from it.

    The shape sits beside the picture on purpose. The bind reply used to name the
    arrangements in one list and show the renders under bare numbers, and the plan
    that followed was written many turns later from memory of the pictures; a live
    run then composed seventeen of seventeen content pages inside a template with
    eleven content examples. What a reader decides from is the picture, so the words
    that make it a choice -- its shape, its slots, the call that takes it -- go next
    to the picture.
    """
    charts = tuple(getattr(entry, "charts", ()) or ()) if entry is not None else ()
    if role:
        return f"Template page {number} -- the template's {role}" + _chart_clause(charts)
    shape = getattr(entry, "arrangement", "") if entry is not None else ""
    slots = getattr(entry, "slots", 0) if entry is not None else 0
    said = f"Template page {number} -- a content example"
    if shape:
        said += f": {shape}" + (f", {slots} slots" if slots else "")
    places = tuple(getattr(entry, "picture_slots", ()) or ()) if entry is not None else ()
    if places:
        said += (
            f". Picture slots: {', '.join(places)} -- what goes in each is yours to decide: a figure from the "
            "sources, a photograph, or `ppt_generate_image(..., transparent=true)` for an illustration that "
            "sits on the page's own ground; `replace_picture(shape_at(s, n), FIGURES/'x.png')` puts it there"
        )
        if any(" cut-out" in place for place in places):
            said += (
                ". A cut-out slot is a transparent drawing floating on the ground, and its box runs wherever "
                "the drawing does -- into the title row, over a band; a photograph there wants a box of its "
                "own clear of the copy, `replace_picture(shape, image, box=(left, top, width, height))`, or a cut-out of "
                "its own from `ppt_generate_image(..., transparent=true)`"
            )
        if any(" icon" in place for place in places):
            said += ". A" + ICON_SLOT_NOTE[1:]
    said += _chart_clause(charts)
    return said + (
        f". A page of this information shape starts here: `s = clone_page(prs, prototype(tpl, {number}))`, then "
        f"one `replace_text(s, old, new)` per line the page says"
        f"{', `remove_unit` for the slots it does not fill' if slots else ''}"
        f"{', `replace_picture` for the figure' if places else ''}"
        f". Record `prototype: {number}` on it in the plan"
    )


def _page_notes(source: Path, count: int) -> tuple[dict[int, tuple[str, ...]], dict[int, tuple[str, ...]]]:
    """What each example page holds that plain code does not answer: page -> what.

    Two answers off one pass -- the shapes cloning keeps, and the boxes of the charts
    cloning does not. Every page, not the ones asked for: the verdict an author needs
    at the moment of choosing is "9 of 13", and a per-request answer cannot say that.
    No images are written because none are wanted here.
    """
    unwritable: dict[int, tuple[str, ...]] = {}
    charts: dict[int, tuple[str, ...]] = {}
    for number in range(1, count + 1):
        page = decompile(source, number - 1)
        if page is None:
            continue
        if page.unredrawable:
            unwritable[number] = page.unredrawable
        if page.redraw_yourself:
            charts[number] = page.redraw_yourself
    return unwritable, charts


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
