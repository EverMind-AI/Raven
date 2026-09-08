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
 * why its opener is published on the composer's bag instead of as a global of
 * its own -- the rack's six names are already there and this is the seventh
 * thing the layers do to that rack.
 *
 * "The conversation that asked" is a fact the caller has to carry in, not one
 * this module can read: see `owner` on :func:`open`.
 */

import { t } from '../../shell/bridge'
import { CROSS, ico } from '../../shell/ico'
import { add as sheetAdd, dropClass, remove as sheetRemove, session } from './sheets'
import { composing } from './store'

export interface Approval {
  /* Takes the sheet down without answering. For a caller that has learned the
     question is moot -- the turn was cancelled, the session closed. */
  close(): void
}

const el = <K extends keyof HTMLElementTagNameMap>(
  tag: K, cls?: string, text?: string,
): HTMLElementTagNameMap[K] => {
  const n = document.createElement(tag)
  if (cls) n.className = cls
  if (text != null) n.textContent = text
  return n
}

export function open(
  prompt: string, onAllow?: () => void, onDeny?: () => void, owner?: string,
): Approval {
  /* The conversation this request belongs to, read once and passed to all three
     of the calls that are scoped by it. Reading it again later would be a way
     for them to disagree -- the reader can switch conversations between any two
     lines of an async page.

     `owner` is the conversation the request was raised in, which the caller
     learns from the frame that raised it (live/070-notify.js). It is not always
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

  const sheet = el('div', 'csheet perm')
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

  const head = el('div', 'hd')
  const x = el('button', 'ic tipdn')
  x.appendChild(ico(CROSS))
  x.dataset.tip = t('gui.confirm.deny')
  x.setAttribute('aria-label', t('gui.confirm.deny'))
  x.onclick = () => close(onDeny)
  head.append(el('div', 'q', t('gui.confirm.title')), x)
  sheet.appendChild(head)

  /* The request itself is the agent's own words about what it wants to do, so
     it is quoted rather than restated. */
  const body = el('div', 'body')
  body.appendChild(el('div', 'what', prompt || ''))
  const opts: Array<[string, () => void]> = [
    [t('gui.confirm.allow'), () => close(onAllow)],
    [t('gui.confirm.deny'), () => close(onDeny)],
  ]
  opts.forEach(([label, fn], i) => {
    const b = el('button', 'opt' + (i === 0 ? ' go' : ''))
    b.append(el('span', 'n', String(i + 1)), el('span', undefined, label))
    b.onclick = fn
    body.appendChild(b)
  })
  sheet.appendChild(body)

  function onKey(e: KeyboardEvent): void {
    /* A sheet parked with another conversation is still listening: the handler
       is on the document, and the rack detaches the element rather than
       destroying it so a half-typed answer survives a switch. Only the mounted
       one may be answered from the keyboard, or "1" typed here would allow
       something another conversation asked. */
    if (!sheet.isConnected || composing(e)) return
    if (e.key === 'Escape') { e.preventDefault(); close(onDeny); return }
    const n = Number(e.key)
    if (n === 1 || n === 2) { e.preventDefault(); opts[n - 1]![1]() }
  }
  document.addEventListener('keydown', onKey, true)

  /* The withdrawal handed to the rack, not kept here: the rack sees every exit
     -- including the conversation being deleted, which never reaches this
     module -- and a second copy of who-owns-what could only disagree with it. */
  sheetAdd(sheet, key, withdraw)
  const first = sheet.querySelector<HTMLElement>('.opt')
  if (first && sheet.isConnected) first.focus()
  return { close: withdraw }
}
