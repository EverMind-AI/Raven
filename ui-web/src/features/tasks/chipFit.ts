/* How many of a task's file chips fit in a folded strip.
 *
 * The strip is a wrapping flex row, so which chip lands on which line is a
 * greedy fill the page can replay from widths alone: a chip joins the current
 * line while it and the gap before it still fit, and starts the next one
 * otherwise. Replaying it here, instead of capping at a fixed count, is what
 * lets a strip of short names show more of them than a strip of long ones --
 * file names vary far too much in length for one number to fold both well.
 */

const linesOf = (widths: number[], avail: number, gap: number): number => {
  let lines = 0
  let used = 0
  for (const w of widths) {
    if (lines && used + gap + w <= avail) used += gap + w
    else {
      lines += 1
      used = w
    }
  }
  return lines
}

/* The count of leading chips to show when folded to `rows` lines. All of them
   when they already fit; otherwise the most that still leave room on the last
   line for the `+N` chip (`more` wide) that stands in for the rest. */
export function fitChips(widths: number[], avail: number, gap: number, more: number, rows: number): number {
  if (linesOf(widths, avail, gap) <= rows) return widths.length
  for (let k = widths.length - 1; k > 0; k -= 1) {
    if (linesOf([...widths.slice(0, k), more], avail, gap) <= rows) return k
  }
  return 0
}
