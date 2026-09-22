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

import type { FileChange, FileRemoval } from '../../rpc/generated'
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

/* This turn's row for one path, made if the turn has not touched it yet. */
function rowFor(key: string, kind: WsChange['kind']): WsChange {
  const WS = record()
  const found = WS.changes.find((x) => x.key === key && x.turn === WS.turn)
  if (found) return found
  const shown = shortPath(key)
  const cut = shown.lastIndexOf('/')
  /* The newest change is the one you came here to read, so it arrives
     expanded. `auto` marks it as opened by us, so the next arrival folds it
     back without touching a row the reader opened on purpose. */
  WS.changes.forEach((x) => { if (x.auto) { x.open = false; x.auto = false } })
  const made: WsChange = {
    key, dir: cut < 0 ? '' : shown.slice(0, cut + 1), name: cut < 0 ? shown : shown.slice(cut + 1),
    kind, add: 0, del: 0, hunks: [], turn: WS.turn, open: true, auto: true, seen: false,
  }
  WS.changes.unshift(made)
  return made
}

/* One row per path, not per call: five edits to the same file is one changed
   file with five hunks, which is how a person thinks about it. */
export function wsRecordChange(path: string, kind: WsChange['kind'], hunk: WsHunk): WsChange {
  const c = rowFor(String(path), kind)
  /* A creation stays one for the rest of the turn: rewriting a file the turn
     itself made does not turn it into a file that was already there. */
  if (kind === 'write' && c.kind === 'edit') c.kind = 'write'
  c.add += hunk.add; c.del += hunk.del
  c.hunks.push(hunk)
  return c
}

/* Following the file's contents through the turn's own calls, so a removal
   that arrives without them can still say what was lost. A whole-file write is
   the file; an append adds to what was followed and is lost when nothing was.
   An edit is applied to the followed text, and one that does not fit it -- the
   tool matched loosely, or the file was never written whole this turn -- ends
   the following: a body that might be wrong is worse than none. */
function followWrite(c: WsChange, content: string, append: boolean): void {
  c.body = append ? (c.body == null ? null : c.body + content) : content
}

function followEdit(c: WsChange, oldText: string, newText: string): void {
  c.body = c.body != null && c.body.includes(oldText) ? c.body.replace(oldText, newText) : null
}

/* Two spellings of one file. A change row is keyed by the path as the model
   typed it, and `write_file` takes a relative one; a removal arrives under the
   path the runtime resolved. Equality alone would leave the turn showing a row
   for the file it created and a second one saying that file went. */
function sameFile(rowKey: string, removed: string): boolean {
  return rowKey === removed || rowKey.endsWith('/' + removed) || removed.endsWith('/' + rowKey)
}

/* A file that is gone is not a change to its contents, so the row is rebuilt
   rather than added to: nothing added, every line it held deleted.

   `before` is the contents the runtime captured as the file went, absent when
   it could not (too large, not text, or nothing had read it) -- and then the
   text this turn's own calls left in the file is the next best account of what
   was lost. `lines` is the stored count a replay carries in place of any
   contents at all.

   A file the same turn created leaves no row: created and removed inside one
   turn is the nothing git shows for it too. */
function wsRecordRemoval(path: string, before?: string, lines?: number | null): void {
  const WS = record()
  const key = String(path)
  const at = WS.changes.findIndex((x) => x.turn === WS.turn && sameFile(x.key, key))
  const had = at < 0 ? null : WS.changes[at]!
  if (had && had.kind === 'add') { WS.changes.splice(at, 1); return }
  const text = before !== undefined ? before : (had && had.body != null ? had.body : null)
  const hunk = text == null ? null : hunks.fromDelete(text)
  const c = had || rowFor(key, 'delete')
  c.kind = 'delete'
  c.add = 0
  /* An empty file leaves a hunk with no rows to draw; the stored count says
     the same nothing and keeps a replay reading the same as the live row. */
  c.hunks = hunk && hunk.rows.length ? [hunk] : []
  c.del = c.hunks.length ? c.hunks[0]!.del : (lines == null ? 0 : lines)
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
    followEdit(hit, String(a.old_text ?? ''), String(a.new_text ?? ''))
  } else if (name === 'write_file' && path) {
    hit = wsRecordChange(path, 'write', hunks.fromWrite(a.content as string))
    followWrite(hit, String(a.content == null ? '' : a.content), a.mode === 'append')
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

/* A whole-file write onto nothing is a creation, and the tool reports it twice:
   `file_change` leaves `before` out when there was no file to replace (an empty
   string means the file was there and empty), and the unified diff it carries
   opens its first hunk at line zero of the old side. The payload settles it
   alone wherever it reaches, because the header cannot: a write over a file
   that existed and was empty diffs against no old lines and opens at zero too.
   The header is read only where the payload never reaches -- a reloaded
   conversation stores the diff and nothing else. */
function createdTheFile(fileChange: FileChange | undefined, diff: string | undefined): boolean {
  if (fileChange) return fileChange.before === undefined
  return /^@@ -0,0 /m.test(diff || '')
}

export function wsOnToolDone(
  name: string, args: unknown, _ok?: boolean, _preview?: string, _ms?: number | null, diff?: string,
  fileChange?: FileChange, fileRemoved?: FileRemoval[],
): void {
  const WS = record()
  const a = wsArgs(name, args)
  if (/^(edit_file|write_file)$/.test(name)) {
    const path = (a.path || a.file_path || '') as string
    const c = WS.changes.find((x) => x.key === path && x.turn === WS.turn)
    /* The tool's own diff is the ground truth -- for a whole-file write it is
       the only record of what was replaced, which the arguments cannot show. It
       replaces the hunk guessed at tool.start. */
    if (c && diff && diff.length) {
      const h = hunks.fromUnified(diff)
      const stale = c.hunks.pop()
      if (stale) { c.add -= stale.add; c.del -= stale.del }
      c.hunks.push(h); c.add += h.add; c.del += h.del
    }
    if (c && c.kind === 'write' && createdTheFile(fileChange, diff)) c.kind = 'add'
  }
  /* Outside the write/edit branch: a file goes when whatever call made it go
     returns, and that is an `exec` far more often than a file tool. */
  ;(fileRemoved || []).forEach((r) => { if (r && r.path) wsRecordRemoval(String(r.path), r.before) })
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
  file_removed?: Array<{ path?: string; del?: number }>
  tool_calls?: Array<{ id?: string | number; name?: string; arguments?: string }>
}

export function wsOnHistory(messages: StoredMessage[] | null | undefined): void {
  const WS = record()
  /* The stored tool entries carry the real unified diff when the tool reported
     one; keyed here so each replayed call can swap its argument-guessed hunk
     for the numbered rows, exactly as the live completion event does. */
  const diffs = new Map<string, string>()
  /* The stored removals carry a line count and no contents -- the runtime does
     not keep a deleted file's text on disk -- so a replayed deletion draws the
     row and the count, and the hunk only when this turn's own write is still
     on the row to rebuild it from. */
  const gone = new Map<string, Array<{ path?: string; del?: number }>>()
  ;(messages || []).forEach((m) => {
    if (!m || m.role !== 'tool' || !m.tool_call_id) return
    if (m.diff) diffs.set(String(m.tool_call_id), m.diff)
    if (Array.isArray(m.file_removed)) gone.set(String(m.tool_call_id), m.file_removed)
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
      ;(gone.get(String(c.id || '')) || []).forEach((r) => {
        if (r && r.path) wsRecordRemoval(String(r.path), undefined, r.del == null ? null : Number(r.del))
      })
    })
  })
  /* Restored rows have no completion event coming, and nothing counts as
     unread because none of it arrived while the reader was away. */
  WS.urls.forEach((u) => { u.at = t('gui.ws.turn_earlier') })
  WS.changes.forEach((c) => { c.seen = true })
  WS.unseen = 0
  pane().bump()
}
