/* One line above the composer when a conversation the reader is not looking at
 * is waiting on them -- an approval, a question -- with the way there.
 *
 * The rail already lights that conversation's row; this is the same fact said
 * where the reader's attention is. The source is the rack's own count of
 * asking sheets per conversation (state/sheetRack.ts `watchAsking`), so a
 * tenant docking a question anywhere is enough: nothing here knows what kind
 * of question it is.
 *
 * The container is the root React draws into, so its hidden flag is set here
 * rather than rendered -- the same arrangement the attachment tray has.
 */
import { useEffect, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import { current, onChange, setCurrent } from '../../lib/session'
import * as page from '../../state/page'
import { open as openRow, sess } from '../../state/session/rows'
import { watchAsking } from '../../state/sheetRack'
import { makeStore } from '../../state/store'

import type { SessRow } from '../rail/types'
import type { JSX } from 'react'

const counts = new Map<string, number>()
const store = makeStore<readonly string[]>([])

/* Conversations waiting on the reader other than the open one, oldest first.
   A draft's rack key is not a conversation anyone could open, and goes; one
   the list does not hold yet stays -- a conversation whose very first turn is
   blocked on its question is not on disk, and so not in the list, and the
   question is the reason the reader has to get there. */
function recount(): void {
  const here = current()
  const next = [...counts].filter(([key, n]) => n > 0 && key !== here && key !== '(draft)').map(([key]) => key)
  const shown = store.get()
  if (next.length === shown.length && next.every((k, i) => shown[i] === k)) return
  store.set(next)
}

let listening: Array<() => void> = []

/** Start listening. Once per page; the rack and the session pointer both outlive any island. */
export function installElsewhere(): void {
  if (listening.length) return
  listening = [
    watchAsking((key, n) => {
      counts.set(key, n)
      recount()
    }),
    onChange(recount),
  ]
}

export const waitingElsewhere = (): readonly string[] => store.get()

export function ElsewhereBar({ host }: { host: HTMLElement }): JSX.Element | null {
  const waiting = useSyncExternalStore(store.subscribe, store.get)
  useEffect(() => {
    host.hidden = waiting.length === 0
  }, [host, waiting])
  const id = waiting[0]
  if (!id) return null
  const go = (): void => {
    /* A detached row when the list has none yet, the way a reconnect reopens
       the current conversation (state/session/registry.ts). */
    const row = sess(id) || ({ id, title: id } as SessRow)
    page.show(null)
    setCurrent(id)
    void openRow(row)
  }
  return (
    <>
      <span>{t('gui.confirm.elsewhere')}</span>
      <button onClick={go}>{t('gui.confirm.elsewhere_go')}</button>
    </>
  )
}

/* Test seam only: the map and the store outlive a test file's DOM. */
export function _resetForTests(): void {
  listening.forEach((off) => off())
  listening = []
  counts.clear()
  store.set([])
}
