/* What each node label should read, given the room it has.
 *
 * Split out of the sheet's builder, where the deciding and the measuring were
 * welded together -- which is the reason the transcript's dag card could not
 * reuse it and drew raw ids that ran past their box instead.
 *
 * The measuring stays with the caller, because only the caller knows what a
 * string is going to be wide in. It arrives as a function rather than as an
 * array of widths: truncation asks about strings that do not exist yet (each
 * candidate as characters come off the tail), so a table of the full labels'
 * widths cannot answer it without assuming a fixed advance per character. The
 * labels are monospace today and that assumption would hold today.
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

/* Three properties, all of them deliberate and none of them free:

   - the shared prefix is decided for the WHOLE graph at once. A playbook
     namespaces every node with its own name and run tag, so the first twenty-odd
     characters are identical across the graph, and cutting from the tail removes
     the only part that tells the nodes apart. Applied per label, one node would
     keep its namespace while its neighbour lost it, and the labels would stop
     being comparable -- which is what a reader is doing with them.
   - it fires only when something actually overflows. An id that fits is shown
     as its author wrote it.
   - the tail truncation re-measures each candidate rather than dividing a width
     by a character count, so it is right for a proportional face too. */
export function fitLabels(
  ids: string[],
  avail: number,
  measure: (label: string, index: number) => number,
): string[] {
  if (!ids.some((id, i) => measure(id, i) > avail)) return [...ids]
  const prefix = sharedPrefix(ids)
  const cut = prefix ? ids.map((id) => id.slice(prefix.length)) : [...ids]
  return cut.map((label, i) => {
    /* Measures what is on screen, not the next candidate: a label that fits is
       left alone even when one more character would not fit, which is what the
       sheet has always done. Getting this backwards puts an ellipsis on a label
       that had room. */
    let s = label
    let shown = label
    while (s.length > 1 && measure(shown, i) > avail) {
      s = s.slice(0, -1)
      shown = s + '…'
    }
    return shown
  })
}
