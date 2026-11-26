/* Plugins tab (Scheme C — Tactile Bento Studio): two-tone Bento cards for local
 * Agent Plugin packages, showing bundled skills, CLI import commands with copy
 * buttons, MCP server status, and the per-package MCP permission switch.
 */
import { Alert02Icon, ArrowRight01Icon, PuzzleIcon } from '@hugeicons/core-free-icons'

import { Icon } from '../../components/Icon'
import { t } from '../../i18n/t'
import { importCommand, pkgChip, pluginTone } from './helpers'
import { CopyCmd, EmptyBox, IconTile, Pill, StatusDot, Switch, WaitView } from './Parts'
import * as store from './store'

import type { AgentPluginRow } from './types'
import type { JSX } from 'react'

const busyKey = (name: string): string => `pkg:${name}`

const SERVER_WORD: Record<string, string> = {
  off: 'gui.skillsTools.plugins.server_off',
  disconnected: 'gui.skillsTools.plugins.server_idle',
  connecting: 'gui.skillsTools.mcp.connecting',
  connected: 'gui.skillsTools.mcp.connected',
  auth_required: 'gui.skillsTools.mcp.failed',
  error: 'gui.skillsTools.mcp.failed',
}

function chipMeta(p: AgentPluginRow): { pillTone: 'warn' | 'bad' | 'neutral'; label: string } | null {
  const kind = pkgChip(p)
  if (kind === 'restart') return { pillTone: 'warn', label: t('gui.plug.needs_restart') }
  if (kind === 'problem') return { pillTone: 'bad', label: t('gui.skillsTools.plugins.problem') }
  if (kind === 'unimported') return { pillTone: 'neutral', label: t('gui.skillsTools.plugins.unimported') }
  return null
}

function PluginCard({ p }: { p: AgentPluginRow }): JSX.Element {
  const busy = store.isBusy(busyKey(p.name))
  const chip = chipMeta(p)
  const switchable = p.mcp.servers.length > 0 || p.mcp.allowed
  const open = (): void => store.openPlugin(p.name)

  const flip = (on: boolean): void => {
    void store.run(busyKey(p.name), async () => {
      store.syncPkgs(await store.source().setPackageMcp(p.name, on))
    }, { remount: false })
  }

  return (
    <div
      className="skillsTools-tray-card"
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
          <IconTile tone={pluginTone(p.name)} size="md">
            <Icon icon={PuzzleIcon} size={17} />
          </IconTile>
          <div className="skillsTools-cmeta">
            <div className="skillsTools-cname mono">{p.name}</div>
            <div className="skillsTools-ctags">
              <Pill tone="mono">{p.version}</Pill>
              {chip ? <Pill tone={chip.pillTone}>{chip.label}</Pill> : <Pill tone="ok">{t('gui.skillsTools.plugins.healthy')}</Pill>}
            </div>
          </div>
        </div>

        {p.description ? <p className="skillsTools-cdesc">{p.description}</p> : null}

        <div className="skillsTools-pkg-facts">
          <div className="skillsTools-pkg-line">
            <span className="skillsTools-pkg-k">{t('gui.skillsTools.plugins.skills')}</span>
            <span className="skillsTools-pkg-v">
              {t(p.skills_imported ? 'gui.skillsTools.plugins.skills_in' : 'gui.skillsTools.plugins.skills_out', { n: p.skills.length })}
            </span>
          </div>
          {p.mcp.servers.map((srv) => (
            <div key={srv.name} className="skillsTools-pkg-line">
              <span className="skillsTools-pkg-k mono">{srv.name}</span>
              <span className="skillsTools-pkg-v">
                <StatusDot tone={srv.state === 'connected' ? 'ok' : srv.state === 'error' || srv.state === 'auth_required' ? 'bad' : 'off'} />
                <span>{t(SERVER_WORD[srv.state] || 'gui.skillsTools.plugins.server_idle')}</span>
              </span>
            </div>
          ))}
        </div>

        {p.skills.length > 0 && !p.skills_imported ? (
          <div className="skillsTools-pkg-cli">
            <div className="skillsTools-pkg-cli-hint">{t('gui.skillsTools.plugins.import_hint')}</div>
            <CopyCmd command={importCommand(p.source)} />
          </div>
        ) : null}
      </div>

      <div className="skillsTools-tray-foot" data-tone={chip?.pillTone === 'bad' ? 'bad' : chip?.pillTone === 'warn' ? 'warn' : 'neutral'}>
        {switchable ? (
          <span className="skillsTools-pkg-swrow" onClick={(e) => e.stopPropagation()}>
            <span>{t('gui.skillsTools.plugins.mcp_switch')}</span>
            <Switch on={p.mcp.allowed} disabled={busy} label={`${p.name} ${t('gui.skillsTools.plugins.mcp_switch')}`} stop onChange={flip} />
          </span>
        ) : (
          <span className="skillsTools-pkg-src mono" title={p.source}>{p.source}</span>
        )}
        <span className="skillsTools-foot-cue">
          <span>{t('gui.skillsTools.details')}</span>
          <Icon icon={ArrowRight01Icon} size={13} />
        </span>
      </div>
    </div>
  )
}

export function PluginsTab(): JSX.Element {
  const s = store.get()

  if (!s.pkgs) return <WaitView tab="plugins" />

  const { packages, problems } = s.pkgs

  return (
    <div className="skillsTools-pane">
      <div className="skillsTools-toolbar">
        <div className="skillsTools-toolbar-meta">
          {packages.length > 0 ? (
            <span className="skillsTools-counter mono">{t('gui.skillsTools.plugins.counter', { n: packages.length })}</span>
          ) : null}
          <span className="skillsTools-hint">{t('gui.skillsTools.plugins.add_hint', { path: s.snap.configPath })}</span>
        </div>
      </div>

      {packages.length === 0 ? (
        <EmptyBox icon={<Icon icon={PuzzleIcon} size={22} />} title={t('gui.skillsTools.plugins.none')}>
          {t('gui.skillsTools.plugins.none_sub')}
        </EmptyBox>
      ) : (
        <div className="skillsTools-pkg-grid">
          {packages.map((p) => <PluginCard key={p.name} p={p} />)}
        </div>
      )}

      {problems.length > 0 ? (
        <section className="skillsTools-sec">
          <div className="skillsTools-eyebrow">
            <span>{t('gui.skillsTools.plugins.path_problems')}</span>
            <span className="skillsTools-eyebrow-n">{problems.length}</span>
          </div>
          <div className="skillsTools-prob-card">
            {problems.map((item) => (
              <div key={item.path} className="skillsTools-prob-row">
                <Icon icon={Alert02Icon} size={15} />
                <span className="mono">{`${item.path}: ${item.reason}`}</span>
              </div>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  )
}
