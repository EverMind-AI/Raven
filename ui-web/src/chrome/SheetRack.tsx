/* The interiors of the sheets docked above the composer.
 *
 * One portal per sheet, into the sheet's own element rather than into
 * `#sheetRack`, and that is the whole of the arrangement. Three things ask for
 * it. `.dock .sheets > *` styles a sheet as the rack's flex item and the rack
 * writes `data-sess` on what it is handed (src/styles/page.css:4385-4393), so a
 * sheet cannot be wrapped. The rack's child list is mixed -- the dag sheet is a
 * host element with a React root of its own -- and a portal appends its children
 * to the container it is given, so a portal into `#sheetRack` would put a
 * question under a graph that docked before it instead of over it. And the order
 * the rack keeps is "newest first" with the caret left alone on a sync, which is
 * an insertBefore the store already does node by node.
 *
 * So the rack resolves `#sheetRack` when a sheet docks, which is also what makes
 * this indifferent to whether that element is page.html's or React's.
 *
 * Only the open conversation's sheets are here: a parked sheet's interior is
 * unmounted, and what the reader typed into it is in state/sheetDrafts.ts. The
 * host element itself is kept by the store, so coming back re-fills the same
 * sheet rather than replaying its entrance animation.
 */
import { useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { sheets, subscribe } from '../state/sheetRack'

import type { JSX, ReactNode } from 'react'

export function SheetRack(): JSX.Element {
  const live = useSyncExternalStore(subscribe, sheets)
  return <>{live.map((s) => createPortal(s.view, s.el, s.id))}</>
}

/* One numbered row, which all three sheets wear: the number the keyboard picks
   it by, the wording, and for the approval sheet's persisted grant the prefix
   field that rides inside the row. Shared rather than drawn three times, for the
   reason shell/ico.tsx gives about its glyphs. */
export interface SheetOptionRow {
  readonly label: string
  readonly run: () => void
  /** The default answer, which the sheet marks. */
  readonly go?: boolean
  /** The row carrying the prefix rule. */
  readonly rule?: boolean
}

export function SheetOption(
  { n, row, children }: { n: number; row: SheetOptionRow; children?: ReactNode },
): JSX.Element {
  return (
    <button className={`opt${row.go ? ' go' : ''}${row.rule ? ' rule' : ''}`} onClick={row.run}>
      <span className="n">{n}</span>
      <span>{row.label}</span>
      {children}
    </button>
  )
}
