/* The credential card: a secret typed where the model cannot read it.
 *
 * When a tool needs a key, a token or a password (raven_config naming a vendor
 * key with an empty value, a channel's secret field), the host sends
 * `credential.request` with what to ask for -- never where the value goes --
 * and this docks a card above the composer, filed under the conversation that
 * asked, the way the approval sheet is (features/composer/approve.ts). What the
 * reader types goes to the host in `credential.submit`, which writes it through
 * the settings handler that owns it and only then resumes the tool; a value the
 * host refuses keeps the card up with the reason. Skip, Escape or the close
 * button answers `credential.skip`, and `credential.closed` takes the card down
 * whatever ended it (saved, skipped, timed out, the turn stopped).
 *
 * Spared by the rack's sweeps like a pending approval: the turn is stopped on
 * this card, and a card swept away unanswered leaves it waiting out the host's
 * deadline for nothing.
 */

import { createElement } from 'react'

import { t } from '../../i18n/t'
import { ESC_LABEL, chordLabel, sendChord } from '../../lib/platform'
import { add as sheetAdd, dropClass, remove as sheetRemove, session } from '../../state/sheetRack'
import { sparePendingApproval } from './approve'
import { CredentialSheet } from './CredentialSheet'
import { composing } from './store'

import type { SheetOptionRow } from '../../chrome/SheetRack'
import type { CredentialWords } from './CredentialSheet'

export interface CredentialReq {
  readonly requestId: string
  readonly label: string
  readonly note: string
  readonly replaces: boolean
}

export interface CredentialHandlers {
  /* Sends what was typed. Resolves `{ok: true}` once the host has written it,
     `{ok: false, error}` when it would not take it; rejects when it never got
     there (a dropped socket). */
  submit: (value: string) => Promise<{ ok?: boolean; error?: string }>
  skip: () => Promise<unknown>
}

/* Open cards by request id: a replay after a reload may name one already on
   screen, and credential.closed withdraws exactly the card it ends. */
const openCredentials = new Map<string, () => void>()

const topmost = (sheet: HTMLElement): boolean =>
  !sheet.parentElement || sheet.parentElement.firstElementChild === sheet

const inField = (e: KeyboardEvent): boolean => {
  const el = e.target as HTMLElement | null
  return !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)
}

export function openCredential(req: CredentialReq, handlers: CredentialHandlers, owner?: string): { close(): void } {
  const already = openCredentials.get(req.requestId)
  if (already) return { close: already }
  const key = owner || session()
  dropClass('csheet', key, sparePendingApproval)

  const words: CredentialWords = {
    title: t('gui.confirm.cred.title'),
    skip: t('gui.confirm.cred.skip'),
    hint: t('gui.confirm.cred.hint'),
    replaces: t('gui.confirm.cred.replaces'),
    placeholder: t('gui.confirm.cred.placeholder'),
  }
  const sheet = document.createElement('div')
  sheet.className = 'csheet perm'
  sheet.dataset.asks = '1'
  sheet.dataset.noDeadline = '1'
  sheet.setAttribute('role', 'dialog')
  sheet.setAttribute('aria-modal', 'true')
  sheet.setAttribute('aria-label', words.title)

  let done = false
  let busy = false
  let error = ''
  const field = (): HTMLInputElement | null => sheet.querySelector<HTMLInputElement>('input[data-credential]')
  const leave = (): void => {
    done = true
    openCredentials.delete(req.requestId)
    document.removeEventListener('keydown', onKey, true)
    sheetRemove(sheet)
  }
  const withdraw = (): void => {
    if (!done) leave()
  }
  const skip = (): void => {
    if (done) return
    leave()
    void handlers.skip().catch(() => {})
  }
  const save = (): void => {
    if (done || busy) return
    const input = field()
    const value = (input?.value || '').trim()
    if (input) input.value = ''
    if (!value) {
      input?.focus()
      return
    }
    busy = true
    error = ''
    paint()
    handlers.submit(value).then(
      (r) => {
        busy = false
        if (r?.ok) {
          leave()
          return
        }
        error = r?.error || t('gui.confirm.cred.unsent')
        paint()
      },
      () => {
        busy = false
        error = t('gui.confirm.cred.unsent')
        paint()
      },
    )
  }
  /* Laid out and answered as the approval sheets are (approve.ts): the way out
     first with Escape, the action last with the send chord. Not digits -- a
     key is mostly digits. */
  const opts = (): SheetOptionRow[] => [
    { label: words.skip, run: skip, keys: ESC_LABEL },
    { label: busy ? t('gui.confirm.cred.saving') : t('gui.confirm.cred.save'), run: save, go: true, keys: chordLabel() },
  ]
  function paint(): void {
    if (done) return
    sheetAdd(sheet, key, withdraw, createElement(CredentialSheet, {
      words, label: req.label, note: req.note, replaces: req.replaces, error, busy,
      opts: opts(), onSave: save, onSkip: skip,
    }))
    if (!busy) queueMicrotask(() => { if (sheet.isConnected && !done) field()?.focus() })
  }

  function onKey(e: KeyboardEvent): void {
    /* Parked with another conversation, or under a newer sheet: not this card's key. */
    if (!sheet.isConnected || composing(e) || !topmost(sheet)) return
    /* Stopped here, unlike the approval sheet's Escape: skipping a key is not
       stopping the turn. The tool is told the key was skipped and the turn
       goes on to say where it can be entered, so the page's Escape chain
       (state/escapeOrder.ts, on the document's bubble phase) must not also
       interrupt it. */
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); skip(); return }
    /* In the field, Enter with or without the chord is the field's own save. */
    if (inField(e)) return
    if (sendChord(e) === 'plain') { e.preventDefault(); save() }
  }
  document.addEventListener('keydown', onKey, true)
  openCredentials.set(req.requestId, withdraw)
  paint()
  return { close: withdraw }
}

/* credential.closed: the host ended this request -- saved, skipped from
   another surface, timed out, or its turn went away. Nothing is sent back. */
export function closeCredential(requestId: string): void {
  openCredentials.get(requestId)?.()
}

/* Test seam only. */
export function _resetForTests(): void {
  openCredentials.clear()
}
