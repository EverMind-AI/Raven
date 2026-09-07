# SVG and vector

Apply this card to standalone or inline SVG, vector illustrations, icons,
diagrams, maps, and edited SVG assets.

## Route gate

This card explains how to implement an object that has already been authorized as
vector output. It does not grant permission to choose SVG as the production method.

Do not enter this card for a new hero image, decorative illustration, scene,
texture, expressive icon, feature pictogram, abstract technology artwork, or any
asset whose quality depends on composition, style, material, lighting, organic
form, or family consistency. Route those assets to `image_generate` through the
shared asset contract. Do not replace that call with handwritten paths, gradients,
filters, Canvas shapes, or CSS decoration.

New SVG is allowed only when its source is one of:

- an authoritative brand, standard, or user-provided vector master;
- an existing asset from the selected icon or design system;
- the deterministic output of a professional chart, map, diagram, or vector tool;
- minimal geometry whose exact text, data, coordinates, topology, or interaction
  state is the visual claim;
- a vector master explicitly required by the owning domain contract.

If a generated visual must later become vector, use a professional vectorization
or icon-authoring tool and preserve lineage to the generated master. Hand tracing
by the model, or rebuilding the shape from arcs and formulas in code, is not an
acceptable substitute.

## Vectorization tools (shipped in the runtime image)

Two tracers ship in the image; pick by the source, never redraw by hand:

| tool | use for | command |
| --- | --- | --- |
| `potrace` (CLI) | black-and-white marks, glyph-like symbols, one colour | `convert mark.png -colorspace Gray -threshold 50% mark.pbm && potrace mark.pbm --svg --alphamax 1.0 --turdsize 8 --opttolerance 0.2 -o mark.svg` |
| `vtracer` (Python, in the Design environment) | multi-colour flat artwork, pictograms with several fills | `python3 -c "import vtracer; vtracer.convert_image_to_svg_py('src.png','out.svg', colormode='color', hierarchical='stacked', mode='spline', filter_speckle=8, corner_threshold=60, length_threshold=4.0, splice_threshold=45, path_precision=3)"` |

Workflow for a logo or icon drawn from a generated concept sheet:

1. Crop the chosen candidate from the concept image at full resolution; upscale to ≥ 1500 px on the long edge before tracing if it is smaller.
2. For one-colour marks binarize first (`-threshold 50%`); otherwise the tracer returns the background as a shape.
3. Trace, then clean in Inkscape CLI (`inkscape in.svg --export-plain-svg --export-filename=out.svg`; run `path-simplify` through `--actions` only after `inkscape --action-list` confirms it exists), set an intentional `viewBox`, name groups, and remove speckles.
4. Record lineage: generated master path, crop box, tracer and parameters, in the asset ledger. Recolour by editing fills, never by re-tracing a recoloured raster.
5. Check at 32 px, 256 px and print size; if the silhouette breaks, fix the raster and re-trace instead of hand-editing nodes.

Inkscape 1.2's bitmap tracing is GUI-only; do not attempt it from the CLI.

## Output contract

Decide explicitly:

- standalone document or inline fragment;
- self-contained or linked assets;
- pure vector or hybrid raster/vector;
- static or active content;
- target use: browser document, inline DOM, `<img>`, `<object>`, CSS image,
  design-editor import, or print.

Validate in the named target because those consumption modes support different
fonts, styles, scripting, links, and accessibility behavior.

## Construction

- Produce valid XML with `xmlns="http://www.w3.org/2000/svg"` and an
  intentional `viewBox`. Add fixed `width` and `height` only when required.
- Define paint order explicitly. Organize major parts into semantic groups with
  local origins; move each component through its parent transform.
- Put symbols, gradients, patterns, masks, clips, and filters in `<defs>`. Use
  unique, stable, CSS/XML-safe IDs and resolve every reference.
- Derive aligned, mirrored, connected, or repeated geometry from shared anchors
  and formulas. Prefer simple primitives and paths over flattened opaque data.
- Account for strokes, markers, transforms, clips, masks, and filter overflow
  when checking visible bounds. Expand filter regions to avoid clipped blur or
  shadow.
- Use `preserveAspectRatio` and `vector-effect` only when their behavior is
  intentional. Keep an editable source before optimization or path flattening.

## Text, accessibility, and editing

- Add `<title>` and `<desc>` or an equivalent accessible name to meaningful
  standalone artwork. Mark decorative SVG appropriately in its embedding
  context.
- Preserve editable text when possible. If font fidelity is uncertain, record
  the source font; disclose outlined lettering and retain an editable source.
- When editing, inventory and preserve public IDs, CSS selectors, DOM hooks,
  accessibility labels, animation targets, references, masks, filters, and
  external dependencies unless a breaking change is authorized.
- Modify the smallest coherent component. Do not flatten or rewrite an entire
  SVG for a local edit without an explicit reason.

## Validation matrix

- Parse with DTD and external entities disabled.
- Check unique IDs and resolve every `<use>`, paint server, mask, clip, filter,
  and linked asset.
- Inspect at thumbnail, intended, and zoomed sizes. Confirm silhouette, focal
  point, reading order, text, and filter edges.
- When portability matters, render with the target consumer and one additional
  renderer.
- For edits, compare the same viewport before and after and confirm unrelated
  structure and behavior remain unchanged.
- For responsive or animated SVG, inspect required minimum/maximum sizes and
  representative animation states.
