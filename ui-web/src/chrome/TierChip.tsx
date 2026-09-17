/* The sub-agent effort chip, beside the model chip and deliberately not inside
 * its picker: a tier is not a model. It moves what raven asks of the sub-agents
 * it dispatches, and leaves raven's own effort alone.
 *
 * Served hidden, and shown once a catalogue has answered -- a build can offer
 * none, and then the chip stays hidden rather than drawing a control over
 * nothing. All of that is state/tier.ts's `paint`: `off` while there is no
 * honest tier to name, and the bars, the name and the accessible name when
 * there is. The three are read only while it is on, which is what keeps the
 * served literals under a hidden chip instead of a tier this side invented.
 * aria-expanded is the panel's, and so is the click.
 */

import { useSyncExternalStore } from 'react'

import * as tier from '../state/tier'

import type { JSX } from 'react'

/** The bars page.html serves, and the one value with no paint behind it. */
const SERVED = '<path d="M6 18.5v-4"/><path d="M12 18.5v-9"/>'

export function TierChip(): JSX.Element {
  const s = useSyncExternalStore(tier.subscribe, tier.get)
  const p = s.paint
  const on = p && p.off === false ? p : null
  return (
    <button
      className="chip"
      id="tierChip"
      aria-expanded={s.open ? 'true' : 'false'}
      aria-haspopup="true"
      hidden={!p || p.off}
      aria-label={on ? on.aria : undefined}
      onClick={() => tier.toggle()}
    >
      <svg
        className="pico"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        aria-hidden="true"
        dangerouslySetInnerHTML={{ __html: on ? on.ico : SERVED }}
      />
      <span id="tierName">{on ? on.label : 'High'}</span>
    </button>
  )
}
