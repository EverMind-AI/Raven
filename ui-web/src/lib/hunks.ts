/* Builds the diff rows shared by workspace records and transcript call cards.
 * A pure leaf, with no store and no page of its own -- see the domain the
 * two callers pull it into. */

const CTX_KEEP = 3

/* One diff row: a tuple of [kind, text, oldLineNo, newLineNo], where a 'gap'
   row carries the folded lines as its second slot and an `open` expando the
   reader toggles in place. */
export type DiffRow = [string, string | string[], (number | null)?, (number | null)?] & {
  open?: boolean
}

export interface WsHunk {
  rows: DiffRow[]
  add: number
  del: number
}

export function fromEdit(oldText: string, newText: string): WsHunk {
  const removed = String(oldText || '').split('\n')
  const added = String(newText || '').split('\n')
  let head = 0
  while (head < removed.length && head < added.length && removed[head] === added[head]) head += 1
  let tail = 0
  while (tail < removed.length - head && tail < added.length - head
    && removed[removed.length - 1 - tail] === added[added.length - 1 - tail]) tail += 1
  const rows: DiffRow[] = []
  const lead = removed.slice(0, head)
  if (lead.length > CTX_KEEP) rows.push(['gap', lead.slice(0, lead.length - CTX_KEEP)])
  lead.slice(Math.max(0, lead.length - CTX_KEEP)).forEach((line) => rows.push(['ctx', line]))
  removed.slice(head, removed.length - tail).forEach((line) => rows.push(['del', line]))
  added.slice(head, added.length - tail).forEach((line) => rows.push(['add', line]))
  const rest = removed.slice(removed.length - tail)
  rest.slice(0, CTX_KEEP).forEach((line) => rows.push(['ctx', line]))
  if (rest.length > CTX_KEEP) rows.push(['gap', rest.slice(CTX_KEEP)])
  return { rows, add: added.length - head - tail, del: removed.length - head - tail }
}

export function fromWrite(content: string): WsHunk {
  const all = String(content == null ? '' : content).split('\n')
  /* A trailing empty split is not a line in the file or in a diff count. */
  if (all.length > 1 && all[all.length - 1] === '') all.pop()
  const rows: DiffRow[] = all.slice(0, 40).map((line, i) => ['add', line, null, i + 1])
  if (all.length > 40) rows.push(['gap', all.slice(40)])
  return { rows, add: all.length, del: 0 }
}

/* One hunk's rows, back into unified-diff lines: a `@@ ... @@` header per run
   the rows carry, then a prefixed line per row. A 'gap' row is dropped -- it
   marks lines the builder chose not to number (fromEdit's context beyond
   CTX_KEEP, fromWrite's rows past its cap), so there is nothing to restore
   there either, the same as a real diff taken at that width would show. A
   'hunk' row (only fromUnified emits one) carries the header text verbatim,
   read off the source diff rather than recomputed; a run with none -- every
   fromEdit or fromWrite hunk -- gets one synthesised from what it kept. */
function unifiedHunk(rows: DiffRow[]): string[] {
  const out: string[] = []
  let run: DiffRow[] = []
  let header: string | null = null
  const flush = (): void => {
    if (header == null && !run.length) return
    out.push(header ?? syntheticHeader(run))
    run.forEach((row) => out.push(bodyLine(row)))
    run = []
    header = null
  }
  rows.forEach((row) => {
    if (row[0] === 'gap') return
    if (row[0] === 'hunk') {
      flush()
      header = String(row[1])
      return
    }
    run.push(row)
  })
  flush()
  return out
}

function bodyLine(row: DiffRow): string {
  const text = String(row[1])
  if (row[0] === 'add') return `+${text}`
  if (row[0] === 'del') return `-${text}`
  return ` ${text}`
}

/* Without a stored header, the true old/new line numbers are known only
   where a row happens to carry one (fromWrite's rows do, fromEdit's never
   do): the run's first numbered row anchors the start, and a run with no
   numbers at all anchors at 1 -- or at 0 when that side has no lines, the
   same convention a brand-new file's diff uses. */
function syntheticHeader(run: DiffRow[]): string {
  const oldCount = run.filter((row) => row[0] === 'ctx' || row[0] === 'del').length
  const newCount = run.filter((row) => row[0] === 'ctx' || row[0] === 'add').length
  const firstOld = run.find((row) => typeof row[2] === 'number')
  const firstNew = run.find((row) => typeof row[3] === 'number')
  const oldStart = firstOld ? (firstOld[2] as number) : (oldCount ? 1 : 0)
  const newStart = firstNew ? (firstNew[3] as number) : (newCount ? 1 : 0)
  return `@@ -${oldStart},${oldCount} +${newStart},${newCount} @@`
}

/* The inverse of fromUnified/fromEdit/fromWrite: a change's hunks, rendered
   back into the raw patch text a real tool call would have produced. The
   desk's diff pane draws this text directly, the way the prototype's
   diffBody feeds codeBlock the tool's own patch rather than these structured
   rows. */
export function toUnified(path: string, hunkList: WsHunk[]): string {
  const lines = [`diff --git a/${path} b/${path}`]
  hunkList.forEach((hunk) => lines.push(...unifiedHunk(hunk.rows)))
  return lines.join('\n')
}

export function fromUnified(lines: string | string[]): WsHunk {
  const rows: DiffRow[] = []
  let add = 0
  let del = 0
  let oldLine: number | null = null
  let newLine: number | null = null
  const source = typeof lines === 'string' ? lines.split('\n') : lines
  source.filter((line) => !/^(---|\+\+\+)( |$)/.test(String(line))).forEach((raw) => {
    const line = String(raw)
    const header = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/)
    if (header) {
      oldLine = Number(header[1])
      newLine = Number(header[2])
      rows.push(['hunk', line])
      return
    }
    if (line.startsWith('@@')) {
      rows.push(['hunk', line])
      return
    }
    if (line.startsWith('+')) {
      rows.push(['add', line.slice(1), null, newLine == null ? null : newLine++])
      add += 1
      return
    }
    if (line.startsWith('-')) {
      rows.push(['del', line.slice(1), oldLine == null ? null : oldLine++, null])
      del += 1
      return
    }
    rows.push([
      'ctx', line.replace(/^ /, ''),
      oldLine == null ? null : oldLine++, newLine == null ? null : newLine++,
    ])
  })
  return { rows, add, del }
}
