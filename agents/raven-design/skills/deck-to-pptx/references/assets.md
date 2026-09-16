# Pictures, icons and evidence

## Finding a real picture

A logo, a product screen, a published chart, a paper's own figure and a photograph of a
real place exist already. Search for those. Generate only what does not exist:
illustration, backdrop, atmosphere.

**Never generate a substitute for something real** -- a product UI, a logo, a person, a
scientific result, a published figure, a statistical claim. A drawn approximation of a real
mark is not a placeholder, it is a page stating something false about a thing that exists.
If the search finds nothing, the page says so and names who can supply it.

Search the whole deck at once, before the first page is drawn: go through the plan page by
page, name the picture each one wants, and run them together. The pool is what the pages
choose from; one query per page, asked while that page is being drawn, gets whatever comes
back. Search what the material names -- a product, a company, a standard, a benchmark, a
paper -- and the furniture no material ever links: the logos and the marks.

`web_search` returns pages. For pictures call `image_search`, with every picture the deck
needs as `queries=[...]` in one call; the results come back grouped by query. Each hit
carries the direct image URL, its pixel size, the domain and the page it came from, and
anything under 640px wide or 360px tall is already dropped.
Keep the page link beside what you took: it is what the page's source note credits. When
`image_search` is not in your tool list, this deployment has no image search: say so, and
generate or go without.

Look at what you fetched before placing it. A hit that is the right size can still be a
thumbnail sheet, a watermarked stock frame, or somebody else's slide about the subject.

A fetched mark that already has an alpha channel lands on the page's own ground with
nothing behind it. The worst thing to do with a cut-out is to box it in a white rectangle.

## A paper's figures and formulas

Both run on `raven-python`.

```python
import pymupdf

doc = pymupdf.open("paper.pdf")

# Find the figure by its caption (with the colon), whichever page carries it.
page, caption = next((p, hit) for p in doc for hit in p.search_for("Figure 2:"))

# A raster figure is embedded as an image and comes out at its own resolution.
for i, info in enumerate(page.get_images(full=True)):
    pix = pymupdf.Pixmap(doc, info[0])
    if pix.n - pix.alpha >= 4:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    pix.save(f"assets/p{page.number + 1}_img{i}.png")

# A vector figure or a table has no embedded image: crop the region above the caption,
# look at the crop, move the rectangle until the whole figure and nothing else is inside.
region = pymupdf.Rect(page.rect.x0 + 40, caption.y0 - 300, page.rect.x1 - 40, caption.y0 - 4)
page.get_pixmap(clip=region, dpi=220).save("assets/fig2.png")
```

```python
from raven_ppt.services.assets.formulas import add_formula

shape = add_formula(
    slide,
    r"L(\theta)=L_{\mathrm{new}}(\theta)+\frac{\lambda}{2}\sum_i F_i\,(\theta_i-\theta^{*}_{i})^{2}",
    left_in=0.8, top_in=2.6, size_pt=18, colour="1F2A44", serif=False,
)
```

`size_pt` = the body size beside it. `serif=True` for a deck set in a serif face. `colour` =
the deck's ink. `max_width_in` scales a long expression down to a column. `\mathrm{}` sets a
word upright. No `\text{}`, no `align`, no matrix; prose stays outside the expression. The
returned shape carries the `width` and `height` to make room for.

Symbols inside a sentence are text, set as runs with real scripts rather than a picture:

```python
from raven_ppt.services.assets.formulas import math_runs

paragraph = box.text_frame.paragraphs[0]
math_runs(paragraph, "旧后验 p(θ | D_A) 由 F_i 和 θ* 决定", size_pt=16, colour="1F2A44")
```

`_A` and `_{new}` subscript; `^2` and `^{T}` superscript; `θ*` is θ with a raised star. A
plain Greek letter or a word needs neither call.

## Generating one

```python
image_generate(
    prompt="<subject, mood, what to leave out>",
    images=["/abs/path/logo.png"],      # up to 6 on a chat-routed model, 16 on gpt-image
    aspect_ratio="16:9",
)
```

- `images` takes local paths, http(s) URLs or data URIs. Put the brand mark there rather
  than describing it.
- Name the region the type needs, by side and by share: `the left 55% of the canvas stays
  almost pure dark, reserved for title text`.
- End every prompt with `no text, no letters, no numbers`.
- Render the page afterwards and look at it.
- No image key configured: the tool says so. Say which pages would have had a picture.

## A cut-out that really has an alpha channel

An illustration that floats on the page's own ground needs transparency, and asking for
"a transparent background" in words returns a drawn checkerboard. Generate the subject on a
green screen and key it out:

```python
CUT_OUT = (
    "Transparent-background production constraint: generate the subject on a solid pure "
    "green background, hex #00ff00. Keep the background flat, evenly lit and shadow-free; "
    "do not use green in the subject, its reflections, glow or edge details; nothing "
    "touches the edges of the image. The green will be removed by chroma keying into a "
    "real alpha channel."
)
```

```python
import io
from PIL import Image, ImageFilter

DESPILL_REACH = 5


def is_screen_green(r, g, b):
    if g >= 145 and r <= 130 and b <= 130 and g - max(r, b) >= 35:
        return True
    top, low = max(r, g, b), min(r, g, b)
    if top == 0 or g <= r or g <= b:
        return False
    spread = top - low
    if spread == 0:
        return False
    hue = 60 * ((b - r) / spread + 2)
    return 80 <= hue <= 160 and spread / top >= 0.35 and top / 255 >= 0.45


def key_out_green(png: bytes) -> tuple[bytes, float]:
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    width, height = image.size
    pixels = image.load()
    keyed = Image.new("L", image.size, 0)
    marks = keyed.load()
    count = 0
    for y in range(height):
        for x in range(width):
            r, g, b, _ = pixels[x, y]
            if is_screen_green(r, g, b):
                marks[x, y] = 255
                count += 1
    near = keyed.filter(ImageFilter.MaxFilter(DESPILL_REACH)).load()
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if marks[x, y]:
                pixels[x, y] = (r, g, b, 0)
            elif near[x, y] and g > max(r, b):
                pixels[x, y] = (r, max(r, b), b, a)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue(), count / float(width * height)
```

Every pixel is tested, so the pockets a flood from the border cannot reach -- between an arm
and a body -- go too. The rim a few pixels wide around what was keyed is despilled: an edge
pixel that blended with the screen has more green than its other channels, and taking that
excess off leaves the subject's colour instead of a green fringe.

**Under 20% keyed means it is not a cut-out.** The model painted a scene rather than a
subject on the screen. Ask again with one subject and nothing behind it.

## Helper modules

Write them beside the build script, once:

```
raven-python -c "from pathlib import Path; from raven_ppt.services.assets.script_helpers import script_helper_files; [Path(n).write_text(t, encoding='utf-8') for n, t in script_helper_files().items()]"
```

Files: `ppt_layout.py`, `ppt_theme.py`, `ppt_icons.py`, `ppt_shapes.py`, `ppt_charts.py` and their
JSON. Each module's docstring is its manual.

```python
from ppt_theme import THEMES
from ppt_layout import page, heading, write, points, card, plane, rule, footer, table, table_size, picture_fit, formula, stack, Box, fits, text_size, GUTTER, BODY_PT, LEAD_PT, LABEL_PT
from ppt_icons import add_icon, find_icons
from ppt_shapes import connect, timeline

T = {**THEMES[next(iter(THEMES))], "background": "#F8F7F3", "surface": "#FFFFFF", "foreground": "#1C3350",
     "muted": "#6B7280", "accent": "#C2571B", "accent_soft": "#F3E2D6", "grid": "#D8D5CC",
     "font_family": "Noto Sans", "cjk_font_family": "Noto Sans CJK SC"}
FONT, HAN = T["font_family"], T["cjk_font_family"]

slide = prs.slides.add_slide(prs.slide_layouts[6])
plane(slide, Box(0, 0, 13.333, 7.5), T, tint="background", radius=False)
frame = page(kicker=True, footer=True)
heading(slide, frame, T, "Title", kicker="Section", font=FONT, cjk_font=HAN)
for box, name in zip(frame.body.grid(3, 1), ("camera", "brain", "target")):
    card(slide, box, T, icon=name, title="...", body=("...", "..."), font=FONT, cjk_font=HAN)
footer(slide, frame.footer, T, note="Source: ...", font=FONT, cjk_font=HAN)
```

- `stack(box)` hands out bands: `down.take(h)`, `down.skip(GUTTER)`, `down.room` measures what is
  left without taking it, `down.rest()` takes it.
- `table_size(rows, T, box=down.room)` before `table(...)`; a table is as tall as its rows.
- `picture_fit(slide, path, box, T, caption=...)` places a picture with its caption.
- `formula(slide, box, tex, T, size=BODY_PT)` places a formula picture.
- `connect(slide, box_a, box_b, T, kind="straight", arrow=True)` joins two boxes edge to edge.
- Every drawing call returns what it covered as `.box`; the next thing goes at `.box.y1 + GUTTER`.

## Icons

1304 outline icons ship beside this agent. Each carries its upstream tags, so search by what
the unit is about.

```python
from raven_ppt.services.assets.icons import icon_candidates, resolve_icon_name

icon_candidates("deadline")     # -> calendar_due
icon_candidates("risk")         # -> warning
icon_candidates("inventory")    # -> building_warehouse
resolve_icon_name("map-pin")    # -> map_pin
```

In the script, `find_icons("risk")` from `ppt_icons` answers the same names, and
`add_icon(slide, name, Inches(x), Inches(y), Inches(0.7), "#1C3350")` draws one; `card(icon=name)`
places one on a card. Ask one concrete word at a time. An abstraction (`throughput`,
`supply chain`) returns nothing usable. Do not pass `width_pt`.

Where that import is unavailable, design the marks yourself. Do not generate them as
pictures.
