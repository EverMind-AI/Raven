# Fixed media

Apply this card to slides, posters, documents, fixed pages, print layouts, and
raster image exports.

## Pages and slides

- Treat every page or slide as a bounded canvas with safe margins and a clear
  entry point. Keep essential content away from crop and projection edges.
- Use a consistent grid, type system, reusable styles, and page roles while
  giving each page a clear reading priority. A reference table, comparison,
  index, or evidence page may coordinate several equally necessary objects;
  split content only when doing so preserves rather than fragments its task.
- Preserve intentional reading order across headings, body, figures, captions,
  footnotes, and page numbers. Keep repeated navigation and furniture stable.
- For slide decks, verify the sequence as a story and each slide at
  presentation distance. For documents, verify reading flow, pagination, and
  print behavior.

## Posters and raster output

- Establish native dimensions, aspect ratio, pixel density, transparency,
  color expectations, and export format before composing.
- Make the silhouette, focal point, and main message survive at thumbnail size;
  make fine detail and text hold at intended size.
- Avoid accidental resampling, soft text, jagged masks, halos, color banding,
  and unintended matte backgrounds. Inspect at 100% and at display size.
- Retain an editable source when delivering a flattened raster export.

## Validation matrix

- For a small set, inspect every page or slide. For a large repeated document,
  run automated checks over every page, then visually inspect every template,
  section boundary, exceptional page, densest/emptiest case, and first/last
  page. Record the coverage rule; an arbitrary sample is not whole-document
  evidence.
- Check pagination, line and page breaks, tables, figures, captions, footnotes,
  crop/safe margins, and page numbers in the rendered output.
- Compare the smallest intended preview with the full-size render for hierarchy
  and legibility.
- Confirm fonts and linked assets are embedded or packaged as required and that
  the exported deliverable opens in the target viewer.
- Claim a native master, theme, layout, or style system only when the editable
  source contains those actual structural references and they survive reopen
  and export. A visually consistent HTML or PDF can be a template or final
  export, but cannot by itself prove a presentation or publishing master.
