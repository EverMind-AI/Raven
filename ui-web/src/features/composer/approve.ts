/* The approval sheet.
 *
 * An approval request is the same kind of interruption as ask_user -- the turn
 * is blocked on the reader -- so it wears the same clothes: the sheet above the
 * composer, numbered options, Esc or the close button means no. A centred modal
 * made the two read as different classes of event and put the answer somewhere
 * the reader was not already looking.
 *
 * Beside the rack rather than in shell/, because it is a tenant of it: one sheet
 * appended to #sheetRack, filed under the conversation that asked. That is also
 * why its opener is the composer's rather than a domain of its own -- the
 * rack's six names are already the composer's, and this is the seventh thing
 * the page does to that rack.
 *
 * What is here is each sheet's own element and the answers it can give; the
 * markup inside them is features/composer/AskApproveSheet.tsx (the preview variant) and
 * features/composer/GateSheet.tsx. The element belongs to this module because the
 * rack files it under a conversation and styles it as its flex item, and so does
 * the key handler, which lives as long as the request rather than as long as its
 * interior: a parked sheet is unmounted and still pending.
 *
 * "The conversation that asked" is a fact the caller has to carry in, not one
 * this module can read: see `owner` on :func:`open`.
 */

import { createElement } from 'react'

import { t } from '../../i18n/t'
import { add as sheetAdd, dropClass, remove as sheetRemove, session } from '../../state/sheetRack'
import { AskApproveSheet } from './AskApproveSheet'
import { GateSheet, LandedSheet } from './GateSheet'
import { composing } from './store'

import type { SheetOptionRow } from '../../chrome/SheetRack'
import type { Evidence, GateWords, LandedProps, LandedWords } from './GateSheet'

/* The permission gate's approval, keyed so approval.closed can withdraw the
   exact request it retires (a teardown, an answer from another surface)
   without touching a newer one. */
const openApprovals = new Map<string, () => void>()

export interface Approval {
  /* Takes the sheet down without answering. For a caller that has learned the
     question is moot -- the turn was cancelled, the session closed. */
  close(): void
}

export function open(
  prompt: string, onAllow?: () => void, onDeny?: () => void, owner?: string,
): Approval {
  /* The conversation this request belongs to, read once and passed to all three
     of the calls that are scoped by it. Reading it again later would be a way
     for them to disagree -- the reader can switch conversations between any two
     lines of an async page.

     `owner` is the conversation the request was raised in, which the caller
     learns from the frame that raised it (state/session/pipeline.ts). It is not always
     the open one: a turn the reader stepped away from can block on an approval
     at any moment, and filing that under whatever is on screen puts the question
     over a conversation it does not belong to -- while the conversation that
     asked shows nothing pending and stays paused on the server.

     The fallback is for a caller with no conversation to name: the design canvas
     preview, and a frame from a dispatch the server could not attribute. */
  const key = owner || session()

  /* One question at a time in THIS conversation: a new request replaces the
     pending one rather than stacking a second sheet the reader has to answer
     twice. One sweep does it: every sheet of this kind in this bucket goes,
     including a pending approval and a clarify question, which wears `.csheet`
     too -- and each goes down through the rack's teardown, so the one that was
     holding a key handler unregisters it on the way out. */
  dropClass('csheet', key)

  const sheet = document.createElement('div')
  sheet.className = 'csheet perm'
  /* This one asks: the reader cannot get on until they answer it. The rack
     passes that on to whatever else is docked -- see `watchAsking`. */
  sheet.dataset.asks = '1'
  sheet.setAttribute('role', 'dialog')
  sheet.setAttribute('aria-modal', 'true')
  sheet.setAttribute('aria-label', t('gui.confirm.title'))

  let answered = false
  const close = (fn?: () => void): void => {
    if (answered) return
    answered = true
    document.removeEventListener('keydown', onKey, true)
    sheetRemove(sheet)
    if (fn) fn()
  }
  /* Answering nothing, which is what a withdrawal is: the question stopped
     mattering, so neither side is told. */
  const withdraw = (): void => close()

  const opts: SheetOptionRow[] = [
    { label: t('gui.confirm.allow'), run: () => close(onAllow), go: true },
    { label: t('gui.confirm.deny'), run: () => close(onDeny) },
  ]

  function onKey(e: KeyboardEvent): void {
    /* A sheet parked with another conversation is still listening: the handler
       is on the document, and the rack detaches the element rather than
       destroying it so the reader comes back to the same question. Only the
       mounted one may be answered from the keyboard, or "1" typed here would
       allow something another conversation asked. */
    if (!sheet.isConnected || composing(e) || !topmost(sheet)) return
    if (e.key === 'Escape') { e.preventDefault(); close(onDeny); return }
    if (typing(e)) return
    const n = Number(e.key)
    if (n === 1 || n === 2) { e.preventDefault(); opts[n - 1]!.run() }
  }
  document.addEventListener('keydown', onKey, true)

  /* The withdrawal handed to the rack, not kept here: the rack sees every exit
     -- including the conversation being deleted, which never reaches this
     module -- and a second copy of who-owns-what could only disagree with it. */
  sheetAdd(sheet, key, withdraw, createElement(AskApproveSheet, {
    title: t('gui.confirm.title'),
    deny: t('gui.confirm.deny'),
    prompt: prompt || '',
    opts,
    onDeny: () => close(onDeny),
  }))
  const first = sheet.querySelector<HTMLElement>('.opt')
  if (first && sheet.isConnected) first.focus()
  return { close: withdraw }
}


export interface ApprovalOrigin {
  kind: string
  name: string
}

/** raven/rpc/approval_broker.py's request, as the sheet reads it. */
export interface ApprovalReq {
  approvalId: string
  command: string
  description: string
  /* The prefix rule the runtime found safe to offer for persisting; absent
     when there is none, and then the sheet offers no such choice. */
  suggestedPattern?: string
  /* The prompt's view, as the engine sent it: the layout (`shell.exec`,
     `file.write`, `mcp.call`, `unknown`), the shell command family that words
     it, who is asking, and the tool's own account of the call. */
  kind?: string
  family?: string
  origin?: ApprovalOrigin
  evidence?: Evidence
}

export interface ApprovalHandlers {
  /** The answer: allow, allow_always (with the rule to save), or deny. Resolving
      false means the engine did not take it -- the request had gone, or the
      connection dropped -- and the landed line says so. */
  onChoice: (choice: string, feedback: string, pattern?: string) => void | Promise<boolean>
  /** Takes back the rule `allow_always` saved; resolves to whether it was there. */
  onRevoke?: (pattern: string) => Promise<boolean>
  /** A sentence typed after a refusal, sent on as the reader's next message. */
  onNote?: (text: string) => void
}

/* How long a landed sheet stays: long enough to read, and for a saved rule or
   an invited note long enough to act on. */
export const LANDED_MS = 4000
export const LINGER_MS = 12000

/* The timers taking landed sheets down, so a test can clear what it started. */
const landings = new Set<ReturnType<typeof setTimeout>>()

const str = (v: unknown): string => (typeof v === 'string' ? v : '')

/* A key pressed into a field is text, not an answer: the composer sits under
   every sheet, and a message that starts with a digit must not allow a command
   -- or, worse, save a rule. Escape is not guarded: leaving a field and refusing
   the question is what it has always done. */
const typing = (e: KeyboardEvent): boolean => {
  const el = e.target as HTMLElement | null
  return !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)
}

/* The newest sheet owns the keyboard: a question docked above this one takes
   the digits until it is answered, or "1" would answer both. */
const topmost = (sheet: HTMLElement): boolean =>
  !sheet.parentElement || sheet.parentElement.firstElementChild === sheet

/* The words a request is asked in. The family names the sentence when the
   engine sent one; a bare shell command, a file write, an MCP call and a tool
   the page has no layout for each have a sentence of their own. */
function wordsFor(req: ApprovalReq): GateWords {
  const kind = req.kind || 'unknown'
  const ev = req.evidence || {}
  const path = str(ev.path)
  const vars = {
    who: req.origin?.kind === 'subagent' && req.origin.name ? req.origin.name : 'Raven',
    cwd: str(ev.cwd) || str(ev.machine),
    path,
    name: path.slice(path.lastIndexOf('/') + 1) || path,
    server: str(ev.server),
    tool: str(ev.tool),
  }
  const slot = kind === 'shell.exec' ? (req.family || 'shell')
    : kind === 'file.write' ? 'file_write'
      : kind === 'mcp.call' ? 'mcp_call' : 'unknown'
  return {
    title: t('gui.confirm.title.' + slot, vars, t('gui.confirm.title.unknown', vars)),
    why: t('gui.confirm.why.' + slot, vars, t('gui.confirm.why.unknown', vars)),
    deny: t('gui.confirm.deny'),
    created: t('gui.confirm.ev.created'),
    nodiff: t('gui.confirm.ev.nodiff'),
  }
}

/* The permission gate's ask: deny (the agent reads the refusal and goes on),
   allow and save the rule the runtime suggested (only when it did), or allow
   once. Deny is the default and takes the focus, so an accidental Enter never
   grants. The answer is sent the moment it is chosen; what follows is the
   landed sheet, where a saved rule can be taken back and a refusal can carry a
   sentence -- sent on as the reader's next message, since the refusal itself
   has already reached the model. */
export function openApproval(req: ApprovalReq, handlers: ApprovalHandlers, owner?: string): Approval {
  /* One sheet per request: a replay after a reload may name a request that is
     already on screen, and a second sheet for it would be answered twice. */
  const already = openApprovals.get(req.approvalId)
  if (already) return { close: already }
  const key = owner || session()
  /* This sweep takes a pending clarify question down, while clarify's spares a
     pending approval. The asymmetry is the deadline: a question ends on its own
     after ten minutes (raven/rpc/question_broker.py), an approval waits for the
     person. Two of this conversation's own approvals cannot meet here -- the
     broker sends the second only once the first is answered. */
  dropClass('csheet', key)

  const words = wordsFor(req)
  const sheet = document.createElement('div')
  sheet.className = 'csheet perm'
  sheet.dataset.asks = '1'
  sheet.setAttribute('role', 'dialog')
  sheet.setAttribute('aria-modal', 'true')
  sheet.setAttribute('aria-label', words.title)

  let answered = false
  const leave = (): void => {
    answered = true
    openApprovals.delete(req.approvalId)
    document.removeEventListener('keydown', onKey, true)
    sheetRemove(sheet)
  }
  const withdraw = (): void => {
    if (!answered) leave()
  }
  const answer = (choice: string, pattern?: string): void => {
    if (answered) return
    leave()
    land(key, choice, pattern, handlers, handlers.onChoice(choice, '', pattern))
  }
  openApprovals.set(req.approvalId, withdraw)

  const opts: SheetOptionRow[] = [
    { label: t('gui.confirm.deny'), run: () => answer('deny'), go: true },
    ...(req.suggestedPattern
      ? [{
        label: t('gui.confirm.always', { pattern: req.suggestedPattern }),
        run: () => answer('allow_always', req.suggestedPattern),
      }]
      : []),
    { label: t('gui.confirm.allow'), run: () => answer('allow') },
  ]

  function onKey(e: KeyboardEvent): void {
    if (!sheet.isConnected || composing(e) || !topmost(sheet)) return
    if (e.key === 'Escape') { e.preventDefault(); answer('deny'); return }
    if (typing(e)) return
    const n = Number(e.key)
    if (n >= 1 && n <= opts.length) { e.preventDefault(); opts[n - 1]!.run() }
  }
  document.addEventListener('keydown', onKey, true)

  sheetAdd(sheet, key, withdraw, createElement(GateSheet, {
    kind: req.kind || 'unknown',
    evidence: req.evidence || {},
    command: req.command || '',
    words,
    opts,
    onDeny: () => answer('deny'),
  }))
  const first = sheet.querySelector<HTMLElement>('.opt')
  if (first && sheet.isConnected) first.focus()
  return { close: withdraw }
}

/* The sheet an answer leaves behind. Docked as a sheet of its own rather than
   the asking one re-dressed, so the rack's count of who is asking drops the
   moment the answer is given, and a new request's sweep takes it down. */
function land(
  key: string, choice: string, pattern: string | undefined, handlers: ApprovalHandlers,
  sent: void | Promise<boolean>,
): void {
  const el = document.createElement('div')
  el.className = 'csheet perm'
  let timer: ReturnType<typeof setTimeout> | null = null
  const drop = (): void => {
    if (timer) {
      clearTimeout(timer)
      landings.delete(timer)
    }
    timer = null
  }
  const gone = (): void => {
    drop()
    sheetRemove(el)
  }
  const stay = (ms: number): void => {
    drop()
    timer = setTimeout(gone, ms)
    landings.add(timer)
  }
  const show = (words: LandedWords, more: Omit<LandedProps, 'words'> = {}): void =>
    sheetAdd(el, key, drop, createElement(LandedSheet, { words, ...more }))
  /* Landed on the reader's click, corrected if the engine never took the
     answer: the request had gone, or the socket dropped mid-call. The turn is
     then still waiting, which the line has to say rather than "allowed". */
  const undelivered = (): void => {
    show({ text: t('gui.confirm.land.unsent') })
    stay(LINGER_MS)
  }
  void Promise.resolve(sent).then((ok) => { if (ok === false) undelivered() }, undelivered)

  if (choice === 'allow_always' && pattern) {
    const onUndo = handlers.onRevoke
      ? (): void => {
        void handlers.onRevoke!(pattern).then((ok) => {
          show({ text: t(ok ? 'gui.confirm.land.revoked' : 'gui.confirm.land.revoke_failed') })
          stay(LANDED_MS)
        })
      }
      : undefined
    show({ text: t('gui.confirm.land.always', { pattern }), undo: t('gui.confirm.land.revoke') }, { onUndo })
    stay(LINGER_MS)
    return
  }
  if (choice === 'deny') {
    const onNote = handlers.onNote
      ? (text: string): void => {
        handlers.onNote!(text)
        show({ text: t('gui.confirm.land.deny_note', { text }) })
        stay(LANDED_MS)
      }
      : undefined
    show({ text: t('gui.confirm.land.deny'), notePh: t('gui.confirm.note_ph') }, { onNote })
    stay(onNote ? LINGER_MS : LANDED_MS)
    return
  }
  show({ text: t('gui.confirm.land.once') })
  stay(LANDED_MS)
}

/* approval.closed: the server retired this request (a teardown, or an answer
   from another surface). Nothing is sent back -- the question is over. A
   request already answered here has left this map, so its landed sheet stays. */
export function closeApproval(approvalId: string): void {
  openApprovals.get(approvalId)?.()
}

/* Test seam only: the landing timers outlive a test file's DOM. */
export function _resetForTests(): void {
  landings.forEach((timer) => clearTimeout(timer))
  landings.clear()
  openApprovals.clear()
}
