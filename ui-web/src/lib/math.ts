/* TeX in an answer, turned into something a browser can typeset.
 *
 * A model asked for mathematics writes TeX, because that is what mathematics
 * is written in everywhere it is written down. The prose renderer had no
 * opinion about it, so a report on group theory arrived as its own source: a
 * third of one such document was `$...$` runs sitting in the paragraph text,
 * dollar signs and backslashes and all.
 *
 * The output is MathML, not a box of positioned glyphs. That is the whole
 * reason this is affordable: the browser already knows how to set mathematics
 * and already has the fonts for it, so nothing here ships a typeface. The
 * alternative -- a layout engine that draws the formula itself -- costs about
 * 1.4MB once its fonts are counted, against a page that is 1.4MB entire, and
 * the page is served as one self-contained file.
 *
 * TeX that cannot be read is left as the author wrote it. A formula rendered
 * wrong is worse than a formula not rendered: the source is at least honest
 * about being source, while a silently mis-set equation is a claim about
 * mathematics that nobody asked this page to make.
 */

import temml from 'temml'

/** One formula as MathML, or null when the TeX could not be read. */
export function mathHtml(tex: string, display: boolean): string | null {
  const src = tex.trim()
  if (!src) return null
  try {
    /* `throwOnError` so a formula this cannot read reaches the caller as a
       refusal rather than as Temml's own error markup in the middle of a
       sentence. The caller's answer to null is to leave the source alone. */
    const html = temml.renderToString(src, { displayMode: display, throwOnError: true })
    return html.includes('<math') ? html : null
  } catch {
    return null
  }
}
