---
name: deck-to-pptx
description: "Build a PowerPoint .pptx file with python-pptx, on this deployment, without the deck engine. Use when the deliverable is a .pptx on disk: presentation, deck, slides, pitch deck, keynote, report deck, 幻灯片, 演示文稿, 做一份 PPT, 出一个 pptx, 汇报材料. Carries what is specific to this deployment -- which tools exist, which parameters they take, and what the gates refuse -- and nothing a competent author already knows. Pairs with design-editorial-and-presentations, which owns content order and editorial judgement. Not for a deck bound to one of the packaged templates: that route runs on Raven-PPT and uses ppt-script-authoring."
---

# Deck to .pptx

The deliverable is a `.pptx` file on disk. Not a web page, an image, a PDF, or a Markdown
outline. Name its absolute path in the reply.

## What this deployment has

| Need | Call | Note |
| --- | --- | --- |
| Facts the material does not carry | `web_search`, then `web_fetch` the page | there is no other search |
| A real logo, product shot, published chart | `https://google.serper.dev/images` | key at `tools.web.search.apiKey`, see `references/assets.md` |
| A picture that does not exist yet | `image_generate` | reference pictures go in `images`, up to six |
| An icon | `raven_ppt.services.assets.icons` | 1304 outline icons, see `references/assets.md` |
| Render a page to look at it | `soffice --headless --convert-to pdf`, then `pdftoppm` | |

With no image key configured, `image_generate` says so. Say which pages would have had a
picture and carry them on type, grid, rule and colour.

## Settle four things first

Language, audience, length, and whether the deck runs light or dark. They are the user's to
decide, every page is measured against them, and a deck built on a guess is measured
against a brief nobody agreed to.

Ask with `ask_user`, in one call, before any other work. Where there is no user to ask --
the request arrived from another agent -- read all four out of the request and say in the
reply which you took and where from. Do not default any of them silently.

## Rules

1. Invent no number, no source, no person, no place. Search first. What the search does not
   find stays a gap that names what is missing and who supplies it.
2. A logo, a product screen, a real person, a real place, a published chart, a paper's own
   figure: **fetch it, never generate it.** A generated stand-in for something that exists
   is a fabrication the page presents as evidence. Generate only what has no original:
   illustration, backdrop, atmosphere.
3. Anything the deck names that has a face of its own -- a company, a product, a repository,
   a place -- has a picture somewhere. Search the whole deck's list in one pass before
   drawing anything: what the material names, and the ordinary furniture it never links --
   the logos, the marks, the product shots. Keep each picture's page URL beside it for the
   source note. A page about five products with no mark of any of them is a page that did
   not look.
4. Decide the deck's visual direction once, before the first picture, and say what it is:
   ground, two or three colours, and whether pictures are photographs or drawings. **A
   brand's website is a starting point, not the verdict** -- a dark web hero does not make
   a dark deck, and a printed handout and a projected keynote want opposite grounds.
   Chaining every background off the first one gives a deck one look and no decision.
5. [layouts.md](references/layouts.md) holds reference shapes with their proportions and
   the type ramp. Read it to widen the list you choose from, not to pick from a menu: what
   a page has to say decides its shape. No one shape on more than 60% of the deck.
6. Repeating units -- a card, a row, a step -- take an icon from the packaged set. Search it
   by what the unit is about, not by a filename: 1304 of them ship beside this agent, and
   `references/assets.md` says how.
   An icon is a mark and not an illustration: about 0.7in, one weight and one colour across
   the deck, and no filled disc behind it.
7. The cover, the contents page, the closing page and every section opener get a generated
   background. Not a flat colour block, not a body page's photograph, not nothing.
   **Two or three backgrounds cover a deck.** Section openers share one; the cover and the
   closing page can be the same picture at different crops. A distinct generation per
   section is 8 serial calls where 3 would do, and a deck whose openers all look different
   has no house. Each call takes over a minute and they do not overlap, so settle the whole
   short list before building rather than asking for one more while drawing each page.
8. Before generating a picture for a page, render that page and look at it. Ask for what the
   page lacks.
9. Give `image_generate` the brand material in `images`. Do not describe it in words.
   **Crop the reference to the mark first.** A reference outranks the prompt: hand it a
   web hero, an og image or a screenshot and it will reproduce that page -- the wordmark,
   the headline, the buttons -- however firmly the prompt says no text. Say what to take
   from the reference (its palette, its texture, its light) and that its layout, its type
   and its furniture are not to be reproduced.
10. Every prompt names the region the type needs -- which side, what share -- and ends with
   `no text, no letters, no numbers`. All words on a page are set by the typography.
11. After generating, rebuild the page and look at the render. Only the composed page counts.
12. Read the deck's own render before delivering. Every page.

## The numbers a page is measured against

13.333 x 7.5in is 16:9. Keep content **0.7in** clear of every edge.

| Role | Size |
| --- | --- |
| Cover title | 32-48pt |
| Page title | 28-40pt |
| Section opener | 28-36pt, over a 14pt muted label |
| Statement, quote | 24-32pt, at most three lines |
| Hero number | 48-100pt, one per page |
| Card heading | 16-20pt |
| Body | 14-18pt |
| Caption, label, footer | 11-12pt |

Body under **14.0pt** is reported and under **10.8pt** is refused; footers may go to 8.0pt.
If it does not fit at these sizes, split the page or cut it -- never shrink the type.

## Mechanics that bite here

- **A connector lands on a box at both ends.** A line into empty space is a node you did
  not draw, and nothing checks for it.
- **A hand-drawn table does not reflow.** Every box is placed absolutely, so a cell that
  wraps to two lines does not push the row below it down -- it runs under the rule drawn at
  a fixed y, and the rule crosses its second line. Measure the tallest cell in the row, set
  the row height from that, then place the rule. Set every element in a row from one
  baseline, and put every ground down before any word, or the fill covers the copy.

- **`python3` on the path is not the interpreter that has `python-pptx`.** Check with
  `python3 -c "import pptx"` before writing the build script. Where it fails, the one that
  works is the raven install's own `.venv/bin/python`, and `raven_ppt` (the icons) is on
  its path too. Run the script with that interpreter, or put its `site-packages` on
  `sys.path` at the top of the script. A build whose first line is
  `from pptx import Presentation` dies on line one otherwise.
- `spAutoFit` with `word_wrap=False` makes LibreOffice re-centre the text. Remove
  `a:spAutoFit` and `a:normAutofit` from `bodyPr` when alignment has to hold.
- `shape.shadow.inherit = False` on every drawn shape, or the theme stamps a drop shadow.
- Remove the `p:style` element from a freeform, or the theme restyles its stroke.
- `slide_layouts[6]` is the blank layout. A layout with placeholders puts furniture on the
  page that nothing asked for.
- Set `slide_width` and `slide_height` before adding pages. 13.333 x 7.5in is 16:9.

## Load when you need it

- [layouts.md](references/layouts.md) -- eleven reference page shapes with the proportions
  they measured, seventeen cover/section/KPI/process compositions, and when a table is the
  wrong page. A registry to widen the list you choose from; the numbers above hold without
  it.
- [gates.md](references/gates.md) -- what refuses a deck and what only reports, with the
  number each check compares against. Read before the first build.
- [assets.md](references/assets.md) -- the icon library, image generation, and finding real
  pictures.
