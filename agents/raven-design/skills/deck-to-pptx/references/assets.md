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

`web_search` returns pages. For pictures, ask Serper's image surface directly with the key
already configured at `tools.web.search.apiKey`:

```python
httpx.post(
    "https://google.serper.dev/images",
    json={"q": query, "num": 20},
    headers={"X-API-KEY": key, "Content-Type": "application/json"},
)
```

Each hit carries `imageUrl`, `imageWidth`, `imageHeight`, `domain` and `link`. Drop anything
under 640px wide or under 360px tall -- below that it is soft at half-page width. Keep the
`link` beside what you took: it is what the page's source note credits.

Look at what you fetched before placing it. A hit that is the right size can still be a
thumbnail sheet, a watermarked stock frame, or somebody else's slide about the subject.

A fetched mark that already has an alpha channel lands on the page's own ground with
nothing behind it. The worst thing to do with a cut-out is to box it in a white rectangle.

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

## Icons

1304 outline icons ship beside this agent. Each carries its upstream tags, so search by what
the unit is about.

```python
from raven_ppt.services.assets.icons import icon_candidates, icon_paths, resolve_icon_name

icon_candidates("deadline")     # -> calendar_due
icon_candidates("risk")         # -> warning
icon_candidates("inventory")    # -> building_warehouse
resolve_icon_name("map-pin")    # -> map_pin
```

Ask one concrete word at a time. An abstraction (`throughput`, `supply chain`) returns
nothing usable.

`icon_paths(name)` returns the strokes on a 24x24 grid as `(op, coords)` pairs: `M` starts a
run, `L` adds a point, `C` is a cubic carrying two control points then its endpoint. There is
no `Z`. Draw them as freeforms:

```python
from pptx.dml.color import RGBColor
from pptx.util import Emu, Pt
from raven_ppt.services.assets.icons import icon_paths

EMU, STYLE = 914400, "{http://schemas.openxmlformats.org/presentationml/2006/main}style"


def icon_runs(name):
    runs = []
    for path in icon_paths(name):
        run, here = [], None
        for op, xy in path:
            if op == "M":
                if len(run) > 1:
                    runs.append(run)
                here = (xy[0], xy[1])
                run = [here]
            elif op == "L":
                here = (xy[0], xy[1])
                run.append(here)
            elif op == "C" and here is not None:
                (x0, y0), (x1, y1, x2, y2, x3, y3) = here, xy
                for step in range(1, 9):
                    t, u = step / 8, 1 - step / 8
                    run.append((u**3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t**3 * x3,
                                u**3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t**3 * y3))
                here = (x3, y3)
        if len(run) > 1:
            runs.append(run)
    return runs


def add_icon(slide, name, left_in, top_in, size_in, rgb, width_pt=1.5):
    scale, pen = size_in * EMU / 24.0, Pt(width_pt)
    for run in icon_runs(name):
        pts = [(left_in * EMU + x * scale, top_in * EMU + y * scale) for x, y in run]
        xs, ys = [x for x, _ in pts], [y for _, y in pts]
        dot = max(max(xs) - min(xs), max(ys) - min(ys)) < pen
        if dot:
            pts = [(xs[0], ys[0]), (xs[0] + pen, ys[0]), (xs[0] + pen, ys[0] + pen), (xs[0], ys[0] + pen)]
        builder = slide.shapes.build_freeform(Emu(int(pts[0][0])), Emu(int(pts[0][1])))
        builder.add_line_segments([(Emu(int(x)), Emu(int(y))) for x, y in pts[1:]], close=dot)
        shape = builder.convert_to_shape()
        if dot:
            shape.fill.solid()
            shape.fill.fore_color.rgb = RGBColor.from_string(rgb)
            shape.line.fill.background()
        else:
            shape.fill.background()
            shape.line.color.rgb = RGBColor.from_string(rgb)
            shape.line.width = pen
        for style in shape._element.findall(STYLE):
            shape._element.remove(style)
```

A run whose whole span is thinner than the pen is one of upstream's dots: fill it rather than
stroke it. The `p:style` element has to go or the theme adds a shadow.

Where that import is unavailable, design the marks yourself. Do not generate them as
pictures.
