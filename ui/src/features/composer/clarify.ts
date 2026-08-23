/* The clarify sheet: the agent asking the reader a question mid-turn.
 *
 * A tenant of the rack, beside the approval sheet and wearing the same clothes
 * -- `.csheet` above the composer, numbered options, a free-text row last. The
 * two are the same kind of interruption (the turn is blocked on the reader), so
 * they interrupt in the same place, and only one of them may be pending in a
 * conversation at a time. That is why the class sweep on the way in is by class
 * rather than by kind.
 *
 * The transport stays with the caller. This module raises the sheet and reports
 * one string back -- the chosen option, the typed answer, or the wording of a
 * skip -- and the live layer turns that into `clarify.respond` and marks the
 * step as an exchange. What the reader sees is this module's; what the server
 * hears is not.
 */

import { t } from '../../shell/bridge'
import { CHEVRON_DOWN, CROSS, ico } from '../../shell/ico'
import { add as sheetAdd, dropClass, remove as sheetRemove, session } from './sheets'
import { composing, dockLift } from './store'

export interface ClarifyRequest {
  question?: string
  choices?: string[]
  /* The conversation the server is asking on behalf of, which is not always the
     one on screen: a question can arrive for a turn the reader stepped away
     from. Its own answer beats "wherever the reader happens to be", and the
     fallback is only for a frame that predates the field. */
  conversation_id?: string
}

const el = <K extends keyof HTMLElementTagNameMap>(
  tag: K, cls?: string, text?: string,
): HTMLElementTagNameMap[K] => {
  const n = document.createElement(tag)
  if (cls) n.className = cls
  if (text != null) n.textContent = text
  return n
}

export function open(req: ClarifyRequest, answered: (text: string) => void): void {
  const owner = req.conversation_id || session()
  dropClass('csheet', owner)

  const sheet = el('div', 'csheet')
  sheet.setAttribute('role', 'dialog')
  sheet.setAttribute('aria-label', t('gui.clarify.aria'))

  const done = (text: string): void => {
    answered(text)
    /* Through the rack, so the takedown registered below runs whichever way
       this sheet leaves -- answered here, replaced by the next question, or
       dropped with its conversation. */
    sheetRemove(sheet)
  }
  const skip = (): void => done(t('gui.clarify.skipped_msg'))

  const head = el('div', 'hd')
  const q = el('div', 'q', req.question || '')
  const fold = el('button', 'ic tipdn')
  fold.appendChild(ico(CHEVRON_DOWN, 'cv'))
  const setFold = (v: boolean): void => {
    sheet.dataset.fold = String(v)
    const lb = t(v ? 'gui.clarify.unfold' : 'gui.clarify.fold')
    fold.dataset.tip = lb
    fold.setAttribute('aria-label', lb)
  }
  fold.onclick = () => setFold(sheet.dataset.fold !== 'true')
  setFold(false)
  q.onclick = () => { if (sheet.dataset.fold === 'true') setFold(false) }
  const x = el('button', 'ic tipdn')
  x.appendChild(ico(CROSS))
  x.dataset.tip = t('gui.clarify.skip')
  x.setAttribute('aria-label', t('gui.clarify.skip_aria'))
  x.onclick = skip
  head.append(q, fold, x)
  sheet.appendChild(head)

  const body = el('div', 'body')
  const choices = req.choices || []
  choices.forEach((c, i) => {
    const b = el('button', 'opt')
    b.append(el('span', 'n', String(i + 1)), el('span', undefined, c))
    b.onclick = () => done(c)
    body.appendChild(b)
  })

  const other = el('div', 'other')
  other.appendChild(el('span', 'n', String(choices.length + 1)))
  const inp = el('input')
  inp.placeholder = t(choices.length ? 'gui.clarify.other_ph' : 'gui.clarify.ph')
  other.appendChild(inp)
  body.appendChild(other)
  sheet.appendChild(body)

  const foot = el('div', 'foot')
  const skipBtn = el('button', 'btn', t('gui.clarify.skip'))
  skipBtn.onclick = skip
  const submit = el('button', 'btn key', t('gui.clarify.submit'))
  submit.disabled = true
  submit.onclick = () => { if (inp.value.trim()) done(inp.value.trim()) }
  foot.append(skipBtn, submit)
  sheet.appendChild(foot)

  inp.oninput = () => { submit.disabled = !inp.value.trim() }
  inp.onkeydown = (e) => {
    /* Stopped here so the page's own shortcuts do not read what is being typed
       into this field. */
    e.stopPropagation()
    if (composing(e)) return
    if (e.key === 'Enter' && inp.value.trim()) done(inp.value.trim())
  }

  /* Number keys pick an option while the focus is outside the field. */
  const onKey = (e: KeyboardEvent): void => {
    /* Parked with another conversation, this sheet is still on the document's
       keydown: the rack detaches the element rather than destroying it, so a
       half-typed answer survives a switch. Only the mounted one may be answered
       by number, or "1" typed here would answer another conversation. */
    if (!sheet.isConnected || document.activeElement === inp || composing(e)) return
    const n = Number(e.key)
    if (n >= 1 && n <= choices.length) { e.preventDefault(); done(choices[n - 1] as string) }
    if (n === choices.length + 1) { e.preventDefault(); setFold(false); inp.focus() }
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
  })
  if (ro) ro.observe(sheet)
  /* Only the question on screen takes the caret. The guard reads as intent
     rather than as the mechanism: measured in Chromium, `focus()` on an input
     inside a detached subtree leaves `document.activeElement` where it was, so
     the DOM already declines. Kept because a reader of this line should not
     have to know that, and because the rack may one day mount a parked sheet
     somewhere off-screen rather than not at all. No test asserts it -- one
     would pass with the guard deleted. */
  if (sheet.isConnected) inp.focus()
}
