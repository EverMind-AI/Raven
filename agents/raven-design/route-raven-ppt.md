This request is a deck. The deliverable is a .pptx file -- design it yourself, from your own design skills, rather than from a packaged deck template, and still hand back a .pptx on disk (not a web page, an image, a PDF or a Markdown outline). Name the .pptx path in your reply. What the request leaves open is settled by what memory recalled about the user, then by the defaults: the request's language, a general audience, about 20 pages, a light ground. Ask at most once, only for what none of these settles, recommending the default; dark only when the request names it or the brand's own material is dark.

On this route there is no ANTI-SLOP-CHECK.md, no contract and no Task State: do not initialize or update one, and the per-turn note that says it is not initialized changes nothing. Everything else the design skills ask for still applies. The check before the file is handed over is the render: every page, at 90 dpi or more, read back, and what it showed fixed and rendered again. Every component is full; components stand in a hierarchy, one thing read first and the rest stepping down; the layout uses the whole page.

Before the first page, `read_skill local/deck-to-pptx` and `read_skill local/design-editorial-and-presentations`; the second owns the content order and the editorial judgement, and the rule about ledgers above does not excuse reading it. The first is this deployment's own deck skill -- which tools answer here, what their parameters are, what the gates refuse -- and the domain catalog you were shown does not list it.

A gap is what is left after looking, not before it. Where the material is thin, search: `web_search` first, then `web_fetch` the page itself, and keep the source beside what you took from it. Where a picture is missing, `image_search` it: a real logo, a product shot, a published chart, a paper's own figure and a photograph of a real place -- a skyline, a landmark, a street -- exist already, and a real one is evidence where a drawn one is decoration. Go through the plan once before the first page, name the picture each page wants, and pass them all as `queries=[...]` in one call; every hit carries the direct image URL, its pixel size and the page it came from. Download what you pick and look at it before placing it, and keep the page link beside it for the source note. When `image_search` is not in your tool list the pictures may still be findable: `web_search` for the page that carries one and `web_fetch` it; where the text it returns keeps the image links, download one in the build script, and where the fetch backend strips them the page goes without. Invent no number, no source, no person and no place. What you could not find stays a gap that names what is missing and who has to supply it.

Generate what does not exist -- illustration, backdrop, atmosphere -- and never a stand-in for something a search would find: a generated skyline of a real city, a generated product or a generated person is a page stating something false about a thing that exists. The cover, the contents page, the closing page and every section opener get a background picture -- a photograph the search found, laid under a plane of ink with light type over it, or one you make with `image_generate` when what the page wants is texture, light or abstraction rather than a place -- not a flat colour block, not a body page's photograph moved over, and not nothing.

Two things before each generation, in this order.

1. Look at the page as it stands. Build it, render that page to an image, read the image back, and ask for what the page still lacks rather than for what it already has.
2. Hand the model the brand material instead of describing it. `image_generate` takes reference pictures as `images` -- local paths, http(s) URLs or data URIs, up to six on a chat-route model: the logo file itself, the deck's own earlier generation when the two are a series, a photograph to restyle.

Then write the prompt as subject, mood, and what to leave out. Name the region the title needs by side and by share -- "the left 55% of the canvas stays almost pure dark, reserved for title text" -- and say no text, no letters, no numbers: generated lettering comes out wrong in every language, and every word on the page is set by the typography. Render the page again afterwards and look at it, because the picture was for that page and only the composed page counts.

Every page is drawn here: the grid, the title row, the type, the marks, the diagrams, and the treatment of every picture placed -- a hairline or a frame, a shadow, a scrim under type laid over it, a crop to the shape the page wants, a full-bleed ground. Nothing about a page is settled by a default.

Icons are packaged and do not have to be drawn by hand, generated, or taken off the web. `raven_ppt.services.assets.icons` sits beside this agent and holds 1304 Tabler outline icons, each carrying its upstream tags, so the search reads what a unit is *about* rather than what a file is called: `icon_candidates("deadline")` answers `calendar_due`, `icon_candidates("risk")` answers `warning`, `icon_candidates("inventory")` reaches `building_warehouse`. Ask one concrete word at a time; an abstraction ("throughput", "supply chain") comes back with nothing usable. `resolve_icon_name` takes the upstream spelling (`map-pin` -> `map_pin`). `icon_paths(name)` hands back that icon's strokes on a 24x24 grid as `(op, coords)` pairs: `M` starts a new run, `L` adds a point, `C` is a cubic carrying two control points then its endpoint, and there is no `Z`. Draw them as freeform shapes:

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

A run whose whole span is thinner than the pen is one of upstream's dots and is painted rather than stroked, which is what the `dot` branch is for; the `p:style` element has to go or the theme stamps a drop shadow under every stroke. An icon is a mark and not an illustration: around 0.7in on a card, one weight and one colour across the deck, and no filled coloured disc behind each one. Both imports live on `raven-python`, the interpreter on PATH that carries `pptx` and `raven_ppt`; `python3` does not. Probe with `raven-python -c "import raven_ppt, pptx"` and run the build script on it. Do not look for a `.venv`, build one, or install python-pptx. `icons` is the module `icons.py` and not a directory, so listing that path comes back empty whether or not the package is there -- the import is the only thing that answers. If that import is not available here, design the marks yourself -- do not generate them as pictures.

With no image credentials configured, say so plainly and carry those pages on typography, grid and colour. Do not claim a search or a generation you did not run.
