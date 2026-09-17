/* The clarify sheet: the agent asking the reader a question mid-turn.
 *
 * A tenant of the rack, beside the approval sheet and wearing the same clothes
 * -- `.csheet` above the composer, numbered options, a free-text row last. The
 * two are the same kind of interruption (the turn is blocked on the reader), so
 * they interrupt in the same place, and only one of them may be pending in a
 * conversation at a time. That is why the class sweep on the way in is by class
 * rather than by kind.
 *
 * What is here is the sheet's own element and the answers it can give; the
 * markup inside it is src/chrome/ClarifySheet.tsx. The element belongs to this
 * module because the rack files it under a conversation and styles it as its
 * flex item -- see that component -- and so do the key handler and the
 * ResizeObserver, which live as long as the question rather than as long as its
 * interior: a parked sheet is unmounted and still pending.
 *
 * The transport stays with the caller. This module raises the sheet and reports
 * one string back -- the chosen option, the typed answer, or the wording of a
 * skip -- and the live layer turns that into `clarify.respond` and marks the
 * step as an exchange. What the reader sees is this module's; what the server
 * hears is not.
 */

import { createElement } from 'react'

import { ClarifySheet } from '../../chrome/ClarifySheet'
import { t } from '../../shell/bridge'
import * as drafts from '../../state/sheetDrafts'
import { add as sheetAdd, dropClass, remove as sheetRemove, session } from '../../state/sheetRack'
import { composing, dockLift } from './store'

import type { ClarifyControls } from '../../chrome/ClarifySheet'

export interface ClarifyRequest {
  question?: string
  choices?: string[]
  /* The server's handle for this question, which is what a later
     `clarify.closed` names. Optional only for a frame that predates the
     field -- such a sheet simply cannot be closed from the server. */
  request_id?: string
  /* The conversation the server is asking on behalf of, which is not always the
     one on screen: a question can arrive for a turn the reader stepped away
     from. Its own answer beats "wherever the reader happens to be", and the
     fallback is only for a frame that predates the field. */
  conversation_id?: string
}

/* The questions on screen, by the id a `clarify.closed` names. Several at once
   rather than one: `open` sweeps the class only inside the conversation the
   sheet is filed under, so a question raised for a conversation the reader
   stepped away from stands beside the one they are looking at -- which is the
   case this close exists for, since a sub-agent asks after the turn that
   spawned it has already replied. */
const live = new Map<string, HTMLElement>()

/* Take the sheet down because the server says the question can no longer be
   answered -- it timed out, its turn was interrupted, or it was superseded.
   Reports nothing back: the server already fell back to the question's default,
   and answering now would be a second answer to a settled question.

   Matched on the request id, because that fail-safe can land after the next
   question is already up. */
export function close(requestId: string): void {
  const sheet = live.get(requestId)
  if (sheet) sheetRemove(sheet)
}

export function open(req: ClarifyRequest, answered: (text: string) => void): void {
  const owner = req.conversation_id || session()
  const id = req.request_id
  /* Read before the sweep, written on every keystroke: the answer being typed
     has to outlive the element it is typed into. */
  const draft = drafts.slot(owner, id)
  dropClass('csheet', owner)

  const sheet = document.createElement('div')
  sheet.className = 'csheet'
  /* This one asks: the turn is waiting on the answer. The rack passes that on to
     whatever else is docked -- see `watchAsking` -- so a running graph steps
     aside instead of pushing the question below the fold. */
  sheet.dataset.asks = '1'
  sheet.setAttribute('role', 'dialog')
  sheet.setAttribute('aria-label', t('gui.clarify.aria'))
  /* Set here as well as by the component, and in this order on purpose: the rack
     writes `data-sess` the moment it is handed the element, so leaving the fold
     state to the first render would order the sheet's attributes differently
     than the imperative builder did. */
  sheet.dataset.fold = 'false'

  const choices = req.choices || []
  const done = (text: string): void => {
    answered(text)
    /* Through the rack, so the takedown registered below runs whichever way
       this sheet leaves -- answered here, replaced by the next question, or
       dropped with its conversation. */
    sheetRemove(sheet)
  }
  const skip = (): void => done(t('gui.clarify.skipped_msg'))

  /* Read once, when the question arrives: a language flip rewrites the page's
     own markup, and it never re-worded a sheet already on screen. */
  const words = {
    fold: t('gui.clarify.fold'),
    unfold: t('gui.clarify.unfold'),
    skip: t('gui.clarify.skip'),
    skipAria: t('gui.clarify.skip_aria'),
    placeholder: t(choices.length ? 'gui.clarify.other_ph' : 'gui.clarify.ph'),
    submit: t('gui.clarify.submit'),
  }
  const ctl: ClarifyControls = { input: null, setFold: null }

  /* Number keys pick an option while the focus is outside the field. */
  const onKey = (e: KeyboardEvent): void => {
    /* Parked with another conversation, this sheet is still on the document's
       keydown: the rack detaches the element rather than destroying it, so the
       reader comes back to the same question. Only the mounted one may be
       answered by number, or "1" typed here would answer another
       conversation. */
    if (!sheet.isConnected || composing(e)) return
    if (ctl.input && document.activeElement === ctl.input) return
    const n = Number(e.key)
    if (n >= 1 && n <= choices.length) { e.preventDefault(); done(choices[n - 1] as string) }
    if (n === choices.length + 1) { e.preventDefault(); ctl.setFold?.(false); ctl.input?.focus() }
  }
  document.addEventListener('keydown', onKey, true)

  /* The sheet is absolutely positioned, so growing it does not change the
     dock's own height and the dock's ResizeObserver never fires -- dockLift has
     to be called by hand here. It reads the sheet out of the DOM, so folding or
     resizing only needs to re-measure. */
  const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(() => dockLift()) : null

  sheetAdd(sheet, owner, () => {
    document.removeEventListener('keydown', onKey, true)
    if (ro) ro.disconnect()
    /* The draft goes with the sheet, and only here: this runs on the exits that
       settle the question, never on the conversation switch the draft outlives. */
    drafts.forget(draft)
    /* Identity-checked: a question re-asked under the id of one still on
       screen replaces this entry, and by the time this takedown runs the entry
       is the sheet that replaced it. */
    if (id && live.get(id) === sheet) live.delete(id)
  }, createElement(ClarifySheet, {
    host: sheet, ctl, draft, question: req.question || '', choices, words, done, skip,
  }))
  if (id) live.set(id, sheet)
  if (ro) ro.observe(sheet)
  /* Only the question on screen takes the caret. The guard reads as intent
     rather than as the mechanism: measured in Chromium, `focus()` on an input
     inside a detached subtree leaves `document.activeElement` where it was, so
     the DOM already declines. Kept because a reader of this line should not
     have to know that, and because the rack may one day mount a parked sheet
     somewhere off-screen rather than not at all. No test asserts it -- one
     would pass with the guard deleted. */
  if (sheet.isConnected) ctl.input?.focus()
}
