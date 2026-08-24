import { ds, shell, t } from '../../shell/bridge'
import * as attachmentCache from '../../shell/attachment-cache'
import { formatDuration } from '../../shell/duration'
import { current as currentSession } from '../../shell/session'
import { show as toast } from '../../shell/toast'
import { note as transcriptNote } from '../transcript/mount'
import * as tail from '../transcript/tail'
import * as turn from './turn'

import type { Attachment, ComposerSource, SlashCmd } from './types'
import type { Shell } from '../../shell/bridge'

/* Plain external store, same shape as the other islands: the dock is driven
 * imperatively by the legacy page (the turn machine advances phase, the queue
 * drains into `send`, a session switch resets everything), so the state lives
 * here where the shims can reach it and the views subscribe.
 *
 * `use` remains demo fixture state. The live phase and the queue are composer
 * state: every path that changes, parks, or restores them goes through this
 * island, so their ownership and rendering cannot diverge.
 */

export interface ComposerState {
  atts: Attachment[]
  queue: string[]
  /* The queue row being edited, by index, or null. The text itself stays in
     the uncontrolled input until it is committed, exactly as before: a store
     write per keystroke would repaint the row the reader is typing in. */
  editing: number | null
  slashOpen: boolean
  slashRows: SlashCmd[]
  slashSel: number
  /* The live turn row: shown while the page is busy, its clock anchored at
     `liveT0` and ticking through `tick`. */
  live: boolean
  tick: number
  /* Bumped by every paint the legacy shims ask for. The views read the page's
     own arrays through the source, so one counter is the whole subscription. */
  v: number
}

const initial: ComposerState = {
  atts: [], queue: [], editing: null, slashOpen: false, slashRows: [], slashSel: 0,
  live: false, tick: 0, v: 0,
}

let state: ComposerState = { ...initial }
const listeners = new Set<() => void>()

export const getState = (): ComposerState => state

export function subscribe(l: () => void): () => void {
  listeners.add(l)
  return () => listeners.delete(l)
}

function set(p: Partial<ComposerState>): void {
  state = { ...state, ...p, v: state.v + 1 }
  for (const l of listeners) l()
}

export const source = (): ComposerSource => ds<ComposerSource>('composer')

function verb<K extends keyof Shell>(name: K): NonNullable<Shell[K]> {
  const v = shell()[name]
  if (!v) throw new Error(`RavenShell.${String(name)} is not wired`)
  return v as NonNullable<Shell[K]>
}

const el = <T extends HTMLElement>(id: string): T | null => document.getElementById(id) as T | null

export const durText = formatDuration

/* ── the field ────────────────────────────────────────────────────────── */

export const field = (): HTMLTextAreaElement | null => el<HTMLTextAreaElement>('ta')

const DRAFT_KEY = 'raven.gui.drafts'
const DRAFT_MAX = 30
let draftOwner: string | null = null
let draftTick: ReturnType<typeof setTimeout> | null = null

interface Draft {
  t: string
  at: number
}

type Drafts = Record<string, Draft>

function draftsRead(): Drafts {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(DRAFT_KEY) || 'null')
    return value && typeof value === 'object' ? value as Drafts : {}
  } catch {
    return {}
  }
}

function draftsWrite(all: Drafts): void {
  const keys = Object.keys(all)
  if (keys.length > DRAFT_MAX) {
    keys.sort((a, b) => (all[a]?.at || 0) - (all[b]?.at || 0))
      .slice(0, keys.length - DRAFT_MAX)
      .forEach((key) => delete all[key])
  }
  try {
    localStorage.setItem(DRAFT_KEY, JSON.stringify(all))
  } catch {
    /* Storage may be unavailable in private mode or over quota. */
  }
}

export function parkDraft(): void {
  const key = draftOwner || currentSession() || 'new'
  const text = field()?.value || ''
  const all = draftsRead()
  if (text.trim()) all[key] = { t: text, at: Date.now() }
  else delete all[key]
  draftsWrite(all)
}

export function loadDraft(id: string | null): void {
  draftOwner = id || 'new'
  const ta = field()
  if (!ta) return
  ta.value = draftsRead()[draftOwner]?.t || ''
  fitField()
  goPaint()
}

export function dropDraft(id: string | null): void {
  const all = draftsRead()
  delete all[id || 'new']
  draftsWrite(all)
}

export function claimDraft(id: string | null): void {
  if (draftOwner === 'new') draftOwner = id || currentSession() || 'new'
}

export function touchDraft(): void {
  if (draftTick) clearTimeout(draftTick)
  draftTick = setTimeout(parkDraft, 250)
}

export function dropOwnedDraft(): void {
  if (draftTick) clearTimeout(draftTick)
  draftTick = null
  dropDraft(draftOwner)
}

/* A file on its own is a message -- "look at this" is what dropping it already
   said -- so an empty field with something attached must still be sendable. */
export const hasAtts = (): boolean => state.atts.length > 0

const ICON_SEND = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
  + ' stroke-width="2.4" aria-hidden="true"><path d="M5 12h13M12 5l7 7-7 7"/></svg>'
const ICON_STOP = '<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
  + '<rect x="5" y="5" width="14" height="14" rx="2.5"/></svg>'

/* The send/stop button. Written imperatively rather than rendered: it is one
   static element in page.html that half the page reaches by id, and the whole
   of its state is four attributes. */
export function goPaint(): void {
  const b = el<HTMLButtonElement>('go')
  if (!b) return
  if (turn.busy() && turn.cancellable()) {
    b.disabled = false
    b.classList.add('halt')
    b.innerHTML = ICON_STOP
    b.setAttribute('aria-label', t('gui.stop'))
    return
  }
  const ta = field()
  b.disabled = !(ta && ta.value.trim()) && !hasAtts()
  b.classList.remove('halt')
  b.innerHTML = ICON_SEND
  b.setAttribute('aria-label', t('gui.send'))
}

/* A textarea cannot size itself to its content, so the height is set here --
   but the ceiling stays in CSS and is read back, so there is one cap to tune
   and it can be viewport-relative. */
export function fitField(): void {
  const e = field()
  if (!e) return
  e.style.height = 'auto'
  e.style.height = Math.min(parseFloat(getComputedStyle(e).maxHeight) || 168, e.scrollHeight) + 'px'
  dockLift()
}

/* Everything docked at the bottom moves: the composer grows with its content,
   queued rows and a clarify sheet stack above it. The pill hangs off .chat, a
   different positioned ancestor, so keeping them apart takes one measured
   number -- the distance from the chat column's bottom edge up to the top of
   the highest docked thing -- published for the CSS to offset against. */
export function dockLift(): void {
  const chat = document.querySelector<HTMLElement>('.chat')
  const dock = document.querySelector<HTMLElement>('.dock')
  if (!chat || !dock) return
  /* Measured against what the reader SEES, not the dock's box: the dock carries
     padding and an empty hint strip under the field, and offsetting against
     those parks the pill a finger's width above the composer for no reason. */
  let top = dock.getBoundingClientRect().bottom
  /* Scoped to the dock on purpose: a clarify sheet lives in the transcript, so
     a global match would anchor the pill to wherever that sheet has scrolled.
     The card itself is included: its border and padding are visual height too,
     and measuring only its children parked the pill 6px into the glass. */
  dock.querySelectorAll('.dock-in, .dock-in > *, .sheets > *').forEach((n) => {
    const r = n.getBoundingClientRect()
    if (r.height > 0) top = Math.min(top, r.top)
  })
  /* Written only on a real change: this also runs per scroll event, and a style
     write there would invalidate layout on every frame of a flick. */
  const v = Math.round(chat.getBoundingClientRect().bottom - top) + 'px'
  if (chat.style.getPropertyValue('--lift') !== v) chat.style.setProperty('--lift', v)
}

/* ── the back-to-bottom pill ──────────────────────────────────────────── */

const scroller = (): HTMLElement | null => el('scroll')

/* The pill only appears when the reader has left the tail. */
export function pillPaint(): void {
  const sc = scroller()
  const pill = el('backpill')
  if (!sc || !pill) return
  const away = sc.scrollHeight - sc.scrollTop - sc.clientHeight > 120
  const stuck = tail.isStuck()
  pill.hidden = stuck || !away
  if (pill.hidden) return
  /* The offset must be current the moment it appears, not from whenever the
     dock last changed shape. */
  dockLift()
  pill.dataset.tip = t('gui.pill.bottom')
  pill.setAttribute('aria-label', t('gui.pill.bottom'))
}

export function pillClick(): void {
  tail.setStuck(true)
  tail.down()
  pillPaint()
}

export function scrolled(): void {
  const sc = scroller()
  if (sc && sc.scrollHeight - sc.scrollTop - sc.clientHeight < 4) tail.setStuck(true)
  pillPaint()
}

export function wheeled(up: boolean): void {
  if (!up) return
  tail.setStuck(false)
  pillPaint()
}

/* ── the queue ────────────────────────────────────────────────────────── */

export const queue = (): string[] => state.queue

export function queuePush(text: string): void {
  set({ queue: [...state.queue, text], editing: null })
}

export function queueShift(): string | undefined {
  if (!state.queue.length) return undefined
  const [first, ...rest] = state.queue
  set({ queue: rest, editing: null })
  return first
}

export function queueClear(): void {
  set({ queue: [], editing: null })
}

export const queueSnapshot = (): string[] => [...state.queue]

export function queueRestore(items: string[]): void {
  set({ queue: [...items], editing: null })
}

export function drawQueue(): void {
  set({ editing: null })
}

export function editRow(i: number): void {
  set({ editing: i })
}

export function commitRow(i: number, text: string): void {
  const next = [...state.queue]
  if (text.trim()) next[i] = text.trim()
  set({ queue: next, editing: null })
}

export function cancelRow(): void {
  set({ editing: null })
}

export function removeRow(i: number): void {
  set({ queue: state.queue.filter((_, n) => n !== i), editing: null })
}

/* ── the meter and the live turn row ──────────────────────────────────── */

/* The strip under the field, plus the two things that follow from a turn
   being alive. The wording is the source's: the demo canvas reports the canned
   run's usage there, live mode leaves it empty and says it in the row instead. */
export function drawMeter(): void {
  const m = el('meter')
  if (m) m.textContent = source().meter()
  drawTurnLive()
  pillPaint()
}

/* ONE row for the whole life of a turn: it appears the moment the message is
   sent, rides the tail of the transcript under whatever is streaming, and gives
   way to the answer's own footer when the turn lands. One glyph, one clock --
   the clock is what proves the stream is alive, since an animation on its own
   keeps dancing over a dead socket. */
let liveT0 = 0
let liveTick: ReturnType<typeof setInterval> | null = null

/* The clock's anchor, read and restored by the parked-turn machinery: a turn
   ten minutes in must not read "2s" after a round trip through another
   session. */
export const liveAnchor = (): number => liveT0
export function setLiveAnchor(ms: number): void {
  liveT0 = ms || 0
}

export const liveMs = (): number => (liveT0 ? Date.now() - liveT0 : 0)

export function drawTurnLive(): void {
  if (!turn.busy()) {
    if (liveTick) {
      clearInterval(liveTick)
      liveTick = null
    }
    liveT0 = 0
    if (state.live) set({ live: false })
    return
  }
  if (!liveT0) liveT0 = Date.now()
  if (!state.live) set({ live: true })
  else set({ tick: state.tick + 1 })
  if (liveTick) return
  liveTick = setInterval(() => {
    if (!turn.busy()) {
      drawTurnLive()
      return
    }
    set({ tick: state.tick + 1 })
  }, 250)
}

/* ── the attachment tray ──────────────────────────────────────────────── */

export const fmtSize = (n: number): string => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB`
  : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`)

/* The tray's own container is the root React draws into, so its hidden flag is
   set here rather than rendered. */
function trayPaint(atts: Attachment[]): void {
  const box = el('atts')
  if (box) box.hidden = !atts.length
  set({ atts })
}

export function removeAtt(i: number): void {
  const atts = state.atts.slice()
  atts.splice(i, 1)
  trayPaint(atts)
  goPaint()
}

/* How many staged files are still on their way up. A message must not leave
   carrying a path the server has not written yet. */
export const attsPending = (): number => state.atts.filter((a) => a.uploading).length

/* Hand the staged paths to whoever is sending, and clear the tray: the message
   itself is the record of what was handed over from here on. */
export function takeAtts(): string[] {
  const paths = state.atts.map((a) => String(a.path || '')).filter(Boolean)
  trayPaint([])
  goPaint()
  return paths
}

const failDetail = (e: unknown): string => {
  const o = e as { data?: { detail?: string }; message?: string }
  return (o && o.data && o.data.detail) || (o && o.message) || String(e)
}

/* Whether files can be staged at all. False on the demo canvas, which has
   nowhere to put the bytes -- so it does not offer a drop target either. */
export function canAttach(): boolean {
  try {
    return !!source().upload
  } catch {
    return false
  }
}

/* Files are uploaded into <workspace>/uploads and handed to the agent as
   paths: every file tool is already workspace-scoped, so a path is all it
   needs. Bytes never ride inside the message. */
export function addFiles(files: ArrayLike<File>): void {
  const up = source().upload
  if (!up) return
  Array.from(files).forEach((file) => {
    const entry: Attachment = { name: file.name, size: file.size, uploading: true, path: null, url: null }
    trayPaint(state.atts.concat([entry]))
    goPaint()
    const drop = (): void => {
      const rest = state.atts.filter((a) => a !== entry)
      trayPaint(rest)
      goPaint()
    }
    const reader = new FileReader()
    reader.onload = () => {
      const dataUrl = String(reader.result)
      const b64 = dataUrl.split(',')[1] || ''
      /* Keep the bytes for display only: an image renders as itself in the
         composer, and once uploaded, keyed by path, in the sent bubble. */
      if (/^image\//.test(file.type || '')) entry.url = dataUrl
      up({ name: file.name, content_b64: b64 })
        .then((r) => {
          entry.path = r.path
          entry.size = r.size
          entry.uploading = false
          if (entry.url) attachmentCache.set(r.path, entry.url)
          trayPaint(state.atts.slice())
          goPaint()
        })
        .catch((e: unknown) => {
          drop()
          transcriptNote(t('gui.att.fail', { name: file.name }), failDetail(e))
        })
    }
    reader.onerror = drop
    reader.readAsDataURL(file)
  })
}

/* ── the slash palette ────────────────────────────────────────────────── */

export const slashCmd = (x: SlashCmd): string => '/' + verb('slashName')(x.id)
export const slashDesc = (x: SlashCmd): string => verb('slashHelp')(x.id)

function popOpen(on: boolean): void {
  const pop = el('slashPop')
  if (pop) pop.dataset.open = String(on)
}

export const slashIsOpen = (): boolean => state.slashOpen

export function drawSlash(term: string): void {
  const q = term.slice(1).toLowerCase()
  const rows = source().slash.filter((x) => (!x.when || x.when())
    && (!q || x.id.includes(q) || verb('slashName')(x.id).toLowerCase().includes(q)
      || slashDesc(x).toLowerCase().includes(q)))
  if (!rows.length) {
    closeSlash()
    return
  }
  popOpen(true)
  set({ slashRows: rows, slashSel: 0, slashOpen: true })
}

export function closeSlash(): void {
  popOpen(false)
  if (state.slashOpen || state.slashRows.length) set({ slashOpen: false, slashRows: [] })
}

export function moveSlash(d: number): void {
  const n = state.slashRows.length
  if (!n) return
  set({ slashSel: (state.slashSel + d + n) % n })
}

export function runSlash(x: SlashCmd | undefined): void {
  if (!x) return
  closeSlash()
  const ta = field()
  if (ta) ta.value = ''
  fitField()
  goPaint()
  dropOwnedDraft()
  x.fn()
}

export const slashSelected = (): SlashCmd | undefined => state.slashRows[state.slashSel]

/* ── sending ──────────────────────────────────────────────────────────── */

export function fireSend(): void {
  const ta = field()
  const v = ta ? ta.value.trim() : ''
  if (!v && !hasAtts()) return
  /* The tray is this island's, and so is what becomes of a staged file when the
     message leaves: the note is what the reader's own bubble renders from and
     what survives into session history. The page layer used to do this by
     reaching back in here (RavenIslands.composer.attsPending / takeAtts), which
     was the only direction available while `send` was a shell verb. */
  const pending = attsPending()
  if (pending) {
    /* Before the field is cleared, which is a change: the page layer ran this
       same check AFTER this function had already emptied the textarea, so a
       send refused for a still-uploading file took the typed message with it. */
    transcriptNote(t('gui.att.pending'), t('gui.att.pending_body', { n: pending }))
    return
  }
  let text = v
  const staged = takeAtts()
  if (staged.length) {
    const list = staged.map((p) => `- ${p}`).join('\n')
    /* Handing over a file with nothing typed is a message in itself, so the note
       leads on its own rather than trailing a blank line -- which is what this
       plain concatenation gives, because `v` is already trimmed above. The line
       this replaces branched on `text.trim()` and picked between two strings
       that are equal for every input that can reach here; it made sense while
       the page layer received the raw field value, and stopped when the island
       started trimming before the hand-off. */
    const note = `${t('gui.att.note')}\n${list}`
    text = `${text}\n\n${note}`
  }
  if (ta) ta.value = ''
  fitField()
  dropOwnedDraft()
  source().send(text)
  goPaint()
}

export function goClick(): void {
  if (turn.busy() && turn.cancellable()) source().stop()
  /* A busy turn that cannot be cancelled is still a live turn: the Send
     action stays functional and QUEUES the message, exactly what Enter does
     and what the button says it does. An inert click that neither sends nor
     queues would be a lie the label makes. */
  else fireSend()
}

/* Whether the file picker has anywhere to put the bytes. The demo canvas says
   so rather than opening a picker whose upload would go nowhere. */
export function pickFiles(open: () => void): void {
  if (source().upload) {
    open()
    return
  }
  const hint = source().pickHint
  if (hint) toast(hint)
}

/* ── the field's own keyboard and input ───────────────────────────────── */

/* An IME sends its keystrokes as keydown too, so while a composition is open
   Enter belongs to the input method: it commits the candidate being typed
   (letters included, which is how CJK users type Latin). keyCode 229 is the
   older spelling some IMEs still send instead of isComposing. */
export const composing = (e: { isComposing?: boolean; keyCode?: number }): boolean =>
  !!(e.isComposing || e.keyCode === 229)

export function parkDraftNow(): void {
  parkDraft()
}

export function fieldInput(): void {
  fitField()
  goPaint()
  const ta = field()
  const v = ta ? ta.value : ''
  if (v.startsWith('/') && !v.includes(' ')) drawSlash(v)
  else closeSlash()
  touchDraft()
}

export function fieldKeydown(e: KeyboardEvent): void {
  /* the whole handler, not just Enter: arrows and Tab drive the candidate list
     while an IME is composing */
  if (composing(e)) return
  if (state.slashOpen) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      moveSlash(1)
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      moveSlash(-1)
      return
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault()
      runSlash(slashSelected())
      return
    }
    /* stopPropagation: the document handler also owns Escape, and dismissing
       the menu must not fall through to "interrupt the running turn" */
    if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      closeSlash()
      return
    }
  }
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    fireSend()
  }
}

/* Test seam only: module state survives between tests. */
export function _resetForTests(): void {
  if (liveTick) clearInterval(liveTick)
  if (draftTick) clearTimeout(draftTick)
  liveTick = null
  liveT0 = 0
  draftOwner = null
  draftTick = null
  state = { ...initial }
  listeners.clear()
}
