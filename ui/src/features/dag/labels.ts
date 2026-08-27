/* What each node label should read.
 *
 * Split out of the sheet's builder, where the deciding and the drawing were
 * welded together -- which is the reason the transcript's dag card could not
 * reuse it and drew raw ids that ran past their box instead.
 *
 * Only the namespace decision lives here. Where a line actually ENDS is the
 * node box's business and CSS answers it, at the real width, in the real font,
 * every time either changes. This file used to answer that too, by measuring
 * each candidate in the document and cutting until it fitted, and the answer
 * was a string the render then cached: a graph measured before its font arrived
 * or inside a folded ancestor -- where nothing has a width -- kept an answer
 * taken in the dark for the rest of the session, and its labels ran past the
 * box and across the node beside them.
 */

/* The longest prefix every id shares, cut back to a separator so a label never
   starts mid-word. Empty for fewer than two ids, for ids that share nothing, and
   for a prefix that would leave a label empty -- in each of those the ids are
   already telling them apart. */
export function sharedPrefix(ids: string[]): string {
  if (ids.length < 2) return ''
  const first = ids[0] as string
  let n = 0
  while (n < first.length && ids.every((s) => s[n] === first[n])) n += 1
  const head = first.slice(0, n)
  const cut = Math.max(head.lastIndexOf('-'), head.lastIndexOf('_'))
  if (cut < 0) return ''
  const prefix = head.slice(0, cut + 1)
  return ids.every((s) => s.length > prefix.length) ? prefix : ''
}

/* Roughly how wide a label sets, without asking the document: a character from
   the CJK and kana ranges takes about one em, everything else about six tenths
   of one in the faces these are drawn in.

   Rough is enough, because the only question it answers is whether the labels
   are long enough to be worth stripping a namespace from. Being a few percent
   out moves that decision at the margin; it cannot leave a label overflowing,
   which is what a measurement used to be relied on for. */
export function roughWidth(label: string, em: number): number {
  let w = 0
  for (const ch of label) w += (ch.codePointAt(0) as number) >= 0x1100 ? em : em * 0.6
  return w
}

/* Two properties, both deliberate:

   - the shared prefix is decided for the WHOLE graph at once. A playbook
     namespaces every node with its own name and run tag, so the first twenty-odd
     characters are identical across the graph, and the box cuts from the tail --
     the only part that tells the nodes apart. Applied per label, one node would
     keep its namespace while its neighbour lost it, and the labels would stop
     being comparable, which is what a reader is doing with them.
   - it fires only when something is long enough to be cut. Ids that all fit are
     shown as their author wrote them, namespace and all. */
export function trimShared(labels: string[], room: number, em: number): string[] {
  if (!labels.some((label) => roughWidth(label, em) > room)) return [...labels]
  const prefix = sharedPrefix(labels)
  return prefix ? labels.map((label) => label.slice(prefix.length)) : [...labels]
}
