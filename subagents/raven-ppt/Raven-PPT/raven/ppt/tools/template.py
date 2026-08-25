"""`ppt_template`: build inside the template the user gave us.

What a template is for turned out to be two different things, and treating them as
one produced the decks that got sent back.

A template's **structural pages** -- its cover, its contents list, its section
divider, its closing -- are the pages a reader recognises the house by, and they
are cloned. A deck that draws its own cover announces itself as not the user's
before a word of it is read.

Its **content pages** are not prototypes for this deck's content, and three live
runs proved it. The slots on those pages are drawn for the phrases a template ships
in them ("单击添加小标题"), so a review deck's real sentences arrive in boxes that
shrink their own text: measured on one deck, four cards holding one sentence each
came out at 11.7, 13.7, 13.5 and 10.8pt from a single 18pt slot. A six-card grid
filled with four cards leaves a hole where the other two were. A portrait picture
frame cannot take a landscape figure, and the author, given no way to reshape it,
spent three rounds permuting shape numbers. None of that is the template's fault:
it is what filling a form does to content that is not the shape of the form.

So the content pages are not offered. What comes back instead is the house style
measured off them -- the layout their background lives on, where the title row sits,
the type ladder the master declares, the face its pages actually use, the area it
keeps content inside -- and the author draws each page inside that. The template
supplies the style; the content decides the layout.
"""

from __future__ import annotations

from dataclasses import is_dataclass, replace
from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool, ToolResult
from raven.ppt.contracts import Project
from raven.ppt.services.template import bind, bound, decompile
from raven.ppt.services.template.house import house_style
from raven.ppt.services.template.menu import menu, roles
from raven.ppt.tools import _return
from raven.ppt.tools._args import ArgumentError, as_ints
from raven.utils.helpers import image_block, text_block

# Every example page is reachable, in batches. A cap was the first answer and it
# was indefensible: the templates measured ship thirteen pages, the cap was twelve,
# and the thirteenth was simply never shown -- an author choosing a page to adapt
# could not choose the one it could not see. So the batch bounds the request body
# and the reply says how many remain and how to ask for them.
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
        "Build this deck inside a .pptx template the user supplied. Call it with the file's path to "
        "bind it: the deck is then built in a copy of that file, so the template's master, theme, "
        "layouts, fonts and canvas are this deck's house style. What comes back is the template's own "
        "cover, contents, section divider and closing as renders -- clone those -- and the house style "
        "every other page keeps: the layout to add a page on, where the title row sits, the type "
        "ladder in points, the face, and the area to stay inside. The content pages are yours to "
        "compose: nothing about your page has to match the shape of one of the template's."
    )
    timeout_seconds = 300.0

    def __init__(self, workspace: Path, views: Any) -> None:
        self.workspace = workspace
        self.views = views
        self.thumbnails = replace(views, dpi=RENDER_DPI) if is_dataclass(views) else views

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "description": "the deck project, as given to ppt_ingest"},
                "path": {
                    "type": "string",
                    "description": (
                        "path to the user's .pptx, relative to the workspace. Give it once to bind the "
                        "template; leave it out afterwards, the deck stays bound to it"
                    ),
                },
                "pages": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "maxItems": MAX_SOURCE_PAGES,
                    "description": (
                        "which of the template's structural pages to read as python-pptx source -- its "
                        "cover, contents, divider or closing, by the numbers the reply names. Its content "
                        "pages are not offered: you compose those"
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
        template = rebound or already
        if template is None:
            error, detail = self._nothing(path)
            return _return.failed(error, **detail)

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

    async def _as_renders(self, deck: Project, template: Any, payload: dict[str, Any]) -> str | ToolResult:
        """The template's structural pages as pictures, and the style the rest keep."""
        folder = deck.review_dir / "template"
        listing = menu(template.source, _unwritable_pages(template.source, template.example_pages))
        named = roles(listing)
        pdf = await self.thumbnails.pdf(template.source, folder)
        wanted = sorted(named.values())
        renders = await self.thumbnails.pages_of(pdf, folder, wanted) if pdf is not None and wanted else {}
        # The render is read twice: once as pictures for the author, once as type sizes
        # for the house style. A template's title placeholder usually declares no size
        # at all, so without the render the ladder is a guess -- and the conversion has
        # already been paid for here.
        house = house_style(template.source, listing, pdf)
        if named:
            payload["house_pages"] = {role: number for role, number in named.items()}
        if house is not None:
            payload["house_style"] = house.brief()
        if listing:
            payload["structural_pages_are"] = [entry.line() for entry in listing if entry.role]
            payload["content_pages"] = (
                f"{len(house.content_pages) if house else 0} of the template's pages are content pages. "
                "They are not offered as prototypes and not rendered here: their slots are drawn for the "
                "placeholder phrases they ship with, and filling them with real copy produced pages with "
                "holes in the grid, body copy autofitted to 10.8pt, and figures that did not fit the frame. "
                "Compose your own inside the house style above"
            )
        asks = []
        if named:
            asks.append(
                "clone the template's structural pages -- "
                + ", ".join(f"its {role} is page {number}" for role, number in named.items())
                + " -- with `from ppt_template import adapt, prototype` then "
                "`adapt(prs, prototype(tpl, N), texts={...})`, where tpl is Presentation(the template's own file). "
                "Those four are the deck's house frame and are not redrawn"
            )
        if house is not None and house.layout:
            asks.append(
                f"draw every other page yourself: `prs.slides.add_slide(layout)` on the {house.layout!r} layout "
                "so the page inherits the template's background, put the page title in the title row exactly "
                "as given above, and lay the content out inside body_area_in with `ppt_layout` -- Box.columns, "
                ".rows, .grid, plane(), write(), table(). The arrangement is yours to decide from what the page "
                "has to say"
            )
        if house is not None and house.scale:
            asks.append(
                "one scale for the whole deck: every page title at "
                f"{house.scale.get('title', 28):g}pt, body copy at {house.scale.get('body', 18):g}pt, and "
                "nothing under 14pt. Same size for the same role on every page -- a reader reads each page "
                "against the one before it, and a deck whose body size moves page to page reads as unfinished"
            )
        asks.append(
            "open the deck with Presentation(os.environ['PPT_TEMPLATE']) -- the template with its example "
            "pages removed, so a page you add inherits its master, theme and canvas"
        )
        if named and not renders:
            payload["renders"] = "unavailable on this machine; ask for the structural pages as code instead"
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
        listing = menu(template.source)
        named = roles(listing)
        structural = set(named.values())
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
        refused = [number for number in asked if structural and number not in structural and number not in beyond]
        if refused and not any(number in structural for number in asked):
            return _return.failed(
                f"page(s) {refused} are content pages, and those are not handed out as prototypes",
                hint=(
                    "the structural pages are "
                    + ", ".join(f"{role}={number}" for role, number in named.items())
                    + ". For a content page, compose your own inside the house style ppt_template "
                    "returned: add_slide on its layout, the title row where it says, the type ladder "
                    "it gives, everything inside body_area_in"
                ),
            )
        asked = [number for number in asked if (not structural or number in structural) and number not in beyond]
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
        if refused:
            payload["pages_refused"] = refused
            asks.append(f"page(s) {refused} were not read: they are content pages, which you compose rather than fill")
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
