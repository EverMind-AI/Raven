/* The header the two connection pages share: the module tabs, and one line
   under them on what the module on screen is for.

   Agents and channels are one place on the rail (state/hub.ts), and the rail
   row already names it, so the page carries no title of its own: the tabs are
   the top of the page. Underlined rather than filled, so they read as the
   page's two halves and the filter pills each module draws under them stay
   the smaller, second row. The line is the module's, so it changes with the
   tab rather than describing the hub as a whole. */

import { t } from '../i18n/t'
import * as hub from '../state/hub'

import type { HubModule } from '../state/hub'
import type { JSX } from 'react'

const MODULES: ReadonlyArray<{ which: HubModule; key: string; sub: string }> = [
  { which: 'agents', key: 'gui.hub.agents', sub: 'gui.page.agents_sub' },
  { which: 'channels', key: 'gui.hub.channels', sub: 'gui.page.conn_sub' }
]

export function HubHead({ current }: { current: HubModule }): JSX.Element {
  const sub = MODULES.find((m) => m.which === current)?.sub
  return (
    <>
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
      {sub ? <p className="hub-head-sub">{t(sub)}</p> : null}
    </>
  )
}
