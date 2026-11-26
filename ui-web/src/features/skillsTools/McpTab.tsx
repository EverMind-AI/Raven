/* MCP tab (Scheme C — Tactile Bento Studio): two-tone tray cards for configured
 * MCP servers with live status trays, inline toggle switches, and centered
 * `#detail` credential & diagnostics modals.
 */
import { ArrowRight01Icon, Plug02Icon, PlusSignIcon } from '@hugeicons/core-free-icons'

import { Icon } from '../../components/Icon'
import { t } from '../../i18n/t'
import { mcpChip, mcpTone } from './helpers'
import { EmptyBox, IconTile, Pill, StatusDot, Switch } from './Parts'
import * as store from './store'

import type { McpSnapshot } from './types'
import type { JSX } from 'react'

const busyKey = (name: string): string => `mcp:${name}`

function statusMeta(m: McpSnapshot, busy: boolean): {
  tone: 'ok' | 'bad' | 'warn' | 'off'
  trayTone: 'neutral' | 'bad' | 'warn' | 'off'
  label: string
  spinning: boolean
  cue: string
} {
  const kind = mcpChip(m, busy)
  if (kind === 'connected') {
    return {
      tone: 'ok',
      trayTone: 'neutral',
      label: t('gui.skillsTools.mcp.connected'),
      spinning: false,
      cue: t('gui.skillsTools.configure'),
    }
  }
  if (kind === 'connecting') {
    return {
      tone: 'warn',
      trayTone: 'warn',
      label: t('gui.skillsTools.mcp.connecting'),
      spinning: true,
      cue: t('gui.skillsTools.details'),
    }
  }
  if (kind === 'setup') {
    return {
      tone: 'warn',
      trayTone: 'warn',
      label: t('gui.skillsTools.mcp.setup'),
      spinning: false,
      cue: t('gui.skillsTools.configure'),
    }
  }
  if (kind === 'failed') {
    return {
      tone: 'bad',
      trayTone: 'bad',
      label: t('gui.skillsTools.mcp.failed'),
      spinning: false,
      cue: t('gui.skillsTools.configure'),
    }
  }
  return {
    tone: 'off',
    trayTone: 'off',
    label: t('gui.skillsTools.mcp.off'),
    spinning: false,
    cue: t('gui.skillsTools.configure'),
  }
}

function McpCard({ m, onFlip }: { m: McpSnapshot; onFlip(on: boolean): void }): JSX.Element {
  const busy = store.isBusy(busyKey(m.name))
  const st = statusMeta(m, busy)
  const cardCls = !m.enabled ? 'skillsTools-tray-card skillsTools-is-off' : 'skillsTools-tray-card'
  const open = (): void => { void store.openMcp(m.name) }
  return (
    <div
      className={cardCls}
      role="button"
      tabIndex={0}
      onClick={open}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          open()
        }
      }}
    >
      <div className="skillsTools-tray-body">
        <div className="skillsTools-ctop">
          <IconTile tone={mcpTone(m.name)} size="sm">
            <Icon icon={Plug02Icon} size={15} />
          </IconTile>
          <div className="skillsTools-cmeta">
            <div className="skillsTools-cname mono">{m.name}</div>
            <div className="skillsTools-ctags">
              <Pill tone="mono">{m.transport.toUpperCase()}</Pill>
              <Pill tone="neutral">{t('gui.skillsTools.mcp.tools_n', { n: m.tool_count })}</Pill>
            </div>
          </div>
          <Switch on={m.enabled} label={m.name} stop onChange={onFlip} />
        </div>
        {m.enabled && m.error && st.tone === 'bad' ? (
          <p className="skillsTools-mcp-err mono" title={m.error}>{m.error}</p>
        ) : null}
      </div>
      <div className="skillsTools-tray-foot" data-tone={st.trayTone}>
        <span className="skillsTools-foot-status">
          {st.spinning ? <span className="skillsTools-spin" aria-hidden="true" /> : <StatusDot tone={st.tone} />}
          <span>{st.label}</span>
        </span>
        <span className="skillsTools-foot-cue">
          <span>{st.cue}</span>
          <Icon icon={ArrowRight01Icon} size={13} />
        </span>
      </div>
    </div>
  )
}

export function McpTab(): JSX.Element {
  const s = store.get()
  const rows = s.snap.mcp
  const connected = rows.filter((m) => mcpChip(m, store.isBusy(busyKey(m.name))) === 'connected').length

  const flip = (m: McpSnapshot, on: boolean): void => {
    if (on && m.auth && m.auth !== 'none' && !m.credentialed) void store.openMcp(m.name)
    void store.run(busyKey(m.name), () => store.source().toggleServer(m.name, on), { remount: false })
  }

  const add = (): void => store.addInChat(t('gui.skillsTools.mcp.add_prompt'))

  return (
    <div className="skillsTools-pane">
      <div className="skillsTools-toolbar">
        <div className="skillsTools-toolbar-meta">
          {rows.length > 0 ? (
            <span className="skillsTools-counter mono">{t('gui.skillsTools.mcp.counter', { on: connected, total: rows.length })}</span>
          ) : null}
          <span className="skillsTools-hint">{t('gui.skillsTools.mcp.add_hint', { path: s.snap.configPath })}</span>
        </div>
        <button type="button" className="skillsTools-btn skillsTools-btn-primary skillsTools-toolbar-end" onClick={add}>
          <Icon icon={PlusSignIcon} size={14} />
          <span>{t('gui.skillsTools.mcp.add')}</span>
        </button>
      </div>

      {rows.length === 0 ? (
        <EmptyBox
          icon={<Icon icon={Plug02Icon} size={22} />}
          title={t('gui.skillsTools.mcp.none')}
          action={{ label: t('gui.skillsTools.mcp.add'), onClick: add }}
        >
          {t('gui.skillsTools.mcp.none_sub')}
        </EmptyBox>
      ) : (
        <div className="skillsTools-grid">
          {rows.map((m) => (
            <McpCard key={m.name} m={m} onFlip={(v) => flip(m, v)} />
          ))}
        </div>
      )}
    </div>
  )
}
