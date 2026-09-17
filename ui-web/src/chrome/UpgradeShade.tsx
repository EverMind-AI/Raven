/* The upgrade card over the page (shell/upgrade.ts).
 *
 * A portal at the body, like the other two overlays that belong to no page:
 * `.upshade` is the full-window shade itself, so a wrapper around it would take
 * the inset and the `--z` step the class carries.
 *
 * While the install is running the card is a bar and a line of text. A failure
 * hides the bar and adds the three things only the reader can act on: what
 * went wrong, the command to run by hand, and a way to put the card down.
 */
import { useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import * as upgrade from '../shell/upgrade'

import type { JSX } from 'react'

export function UpgradeShade(): JSX.Element | null {
  const card = useSyncExternalStore(upgrade.subscribe, upgrade.get)
  if (!card) return null
  const failure = card.failure
  return createPortal(
    <div className="upshade">
      <div className="upcard">
        <div className="upbar" hidden={!!failure}><i /></div>
        <div className="t">{card.text}</div>
        {failure ? (
          <>
            <div className="sub">{failure.sub}</div>
            <div className="cmd">
              <code>{upgrade.COMMAND}</code>
              <button onClick={() => upgrade.copyCommand()}>{failure.copy}</button>
            </div>
            <div className="foot">
              <button className="btn" onClick={() => upgrade.dismiss(card.id)}>{failure.close}</button>
            </div>
          </>
        ) : null}
      </div>
    </div>,
    document.body
  )
}
