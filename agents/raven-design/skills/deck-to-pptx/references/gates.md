# What the gates measure

Nine checks refuse a deck. The rest report. Both read the render, not the file, wherever a
render exists.

## Refuse publication

| Check | Criterion |
| --- | --- |
| `unreadable` | text-to-ground contrast ratio under 2.0 |
| `word_collision` | words overlap over 40% of a word's area |
| `displaced_copy` | copy landed outside its column, past 3.0pt |
| `emptied_page` | a page carrying nothing |
| `placeholder_copy` | a cloned page still says the template's own words |
| `template_underlay` | the template's layer shows through the page |
| `page_budget` | more pages than the brief agreed |
| `language` | a page not in the brief's language |
| `citation` | a claim from a source with no source named |

## Emptiness

| Check | Criterion |
| --- | --- |
| `sparse_container` | a card or panel filled under 35%, or with over 0.45in clear on all sides, or 1.5in larger than its contents |
| `unanchored_page` | no type at 20pt or more, no figure over 20% of the page, under 0.35in of ink |
| `thin_copy` | under 150 characters on a content page, 120 on a card page, 90 on a data page |
| `undivided_body` | several blocks of body copy with nothing between them |
| `excessive_whitespace` | measured off the render |

## Type

| Check | Criterion |
| --- | --- |
| `type_floor` | body under 14.0pt; hard floor 10.8pt; footers 8.0pt |
| `type_scale` | body between the floor and 16.0pt, off the ramp |
| `type_drift` | one repeated box rendered at sizes differing by more than 1.12x |
| `row_type_drift` | members of one row differing by more than 1.10x |
| `outranked_title` | a title under 1.4x the line beneath it |

## Alignment

| Check | Criterion |
| --- | --- |
| `flush_drift` | blocks declared flush, rendered over 1.0pt apart |
| `sibling_drift` | the same element of side-by-side containers, over 2.0pt apart |
| `grid_drift` | two column grids on one page, gaps 0.06in, from 3 rows |
| `off_page` | content outside the canvas |

## Figures

| Check | Criterion |
| --- | --- |
| `figure_distortion` | aspect ratio off by more than 1.20x |
| `figure_crop` | over half the picture cropped away |
| `figure_undersized` | a hero under 40% of the body, a mark over 5% |
| `washed_backdrop` | a picture over 80% of the page at 12-80% opacity |
| `covered_shape` | something painted over 60% of what was drawn before it |
| `over_layout_art` | content on top of the layout's own artwork |

## Habits that read as machine-made

| Check | Criterion |
| --- | --- |
| `equal_card_habit` | 3 cards in a row spanning 60% of the width, on 35% of pages, from 2 pages |
| `symmetry_habit` | under 40% of pages placing anything off-centre |
| `repeated_layout` | one prototype on over 60% of pages, from 4 pages |
| `same_mark` | 5 or more identical marks |
| `rule_strike` | a rule under 4.5pt tall and over 36pt wide serving no grid |
| `banded_table`, `native_table`, `wide_table` | a table in Office's own dress |
| `flat_formula` | one layout formula repeated down the deck |

## Furniture

| Check | Criterion |
| --- | --- |
| `no_footer`, `unnumbered_pages` | from 5 composed pages, under half of them naming themselves |
| `no_anchor` | a page with nothing for the eye to land on |
| `orphan_line` | one line of a paragraph alone |
| `clipped_copy`, `overset_copy`, `spilled_copy`, `box_overflow`, `card_overflow` | copy past the box that holds it |

## Order

`ppt_build` refuses without a brief, an intake plan and an outline, in that order. On this
route there is no `ppt_build`: the same three decisions are still made before the program is
written.
