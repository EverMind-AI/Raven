/** Center-stage tab strip for the Raven transcript and hosted terminals. */

import { useEffect, useSyncExternalStore } from 'react'

import { t } from '../../shell/bridge'
import * as store from './store'
import { TerminalPane } from './TerminalPane'

import type { TerminalRow } from './types'
import type { JSX } from 'react'

export function TerminalApp(): JSX.Element {
  const state = useSyncExternalStore(store.subscribe, store.getState)

  useEffect(() => {
    store.start()
    return store.stop
  }, [])

  useEffect(() => {
    const chat = document.querySelector<HTMLElement>('.chat')
    if (!chat) return
    chat.dataset.terminalActive = String(state.activeTab !== store.TRANSCRIPT_TAB)
    return () => {
      chat.dataset.terminalActive = 'false'
    }
  }, [state.activeTab])

  return (
    <section className="terminal-surface" aria-label={t('gui.terminal.surface')}>
      <div className="terminal-tabs" role="tablist" aria-label={t('gui.terminal.tabs')}>
        <Tab id={store.TRANSCRIPT_TAB} label={t('gui.terminal.transcript')} active={state.activeTab === store.TRANSCRIPT_TAB} />
        {state.terminals.map((row) => (
          <Tab key={row.handle} id={row.handle} label={canonicalName(row)} active={state.activeTab === row.handle} />
        ))}
      </div>
      <div className="terminal-panels">
        {state.terminals.map((terminal) => (
          <TerminalPanel key={terminal.handle} terminal={terminal} active={state.activeTab === terminal.handle} />
        ))}
      </div>
    </section>
  )
}

function canonicalName(terminal: TerminalRow): string {
  return terminal.identity?.agentName || terminal.title || terminal.owner || terminal.handle
}

function Tab({ id, label, active }: { id: string; label: string; active: boolean }): JSX.Element {
  return (
    <button
      className={`terminal-tab${active ? ' active' : ''}`}
      type="button"
      role="tab"
      aria-selected={active}
      aria-controls={id === store.TRANSCRIPT_TAB ? undefined : `terminal-panel-${id}`}
      onClick={() => store.selectTab(id)}
    >
      {label}
    </button>
  )
}

function TerminalPanel({ terminal, active }: { terminal: TerminalRow; active: boolean }): JSX.Element {
  const name = canonicalName(terminal)
  return (
    <div className="terminal-panel" id={`terminal-panel-${terminal.handle}`} role="tabpanel" hidden={!active}>
      <header className="terminal-identity">
        <Identity label={t('gui.terminal.canonical_name')} value={name} valueClass="terminal-name" />
        <Identity label={t('gui.terminal.provider')} value={terminal.identity?.brand || t('gui.terminal.unknown')} />
        <Identity label={t('gui.terminal.instance')} value={terminal.handle} />
        <Identity label={t('gui.terminal.incarnation')} value={terminal.incarnationId} />
      </header>
      <div className="terminal-stage">
        <span className="terminal-waiting">{t('gui.terminal.waiting')}</span>
        <TerminalPane terminal={terminal} active={active} />
      </div>
    </div>
  )
}

function Identity({ label, value, valueClass = '' }: { label: string; value: string; valueClass?: string }): JSX.Element {
  return (
    <span className="terminal-identity-field">
      <span>{label}</span>
      <code className={valueClass}>{value}</code>
    </span>
  )
}
