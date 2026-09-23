/* The onboarding wizard's agents step: the Agent Hub's rows (Rows.tsx),
 * reduced to the two sections a first-run reader decides on -- what this
 * machine has that could be connected, then what already is. The hub's third section
 * (not installed on this machine) and its sheet stay on the hub: a wizard step
 * is a decision, not a roster to manage, so a row here has no sheet to open and
 * its one control is the whole of what it offers. Which rows count towards the
 * step being done is `isFound` (source.ts).
 */

import { useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import * as lang from '../../state/lang'
import { SectionBlock, Spin, ordered } from './Rows'
import { wizardSection } from './source'
import * as store from './store'

import type { JSX } from 'react'

export function AgentsStepBody(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  /* Every word below is a t(key) read at render time, same as every other
     island (state/lang/store.ts). */
  useSyncExternalStore(lang.subscribe, lang.get)

  /* The cold start only: a reload of a roster the pane has already drawn once
     keeps showing those rows rather than replacing them with the scan
     placeholder. */
  const scanning = s.loading && s.rows.length === 0
  const avail = ordered(s.rows.filter((row) => wizardSection(row) === 'avail'))
  const on = ordered(s.rows.filter((row) => wizardSection(row) === 'on'))

  return (
    <>
      {scanning ? (
        <section className="extAgents-sec">
          <div className="extAgents-hd">
            <b>{t('gui.agent.g_avail')}</b>
            <span className="extAgents-n">0</span>
          </div>
          <div className="extAgents-scan">
            <Spin />
            <span className="hint">{t('gui.agent.setup_scanning')}</span>
          </div>
        </section>
      ) : null}
      {!scanning && avail.length ? <SectionBlock label={t('gui.agent.g_avail')} rows={avail} s={s} /> : null}
      {on.length ? <SectionBlock label={t('gui.agent.g_on')} rows={on} s={s} /> : null}
    </>
  )
}
