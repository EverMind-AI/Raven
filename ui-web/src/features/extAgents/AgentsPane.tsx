/* The onboarding wizard's agents step: a read-only slice of the extAgents
 * roster, reduced to the two buckets a first-run reader needs -- what this
 * machine already found, and what is already connected. The settings page's
 * own four-group triage (`ExtAgentsPage.tsx`) stays there; a wizard step is a
 * decision, not a roster to manage, so this pane has no folding "you would
 * have to install this" group and no detail sheet -- a row's action is the
 * whole of what it offers.
 */

import { useSyncExternalStore } from 'react'

import { AgentMark, isOwnAgent } from '../../components/AgentMark'
import { SetupGroup, SetupRow } from '../../components/SetupRow'
import { t } from '../../i18n/t'
import * as lang from '../../state/lang'
import { isAvailable, isConnected } from './source'
import * as store from './store'

import type { ExtAgentRow } from './types'
import type { JSX } from 'react'

/* The same kind labels the settings page prints, without its transport-
   migration tag: a row in either of this pane's buckets never carries
   `upgrade_to`. */
const kindText = (kind: string): string =>
  t(
    kind === 'builtin'
      ? 'gui.agent.kind_builtin'
      : kind === 'openai'
        ? 'gui.agent.kind_openai'
        : kind === 'acp'
          ? 'gui.agent.kind_acp'
          : 'gui.agent.kind_cli',
  )

function AvailableRow({ row, joining }: { row: ExtAgentRow; joining: boolean }): JSX.Element {
  return (
    <SetupRow
      act={
        <button className="mini" disabled={joining} onClick={() => void store.connect(row)}>
          {joining ? (
            <>
              <span className="extAgents-spin" />
              {t('gui.agent.setup_connecting')}
            </>
          ) : (
            t('gui.agent.connect')
          )}
        </button>
      }
      name={row.name}
      onOpen={() => {}}
      state={{ cls: 'off', text: '' }}
      tags={<span className="kd">{kindText(row.kind)}</span>}
      tile={<AgentMark preset={row.preset} own={isOwnAgent(row)} />}
    />
  )
}

function ConnectedRow({ row }: { row: ExtAgentRow }): JSX.Element {
  return (
    <SetupRow
      act={
        <button className="mini ghost" onClick={() => void store.disconnect(row)}>
          {t('gui.agent.disconnect')}
        </button>
      }
      name={row.name}
      onOpen={() => {}}
      state={{ cls: 'ok', text: '' }}
      tags={<span className="kd">{kindText(row.kind)}</span>}
      tile={<AgentMark preset={row.preset} own={isOwnAgent(row)} />}
    />
  )
}

export function AgentsPane(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  /* Every word below is a t(key) read at render time, same as every other
     island (state/lang/store.ts). */
  useSyncExternalStore(lang.subscribe, lang.get)

  /* The cold start only: a reload of a roster the pane has already drawn once
     keeps showing those rows rather than replacing them with the scan
     placeholder. */
  const scanning = s.loading && s.rows.length === 0
  const available = s.rows.filter(isAvailable)
  const connected = s.rows.filter(isConnected)

  return (
    <>
      {scanning ? (
        <SetupGroup count={0} label={t('gui.agent.setup_available')}>
          <div className="sulist">
            <div className="extAgents-scan">
              <span className="extAgents-spin" />
              <span className="hint">{t('gui.agent.setup_scanning')}</span>
            </div>
          </div>
        </SetupGroup>
      ) : null}
      {!scanning && available.length ? (
        <SetupGroup count={available.length} label={t('gui.agent.setup_available')}>
          <div className="sulist">
            {available.map((row) => (
              <AvailableRow joining={s.joining.includes(row.name)} key={row.name} row={row} />
            ))}
          </div>
        </SetupGroup>
      ) : null}
      {connected.length ? (
        <SetupGroup count={connected.length} label={t('gui.agent.setup_connected')}>
          <div className="sulist">
            {connected.map((row) => (
              <ConnectedRow key={row.name} row={row} />
            ))}
          </div>
        </SetupGroup>
      ) : null}
    </>
  )
}
