/* What the workspace panel is a record OF: one row per path the agent
 * rewrote, one row per page it read, and the turn each of them belongs to.
 *
 * Fed from three places that all hand over the same thing -- a tool call and
 * its arguments -- so the rules live once: the live pipeline's `tool.start`
 * and `tool.complete` stages (src/state/session/stages.ts), and the replay of
 * a stored conversation (`wsOnHistory`), which is the same input read back off
 * disk. The arguments ARE the ground truth of an edit: `edit_file` carries
 * old_text and new_text, so the diff needs no backend support at all.
 *
 * The panel chrome around the record -- which view is up, the header badge,
 * the redraw -- is the page's (src/state/ws.ts), not this module's.
 */

import { t } from '../../i18n/t'
import * as hunks from '../../lib/hunks'
import { pane } from '../../state/wsPane'
import { shortPath } from './source'
import { shared as workspaceShared } from './store'

import type { WsChange, WsHunk, WsShared } from './types'

const record = (): WsShared => workspaceShared()

/* The two front ends hand over different shapes -- the live RPC gives the
   whole argument object, a replayed call gives the one string it displays.
   Normalising here keeps every caller downstream simple. */
export function wsArgs(name: string, args: unknown): Record<string, unknown> {
  if (args && typeof args === 'object') return args as Record<string, unknown>
  const s = String(args == null ? '' : args)
  if (name === 'exec') return { command: s }
  if (name === 'web_fetch') return { url: s }
  if (name === 'web_search') return { query: s }
  if (name === 'spawn') return { label: s }
  return { path: s }
}

/* One row per path, not per call: five edits to the same file is one changed
   file with five hunks, which is how a person thinks about it. */
export function wsRecordChange(path: string, kind: string, hunk: WsHunk): WsChange {
  const WS = record()
  const key = String(path)
  let c = WS.changes.find((x) => x.key === key && x.turn === WS.turn)
  if (!c) {
    const shown = shortPath(key)
    const cut = shown.lastIndexOf('/')
    /* The newest change is the one you came here to read, so it arrives
       expanded. `auto` marks it as opened by us, so the next arrival folds it
       back without touching a row the reader opened on purpose. */
    WS.changes.forEach((x) => { if (x.auto) { x.open = false; x.auto = false } })
    c = { key, dir: cut < 0 ? '' : shown.slice(0, cut + 1), name: cut < 0 ? shown : shown.slice(cut + 1),
      kind, add: 0, del: 0, hunks: [], turn: WS.turn, open: true, auto: true, seen: false }
    WS.changes.unshift(c)
  }
  if (kind === 'write') c.kind = 'write'
  c.add += hunk.add; c.del += hunk.del
  c.hunks.push(hunk)
  return c
}

/* ── tool-event hooks ──────────────────────────────────────────────────
   Fed the FULL argument object, because that is where the diff lives. */
export function wsOnTool(name: string, args: unknown, _silent?: boolean): void {
  const WS = record()
  const a = wsArgs(name, args)
  const path = (a.path || a.file_path || '') as string
  let hit: WsChange | null = null
  if (name === 'edit_file' && path) {
    hit = wsRecordChange(path, 'edit', hunks.fromEdit(a.old_text as string, a.new_text as string))
  } else if (name === 'write_file' && path) {
    hit = wsRecordChange(path, 'write', hunks.fromWrite(a.content as string))
  } else if (name === 'web_fetch' && a.url) {
    WS.urls.unshift({ url: String(a.url), kind: 'fetch', at: t('gui.sess.just_now') })
  } else if (name === 'web_search' && a.query) {
    WS.urls.unshift({ url: String(a.query), kind: 'search', at: t('gui.sess.just_now') })
  } else return

  const shown = pane().view()
  if (hit && shown.open && shown.tab === 'diff') hit.flash = true
  /* Draw before counting: the Changes view marks rows seen as it renders, so
     counting first would flash a badge that the very next line clears. */
  if (pane().showsTurn()) pane().draw()
  pane().bump()
}

export function wsOnToolDone(
  name: string, args: unknown, _ok?: boolean, _preview?: string, _ms?: number | null, diff?: string,
): void {
  const WS = record()
  const a = wsArgs(name, args)
  /* The tool's own diff is the ground truth -- for a whole-file write it is the
     only record of what was replaced, which the arguments cannot show. It
     replaces the hunk guessed at tool.start. */
  if (diff && diff.length && /^(edit_file|write_file)$/.test(name)) {
    const path = (a.path || a.file_path || '') as string
    const c = WS.changes.find((x) => x.key === path && x.turn === WS.turn)
    if (c) {
      const h = hunks.fromUnified(diff)
      const stale = c.hunks.pop()
      if (stale) { c.add -= stale.add; c.del -= stale.del }
      c.hunks.push(h); c.add += h.add; c.del += h.del
    }
  }
  if (pane().showsTurn()) pane().draw()
  pane().bump()
}

/* ── resumed sessions ──────────────────────────────────────────────────
   The panel is rebuilt from the stored calls rather than starting empty after a
   reload: session.resume carries every assistant tool_call WITH its arguments,
   which is the same input the live hooks are fed, so replaying them yields the
   same rows, the same diffs and the same turn grouping. */
interface StoredMessage {
  role?: string
  text?: string
  delegated?: unknown
  mid_turn?: boolean
  tool_call_id?: string | number
  diff?: string
  tool_calls?: Array<{ id?: string | number; name?: string; arguments?: string }>
}

export function wsOnHistory(messages: StoredMessage[] | null | undefined): void {
  const WS = record()
  /* The stored tool entries carry the real unified diff when the tool reported
     one; keyed here so each replayed call can swap its argument-guessed hunk
     for the numbered rows, exactly as the live completion event does. */
  const diffs = new Map<string, string>()
  ;(messages || []).forEach((m) => {
    if (m && m.role === 'tool' && m.tool_call_id && m.diff) diffs.set(String(m.tool_call_id), m.diff)
  })
  ;(messages || []).forEach((m) => {
    if (!m) return
    if (m.role === 'user' && m.delegated) {
      /* A delegated result re-entering counts as one turn here too -- a live
         client advances on turn.started, and without the same step on replay
         a reloaded session files the delegated reaction's files under its
         parent's turn. Mirrors the rule in features/transcript/store.ts. An
         origin-only entry (cron, sentinel) does NOT: it opens no workspace
         turn there either. */
      WS.turn += 1; return
    }
    /* A mid-turn message joined the turn that was running; it opens none of its
       own, and a live client advances no workspace turn for it either. Same rule
       as in features/transcript/store.ts, over the same messages. */
    if (m.role === 'user' && !m.mid_turn && m.text && m.text.trim()) { WS.turn += 1; return }
    if (m.role !== 'assistant' || !Array.isArray(m.tool_calls)) return
    m.tool_calls.forEach((c) => {
      let args: unknown = null
      try { args = JSON.parse(c.arguments || '{}') } catch { return }
      if (!args || typeof args !== 'object') return
      const name = String(c.name || '')
      wsOnTool(name, args, true)
      const diff = diffs.get(String(c.id || ''))
      if (diff) wsOnToolDone(name, args, true, '', null, diff)
    })
  })
  /* Restored rows have no completion event coming, and nothing counts as
     unread because none of it arrived while the reader was away. */
  WS.urls.forEach((u) => { u.at = t('gui.ws.turn_earlier') })
  WS.changes.forEach((c) => { c.seen = true })
  WS.unseen = 0
  pane().bump()
}
