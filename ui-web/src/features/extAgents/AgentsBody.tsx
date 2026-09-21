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
import { ask as confirmAsk } from '../../state/confirm'
import * as lang from '../../state/lang'
import { isAvailable, isConnected, stageOf } from './source'
import * as store from './store'

import type { ExtAgentRow } from './types'
import type { JSX } from 'react'

/* The same kind labels the settings page prints. */
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
  /* A stale row -- its preset moved to another transport -- connects by a
     remove plus an add, which drops the handles of runs already in flight;
     the settings page asks first and so does this. The tag beside the kind
     says which transport it moves to, the same words the page uses. */
  const connect = (): void => {
    if (stageOf(row) === 'stale') {
      confirmAsk(
        t('gui.agent.migrate_do'),
        t('gui.agent.migrate_body', { name: row.name, to: kindText(row.upgrade_to || '') }),
        t('gui.agent.migrate_do'),
        () => void store.connect(row),
      )
    } else void store.connect(row)
  }
  return (
    <SetupRow
      act={
        <button className="mini" disabled={joining || stageOf(row) === 'unauthorized'} onClick={connect}>
          {joining ? (
            <>
              <span className="extAgents-spin" />
              {t('gui.agent.setup_connecting')}
            </>
          ) : (
            t(stageOf(row) === 'unauthorized' ? 'gui.agent.unauthorized' : 'gui.agent.connect')
          )}
        </button>
      }
      name={row.name}
      onOpen={() => {}}
      state={{ cls: 'off', text: '' }}
      tags={
        <>
          <span className="kd">{kindText(row.kind)}</span>
          {row.upgrade_to ? <span className="kd">{t('gui.agent.stale_to', { to: kindText(row.upgrade_to) })}</span> : null}
        </>
      }
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

export function AgentsStepBody(): JSX.Element {
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
