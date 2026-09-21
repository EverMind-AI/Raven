/* The deck template picker: a sheet in the rack over the composer, listing the
 * templates the deck engine ships, each as its cover. Picking one stages it in
 * the attachment tray (store.addTemplate), where it becomes an ordinary
 * attachment once the server has copied it under uploads -- the turn then hands
 * the path over like any file, and the deck engine's route opens on it.
 *
 * The sheet element, the key handler and the teardown belong here, as
 * approve.ts keeps them for its sheets; TemplateSheet.tsx renders the interior.
 * One picker at a time: opening drops the one already docked in this
 * conversation rather than stacking a second gallery under it.
 */
import { createElement } from 'react'

import { t } from '../../i18n/t'
import { add as sheetAdd, dropClass, remove as sheetRemove, session } from '../../state/sheetRack'
import * as store from './store'
import { TemplateSheet } from './TemplateSheet'

import type { TemplateRow } from './types'

const CLASS = 'cp-tpl-sheet'
/* How long to wait before asking for the covers again while one is still being
   drawn. The first render of the set is a minute and a half of LibreOffice on
   the server, so the sheet fills in as they land rather than waiting for all. */
export const REFRESH_MS = 2500
/* And how many times: the server stops saying pending once a cover has landed
   or failed, so this only ever ends a poll the server forgot to end. */
export const REFRESH_MAX = 60

export function open(): void {
  const api = store.source().templates
  if (!api) return
  const key = session()
  dropClass(CLASS, key)

  const sheet = document.createElement('div')
  sheet.className = `csheet ${CLASS}`
  sheet.setAttribute('role', 'dialog')
  sheet.setAttribute('aria-label', t('gui.tpl.title'))

  let closed = false
  const close = (): void => {
    if (closed) return
    closed = true
    document.removeEventListener('keydown', onKey, true)
    sheetRemove(sheet)
  }
  function onKey(e: KeyboardEvent): void {
    if (!sheet.isConnected || store.composing(e)) return
    if (e.key === 'Escape') {
      e.preventDefault()
      close()
    }
  }
  document.addEventListener('keydown', onKey, true)

  /* Read once when the sheet opens, so a language flip does not re-word a
     gallery already on screen. */
  const words = {
    title: t('gui.tpl.title'),
    close: t('gui.tpl.close'),
    hint: t('gui.tpl.hint'),
    back: t('gui.tpl.back'),
    use: t('gui.tpl.use'),
    pagesLoading: t('gui.tpl.pages_loading'),
    pagesNone: t('gui.tpl.pages_none'),
    page: t('gui.tpl.page'),
    prev: t('gui.tpl.prev'),
    next: t('gui.tpl.next'),
  }
  const pages = (name: string): Promise<string[]> => api.pages(name).then((r) => r.pages)
  const paint = (rows: TemplateRow[] | null, empty: string): void => {
    /* Re-added under the same element: the rack keeps the element and swaps
       the view, so a list arriving after the sheet docked repaints in place. */
    sheetAdd(sheet, key, close, createElement(TemplateSheet, {
      words,
      empty,
      rows,
      pages,
      onClose: close,
      onPick: (row: TemplateRow) => {
        store.addTemplate(row)
        close()
      },
    }))
  }
  paint(null, t('gui.tpl.loading'))
  let asked = 0
  const ask = (): void => {
    asked += 1
    api.list()
      .then((r) => {
        if (closed) return
        paint(r.templates, r.available ? t('gui.tpl.none') : t('gui.tpl.unavailable'))
        if (r.pending && asked < REFRESH_MAX) setTimeout(() => { if (!closed) ask() }, REFRESH_MS)
      })
      .catch(() => {
        if (closed) return
        paint([], t('gui.tpl.unavailable'))
      })
  }
  ask()
}
