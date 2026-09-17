/* The two failure bars over the page (state/failureBar.ts).
 *
 * A portal at the body, like the other overlays that belong to no page: each
 * bar is `position: fixed` across the top, so a wrapper would take the inset.
 *
 * The boot bar's style is written through a ref rather than as a style prop:
 * it is one declaration block, kept as the single string it has always been so
 * that the page too broken to boot gets exactly the bar it used to get. React
 * never touches it again -- no style prop is passed, so nothing diffs it.
 */
import { useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import * as failure from '../state/failureBar'

import type { JSX } from 'react'

function BootErrorBar({ text }: { text: string }): JSX.Element {
  return <div ref={(node) => { if (node) node.style.cssText = failure.BOOT_CSS }}>{text}</div>
}

export function FailureBars(): JSX.Element | null {
  const bars = useSyncExternalStore(failure.subscribe, failure.get)
  if (!bars.length) return null
  return createPortal(
    bars.map((bar) => (bar.kind === 'boot'
      ? <BootErrorBar key={bar.id} text={bar.text} />
      : <div key={bar.id} className="topfail">{bar.text}</div>)),
    document.body
  )
}
