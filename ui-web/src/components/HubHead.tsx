/* The header the two connection pages share: the module tabs, and nothing
   above them.

   Agents and channels are one place on the rail (state/hub.ts), and the rail
   row already names it, so the page carries no title of its own: the tabs are
   the top of the page. Underlined rather than filled, so they read as the
   page's two halves and the filter pills each module draws under them stay
   the smaller, second row. */

import { t } from '../i18n/t'
import * as hub from '../state/hub'

import type { HubModule } from '../state/hub'
import type { JSX } from 'react'

const MODULES: ReadonlyArray<{ which: HubModule; key: string }> = [
  { which: 'agents', key: 'gui.hub.agents' },
  { which: 'channels', key: 'gui.hub.channels' },
]

export function HubHead({ current }: { current: HubModule }): JSX.Element {
  return (
    <div className="hub-head" role="tablist">
      {MODULES.map((m) => (
        <button
          aria-selected={m.which === current}
          className="hub-head-tab"
          key={m.which}
          onClick={() => {
            if (m.which !== current) hub.open(m.which)
          }}
          role="tab"
          type="button"
        >
          {t(m.key)}
        </button>
      ))}
    </div>
  )
}
