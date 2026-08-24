/* Builds the diff rows shared by workspace records and transcript call cards. */

import type { DiffRow, WsHunk } from './types'

const CTX_KEEP = 3

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
