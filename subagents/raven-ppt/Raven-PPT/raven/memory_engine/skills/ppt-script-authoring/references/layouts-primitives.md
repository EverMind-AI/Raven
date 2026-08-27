# The picture primitives -- what python-pptx has no API for

One of the passage files behind
[deck/build/references/layouts.md](deck/build/references/layouts.md), and the one to open
first when a page has a photograph on it: the seven treatments the picture structures and
the `M12`-`M24` layers are built out of, and the three things they cost. Every other
passage file assumes these are already defined.

`picture_fit` scales a figure to fit its box whole, which is right for evidence and wrong
for a photograph: a photograph asked to fill a region has to be cropped to it. Nothing in
`ppt_layout` does that, and none of the treatments below has a python-pptx API either, so
the picture patterns start from these seven. Written once, here, in the
shape §4 asks for: geometry positional, everything after it named.

```python
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def cover(slide, image, box):
    """A picture that fills `box` and is cropped to it, rather than centred in it."""
    shape = slide.shapes.add_picture(
        str(image), Inches(box.x0), Inches(box.y0), width=Inches(box.w), height=Inches(box.h)
    )
    wide, tall = shape.image.size
    have, want = wide / tall, box.w / box.h
    if have > want:
        shape.crop_left = shape.crop_right = (1 - want / have) / 2
    elif have < want:
        shape.crop_top = shape.crop_bottom = (1 - have / want) / 2
    return shape


def scrim(slide, box, colour, near, far, *, angle=0.0):
    """A gradient over a picture: `near` alpha where the angle starts, `far` where it ends.

    `angle` puts `near` at the left at 0, the bottom at 90, the right at 180, the top at 270.
    """
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, *box.pptx())
    shape.line.fill.background()
    shape.shadow.inherit = False
    shape.fill.gradient()
    shape.fill.gradient_angle = angle
    for stop, share in zip(shape.fill.gradient_stops, (near, far)):
        stop.color.rgb = rgb(colour)
        paint = stop._element.find(f"{A}srgbClr")
        paint.append(paint.makeelement(f"{A}alpha", {"val": str(int(share * 100000))}))
    return shape


def vignette(slide, box, colour, *, centre=0.0, edge=0.80):
    """A radial wash: `centre` alpha in the middle, `edge` at the corners."""
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, *box.pptx())
    shape.line.fill.background()
    shape.shadow.inherit = False
    shape.fill.gradient()
    for stop, share in zip(shape.fill.gradient_stops, (centre, edge)):
        stop.color.rgb = rgb(colour)
        paint = stop._element.find(f"{A}srgbClr")
        paint.append(paint.makeelement(f"{A}alpha", {"val": str(int(share * 100000))}))
    fill = shape.fill._xPr.find(f"{A}gradFill")
    fill.remove(fill.find(f"{A}lin"))
    path = fill.makeelement(f"{A}path", {"path": "circle"})
    path.append(path.makeelement(f"{A}fillToRect", {"l": "50000", "t": "50000", "r": "50000", "b": "50000"}))
    fill.append(path)
    return shape


def clip(picture, prst, *, adj=None):
    """Clip a picture to an Office preset shape instead of to its own rectangle."""
    geometry = picture._element.spPr.find(f"{A}prstGeom")
    geometry.set("prst", prst)
    if adj is not None:
        values = geometry.find(f"{A}avLst")
        values.append(values.makeelement(f"{A}gd", {"name": "adj", "fmla": f"val {int(adj * 100000)}"}))
    return picture


def fade(picture, share):
    """The picture's own transparency: `share` of it kept, the ground showing through."""
    blip = picture._element.blipFill.find(f"{A}blip")
    blip.append(blip.makeelement(f"{A}alphaModFix", {"amt": str(int(share * 100000))}))
    return picture


def duotone(picture, dark, light):
    """Re-grade a picture into two of the deck's own colours."""
    blip = picture._element.blipFill.find(f"{A}blip")
    both = blip.makeelement(f"{A}duotone", {})
    for colour in (dark, light):
        both.append(both.makeelement(f"{A}srgbClr", {"val": colour.lstrip("#")}))
    blip.append(both)
    return picture


def lift(picture, *, blur=0.20, drop=0.05, share=0.22):
    """A soft drop shadow under a picture panel."""
    picture.shadow.inherit = False
    effects = picture._element.spPr.find(f"{A}effectLst")
    shadow = effects.makeelement(
        f"{A}outerShdw",
        {"blurRad": str(int(blur * 914400)), "dist": str(int(drop * 914400)), "dir": "5400000", "rotWithShape": "0"},
    )
    paint = shadow.makeelement(f"{A}srgbClr", {"val": INK.lstrip("#")})
    paint.append(paint.makeelement(f"{A}alpha", {"val": str(int(share * 100000))}))
    shadow.append(paint)
    effects.append(shadow)
    return picture
```

Three things they cost:

- **A gradient is opaque as far as `covered_shape` is concerned.** python-pptx reports no
  alpha for a gradient fill, so `scrim` and `vignette` count as solid panels: a picture
  more than 60% under one is a blocking finding. A picture over 55% of the canvas is the
  page's *ground* and exempt, which is why a full-bleed photograph takes any scrim you
  like -- and why a card-sized one takes a scrim over part of it, or a flat `rect` under
  0.8 over all of it.
- **Type wants a flat plate, not a gradient.** `contrast` reads the ground as the modal
  pixel under the text box. Over a gradient every ground pixel differs slightly, the
  type's own colour wins the count, and the page comes back `unreadable` at 1.0:1 while
  looking perfectly fine in the render. A `rect` under the words fixes it; so does putting
  them where a two-stop scrim has flattened out.
- **`crop_*` is a fraction of the source, not of the box.** `crop_left = 0.06` throws away
  the left 6% of the *image*; the rest is then stretched to the frame, so the frame's
  aspect has to match what is left or the picture distorts. `cover` does that arithmetic;
  `P30` does it for a sub-region.
