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
 * markup inside it is features/composer/ClarifySheet.tsx. The element belongs to this
 * module because the rack files it under a conversation and styles it as its
 * flex item -- see that component -- and so do the key handler and the
 * ResizeObserver, which live as long as the question rather than as long as its
 * interior: a parked sheet is unmounted and still pending.
 *
 * A frame may carry a whole batch of questions rather than one. Where every
 * question from this one on arrives with its own options, the sheet is a form
 * the reader steps through -- back as well as forward, answered in one report
 * at the end -- and where it does not, it is the single question it always was.
 * Which of the two it is is decided here, once, because a frame that predates
 * the batch fields and a producer that sends only the question text both have
 * to keep working: see `steps` below.
 *
 * The transport stays with the caller. This module raises the sheet and reports
 * one string per question back -- the chosen option, the typed answer, or the
 * wording of a skip, and an empty string for a position answered before this
 * frame -- and the live layer turns that into `clarify.respond` and marks the
 * step as an exchange. What the reader sees is this module's; what the server
 * hears is not.
 */

import { createElement } from 'react'

import { t } from '../../i18n/t'
import * as drafts from '../../state/sheetDrafts'
import { add as sheetAdd, dropClass, remove as sheetRemove, session } from '../../state/sheetRack'
import { ClarifySheet } from './ClarifySheet'
import { composing, dockLift } from './store'

import type { ClarifyBatchEntry } from '../../rpc/notifications'
import type { ClarifyControls, ClarifyStep } from './ClarifySheet'

export interface ClarifyRequest {
  question?: string
  choices?: string[]
  /* What this question is called in the batch's progress row, the option the
     tool would pick itself, and whether the reader may choose more than one. */
  header?: string
  recommended?: string
  multi_select?: boolean
  /* Where this question sits in the batch the tool asked in one call, and the
     batch itself. A producer that asks one question at a time sends neither. */
  index?: number
  total?: number
  batch?: ClarifyBatchEntry[]
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

/* A field has the caret, so what is being pressed is being typed rather than
   aimed at this sheet. Any field, not only this sheet's own row: the composer
   sits directly below the sheet and an arrow key answering the form would move
   the caret out of a half-written message. */
const typing = (el: Element | null): boolean => {
  if (!el) return false
  const tag = el.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || (el as HTMLElement).isContentEditable === true
}

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

export function open(req: ClarifyRequest, answered: (answers: string[]) => void): void {
  const owner = req.conversation_id || session()
  const id = req.request_id
  /* Read before the sweep, written on every keystroke: the answer being typed
     has to outlive the element it is typed into. */
  const draft = drafts.slot(owner, id)
  /* Every pending ask stays -- the gate's and the confirm preview's alike: the
     question docks above and hands the keyboard back when it is answered. A
     landed line carries no `data-asks` and goes. */
  dropClass('csheet', owner, (el) => el.classList.contains('perm') && el.dataset.asks === '1')

  const sheet = document.createElement('div')
  /* The rack's clothes plus this sheet's own: the stepper's vocabulary is the
     composer's, so its rules hang off `.cp-ask` in features/composer/styles.css
     rather than off the rack's class in styles/page.css. */
  sheet.className = 'csheet cp-ask'
  /* This one asks: the turn is waiting on the answer. The sweeps read the mark
     to know which sheets a new question may replace (state/sheetRack.ts). */
  sheet.dataset.asks = '1'
  sheet.setAttribute('role', 'dialog')
  sheet.setAttribute('aria-label', t('gui.clarify.aria'))
  /* Set here as well as by the component, and in this order on purpose: the rack
     writes `data-sess` the moment it is handed the element, so leaving the fold
     state to the first render would order the sheet's attributes differently
     than the imperative builder did. */
  sheet.dataset.fold = 'false'

  const batch = req.batch || []
  const offset = req.index ?? 0
  /* The frame's own fields describe the question at `index`, and they are what
     the step is built from: the batch entry at that position says the same
     thing, and only the top level is guaranteed to carry it. */
  const asked: ClarifyStep = {
    question: req.question || '',
    header: req.header,
    choices: req.choices || [],
    recommended: req.recommended,
    multi: !!req.multi_select,
  }
  const rest = batch.slice(offset)
  /* A form only where every question left in the batch brought its options
     with it. A producer that sends the question text alone (the elicitor) is
     answered one question at a time, the way it always was -- the reader still
     sees how many are coming, because the progress row is drawn from the batch
     either way. */
  const ahead = batch.length > 1 && rest.length > 0 && rest.every((e) => Array.isArray(e.choices))
  const steps: ClarifyStep[] = ahead
    ? rest.map((e, i) => (i === 0 ? asked : {
      question: e.question,
      header: e.header,
      choices: e.choices || [],
      recommended: e.recommended,
      multi: !!e.multi_select,
    }))
    : [asked]

  /* One string per position the report covers: the questions answered before
     this frame are empty, and the caller drops them. */
  const done = (all: string[]): void => {
    answered(all)
    /* Through the rack, so the takedown registered below runs whichever way
       this sheet leaves -- answered here, replaced by the next question, or
       dropped with its conversation. */
    sheetRemove(sheet)
  }

  /* Read once, when the question arrives: a language flip rewrites the page's
     own markup, and it never re-worded a sheet already on screen. Both
     placeholders, and one aria label per batch position, because which of them
     is on screen changes as the reader steps through. */
  const words = {
    fold: t('gui.clarify.fold'),
    unfold: t('gui.clarify.unfold'),
    skip: t('gui.clarify.skip'),
    skipAria: t('gui.clarify.skip_aria'),
    skipped: t('gui.clarify.skipped_msg'),
    otherPh: t('gui.clarify.other_ph'),
    answerPh: t('gui.clarify.ph'),
    submit: t('gui.clarify.submit'),
    back: t('gui.clarify.back'),
    next: t('gui.clarify.next'),
    recommended: t('gui.clarify.recommended'),
    multiHint: t('gui.clarify.multi_hint'),
    stepAria: batch.map((_, i) => t('gui.clarify.step_aria', { n: i + 1, m: batch.length })),
  }
  const ctl: ClarifyControls = { input: null, move: null, pick: null, setFold: null }

  /* Number keys pick an option and the arrows walk the batch, while the focus
     is outside any field. What each does is the interior's to decide -- it is
     the half that knows which step is on screen -- so the handler here asks it
     and only claims the key when it was taken. */
  const onKey = (e: KeyboardEvent): void => {
    /* Parked with another conversation, this sheet is still on the document's
       keydown: the rack detaches the element rather than destroying it, so the
       reader comes back to the same question. Only the mounted one may be
       answered by number, or "1" typed here would answer another
       conversation. */
    if (!sheet.isConnected || composing(e)) return
    if (typing(document.activeElement)) return
    const n = Number(e.key)
    if (e.key.length === 1 && n >= 1 && ctl.pick?.(n)) { e.preventDefault(); return }
    if (e.key === 'ArrowLeft' && ctl.move?.(-1)) e.preventDefault()
    if (e.key === 'ArrowRight' && ctl.move?.(1)) e.preventDefault()
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
  }, createElement(ClarifySheet, { host: sheet, ctl, draft, steps, batch, offset, words, done }))
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
