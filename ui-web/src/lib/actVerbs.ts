/* Tool-call vocabulary shared by every renderer that draws a raw tool name as
 * something a reader can read: a verb (done or still in flight), a folded
 * "did N of these" phrase, an MCP server split, the first error-shaped line
 * of a result, and a clamped one-liner. Pure -- no store, no `ds()` -- so a
 * sibling domain can import it directly rather than reaching across features/
 * (CONTRIBUTING 1.2). Carried here from features/transcript/store.ts, which
 * still holds `actLabel` because that one needs the workspace's own path
 * shortener.
 */

import { t } from '../i18n/t'

const MCP_RE = /^mcp_([^_]+)_(.+)$/

export interface McpSplit { srv: string | null; bare: string }

/* `mcp_<server>_<tool>` is the name an MCP call actually carries on the wire;
   every other name is already bare. */
export function splitMcp(name: string): McpSplit {
  const m = MCP_RE.exec(name || '')
  return m ? { srv: m[1] as string, bare: m[2] as string } : { srv: null, bare: String(name || '') }
}

export const rawVerb = (n: string): string => splitMcp(n).bare.split('_').join(' ')

export const verbOf = (n: string): string => t('gui.act.v.' + n, undefined, rawVerb(n))
export const verbIngOf = (n: string): string => t('gui.act.ing.' + n, undefined, rawVerb(n))

/* Verbs and counts only -- the folded line answers "what kind of work". Takes
   anything shaped like a call rather than the transcript's own `CallData`, so
   a caller with a lighter record does not have to fake the rest of it. */
export function phraseOf(calls: Array<{ name: string }>): string {
  const n = new Map<string, number>()
  calls.forEach((c) => n.set(c.name, (n.get(c.name) || 0) + 1))
  return [...n].map(([name, k]) => (k === 1
    ? verbOf(name)
    : t('gui.act.n.' + name, { n: k }, `${verbOf(name)} ×${k}`))).join(' · ')
}

export const shortArg = (a: unknown, max = 40): string => {
  if (!a) return ''
  const one = String(a).replace(/\s+/g, ' ').trim()
  return one.length > max ? one.slice(0, max - 1) + '…' : one
}

/* The line shown on a failed row: the first error-shaped line if any. */
export const firstErrLine = (res: unknown, cap?: number): string => {
  const lines = String(res || '').split('\n').filter((x) => x.trim())
  const hit = lines.find((x) => /error|failed|traceback|could not|denied|exception/i.test(x))
  const l = hit || lines[0]
  return l ? shortArg(l.replace(/^\s*\[|\]\s*$/g, ''), cap || 62) : ''
}

/* The file a call names, wherever it named it -- schema violations included. */
export function argPath(a: Record<string, unknown>): string {
  for (const k of ['path', 'file_path', 'filename', 'file', 'target']) {
    if (a && typeof a[k] === 'string' && a[k]) return a[k] as string
  }
  return ''
}
