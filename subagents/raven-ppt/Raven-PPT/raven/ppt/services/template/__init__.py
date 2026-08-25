"""Working inside a template the user gave us.

The route's author writes a python-pptx program, so a template reaches it as
three things: a copy to build in, the code that drew its example pages, and the
operations python-pptx has no API for.

`prepare` gives the copy -- the same file with its example slides removed, so the
master, the theme, the layouts and the canvas all come across and a page added to
it inherits them. Every template in a sample of thirty ships example slides,
thirteen each, and an author that opens the original and starts adding pages
produces thirteen of somebody else's content plus its own.

`decompile` gives the code. This is the part that decides whether a template is
usable at all: measured on twenty real templates, 85% of their visual elements
live on those example slides rather than on the layouts -- 29 per template
against 5 -- so a page built from `add_slide(layout)` gets the placeholders and
almost none of the design. Reading the example page back out as python-pptx puts
the template's real geometry, colours and type in front of an author who then
writes its own page, and a six-card grid becomes an eight-card one by changing a
loop bound.

`compose` is the part the code cannot reach. Two thirds of real template pages
hold something python-pptx has no way to write -- custom geometry, a gradient, a
fill at 60% opacity -- so for those pages the only route into a deck is to clone
the page and edit it: `clone_page`, then `replace_text` and `replace_picture` to
put this deck's content in it, then `drop_shape` for what is left over. Each is
an operation python-pptx does not offer and each has a way of going quietly
wrong, which is why they are here rather than in an author's program.

Two things were measured here rather than assumed, and both changed the design.
A master's or a layout's own decoration does not travel onto a slide, so the band
a corporate template paints across its header is invisible to every check that
reads the built `.pptx` -- which means using the user's template is not refused
for the user's own design, and also that such a band can sit under type nothing
will see it collide with. And a slot-and-capacity matcher was built here first
and then deleted: it bounded a deck at what the template's own pages happened to
hold, which is the opposite of what a program-writing author needs.
"""

from raven.ppt.services.template.bind import BoundTemplate, bind, bound
from raven.ppt.services.template.compose import (
    adapt,
    arrangement,
    boxes,
    clone_page,
    drop_shape,
    fill,
    helper_source,
    place,
    prototype,
    replace_picture,
    replace_text,
    shape_at,
    units,
)
from raven.ppt.services.template.decompile import PageSource, decompile
from raven.ppt.services.template.house import House, Row, house_style
from raven.ppt.services.template.inventory import (
    TemplateInventory,
    TemplateLayout,
    inspect_template,
    template_dir,
    template_path,
)
from raven.ppt.services.template.prepare import PREPARED_FILE, Prepared, prepare, prepared_path
from raven.ppt.services.template.theme import theme_name, theme_of
from raven.ppt.services.template.defaults import (
    DEFAULT_TEMPLATES,
    DefaultTemplate,
    default_template_catalog,
    default_template_prompt,
    fallback_default_template,
    find_default_template,
)

__all__ = [
    "House",
    "Row",
    "house_style",
    "adapt",
    "prototype",
    "shape_at",
    "units",
    "place",
    "fill",
    "boxes",
    "arrangement",
    "PREPARED_FILE",
    "BoundTemplate",
    "PageSource",
    "Prepared",
    "TemplateInventory",
    "TemplateLayout",
    "bind",
    "bound",
    "clone_page",
    "decompile",
    "drop_shape",
    "helper_source",
    "inspect_template",
    "prepare",
    "prepared_path",
    "template_dir",
    "theme_name",
    "theme_of",
    "replace_picture",
    "replace_text",
    "template_path",
    "DEFAULT_TEMPLATES",
    "DefaultTemplate",
    "default_template_catalog",
    "default_template_prompt",
    "fallback_default_template",
    "find_default_template",
]
