# Page shapes

**Reference shapes, not a menu.** Their job is to make the list you choose from wider than
the one you would have thought of. Combine them, cut them across each other, invent the one
this argument needs: what a page has to say decides its shape, and a page that resolves to
none of these because the argument had another form is a right answer. What is not an
answer is every page taking the same shape because no other was considered.

A page is one structure plus what is layered on it. Shares are of the body box (the canvas
inside its margins), so they hold at any size; where a range is given, that is what the
drawn pages measured, not a house style.

## Reference structures

| Use | The shape, with its numbers | Must not become |
| --- | --- | --- |
| S1 a visual and its reading, two columns | evidence to be read (figure, chart, table) takes **0.55-0.64** of the width and the reading the rest; a visual that is a mark rather than evidence takes **0.37-0.42** and the reading the larger share. Both columns end on one line | a picture in one column and unrelated bullets in the other. The reading has to name what the visual shows |
| S2 bands across the page | a statement band **0.17-0.23** of the height over what it heads at **0.53-0.77**; the row beneath is 2-4 blocks at **0.32w** (three) or **0.23w** (four) | three bands of equal height -- that says the page has three unrelated things on it |
| S3 two comparable things on one baseline | **0.48-0.51** each, both cut to the taller, one accent on the side that carries the answer, the same labels in the same order down both | two panels whose rows are in different orders. Then the reader is matching by eye |
| S4 an equal grid of cells | three columns **0.32w**, four **0.23w**, two rows **0.41-0.47h**. Give one cell back to copy | filling every slot because there is a grid. The missing tile is what makes it a composition |
| S5 one dominant visual, subordinates with it | the dominant **0.53-0.61** of the width, the rest stacked beside it; any overlap well under **0.60** of what is under it | regions of equal weight. The unequal division is the argument |
| S6 a visual as the page's ground | the whole canvas, or a belt **0.19-0.24** of the height, or a strip **0.23** of the width. Type sits on a flat plate or the opaque end of a scrim | type laid straight onto a photograph or a gradient -- it comes back `unreadable` on a page that looks fine |
| S7 a rail down one side | **0.22-0.28** of the width, full height, carrying the page's header | a different header on every page |
| S8 a spine with stops on it | 4-6 stops, the spine band **0.44-0.65** of the height and what it explains under it; the stops need not be equal | rectangles with gaps between them. The spine is what makes them a sequence |
| S9 a hub with spokes | the hub at **0.32w**, three to five spokes, each to something the hub actually reaches | a spoke per noun in the title |
| S10 the table as the page | six rows or more, margin to margin, in the columns' own proportions | four rows that needed explaining -- that is S1 with a reading lane |
| S11 one statement, the rest air | content under **0.40** of the canvas, centred in the height rather than parked at the top. About one per deck | a page that ran out. Spend it where the argument turns |

## Type, in pt on a 13.333 x 7.5in canvas

| Role | Size |
| --- | --- |
| Cover title | 32-48pt, at most two lines |
| Page title | 28-40pt |
| Section opener | 28-36pt, with a 14pt muted label over it |
| Statement or quote | 24-32pt, at most three lines |
| Hero number | 48-100pt. One per page, nothing competes with it |
| Card heading | 16-20pt |
| Body | 14-18pt |
| Caption, label, footer | 11-12pt |

Two families at most, and use weight rather than more sizes. Below 14pt is body under the
floor and below 10.8pt is refused outright; footers may go to 8.0pt. If the content does
not fit at these sizes, split the page or cut it. Never shrink the type to make it fit.

Keep content **0.7in** clear of every edge.

## Compositions worth having in the list

Common slide kits converge on these; sizes converted to pt for a 13.333 x 7.5in canvas.
Reference, like everything above.

| Composition | What is on it |
| --- | --- |
| Cover, centred | title 32-48pt bold, subtitle 16-20pt, meta 12-14pt, centred both ways, nothing else |
| Cover, left block | the same three on a left block about 0.8in in, mark bottom-right |
| Section break | a 14pt muted label and a 28-36pt title. Only those two, and air |
| Key statement | one sentence at 24-32pt, at most two lines, optional 14pt attribution |
| Quote | 18-24pt, at most three lines, 12-14pt attribution, generous padding |
| One hero number | 14pt muted label, the number at 60-100pt, 14-16pt of context. Nothing competes |
| Two or three numbers | 48-60pt (two) or 36-48pt (three) with 14pt labels, on one baseline |
| Three pillars | three columns, each a mark, a 16pt label and 12-14pt of copy at most two lines |
| Icon row | three or four, same icon size, labels on one baseline |
| Process | 3-5 steps, each a numeral or icon, a 16pt label and one line under it, equal spacing |
| 2x2 matrix | four cells, 16pt heading and 12-14pt body each, equal cards, tight gutter |
| Compare two | two columns, 16-20pt heading and 2-4 points each, balanced content |
| Before and after | the same two columns with an arrow between, the left muted and the right strong |
| Chart and its reading | chart about 60% of the height, the finding under it at 14-16pt bold, one highlight |
| List | a 24pt title and 3-5 items at 14-16pt, no wrapping, large gaps |
| Hero image | full bleed, title 28-36pt over a scrim, subtitle 14-16pt |
| Closing | headline 28-36pt, one line under it, where to go next |

## Compositions the packaged templates use

Read off the 54 pages the deck engine keeps as reference layouts (ten templates under
`raven_ppt/assets/templates`), rendered and measured. Shares are of the canvas width `w` and
height `h`; the title row sits in the top 0.16-0.20 on every one of them. Each is a shape to
take when the page's content has that structure, not a page to reproduce.

| Composition | The shape, with its numbers | Template pages |
| --- | --- | --- |
| P1 photo panel, numbered list beside | one photograph **0.40-0.55w**, cut to a circle or a rounded panel, on one side; 2-4 numbered items with a bold label and one line each on the other, each item **0.10-0.12h** apart, a hairline under each | amber 8, amber 13, teal 13 |
| P2 photo band, cards under | a photograph the full text width, **0.26-0.36h**, at **0.16-0.19h**; three or four cards under it in one row, each **0.21-0.27w**, label and two lines | amber 10, beige 17, warm 14, warm 6 |
| P3 photo mosaic with a number | a 3-column strip: two photographs **0.25w** each, one cut in half rows, and a tinted column **0.35w** carrying one number at 54pt and 2-3 lines | amber 9 |
| P4 photo on a slanted plane | the photograph fills one side **0.45-0.55w** full height, its inner edge cut on a diagonal; 3 numbered points stacked on the other side, numerals in the accent | warm 9, gold 7 |
| P5 photo right, pills left | 4 numbered pills **0.26w** stacked at **0.10h** pitch on the left, each with one line of copy to its right; a photograph **0.36w x 0.43h** on the right under a two-line heading | green 8 |
| P6 object in a ring, labels radiating | one object (photograph, render or icon disc) **0.28-0.32w** centred, in a ring; 4-6 labels around it at two heights each side, each label a pill with one line under it | amber 17, gold 6, gold 15, black 10, black 11, black 13, black 15 |
| P7 photo strips staggered | three portrait photographs **0.16w x 0.59h**, the middle one dropped **0.14h**, on the right **0.55w**; heading and 3 short paragraphs on the left | black 14 |
| P8 numbers in a row | four numbers at 60pt in tinted boxes **0.16w x 0.24h** on one baseline, a bold label and two lines under each | amber 6 |
| P9 rings in a row | three or four icon rings **0.10-0.18w**, a label under each and two lines of copy under that; the rings on one baseline at **0.45-0.50h** | beige 19, teal 5, gold 5 |
| P10 chevron process | five chevrons **0.17w x 0.05h** in one row at **0.40-0.47h**, numbered; a bold label and two lines under each, dropped from the chevron by a thin stem | mint 6, green 9, blue 7 |
| P11 staircase | 3-4 pills stepping up a diagonal from bottom-left to top-right, each **0.16w**, one step **0.08-0.10h** and **0.2w** on from the last; the label and one line beside each step; the last step may carry a large arrow | mint 9, green 10 |
| P12 labels along a curve | one curved spine (an S or a rising arc) across the page; 4-6 labels sit on it as pills, alternating above and below, each with one or two lines; the spine is the only line on the page | gold 4, gold 13 |
| P13 tree | one root pill centred at **0.30h**, two children at **0.50h**, four grandchildren at **0.72h**, joined by elbow connectors; every node a pill **0.14w** with one line under it | green 6 |
| P14 two chart panels | two white panels **0.43w x 0.40h** side by side at **0.20h**, a titled bar or line chart in each; under them one row of 3-4 columns of caption at 12-14pt, or 3 tinted cards | amber 4, gold 12, gold 20 |
| P15 hexagon or angled cards | five dark cards **0.17w x 0.41h** in one row, tops cut to a hexagon or a chamfer, an icon at the top of each and a label and two lines below | black 5 |
| P16 half-discs on the floor | five half-discs **0.17w** standing on the bottom edge with an icon in each, the labels and copy above them in five columns | green 14 |
| P17 arrow block with a list | one large arrow shape **0.40w x 0.55h** on the left carrying the page's claim in 24-28pt; on the right 4 rows, each an icon disc, a bold label and two lines | amber 12 |
| P18 circles and a 2x2 | two big circles **0.13w** stacked on the left with a label each; four cards **0.27w x 0.26h** in a 2x2 on the right, a pill heading on each | gold 9 |
| P19 stagger over a band | a tinted band **0.15h** across the middle; five cards **0.18w x 0.20h** alternating above and below it, each with a pill heading | warm 8 |
| P20 gear or hub with spokes | one gear or disc **0.25w** on the left, five pills **0.30w** stacked on the right joined to it by short spokes | black 11, amber 17 |

The first and last pages are statements, not information: a picture and few words, aiming
for a feeling rather than a summary. On a page with no picture, let the type carry it --
oversized, asymmetric, off the grid if that is what the sentence wants. Unusual is allowed;
unreadable is not.

## Tables

**Ask what the grid buys before drawing one.** What it buys is a reader scanning down a
column and comparing -- 93.05% against 66.80%, 200ms against 3000ms. Where that is what the
cells hold, a table is the right page and the rest of this is detail.

Where each cell is a phrase or a sentence, the grid usually buys nothing and costs a third
of the page: no mark, no ground, no scannable column, and a reader taking each row as a
unit anyway. Cards, one per row, fill the page instead. Usually, not always -- a reference
page somebody scans for one line wants the grid whatever is in the cells.

There is no house table style here. A table of figures, a matrix of marks, a table with one
column tinted to carry the answer, a grouped statement with totals under a rule and a
lookup nobody reads straight through are five different pages. Draw the one this content
is, and let two tables in one deck look different if they are doing different work.

Three things bite when it is drawn by hand rather than by a table API:

- **Nothing reflows.** Every box is placed absolutely, so a cell that wraps to two lines
  does not push the row below it down -- it runs under the rule drawn at a fixed y, and the
  rule crosses its second line. Measure the tallest cell in the row, set the row height
  from that, then place the rule.
- **One baseline per row.** A mark, a name, a value and a note sitting at four different
  heights is the commonest way a hand-drawn table looks hand-drawn.
- **Paint before you type.** Grounds go down before any word, or the fill covers the copy;
  bands first, column tints over them.

A column too narrow for its longest cell is the cause, not the symptom. Widen it, cut the
copy, or let that column be a card.

## Which shape for what

| The page | Shape |
| --- | --- |
| Cover, section opener, closing | S6 or S11, with a generated background |
| One statement, one quote | S11 |
| Three or four pillars, features, a 2x2 matrix | S4, each cell with an icon |
| Two things compared, before and after | S3 |
| One headline number | S1 with the number as the visual; three of them, S4 |
| A process, a timeline, a roadmap | S8 |
| A chart and what it means | S1 |
| An architecture, a dependency map | S9 or S5 |
| A benchmark, a price list, a year-by-year rate | S10 |

## Across every shape

- One idea per page. The title states the takeaway, not the method or the topic.
- A connector lands on a box at both ends. A line into empty space is a node you did not
  draw, and nothing checks for it.
- Align to one grid. Four different left edges on a page read as four pages.
- 2-3 colours plus neutrals. The accent marks one thing per page; two accents and neither
  is the answer.
- No one shape on more than 60% of the pages, and at least 40% of them place something
  off-centre. Both are measured off the built file.
- Cover and closing are statements, not summaries: a strong picture and few words.
