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

This document owns the craft, and the tool schemas own their fields. What refuses a
deck and what only reports is written out once, in §12, because an author who has to
guess at it cannot plan around it — and a test holds that section against the gate
registry, kind by kind, so it cannot drift away from the code the way it did before.

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
implication or a constraint. Nothing counts the kinds and nothing counts the units --
how much copy a page ends up with is measured rather than allocated (§2). If the
evidence cannot support a complete argument, merge the page rather than enlarging
fragments.

The language, the audience and the length are the user's, and they are asked for
rather than inferred. `ppt_prepare` reads what the request already states and hands
back the rest as questions; put those to the user with `ask_user` and record the
answers with `ppt_brief`. Two of the three are then measured against the finished
file — the length against the page count, the language against what the pages actually
say — and both refuse the deck, so a brief nobody agreed to is worse than no brief at
all: it fails the deck against a decision the user never made. The audience is not
measured; it is the room you judge the deck for when you look at the renders (§10).

There is a fourth thing to record, and it is the one that gets dropped: whatever the
user ruled out. `ppt_brief(forbidden=["no icons", "never name a competitor"])` is
quoted back to you in so many words on **every** build, which is what keeps a
prohibition agreed at turn three alive at turn forty; the same thing filed under
`notes` is kept with the brief and never restated. Only what the user actually ruled
out belongs there — a preference you inferred, recorded as a rule, is a constraint
nobody agreed to.

## 2. Write the outline before you write the program

**Go looking for the deck's pictures first, and look widely.** Not for a page — there
are no pages yet — but for the pool the outline gets to choose from, and it is worth
more than the sweep of what the material happens to link. `web_search(kind="images")`
returns each candidate with its pixel size and the page it came from, and its titles are
descriptive where a sweep's are a bare position label. Search the things the material
*names* as well as the things it links — a paper, a benchmark, a product, a release —
and search for the ordinary furniture too, the logo and the product shot, which every
deck wants and no material bothers to link. Bring in more than one page will use:
`ppt_fetch` each, and `ppt_figure_inspect` them, because the pool is what you are
choosing from and a candidate you have not looked at is a candidate you cannot choose.
Which picture a page needs is decided below, against the plan; what you are doing first
is making sure there is something to decide between.

`ppt_outline` records it, and the build refuses until it exists. This is the stage that
decides how much is on a page: without one, what a page says gets decided while its
geometry is being typed, and the page comes out thin.

Per page: the **claim** as a statement (which is also its title), what **carries**
it, the **figures** it places by id, what it **says** in the deck's language, as
it will read on the page, and — where what carries it is a table — the **table_plan**:
the columns, the complete rows and one reading cue. That last field is the one nothing
on the page will remind you of. A page whose `carries` is table-led and whose plan says
only "pricing table" leaves the builder prose where it needs columns and cells, and it
comes back as a reading against the plan rather than as anything you can see.

`says` is where density is decided, and it is the field that gets under-written. A
content page carries 200 to 467 characters of copy and the **median is 250** — a
five-row comparison, two takeaways and a caveat is that page, and it is a good page.
Plan for the median rather than the top of the range: the top of it is where the type
reaches its floor and `overset_copy` starts reporting. Points that are each half a line
add up to a page that is a title and some labels, and the build cannot rescue it:
geometry can arrange copy, never supply it. So write each point as the sentence a reader
gets, evidence included, and if a page has only one short line in it either the evidence
for it belongs there too or the page belongs merged into its neighbour.

What is checked there rather than after the build — the difference between a cheap
edit and an expensive one — is the plan against everything already settled: the page
count against the brief, every figure id against the catalogue, and, with a template
bound, that the cover, the index and the closing name the template's own pages. Those
three refuse the outline and nothing is recorded until they clear. Beside them come
readings you judge: a page planned with one line and nothing else, a table-led page
with no `table_plan`, a deck spending a quarter of itself on pages that carry no
argument, and material too thin for the number of pages agreed.

Nothing checks a number on a page against the materials, here or later. No stage
builds an index of what the sources state, so a figure printed on a slide is yours to
have read and yours to be right about.

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

The reading it takes off that file is narrower than it looks. Three roles come out of
the template — the ground, the ink and its first accent — and `surface`, `accent_soft`,
`accent_ink`, `grid` and `muted` are mixed from those three rather than chosen by
anybody. What a file declares is also not what its pages paint: all twelve bundled
templates declare their second background as `#F0F0F0` and not one of them paints that
colour on any page it ships, which is why that slot is not read at all. So look at the
renders `ppt_template` returns and say what you see —
`ppt_template(project=..., palette={"accent": "#155FFD", "surface": "#DDE8FF"})` keeps
those for the deck's whole life and every page's `ppt_theme` carries them.

**State the roles you are sure of; a partial palette is the ordinary one, not a
degraded one.** What you leave out is re-derived from what you named rather than from
the file — `palette={"accent": "#7A1FA2"}` inside an amber template brings `surface`,
`accent_soft` and `accent_ink` back as tints of that purple. Reach for it when the deck
has a brand of its own, when a page needs an accent the template does not carry, or
when a series wants a colour the content decides: `chart_series` is the one role a
stated accent does *not* move, so a comparison drawn in your own two colours states
them. A later call merges over what the deck already said, so correcting one role keeps
the rest — state it before the first page either way.

What the build refuses is a deck not built inside the template's own copy — the theme
part, which you pass by opening `PPT_TEMPLATE` at all. `house_style` compares that
part's colour slots and a palette never touches them, so a deck whose every plane is
painted in a colour the template's scheme has never heard of passes it with nothing
said. It does not check what colour you filled a shape with, and never did.

**The role names are open; a role's meaning is not.** `foreground` is body type,
`muted` is secondary type and source lines, `accent` marks the focal point of a page,
`surface` and `accent_soft` are planes content sits on, `grid` is for hairlines. Those
eight are the ones the derivation knows how to finish, and you may add your own beside
them: a deck comparing two things wants two colours for the pair, and
`palette={"ours": "#0B3D91", "theirs": "#F2E8D5"}` puts both in every page's theme,
where `card(..., tint="ours")` reaches them by name. What may not move is the meaning
— if the accent marks "our result" on one page it cannot mark "prior work" on the next,
and a pair named on page 3 is the same pair on page 11.

**A helper that holds the theme takes a colour by name.** `tint=` on `plane`, `card`,
`heading`, `preset`, `chevron_row` and `timeline`, and `colour=`/`mark_colour=` on
`points`, `mark`, `rule` and `connect`, each take a role name, a key you added to the
palette or to your own copy of the theme dict, or a literal `#RRGGBB`. A name the theme
does not carry is refused and says so, so a misspelling is a message and not a colour.

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

So a page needing a colour the deck does not carry writes it; a colour the deck should
carry goes in the palette, where every page can then ask for it by name.

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

Which floor a box answers to is decided from the box, not from you. In the bottom 0.9in
of the page a box is furniture and answers to **8pt** — nobody reads a running credit
from a seat. Above that band, a box holding twenty characters or more is copy and gets
the 14pt floor, unless it opens with `来源`, `注：`, `图`, `表`, `source:`, `figure` or
`table`, which is a caption in either language and gets 10.8; anything shorter than
twenty characters is a label and gets 10.8 too. One thing is outside the measurement
altogether: a box of fewer than four characters, which is a page number or a chart tick
and is set small on purpose.

The size measured is the one the *render* came out at, not the one you declared, so a
box too small for its copy is caught where autofit shrank it. Every page that breaks a
floor comes back with the number and the box on it. If the copy will not fit above the
floor, the page has too much on it: merge the blocks that state one thing, split the
page, or move a section elsewhere. Shrinking the type is not the fix; it is how the
page stopped being readable.

**One filled bar is the deck's own**: the band the title row sits on. Two shapes come
back reported, and they are not symmetrical. A **full-width band** — 85% of the page or
more, under 1.3in tall — is allowed when copy sits on it and reported when nothing
does; where on the page it sits is not part of the test, so a tinted plane grouping a
row of formulas is as legitimate as a title row. A **narrow accent strip** — 0.22in or
less on its short side and six times that on its long one — is reported wherever it
sits and whatever sits on it, because a strip welded to a card edge is decoration
however much text it borders. A bar that is neither is not this gate's business.

A hairline is: under 4.5pt on its short side nothing here looks at it, so a rule in the
grey you already use for secondary text is the answer where a page genuinely wants a
divider.

Bars encoding values are read as data and left alone — a series, a stack, a track and
its value — but only when the row reads as data off its geometry alone: **two or more
slots, and lengths that differ.** That second clause is the one that surprises, and it
bites only where the bars are thin enough to be strips to begin with. A row of bars all
drawn to the same length encodes nothing the eye can compare, and no measurement of
rectangles tells it from a row of identical accent strips — so twelve equal bars packed
into a 2in band come back reported, one finding each, even when
`ppt_charts.horizontal_bar` drew them. Give the row real values, or give it the height
that puts each bar over 0.22in, and nothing is said.

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
three of them. This is the only part of a page that repeats on
purpose; everything else varies because the content varies (§3.5).

## 3.5 Structure follows the content

Decide what carries the point before placing anything.

Then compose around it. Give the load-bearing element the space its role deserves
and let the rest defer; a page where every region carries equal weight has argued
nothing. Vary composition because the content varies, not to fill a quota, and keep
a series of comparable cases on one shape so the reader can compare them. Give a
conclusion or decision its own baseline, weight or restrained accent instead of styling
every component identically.

Prefer one coherent composition over a dashboard of unrelated panels. Do not draw boxes
merely to avoid a prose page.

**Which shape the page takes is a choice out of a catalogue, not an invention.**
[deck/build/references/layouts.md](deck/build/references/layouts.md) is that catalogue:
a registry of page structures, each with a skeleton that runs, and of modifier layers
that stack on any of them — icons, tinted grounds, badges, rules, leader lines, marks,
one accented item. Open it while deciding, and say in the outline's `layout` which ids
the page uses. The failure it exists for is not overreach: it is a deck whose composed
pages all come out the same shape.

**Every band of the page is declared, and the hierarchy is legible.** A composed page
divides into regions before it divides into shapes: count them and name each one in the
`layout`, because the band nobody planned is the one that comes back as bare paragraphs.
Then rank the regions so the reader sees it before reading a word: which one is the
claim, which supports it, which is aside. The structure is the skeleton; the modifier
layers of [deck/build/references/layouts.md](deck/build/references/layouts.md) are what
carry the information.

Cards, planes and helper presets are optional primitives, not a required page grammar.
The model may write a typographic page, a dominant figure, a drawn table, a comparison matrix, a chart, a timeline,
a process, a comparison on a shared baseline, or a free composition when that better
matches the argument. Do not wrap every component in a filled rectangle just to make
its boundary visible; use hierarchy, alignment, whitespace and meaningful rules first.

**Fill the page.** A 13.3 x 7.5in canvas holds far more than a title and four
bullets, and content that stops two thirds of the way down is the most common
failure there is. Whitespace you chose is a margin around content that fills the
frame; whitespace left over reads as unfinished. When a page comes back with a large
accidental empty field, grow the load-bearing content, introduce a second visual
layer, or redistribute what is already there. Adding empty decoration, repeating copy
in another form, or shrinking the type to spread it out disguises the problem rather
than fixing it, and each of those makes the page worse than the gap did.

When the source already carries a real table, comparable rows or a set of parallel items
with several attributes, that information shape is part of the evidence: do not flatten
it because the text happens to fit.

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

**Write it in pieces, not in one call.** `write_file` for the setup and the first
three or four pages, then `mode="append"` for two or three pages at a time, and
`ppt_build(draft=true)` in between: a draft builds what exists, measures it and hands
back the renders, without holding a part-written deck to the length that was agreed
and without publishing. Drop `draft` when the deck is whole and you want the gates.

Two reasons, and the second outlives the first. The gateway in front of this model
closes a connection that streams one very long reply, so a twenty-page program written
in one call gets cut before its tool call arrives -- and sending the same thing again
gets cut in the same place. What stays is that a draft shows you a page while there are
three of them rather than twenty: every fix is one page wide, and the pages after it are
written already knowing what the last one looked like.

It runs in the build directory with python-pptx and Pillow, and reads its paths from
the environment: `PPT_OUTPUT` (save there and nowhere else), `PPT_FIGURES_DIR`
(the figures ingest extracted, and the only way to one — the program's own directory
is `deck/build` and there is no `figures` under it, so take
`FIGURES = os.environ["PPT_FIGURES_DIR"]` in the setup and write
`f"{FIGURES}/fig2.png"` wherever a page places a figure), and — when a template is
bound — two paths that are not interchangeable: `PPT_TEMPLATE` is the template with
its example pages **removed**, which is the deck you build into (`prs = Presentation(os.environ['PPT_TEMPLATE'])`),
and `PPT_TEMPLATE_SOURCE` is the user's original file with those pages still in it,
which is the only thing `prototype` can read a page out of
(`tpl = Presentation(os.environ['PPT_TEMPLATE_SOURCE'])`). Handing `prototype` the one
you build into raises `this template ships 0 pages, so there is no page 4` — the pages
it wants to clone are exactly the ones that file had taken out.

**One block per page, opened with a `# SLIDE <n>` banner.** Shared helpers above the
blocks are what you want; the page's own composition belongs in the page's own
block. Two shapes are refused: a `build.py` that runs another file, and a loop that
draws every page from one call site. Both leave no code belonging to one page, and
every per-page thing the build does — matching a render to the code that drew it,
reporting a defect against the block that caused it, improving one page without
touching another — has nothing to hold on to.

**The whole program, ending included.** Every other example on this page and in the
references is a page fragment. This is the file they go inside, and its last line is the
one that gets left out: a program that draws twenty pages and never saves them exits
cleanly, reports nothing, and leaves no deck.

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

Then write the helpers *your* pages need. The header is meant to repeat (§3); what
must not repeat is everything under it. A deck where every page is the header, one
bullet row and the same three-card strip is a template — you just wrote it yourself
instead of loading one. Beyond the header, the source line and the page number, page
types have no reason to share a skeleton. A cover, an
agenda, a two-way comparison, a table page, a full-bleed figure and a closing page
should each be recognisably a different kind of page.

**Default — a `*` after the geometry in every helper you write.**
`def card(slide, x, y, w, h, *, fill=SOFT, bold=False, align="left")` — the boxes stay
positional because they are always the same four numbers in the same order, and
everything after them is named at the call site. Seven or eight positional parameters is
the largest single cause of a build that will not run at all: an empty string arriving
where a bold flag goes, an argument short, a keyword the function never had. It also
costs pages later — reviewing a deck means rebuilding one block at a time (§10), and a
helper you can miscount is a page whose fix gets dropped for a `TypeError`. The helpers
in `ppt_layout` are shaped this way, which is why a call to them cannot fail this way.

**The agenda is the page that goes wrong most often.** It is a map of the argument:
the movements the talk makes, each named with one line saying what it settles.
*Default — five to seven of them*, and nothing counts them; the outline's own sections
usually run to eight to twelve, so a deck may well want more. What is not a default:
it is **not an index of slide numbers**. "3 Introduction, 4 Limitations, 5 Overview…"
tells the audience nothing the page numbers do not, and it starts at 3 because it is
enumerating the file rather than the talk.

**Ask how wide the text is instead of guessing at it.** This is the single commonest
geometry bug in a generated deck: a box sized to what the string looks like wraps it
onto a second line, the second line pushes into whatever sits below, and the page
reads as broken rather than as tight. `text_size` is the width and height the copy
really takes and `fits` is the yes-or-no, both answered before anything is placed, so
this costs a call rather than a render. Where a line must not break whatever happens,
`word_wrap = False` says so: a number, a label, a kicker, a card heading. `write` sets
it on every box it makes, so the way to turn it off is on the frame it hands back —
`write(...).shape.word_wrap = False`, where `.shape` is the text frame itself. Extra width
itself costs nothing — an unfilled text box is invisible, it has no fill and no
outline, and a left-aligned line starts in the same place whether its box ends at 4in
or 7in.

**`print()` is how those answers reach you.** Whatever the program writes to stdout
comes back in the build reply, on a build that failed as well as one that worked — so
`text_size`, `fits`, `formula_type_size`, `len(units(slide))` and the theme your script
actually resolved are one `print` from being visible, instead of being inferred from
the shape they drew. Print the measurement, not the whole theme: the last 20,000
characters are what comes back, and a loop printing per shape pushes out the line you
wanted. This is also the fastest way to find a page that runs and draws the wrong
thing, which no render can explain and no traceback mentions.

The agenda's geometry breaks the same three ways every time, so compute it:

- **A two-digit number needs a box that fits two digits.** `01` and `10` fold into
  two stacked characters in a box sized for `1` — the same failure as above, in its
  most common disguise. Set `write(...).shape.word_wrap = False`, or give the box the width the widest
  label needs rather than the width the first one happens to need.
- **Lay repeated rows on a pitch.** With `n` rows in height `H` the pitch is `H / n`,
  the label sits at `y = top + i * pitch`, its description at a fixed offset below.
  Guessed offsets are what puts a title on top of the line under it.
- **A label and its description are one block.** If the description can run to two
  lines, the pitch has to allow for two, or row `i + 1` lands on top of it.

The same three apply to any page built from repeated rows or cards.

### The helpers, by signature

Everything below is already beside your script: what each helper takes, what it hands
back, and what it can be asked before it draws anything. Read this rather than the
modules -- their source runs to 51k tokens and stays in context for the rest of the
run -- and a helper that is neither here nor in one of the reference documents this
page links is one you should not be calling. The drawing base `ppt_charts` is built on
is the case that matters: its two dozen primitives are public, they are what a form
none of the twenty-three can carry is written out of, and their signatures are in
[deck/build/references/charts.md](deck/build/references/charts.md) rather than in the
table below (§6).

Every `deck/build/references/...` link on this page is a file in the build directory,
beside the modules it documents: `deck/build/references/tables.md`,
`deck/build/references/charts.md`, `deck/build/references/icons.md`,
`deck/build/references/shapes.md`, `deck/build/references/formulas.md`,
`deck/build/references/layouts.md`. They are
written there on every build, and that whole path is what `read_file` takes, because
the file tools resolve against the workspace and not against the directory your
program runs in. This page carries the vocabulary and they carry the detail it stands
for: a table drawn without reading `deck/build/references/tables.md` gets the bare
default, and the icon names worth knowing by heart are only in
`deck/build/references/icons.md`.

`ppt_layout`, always present:

| | |
| --- | --- |
| `page(kicker=True, footer=False)` | → `Frame(kicker, title, body, footer)`, four boxes inside the safe area; ask for the footer on a page that cites and the body gives up the strip for it |
| `Frame(kicker, title, body, footer)` | the same four boxes as a value you can build. `page()` is the ordinary page and not the only one: a frame you make yourself is what a left rail, a full-bleed opener or a title over two thirds of the canvas is made of, and every helper that takes a frame takes yours without knowing the difference |
| `Box.corners(x0, y0, x1, y1)` | a box from its **two corners** |
| `Box.at(x, y, w=, h=)` | a box from a corner and a **size**; the size is keyword-only |
| `box.rows(n, gutter=GUTTER, weights=None)`, `box.columns(...)` | n boxes filling this one |
| `box.grid(cols, rows, gutter=GUTTER)` | row-major cells |
| `box.split_left(fraction, gutter=GUTTER)`, `box.split_top(...)` | two boxes, the first taking `fraction` |
| `box.inset(dx=PAD, dy=None)` | a smaller box inside this one |
| `stack(box, gutter=0)` | a cursor down a region: `.take(height)`, `.rest()`, `.skip(height)`, `.left`, `.short_by(*heights)`; bands are adjacent, `skip` is the gap |
| `picture_fit(slide, image, box, theme, *, caption=None, size=LABEL_PT, align="center", font=None, cjk_font=None)` | a picture scaled to fit the box whole, centred, caption under it |
| `heading(slide, frame, theme, title, kicker=None, *, tint="surface", bleed=True, size=TITLE_PT, anchor="middle", font=None, cjk_font=None)` | §6.5 — `anchor` is the template's, not this default; §8 |
| `write(slide, box, text, *, size=BODY_PT, colour="#000000", font=None, cjk_font=None, bold=False, align="left", anchor="top", spacing=1.15)` | `text` may be a list of paragraphs |
| `points(slide, box, theme, items, *, size=BODY_PT, numbered=False, mark="•", colour=None, font=None, cjk_font=None, mark_colour=None, spacing=1.25)` | §6.5 — it takes the theme, so the faces are optional here |
| `card(slide, box, theme, *, icon=None, title="", body=(), tint="surface", size=BODY_PT, title_size=LEAD_PT, font=None, cjk_font=None)` | §7 |
| `formula(slide, box, text, theme, *, size=BODY_PT, align="left", anchor="top", font=None, cjk_font=None)` | §9 |
| `plane(slide, box, theme, tint="surface", radius=False)` | a painted region |
| `rule(slide, box, theme, thickness=0.03, colour=None)` | a hairline **0.06in below** the box and **at most 1.05in long** — a short mark under a heading or beside a number, not a divider across a region. For a full-width line draw a thin `plane`, or take the box a chart hands back |
| `table(slide, box, rows, theme, *, weights=None, size=LABEL_PT, numeric_from=None, style="minimal", emphasize_rows=(), emphasize_columns=(), group_rows=None, indent_rows=(), total_rows=(), marks=None, header_size=None, align=None, rule_pt=None, grid_pt=None, row_height=None, header_height=None, padding=None, fill=True, column_rules=False, banding=False, fills=None)` | §6 |
| `mark(slide, box, theme, kind, value=None, *, colour=None)` | [deck/build/references/tables.md](deck/build/references/tables.md) |
| `overlaps(boxes, tolerance=0.01)` | → the `(i, j)` pairs that overlap |

> **The page's shape is yours to choose; only the safe area is not.** `page()` returns
> one skeleton and it is the right one for an ordinary page -- and a deck that reaches
> for nothing else puts every title in the same band at the same height. A deck whose
> pages all open identically is reading itself out. When a page argues something a rail
> or a full-bleed opener says better, build the `Frame` for it: `heading` will paint the
> band under whatever title box you hand it, and `overlaps` still answers whether the
> result collides. What you may not do is give each page a different header -- the
> header is what every page shares, so whatever shape it takes, it takes throughout.

> **Say which rectangle you mean: `Box.corners(...)` or `Box.at(...)`.** `add_textbox`,
> `add_shape`, `add_picture` and any helper you write yourself all take
> `left, top, width, height`; a box takes two corners. Four numbers cannot say which, so
> the two calls are named and the bare `Box(a, b, c, d)` is not yours to write.
> `Box.corners(0.82, 1.5, 4.25, 2.6)` is a box ending at x=4.25; a 4.25×2.6 box is
> `Box.at(0.82, 1.5, w=4.25, h=2.6)`. And the mistake compounds: having seen it in the
> render, the repair to reach for is the other named call — not "corrections" to your
> own helpers, which take a size and were right all along.

Constants: `CANVAS_W`, `CANVAS_H`, `MARGIN`, `GUTTER`, `PAD`; the ramp `TITLE_PT`,
`LEAD_PT`, `BODY_PT`, `LABEL_PT`, `KICKER_PT`, `NUMBER_PT`, `BODY_FLOOR_PT`.

### What comes back, and how to ask before you draw

Every helper that *draws* hands back a `Drawn` — the five that compute instead hand
back what they computed: `page()` a `Frame`, `stack()` a `Stack`, `overlaps()` a list
of `(i, j)` pairs, a `Box` divider a `Box` or a list of them, and `mark()` a `Marks`,
which is a list of the shapes it made carrying `.box` for the ink they actually cover
— the one measurement that says whether a marked column is wide enough to read.

For the drawing ones: `.shape` is the python-pptx object it made
and `.box` is **what it actually covered**, which is not the box you passed in: a rule
sits below the box it underlines, a picture keeps its own aspect inside the region it
was given, and copy that did not fit comes back taller than the box it was handed
rather than as a claim that it fit. An attribute the tuple does not carry is looked
for on the shape and then on the box, so `write(...).paragraphs`,
`table(...).columns` and `heading(...).x1` all read straight through. Place the next
thing on the page off the last thing's `.box`, never off the number you chose for it.

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
| `card_size(width, *, icon=None, title="", body=(), size=BODY_PT, title_size=LEAD_PT, font=None)` | how tall a card has to be for what goes in it — `max(card_size(w, **c).h for c in cards)` levels a row without padding it out to the page |
| `stack(box).room` | what is still unspoken for, as a box, **without taking it** — `fits(picture_size(fig, down.room), down.room)` is the whole question |
| `stack(box).short_by(*heights)` | how many inches the whole plan runs over, or 0.0 — measure every band, ask this, **then** draw. `rest()` spends the region it answers with; `room` is the same box and does not |

**A short label in a big box: ask, do not name a step.** Every call above except the
last measures downwards — whether the size you already chose will do. What that leaves
out is the label its box is far too big for: naming the smallest step of the ramp puts a
chevron label at 14pt in a shape 1.25in tall. Two or three characters in a box over an
inch tall want `the_largest_step_this_copy_takes(label, one.box, font=F)`, which answers
30pt for that chevron row. Everything playing one role on the page shares one size, so
ask for each and take the `min` — the longest label is what the row can afford. It
answers a step of the ramp and never a size between two.

**Ask `lines_needed` before you fix a band's height.** `write` turns autofit off on
purpose, so copy that runs long does not shrink -- it runs out of its box and over
whatever is under it, and the render is the first place that shows.
`lines_needed(title, frame.title.w, size=TITLE_PT)` coming back 2 is the row below
moving down or the box getting wider, decided before anything is drawn.

`ppt_charts` answers the same kind of question about a chart, and it answers it by
running the chart against a slide that draws nothing -- so what comes back is the
chart's own arithmetic and not a second copy of it free to disagree:

| | |
| --- | --- |
| `the_smallest_box_a_chart_needs(chart, theme, *data, **knobs)` | the smallest box this chart takes **this** data in, as a `Box` at the origin |
| `whether_a_chart_fits(chart, box, theme, *data, **knobs)` | whether it would take the box you have at all |
| `what_a_chart_will_do(chart, box, theme, *data, **knobs)` | the `Drawn` a real draw would return, with nothing written |

A chart's `Drawn` is a different animal from a helper's: it **is** a `Box` — the plot
itself, what is left once the category labels, the axis readings and the key have
taken theirs — and it carries `where` (the chart's own scale, so `where(value)` is
the inch a reading lands on and a rule at 80% goes where the chart put 80%),
`names_not_written` and `readings_not_written` (what it dropped for want of room),
`marks_not_to_scale` (bubbles drawn at the floor instead of to area) and `type_pt`
(the step of the ramp its labels landed on — a reading, never a setting; there is
still no way to hand a chart a size). `nothing_was_dropped` is those three in one
boolean.

A chart that will not take its box raises `TooSmall`, carrying `short`: the deficit in
inches as `(across, down)`. **It raises before it draws anything** — a refusal leaves
zero shapes on the slide — so a `try/except` around one has no wreckage to clear, and
`whether_a_chart_fits` is that `try/except` already written.

`ppt_charts`, always present — twenty-three charts drawn as shapes. Each takes the slide, a
`Box` out of the grid, the theme, and its data; each fills the box, and there is no
size, face or colour to pass: type comes off the ramp above and steps down when a
label does not fit, colour comes off the theme. The twenty-three signatures and the data
shape each one reads are in
[deck/build/references/charts.md](deck/build/references/charts.md) — open it when a page carries numbers.

`ppt_shapes`, always present — the 109 Office preset geometries a business page can
use, by their DrawingML names, and the two layouts built on them. Signatures in
[deck/build/references/shapes.md](deck/build/references/shapes.md), with §7.5 — open it when a page carries
a sequence, a branch or a route.

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

**With a template bound, the title row is the template's.** `house_style` measures
where its own pages put one and `title_row` reports a page that puts its title
somewhere else, so use the box it names — `title_row_box_in`, at the size it names.
Decide it **once, in the shared setup**, and have every content page call that one
thing. A page block that computes its own title coordinates is how a deck ends up
with three title rows, which is the failure this is about — not which construction
you picked.

Five of those have a second question a signature cannot answer, because a signature
says what to pass and none of these is about what to pass:

- `replace_text(shape, "新文字")` writes one shape. `replace_text(slide, "旧文字", "新文字")`
  finds whatever on the page holds that string and writes it — the form to reach for,
  because finding the shape is the tedious half.
- `replace_picture(shape, image, fit)` — `"contain"` shrinks the frame to the picture's
  own proportions; `"cover"` crops the picture to fill the frame as it stands. Either
  way it refuses a landscape figure in a portrait frame rather than squashing it.
- `units(container)` returns runs of repeating sibling groups — a card row, an agenda list.
- `arrangement(run)` returns `("row"|"column"|"grid"|"irregular", rows, cols)`. Nothing
  **moves** the survivors for you, and a run need follow no grid at all: closing the hole
  four units leave on a 2x4 grid is a design decision, and `place` is how you make it.
  Deleting the spares is not that decision and is not yours to do — `fill` and
  `adapt(items=...)` already did it.
- `fill(run, items)` is that deletion, per run: `fill(units(slide)[1], [["03", "本文", "小标题"]])`
  writes the items into a run's units and removes the ones left over, group and all. Reach
  for it when `adapt(items=...)` was not enough, which is the two-run page below.

**Re-flow off the geometry the template already fixed: `boxes(run)` is what you decide
from.** It hands back each unit's `(left, top, width, height)` in inches, in page
order -- the pitch and the size the template drew -- and that tuple is exactly what
`place` takes. `arrangement` says whether the run is a row, a column or a grid; computing a
position without `boxes` is guessing at coordinates the page already holds.

`ppt_theme` gives `THEMES` and `rgb`. `ppt_icons` gives `add_icon(slide, name, left,
top, size, colour, width_pt=1.75)`, `find_icons(term)` and `ICON_NAMES`.

**`adapt(items=...)` in detail.** One entry per repeating unit, and the units left over
are deleted. Each entry is a list positional over that unit's text shapes, or a dict
keyed by the text a shape holds now. In a list: `None` keeps the template's own words, a
string replaces them, and `""` empties the shape — **except over a number, where `""`
means "this unit's number" and the slot is renumbered for its position**, in the
template's own padding. That is what an agenda wants: six sections in a page that ships
eight numbered slots come out numbered 01 to 06.

**Count the unit's text shapes and give one value each.** A list shorter than the unit
leaves the remainder exactly as the template wrote it — which is what you want over a
number and not what you want over example copy. A real agenda unit here holds three text
shapes, `[number, heading, small-heading]`; `items=[["01", "第一部分"], ...]` fills two of
them and ships `单击添加小标题` three times, once per surviving slot. `["01", "第一部分", ""]`
is the same call with the third shape emptied. Pass more values than the unit holds and it
raises, naming each shape it found — which is the cheapest way to learn the count if the
decompiled page did not already tell you.

**`items` fills one run, and a page can have two.** It fills the longest —
`max(units(slide), key=len)` — and every other run on the page is a run of shapes it was
not told about, so their words get emptied like any other unnamed text. One template's
four-card page is not one run of four, it is two runs of two (cards 01–02 and 03–04,
grouped in pairs). `adapt(..., items=[a, b])` there ships two filled cards beside two
blank ones. `fill(run, items)` is the second half:

```python
slide = adapt(prs, prototype(tpl, 4), title="四项发现",
              items=[["01", "本文", "第一项"],          # the longest run
                     ["02", "本文", "第二项"]])
fill(units(slide)[1], [["03", "本文", "第三项"],       # every other run, by hand
                       ["04", "本文", "第四项"]])
```

`units(slide)` after `adapt` returns tells you how many runs there are — check it
whenever the render shows more repeated cards than you passed items for. Two things
change once `adapt` has run: those units hold no text any more, so **address them
positionally and not by a dict keyed on the template's words** — every key such a dict
could match now holds `''`, so it raises `KeyError` listing empty strings — and `""` no
longer restates a number, because the number it would have restated was emptied a moment
ago. Write `"03"` yourself.

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

**A picture set flush against a panel fights the panel's outline.** Inset it —
`box.inset(...)` gives the picture the padding the panel's own copy has — so the edge
of the image is not doing the work of the panel's border.

Never distort aspect ratio, crop away interpretive labels, include a neighbouring
caption by accident, duplicate a printed caption, or leave transparency composited
onto black. If evidence is unreadable at the size it has: crop to the panel you
cite, give it more of the page, or rebuild it from the exact values.

Fetched pictures are first-class, not a fallback. Run two discovery paths beside each
other. For every URL the source report cited, `web_fetch(extractMode="images")` lists
the page's own figures, screenshots and preview image; at the same time,
`web_search(kind="images")` searches for anything those citations do not hold.
`ppt_fetch` brings a selected candidate into the deck's sources and reads it, and the
URL travels with it so a page can credit where it came from. Pass the caption or alt
text the listing gave you as `ppt_fetch(caption=...)` and the source's own words
travel with it too, into the same catalogue field a paper's figure fills; leave it
out and that figure reaches the deck with nothing but its pixels to say what it is.
Copy the page's words, never your reading of the picture. Reach for these paths
whenever a page names something that has a face — a product, a company, a standard,
a chart somebody else published — rather than only when the materials left a hole.
The results carry pixel dimensions, so a mark that would land soft on the page can be
rejected before it is placed. Generate with `ppt_generate_image` only after both paths
find no suitable existing visual; a generated image illustrates a concept and never
replaces evidence. Ask for it at the shape of the region it will sit in —
`aspect_ratio` takes `16:9`, `4:3`, `3:2`, `1:1`, `3:4` or `9:16` and defaults to
`16:9`, so a full-height rail or a portrait strip gets a landscape image to crop or
letterbox unless you say otherwise. Fetched *numbers* are another matter: those
are a source the user did not choose, so anything the deck states as fact still comes from the materials.

A cover does not need a figure. Reaching for one is a habit: the cover's job is the
title, who wrote it and where, and a paper's Figure 1 pressed into its corner is
smaller than the page it will get later.

## 6. Charts and tables, drawn

There is no chart library here and matplotlib is not a dependency of this route.
Charts are shapes — rectangles, hairlines, marks and labels — which is what keeps
them editable and on the deck's palette.

**`ppt_charts` draws twenty-three of them for you**, and the mapping from a value to a
length is the half you must not write by hand: an axis worked out per page is how a
bar ends up out of proportion to the number over it. Import what the page needs:

```python
from ppt_charts import column, horizontal_bar, waterfall
column(slide, frame.body, T, [("East", 185), ("South", 142)], accent="East", unit="M")
```

The signatures are in §4. The forms below name the primitive that draws them; where
a row names no primitive, none exists and the row says what to draw instead.

### Which form, and what to draw when none of them can be

[deck/build/references/charts.md](deck/build/references/charts.md) carries the rest of it: the twenty-three
signatures, a row per form saying what it *encodes* (reference, not a constraint), and
what to substitute where the shape wanted cannot be reached from rectangles and
straight lines at all — a pie, a gauge, a radar, a sankey, an area chart. Open it
before choosing a form, and again when the form you wanted is one of those five.

**The twenty-three are shortcuts, not a ceiling.** The base they are built on is public
on `ppt_charts` too — the ink (`rect`, `disc`, `ring`, `hline`, `vline`, `poly`,
`write_label`), the scale (`span`, `snap`, `linear`), the palette discipline
(`series_paints`, `stack_paints`, `shades`, `emphasis`, `ink_on`, `contrast`) and the
label fitting (`type_face`, `text_width`, `pick_size`, `line_height`). Where none of the
forms is the shape of what the page argues, write the form: that is a design decision,
not a rule broken. What does not change is the rest of this section — a length starts at
zero, the label sits on the mark, one thing is accented, and the colour comes out of
`ppt_theme` through the calls above rather than out of a string you typed. A chart you
write that raises `TooSmall` and returns a `Drawn` also works with `whether_a_chart_fits`,
`what_a_chart_will_do` and `the_smallest_box_a_chart_needs`.
[deck/build/references/charts.md](deck/build/references/charts.md) has the signatures, a
table of what the twenty-three cannot say, and a worked example.

### The rules a drawn chart obeys

- **Length is the value.** Every bar, segment and track is exactly proportional, and a
  length axis starts at zero. Nothing measures this: no check compares a bar to the
  number over it, so the only thing standing between a deck and a misdrawn axis is you
  looking at the render (§10). That is the reason to let `ppt_charts` map the values —
  it is the one half of a chart no one downstream will catch you getting wrong.
- **Default — label the mark**, and leave the legend to the plot where no label can
  reach its own. Name the units, and put the axis maximum where the reader can see it.
- **Default — one thing is accented.** The bar, segment or point carrying the claim
  takes the accent and the rest take `muted` or `grid`; how many colours a chart ends
  up with follows what it encodes, not a quota, and nothing counts them. `SERIES` in
  order is for a genuine multi-series plot — its later colours are quiet on a light
  ground, so give them the supporting series and the accent to the one that matters.
- **Baselines and axes are hairlines.** The charts draw their own; `rule` is the short
mark you add beside one, and it caps at 1.05in, so it is not the way to underline a
region. No vertical gridlines, no
  frame around a bar chart, no ground behind a plot unless it separates layers.
- **Never invent a value.** No placeholder, no number reconstructed off a plot you
  cannot read, no interpolated point. Where the series has a hole, the page says so.

For the compact bars that sit beside a table or under a claim, a row of rectangles
beats any chart object: you control the length, the colour of the one bar that
matters, and where its value sits — `horizontal_bar` in a `body.rows(3)[2]` is that
page. Do not reach for `add_chart`: it arrives with Office's own six colours and its
own gridlines, which is as recognisable a tell as any stock template.

**Never draw a table with a bare `add_table`**, which arrives with Office's own look — a
white hairline around every cell, banding on, and a header style that fights whatever
palette the deck is in. A screenshot of a source table is no better: it arrives in
another typeface and cannot be reweighted around the page's conclusion.

Everything above that ban is yours. **`ppt_layout.table()` is the shortcut** for the
ordinary comparison table — the Office look taken off, plus the part that is
arithmetic rather than design: **it sizes each column from what that column holds**,
measures each row from the lines its cells really wrap onto, and spreads the rows into
the box you gave it. Its look is a set of defaults and every one of them is a keyword
away — the two line weights, the header's own type size, the row heights and padding,
per-column alignment, vertical rules, a cell or column filled in a colour you name,
banding. **Or draw the table yourself** out of `Box`, `text_size`, `write` and
`plane`, which is the right call whenever the page wants a grid `table()` does not
draw: merged cells, a header spanning three columns, an icon inside a cell, a
sparkline down one. Both paths, the dials and why each default is the default, a full
worked hand-drawn table, and the seven `mark` kinds are in
[deck/build/references/tables.md](deck/build/references/tables.md). Open it when a page carries a table.

Compose freely around one: a two-product comparison may be two parallel regions with
the same row labels rather than one table, and a final conclusion may sit on its own
baseline below.

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
ground = stack(frame.body).take(tall + 2 * PAD)
plane(slide, ground, T, tint="surface")
for box, (icon, head, body) in zip(ground.inset(PAD).columns(3), said):
    card(slide, box, T, tint="background", icon=icon, title=head, body=body,
         font=FACE, cjk_font=HAN)
```

**`heading()` is one ready-made top edge** — a quiet ground behind the title row with
the kicker and the title set on it, stopping short of the body, no rule under the
title, and `bleed=False` to keep the ground inside the safe area rather than running it
the full width of the canvas. It sets the title in `page()`'s own box rather than the
template's, so reach for it where you are composing a page yourself (§8) and where that
box agrees with the `title_row_box_in` the template was measured for (§4).

**The header is one compact group.** A section tag, title and one explanatory line
sit close enough to scan as a single unit; the larger vertical break belongs between
that unit and the body. Do not put a decorative rule in the middle and leave the
subtitle stranded beneath it, and tighten these internal gaps when the actual title
uses fewer lines than the template's example did. Where it is defined is §4.

**Two claims in one frame read as one paragraph that happens to have a line break in
it**, and putting a mark in front of each does not change that reading: four claims in a
6.5in column drawn as four dots with running text after each, no card, no ground, five
inches of the column empty under them, reads as that same page. **A mark is not a
structure.** A single paragraph of explanation is a paragraph and nothing here is about
one; what the build reports, as `listed_claims`, is a box carrying two or more parallel
claims (§12).

**What a card holds, and what it costs, before it is drawn.** `card` paints a rounded
plane in the tint you name and sets the icon's square, the title bold beside it and the
copy under it, all inside `PAD`, and it owns that geometry (§7).
`card_size(width, icon=, title=, body=, font=FACE).h` is how tall it has to be for what
goes in it, answered before anything is drawn; `max(...)` across a set is the height
that levels a row without padding each card out to its cell; and
`fits(body, card_body_box(box, icon=..., title=...))` says whether the copy went in,
without spending a render on the question. `card_body_box` is the box the copy really
gets inside a height already chosen, so that question lands on the card rather than on
the cell. Both answers come off one estimate, which scales a Latin run by the face it is
told about and by the widest face it knows when it is told none -- so where the unnamed
question refuses the levelled height, the row above takes what that answer asks for
instead.

**Dividing a region, and what the `evidence` count sees.** `columns`, `rows` and
`box.grid(cols, rows)` cut a box into cells, row-major; a `stack` given
`gutter=(box.h - n * tall) / (n - 1)` spends the region's slack as the air between its
bands instead of leaving it in a heap at the bottom.
`plane(slide, box, T, tint="surface")` paints a region, and it is a filled shape like
any other: `evidence` counts a page as showing something at four of them, so three cards
come to three and the same three on a `plane` come to four.

`points(slide, box, theme, items, numbered=False)` writes a marked list, a hanging mark
per item and `numbered=True` for an ordered one; `points_size` measures one before it is
drawn, which `text_size` cannot, knowing nothing of the mark or the paragraph spacing.

**Never guess a y coordinate.** `stack(box)` is a cursor down a region: `take(h)` hands
back the next band and moves on, `rest()` hands back everything still unspoken for, and
`skip(GUTTER)` puts the deck's one gutter between them. Bands are adjacent otherwise, so
heights from `table_size` and `the_smallest_box_a_chart_needs` add up to what the region
has and the page can be budgeted before it is drawn. Guessing one instead is a
coordinate tried, rendered, tried again — builds spent on a number the cursor already
knows. A coordinate you were *given* is different and you should paste it:
`house_style`'s `body_area_as_code` (§8) is measured off the template, not guessed.

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
band = down.rest()
plane(slide, band, T, tint="accent_soft", radius=True)
write(slide, band.inset(PAD + 0.10), answer, size=LEAD_PT, bold=True, colour=INK,
      font=FACE, cjk_font=HAN, anchor="middle")
```

**Cut the region to what goes in it, in both directions.** `table_size(rows, T)` with no
box answers both, and the lane and the band above are cut to its two fields: `.w` is the
width those columns want, `.h` what the rows come to at it -- the band taking the larger
of that and what `picture_size` says the figure needs in the room the answer's own line
leaves. `card_size` answers a height alone, for a width you hand it; `text_size` answers
both for copy, capped at the width asked about, so at a width the line cannot exhaust its
`.w` is what that line sets. A band or a lane chosen by eye instead is what sits in it
printing past its own edge, which the file measures as fitting and only the render shows.

`picture_fit` is the other half of that: it scales the image to fit the box whole
without cropping, centres it, and sets the caption under it. Giving `add_picture` one
dimension and letting it scale the other only works if you know which dimension runs
out first, which depends on the image.

**Cut the band to the figure, not the figure to the band.** A figure keeps its own
aspect, so one dimension runs out first and `picture_fit` centres what is left -- on a
band cut by eye that is a strip of white over the figure and another under it.
`picture_size(fig, region, caption=cap)` reads the image's own pixels and answers what
it and its caption really need; hand that `.h` to `stack.take` and the centring has
nothing left to centre in.

## 7. Icons, on the cards and on the blocks beside them

```python
from ppt_layout import card
from ppt_icons import add_icon, find_icons, ICON_NAMES   # 1304 Tabler Outline names

card(slide, box, T, icon="target", title="语义查询是必要的",
     body="去掉 Qsem 改用线性分类头：YouTube-VIS 44.7 对 46.3")
add_icon(slide, "clock", Inches(0.7), Inches(2.1), Inches(0.42), ACCENT)
```

**`card(icon=)` takes one, and so does a block drawn without a card.** Not every block
needs one and nothing counts them. Pick the icon for what the block argues, not for a
noun in its title.

`card()` draws the surface, the icon, the title beside it and the copy under it, and
it owns that geometry — the icon's square, the gap after it, the title's line, the
padding inside the box you give it.

**`card` hands back the box you gave it, so ask `card_body_box` what is left inside
it.** The icon's square and the title's line come out of the card before the copy does,
and whether the copy cleared them is otherwise a question only the render answers:
`fits(body, card_body_box(box, icon=name, title=head), size=BODY_PT)` asks it first. An
icon takes that line whether or not a title shares it.

**A card fills the box you hand it, so work out the height before you hand one over.**
Four cards off `frame.body.columns(4)` are each as tall as the body, and two lines of
copy in a 5.4in card is nine tenths void.
`max(card_size(width, **spec).h for spec in cards)` is what the row actually needs: ask
a `stack` for that and the cards come out level, none of them padded out to the page.

`add_icon` is for the icons that are not on a card: beside a kicker, in the corner of a
metric, at the head of each band of a panel drawn by hand (§6.5). Stroked vectors, so
they scale and stay editable after export, in whatever colour you pass. The ink is inset
in the square you give it, and by no fixed fraction -- from nothing of the square's
height (`minus`, a rule) to nearly the whole of its width (`json`) -- so a row of icons
centred on their squares is not a row centred on its ink.
`the_ink_an_icon_covers(name, size)` is the box the strokes will really cover, in
inches, before anything is drawn — and `add_icon` hands back the same box, measured off
what landed.

**Search by meaning, not by filename.** `find_icons("deadline")` returns
`calendar_due`; `find_icons("risk")` returns `warning`; `find_icons("warehouse")`
returns `building_warehouse`. Every icon carries its upstream tags and shelf, and the
search reads them, so the word you would use on the slide is a good enough query. A
name that misses answers the same way — `add_icon(slide, "kpi", ...)` raises with the
closest names in the message, so a wrong guess costs no round.

**A few hundred names need no lookup at all**, and guessing at one costs a request that
`find_icons` or the list would have saved. The ones worth knowing by heart are grouped
by what they are for in
[deck/build/references/icons.md](deck/build/references/icons.md); `find_icons` reaches
the rest. Open the list when you want to scan for the right idea rather than guess a
word for it.

Keep them small -- an icon is a mark beside type, so the square follows the line it
labels rather than a size of its own -- and give each real space. On a card that
arithmetic is `card()`'s and not yours. What to avoid is the filled chip behind every
one: a row of identical coloured badges is the thing that makes a deck look generated.
If a card wants a surface, give the whole card a surface.

## 7.5 Processes, flows and timelines

A sequence is drawn with the shape that means sequence: five rectangles with gaps
between them is a list, five chevrons that interlock is a process, and the difference
is legible from the back of the room. `chevron_row`, `timeline`, `connect` and
`preset` are how, and never by computing the geometry yourself.
[deck/build/references/shapes.md](deck/build/references/shapes.md) has the signatures, the label box to
write into, and the knobs that are angles in degrees rather than fractions — the one
that fails silently. Open it when the page has a sequence, a branch or a route, and
not otherwise: if it has none of those it needs none of this.

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

**`ppt_template(project=..., pages=[4, 5])` reads an example page back as the
python-pptx that would draw it** — up to six pages a call, and the second way to look
at a template after its renders. It is flat and literal on purpose: every position in
inches, inherited sizes and colours resolved to what they resolve to, groups opened,
and a `# [n]` above each shape, which is the numbering `shape_at` and an integer key in
`adapt` both use. The page's own pictures are written into the build directory beside
your script, so an `add_picture("template_00.png", ...)` line in it runs as pasted; the
imports it needs head the block as comments, so uncomment the ones you keep.
Read a page this way when you need its real numbers — the pitch of a card row, the
title's exact box, what its accent actually is. What it cannot give you comes back
saying so: a custom-drawn shape, a gradient, a pattern fill or a semi-transparent fill
is emitted as a comment naming itself, and the reply lists which pages hold one. Those
are the pages to clone rather than redraw.

### The pages you clone

`ppt_template` names them —
`house_pages: {"cover": 1, "agenda": 2, "section": 3, "closing": 11}` — and renders
them. Those four are what a reader recognises the house by, and a deck that draws its
own cover announces itself as not the user's before a word of it is read. They are also
the pages code cannot reproduce: custom geometry, a gradient or a semi-transparent fill
that python-pptx has no way to write.

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
`texts={"单击此处添加页面标题": ...}` still works and needs you to know what the slot currently
says. Without either, `items` alone leaves the header empty -- text this call does not
name is emptied -- and writing "目录" and "Agenda" back as two new boxes over the clone is
the one construction `template_underlay` refuses.

**Both land on a content page; `subtitle` runs out on a closing one.** `title` finds a
row on nearly any page and `subtitle` on nearly any content page, so on the page you are
adapting, use them. Where `subtitle` has nothing to resolve to is a closing page, whose
line under the title is the presenter's name rather than a second heading, and a cover
that centres its title mid-page. When it does not resolve `adapt` raises rather than
guessing, listing every shape on the page, so name that line in `texts={...}` off the
render and carry on. A cover can go the other way and resolve `title` onto the presenter
credit sitting above the real title -- if the render after your call shows the wrong row
rewritten, that is this, and `texts` keyed on the words is the fix.

**Numbering: an integer key is the shape's place on the page**, and the reference prints
it — every shape carries a `# [n]` line above it, groups opened, counting shapes that
cannot be drawn as well as those that can. `shape_at(slide, n)` is the same numbering
after `adapt` returns, for anything else the page needs.

**A string key is the text a shape holds, never the shape's name.** It matches on the
words in the box, so `drop=["Presenter name"]` removes the cover line that says that,
and `drop=["template-credit"]` raises `KeyError` listing every shape on the page — as
does a real shape name out of the file, `drop=["标题 4"]`. Names look like the obvious
handle for furniture you want gone; the page's text and its index are the only two
handles there are.

**Text you do not name is emptied**, because the words in a template page are its example
copy. Shapes you do not name keep what the template put there — that is the point, the
page arrives designed.

**An emptied shape is not a removed one.** `adapt` empties the text it does not name and
leaves the box standing, because the box is often the design -- a tinted panel, a
numbered circle -- and an empty box shows nothing while a deleted one takes its panel
with it. So `""` is not a spelling of delete: written into the agenda slots a deck has
no sections for, it ships the numbered bubbles anyway. `drop=` removes a shape before
the clone is filled, by the same two keys as `texts`; `drop_shape(shape_at(slide, 7))`
removes one afterwards, on the slide `adapt` handed back -- the rule left over a picture
you moved, the panel a re-flow made redundant, the shapes you only find on the render.

**Never lay a new text box over a page you cloned.** The construction is `clone_page`
for the background, `add_textbox` for the copy, `replace_text` never called — and it
ships the template's own "单击此处添加长一点的副标题" under your own text on every page.
`placeholder_copy` and `template_underlay` both refuse it. If you cloned a page, every
word on it arrives by replacing a word that was there.

**The template's photographs are placeholders.** A cover's stock photograph of a meeting
table says nothing about the deck's subject. Replace it with a figure from the sources
or take the frame out — `pictures={7: f"{FIGURES}/fig3.png"}`, or `drop=[9]`. Small
graphics are different: an image under a tenth of the page is an icon, a corner flourish
or a rule, and belongs to the design.

**A landscape figure does not go in a portrait frame.** `replace_picture` refuses when
the two are more than 2x apart and says both numbers, because contained it becomes a
strip in an empty frame and cropped it loses its outer columns. Give the frame the box
the figure needs: `pictures={4: (f"{FIGURES}/fig2.png", (0.8, 1.6, 7.4, 4.2))}`, where
the four numbers are `(left, top, width, height)` in inches — a **size**, the same one
`place` takes, and not the two corners a `ppt_layout.Box` holds. A `Box` may be handed
over whole and is converted, so with `frame, notes = body.split_left(0.52)` both
`pictures={4: (f"{FIGURES}/fig2.png", frame)}` and `place(shape_at(slide, 4), frame)`
say what they mean; what you must not do is unpack one into four numbers, because
`Box.corners(0.72, 1.24, 12.6, 6.7)` read as a size draws a 12.6x6.7in frame off the
side of a 13.33in page where those corners name an 11.88x5.46in one. Same four numbers,
and only one of the two readings is your figure.

### The pages you compose

Most of a deck is these. A content page is *composed* rather than filled, and that is
the ordinary road, not the last resort: the template's example pages are made of covers,
contents lists and dividers plus arrangements built for somebody else's content, and a
six-card prototype filled with four cards leaves a hole where the other two were.
Adapting the nearest prototype is right where one carries the page's information shape;
where none does, composing the page is the answer and needs no apology in `needs`.

**Start the page from the layout the template's own content pages sit on.** That is
`layout_for_a_page_you_draw` in the measured `house_style`, and `add_slide` on it brings
the template's background with it — the corner device, the edge rules, the ground
colour, all of which hang on the layout and none of which you have to draw. Every
composed page added on that one layout carries the same corner decoration and border
without a line of code about it, which is what makes a deck read as one deck.
`add_slide` on a blank layout, or painting your own background, is how a deck ends up
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
layers, chosen for what this page argues (§3.5). That file is where the composition
vocabulary is; this section is only how a composed page joins the template.

**Match the title row's anchor, not just its box.** The box is where the row is and the
anchor is where the ink lands in it, and the two readings of one 0.98in row are most of
an inch apart. A template's title placeholder usually declares nothing and inherits from
its master, and one bundled template's master says `anchor="b"` — so its cloned pages
sink a single-line title to the bottom of the row while `write`, which anchors to the
top unless told otherwise, puts the same line near the top. `title_row_as_code` is that
call with the anchor already in it; `title_row` states it in words. A composed page
whose anchor disagrees with the template's comes back as `title_row` (§12).

The subtitle row is reported the same way and for the same reason — a template may set
its own **centred** under a left-aligned title, which is its designer's decision and not
a mistake to correct. Take the alignment from `subtitle_row_as_code` rather than
assuming it matches the title's.

**One scale for the whole deck.** Every page title at `type_pt["title"]`, body copy at
`type_pt["body"]`, captions at `type_pt["caption"]`, and the floors of §3 under all
three. The same
role at the same size on every page: `type_drift` measures this off the render and
reports a slot the deck sets at more than one size, because a reader reads each page
against the one before it and a body size that moves page to page reads as unfinished.

**Set every size yourself, and give the box the room it needs.** `write` turns autofit
off on purpose: a box that cannot hold its copy comes back as a measurement rather than
as type quietly dropping to 10.8pt. If `overset_copy` says a box needs 1.51in and has
0.90in, the answer is a taller box, a wider column, less copy or a second page —
never a smaller size. `text_size`, `card_size` and `points_size` say the same 1.51in before the
build does (§4), which is the cheaper place to hear it.

**Stay inside the safe area.** `safe_area_in` is where the template keeps its own
content; a layout's artwork lives outside it, and copy laid across that artwork comes
back as `over_layout_art`. The title row is not a suggestion either — put the page
title in the box `title_row` names, at the size it names, and every page of the deck
lines up with the template's own.

**Ink is `foreground`. `surface` and `background` are what the ground is painted with.**
On a dark template those three are white, near-black and black, so reaching for "the
dark one" as a type colour puts near-black type on black. One line is measured off the
render, against the ground each box actually landed on: under **2:1** nothing is legible
and `unreadable` refuses the deck. Above that line nothing is said, because a template's
own pages ship shapes barely above it and an authored shape there cannot be told from
one. So everything between "legible" and "comfortable" is yours to judge off the render
(§10): a title over a mid-tone plane is worth looking at twice even though nothing will
mention it.

**Take the face from the page, not out of the theme.** `ppt_theme`'s `font_family` is
what the template's *theme* declares, and a template can declare 微软雅黑 while every run on
its example pages is Arial. `house_style`'s `face` is what the pages actually use — pass
it as `font=` and its CJK companion as `cjk_font=`, or a line of mixed text comes out in
two unrelated faces.

Which pages are cloned is decided in the outline, not here: `ppt_outline` takes a
`prototype` per page, and with a template bound it refuses an outline whose cover,
index and closing do not name the template's own. Every other page that names none is
listed back to you as a question — pick the nearest content example, or say in `needs`
why no example carries this page's information shape. The opposite failure is measured
after the build: a page that named a prototype and was not built on it comes back as
`prototype_kept`, which reports rather than refuses, because a page that composed its
own layout and reads well is a page, and rewriting the outline is one line.

## 9. Formulas

**Never set an expression with `write`.** A formula in a text box is prose: it wraps
where the box runs out, and where it runs out is the middle of a symbol — and every
subscript in it is flat, so the one thing the notation carried is gone. That is what
`flat_formula` reports. `formula()` sets it as one unbreakable line, steps down the
ramp until it fits, and splits at the expression's own separators at the floor;
`formula_type_size` says which step it will land on before it lands there.
[deck/build/references/formulas.md](deck/build/references/formulas.md) has the notation it reads and what to
do with an expression that is not load-bearing. Open it when a page carries
mathematics.

## 10. Look at the deck

`ppt_build` renders **one page** back per call, labelled with its number and anything
measured on it, and says how many of the deck's pages it did not show. So looking at
the deck is a call per page: `page_from=` walks it from where you left off and
`slides=[7]` asks for one page by number. **A program that ran without error is not
visual evidence.** Open each render and name something concrete on it before you
change it.

**Reference — angles to look from, not a checklist and not a score.** What the code
measures for you is in §12; these are the readings nothing measures, so they are the
ones that need your eyes:

- a role at the wrong step of the scale — a caption as large as the body, a title a
  point above its subhead, a source line as large as what it credits;
- consecutive pages on the same skeleton, differing only in their words;
- unreadable, distorted, blank or badly cropped figures;
- empty charts, weak labels, bars out of proportion to their values, misleading
  scales — nothing measures any of these, so this line is the only check there is;
- icons too small to read, or crowding the text they belong to;
- a sparse or mechanically forced layout — compartments where an argument belongs;
- decorative treatment repeated with no relation to what the page says.

Two questions settle most of it: is the smallest type still readable with the page
shrunk to a third — roughly the back of a lecture theatre — and, held beside the page
before it, is this a different page or the same page with different words.

The reply names how many pages it did not show and how to ask for them. **A named
subset is not the deck**, and a page you have not looked at is a page you have not
checked. Then edit and build again: the first build is a draft, and a build that
passed the gates is not finished.

**Work one page at a time when a page is wrong.** `ppt_build(slides=[7])` rebuilds and
hands back page 7 alone, in seconds: edit that page's block, look at it, fix it, look
again. A tight loop on one page finds what a sweep over twelve does not — a sweep
tells you twelve pages have something wrong, and a loop tells you what. Run the full
build when a batch is settled; that is what runs the gates and publishes.

**This is the last look the deck gets.** There is no pass after you, and no stage that
rearranges the pages once the gates clear. Every reading above is yours, and a page you
send on unlooked-at is a page that ships as it is.

## 11. The order is enforced, not suggested

Two of these are refusals rather than advice, because asking did not work — a model
reading this same document still went straight from the request to a build.

`ppt_build` refuses until `ppt_prepare` has read the task, and until `ppt_brief`
holds the three things the user decides. And it refuses a deck carrying a page whose
code has never been rendered back to you: the record is keyed on the block that drew
each page, so a page you looked at stays looked at and a page you then edited does
not. `slides=[1]` on an eighteen-page deck is no longer a way to ship seventeen pages
nobody saw.

That one cannot be answered by editing the deck. Run the build again, walk the deck a
page at a time, and look.

One more thing the order decides: **`deck/ingest/materials.md` does not exist until
`ppt_ingest` has run.** Reaching for it first costs a request and gets a
`No such file or directory`. When it is there, `ppt_ingest` returns `materials_index` —
every part of it with the line it starts at — and `read_file` takes `offset` and
`limit`. Read the parts the deck stands on rather than the file: whatever you read stays
in the context and is re-sent on every request after it, so a single whole-file read is
the most expensive thing in the deck.

## 12. What the build refuses, and what it only reports

**Refused**, with the page named. Four of them land at the outline, before a line of
the program is written: a **figure id the catalogue does not hold**; a **layout id
the catalogue does not carry** -- the ids are `P1` to `P41` as page structures and `M1`
to `M26` as modifier layers, not
zero-padded, and one that is not in `deck/build/references/layouts.md` can only come
from not having opened it; with a template
bound, an outline whose **cover, index and closing** do not name the
template's own pages; and, when ingest extracted no figures at all, a **cited page
nobody opened**. That last one is the deck's pictures, still on the other end of the
links its own sources give you: `web_fetch(extractMode="images")` each URL the
materials cite — a picture on a page a source cites arrives with the caption its
author wrote — and `ppt_fetch` what you will use, or the PDF behind an abstract so
`ppt_ingest` extracts its figures. For each one that holds nothing usable, or that
will not load, say so in `ppt_outline`'s `swept`:
`[{"url": "...", "found": "text only, no figures"}]`. That record is kept with the
deck, so a later call does not ask again. Nothing is recorded until all three clear.

The rest land on the built deck:

- a page **citing one figure while showing another**;
- a **length the brief did not agree**, or **the wrong language**;
- a theme that is **not the bound template's**;
- a page the build **cannot map back to** a block of your code;
- a page you have **never been shown**;
- a page whose plan **promised a figure and that shows no picture** — place it, or
  plan the page again without it;
- a page whose plan **planned a table and that shows none** — draw it with
  `ppt_layout.table()`, or plan the page again without one;
- copy **printing an escape** (`48.3\nOVIS` — pass a real newline);
- content **three fifths hidden behind an opaque shape** drawn after it;
- type a reader **cannot make out**: under 2:1 against the ground it landed on;
- words **colliding in the render**, which is not answerable by shrinking them;
- the template's own **placeholder text** still on a page you cloned, or that page
  cloned for its background with **new text boxes laid over** it.

**Reported**, most of them with a page number, and all of them yours to judge: a
**filled colour bar** carrying nothing; **type under the floors**, and body copy set at a
size that is **not a step of the ramp** and under `BODY_PT`; a deck with **too few
content pages showing anything** — a figure, a table, a chart or a diagram, counted
across the deck rather than page by page; a **table too wide to read**, or one still
**wearing Office's own look**, or one drawn with **fewer columns or rows than its plan
names**, or a page with **no room for the table its plan describes** — the last read off
the plan and the drawn table together, so the plan-time warning is settled by the file
rather than argued with; a **rule struck through** a row; copy **escaping a
card**, or **crowding its panel's rim**; a shape **over the page edge**, and copy
**painted off the page** by a box with wrapping off; copy **on the layout's artwork**;
an **expression set as prose**; **two or more parallel claims in one box**; an
**inspected figure caption naming something the materials never mention**; a
**label the render broke** onto a second line, or one **wrapped** in a box too narrow
for it; **copy that does not fit** the box it was put in, and copy the file states that
the render **clips instead of wrapping**; a **large blank field** where content should
be; **two groups with no more air between them** than inside them; **one slot the deck
sets at several sizes**, and **titles that start at different left edges** or sit at a
different anchor inside that row than the template's own; a deck whose composed pages
**nearly all resolve to one page structure**, read off the shapes each page actually
carries; and a build
whose **pages cannot be told apart** in what the run recorded — a separate reading from
the refusal above, which is about the `# SLIDE` blocks in your file where this one is
about what the run made of them. The two can arrive together.

Inside a template, three more report: a deck **none of whose pages came from the
template's own** structural pages; the template's **photographs still showing**; and a
page built on a **prototype other than the one its outline named**. A fourth arrives
earlier and as a question rather than a finding — a page **drawn from scratch where the
plan named no prototype** is listed back at the outline, for you to answer with an
example page or with a reason in `needs`.

At the plan, three more: a page planned with **one line and nothing else**, a deck
spending **a quarter of itself on pages carrying no argument**, and **material too thin
for the pages agreed** — plus, at ingest, a **source that could not be read** at all.

Some come with the move that answers them:

| Reported | What it means | The move |
| --- | --- | --- |
| `crowded_panel` | a line touching the bottom rim of its own panel | the padding your other panels have, or one line less |
| `orphan_line` | a label broken as "为什么要统 / 一" | a hair more width, or fewer characters |
| `unseparated_blocks` | two groups with no more air between them than inside them | a wider gap, a surface, or a hairline |
| `excessive_whitespace` | a large blank field between body groups, below the content, or inside a panel | grow the load-bearing content, redistribute it, or shorten the panel |
| `native_table` | a table still wearing Office's banding and gallery style | `ppt_layout.table()`, which takes both off and keeps every row -- or draw the grid yourself, which never had them |
| `spilled_copy` | a `wrap=False` box painting its copy off the page | turn wrapping on and give the box a second line's height |
| `type_drift` | one slot the deck repeats, set at several sizes | give the boxes the height one size needs |
| `type_scale` | copy set over the 14pt floor but under `BODY_PT`, at a size the ramp does not have | `size=BODY_PT`, `size=LABEL_PT`, or `the_largest_step_this_copy_takes(text, box, font=F)` |
| `flat_formula` | an expression set with `write`, so its subscripts are flat and the box may break it inside a symbol | `formula()` (§9) |
| `listed_claims` | one box holding two or more parallel claims, so they read as a list to be read out | separate them so a reader can see where one ends; §6.5 has the calls that measure a region before it is drawn |
| `inferred_caption` | a caption written by looking at a figure, naming a product or system the materials never mention -- or disagreeing with the name its source printed | caption what is visible, or ingest the source that establishes the name; never credit the figure to it |

Warnings are mostly not refusals for a reason: nearly every one of them can be "fixed"
by making the type smaller, and a gate that demanded they clear would get exactly that.
The two that do refuse — words over words, and content behind a panel — are the two
that shrinking makes worse rather than better.

**Three silences the build reports rather than hides.** With nothing ingested there is
no figure catalogue, so a page crediting Figure 4 while showing Figure 5 would not have
been caught. With no brief recorded, neither the deck's length nor its language has
been checked against anything that was agreed. With no renderer on the machine, nothing
measured on the rendered page ran at all — the collisions, the contrast, the sizes the
type actually came out at. None of the three stops a deck, all three mean "not checked"
rather than "clean", and all three belong in what you tell the user.

**What nothing checks at all:** whether a number or a name on a page is one the sources
printed. There is no index of what the materials state and no gate that reads one, so
every figure on every page is yours to have read correctly and yours to be right about.
