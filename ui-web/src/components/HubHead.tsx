/* The header the hub's modules share: the page's title row, the module tabs,
   and one line under them on what the module on screen is for.

   Agents, channels and machines are one place on the rail (state/hub.ts). The
   page used to carry no title of its own, since the rail row already names
   it, and its tabs were the top of the page; it now starts with the title row
   every module page starts with (components/PageBar.tsx), because a top that
   moved with every page on the rail read as four different products. The tabs
   sit under it as the page's parts. Underlined rather than filled, so they read
   as those parts and the filter pills a module draws under them stay the
   smaller, last row. The line is the module's, so it changes with the tab
   rather than describing the hub as a whole. */

import { t } from '../i18n/t'
import * as hub from '../state/hub'
import { PageBar } from './PageBar'

import type { HubModule } from '../state/hub'
import type { JSX } from 'react'

const MODULES: ReadonlyArray<{ which: HubModule; key: string; sub: string }> = [
  { which: 'agents', key: 'gui.hub.agents', sub: 'gui.page.agents_sub' },
  { which: 'channels', key: 'gui.hub.channels', sub: 'gui.page.conn_sub' },
  { which: 'machines', key: 'gui.hub.machines', sub: 'gui.page.machines_sub' }
]

export function HubHead({ current }: { current: HubModule }): JSX.Element {
  const sub = MODULES.find((m) => m.which === current)?.sub
  return (
    <>
      <PageBar title={t('gui.nav.hub')} />
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
