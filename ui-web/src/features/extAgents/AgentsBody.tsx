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
  /* Raven's shipped specialists are a group of their own: they come with
     Raven, so "connected" and "available" are not things they are. */
  const shipped = ordered(s.rows.filter((row) => row.vendored && wizardSection(row) !== null))
  const avail = ordered(s.rows.filter((row) => !row.vendored && wizardSection(row) === 'avail'))
  const on = ordered(s.rows.filter((row) => !row.vendored && wizardSection(row) === 'on'))
  /* Nothing of the reader's own to connect: said, rather than a section that
     holds only Raven's shipped agents and reads as if those were what was found. */
  const none = !scanning && store.found().length === 0

  return (
    <>
      <p className="extAgents-lede">{t('gui.page.agents_sub')}</p>
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
      {!scanning && (avail.length || none) ? (
        <SectionBlock label={t('gui.agent.g_avail')} rows={avail} s={s} note={none ? t('gui.agent.setup_none') : undefined} />
      ) : null}
      {on.length ? <SectionBlock label={t('gui.agent.g_on')} rows={on} s={s} /> : null}
      {shipped.length ? (
        <SectionBlock label={t('gui.agent.g_shipped', { n: shipped.length })} rows={shipped} s={s} counted={false} />
      ) : null}
    </>
  )
}
