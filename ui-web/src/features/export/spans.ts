/* Cutting a context into the plain text and the hits to highlight.

   The gateway counts a hit's start and end in Unicode code points (Python
   string offsets), while a JS string is indexed in UTF-16 code units, so a
   context with an emoji or any other astral character before a hit would be
   highlighted off by one per such character if the offsets were used on the
   string directly. The text is split into code points first. Spans that are
   not whole numbers inside the text, or that end before they start, are
   dropped; overlapping and touching spans are merged, so the pieces put back
   together are exactly the text. */

export interface Segment {
  text: string
  hit: boolean
}

export function segments(text: string, spans: readonly (readonly number[])[]): Segment[] {
  const chars = Array.from(text)
  const ranges = spans
    .filter((span) => span.length >= 2 && Number.isInteger(span[0]) && Number.isInteger(span[1]))
    .map((span) => [Math.max(0, span[0]!), Math.min(chars.length, span[1]!)] as [number, number])
    .filter(([from, to]) => from < to)
    .sort((a, b) => a[0] - b[0])
  const merged: [number, number][] = []
  for (const range of ranges) {
    const last = merged[merged.length - 1]
    if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1])
    else merged.push([range[0], range[1]])
  }
  const out: Segment[] = []
  let at = 0
  for (const [from, to] of merged) {
    if (from > at) out.push({ text: chars.slice(at, from).join(''), hit: false })
    out.push({ text: chars.slice(from, to).join(''), hit: true })
    at = to
  }
  if (at < chars.length || !out.length) out.push({ text: chars.slice(at).join(''), hit: false })
  return out
}
