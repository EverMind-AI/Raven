---
name: ppt-script-authoring
description: "Design and build a deck as a python-pptx program: your own layout system, the source's own figures, charts you draw, and a template's design when the user gave one."
metadata: {"raven":{"emoji":"🎞️","always":true}}
---

# Deck authoring

You design the deck and you build it, by writing a python-pptx program that
`ppt_build` runs. Every page is yours: the grid, the type scale, the palette, the page
furniture, where a figure sits and how large. What refuses a deck and what only reports
is in §12.

## 1. Define the communication job

Infer the audience, the deck's job, the outcome you want, the central takeaway and
the evidence that supports it. Build a cumulative argument rather than an inventory
of topics: each content page performs one narrative job and makes one primary claim.

Write audience-facing titles that state the point. Open with the context, question
or stakes that make the deck worth sitting through. Close by resolving that opening
— a decision, an implication, a next question. A separate closing page is optional:
add one when the user asks for it, otherwise let the last content page carry the
conclusion.

Pack the evidence before allocating pages. A page carries one claim and the developed
support that claim takes, and what makes the support developed is that it is more than
one kind of thing: evidence from the source, a mechanism or a comparison, an
implication or a constraint. Nothing counts the kinds, and how much copy a page ends up
with is measured rather than allocated (§2). If the evidence cannot support a complete
argument, merge the page rather than enlarging fragments.

The language, the audience and the length are the user's, and they are asked for
rather than inferred. `ppt_prepare` reads what the request already states and hands
back the rest as questions; put those to the user with `ask_user` and record the
answers with `ppt_brief`. The length and the language are then measured against the
finished file and both refuse the deck (§12), so record what the user said rather than
what you inferred. The audience is not measured; it is the room you judge the deck for
when you look at the renders (§10).

The fourth thing to record is the one that gets dropped: whatever the user ruled out. `ppt_brief(forbidden=["no icons", "never name a competitor"])` is
quoted back to you in so many words on **every** build, which is what keeps a
prohibition agreed at turn three alive at turn forty; the same thing filed under
`notes` is kept with the brief and never restated. Only what the user actually ruled
out belongs there — a preference you inferred, recorded as a rule, is a constraint
nobody agreed to.

## 2. Write the outline before you write the program

**Go looking for the deck's pictures first, and look widely.** Not for a page but for
the pool the outline gets to choose from. `web_search(kind="images")` returns each
candidate with its pixel size and the page it came from. Search the things the material
*names* as well as the things it links — a paper, a benchmark, a product, a release —
and the ordinary furniture too, the logo and the product shot, which every deck wants and
no material bothers to link. Bring in more than one page will use: `ppt_fetch` each, and
`ppt_figure_inspect` them, because a candidate you have not looked at is a candidate you
cannot choose.

`ppt_outline` records it, and the build refuses until it exists. This is the stage that
decides how much is on a page: without one, what a page says gets decided while its
geometry is being typed, and the page comes out thin.

Per page: the **claim** as a statement (which is also its title), what **carries**
it, the **figures** it places by id, and what it **says** in the deck's language, as
it will read on the page.

The outline does not decide a page's *shape*. It used to record a table's columns and
cells for any page whose `carries` mentioned a comparison, and a brief that asks for a
「对照表」 is ordinary — so a deck came back with five tables whose every cell was a
phrase, planned before anyone had seen the content laid out. What a page carries is
settled where it is drawn, with
[deck/build/references/tables.md](deck/build/references/tables.md) open. **Figures a
reader compares down a column are a table** — a benchmark, a price list, a
year-by-year rate — and that page should draw one. Words in every cell are cards.

`says` is where density is decided, and it is the field that gets under-written. A page
carries 200 to 467 characters of copy and the **median is 250** — a
five-row comparison, two takeaways and a caveat is that page, and it is a good page.
Plan for the median rather than the top of the range: the top of it is where the type
reaches its floor and `overset_copy` starts reporting. Points that are each half a line
add up to a page that is a title and some labels, and the build cannot rescue it:
geometry can arrange copy, never supply it. So write each point as the sentence a reader
gets, evidence included, and if a page has only one short line in it either the evidence
for it belongs there too or the page belongs merged into its neighbour.

Checking the plan here rather than after the build is the difference between a cheap
edit and an expensive one: what refuses an outline, and what it only reports, is in §12.

And `needs` is where gathering belongs — this is the first moment anything knows
what each page will show. Go through the outline page by page and name the picture
each one wants, then find it with `web_search(kind="images")` and bring it in with
`ppt_fetch`. A fetch joins the deck's own source set and is read on arrival, so there
is no separate ingest to remember; `ppt_ingest` is for bringing in another directory
of the user's, or re-reading after you have edited a source by hand.

A deck whose only imagery is what the materials happened to contain is a deck of
panels: the products it names have logos, the organisations have marks, the standards
have badges, and the architectures have diagrams their own documentation draws. Search
for those before deciding a page is text.

## 3. One visual identity, defined once

**What a finished page owes is written down, and it is what the review at the end judges
by: [deck/build/references/design-requirements.md](deck/build/references/design-requirements.md).
Open it now, before the first page.** Seven short sections -- room, edges, marks, figures,
tables, type, the claim -- each decidable from the render alone. Two of them decide most
of what a delivered deck gets wrong, so they are here as well:

- **A blank region has two kinds and they have different answers.** A shape bigger than
  what it holds is fixed by measuring (`card_size`, `text_size`, `picture_size`,
  `table_size`) and taking only that much. A *page* bigger than what it holds is not
  fixed by shrinking anything -- taking a cover off room leaves the room -- and wants
  either more that the page needs said, or this page and its neighbour becoming one page.
  The test is one question: would making the shape smaller fill the page?
- **A table under a full-width row needs `weights`, or it comes out as narrow as its
  content** whatever box you hand it -- measured at 6.98in in an 11.9in band.

Take the palette and font from `ppt_theme`. It is written into the build directory on
every build, so the import is plain — that directory is already on `sys.path`:

```python
from ppt_theme import THEMES, rgb

theme_id = next(iter(THEMES))                    # one entry, and it is the template's
T = THEMES[theme_id]
BG, SURFACE = rgb(T["background"]), rgb(T["surface"])
INK, MUTED, ACCENT = rgb(T["foreground"]), rgb(T["muted"]), rgb(T["accent"])
SOFT, GRID = rgb(T["accent_soft"]), rgb(T["grid"])
SERIES = [rgb(c) for c in T["chart_series"]]     # six, in order
FONT = T["font_family"]
```

**Do not write a module into that directory whose name shadows a standard one.** A
`copy.py` beside your script breaks python-pptx's own import, and the traceback will
not mention your file.

**There is no palette to pick, and one to state.** A deck is always built inside a
template: `ppt_prepare` binds the one the user gave, and binds a bundled default when
the user gave none. The build directory then holds one theme, named after that file —
so `THEMES` has exactly one entry and its face is the template's own. Take that entry
by iteration; a theme id typed into `THEMES[...]` is a `KeyError`.

**Only three roles are read off the file** -- the ground, the ink and its first accent.
`surface`, `accent_soft`, `accent_ink`, `grid` and `muted` are mixed from those three.
What a file declares is not what its pages paint -- all twelve bundled templates
declare their second background as `#F0F0F0` and none of them paints it -- so say what
the renders show:
`ppt_template(project=..., palette={"accent": "#155FFD", "surface": "#DDE8FF"})` holds
for the deck's whole life and reaches every page through `ppt_theme`.

**A partial palette is the ordinary one.** What you leave out is re-derived from what
you named: `palette={"accent": "#7A1FA2"}` in an amber template brings `surface`,
`accent_soft` and `accent_ink` back as tints of that purple. Reach for it for a deck
with a brand of its own, an accent the template does not carry, or a series the content
decides -- `chart_series` is the one role a stated accent does *not* move, so a
comparison drawn in your own two colours states them. A later call merges over the
earlier one, so state it before the first page.

The theme part is what the build checks, and you pass it by opening `PPT_TEMPLATE` at
all (§12). Nothing checks what colour you filled a shape with.

**The role names are open; a role's meaning is not.** These eight are the ones the
derivation knows how to finish:

| Role | What it is for |
| --- | --- |
| `background` | the ground a page is painted on |
| `surface`, `accent_soft` | planes content sits on |
| `foreground` | body type |
| `muted` | secondary type and source lines |
| `accent` | the focal point of a page |
| `accent_ink` | type in the accent's colour (below) |
| `grid` | hairlines |

You may add your own beside them: a deck comparing two things wants two colours for the
pair, and `palette={"ours": "#0B3D91", "theirs": "#F2E8D5"}` puts both in every page's
theme, where `card(..., tint="ours")` reaches them by name. What may not move is the
meaning — if the accent marks "our result" on one page it cannot mark "prior work" on
the next, and a pair named on page 3 is the same pair on page 11.

**A helper that holds the theme takes a colour by name.** `tint=` and
`colour=`/`mark_colour=` -- the signatures in §4 say which helper takes which -- each
take a role name, a key you added to the palette or to your own copy of the theme dict,
or a literal `#RRGGBB`. A name the theme does not carry is refused and says so, so a
misspelling is a message and not a colour.

`write` is the exception and the reason is in its signature: it is handed a box and a
string and no theme, so it takes the colour itself — `colour=theme["muted"]`,
`colour=theme["accent_ink"]`, or hex. `"muted"` there is six characters of a hex digit
and raises as one.

**A chart's `accent=` is not a colour.** It names *which item* to bring forward — a
category from the data you passed, `accent="Q3"` — and handing it `#RRGGBB` is refused
with the list of items it accepts.

A chart's colours are `chart_series`, and that is yours to set. Its order is the
contract — `chart_series[0]` is the series the page is about — so a comparison reads
by putting your own two colours in it: for the deck, `ppt_template(palette=
{"chart_series": ["#0B3D91", "#E4572E"]})`; for one page, copy the theme and set it
there before the call. A chart drawn from a theme you did not touch uses the
template's own series, which is the right default and not a rule.

**A card chooses its own ink.** `card` and `heading` set their type in whichever of the
theme's inks reads on the plane they were given, so `card(..., tint="accent")` on a
deep blue comes out with white type and the same call on a pale tint comes out dark.
You do not compute that, and you should not work around it: a `write` you place over a
plane yourself is yours to colour, and `theme["background"]` is the ink on a saturated
one.

**`accent` fills; `accent_ink` writes.** `accent_soft` is the accent mixed towards
the background, so a pale accent cannot be read on its own tint. The theme carries
`accent_ink` for this: the same hue, dark enough to write with, or the accent itself
where that already reads. Fills, markers and bars take `accent`; a number or heading
in the accent's colour takes `accent_ink`, by that name or as `theme["accent_ink"]`.

**One font family, and its CJK companion.** Inside a template, the face `ppt_theme`
hands you is the template's own, whatever it is (微软雅黑, say), and that is the right
one to use: it is the house style, and the width measurement degrades to an estimate
for it rather than failing. Anything else is substituted on export and every position
you computed is wrong.

For a deck in Chinese that is only half the answer. `font_family` is a Latin face,
and Han characters set in one fall back to whatever the viewer has — a different
design at a different weight, and on the review renderer no Han glyphs at all. Every
theme carries `cjk_font_family` beside it — the template's own Han face where it names
one, and otherwise a face matched to the Latin one's class (`Noto Serif CJK SC` for a
serif, `Noto Sans CJK SC` for a sans) — and both names go on
the same run: `write(..., font=FONT, cjk_font=HAN)` from `ppt_layout` does it, or
set `a:ea` yourself. Then 目标查询 is set on purpose and `TarViS` in the same line
stays in the Latin face.

**A type scale with a big step at the top and fine steps below.** Same size for the
same role on every page. A 16.5pt title over 16pt body is not a scale; it is no
hierarchy at all. Two numbers in it are measured rather than chosen, because a deck is
read projected: body copy stays at or above **14pt**, and a caption, a source line or
any other short label may go to **10.8pt** but no further.

**A dominant element takes the top of the scale, and the top of it is above the title.**
A page whose point is one number sets that number at `NUMBER_PT`, the ramp's largest step
-- `the_largest_step_this_copy_takes(..., largest=NUMBER_PT)` is how to ask for it (§4) --
and it outranks the page title rather than sitting under it: a number set smaller than
the heading over it is not a dominant number, it is a caption on one. The inversion runs
the other way too, because copy is copy -- a concluding sentence set larger than its own
title makes the title the subordinate line. Where the size a page seems to want falls
between two steps, take the step above rather than a size off the ramp (§12).

Which floor a box answers to is decided from the box, not from you:

| The box | Floor |
| --- | --- |
| in the bottom 0.9in of the page -- furniture nobody reads from a seat | **8pt** |
| twenty characters or more, above that band -- copy | **14pt** |
| opening with `来源`, `注：`, `图`, `表`, `source:`, `figure` or `table` -- a caption in either language | **10.8** |
| shorter than twenty characters -- a label | **10.8** |
| fewer than four characters -- a page number or a chart tick | not measured |

The size measured is the one the *render* came out at, not the one you declared, so a
box too small for its copy is caught where autofit shrank it. Copy that will not fit
above the floor is a page with too much on it, and §12 says what to do instead of
shrinking it.

**One set of spacings, one surface treatment, one rule weight.** Fix them in the
setup beside the palette and call them everywhere: a larger gap between groups and a
smaller one inside a group, so grouping is legible from the spacing alone; one
restrained tint that planes are painted with; one thickness a hairline is drawn at.
Spacing that changes page to page reads as a deck assembled rather than designed, and
two groups with no more air between them than inside them come back as
`unseparated_blocks`.

**The header is the same on every page.** It is the one thing a reader sees eighteen
times, so the deck picks one header form and keeps it: the same kicker, the same
title position, the same treatment. Which form is yours, within the one the template
already has: its own title row, measured for you (§8). What a deck may not have is
three of them.

**Avoid -- the treatments that read as generated.** Each is a default reached for
instead of a decision, and each is legible as one from the back of the room:

- **A decorative rule or accent line under a title.** The title's own step in the scale
  already separates it from the page; a line drawn under it adds a horizon that cuts the
  heading off what it heads, and it appears on every page because it was never chosen
  for any of them. `heading` draws none, and `rule` caps at 1.05in so it cannot become
  one -- it is a short mark under a heading or beside a number. A page that genuinely
  wants a divider gets a hairline under 4.5pt, or a thin `plane` for a full-width one.
- **A filled shape whose content is the space it covers.** A colour bar across the page,
  a saturated strip down the edge of a card, a tinted panel painted over whatever room
  was left with one sentence centred in it: each is a shape carrying no information,
  which is what makes it read as filler -- a reader sees a region announced and finds
  nothing in it, and the emptiness the shape was covering is now emphasised instead.
  Grow the load-bearing content or redistribute it (§3.5). One filled bar is the deck's
  own, the band the title row sits on; `band` (§12) says which shapes and at what size,
  and `excessive_whitespace` measures what the panel was hiding.

  The call is `card_size`, and a region is not a height: `card_size(w, icon=, title=,
  body=)` answers how tall one card's own content makes it, so a row of them is
  `max(card_size(...).h for each)` and the row is drawn at that. Take only that much of
  the region and leave the rest to the next band -- a card handed `cells[i]` straight
  out of `grid` is as tall as the region however little it holds, which is the void.
  Cards take an `icon` for the same reason: three lines under a mark fill a card that
  three lines alone leave two thirds empty, and the mark is what makes the row scannable
  rather than three paragraphs side by side.

  **The bar is usually not a mistake -- it is an answer to a page that felt empty**, and
  wanting a full page is right. A stretched shape is the wrong way to get one, because it
  fills the pixels and not the page: the reader still finds three short lines in a
  half-page region, only now with a colour announcing it. What fills a page is content,
  and the moves are these, in this order -- say more on the page (a line of detail under
  each card title, the figure the number came from, the units, the year); give each card
  an `icon`; use fewer, wider cards so the copy reaches their edges; put the figure or
  the table in the room the cards left; or move a point here from a page that has one too
  many. Only when none of those is true is the page genuinely short, and then the answer
  is one page fewer, not one bar more. If you have already drawn the bar, the check is
  the same either way: cover the shape's fill with your hand and ask whether the page
  still says as much. If it does, the fill was doing nothing.
- **A title too close in size to its body copy.** The step at the top of the ramp is
  what makes a title a title, and two sizes a point apart are one size to a reader.
  `type_scale` and `type_drift` (§12) read the sizes back off the render.
- **Type too small to read projected.** The floors above are the measured half of this;
  the other half is the page shrunk to a third of its size (§10).
- **The same page skeleton with only the words changed.** The header repeats on purpose
  and nothing under it does: a cover, an agenda, a two-way comparison, a table page, a
  full-bleed figure and a closing page are each recognisably a different kind of page.
  `layout_variety` (§12) reads this off the shapes each page actually carries.
- **A filled chip behind every icon.** A row of identical coloured badges is the tell.
  An icon is a mark beside type (§7); where a block wants a surface, give the whole
  block one.

## 3.5 Structure follows the content

Decide what carries the point before placing anything.

**Every content page carries a visual element** -- a source figure, a chart you drew, a
drawn table, a diagram, or a region led by icons. A title over a column of copy is the
page that comes out when nothing was decided. The deck-wide count, its threshold and what
counts are `evidence`'s (§12); the decision here is which element this page's claim is
made of.

**Reference — not a constraint.** The table below is vocabulary, not a lookup. A page
may take one row, several rows together, or a composition no row names; no row carries
a coverage quota and nothing counts which ones a deck used.

| Content logic | What carries it |
|---|---|
| Real product, interface, experiment or published plot | a source figure |
| Source-backed numeric comparison or trend | a chart you draw |
| Tabular data or comparable rows and columns | a drawn table or comparison matrix |
| Familiar capabilities or categories | icon-led regions |
| Sequential steps or milestones | an interlocking chevron row, or a timeline; §7.5 |
| Two genuinely contrasted alternatives | paired regions on one baseline |
| One memorable conclusion | a single dominant statement |
| One dominant number | the number at display size, reasoning beside it |

Then compose around it. Give the load-bearing element the space its role deserves
and let the rest defer; a page where every region carries equal weight has argued
nothing. Vary composition because the content varies, not to fill a quota, and keep
a series of comparable cases on one shape so the reader can compare them. Give a
conclusion or decision its own baseline, weight or restrained accent instead of styling
every component identically.

Prefer one coherent composition over a dashboard of unrelated panels. Do not draw boxes
merely to avoid a prose page.

**A composed page takes its shape from the catalogue.** Open
[deck/build/references/layouts.md](deck/build/references/layouts.md) while deciding, and
name the ids you used in the outline's `layout`; a page cloned from a template example
leaves that field empty, because its structure is the example's.

**Every band of the page is declared, and the hierarchy is legible.** A composed page
divides into regions before it divides into shapes: count them and name each one in the
`layout`, because the band nobody planned is the one that comes back as bare paragraphs.
Then rank them so the reader sees it before reading a word -- which region is the claim,
which supports it, which is aside.

Cards, planes and helper presets are primitives, not a required page grammar. Do not wrap
every component in a filled rectangle just to make its boundary visible; use hierarchy,
alignment, whitespace and meaningful rules first.

**Fill the page.** A 13.3 x 7.5in canvas holds far more than a title and four
bullets, and content that stops two thirds of the way down is the most common
failure there is. Whitespace you chose is a margin around content that fills the
frame; whitespace left over reads as unfinished. When a page comes back with a large
accidental empty field, grow the load-bearing content, introduce a second visual
layer, or redistribute what is already there -- never decoration, restated copy or
smaller type, each of which makes the page worse than the gap did.

When the source already carries a real table, comparable rows or a set of parallel items
with several attributes, that information shape is part of the evidence: do not flatten
it because the text happens to fit.

On a figure-and-text page, let the figure dominate and turn the supporting copy into
scan points with clear labels -- as many as the region beside the figure actually
holds, which `points_size` answers before the page is drawn rather than after (§4).
Put those points on one or more restrained theme-coloured surfaces so the image and
explanation read as distinct layers; do not leave a bare paragraph floating beside the
figure. The cards defer to the figure -- they are supporting structure, not a row of
equal dashboard tiles.

Every placed figure carries a concise caption, and a figure can carry two of them
that are not the same kind of claim. Its **source caption** is what the source
printed under it: quote it, credit it. The **`visual_caption`** `ppt_figure_inspect`
wrote is a description of the pixels and nobody's caption: write your own line from
it, never present it as the source's words, and never put a name in front of it the
materials do not establish -- a figure credited to the product it happens to sit
beside is the failure this exists to stop. A caption says what is shown, not a claim
the pixels do not prove.

## 4. The program

It goes at `deck/build/build.py` — that whole path, relative to the
workspace; `ppt_prepare` reports it as `write_the_program_to`. A bare
`build/build.py` lands somewhere the build does not look. `write_file` creates it,
`write_file` with `mode="append"` extends it, `edit_file` revises it. Never restate
the whole file to change part of it.

**Write it in pieces, not in one call.** `write_file` for the setup and the first three
or four pages, then `mode="append"` for two or three pages at a time, with
`ppt_build(draft=true)` in between: a draft builds what exists, measures it and hands
back the renders without holding a part-written deck to the agreed length and without
publishing. Drop `draft` when the deck is whole and you want the gates. A twenty-page
program sent in one call is cut before it arrives, and a draft shows you a page while
there are three rather than twenty.

It runs in the build directory with python-pptx and Pillow and reads its paths from the
environment: `PPT_OUTPUT` (save there and nowhere else); `PPT_FIGURES_DIR` (the figures
ingest extracted, and the only way to one — take
`FIGURES = os.environ["PPT_FIGURES_DIR"]` in the setup and write `f"{FIGURES}/fig2.png"`
wherever a page places a figure; there is no `figures` directory under `deck/build`);
and, with a template bound, two paths that are not interchangeable — `PPT_TEMPLATE` is
the template with its example pages **removed**, the deck you build into
(`prs = Presentation(os.environ['PPT_TEMPLATE'])`), and `PPT_TEMPLATE_SOURCE` is the
user's original with those pages still in it, the only thing `prototype` can read a page
out of (`tpl = Presentation(os.environ['PPT_TEMPLATE_SOURCE'])`). Handing `prototype` the
deck you build into raises `this template ships 0 pages`.

**One block per page, opened with a `# SLIDE <n>` banner.** Shared helpers above the
blocks; the page's own composition inside the page's own block. Two shapes are refused: a
`build.py` that runs another file, and a loop that draws every page from one call site.
Both leave no code belonging to one page, so nothing can match a render to the code that
drew it, report a defect against the block that caused it, or fix one page without
touching another.

**The whole program, ending included.** Every other example here and in the references is
a page fragment; this is the file they go inside, and its last line is the one that gets
left out. A program that draws twenty pages and never saves them leaves no deck.

```python
import os

from pptx import Presentation

from ppt_layout import BODY_PT, heading, page, points, write
from ppt_theme import THEMES, rgb

prs = Presentation(os.environ["PPT_TEMPLATE"])       # the template, its example pages gone
T = THEMES[next(iter(THEMES))]                       # one entry, and it is the template's
FACE, HAN = T["font_family"], T["cjk_font_family"]
INK = rgb(T["foreground"])
HOUSE = "Title Only"                                 # house_style's layout_for_a_page_you_draw
LAYOUT = next(one for one in prs.slide_layouts if one.name == HOUSE)


def content_page():                                  # the one skeleton every page shares
    return prs.slides.add_slide(LAYOUT), page()      # the template's background comes with it


# SLIDE 1
slide, frame = content_page()
heading(slide, frame, T, "四类任务，一套权重", "01 结论", font=FACE, cjk_font=HAN)
write(slide, frame.body, "切任务只换输入查询，不重训。", size=BODY_PT, colour=INK,
      font=FACE, cjk_font=HAN)

# SLIDE 2
slide, frame = content_page()
heading(slide, frame, T, "证据", "02 消融", font=FACE, cjk_font=HAN)
points(slide, frame.body, T, ["去掉时序颈：44.7 对 46.3", "去掉语义查询：不收敛"],
       font=FACE, cjk_font=HAN)

prs.save(os.environ["PPT_OUTPUT"])                   # the last line of every program
```

**`prs.save(os.environ["PPT_OUTPUT"])` is the last line of every program**, draft builds
included. Nothing saves for you and nothing else is the place to save to.

Then write the helpers *your* pages need. Beyond the header, the source line and the
page number, page types have no reason to share a skeleton (§3).

**Default — a `*` after the geometry in every helper you write.**
`def card(slide, x, y, w, h, *, fill=SOFT, bold=False, align="left")` — the boxes stay
positional because they are always the same four numbers in the same order, and
everything after them is named at the call site. Seven or eight positional parameters is
the largest single cause of a build that will not run at all: an empty string where a bold
flag goes, an argument short, a keyword the function never had. `ppt_layout`'s own helpers
are shaped this way.

**The agenda is the page that goes wrong most often.** It is a map of the argument:
the movements the talk makes, each named with one line saying what it settles.
*Default — five to seven of them*, and nothing counts them; the outline's own sections
usually run to eight to twelve, so a deck may well want more. What is not a default:
it is **not an index of slide numbers**. "3 Introduction, 4 Limitations, 5 Overview…"
enumerates the file rather than the talk, and tells an audience nothing the page numbers
do not.

**Ask how wide the text is instead of guessing at it.** The commonest geometry bug in a
generated deck: a box sized to what the string looks like wraps it onto a second line, the
second line pushes into whatever sits below, and the page reads as broken rather than as
tight. `text_size` is the width and height the copy really takes and `fits` is the
yes-or-no, both answered before anything is placed. Where a line must not break whatever
happens -- a number, a label, a kicker, a card heading -- say so on the frame `write` hands
back: `write(...).shape.word_wrap = False`, where `.shape` is the text frame itself. Extra
width costs nothing; an unfilled text box has no fill and no outline.

**`print()` is how those answers reach you.** Whatever the program writes to stdout comes
back in the build reply, on a failed build as well as one that worked -- so `text_size`,
`fits`, `formula_type_size`, `len(units(slide))` and the theme your script actually
resolved are one `print` from being visible. Print the measurement, not the whole theme:
the last 20,000 characters are what comes back. It is also the only way to find a page
that runs and draws the wrong thing.

The agenda's geometry breaks the same three ways every time, so compute it:

- **A two-digit number needs a box that fits two digits.** `01` and `10` fold into two
  stacked characters in a box sized for `1`. Set `write(...).shape.word_wrap = False`, or
  give the box the width the widest label needs.
- **Lay repeated rows on a pitch.** With `n` rows in height `H` the pitch is `H / n`, the
  label sits at `y = top + i * pitch`, its description at a fixed offset below.
- **A label and its description are one block.** If the description can run to two lines,
  the pitch has to allow for two, or row `i + 1` lands on top of it.

The same three apply to any page built from repeated rows or cards.

### The helpers, by signature

Everything below is already beside your script: what each helper takes, what it hands
back, and what it can be asked before it draws. Read this rather than the modules (51k
tokens of source, in context for the rest of the run), and a helper that is neither here
nor in a reference this page links is one you should not be calling. The drawing base
`ppt_charts` is built on is left out below and is in §6.

Every `deck/build/references/...` link on this page is a file in the build directory,
beside the modules it documents, and that whole path is what `read_file` takes -- the file
tools resolve against the workspace, not the directory your program runs in. A table drawn
without reading its reference gets the bare default.

`ppt_layout`, always present:

| | |
| --- | --- |
| `page(kicker=True, footer=False)` | → `Frame(kicker, title, body, footer)`, four boxes inside the safe area; ask for the footer on a page that cites and the body gives up the strip for it |
| `Frame(kicker, title, body, footer)` | the same four boxes as a value you can build. `page()` is the ordinary page and not the only one: a frame you make yourself is what a left rail, a full-bleed opener or a title over two thirds of the canvas is made of, and every helper that takes a frame takes yours without knowing the difference |
| `frame.holding(*heights)` | the same frame with its body cut to the run these heights add up to, and the leftover split as air above and below that run instead of a band of white along the page's foot. The bands and the gaps between them, in the order they occur; a list works too. For the path where the page measures its bands and takes them off a cursor -- **not** for a run something else already spreads into the whole body (`card_group(..., down=True)`), whose cards would then touch. Only the body moves, so it can be asked before or after `heading`, and a run with no slack comes back unchanged |
| `Box.corners(x0, y0, x1, y1)` | a box from its **two corners** |
| `Box.at(x, y, w=, h=)` | a box from a corner and a **size**; the size is keyword-only. It reads back under the same four names (`box.x`, `box.y`, `box.w`, `box.h`) as well as `box.x0..y1` |
| `box.rows(n, gutter=GUTTER, weights=None)`, `box.columns(...)` | n boxes filling this one |
| `box.grid(cols, rows, gutter=GUTTER)` | row-major cells |
| `box.split_left(fraction, gutter=GUTTER)`, `box.split_top(...)` | two boxes, the first taking `fraction` |
| `box.inset(dx=PAD, dy=None)` | a smaller box inside this one |
| `stack(box, gutter=0)` | a cursor down a region: `.take(height)`, `.rest()`, `.skip(height)`, `.left`, `.short_by(*heights)`, `.spread(*heights)`, `.centre(*heights)`; bands are adjacent, `skip` is the gap |
| `picture_fit(slide, image, box, theme, *, caption=None, size=LABEL_PT, align="center", font=None, cjk_font=None)` | a picture scaled to fit the box whole, centred, caption under it |
| `heading(slide, frame, theme, title, kicker=None, *, tint="surface", bleed=True, size=TITLE_PT, anchor="middle", font=None, cjk_font=None)` | §6.5 — `anchor` is the template's, not this default; §8 |
| `write(slide, box, text, *, size=BODY_PT, colour="#000000", font=None, cjk_font=None, bold=False, align="left", anchor="top", spacing=1.15)` | `text` may be a list of paragraphs |
| `points(slide, box, theme, items, *, size=BODY_PT, numbered=False, mark="•", colour=None, font=None, cjk_font=None, mark_colour=None, spacing=1.25)` | §6.5 — it takes the theme, so the faces are optional here |
| `card(slide, box, theme, *, icon=None, title="", body=(), tint="surface", size=BODY_PT, title_size=LEAD_PT, font=None, cjk_font=None)` | §7 |
| `card_group(slide, box, theme, items, *, down=False, gutter=GUTTER)` | a row of cards across the region, or a column down it with `down=True`. Each item is a dict of `card`'s own arguments, so every field it carries reaches the card; a row is levelled with `card_size` and centred, a column keeps each card's own height and `spread`s the leftover. §7 |
| `formula(slide, box, text, theme, *, size=BODY_PT, align="left", anchor="top", font=None, cjk_font=None)` | §9 |
| `plane(slide, box, theme, tint="surface", radius=False)` | a painted region |
| `rule(slide, box, theme, thickness=0.03, colour=None)` | a hairline **0.06in below** the box and **at most 1.05in long** — a short mark under a heading or beside a number, not a divider across a region. For a full-width line draw a thin `plane`, or take the box a chart hands back |
| `table(slide, box, rows, theme, *, weights=None, size=LABEL_PT, numeric_from=None, style="minimal", emphasize_rows=(), emphasize_columns=(), group_rows=None, indent_rows=(), total_rows=(), marks=None, header_size=None, align=None, rule_pt=None, grid_pt=None, row_height=None, header_height=None, padding=None, fill=True, column_rules=True, banding=False, fills=None)` | §6 |
| `mark(slide, box, theme, kind, value=None, *, colour=None)` | [deck/build/references/tables.md](deck/build/references/tables.md) |
| `overlaps(boxes, tolerance=0.01)` | → the `(i, j)` pairs that overlap |

Constants: `CANVAS_W`, `CANVAS_H`, `MARGIN`, `GUTTER`, `PAD`; the ramp `TITLE_PT`,
`LEAD_PT`, `BODY_PT`, `LABEL_PT`, `KICKER_PT`, `NUMBER_PT`, `BODY_FLOOR_PT`.

### What comes back, and how to ask before you draw

Every helper that *draws* hands back a `Drawn` — the five that compute instead hand
back what they computed: `page()` a `Frame`, `stack()` a `Stack`, `overlaps()` a list
of `(i, j)` pairs, a `Box` divider a `Box` or a list of them, and `mark()` a `Marks`,
which is a list of the shapes it made carrying `.box` for the ink they actually cover
— the one measurement that says whether a marked column is wide enough to read. A
`card_group` reads the same way: the cards' own `Drawn`s in order, carrying `.box` for
the run as a whole.

For the drawing ones: `.shape` is the python-pptx object it made and `.box` is **what it
actually covered**, not the box you passed in -- a rule sits below the box it underlines, a
picture keeps its own aspect, and copy that did not fit comes back taller than its box. An
attribute the tuple does not carry is looked for on the shape and then on the box, so
`write(...).paragraphs`, `table(...).columns` and `heading(...).x1` read straight through.
Place the next thing on the page off the last thing's `.box`, never off the number you
chose for it; `overlaps` takes those boxes and names the pairs that collide.

And every one of them can be asked before it draws, which is what turns a render
round into an `if`:

| | |
| --- | --- |
| `lines_needed(text, width, *, size=BODY_PT, font=None, bold=False)` | how many lines this copy wraps onto at that width |
| `text_size(text, width, *, size=BODY_PT, font=None, bold=False, spacing=1.15)` | the box the copy really needs, at the origin: `.h` is what to ask a stack for, `.w` is what the longest line actually sets |
| `points_size(items, width, *, size=BODY_PT, font=None, spacing=1.25)` | the same for a bulleted list, whose hanging mark and paragraph spacing `text_size` knows nothing about |
| `fits(what, box, *, size=BODY_PT, font=None, bold=False, spacing=1.15)` | yes or no. `what` is copy, or any box one of these handed back |
| `table_size(rows, theme, *, weights=None, size=LABEL_PT, style="minimal", numeric_from=None, group_rows=None, marks=None, header_size=None, indent_rows=(), row_height=None, header_height=None, padding=None, fill=True, box=None)` | where the table ends, before a cell of it is drawn. With `box` it is placed at that box's corner and held to its width |
| `picture_size(image, box, *, caption=None, size=LABEL_PT)` | the room the figure and its caption need inside `box`, off the image's own pixels |
| `formula_type_size(text, width, *, size=BODY_PT, font=None)` | the size `formula` will really set it at; one that comes back at `BODY_FLOOR_PT` wants a wider column, not another build |
| `the_largest_step_this_copy_takes(text, box, *, font=None, bold=False, spacing=1.15, wrap=False, largest=TITLE_PT)` | the biggest step of the ramp the copy still fits that box at — the only call that answers upwards. `wrap=False` keeps it on the lines you gave it; `wrap=True` is for copy meant to reflow. `largest` is the step to stop at and you name it: `TITLE_PT` for a label in a shape, `LEAD_PT` for a line that leads a band, `BODY_PT` for copy, `NUMBER_PT` when the copy is the figure. It is not inferred — a character count cannot tell a long word from a sentence |
| `card_body_box(box, *, icon=None, title="", title_size=LEAD_PT)` | where a card's copy starts, once the icon and the title have taken their line |
| `card_size(width, *, icon=None, title="", body=(), size=BODY_PT, title_size=LEAD_PT, font=None)` | how tall a card has to be for what goes in it — `max(card_size(w, **c).h for c in cards)` levels a row without padding it out to the page, which is what `card_group` does for you; reach for this on its own for a group that is not a plain row or column |
| `stack(box).room` | what is still unspoken for, as a box, **without taking it** — `fits(picture_size(fig, down.room), down.room)` is the whole question |
| `stack(box).short_by(*heights)` | how many inches the whole plan runs over, or 0.0 — measure every band, ask this, **then** draw. `rest()` spends the region it answers with; `room` is the same box and does not |
| `stack(box).slack(*heights)` | the same arithmetic the other way: how many inches the region has left once these bands are in it |
| `stack(box).centre(*heights)` | half that leftover above the run, half below, and hands the cursor back to chain. For a single run that sits against something beside it |
| `stack(box).spread(*heights)` | the leftover becomes the gaps *between* the bands, so the run ends on the region's bottom edge. **A lane of cards, and two columns that have to come out level.** Never less than `GUTTER` between them, so a run with no leftover overruns instead of welding — count the n-1 gaps into `short_by` and shorten the bands. No `skip` after it, which overruns by exactly what was skipped |

**A short label in a big box: ask, do not name a step.** Every call above except the last
measures downwards. What that leaves out is the label its box is far too big for: naming
the smallest step of the ramp puts a chevron label at 14pt in a shape 1.25in tall. Two or
three characters in a box over an inch tall want
`the_largest_step_this_copy_takes(label, one.box, font=F)`. Everything playing one role on
the page shares one size, so ask for each and take the `min`.

**Ask `lines_needed` before you fix a band's height.** Copy that runs long does not
shrink (§8) -- it runs out of its box and over whatever is under it.
`lines_needed(title, frame.title.w, size=TITLE_PT)` coming back 2 is the row below moving
down or the box getting wider, decided before anything is drawn.

`ppt_charts` answers the same kind of question about a chart, by running it against a
slide that draws nothing:

| | |
| --- | --- |
| `the_smallest_box_a_chart_needs(chart, theme, *data, **knobs)` | the smallest box this chart takes **this** data in, as a `Box` at the origin |
| `whether_a_chart_fits(chart, box, theme, *data, **knobs)` | whether it would take the box you have at all |
| `what_a_chart_will_do(chart, box, theme, *data, **knobs)` | the `Drawn` a real draw would return, with nothing written |

A chart's `Drawn` **is** a `Box` — the plot itself, what is left once the category labels,
the axis readings and the key have taken theirs — and it carries `where` (the chart's own
scale, so a rule at 80% goes where the chart put 80%), `names_not_written` and
`readings_not_written` (what it dropped for want of room), `marks_not_to_scale` (bubbles
drawn at the floor instead of to area) and `type_pt` (the step of the ramp its labels
landed on — a reading, never a setting). `nothing_was_dropped` is those three in one
boolean.

A chart that will not take its box raises `TooSmall`, carrying `short`: the deficit in
inches as `(across, down)`. **It raises before it draws anything**, so a `try/except`
around one has no wreckage to clear -- and `whether_a_chart_fits` is that `try/except`
already written.

`ppt_charts`, always present — twenty-three charts drawn as shapes. Each takes the slide,
a `Box` out of the grid, the theme, and its data; each fills the box, and there is no
size, face or colour to pass: type comes off the ramp above and steps down when a label
does not fit, colour comes off the theme. Signatures and the data shape each one reads:
[deck/build/references/charts.md](deck/build/references/charts.md) (§6).

`ppt_shapes`, always present — the 109 Office preset geometries a business page can
use, by their DrawingML names, and the two layouts built on them. Signatures in
[deck/build/references/shapes.md](deck/build/references/shapes.md), with §7.5.

`ppt_template`, only when a template is bound:

| | |
| --- | --- |
| `prototype(template, number)` | the template's page `number`, counting from 1 |
| `adapt(presentation, prototype, texts=None, pictures=None, drop=(), keep=(), items=None, title=None, subtitle=None)` | clone a page and fill it in; §8 |
| `units(container)`, `arrangement(run)` | the page's repeating units; how a run is laid out |
| `boxes(run)` | each unit's `(left, top, width, height)` in inches, page order — a size, **not** a `ppt_layout.Box`, so `boxes(run)[0][2]` is a width and not a far edge |
| `place(unit, box)` | move one unit; the box is `(left, top, width, height)` in inches, and a `ppt_layout.Box` is accepted and converted from its two corners |
| `fill(run, items)` | write `items` into the units of one run and delete the spares — what `adapt(items=...)` does, reachable per run |
| `shape_at(slide, number)`, `drop_shape(shape)` | one shape by index, counting from 1; remove it |
| `replace_text(target, text, new=None)`, `replace_picture(shape, image, fit="contain")` | in place, keeping how the template set it |
| `clone_page(presentation, prototype)` | when `adapt` is more than the page needs |

**With a template bound, the title row is the template's.** `house_style` measures where
its own pages put one, so use the box it names — `title_row_box_in`, at the size it names
— and decide it **once, in the shared setup**, with every content page calling that one
thing. A page block that computes its own title coordinates is how a deck ends up with
three title rows (§8, §12).

Five of those raise a question a signature cannot answer:

- `replace_text(shape, "新文字")` writes one shape. `replace_text(slide, "旧文字", "新文字")`
  finds whatever on the page holds that string and writes it — the form to reach for,
  because finding the shape is the tedious half.
- `replace_picture(shape, image, fit)` — `"contain"` shrinks the frame to the picture's
  own proportions; `"cover"` crops the picture to fill the frame as it stands. Either way
  it refuses a landscape figure in a portrait frame rather than squashing it.
- `units(container)` returns runs of repeating sibling groups — a card row, an agenda list.
- `arrangement(run)` returns `("row"|"column"|"grid"|"irregular", rows, cols)`. Nothing
  **moves** the survivors for you: closing the hole four units leave on a 2x4 grid is a
  design decision, and `place` is how you make it.
- `fill(run, items)` writes items into a run's units and removes the ones left over, group
  and all: `fill(units(slide)[1], [["03", "本文", "小标题"]])`. Reach for it where
  `adapt(items=...)` was not enough, which is the two-run page below.

**Re-flow off the geometry the template already fixed.** `boxes(run)` hands back the
pitch and the size the template drew, and that tuple is exactly what `place` takes;
computing a position without it is guessing at coordinates the page already holds.

`ppt_theme` gives `THEMES` and `rgb`. `ppt_icons` gives `add_icon(slide, name, left,
top, size, colour, width_pt=1.75)`, `find_icons(term)` and `ICON_NAMES`.

**`adapt(items=...)` in detail.** One entry per repeating unit, and the units left over
are deleted. Each entry is a list positional over that unit's text shapes, or a dict
keyed by the text a shape holds now. In a list: `None` keeps the template's own words, a
string replaces them, and `""` empties the shape — **except over a number, where `""`
means "this unit's number" and the slot is renumbered for its position**, in the
template's own padding — six sections in a page that ships eight numbered slots come out
numbered 01 to 06.

**Count the unit's text shapes and give one value each.** A list shorter than the unit
leaves the remainder exactly as the template wrote it — what you want over a number and
not what you want over example copy. An agenda unit holding three text shapes,
`[number, heading, small-heading]`, filled with `items=[["01", "第一部分"], ...]` ships the
template's own `单击添加小标题` once per surviving slot; `["01", "第一部分", ""]` empties it.
Pass more values than the unit holds and it raises, naming each shape it found — the
cheapest way to learn the count.

**`items` fills one run, and a page can have two.** It fills the longest —
`max(units(slide), key=len)` — and every other run is shapes it was not told about, so
their words get emptied like any other unnamed text. One template's four-card page is
two runs of two (cards 01–02 and 03–04, grouped in pairs), where
`adapt(..., items=[a, b])` ships two filled cards beside two blank ones. `fill(run,
items)` is the second half:

```python
slide = adapt(prs, prototype(tpl, 4), title="四项发现",
              items=[["01", "本文", "第一项"],          # the longest run
                     ["02", "本文", "第二项"]])
fill(units(slide)[1], [["03", "本文", "第三项"],       # every other run, by hand
                       ["04", "本文", "第四项"]])
```

`units(slide)` after `adapt` returns tells you how many runs there are — check it
whenever the render shows more repeated cards than you passed items for. Two things
change once it has run: those units hold no text, so **address them positionally and not
by a dict keyed on the template's words** (every key such a dict could match now holds
`''`, and it raises `KeyError` listing empty strings), and `""` no longer restates a
number. Write `"03"` yourself.

## 5. Figures come from the sources

`ppt_ingest` extracts them and nothing generates them. Never generate a substitute
for a real product UI, logo, person, scientific result, published figure or
statistical claim.

It reads PDFs, text, HTML, CSV, images, and office documents -- `.docx`, `.xlsx`,
`.doc`, `.odt` -- by converting those to PDF first, which needs LibreOffice. Anything
it could not read comes back as a finding naming the file: a source that is not among
the deck's evidence is one you must not write pages as though you had read.

**Look before you place.** `ppt_figure_inspect` returns a figure as an image with the
label its own source gave it, the width past which the bitmap softens, and the `concerns`
the extraction recorded — measured sentences you can act on or overrule, not a verdict.
Unseen, you cannot tell a legible plot from a scanned blur, a figure from a logo that
survived extraction, or a composite you should be cropping.

**Cite the label the source printed**, and only when that figure is the one on the page.
A page captioned "Fig. 4" showing Figure 5 is refused (§12).

**Keep source notes short.** One compact line at the bottom, such as
`来源：EverOS 官网；Mem0 官方文档。` A source note is a pointer, not a paragraph: URLs,
methodology caveats and analytical conclusions go in the body or the speaker notes.

A table in the sources is evidence to read, not a figure to place: retype it (§6).

**A picture set flush against a panel fights the panel's outline.** Inset it —
`box.inset(...)` gives the picture the padding the panel's own copy has.

Never distort aspect ratio, crop away interpretive labels, include a neighbouring
caption by accident, duplicate a printed caption, or leave transparency composited
onto black. If evidence is unreadable at the size it has: crop to the panel you
cite, give it more of the page, or rebuild it from the exact values.

Fetched pictures are first-class, not a fallback. Reach for the two paths of §2 whenever
a page names something with a face -- a product, a company, a standard, a published chart
-- not only when the materials left a hole; their results carry pixel dimensions, so a mark
that would land soft can be rejected before it is placed. `ppt_fetch` carries the URL with
the picture so a page can credit it, and the listing's own caption or alt text passed as
`ppt_fetch(caption=...)` travels into the same catalogue field a paper's figure fills. Copy
the page's words, never your reading of the picture.

Generate with `ppt_generate_image` only after both paths find no suitable existing
visual; a generated image illustrates a concept and never replaces evidence. Ask at the
shape of the region it will sit in — `aspect_ratio` takes `16:9`, `4:3`, `3:2`, `1:1`,
`3:4` or `9:16` and defaults to `16:9`, so a portrait strip gets a landscape image to
crop unless you say otherwise. Fetched *numbers* are another matter: those are a source
the user did not choose, so anything the deck states as fact still comes from the
materials.

A cover does not need a figure. Its job is the title, who wrote it and where, and a
paper's Figure 1 pressed into its corner is smaller than the page it will get later.

## 6. Charts and tables, drawn

There is no chart library here and matplotlib is not a dependency of this route.
Charts are shapes — rectangles, hairlines, marks and labels — which is what keeps
them editable and on the deck's palette.

**`ppt_charts` draws twenty-three of them for you**, and the mapping from a value to a
length is the half you must not write by hand. Import what the page needs:

```python
from ppt_charts import column, horizontal_bar, waterfall
column(slide, frame.body, T, [("East", 185), ("South", 142)], accent="East", unit="M")
```

The signatures are in §4.

### Which form, and what to draw when none of them can be

[deck/build/references/charts.md](deck/build/references/charts.md) carries the rest: the
twenty-three signatures, a row per form saying what it *encodes* (reference, not a
constraint), and what to substitute where the shape wanted cannot be reached from
rectangles and straight lines at all — a pie, a gauge, a radar, a sankey, an area chart.
Open it before choosing a form.

**The twenty-three are shortcuts, not a ceiling.** Their base is public on `ppt_charts`
too — the ink (`rect`, `disc`, `ring`, `hline`, `vline`, `poly`, `write_label`), the
scale (`span`, `snap`, `linear`), the palette discipline (`series_paints`,
`stack_paints`, `shades`, `emphasis`, `ink_on`, `contrast`) and the label fitting
(`type_face`, `text_width`, `pick_size`, `line_height`). Where none of the forms is the
shape of what the page argues, write the form; the rules below still hold. A chart you
write that raises `TooSmall` and returns a `Drawn` also works with `whether_a_chart_fits`,
`what_a_chart_will_do` and `the_smallest_box_a_chart_needs`.

### The rules a drawn chart obeys

- **Length is the value.** Every bar, segment and track is exactly proportional, and a
  length axis starts at zero. A misdrawn axis is caught only by you looking at the render
  (§10), which is the reason to let `ppt_charts` map the values.
- **Default — label the mark**, and leave the legend to the plot where no label can reach
  its own. Name the units, and put the axis maximum where the reader can see it.
- **Default — one thing is accented.** The bar, segment or point carrying the claim takes
  the accent and the rest take `muted` or `grid`; how many colours a chart ends up with
  follows what it encodes, not a quota. `SERIES` in order is for a genuine multi-series
  plot, and its later colours are quiet on a light ground, so give them the supporting
  series.
- **Baselines and axes are hairlines.** The charts draw their own (§3 for what `rule` is
  and is not). No vertical gridlines, no frame around a bar chart, no ground behind a plot
  unless it separates layers.
- **Never invent a value.** No placeholder, no number reconstructed off a plot you cannot
  read, no interpolated point. Where the series has a hole, the page says so.

For the compact bars beside a table or under a claim, a row of rectangles beats any chart
object: you control the length, the colour of the one bar that matters, and where its
value sits — `horizontal_bar` in a `body.rows(3)[2]` is that page. Do not reach for
`add_chart`: it arrives with Office's own six colours and gridlines, as recognisable a
tell as any stock template.

**Never draw a table with a bare `add_table`**, which arrives with Office's own look — a
white hairline around every cell, banding on, and a header style that fights the deck's
palette. A screenshot of a source table is no better: another typeface, and it cannot be
reweighted around the page's conclusion.

Everything above that ban is yours. **`ppt_layout.table()` is the shortcut** for the
ordinary comparison table — the Office look taken off, plus the part that is arithmetic
rather than design: **it sizes each column from what that column holds**, measures each
row from the lines its cells really wrap onto, and spreads the rows into the box you gave
it. Its look is defaults, and every one is a keyword away (§4). **Or draw the table
yourself** out of `Box`, `text_size`, `write` and `plane`, the right call whenever the page
wants a grid `table()` does not draw: merged cells, a header spanning three columns, an
icon inside a cell, a sparkline down one. Both paths, a worked hand-drawn table and the
seven `mark` kinds are in
[deck/build/references/tables.md](deck/build/references/tables.md). Open it when a page carries a table.

Compose freely around one: a two-product comparison may be two parallel regions with the
same row labels rather than one table, and a conclusion may sit on its own baseline
below.

Preserve units, scales, qualifiers, series meaning and source labels exactly.

## 6.5 The page's frame, and the helpers that fill it

```python
from ppt_layout import PAD, card, card_body_box, card_size, fits, heading, page, plane, stack

frame = page()
heading(slide, frame, T, "把任务定义抽象成查询，网络本身就与任务无关",
        "02 统一范式 · 任务切换 = 换一组输入查询")
said = [("route", "分类头没了", "类别成了网络的动态输入，语义表示只通过损失监督学到。"),
        ("layers", "权重只有一套", "推理时按需拼装查询集合就能热切换任务，不重训。"),
        ("gauge", "代价", "四类任务合并统计 46.3 mAP，比四套权重各自训练高 0.2。")]
lanes = frame.body.inset(PAD, 0.0).columns(3)
tall = max(card_size(lanes[0].w, icon=i, title=h, body=b, font=FACE).h for i, h, b in said)
cell = stack(lanes[0]).take(tall)
if not all(fits(b, card_body_box(cell, icon=i, title=h)) for i, h, b in said):
    tall = max(card_size(lanes[0].w, icon=i, title=h, body=b).h for i, h, b in said)
frame = frame.holding(tall + 2 * PAD)     # the band is measured, so the body is cut to it
ground = frame.body
plane(slide, ground, T, tint="surface")
for box, (icon, head, body) in zip(ground.inset(PAD).columns(3), said):
    card(slide, box, T, tint="background", icon=icon, title=head, body=body,
         font=FACE, cjk_font=HAN)
```

**The body is the room a page may use, not the run it uses.** `page()` hands back all of
it, this page uses one measured band, and everything left over stays at the foot unless
the page says so: `frame.holding(tall + 2 * PAD)` cuts the body to the run and splits the
leftover as air above and below it. Ask it once the bands are measured and before a cursor
runs down them -- the kicker, the title and the footer do not move, so it reads the same
before or after `heading`, and a body the run already fills comes back unchanged. Pass the
bands and the gaps between them in the order they occur, and pass the taller run where two
lanes differ. **Not** for a run something else already spreads into the whole body:
`card_group(..., down=True)` given the body puts the leftover between its own cards, and a
body cut to the sum of their heights first leaves them touching. More on where the page's
slack goes below.

**`heading()` is one ready-made top edge** — a quiet ground behind the title row with the
kicker and the title set on it, stopping short of the body, and `bleed=False` to keep that
ground inside the safe area. It sets the title in `page()`'s own box rather than the
template's, so reach for it where you are composing a page yourself (§8) and where that box
agrees with the `title_row_box_in` the template was measured for (§4).

**The header is one compact group.** A section tag, title and one explanatory line sit
close enough to scan as a single unit; the larger vertical break belongs between that unit
and the body. Tighten those internal gaps when the actual title runs to fewer lines than
the template's example did.

**Two claims in one frame read as one paragraph with a line break in it**, and putting a
mark in front of each does not change that reading. **A mark is not a structure.** A
single paragraph of explanation is a paragraph and nothing here is about one; what the
build reports as `listed_claims` is a box carrying two or more parallel claims (§12).

**What a card holds, and what it costs, before it is drawn.** `card` paints a rounded
plane in the tint you name and sets the icon's square, the title bold beside it and the
copy under it, all inside `PAD` (§7). It fills whatever box you hand it, so four cards off
`frame.body.columns(4)` are each as tall as the body.
`card_size(width, icon=, title=, body=, font=FACE).h` is how tall one has to be for what
goes in it, and `max(...)` across a set levels a row without padding each card out to its
cell; `fits(body, card_body_box(box, icon=..., title=...))` says whether the copy went in.
Both come off one estimate, which reserves for the widest face it knows when told none --
so where the unnamed call refuses the levelled height, take the height it asks for.

**Dividing a region.** `columns`, `rows` and `box.grid(cols, rows)` cut a box into cells,
row-major; a `stack` given `gutter=(box.h - n * tall) / (n - 1)` spends the region's slack
as air between its bands instead of leaving it in a heap at the bottom. `plane(slide, box,
T, tint="surface")` paints a region, and it counts towards `evidence` like any other
filled shape (§12).

`points(slide, box, theme, items, numbered=False)` writes a marked list, a hanging mark per
item and `numbered=True` for an ordered one; `points_size` measures one first, which
`text_size` cannot.

**Never guess a y coordinate.** `stack(box)` is a cursor down a region: `take(h)` hands
back the next band and moves on, `rest()` hands back everything still unspoken for, and
`skip(GUTTER)` puts the deck's one gutter between them. Bands are adjacent otherwise, so
heights from `table_size` and `the_smallest_box_a_chart_needs` add up to what the region
has. A coordinate you were *given* is different and you should paste it: `house_style`'s
`body_area_as_code` (§8) is measured off the template.

**Full inside, air between.** A component is measured and given exactly that much
(`card_size`, `text_size`, `picture_size`, `table_size`); the room the page has left over
goes *between* components, never inside one. A cursor runs from the top of its region, so
left alone it does the opposite -- every unused inch piles up underneath and the page has
a band of white along its bottom edge. Three calls spend it, all of them before the first
`take`:

- `stack(box).spread(*heights)` -- the leftover becomes the gaps, and the run ends on the
  region's own bottom edge. A lane of cards, a table over a chart, and **two columns given
  the same region, which is what makes them end level.** The gap it leaves is never under
  `GUTTER`: components any closer read as one unfinished shape, so bands with no leftover to
  share have to be shortened by the gaps between them rather than run flush against each
  other. `card_group(..., down=True)` is this call with the cards' heights measured for you.
- `stack(box).centre(*heights)` -- half above and half below, for a run that nearly fills
  its region and sits against something taller beside it.
- `page().holding(*heights)` -- the page's own leftover, taken out of the body before a
  cursor runs down it at all: the body is cut to the run these heights add up to, and what
  is left becomes air above and below that run instead of a band along the foot. Not half
  and half -- the white over the body is the `GUTTER` under the heading and the white under
  it is the `MARGIN`, so an even split still leaves 0.44in more at the foot; the two are
  measured from the heading's edge and to the page's and come out reading equal. **This is
  the call when the page's bands were measured and taken off a cursor** -- a table over a
  card row, a figure over its conclusion -- because that is where the leftover has nowhere
  else to go: spread into the one gap between two groups instead, a whole page's slack is
  reported as a blank field in the middle rather than one at the foot. It moves the region
  and not a cursor, so `columns`, `split_left` and a stack down each lane all come out of
  the same corrected band and two lanes still end level; where the lanes differ, pass the
  taller run. **Not** for a run something else already spreads into the whole body:
  `card_group(..., down=True)` given the body spends the leftover between its own cards, and
  a body cut to the sum of their heights first leaves them touching. A body the run already
  fills comes back unchanged, and so does one the run overruns -- growing it would put type
  through the safe margin, and an overrun is `take`'s to refuse with both numbers.

Measured on one delivered deck: eleven of the eighteen pages an independent reader could
read ended their content between 60% and 70% down, and four more were two columns that
stopped at different heights.

**Spread components, never running copy.** The gap between two cards is air; the gap
between two paragraphs is a break in a thought, and three sentences pulled 1.9in apart
stop reading as one column at all -- rendered and compared, it is worse than leaving them
at the top. So when a copy column comes out far shorter than its region, neither call is
the answer: that is a page bigger than what it holds, and it is answered by turning the
copy into components (the same three sentences as three cards fill the column and land
level with the figure beside them) or by the page having more to say.

**And a chart is as tall as the box you hand it.** A bar's length is the reading, so a
chart given a short box draws short bars and leaves the page empty under them -- the same
page then looks crowded and unfinished at once. Give it the band (`down.rest()`, or the
height `short_by` says is free), not a box guessed at the size the chart "should" be.

**Measure every band, ask `short_by`, then draw.** `take` refuses a band the region
cannot hold, and it refuses at the band that asked -- which is the last one, not the one
that was too tall. So a plan checked band by band as it is drawn fails at the bottom of
the page with nothing said about the top, and the fix is never where the refusal is.
Measure all of them first, hand the heights to `stack(box).short_by(*heights)`, and it
answers how many inches the whole plan runs over, or 0.0. Take that off the bands that
can give it up -- the ones whose content is not the page's claim -- before the first
`take`. This is the most common way a build script dies.

```python
rows = [["任务", "四套权重", "一套权重"],
        ["实例分割 VIS", "46.1", "46.3"],
        ["语义分割 VSS", "52.4", "52.6"],
        ["全景分割 VPS", "50.8", "50.9"],
        ["指代分割 RVOS", "61.2", "61.4"],
        ["四类合并", "46.1", "46.3"]]
answer = "四类任务一套权重，切任务只换输入查询。"
fig, cap = f"{FIGURES}/fig2.png", "Figure 2：架构（论文原图）"
grid = table_size(rows, T, size=BODY_PT)
said = text_size(answer, frame.body.w - 2 * (PAD + 0.10), size=LEAD_PT, bold=True, font=FACE)
share = 1 - grid.w / (frame.body.w - GUTTER)
above, _ = frame.body.split_top(1 - (said.h + 2 * PAD) / (frame.body.h - GUTTER))
figure, _ = above.split_left(share)
down = stack(frame.body)
tall = max(grid.h, picture_size(fig, figure, caption=cap).h)
figure, lane = down.take(tall).split_left(share)
picture_fit(slide, fig, figure, T, caption=cap)
table(slide, lane, rows, T, size=BODY_PT)
down.skip(GUTTER)
band = down.take(said.h + 2 * PAD)
plane(slide, band, T, tint="accent_soft", radius=True)
write(slide, band.inset(PAD + 0.10), answer, size=LEAD_PT, bold=True, colour=INK,
      font=FACE, cjk_font=HAN, anchor="middle")
```

**`take(said.h + 2 * PAD)` and not `rest()`, and that is the whole of what keeps this page
from being a void.** `rest()` hands back everything still unspoken for, so a tinted plane
drawn on it is as tall as whatever the bands above happened to leave -- and this one holds
one line. `said.h` is already in hand two lines up, which is the point of measuring: the
band is cut to the sentence, and room left over stays unpainted rather than being covered.
Copied without that, this fragment is the commonest void in a delivered deck -- one run put
it on three pages, each a colour panel four times the height of the copy inside it. Reach
for `rest()` when what goes in the band is sized to the band by construction -- a
`picture_fit` filling it, a table with `weights` -- and never for a plane with a sentence
on it.

**Cut the region to what goes in it, in both directions -- and cut the band to the figure,
not the figure to the band.** `table_size(rows, T)` with no box answers both directions,
and the lane and the band above are cut to its two fields (§4), the band taking the larger
of its `.h` and what `picture_size` says the figure needs. `picture_fit` scales an image to
fit its box whole without cropping and centres what is left, so a band chosen by eye leaves
a strip of white above the figure and another below; `picture_size(fig, region,
caption=cap)` reads the image's own pixels, and handing that `.h` to `stack.take` leaves
the centring nothing to centre in. A lane chosen by eye fails the other way: what sits in
it prints past its own edge, which the file measures as fitting and only the render shows.

## 7. Icons, on the cards and on the blocks beside them

```python
from ppt_layout import card
from ppt_icons import add_icon, find_icons, ICON_NAMES   # 1304 Tabler Outline names

card(slide, box, T, icon="target", title="语义查询是必要的",
     body="去掉 Qsem 改用线性分类头：YouTube-VIS 44.7 对 46.3")
add_icon(slide, "clock", Inches(0.7), Inches(2.1), Inches(0.42), ACCENT)
```

**`card(icon=)` takes one, `card_group` carries it into a whole row or column of them, and
so does a block drawn without a card.** Not every block needs one. Pick the icon for what the block argues, not for a noun in its title. An icon
takes the card's first line whether or not a title shares it, so what room the copy has
left is `card_body_box`'s answer (§6.5).

`add_icon` is for the icons that are not on a card: beside a kicker, in the corner of a
metric, at the head of each band of a hand-drawn panel (§6.5). Stroked vectors, so they
scale and stay editable after export, in whatever colour you pass. The ink is inset in the
square by no fixed fraction -- from nothing of its height (`minus`, a rule) to nearly all
of its width (`json`) -- so a row of icons centred on their squares is not centred on its
ink. `the_ink_an_icon_covers(name, size)` is the box the strokes really cover, and
`add_icon` hands back that box measured off what landed.

**Check the name before you write it, with a shell rather than a build.** Every name is
a key in `deck/build/icons.json`, so `grep '"wave"' deck/build/icons.json` answers in a
shell and costs nothing:

```bash
python3 -c "import json;print([n for n in json.load(open('deck/build/icons.json')) if 'wave' in n])"
```

**A guessed name is not free.** `add_icon` raises `LookupError` on a name that is not
there, and a raise ends the whole script -- every page after it goes unwritten and the
build round is spent. One run guessed `wave-sine` and lost a round to it. The refusal
does name the nearest three, which is why guessing feels cheap; it is cheap only if the
guess was the last thing the script did.

**Search by meaning, not by filename.** `find_icons("deadline")` returns `calendar_due`
and `find_icons("risk")` returns `warning`: every icon carries its upstream tags and
shelf and the search reads them, so the word you would use on the slide is a good enough
query. It is a function in the build script, so it answers on the next build -- put
every icon you are unsure of in one `print(find_icons(...))` and read them all from one
round. A few hundred names need no lookup at all, grouped by what they are for in
[deck/build/references/icons.md](deck/build/references/icons.md).

Keep them small -- an icon is a mark beside type, so the square follows the line it labels
rather than a size of its own -- and give each real space. On a card that arithmetic is
`card()`'s and not yours.

## 7.5 Processes, flows and timelines

A sequence is drawn with the shape that means sequence: five rectangles with gaps between
them is a list, five chevrons that interlock is a process, and the difference is legible
from the back of the room. `chevron_row`, `timeline`, `connect` and `preset` are how, and
never by computing the geometry yourself.
[deck/build/references/shapes.md](deck/build/references/shapes.md) has the signatures, the
label box to write into, and the knobs that are angles in degrees rather than fractions.
Open it when the page has a sequence, a branch or a route.

## 8. Inside a user's template

Its master, theme, layouts and canvas are the deck's house style, and a deck whose theme
is not the template's is refused (§12).

**A template has structural pages and editable content examples.** For a content page,
start from the nearest example by information shape: replace its text and pictures, delete
spare repeated units, and move or resize the surviving regions when the content needs it
-- a prototype is a starting composition, not an immutable form. Where no example carries
the page's information shape, compose the page instead (below).

**`ppt_template(project=..., pages=[4, 5])` reads an example page back as the python-pptx
that would draw it** — up to six pages a call. It is flat and literal: every position in
inches, inherited sizes and colours resolved, groups opened, and a `# [n]` above each
shape, which is the numbering `shape_at` and an integer key in `adapt` both use. The
page's own pictures are written into the build directory beside your script, so an
`add_picture("template_00.png", ...)` line in it runs as pasted, and the imports it needs
head the block as comments. Read a page this way when you need its real numbers -- a card
row's pitch, the title's exact box, the accent it really uses. What it cannot reproduce is
emitted as a comment naming itself -- a custom-drawn shape, a gradient, a pattern fill or
a semi-transparent fill -- and those are the pages to clone rather than redraw.

### The pages you clone

`ppt_template` names them —
`house_pages: {"cover": 1, "agenda": 2, "section": 3, "closing": 11}` — and renders them.
Those four are what a reader recognises the house by: a deck that draws its own cover
announces itself as not the user's before a word of it is read.

```python
from ppt_template import adapt, prototype, shape_at, units, fill
tpl = Presentation(os.environ["PPT_TEMPLATE_SOURCE"])   # the original, with its example pages
adapt(prs, prototype(tpl, 1),                    # prototype(tpl, N) counts from 1, like the menu
      title="项目标题",                            # the page's own title row, by role
      texts={"探索通用行业创新创业新机遇": "本次汇报副标题"},   # a cover's second line, by its words
      pictures={7: f"{FIGURES}/selected-figure.png"},
      keep=["20XX.XX.XX"],                       # left exactly as the template wrote it
      drop=["Presenter name"])                   # a key is the text the shape shows, or its
                                                 # index -- drop=[3]. Never a shape's name
adapt(prs, prototype(tpl, 2),                    # the contents page: one unit repeated
      title="目录", subtitle="Agenda",
      items=[["01", "第一部分", ""],              # one entry per unit, one value per text
             ["02", "第二部分", ""],              # shape in it; the spare units are
             ["03", "第三部分", ""]])             # deleted, not emptied
```

**Name the heading by its role, not by its shape.** `title=` and `subtitle=` write the
page's own heading rows -- its title placeholder, or the topmost line in the top third.
`texts={"单击此处添加页面标题": ...}` still works and needs you to know what the slot says now.
Without either, `items` alone leaves the header empty, and writing "目录" and "Agenda" back
as two new boxes over the clone is the one construction `template_underlay` refuses.

**Both land on a content page; `subtitle` runs out on a closing one.** `title` finds a row
on nearly any page and `subtitle` on nearly any content page. Where `subtitle` has nothing
to resolve to is a closing page, whose line under the title is the presenter's name, and a
cover that centres its title mid-page; there `adapt` raises rather than guessing, listing
every shape, so name that line in `texts={...}` off the render. A cover can resolve
`title` onto the presenter credit above the real title -- if the render shows the wrong
row rewritten, `texts` keyed on the words is the fix.

**Numbering: an integer key is the shape's place on the page**, as the read-back page
prints it (`# [n]`, groups opened, counting shapes that cannot be drawn as well as those
that can). `shape_at(slide, n)` is the same numbering after `adapt` returns.

**A string key is the text a shape holds, never the shape's name.** It matches on the words
in the box, so `drop=["Presenter name"]` removes the cover line that says that, while
`drop=["template-credit"]` raises `KeyError` listing every shape on the page — as does a
real shape name out of the file, `drop=["标题 4"]`.

**Text you do not name is emptied**, because the words in a template page are its example
copy; shapes you do not name keep what the template put there, which is how the page
arrives designed.

**An emptied shape is not a removed one.** `adapt` empties the text and leaves the box
standing, because the box is often the design -- a tinted panel, a numbered circle. So
`""` is not a spelling of delete: written into agenda slots a deck has no sections for, it
ships the numbered bubbles anyway. `drop=` removes a shape before the clone is filled, by
the same two keys as `texts`; `drop_shape(shape_at(slide, 7))` removes one afterwards, on
the slide `adapt` handed back.

**Never lay a new text box over a page you cloned.** The construction is `clone_page` for
the background, `add_textbox` for the copy, `replace_text` never called — and it ships the
template's own "单击此处添加长一点的副标题" under your own text on every page.
`placeholder_copy` and `template_underlay` both refuse it: if you cloned a page, every word
on it arrives by replacing a word that was there.

**The template's photographs are placeholders.** A cover's stock photograph of a meeting
table says nothing about the deck's subject: replace it with a figure from the sources or
take the frame out — `pictures={7: f"{FIGURES}/fig3.png"}`, or `drop=[9]`. An image under a
tenth of the page is different: an icon, a corner flourish or a rule, and part of the
design.

**A landscape figure does not go in a portrait frame.** `replace_picture` refuses when the
two are more than 2x apart and says both numbers: contained it becomes a strip in an empty
frame, cropped it loses its outer columns. Give the frame the box the figure needs:
`pictures={4: (f"{FIGURES}/fig2.png", (0.8, 1.6, 7.4, 4.2))}`, where the four numbers are
`(left, top, width, height)` in inches — a **size**, the same one `place` takes, and not
the two corners a `ppt_layout.Box` holds. A `Box` handed over whole is converted; what you
must not do is unpack one into four numbers, because `Box.corners(0.72, 1.24, 12.6, 6.7)`
read as a size draws a 12.6x6.7in frame off the side of a 13.33in page.

### The pages you compose

**Adapt first, and compose what no example can carry.** A page started from the nearest
example by information shape arrives already in the house style -- the pitch of its rows,
its title's own box, its accents, the spacing its designer chose -- and none of that is
work you then have to do. Filling one leaves nothing behind either: `adapt(items=...)` and
`fill` delete the units your content does not fill, so a six-card prototype carrying four
cards comes out as four cards. Compose where no example carries the page's information
shape, and say which example you looked at in `needs`; a deck none of whose pages came
from the template's own comes back as `template_adherence` (§12).

**Start the page from the layout the template's own content pages sit on.** That is
`layout_for_a_page_you_draw` in the measured `house_style`, and `add_slide` on it brings
the template's background with it — the corner device, the edge rules, the ground colour,
none of which you have to draw, which is what makes a deck of composed pages read as one
deck. `add_slide` on a blank layout, or painting your own background, is how a deck ends up
with the template surviving as a colour.

The rest of `house_style` is the frame for what you then put on it:

```
layout_for_a_page_you_draw: "Title Only"     # add_slide on this, and the background comes with it
title_row: at (0.72, 0.14) 11.88x0.98in, 28pt, Arial, left-aligned, anchored bottom, on 7 of its pages
title_row_as_code: from ppt_layout import Box, write; TITLE_ROW = Box.corners(0.72, 0.14, 12.6, 1.12);
    write(slide, TITLE_ROW, claim, size=28, bold=True, align="left", anchor="bottom",
          colour=INK, font=FACE, cjk_font=HAN)
type_pt: {title: 28, subtitle: 24, body: 18, secondary: 16, caption: 14}
face: "Arial"
safe_area_in: [0.72, 0.14, 11.88, 6.56]
body_area_in: [0.72, 1.24, 11.88, 5.46]
body_area_as_code: from ppt_layout import Box; body = Box.corners(0.72, 1.24, 12.6, 6.7)
```

```python
from ppt_layout import Box, write
T = THEMES[next(iter(THEMES))]                           # the template's own palette
layout = next(l for l in prs.slide_layouts if l.name == "Title Only")
slide = prs.slides.add_slide(layout)                     # the background comes with it
write(slide, Box.corners(0.72, 0.14, 12.60, 1.12), "消融：时序颈与语义查询是关键",
      size=28, bold=True, align="left", anchor="bottom",  # title_row_as_code, pasted
      colour=T["foreground"], font=FACE, cjk_font=HAN)
body = Box.corners(0.72, 1.24, 12.60, 6.70)              # body_area_as_code, pasted
```

and what goes in `body` is a structure out of
[deck/build/references/layouts.md](deck/build/references/layouts.md) with its modifier
layers, chosen for what this page argues (§3.5).

**Match the title row's anchor, not just its box.** The box is where the row is and the
anchor is where the ink lands in it, and the two readings of one 0.98in row are most of an
inch apart: a master that says `anchor="b"` sinks a single-line title to the bottom of the
row, while `write` anchors to the top unless told otherwise. `title_row_as_code` is that
call with the anchor already in it, and a composed page whose anchor disagrees with the
template's comes back as `title_row` (§12).

The subtitle row is reported the same way — and a template may set its own **centred**
under a left-aligned title, which is its designer's decision and not a mistake to
correct. Take the alignment from `subtitle_row_as_code` rather than assuming it matches
the title's.

**One scale for the whole deck.** Every page title at `type_pt["title"]`, body copy at
`type_pt["body"]`, captions at `type_pt["caption"]`, the same role at the same size on
every page (§3), and the floors of §3 under all three. `type_drift` (§12) measures it off
the render.

**Set every size yourself, and give the box the room it needs.** `write` turns autofit off
on purpose, so a box that cannot hold its copy comes back as a measurement rather than as
type quietly dropping to 10.8pt. The answer to `overset_copy` is a taller box, a wider
column, less copy or a second page — never a smaller size (§12).

**Stay inside the safe area.** `safe_area_in` is where the template keeps its own content;
a layout's artwork lives outside it, and copy laid across that artwork comes back as
`over_layout_art`.

**Ink is `foreground`. `surface` and `background` are what the ground is painted with.** On
a dark template those three are white, near-black and black, so reaching for "the dark
one" as a type colour puts near-black type on black. One line is measured off the render,
against the ground each box actually landed on: under **2:1** nothing is legible and
`unreadable` refuses the deck (§12). Nothing is said above that line, so everything
between "legible" and "comfortable" is yours to judge off the render (§10).

**Take the face from the page, not out of the theme.** `ppt_theme`'s `font_family` is
what the template's *theme* declares, and a template can declare 微软雅黑 while every run on
its example pages is Arial. `house_style`'s `face` is what the pages actually use — pass
it as `font=` and its CJK companion as `cjk_font=`, or a line of mixed text comes out in
two unrelated faces.

Which pages are cloned is decided in the outline (§2), not here. A page there that names
no prototype is listed back to you as a question — pick the nearest content example, or say
in `needs` why no example carries this page's information shape. A page that named one and
was not built on it comes back after the build as `prototype_kept`, which reports rather
than refuses (§12).

## 9. Formulas

**Never set an expression with `write`.** A formula in a text box is prose: it wraps where
the box runs out, and where it runs out is the middle of a symbol — and every subscript in
it is flat, so the one thing the notation carried is gone (`flat_formula`, §12).
`formula()` sets it as one unbreakable line, steps down the ramp until it fits, and splits
at the expression's own separators at the floor; `formula_type_size` says which step it
will land on. [deck/build/references/formulas.md](deck/build/references/formulas.md) has
the notation it reads. Open it when a page carries mathematics.

## 10. Look at the deck

`ppt_build` renders a **batch of pages** back per call, each labelled with its number
and anything measured on it, and says how many of the deck's pages it did not show. So
looking at the deck is a few calls rather than one per page: `page_from=` walks it a
batch at a time from where you left off, and `slides=[4, 5, 6]` asks for the pages you
just edited. How many a call carries is configurable, and `slides`'s own description in
the tool schema names the number. **A program that
ran without error is not visual evidence.** Open each render and name something concrete
on it before you change it.

**Reference — angles to look from, not a checklist and not a score.** What the code
measures is in §12; these are the readings nothing measures:

- a role at the wrong step of the scale — a caption as large as the body, a title a
  point above its subhead, a source line as large as what it credits;
- unreadable, distorted, blank or badly cropped figures;
- empty charts, weak labels, bars out of proportion to their values, misleading scales;
- icons too small to read, or crowding the text they belong to;
- a sparse or mechanically forced layout — compartments where an argument belongs;
- anything on §3's avoid list.

Two questions settle most of it: is the smallest type readable with the page shrunk to a
third -- roughly the back of a lecture theatre -- and, beside the page before it, is this a
different page or the same page with different words.

The reply names how many pages it did not show and how to ask for them: **a named subset
is not the deck** (§11).

**Name the pages you changed when a page is wrong.** `ppt_build(slides=[7])` rebuilds and
hands back page 7 alone, in seconds: edit that page's block, look at it, fix it, look
again. A sweep tells you twelve pages have something wrong; a short loop tells you what.
Run the full build when a batch is settled; that is what runs the gates and publishes.

**What has to clear is the refusals. Everything else you judge once.** A refused build is
not delivered, so §12's refusals get answered whichever way you read them. The warnings
are reports and the renders are the judge: read each one on the page it names, fix what
you agree with -- and what looks wrong to you whether or not anything measured it -- and
leave a finding you have looked at and disagree with alone. Judge it once: the same
warning on the next build is the same warning, and rebuilding a deck nothing refuses to
chase it changes nothing.

**Somebody else looks, and you do not have to ask: `ppt_review`.** A build with nothing
refusing it runs this by itself once five of its pages have not been read -- a draft
counts, so the first reading arrives while the deck is still being written -- and the list
comes back in that same reply under `first_reading`, with the pages it found the most on
rendered beside their entries. It reads the pages nobody has read yet, and a page you
rewrite after it was read is read again, so the build that delivers the deck reads
whatever is outstanding. Call it yourself for a second reading once you have answered the first, or
`ppt_review(pages=[7, 8])` to read back the pages you have just changed. It renders every
page and reads each one on an empty context -- one page, its planned claim, and the
requirements -- and hands back a problem list: where a region carries nothing, where copy crowds a rim, what does not line up,
which group of cards has no icon in it, a figure too small or squeezed or off-centre in
its own region, a column much wider than anything in it, type a step off its role, and
whether the page still carries the claim it was planned for. `pages=[7, 8]` reads just
those; omit it for the deck. It reports which of the two kinds a blank region is, so the
entry names the answer as well as the defect.

It refuses nothing, publishes nothing and edits no file. Every entry is yours to judge on
the same terms as a warning: look at the page it names, fix what you agree with, leave
what you have looked at and disagree with -- and say why.

**One page at a time, and a verdict on a kind is not a judgement.** The entries already
carry their kind; what makes the list worth having is that each one names a page. "Most of
these are whitespace on the table pages, and the cover art is the template's, so keeping
them" answers eighteen pages without opening one -- a live run wrote exactly that, made two
edits, and published. If you are going to leave an entry, open its page, say what you saw
there, and name the page when you say it. Dismissing a kind is how a list of eighteen
becomes a list of two without anything being read, and the ones a category sweeps away are
the ones the reviewer could see and you could not.

**Why a second reader rather than more looking.** You wrote these pages, and a region you
filled on purpose reads to you as a decision already made -- so a warning against it is
one you have already answered. The reviewer has none of that: it did not choose the
layout, and it is not told your program, only the plan. On a delivered deck that cleared
every gate with nothing blocking on any of its 18 pages, it came back with 45 entries,
among them a colour panel stretched to four times the height of the two lines in it and a
table whose right edge stopped at 56% of a band whose card row above it ran to 94%.

**When the pages read right, stop.** A build nothing refuses has delivered the deck, and
after `ppt_review` there is no further pass, no stage that rearranges the pages once the
gates clear. So a page you send on unlooked-at ships as it is, and a deck you have read
through -- and had read back to you -- and would not change is finished.

## 11. The order is enforced, not suggested

`ppt_build` refuses until `ppt_prepare` has read the task, and until `ppt_brief` holds the
three things the user decides. And it refuses a deck carrying a page whose code has never
been rendered back to you: the record is keyed on the block that drew each page, so a page
you looked at stays looked at and a page you then edited does not. A draft render counts --
it is a page put in front of you -- so a deck drafted through page by page has nothing left
for the publishing build to refuse. `slides=[1]` on an eighteen-page deck is not a way to
ship seventeen pages nobody saw.

That one cannot be answered by editing the deck. Run the build again, walk the rest of the
deck a batch at a time with `page_from`, and look.

One more thing the order decides: **`deck/ingest/materials.md` does not exist until
`ppt_ingest` has run.** Reaching for it first gets a `No such file or directory`. When it
is there, `ppt_ingest` returns `materials_index` — every part of it with the line it starts
at — and `read_file` takes `offset` and `limit`. Read the parts the deck stands on rather
than the file: whatever you read is re-sent on every request after it, so a single
whole-file read is the most expensive thing in the deck.

## 12. What the build refuses, and what it only reports

**Refused**, with the page named. Four land at the outline, before a line of the program
is written:

| Refused at the outline | The move |
| --- | --- |
| a **figure id the catalogue does not hold** | take the id from the catalogue |
| a **layout id the catalogue does not carry** -- `P1` to `P41` are page structures, `M1` to `M26` modifier layers, not zero-padded | open [deck/build/references/layouts.md](deck/build/references/layouts.md); an id not in it can only come from not having opened it |
| with a template bound, an outline whose **cover, index and closing** do not name the template's own pages | name them |
| when ingest extracted no figures at all, a **cited page nobody opened** | `web_fetch(extractMode="images")` each URL the materials cite, then `ppt_fetch` what you will use -- or the PDF behind an abstract, so `ppt_ingest` extracts its figures |

For each URL that holds nothing usable, or will not load, say so in `ppt_outline`'s
`swept`:
`[{"url": "...", "found": "text only, no figures"}]` -- kept with the deck, so a later
call does not ask again. Nothing is recorded until all four clear.

The rest land on the built deck:

- a page **citing one figure while showing another**;
- a **length the brief did not agree**, or **the wrong language**;
- a theme that is **not the bound template's**;
- a page the build **cannot map back to** a block of your code;
- a page you have **never been shown**;
- a page whose plan **promised a figure and that shows no picture** -- place it, or
  plan the page again without it;
- a page whose plan **planned a table and that shows none** -- draw it with
  `ppt_layout.table()`, or plan the page again without one;
- copy **printing an escape** (`48.3\nOVIS` -- pass a real newline);
- content three fifths **hidden behind an opaque shape** drawn after it;
- type a reader **cannot make out**: under 2:1 against the ground it landed on;
- words **colliding in the render**, which is not answerable by shrinking them;
- the template's own **placeholder text** still on a page you cloned, or that page
  cloned for its background with **new text boxes laid over** it.

**Reported**, with a page number, and every one of them yours to judge:

| Reported | What it means | The move |
| --- | --- | --- |
| `band` | a **filled colour bar** carrying nothing: a full-width band -- 85% of the page or more, under 1.3in tall -- with no copy on it, or a narrow accent strip -- 0.22in or less on its short side, six times that on its long -- wherever it sits and whatever sits on it | put the copy on the band, or take the strip out. Under 4.5pt it is a hairline and nothing here looks at it. Bars encoding values are left alone, but only where the row reads as data off its geometry alone: two or more slots, and lengths that differ |
| `type_floor` | **type under the floors** of §3 | a taller box, a wider column, less copy or a second page -- never a smaller size |
| `type_scale` | body copy over the 14pt floor but under `BODY_PT`, at a size **not a step of the ramp** | `size=BODY_PT`, `size=LABEL_PT`, or `the_largest_step_this_copy_takes(text, box, font=F)` |
| `type_drift` | **one slot the deck sets at several sizes** | give the boxes the height one size needs |
| `evidence` | a deck with **too few content pages showing anything** -- a figure, a table, a chart or a diagram, counted across the deck rather than page by page | `plane` is a filled shape like any other, so three cards come to three and the same three on a plane come to four |
| `wide_table` | a **table too wide to read** | fewer columns, or the page turned over to it |
| `native_table` | a table still **wearing Office's own look** | `ppt_layout.table()`, which takes the banding and gallery style off and keeps every row -- or draw the grid yourself, which never had them |
| `rule_strike` | a **rule struck** through a row | `rule` sits below the box it underlines; place it off the row |
| `card_overflow` | copy **escaping a card** | `fits(body, card_body_box(box, icon=, title=))` first, then the height it asks for |
| `crowded_panel` | a line **crowding its panel**'s bottom rim | the padding your other panels have, or one line less |
| `off_page` | a shape **over the page edge** | inside `safe_area_in` |
| `spilled_copy` | copy **painted off the page** by a `wrap=False` box | turn wrapping on and give the box a second line's height |
| `over_layout_art` | copy **on the layout's artwork** | inside `safe_area_in` |
| `flat_formula` | an **expression set as prose**, so its subscripts are flat and the box may break it inside a symbol | `formula()` (§9) |
| `listed_claims` | one box holding two or more parallel claims, so they **read as a list to be read out** | separate them so a reader can see where one ends; §6.5 has the calls that measure a region before it is drawn |
| `orphan_line` | a **label the render broke** onto a second line, as "为什么要统 / 一", in a text box or a table cell | a hair more width, or fewer characters |
| `wrapped_label` | a label wrapped **in a box too narrow for it** | the width `text_size` says the line takes |
| `overset_copy` | **copy that does not fit** the box it was put in | the taller box it names; `text_size`, `card_size` and `points_size` say the same number before the build does |
| `clipped_copy` | copy the file states that the render **clips instead of wrapping** | turn wrapping on |
| `excessive_whitespace` | a **large blank field** between body groups, below the content, or inside a panel | grow the load-bearing content, redistribute it, or shorten the panel |
| `unseparated_blocks` | two groups with **no more air between them** than inside them | a wider gap, a surface, or a hairline |
| `title_row` | titles that start at **different left edges**, or sit at a different anchor inside that row than the template's own | `title_row_as_code`, decided once in the shared setup (§8) |
| `layout_variety` | composed pages that **nearly all resolve to one page structure**, read off the shapes each page actually carries | a structure chosen for what each page argues (§3.5) |
| `page_mapping` | a build whose **pages cannot be told apart** in what the run recorded -- a separate reading from the refusal above, which is about the `# SLIDE` blocks in your file where this one is about what the run made of them. The two can arrive together | one block per page, each composing its own page |

| `template_adherence` | a deck **none of whose pages came from** the template's own structural pages | clone the cover, the contents list, the divider and the closing (§8) |
| `template_picture` | the template's **photographs still showing** | `pictures={n: ...}` a figure in, or `drop=[n]` |
| `prototype_kept` | a page built on a **prototype other than the one its outline named** | either page reads, so build it or rewrite that outline line |

At the plan, four more: a page planned with one line and nothing else, a deck spending a
quarter of itself on pages carrying no argument, material too thin for the pages agreed,
and a page drawn from scratch where the plan named no prototype -- that last one arrives as
a question, to answer with an example page or a reason in `needs`. At ingest, a source that
could not be read at all.

**Warnings are mostly not refusals for a reason.** Nearly every one of them can be
"fixed" by making the type smaller, and a gate that demanded they clear would get exactly
that. The two that do refuse -- words over words, and content behind a panel -- are the
two that shrinking makes worse.

**Three silences the build reports rather than hides.** With nothing ingested there is no
figure catalogue, so a page crediting Figure 4 while showing Figure 5 would not have been
caught. With no brief recorded, neither the deck's length nor its language has been
checked against anything agreed. With no renderer on the machine, nothing measured on the
rendered page ran at all. None of the three stops a deck, all three mean "not checked"
rather than "clean", and all three belong in what you tell the user.

**What nothing checks at all:** whether a number or a name on a page is one the sources
printed. There is no index of what the materials state and no gate that reads one, so
every figure on every page is yours to have read correctly and yours to be right about.
