export const PASTE_SNIPPET_RE = /\[\[[^\n]*?\]\]/g

/**
 * One collapsed paste as the composer holds it: the label in the input and the
 * body it stands for.
 */
export interface PasteSnippetLike {
  label: string
  text: string
}

/**
 * Replace each paste label in ``value`` with the body it was collapsed from.
 *
 * Every caller that sends or records the composer's text has to expand first,
 * or the label reaches the model (and now the history) as literal text. Labels
 * are consumed in order, so the same label can appear more than once.
 */
export const expandSnips = (snips: ReadonlyArray<PasteSnippetLike>) => {
  const byLabel = new Map<string, string[]>()

  for (const { label, text } of snips) {
    const hit = byLabel.get(label)
    hit ? hit.push(text) : byLabel.set(label, [text])
  }

  return (value: string) => value.replace(PASTE_SNIPPET_RE, tok => byLabel.get(tok)?.shift() ?? tok)
}
