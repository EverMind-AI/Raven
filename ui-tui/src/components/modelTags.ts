/* What a model can do, as glyphs a terminal can actually draw.
 *
 * The web picker hangs a tooltip off an icon; a terminal has nowhere to put
 * one, so the glyphs come with a legend built from the rows on screen. Only
 * the tags actually present are explained -- a fixed legend of thirteen
 * symbols would be longer than the list it annotates, and most providers use
 * five of them.
 *
 * Every glyph is a single-width BMP symbol with no emoji presentation. The
 * brand mark aside, this file is read on terminals that render an emoji two
 * cells wide and misalign the whole column, so nothing here reaches for one.
 *
 * Input and output modalities are deliberately not drawn: each one beyond text
 * is already a capability (image in is `image-recognition`, image out is
 * `image-generation`), so a second row of glyphs would restate the first.
 */

/* Drawn in this order, so two models with the same tags produce the same
   badge and the eye can compare down the column. */
const TAGS: ReadonlyArray<readonly [string, string, string]> = [
  ['reasoning', '✦', 'reasoning'],
  ['function-call', 'ƒ', 'tools'],
  ['structured-output', '≡', 'structured'],
  ['image-recognition', '▣', 'reads images'],
  ['audio-recognition', '♪', 'reads audio'],
  ['video-recognition', '▷', 'reads video'],
  ['file-input', '▤', 'takes files'],
  ['image-generation', '❖', 'makes images'],
  ['audio-generation', '♫', 'makes audio'],
  ['video-generation', '⧉', 'makes video'],
  ['embedding', '∷', 'embeddings'],
  ['rerank', '⇅', 'reranking'],
  ['computer-use', '▭', 'computer use'],
]

export interface ModelTagFacts {
  capabilities?: string[]
  context_window?: number
}

/* 128000 -> 128K, 1000000 -> 1M. A window is read as a size, not counted: the
   exact figure is noise on every row and the rounded one is what a person
   compares. */
export function contextLabel(tokens: number): string {
  if (tokens >= 1_000_000) {
    const millions = tokens / 1_000_000
    return `${millions >= 10 || Number.isInteger(millions) ? Math.round(millions) : millions.toFixed(1)}M`
  }

  if (tokens >= 1000) {
    return `${Math.round(tokens / 1000)}K`
  }

  return String(tokens)
}

/* The glyphs for one model, plus its window. Empty when the registry publishes
   nothing, which a row renders as no badge -- absence is "unknown", and a
   placeholder would read as a denial. */
export function tagBadge(facts: ModelTagFacts | undefined): string {
  if (!facts) {
    return ''
  }

  const glyphs = TAGS.filter(([name]) => facts.capabilities?.includes(name))
    .map(([, glyph]) => glyph)
    .join('')
  const window = facts.context_window ? contextLabel(facts.context_window) : ''

  return [glyphs, window].filter(Boolean).join(' ')
}

/* What the glyphs on screen mean, in the order they are drawn. Built from the
   rows given rather than from the whole vocabulary, so the line stays as short
   as the list it explains. */
export function tagLegend(rows: ReadonlyArray<ModelTagFacts | undefined>): string {
  const present = new Set<string>()

  for (const row of rows) {
    for (const name of row?.capabilities ?? []) {
      present.add(name)
    }
  }

  return TAGS.filter(([name]) => present.has(name))
    .map(([, glyph, label]) => `${glyph} ${label}`)
    .join('  ')
}
