/* The sub-agent effort chip, beside the model chip and deliberately not inside
 * its picker: a tier is not a model. It moves what raven asks of the sub-agents
 * it dispatches, and leaves raven's own effort alone.
 *
 * Served hidden, and shown by shell/tier.ts's draw once a catalogue has
 * answered -- a build can offer none, and then the chip stays hidden rather
 * than drawing a control over nothing. The showing, the bars, the name and the
 * accessible name are that module's four writes by id, for the reason
 * src/chrome/PermChip.tsx gives; aria-expanded is the panel's and comes from
 * the store, and so does the click.
 */

import { useSyncExternalStore } from 'react'

import * as tier from '../shell/tier'

import type { JSX } from 'react'

export function TierChip(): JSX.Element {
  const s = useSyncExternalStore(tier.subscribe, tier.get)
  return (
    <button
      className="chip"
      id="tierChip"
      aria-expanded={s.open ? 'true' : 'false'}
      aria-haspopup="true"
      hidden
      onClick={() => tier.toggle()}
    >
      <svg className="pico" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
        <path d="M6 18.5v-4" /><path d="M12 18.5v-9" />
      </svg>
      <span id="tierName">High</span>
    </button>
  )
}
