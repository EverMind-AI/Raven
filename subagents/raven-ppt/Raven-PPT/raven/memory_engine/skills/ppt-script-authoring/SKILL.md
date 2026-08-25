---
name: ppt-script-authoring
description: "Design and build a deck as a python-pptx program: your own layout system, the source's own figures, charts you draw, and a template's design when the user gave one."
metadata: {"raven":{"emoji":"🎞️","always":true}}
---

# Deck authoring

You design the deck and you build it, by writing a python-pptx program that
`ppt_build` runs. Every page is yours: the grid, the type scale, the palette, the
page furniture, where a figure sits and how large. Nothing places anything for you
and nothing second-guesses what you placed.

This document owns the craft. The tool schemas own their fields and the gates own
what is refused; neither is restated here, because a rule written twice is a rule
that will eventually disagree with itself.

## 1. Define the communication job

Infer the audience, the deck's job, the outcome you want, the central takeaway and
the evidence that supports it. Build a cumulative argument rather than an inventory
of topics: each content page performs one narrative job and makes one primary claim.

Write audience-facing titles that state the point. Open with the context, question
or stakes that make the deck worth sitting through. Close by resolving that opening
— a decision, an implication, a next question. A separate closing page is optional:
add one when the user asks for it, otherwise let the last content page carry the
conclusion.

Pack the evidence before allocating pages. A normal page carries one claim and two
to four developed support units, and across them covers at least two of: evidence from
the source, mechanism or comparison, implication or constraint. If the evidence
cannot support a complete argument, merge the page rather than enlarging fragments.

The language, the audience and the length are the user's, and they are asked for
rather than inferred. `ppt_prepare` reads what the request already states and hands
back the rest as questions; put those to the user with `ask_user` and record the
answers with `ppt_brief`. All three are then measured against the finished file, so
a brief nobody agreed to is worse than no brief at all — it fails the deck against a
decision the user never made.

## 2. Write the outline before you write the program

`ppt_outline` records it, and the build refuses until it exists. This is the stage
that decides how much is on a page, and the reason it is a stage: without one, what
a page said got decided while its geometry was being typed, and the decks that came
out were thin — a title and three short lines, eight times over.

Per page: the **claim** as a statement (which is also its title), what **carries**
it, the **figures** it places by id, and what it **says** in the deck's language, as
it will read on the page.

That last field is where density is decided, and it is the field that gets
under-written. A page holds around 450 characters of copy comfortably — that is a
five-row comparison, two takeaways and a caveat, and it is a good page. Plan for
that. Points that are each half a line add up to a page that is a title and some
labels, and the build cannot rescue it: geometry can arrange copy, never supply it.
So write each point as the sentence a reader gets, evidence included, and if a page
has only one short line in it either the evidence for it belongs there too or the
page belongs merged into its neighbour.

Two things are checked there rather than after the build, which is the difference
between a cheap edit and an expensive one: figure ids against the catalogue, and the
page count against the brief. Whether a number traces to the materials is not checked
anywhere -- ground every number in a source because a reader will, not because
something will stop you.

And `needs` is where gathering belongs — this is the first moment anything knows
what each page will show. Go through the outline page by page and name the picture
each one wants, then find it with `web_search(kind="images")` and bring it in with
`ppt_fetch`. A fetch joins the deck's own source set and is read on arrival, so there
is no separate ingest to remember; `ppt_ingest` is for bringing in another directory
of the user's, or re-reading after you have edited a source by hand.

This is a step of the route, not a repair. A deck whose only imagery is what the
materials happened to contain is a deck of panels: the products it names have logos,
the organisations have marks, the standards have badges, and the architectures have
diagrams their own documentation draws. A page asserting something about a named
thing reads differently beside that thing's own mark than beside a coloured
rectangle. Search for those before deciding a page is text.

## 3. One visual identity, defined once

Take the palette and font from a reviewed theme. They are written into the build
directory on every build, so the import is plain — that directory is already on
`sys.path`:

```python
from ppt_theme import THEMES, rgb

T = THEMES["warm-paper"]
BG, SURFACE = rgb(T["background"]), rgb(T["surface"])
INK, MUTED, ACCENT = rgb(T["foreground"]), rgb(T["muted"]), rgb(T["accent"])
SOFT, GRID = rgb(T["accent_soft"]), rgb(T["grid"])
SERIES = [rgb(c) for c in T["chart_series"]]     # six, in order
FONT = T["font_family"]
```

**Do not write a module into that directory whose name shadows a standard one.** A
`copy.py` beside your script breaks python-pptx's own import, and the traceback will
not mention your file.

Ten themes ship, every one a white page; they differ in the tint their planes take
and in the accent:

- `archive-sepia`: cream planes, sepia accent
- `indigo-scholar`: pale indigo planes, indigo accent
- `ink-graphite`: cool grey planes, cobalt accent
- `moss-field`: pale green-grey planes, moss accent
- `oxblood-press`: warm blush planes, oxblood accent
- `plum-editorial`: mauve planes, plum accent
- `sage-clinical`: pale green planes, pine accent
- `steel-engineering`: cool blue-grey planes, steel-blue accent
- `terracotta-craft`: warm clay planes, terracotta accent
- `warm-paper`: warm sand planes, walnut accent

Each is reviewed: the accent clears contrast on its own ground, the face is one the
renderer will not substitute at a different width, and the six data colours hold
apart. Pick the one whose character suits the subject, use it for the whole deck,
and invent no colours beside it. With a template bound there is exactly one entry
and it is the template's — and the build refuses a deck whose theme is not it.

**The roles are fixed, so keep them fixed on the page.** `foreground` is body type,
`muted` is secondary type and source lines, `accent` marks the focal point of a page
and nothing else, `surface` and `accent_soft` are planes content sits on, `grid` is
for hairlines. If the accent marks "our result" on one page it cannot mark "prior
work" on the next.

**`accent` fills; `accent_ink` writes.** `accent_soft` is the accent mixed towards
the background, so a pale accent cannot be read on its own tint. A theme built from
a template carries `accent_ink` for this: the same hue, dark enough to write with,
or the accent itself where that already reads. Fills, markers and bars take
`accent`; a number or heading in the accent's colour takes `accent_ink`.

**One font family, and its CJK companion.** With no template, one of the six measured faces: Arial, Helvetica, Times New Roman, Cambria, Century Schoolbook, Bookman Old Style — anything
else is substituted on export and every position you computed is wrong. With a
template, the face `ppt_theme` hands you is the template's own, whatever it is
(微软雅黑, say), and that is the right one to use: it is the house style, and the
width measurement degrades to an estimate for it rather than failing.

For a deck in Chinese that is only half the answer. `font_family` is a Latin face,
and Han characters set in one fall back to whatever the viewer has — a different
design at a different weight, and on the review renderer no Han glyphs at all. Every
theme carries `cjk_font_family` beside it, matched to its class (`Noto Serif CJK SC`
for the serif themes, `Noto Sans CJK SC` for the sans ones), and both names go on
the same run: `write(..., font=FONT, cjk_font=HAN)` from `ppt_layout` does it, or
set `a:ea` yourself. Then 目标查询 is set on purpose and `TarViS` in the same line
stays in the Latin face.

**A type scale with a big step at the top and fine steps below.** Same size for the
same role on every page. A 16.5pt title over 16pt body is not a scale; it is no
hierarchy at all. Two numbers in it are measured rather than chosen, because a deck
is read projected: the size a page mostly runs at stays at or above **14pt**, and
nothing on a page — table cell, caption, chart tick, source line — goes under
**10.8pt**. Every page that breaks this comes back with the number on it. If the
copy will not fit above the floor, the page has too much on it: merge the bullets
that state one thing, split the page, or move a section elsewhere. Shrinking the
type is not the fix; it is how the page stopped being readable.

**One filled bar is the deck's own**: the band a content page's title row sits on,
full width, across the top, with the title on it. Every other one is decoration and
the build refuses it — a band across the middle of a page, a strip welded to a card
edge, a rule of accent colour under every title. They carry no content, they compete
with what they border, and repeating one on every module is the loudest tell of
generated design. A hairline in the grey you already use for secondary text is
enough where a page genuinely wants a divider. A row of bars encoding values is not
a bar in this sense and is never flagged.

## 3.5 Structure follows the content

Decide what carries the point before placing anything:

| Content logic | What carries it |
|---|---|
| Real product, interface, experiment or published plot | a source figure |
| Source-backed numeric comparison or trend | a chart you draw |
| Tabular data or comparable rows and columns | a drawn table or comparison matrix |
| Familiar capabilities or categories | icon-led regions |
| Sequential steps or milestones | numbered or dated regions |
| Two genuinely contrasted alternatives | paired regions on one baseline |
| One memorable conclusion | a single dominant statement |
| One dominant number | the number at display size, reasoning beside it |

Then compose around it. Give the load-bearing element the space its role deserves
and let the rest defer; a page where every region carries equal weight has argued
nothing. Vary composition because the content varies, not to fill a quota, and keep
a series of comparable cases on one shape so the reader can compare them.
Below the title, establish at least two visible levels: the main argument or visual,
then the evidence that supports it. Give a conclusion or decision its own baseline,
weight or restrained accent instead of styling every component identically.

Prefer one coherent composition over a dashboard of unrelated panels. Cards are for
genuinely parallel, same-level items. Asymmetry is for when one region is logically
primary. Do not draw boxes merely to avoid a prose page.

Cards, planes and helper presets are optional primitives, not a required page grammar.
The model may write a typographic page, a dominant figure, a drawn table, a comparison matrix, a chart, a timeline,
a process, a comparison on a shared baseline, or a free composition when that better
matches the argument. Do not wrap every component in a filled rectangle just to make
its boundary visible; use hierarchy, alignment, whitespace and meaningful rules first.

**Fill the page.** A 13.3 x 7.5in canvas holds far more than a title and four
bullets, and content that stops two thirds of the way down is the most common
failure there is. Whitespace you chose is a margin around content that fills the
frame; whitespace left over reads as unfinished.

When the source already carries a real table, comparable rows or a set of parallel items
with several attributes, keep that information shape. Draw it as aligned text regions
and rules; do not flatten it into two bullet columns because the text happens to fit.

On a figure-and-text page, let the figure dominate and turn the supporting copy into
two to four scan points with clear labels. Put those points on one or more restrained
theme-coloured surfaces so the image and explanation read as distinct layers; do not
leave a bare paragraph floating beside the figure. The cards defer to the figure --
they are supporting structure, not four equal dashboard tiles.

Every placed figure carries a concise caption. Use its source caption when present;
otherwise use the `visual_caption` written by `ppt_figure_inspect`, and keep source
provenance separate. A caption says what is shown, not a claim the pixels do not prove.

## 4. The program

It goes at `ppt_projects/<project>/build/build.py` — that whole path, relative to the
workspace; `ppt_prepare` reports it as `write_the_program_to`. A bare
`build/build.py` lands somewhere the build does not look. `write_file` creates it,
`write_file` with `mode="append"` extends it, `edit_file` revises it. Never restate
the whole file to change part of it.

**Write it in pieces, not in one call.** `write_file` for the setup and the first
three or four pages, then `mode="append"` for two or three pages at a time, and
`ppt_build(draft=true)` in between: a draft builds what exists, measures it and hands
back the renders, without holding a part-written deck to the length that was agreed
and without publishing. Drop `draft` when the deck is whole and you want the gates.

Two reasons, and the second outlives the first. The gateway in front of this model
closes a connection that streams one very long reply, so a twenty-page program
written in one call gets cut before its tool call arrives -- and sending the same
thing again gets cut in the same place, which is why four runs died there. That part
is a transport limit and will go when the transport does. What stays is that a draft
shows you a page while there are three of them rather than twenty: every fix is one
page wide, and the pages after it are written already knowing what the last one
looked like.

It runs in the build directory with python-pptx and Pillow, and reads its paths from
the environment: `PPT_OUTPUT` (save there and nowhere else), `PPT_FIGURES_DIR`
(the figures ingest extracted), and — when a template is bound — `PPT_TEMPLATE` and
`PPT_TEMPLATE_SOURCE`.

**One block per page, opened with a `# SLIDE <n>` banner.** Shared helpers above the
blocks are what you want; the page's own composition belongs in the page's own
block. Two shapes are refused: a `build.py` that runs another file, and a loop that
draws every page from one call site. Both leave no code belonging to one page, and
every per-page thing the build does — matching a render to the code that drew it,
reporting a defect against the block that caused it, improving one page without
touching another — has nothing to hold on to.

Then write the helpers *your* pages need. What you must not do is build one header
routine and push all eighteen pages through it: a deck where every page opens with
the same kicker, the same title, the same rule and the same bullet row is a template
— you just wrote it yourself instead of loading one. Beyond the title, the source
line and the page number, page types have no reason to share a skeleton. A cover, an
agenda, a two-way comparison, a table page, a full-bleed figure and a closing page
should each be recognisably a different kind of page.

**Put a `*` after the geometry in every helper you write.** `def card(slide, x, y,
w, h, *, fill=SOFT, bold=False, align="left")` — the boxes stay positional because
they are always the same four numbers in the same order, and everything after them
is named at the call site. Two runs wrote twenty-seven helpers between them and not
one did this; several took seven or eight positional parameters, and miscounting
them was the largest single cause of a build that would not run at all: an empty
string arriving where a bold flag goes, an argument short, a keyword the function
never had. It also costs pages later — the design pass rewrites one block at a time
and calls the helpers you wrote, so a helper it can miscount is a page whose
improvement gets dropped for a `TypeError`. The helpers in `ppt_layout` are shaped
this way, which is why a call to them cannot fail this way.

**The agenda is the page that goes wrong most often.** It is a map of the argument —
five to seven movements the talk makes, each named with one line saying what it
settles. It is not an index of slide numbers: "3 Introduction, 4 Limitations, 5
Overview…" tells the audience nothing the page numbers do not, and it starts at 3
because it is enumerating the file rather than the talk.

**Give a text box more width than the text needs.** This is the single commonest
geometry bug in a generated deck: a box sized to what the string looks like wraps it
onto a second line, the second line pushes into whatever sits below, and the page
reads as broken rather than as tight. Extra width costs nothing — an unfilled text
box is invisible, it has no fill and no outline, and a left-aligned line starts in
the same place whether its box ends at 4in or 7in. So when in doubt make the box
wider, not tighter, and reach for `word_wrap = False` on anything that must stay on
one line: a number, a label, a kicker, a card heading. The build reports a label in a
box too narrow to hold it, but by then the page has already been built wrong.

The agenda's geometry breaks the same three ways every time, so compute it:

- **A two-digit number needs a box that fits two digits.** `01` and `10` fold into
  two stacked characters in a box sized for `1` — the same failure as above, in its
  most common disguise. Set `word_wrap = False`, or give the box the width the widest
  label needs rather than the width the first one happens to need.
- **Lay repeated rows on a pitch.** With `n` rows in height `H` the pitch is `H / n`,
  the label sits at `y = top + i * pitch`, its description at a fixed offset below.
  Guessed offsets are what puts a title on top of the line under it.
- **A label and its description are one block.** If the description can run to two
  lines, the pitch has to allow for two, or row `i + 1` lands on top of it.

The same three apply to any page built from repeated rows or cards.

### The helpers, by signature

Everything below is already beside your script. Read this table rather than the
modules: their source runs to 19k tokens and stays in context for the rest of the run.

`ppt_layout`, always present:

| | |
| --- | --- |
| `page(kicker=True, footer=True)` | → `Frame(kicker, title, body, footer)`, four boxes inside the safe area |
| `Box(x0, y0, x1, y1)` | **two corners**, not a size — see the warning below |
| `Box.at(x, y, w, h)` | a box from a corner and a size, python-pptx's own convention |
| `box.rows(n, gutter=GUTTER, weights=None)`, `box.columns(...)` | n boxes filling this one |
| `box.grid(cols, rows, gutter=GUTTER)` | row-major cells |
| `box.split_left(fraction, gutter=GUTTER)`, `box.split_top(...)` | two boxes, the first taking `fraction` |
| `box.inset(dx=PAD, dy=None)` | a smaller box inside this one |
| `stack(box, gutter=GUTTER)` | a cursor down a region: `.take(height)`, `.rest()`, `.skip(height)`, `.left` |
| `picture_fit(slide, image, box, theme, *, caption=None, size=LABEL_PT, align="center")` | a picture scaled to fit the box whole, centred, caption under it |

> **`Box` takes two corners; everything else in your script takes a size.**
> `add_textbox`, `add_shape`, `add_picture` and any helper you write yourself all
> take `left, top, width, height`. `Box` takes `x0, y0, x1, y1`. Writing
> `Box(0.82, 1.5, 4.25, 2.6)` when you meant a 4.25×2.6 box gives you one that ends
> at x=4.25. And the mistake compounds: having seen it in the render, the repair to
> reach for is `Box.at(x, y, w, h)` — not "corrections" to your own helpers, which
> take a size and were right all along.
| `heading(slide, frame, theme, title, kicker=None, *, tint="surface", underline=True, bleed=True)` | §6.5 |
| `write(slide, box, text, *, size=BODY_PT, colour="#000000", font=None, cjk_font=None, bold=False, align="left", anchor="top", spacing=1.15)` | `text` may be a list of paragraphs |
| `points(slide, box, theme, items, *, size=BODY_PT, numbered=False, mark="•", colour=None, mark_colour=None, spacing=1.25)` | §6.5 |
| `card(slide, box, theme, *, icon=None, title="", body=(), tint="surface", size=LABEL_PT, title_size=BODY_PT)` | §7 |
| `formula(slide, box, text, theme, *, size=BODY_PT, align="left", anchor="top")` | §9 |
| `plane(slide, box, theme, tint="surface", radius=False)` | a painted region |
| `rule(slide, box, theme, thickness=0.03, colour=None)` | the hairline under a title |
| `overlaps(boxes, tolerance=0.01)` | → the `(i, j)` pairs that overlap |

Constants: `CANVAS_W`, `CANVAS_H`, `MARGIN`, `GUTTER`, `PAD`; the ramp `TITLE_PT`,
`LEAD_PT`, `BODY_PT`, `LABEL_PT`, `KICKER_PT`, `NUMBER_PT`, `BODY_FLOOR_PT`.

`ppt_template`, only when a template is bound:

| | |
| --- | --- |
| `prototype(template, number)` | the template's page `number`, counting from 1 |
| `adapt(presentation, prototype, texts=None, pictures=None, drop=(), keep=(), items=None, title=None, subtitle=None)` | clone a page and fill it in; §8 |
| `units(container)`, `boxes(run)`, `arrangement(run)`, `place(unit, box)` | the page's repeating units, where each sits, how the run is laid out, where to move one |
| `shape_at(slide, number)`, `drop_shape(shape)` | one shape by index, counting from 1; remove it |
| `replace_text(target, text, new=None)`, `replace_picture(shape, image, fit="contain")` | in place, keeping how the template set it |
| `clone_page(presentation, prototype)` | when `adapt` is more than the page needs |

**Every content page uses the shared title construction.** Call
`frame = page()` followed by `heading(slide, frame, T, title, kicker=...)` (or use the
template's measured `title_row_box_in` with the same kicker/title/rule construction).
The title starts at the top of that row, keeps the same left edge and height on every
page, and has the shared surface/rule decoration. Do not create a second `title_row`
helper with guessed coordinates, a floating title on bare white, or a title box below
the measured row. The title row is part of the deck's visual identity, not optional
page furniture.

Four of those have a second question a signature cannot answer, because a signature
says what to pass and none of these is about what to pass:

- `replace_text(shape, "新文字")` writes one shape. `replace_text(slide, "旧文字", "新文字")`
  finds whatever on the page holds that string and writes it — the form to reach for,
  because finding the shape is the tedious half.
- `replace_picture(shape, image, fit)` — `"contain"` shrinks the frame to the picture's
  own proportions; `"cover"` crops the picture to fill the frame as it stands. Either
  way it refuses a landscape figure in a portrait frame rather than squashing it.
- `units(container)` returns runs of repeating sibling groups — a card row, an agenda list.
- `arrangement(run)` returns `("row"|"column"|"grid"|"irregular", rows, cols)`. Nothing
  re-flows a page for you: drop units from a run and you decide what the survivors do.

`ppt_theme` gives `THEMES` and `rgb`. `ppt_icons` gives `add_icon(slide, name, left,
top, size, colour, width_pt=1.75)`, `find_icons(term)` and `ICON_NAMES`.

**`adapt(items=...)` in detail**, because three consecutive requests of a live run
went into working it out. One entry per repeating unit, and the units left over are
deleted. Each entry is a list positional over that unit's text shapes, or a dict
keyed by the text a shape holds now. In a list: `None` keeps the template's own
words, a string replaces them, and `""` empties the shape — **except over a number,
where `""` means "this unit's number" and the slot is renumbered for its position**,
in the template's own padding. That is what an agenda wants: six sections in a page
that ships eight numbered slots come out numbered 01 to 06.

## 5. Figures come from the sources

`ppt_ingest` extracts them and nothing generates them. Never generate a substitute
for a real product UI, logo, person, scientific result, published figure or
statistical claim.

It reads PDFs, text, HTML, CSV, images, and office documents -- `.docx`, `.xlsx`,
`.doc`, `.odt` -- by converting those to PDF first, which needs LibreOffice. Anything
it could not read comes back as a finding naming the file: a source that is not among
the deck's evidence is one you must not write pages as though you had read.

**Look before you place.** `ppt_figure_inspect` returns a figure as an image with
the label its own source gave it, the width past which the bitmap softens, and the
`concerns` the extraction recorded — measured sentences you can act on or overrule,
not a verdict. A figure placed unseen is a page that gets rebuilt: you cannot
otherwise tell a legible plot from a scanned blur, a figure from a logo that
survived extraction, or a composite you should be cropping.

**Cite the label the source printed**, and only when that figure is the one on the
page. A page captioned "Fig. 4" showing Figure 5 is refused, and guessing the number
off the picture is how that happens.

**Keep source notes short.** Put one compact line at the bottom, such as
`来源：EverOS 官网；Mem0 官方文档。` or `来源：Mem0 论文 Figure 2。` Keep URLs,
methodology caveats and analytical conclusions in the body or speaker notes. A
source note is a pointer, not a paragraph; never spend two or three lines repeating
the page's argument or listing every source consulted.

A table in the sources is evidence to read, not a figure to place: retype it (§6).
Never redraw values you cannot read off a plot.

Never distort aspect ratio, crop away interpretive labels, include a neighbouring
caption by accident, duplicate a printed caption, or leave transparency composited
onto black. If evidence is unreadable at the size it has: crop to the panel you
cite, give it more of the page, or rebuild it from the exact values.

Fetched pictures are first-class, not a fallback. Run two discovery paths beside each
other. For every URL the source report cited, `web_fetch(extractMode="images")` lists
the page's own figures, screenshots and preview image; at the same time,
`web_search(kind="images")` searches for anything those citations do not hold.
`ppt_fetch` brings a selected candidate into the deck's sources and reads it, and the
URL travels with it so a page can credit where it came from. Reach for these paths
whenever a page names something that has a face — a product, a company, a standard,
a chart somebody else published — rather than only when the materials left a hole.
The results carry pixel dimensions, so a mark that would land soft on the page can be
rejected before it is placed. Generate with `ppt_generate_image` only after both paths
find no suitable existing visual; a generated image illustrates a concept and never
replaces evidence. Fetched *numbers* are another matter: those are a source the user
did not choose, so anything the deck states as fact still comes from the materials.

A cover does not need a figure. Reaching for one is a habit: the cover's job is the
title, who wrote it and where, and a paper's Figure 1 pressed into its corner is
smaller than the page it will get later.

## 6. Charts and tables, drawn

Nothing ships for charts — no chart library, and matplotlib is not among this
install's dependencies. You draw them, which keeps them editable and on-palette.

Pick the form from the analytical question: ranking → sorted horizontal bars;
category comparison → grouped bars; composition → stacked bars; ordered trend → a
line; two quantities → scatter; exact compact lookup → a drawn table.

For the compact bars that sit beside a table or under a claim, a row of rectangles
beats a chart object: you control the length, the colour of the one bar that
matters, and where its value sits. Give each bar a length exactly proportional to
its value, label it directly, and put the axis maximum where the reader can see it.
`add_chart` is there for a page that genuinely wants axes and gridlines — set the
series from `chart_series`, because Office's default six are as recognisable a tell
as any template. The later series colours are quiet on a light ground: use them for
supporting series and the accent for the one that matters.

**Do not use PowerPoint's native table object, `add_table`, or `ppt_layout.table()`.**
Its fixed row model and default cell geometry leave large blank fields when the page
has only a few rows, and the renderer may grow a row without moving separately drawn
rules. A screenshot of a source table is no better: it arrives in another typeface and
cannot be reweighted around the page's conclusion.

Draw the table directly with `Box`, `write`, `rule` and an optional quiet `plane`:
column headers establish alignment, every data row retains all of its cells, and a
final conclusion may sit on its own baseline below. A two-product comparison may be
two parallel regions with the same row labels; a pricing or benchmark table remains a
full row-and-column table. The model chooses row height and type from the rendered page,
so four rows can run at 16–18pt instead of inheriting a 14pt Office-table default.

Use no vertical grid and no zebra banding. One hairline between real row groups is
enough. Emphasise the row or value that carries the claim with weight or accent type,
not a block of colour behind every other row.
Set column headers at the same size as the body rows or one step larger and bold;
never make the header smaller than the cells it governs.

Preserve units, scales, qualifiers, series meaning and source labels exactly. Never
add a placeholder value or reconstruct a number that is not legible in the source.

## 6.5 The page's frame, and its points

```python
from ppt_layout import heading, page, points

frame = page()
heading(slide, frame, T, "把任务定义抽象成查询，网络本身就与任务无关",
        "02 统一范式 · 任务切换 = 换一组输入查询")
points(slide, frame.body.rows(3)[2], T,
       ["分类不再走全连接头：类别被建模成网络的动态输入，语义表示只通过损失监督学到。",
        "同一套权重在推理时按需拼装查询集合即可热切换任务。"])
```

**Open every content page with `heading()`.** A title floating on the same white as
the body leaves the page without a top edge, and a deck of those reads as a document
someone is reading out. It paints a quiet ground behind the title row, sets the
kicker and the title on it, and stops short of the body so nothing is welded to it.
`bleed=False` keeps the ground inside the safe area if the full-width band is too
much for the template.

**The header is one compact group.** A section tag, title and one explanatory line
sit close enough to scan as a single unit; the larger vertical break belongs between
that unit and the body. Do not put a decorative rule in the middle and leave the
subtitle stranded beneath it. When adapting a template, keep its font and title
language but tighten these internal gaps if the actual title uses fewer lines.
Define this geometry once in the shared setup and call it from every content page;
page blocks do not own title coordinates or a private variant of the header.

**Two or more parallel claims in one box take `points()`.** Bare paragraphs read as
one paragraph that happens to have a line break in it: a delivered page stacked two
47- and 58-character sentences with nothing in front of either, and a reader has to
work out that they are two things. `points()` writes a real bullet with a hanging
indent -- so the second line of a point lines up with its first -- and takes
`numbered=True` when the order matters. The build reports a wide box holding
unmarked claims (`unmarked_points`), so this is measured rather than suggested.

A single paragraph of explanation is a paragraph; leave it alone. What this is for is
a set.

**Never compute a y coordinate.** `stack(box)` is a cursor down a region: `take(h)`
hands back the next band and moves on, `rest()` hands back everything still
unspoken for, and the gutter between them is the deck's one gutter. A run that did
the arithmetic instead wrote `Box(x0, 5.34, x1, 6.72)`, looked at the render, tried
5.50, looked again, tried 4.86, and did the same thing on two later pages.

```python
down = stack(frame.body)
left, right = down.take(3.30).split_left(0.52)
picture_fit(slide, "figures/fig2.png", left, T, caption="Figure 2：架构（论文原图）")
for box, item in zip(right.rows(3, gutter=0.16), items):
    card(slide, box, T, icon=item.icon, title=item.head, body=item.body)
points(slide, down.rest(), T, takeaways)
```

`picture_fit` is the other half of that: it scales the image to fit the box whole
without cropping, centres it, and sets the caption under it. Giving `add_picture` one
dimension and letting it scale the other only works if you know which dimension runs
out first, which depends on the image.

## 7. Icons, on the cards and beside the points

```python
from ppt_layout import card
from ppt_icons import add_icon, find_icons, ICON_NAMES   # 180 Tabler Outline names

card(slide, box, T, icon="target", title="语义查询是必要的",
     body="去掉 Qsem 改用线性分类头：YouTube-VIS 44.7 对 46.3")
add_icon(slide, "clock", Inches(0.7), Inches(2.1), Inches(0.42), ACCENT)
```

**When a card is the right form, its title takes an icon, and so does a point that stands on its own line.**
Three decks built before `card()` existed drew every card by hand and not one of them
used an icon: 180 shipped unused while the cards' titles sat in a column of identical
bold lines, which is what makes a page of parallel points read as a list rather than
as a set. Pick the icon for what the card argues, not for a noun in its title.

`card()` draws the surface, the icon, the title beside it and the copy under it, and
it owns that geometry — the icon's square, the gap after it, the title's line, the
padding inside the box you give it.

`add_icon` is for the icons that are not on a card: beside a kicker, in the corner of
a metric, at the head of each row of a comparison. Stroked vectors, so they scale and
stay editable after export, in whatever colour you pass. The ink is inset in the
square you give it — a glyph fills at most about five sixths of it and some fill half
— so ask for a little more than you want to see.

**The 180 names, so none of them has to be guessed at.** A live run spent four
requests cycling through `layout-grid`, `category`, `crosshair` and `stack_2` before
it found one that existed, then printed `ICON_NAMES` into the build log — where it
came back on every request after that. Grouped by what they are about:

| | |
| --- | --- |
| Charts and measurement | `activity`, `chart`, `chart_area_line`, `chart_bar`, `chart_bubble`, `chart_donut`, `chart_histogram`, `chart_line`, `chart_pie`, `chart_radar`, `chart_scatter`, `dashboard`, `file_analytics`, `function`, `math`, `presentation_analytics`, `progress`, `report_analytics`, `table`, `timeline`, `trend_up` |
| Structure and flow | `arrow_right`, `arrow_up`, `arrows_exchange`, `binary_tree`, `box`, `boxes`, `circles_relation`, `git_branch`, `hierarchy`, `hierarchy_2`, `ladder`, `layers`, `layers_intersect`, `network`, `package`, `puzzle`, `route`, `section`, `sign_left`, `sign_right`, `sitemap`, `stack_2`, `target_arrow`, `transform`, `workflow` |
| Verdicts and attention | `alert_triangle`, `bell`, `check`, `check_circle`, `checklist`, `eye`, `filter`, `flag`, `focus_2`, `info`, `info_circle`, `minus`, `plus`, `question_mark`, `scale`, `search`, `shield_check`, `target`, `user_check`, `warning`, `x_circle`, `zoom_in` |
| Systems and security | `adjustments`, `api`, `atom`, `automation`, `bolt`, `brain`, `cloud`, `cloud_computing`, `code`, `cpu`, `database`, `database_search`, `device_desktop`, `device_laptop`, `device_mobile`, `device_tablet`, `fingerprint`, `key`, `lock`, `lock_access`, `monitor`, `refresh`, `robot`, `server`, `server_2`, `settings`, `shield`, `terminal`, `tools`, `wifi` |
| Documents and media | `books`, `brush`, `camera`, `certificate`, `clipboard_list`, `document`, `download`, `external_link`, `file_text`, `folder`, `forms`, `image`, `link`, `mail`, `message`, `messages`, `microphone`, `movie`, `music`, `palette`, `photo`, `receipt`, `typography`, `upload`, `video` |
| People and places of work | `award`, `briefcase`, `building`, `building_bank`, `factory`, `phone`, `podium`, `school`, `speakerphone`, `teacher`, `user`, `user_star`, `users`, `users_group` |
| Science and medicine | `dna`, `first_aid_kit`, `flask`, `heart`, `heartbeat`, `microscope`, `pill`, `stethoscope`, `test_pipe`, `vaccine` |
| Time, place and transport | `calendar`, `calendar_event`, `car`, `clock`, `compass`, `globe`, `hourglass`, `map`, `map_pin`, `plane`, `ship`, `truck_delivery`, `world` |
| Money and nature | `cash`, `coin`, `currency_dollar`, `droplet`, `leaf`, `moon`, `plant`, `recycle`, `shopping_cart`, `sun`, `tree`, `wallet`, `wind` |
| Everything else | `bug`, `diamond`, `lightbulb`, `play`, `rocket`, `sparkles`, `x` |

`find_icons(term)` finds near names for a word not in this table, and `add_icon`
names the closest match when a name does not exist.

Keep them small (a third to a half inch) and give each real space. What to avoid is
the filled chip behind every one: a row of identical coloured badges is the thing that
makes a deck look generated. If a card wants a surface, give the whole card a surface.

## 8. Inside a user's template

Its master, theme, layouts and canvas are the deck's house style, and the build refuses
a deck whose theme is not the template's.

**A template has structural pages and editable content examples.** Its cover, contents
list, section divider and closing retain their native furniture and are normally cloned.
For every other page, start from the nearest example page by information shape: replace
its text and pictures, delete spare repeated units, and move or resize the surviving
regions when the actual content needs it. Only when no example can carry the argument
after those edits may the page compose inside the measured style. A prototype is a
starting composition, not an immutable form.

### The pages you clone

`ppt_template` names them — `house_pages: {"cover": 1, "agenda": 2, "section": 3,
"closing": 11}` — and renders them. Those four are what a reader recognises the house
by, and a deck that draws its own cover announces itself as not the user's before a
word of it is read. They are also the pages code cannot reproduce: on a typical
thirteen-page template, 9 of them hold custom geometry, a gradient or a fill at 15%
opacity that python-pptx has no way to write.

```python
from ppt_template import adapt, prototype, shape_at
tpl = Presentation(user_template_path)           # the original, with its example pages
adapt(prs, prototype(tpl, 1),                    # prototype(tpl, N) counts from 1, like the menu
      title="项目标题",                            # the page's own heading rows, by role
      subtitle="会议副标题",
      pictures={7: "figures/selected-figure.png"},
      drop=["template-credit"])
adapt(prs, prototype(tpl, 2),                    # the contents page: one unit repeated
      title="目录", subtitle="Agenda",
      items=[["01", "第一部分"],                  # one entry per unit;
             ["02", "第二部分"],                  # the spares are deleted, not emptied
             ["03", "第三部分"]])
```

**Name the heading by its role, not by its shape.** `title=` and `subtitle=` write the
page's own heading rows -- its title placeholder, or the topmost line in the top third.
`texts={"单击此处添加页面标题": ...}` still works and needs you to know what the slot
currently says. What happens without either: one run filled the contents page with
`items` alone, the header came back empty (text this call does not name is emptied), and
it wrote "目录" and "Agenda" back as two new boxes over the clone -- which is the one
construction `template_underlay` refuses.

**Numbering: an integer key is the shape's place on the page**, and the reference prints
it — every shape carries a `# [n]` line above it, groups opened, counting shapes that
cannot be drawn as well as those that can. `shape_at(slide, n)` is the same numbering
after `adapt` returns, for anything else the page needs.

**Text you do not name is emptied**, because the words in a template page are its example
copy. Shapes you do not name keep what the template put there — that is the point, the
page arrives designed.

**Never lay a new text box over a page you cloned.** One run did exactly that on ten of
twelve pages — `clone_page` for the background, `add_textbox` for the copy,
`replace_text` never called — and shipped the template's own "单击此处添加长一点的副标题"
under its own text on every page. Two decks of the same paper in the same template:

| | replaced what it cloned | cloned and overlaid |
| --- | --- | --- |
| findings in total | **7** | **116** |
| blocking | **2** | **55** |
| words painted over words | 0 | 25 |
| copy overflowing its card | 0 | 21 |

`placeholder_copy` and `template_underlay` both refuse it now. If you cloned a page,
every word on it arrives by replacing a word that was there.

**The template's photographs are placeholders.** A cover's stock photograph of a meeting
table, on a deck about video segmentation, shipped twice. Replace it with a figure from
the sources or take the frame out — `pictures={7: "figures/fig3.png"}`, or `drop=[9]`.
Small graphics are different: an image under a tenth of the page is an icon, a corner
flourish or a rule, and belongs to the design.

**A landscape figure does not go in a portrait frame.** `replace_picture` refuses when
the two are more than 2x apart and says both numbers, because contained it becomes a
strip in an empty frame and cropped it loses its outer columns. Give the frame the box
the figure needs: `pictures={4: ("figures/fig2.png", (0.8, 1.6, 7.4, 4.2))}` in inches,
or `place(shape_at(slide, 4), box)` afterwards. Permuting the shape number is what one
run did instead, three times, and it never produced a deck.

### Choosing and adapting content examples

Do not fill a template page as if it were an immutable form. Use the closest example
as a starting composition and adapt it to the actual content:

- replace every example text block that is not static furniture;
- replace or remove every example picture, logo, chart or table that is not the page's
  own retained furniture;
- use `adapt(items=...)` to fill the actual repeated units and delete spare units;
- reshape or drop a picture frame when its aspect ratio does not suit the selected
  figure;
- use a different example page or compose inside the house style when the argument
  cannot fit without shrinking or discarding meaning.

The measured `house_style` remains the fallback for pages that need a new composition:

```
layout_for_a_page_you_draw: "Title Only"     # add_slide on this, and the background comes with it
title_row: at (0.72, 0.14) 11.88x0.98in, 28pt, on 7 of its pages
type_pt: {title: 28, subtitle: 24, body: 18, secondary: 16, caption: 14}
face: "Arial"
safe_area_in: [0.72, 0.14, 11.88, 6.56]
body_area_in: [0.72, 1.24, 11.88, 5.46]
body_area_as_code: from ppt_layout import Box; body = Box(0.72, 1.24, 12.6, 6.7)
```

```python
from ppt_layout import Box, plane, write, table
T = THEMES[theme_id]                                     # the template's own palette
layout = next(l for l in prs.slide_layouts if l.name == "Title Only")
slide = prs.slides.add_slide(layout)
write(slide, Box(0.72, 0.14, 12.60, 1.12), "消融：时序颈与语义查询是关键",
      size=28, bold=True, colour=T["foreground"], font=FACE, cjk_font=HAN)
body = Box(0.72, 1.24, 12.60, 6.70)                      # body_area_as_code, pasted
figure, findings = body.split_left(0.52)                  # the arrangement is yours
slide.shapes.add_picture("figures/fig4.png", *figure.pptx())
for card, (head, line) in zip(findings.rows(3), rows):
    plane(slide, card, T, tint="surface", radius=True)
    write(slide, card.inset(0.24), [head, line], size=18, colour=T["foreground"],
          font=FACE, cjk_font=HAN)
```

**The layout follows the content.** Two things compared is two columns; a pipeline is a
row of steps; one figure with three findings is the figure and a stack beside it; seven
numbers is a table. Decide it from what the page has to say, then adapt the nearest
prototype. Draw freely only when no example page carries the content shape after the
prototype has been edited.

**One scale for the whole deck.** Every page title at `type_pt["title"]`, body copy at
`type_pt["body"]`, captions at `type_pt["caption"]`, and nothing under 14pt. The same
role at the same size on every page: `type_drift` measures this off the render and
reports a slot the deck sets at more than one size, because a reader reads each page
against the one before it and a body size that moves page to page reads as unfinished.

**Set every size yourself, and give the box the room it needs.** `write` turns autofit
off on purpose: a box that cannot hold its copy comes back as a measurement rather than
as type quietly dropping to 10.8pt. If `overset_copy` says a box needs 1.51in and has
0.90in, the answer is a taller box, a wider column, fewer points or a second page —
never a smaller size.

**Stay inside the safe area.** `safe_area_in` is where the template keeps its own
content; a layout's artwork lives outside it, and copy laid across that artwork comes
back as `over_layout_art`. The title row is not a suggestion either — put the page
title in the box `title_row` names, at the size it names, and every page of the deck
lines up with the template's own.

**Ink is `foreground`. `surface` and `background` are what the ground is painted with.**
On a dark template those three are white, near-black and black, and a deck that reached
for "the dark one" as its type colour shipped eleven of twelve pages with the title in
#1A1A1A on #000000. `unreadable` refuses that now, measured off the render at 3:1.

**Take the face from the page, not out of the theme.** `ppt_theme`'s `font_family` is
what the template's *theme* declares, and one measured template says 微软雅黑 while all
203 runs on its example pages are Arial. `house_style`'s `face` is what the pages
actually use — pass it as `font=` and its CJK companion as `cjk_font=`, or a line of
mixed text comes out in two unrelated faces.

Which pages are cloned is decided in the outline, not here: `ppt_outline` takes a
`prototype` per page, it is required for the cover, the contents and the closing, and a
content page that names one is reported back to you.

## 9. Formulas

```python
from ppt_layout import formula
formula(slide, box, "掩码 logits = (F_4, Q'_{inst})；分类 logits = (Q'_{inst}, concat(Q'_{sem}, Q'_{bg}))", T)
```

`_x` and `_{xyz}` subscript, `^x` and `^{xyz}` superscript. A lone latin letter comes
out italic the way a variable is, and a word of two or more letters upright the way
`concat` and `softmax` are.

**Never set an expression with `write`.** A formula in a text box is prose: it wraps
where the box runs out, and where it runs out is the middle of a symbol. A delivered
page in a 3.9in column broke that line after "分类", putting the rest of the clause
on the next line, and every subscript in it was flat — F4 for F-sub-4, Qinst for
Q-sub-inst — so the one thing the notation carried was gone. `formula()` sets it as
one unbreakable line, steps down the ramp until it fits, and splits at the
expression's own semicolons if the floor is not enough.

Set the rest as text in the deck's font, real Unicode where it reads cleanly and
plain language where it does not. Never paste TeX source onto a slide. If an
expression is not load-bearing, explain the idea in words.

## 10. Look at the deck

`ppt_build` returns every page it made, in batches, each labelled with its number and
anything measured on it. **A program that ran without error is not visual
evidence.** Open each render and name something concrete on it before you change it.

Walk the checklist:

- copy over the template's artwork, off the page, clipped, or unexpectedly wrapped;
- a role at the wrong step of the scale — a caption as large as the body, a title a
  point above its subhead, a source line as large as what it credits;
- a page that stops two thirds down;
- consecutive pages on the same skeleton, differing only in their words;
- a native Office table or table screenshot instead of an editable drawn matrix;
- unreadable, distorted, blank or badly cropped figures;
- empty charts, weak labels, bars out of proportion to their values, misleading
  scales;
- a label wrapped onto a second line because its box was drawn too narrow;
- icons too small to read, or crowding the text they belong to;
- a sparse or mechanically forced layout — compartments where an argument belongs;
- abrupt body-size changes between comparable pages;
- decorative treatment repeated with no relation to what the page says;
- a template's sample text still on a page you cloned, or a placeholder never filled.

Two questions settle most of it: is the smallest type still readable with the page
shrunk to a third — roughly the back of a lecture theatre — and, held beside the page
before it, is this a different page or the same page with different words.

The reply names how many pages it did not show and how to ask for them. **A named
subset is not the deck**, and a page you have not looked at is a page you have not
checked. Then edit and build again: the first build is a draft, and a build that
passed the gates is not finished.

**Work one page at a time when a page is wrong.** `ppt_build(slides=[7],
polish=false)` rebuilds and hands back page 7 alone, in seconds and without spending
a design round: edit that page's block, look at it, fix it, look again. A tight loop
on one page finds what a sweep over twelve does not — a sweep tells you twelve pages
have something wrong, and a loop tells you what. Run the full build when a batch is
settled; that is what runs the gates and publishes.

After the gates clear, a second pass runs from code on a fresh context and rewrites
pages for design. It sees the render and the block, never your intent, and it may not
touch the palette, the font family, or what a page says. If it reverted itself, its
finding is yours to answer.

## 11. The order is enforced, not suggested

Two of these are refusals rather than advice, because asking did not work — a model
reading this same document still went straight from the request to a build.

`ppt_build` refuses until `ppt_prepare` has read the task, and until `ppt_brief`
holds the three things the user decides. And it refuses a deck carrying a page whose
code has never been rendered back to you: the record is keyed on the block that drew
each page, so a page you looked at stays looked at and a page you then edited does
not. `slides=[1]` on an eighteen-page deck is no longer a way to ship seventeen pages
nobody saw.

That one cannot be answered by editing the deck. Run the build again, walk the
batches, and look.

One more thing the order decides: **`ingest/materials.md` does not exist until
`ppt_ingest` has run.** Reaching for it first costs a request and gets a
`No such file or directory`. When it is there, `ppt_ingest` returns
`materials_index` — every part of it with the line it starts at — and `read_file`
takes `offset` and `limit`. Read the parts the deck stands on rather than the file:
whatever you read stays in the context and is re-sent on every request after it,
which on one measured run made a single whole-file read the most expensive thing in
the deck.

## 12. What the build refuses, and what it only reports

**Refused**, with the page named: a page citing
one figure while showing another; a length the brief did not agree; the wrong
language; a filled colour bar carrying nothing; a page the build cannot map back to
your code; a theme that is not the bound template's; a page you have never been
shown; copy printing an escape (`48.3\nOVIS` -- pass a real newline); content four
fifths hidden behind an opaque shape drawn after it; type a reader cannot make out
(under 2:1 against the ground it landed on).

**Reported**, with a page number, and yours to judge: type under the floors, a page
carrying a talk's worth of copy, a page of pure prose with nothing to show, a table
too wide to read, words colliding in the render, a rule struck through a row, copy
escaping a card, a shape over the page edge, copy on the layout's artwork, an
expression set as prose, and parallel claims with no mark in front of them.

Five of those reports came out of reading finished decks page by page, and each names
the move that fixes it:

| Reported | What it means | The move |
| --- | --- | --- |
| `crowded_panel` | a line touching the bottom rim of its own panel | the padding your other panels have, or one line less |
| `orphan_line` | a label broken as "为什么要统 / 一" | a hair more width, or fewer characters |
| `unseparated_blocks` | two groups with no more air between them than inside them | a wider gap, a surface, or a hairline |
| `excessive_whitespace` | a large blank field between body groups, below the content, or inside a panel | grow the load-bearing content, redistribute it, or shorten the panel |
| `native_table` | a PowerPoint table object fixes the page to Office cell geometry | redraw the same rows and cells with aligned text boxes and rules |
| `spilled_copy` | a `wrap=False` box painting its copy off the page | turn wrapping on and give the box a second line's height |
| `type_drift` | one slot the deck repeats, set at several sizes | give the boxes the height one size needs |
| `flat_formula` | an expression set with `write`, so its subscripts are flat and the box may break it inside a symbol | `formula()` (§9) |
| `unmarked_points` | a wide box holding two or more claims with nothing in front of any of them | `points()` (§6.5) |

Warnings are not refusals for a reason: every one of them can be "fixed" by making
the type smaller, and a gate that demanded they clear would get exactly that.

Two silences the build reports rather than hides: with no sources ingested, no number
on any page has been checked against anything; with no renderer on the machine,
nothing measured on the rendered page ran. Neither stops a deck, and both belong in
what you tell the user.
