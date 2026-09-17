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
 * why its opener sits on the composer's member of the island bag instead of a
 * member of its own -- the rack's six names are already there and this is the
 * seventh thing the layers do to that rack.
 *
 * What is here is each sheet's own element and the answers it can give; the
 * markup inside them is src/chrome/ApproveSheet.tsx (the preview variant) and
 * src/chrome/ApprovalSheet.tsx. The element belongs to this module because the
 * rack files it under a conversation and styles it as its flex item, and so does
 * the key handler, which lives as long as the request rather than as long as its
 * interior: a parked sheet is unmounted and still pending.
 *
 * "The conversation that asked" is a fact the caller has to carry in, not one
 * this module can read: see `owner` on :func:`open`.
 */

import { createElement } from 'react'

import { ApprovalSheet } from '../../chrome/ApprovalSheet'
import { ApproveSheet } from '../../chrome/ApproveSheet'
import { t } from '../../i18n/t'
import * as drafts from '../../state/sheetDrafts'
import { add as sheetAdd, dropClass, remove as sheetRemove, session } from '../../state/sheetRack'
import { composing } from './store'

import type { ApprovalControls } from '../../chrome/ApprovalSheet'
import type { SheetOptionRow } from '../../chrome/SheetRack'

/* The permission gate's approval, keyed so approval.closed can withdraw the
   exact request it retires (a timeout, a teardown, an answer from another
   surface) without touching a newer one. */
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
    if (!sheet.isConnected || composing(e)) return
    if (e.key === 'Escape') { e.preventDefault(); close(onDeny); return }
    const n = Number(e.key)
    if (n === 1 || n === 2) { e.preventDefault(); opts[n - 1]!.run() }
  }
  document.addEventListener('keydown', onKey, true)

  /* The withdrawal handed to the rack, not kept here: the rack sees every exit
     -- including the conversation being deleted, which never reaches this
     module -- and a second copy of who-owns-what could only disagree with it. */
  sheetAdd(sheet, key, withdraw, createElement(ApproveSheet, {
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


export interface ApprovalReq {
  approvalId: string
  command: string
  description: string
  /* The prefix rule the runtime found safe to offer for persisting; absent
     when there is none, and then the sheet offers no such choice. */
  suggestedPattern?: string
}

/* The permission gate's ask: allow once, allow for this session, allow and
   save a prefix rule (only when the runtime suggested one; the prefix is
   editable before it is sent), deny (the agent reads the refusal and goes
   on), or deny and stop the turn. A note typed before a refusal rides to the
   model as the reason. Same sheet clothes as open() above -- this variant
   differs in what an answer is, so it reports a choice string instead of
   calling one of two thunks. */
export function openApproval(
  req: ApprovalReq,
  onChoice: (choice: string, feedback: string, pattern?: string) => void,
  owner?: string,
): Approval {
  const key = owner || session()
  /* Read before the sweep, written on every keystroke: the note and the prefix
     the reader is editing have to outlive the elements they are typed into. */
  const draft = drafts.slot(key, req.approvalId)
  dropClass('csheet', key)

  const sheet = document.createElement('div')
  sheet.className = 'csheet perm'
  sheet.setAttribute('role', 'dialog')
  sheet.setAttribute('aria-modal', 'true')
  sheet.setAttribute('aria-label', t('gui.confirm.title'))

  /* The two fields, once the interior has mounted: what the model is told is
     what stands in them at the moment an answer is sent, so they are read then
     rather than mirrored here. */
  const ctl: ApprovalControls = { note: null, pattern: null }

  let answered = false
  const close = (choice?: string, pattern?: string): void => {
    if (answered) return
    answered = true
    openApprovals.delete(req.approvalId)
    document.removeEventListener('keydown', onKey, true)
    sheetRemove(sheet)
    /* The drafts go with the sheet, and only here: this runs on the exits that
       settle the request, never on the conversation switch they outlive. */
    drafts.forget(draft)
    if (choice) onChoice(choice, ctl.note ? ctl.note.value.trim() : '', pattern)
  }
  const withdraw = (): void => close()
  openApprovals.set(req.approvalId, withdraw)

  /* The persisted grant sits after the session one and before the refusals,
     so the two refusals keep the last two numbers whatever was suggested. Its
     prefix is an input the reader may edit; an emptied input saves nothing. */
  const saveRule = (): void => {
    const rule = ctl.pattern ? ctl.pattern.value.trim() : ''
    if (rule) close('allow_always', rule)
  }
  const opts: SheetOptionRow[] = [
    { label: t('gui.confirm.allow'), run: () => close('allow'), go: true },
    { label: t('gui.confirm.allow_session'), run: () => close('allow_session') },
    ...(req.suggestedPattern
      ? [{ label: t('gui.confirm.allow_always', { pattern: '' }), run: saveRule, rule: true }]
      : []),
    { label: t('gui.confirm.deny'), run: () => close('deny') },
    { label: t('gui.confirm.deny_stop'), run: () => close('deny_stop') },
  ]

  function onKey(e: KeyboardEvent): void {
    if (!sheet.isConnected || composing(e)) return
    if (e.key === 'Escape') { e.preventDefault(); close('deny'); return }
    /* Digits keep working while the note field is focused only when it is
       empty: a typed note starts with whatever the reader types, digits
       included. The prefix field is text from the first key. */
    if (ctl.pattern && document.activeElement === ctl.pattern) return
    if (ctl.note && document.activeElement === ctl.note && ctl.note.value) return
    const n = Number(e.key)
    if (n >= 1 && n <= opts.length) { e.preventDefault(); opts[n - 1]!.run() }
  }
  document.addEventListener('keydown', onKey, true)

  sheetAdd(sheet, key, withdraw, createElement(ApprovalSheet, {
    ctl,
    draft,
    command: req.command || '',
    words: {
      title: `${t('gui.confirm.title')} · ${req.description}`,
      deny: t('gui.confirm.deny'),
      notePh: t('gui.confirm.note_ph'),
      patternFor: t('gui.confirm.pattern_for'),
    },
    opts,
    suggested: req.suggestedPattern,
    onDeny: () => close('deny'),
    onSaveRule: saveRule,
  }))
  const first = sheet.querySelector<HTMLElement>('.opt')
  if (first && sheet.isConnected) first.focus()
  return { close: withdraw }
}

/* approval.closed: the server retired this request (timeout, teardown, or an
   answer from another surface). Nothing is sent back -- the question is over. */
export function closeApproval(approvalId: string): void {
  openApprovals.get(approvalId)?.()
}
