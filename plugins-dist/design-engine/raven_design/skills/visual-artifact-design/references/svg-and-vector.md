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
by the model is not an acceptable substitute.

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
