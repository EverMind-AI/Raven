/* The hover pill's label, in the one layer that holds it.
 *
 * Only the text is React's. Whether the pill is up (`data-on`) and where it
 * sits (`left`/`top`) are written by state/tooltip.ts as it measures, the way
 * every attribute of a container this file only fills is written: the store
 * places the pill against the control the pointer is on, and a re-render must
 * not undo a measurement.
 */
import { useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import * as tip from '../state/tooltip'

import type { JSX } from 'react'

export function Tooltip(): JSX.Element | null {
  const state = useSyncExternalStore(tip.subscribe, tip.get)
  if (!state.host) return null
  return createPortal(state.text, state.host)
}
