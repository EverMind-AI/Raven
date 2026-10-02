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
          <TerminalPanel
            key={terminal.handle}
            terminal={terminal}
            active={state.activeTab === terminal.handle}
            delivery={state.deliveries[terminal.handle]}
            mailboxState={state.mailboxes[terminal.handle]}
          />
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

function messagePhase(msg: import('./types').MailboxMessageRow): 'stored' | 'claimed' | 'processed' | 'blocked' {
  if (
    msg.outcome === 'blocked' ||
    msg.phase === 'dead_letter' ||
    msg.error === 'storage_conflict' ||
    msg.terminal_reason === 'blocked'
  ) {
    return 'blocked'
  }
  if (msg.phase === 'in_progress') {
    return 'claimed'
  }
  if (msg.phase === 'completed' || msg.outcome === 'succeeded' || msg.outcome === 'failed') {
    return 'processed'
  }
  return 'stored'
}

function hasMailboxData(data: import('./types').MailboxOverviewData | null | undefined): boolean {
  if (!data) return false
  const hasBindings = (data.bindings?.length ?? 0) > 0
  const hasMessages = (data.messages?.length ?? 0) > 0
  const hasNotifications = (data.notifications?.length ?? 0) > 0
  const hasAuthority = Boolean(data.authority)
  return Boolean(hasBindings || hasMessages || hasNotifications || hasAuthority)
}

function MailboxSection({ mailbox }: { mailbox: import('./types').MailboxOverviewData }): JSX.Element {
  const counts = {
    stored: 0,
    claimed: 0,
    processed: 0,
    blocked: 0,
  }
  for (const msg of mailbox.messages) {
    counts[messagePhase(msg)] += 1
  }

  return (
    <section className="terminal-mailbox-section" aria-label={t('gui.terminal.mailbox_status', undefined, 'Mailbox Status')}>
      <div className="terminal-mailbox-header">
        <div className="terminal-mailbox-authority">
          <span className="terminal-mailbox-label">{t('gui.terminal.authority', undefined, 'Authority')}</span>
          {mailbox.authority ? (
            <span className="terminal-mailbox-owner">
              {t('gui.terminal.owner', undefined, 'Owner')}: <code>{mailbox.authority.owner_agent_id}</code> (epoch {mailbox.authority.assignment_epoch})
            </span>
          ) : (
            <span className="terminal-mailbox-owner-none">{t('gui.terminal.unassigned', undefined, 'Unassigned')}</span>
          )}
        </div>
        <div className="terminal-mailbox-phases" aria-label={t('gui.terminal.message_phases', undefined, 'Message Phases')}>
          <span className="terminal-phase-pill phase-stored">stored: {counts.stored}</span>
          <span className="terminal-phase-pill phase-claimed">claimed: {counts.claimed}</span>
          <span className="terminal-phase-pill phase-processed">
            processed: {counts.processed}
            <span className="terminal-phase-note">(task incomplete)</span>
          </span>
          <span className="terminal-phase-pill phase-blocked">blocked: {counts.blocked}</span>
        </div>
      </div>

      {mailbox.messages.length > 0 ? (
        <div className="terminal-mailbox-messages">
          {mailbox.messages.map((msg) => {
            const phase = messagePhase(msg)
            return (
              <div key={msg.message_id} className="terminal-msg-row">
                {msg.direction ? (
                  <span className={`terminal-msg-dir ${msg.direction}`}>{msg.direction}</span>
                ) : null}
                <span className={`terminal-msg-phase phase-${phase}`}>{phase}</span>
                <code className="terminal-msg-id">{msg.message_id.slice(0, 8)}</code>
                {msg.envelope?.kind ? (
                  <span className="terminal-msg-kind">{msg.envelope.kind}</span>
                ) : null}
                {msg.handoff?.status ? (
                  <span className={`terminal-handoff-status status-${msg.handoff.status.toLowerCase()}`}>
                    handoff: {msg.handoff.status}
                  </span>
                ) : null}
                {msg.handoff?.unresolved_items && msg.handoff.unresolved_items.length > 0 ? (
                  <span className="terminal-handoff-unresolved">
                    unresolved: {msg.handoff.unresolved_items.join(', ')}
                  </span>
                ) : null}
                {msg.envelope?.in_reply_to ? (
                  <span className="terminal-receipt-link">
                    <span>reply-to:</span> <code>{msg.envelope.in_reply_to.slice(0, 8)}</code>
                  </span>
                ) : null}
                {msg.result_hash ? (
                  <span className="terminal-receipt-link">
                    <span>result:</span> <code>{msg.result_hash.slice(0, 8)}</code>
                  </span>
                ) : null}
                {msg.envelope?.artifacts && msg.envelope.artifacts.length > 0 ? (
                  <span className="terminal-artifacts">
                    {msg.envelope.artifacts.map((art) => (
                      <span key={art.sha256} className="terminal-art-badge">
                        <span>{art.name}</span> (<code>{art.sha256.slice(0, 8)}</code>)
                      </span>
                    ))}
                  </span>
                ) : null}
              </div>
            )
          })}
        </div>
      ) : null}

      {mailbox.notifications.length > 0 ? (
        <div className="terminal-mailbox-notifications">
          <div className="terminal-notifications-title">
            <span>{t('gui.terminal.notifications_ledger', undefined, 'Notifications Ledger')}</span>
          </div>
          {mailbox.notifications.map((n) => (
            <div key={n.request_id} className="terminal-notification-row">
              <span className={`terminal-notif-stage stage-${n.stage}`}>{n.stage}</span>
              <span className="terminal-notif-req">
                req: <code>{n.request_id.slice(0, 8)}</code>
              </span>
              {n.bytes_written !== undefined && n.bytes_written > 0 ? (
                <span className="terminal-notif-bytes">{n.bytes_written}B</span>
              ) : null}
              {n.turn_id ? (
                <span className="terminal-notif-turn">
                  turn: <code>{n.turn_id}</code>
                </span>
              ) : null}
              {n.detail ? <span className="terminal-notif-detail">{n.detail}</span> : null}
            </div>
          ))}
        </div>
      ) : null}
    </section>
  )
}

function TerminalPanel({
  terminal,
  active,
  delivery,
  mailboxState,
}: {
  terminal: TerminalRow
  active: boolean
  delivery?: store.TerminalDelivery
  mailboxState?: store.MailboxState
}): JSX.Element {
  const name = canonicalName(terminal)
  return (
    <div className="terminal-panel" id={`terminal-panel-${terminal.handle}`} role="tabpanel" hidden={!active}>
      <header className="terminal-identity">
        <Identity label={t('gui.terminal.canonical_name')} value={name} valueClass="terminal-name" />
        <Identity label={t('gui.terminal.provider')} value={terminal.identity?.brand || t('gui.terminal.unknown')} />
        {terminal.identity?.taskRef ? (
          <Identity
            label={t('gui.terminal.task_ref', undefined, 'Task')}
            value={terminal.identity.taskRef}
            valueClass="terminal-task-ref"
          />
        ) : null}
        <Identity label={t('gui.terminal.instance')} value={terminal.handle} />
        <Identity label={t('gui.terminal.incarnation')} value={terminal.incarnationId} />
      </header>
      {delivery ? <DeliveryStrip stage={delivery.stage} /> : null}
      {mailboxState?.capabilityUnavailable ? (
        <div className="terminal-mailbox-notice" role="status">
          <span>{t('gui.terminal.mailbox_unavailable', undefined, 'Mailbox capability unavailable')}</span>
        </div>
      ) : hasMailboxData(mailboxState?.data) ? (
        <MailboxSection mailbox={mailboxState!.data!} />
      ) : null}
      <div className="terminal-stage">
        <span className="terminal-waiting">{t('gui.terminal.waiting')}</span>
        <TerminalPane terminal={terminal} active={active} />
      </div>
    </div>
  )
}

function DeliveryStrip({ stage }: { stage: store.TerminalDelivery['stage'] }): JSX.Element {
  return (
    <div className="terminal-delivery" aria-label={t('gui.terminal.delivery')}>
      <span className="terminal-delivery-state active" data-state="delivered">
        <i />
        {t('gui.terminal.delivered')}
      </span>
      <span className={`terminal-delivery-state${stage === 'acknowledged' ? ' active' : ''}`} data-state="acknowledged">
        <i />
        {t('gui.terminal.acknowledged')}
      </span>
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
