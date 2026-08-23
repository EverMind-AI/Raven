/* The island's face: one host and one React root per conversation, and the four
 * calls the live layer makes into it.
 *
 * The rack decides when a host is in the document -- it files sheets under the
 * conversation that raised them and mounts only the open one -- so what is kept
 * here is the root's lifetime, not the sheet's visibility. The takedown handed
 * to `sheets.add` is what stops the clock: the interval lives in the component,
 * and a sheet whose conversation was deleted would otherwise tick for the life
 * of the page.
 */

import { createRoot } from 'react-dom/client'

import { t } from '../../shell/bridge'
import { add as sheetAdd, remove as sheetRemove } from '../composer/sheets'
import { Sheet } from './DagSheet'
import * as store from './store'

import type { DagRun } from './types'
import type { Root } from 'react-dom/client'

const HOSTS = new Map<string, { el: HTMLElement; root: Root }>()

function drop(key: string): void {
  const h = HOSTS.get(key)
  store.forget(key)
  if (h) sheetRemove(h.el)
}

function host(key: string): void {
  if (HOSTS.has(key)) return
  /* The sheet's own element, not a wrapper around it: the rack writes
     `data-sess` on what it is handed and `.dock .sheets > *` styles it as the
     flex item, so an extra div would take both. React owns the children of a
     container it is given; these three attributes never change, so they are set
     once here, and `data-fold`, which does change, is written by the component. */
  const el = document.createElement('div')
  el.className = 'dsheet'
  el.setAttribute('role', 'group')
  el.setAttribute('aria-label', t('gui.dag.aria'))
  /* Written here as well as by the component, and in this order on purpose: the
     rack adds `data-sess` the moment it is handed the element, so setting the
     fold state afterwards would leave the sheet's attributes in a different
     order than the imperative builder left them. Same DOM, same order. */
  el.dataset.fold = String(!!store.run(key)?.folded)
  const root = createRoot(el)
  HOSTS.set(key, { el, root })
  sheetAdd(el, key, () => {
    HOSTS.delete(key)
    /* Off the current task: unmounting a root from inside a React commit --
       which is where this lands when the rack retires a sheet during a render --
       is refused with a warning. */
    setTimeout(() => root.unmount(), 0)
  })
  root.render(<Sheet sess={key} host={el} onClose={() => drop(key)} />)
}

/* A graph arrives whole, before any node runs, so this is also the only moment
   the layout is decided. Replaces whatever that conversation was watching: one
   run at a time is what a sheet can show, and the previous run keeps its own
   card in the trail. */
export function start(key: string, run: DagRun): void {
  store.set(key, run)
  host(key)
}

/* The caller mutated the run it holds -- a node's status, a time, the summary --
   and wants the sheet to say so. */
export const touch = (): void => store.touch()

/* Called wherever the open conversation changes, right after the rack's own
   sync: a sheet that has just been mounted can measure its labels, and one that
   has just been detached should stop trying. */
export const sync = (): void => store.touch()

/* The conversation went away. Not the same as the reader closing the sheet, but
   it ends the same way. */
export const forget = (key: string): void => drop(key)

export const run = (key: string): DagRun | null => store.run(key)

/* Test seam. */
export function _resetForTests(): void {
  HOSTS.forEach(({ root }) => root.unmount())
  HOSTS.clear()
  store._resetForTests()
}
