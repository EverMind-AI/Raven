/* Plugins: the Agent Plugin packages (Agent Plugins 1.0, a directory with a
   plugin.json) this host can see -- what each holds, whether its skills were
   imported, and the one switch the page owns: whether its MCP servers may
   start. Importing and removing stay with `raven agent-plugins`, because both
   take a path on the host's disk and this page may be open on another
   machine. */
import { useEffect } from 'react'

import { t } from '../../../i18n/t'
import { Card, Chip, Empty, GLYPH, Grow, Row, Rov, Switch, Xrow } from '../Fields'
import { PluginsWait } from '../Skeletons'
import * as store from '../store'

import type { AgentPluginRow } from '../types'
import type { JSX, ReactNode } from 'react'

type PkgChip = 'none' | 'restart' | 'problem' | 'unimported'

const busyKey = (name: string): string => `pkg:${name}`

/* The command the reader copies onto the host's shell, so a source with a
   space in it is quoted rather than split into two arguments. */
export const importCommand = (source: string): string =>
  `raven agent-plugins import ${/\s/.test(source) ? `"${source}"` : source}`

/* One chip per row, the most pressing first. A switch flipped since the start
   leaves the running engine on the old answer, and that is the thing a reader
   would otherwise get wrong; a problem comes next; skills that were never
   imported are not in use, which a row that lists them does not say. */
export function pkgChip(p: AgentPluginRow): PkgChip {
  if (p.mcp.allowed !== p.mcp.live) return 'restart'
  if (p.problems.length || p.mcp.problems.length) return 'problem'
  if (p.skills.length && !p.skills_imported) return 'unimported'
  return 'none'
}

function ChipFor({ p }: { p: AgentPluginRow }): JSX.Element | null {
  const kind = pkgChip(p)
  if (kind === 'restart') return <Chip state="warn">{t('gui.plug.needs_restart')}</Chip>
  if (kind === 'problem') return <Chip state="warn">{t('gui.settings.plugins.problem')}</Chip>
  if (kind === 'unimported') return <Chip state="off">{t('gui.settings.plugins.unimported')}</Chip>
  return null
}

const SERVER_WORD: Record<string, string> = {
  off: 'gui.settings.plugins.server_off',
  disconnected: 'gui.settings.plugins.server_idle',
  connecting: 'gui.settings.mcp.connecting',
  connected: 'gui.settings.mcp.connected',
  auth_required: 'gui.settings.mcp.failed',
  error: 'gui.settings.mcp.failed',
}

/* `.settings-pnote` is clay unless told otherwise (it is a verdict's colour on
   the provider page), so a note that reports nothing wrong says so. */
function Note({ children, bad }: { children: ReactNode; bad?: boolean }): JSX.Element {
  return bad
    ? <Row><span className="settings-pnote settings-pbad">{children}</span></Row>
    : <Row><span className="settings-pnote" data-tone="muted">{children}</span></Row>
}

function Panel({ p }: { p: AgentPluginRow }): JSX.Element {
  const busy = store.isBusy(busyKey(p.name))
  const flip = (on: boolean): void => {
    void store.run(busyKey(p.name), async () => {
      store.set({ pkgs: await store.source().setPackageMcp(p.name, on) })
    }, { remount: false })
  }
  const switchable = p.mcp.servers.length > 0 || p.mcp.allowed
  const problems = [...p.notes, ...p.problems, ...p.mcp.problems]
  return (
    <>
      {p.description && <Note>{p.description}</Note>}
      {problems.map((text) => <Note key={text} bad>{text}</Note>)}
      {switchable && (
        <Row label={t('gui.settings.plugins.mcp_switch')} sub={t('gui.settings.plugins.mcp_switch_sub')}>
          <Switch on={p.mcp.allowed} disabled={busy} label={t('gui.settings.plugins.mcp_switch')} onChange={flip} />
        </Row>
      )}
      {/* Facts, read as a definition list beside their labels, the way the
          MCP page's drawer reads its address and tools. */}
      <dl className="settings-pfacts">
        <dt>{t('gui.settings.plugins.source')}</dt>
        <dd className="settings-pmono" title={p.source}>{p.source}</dd>
        <dt>{t('gui.settings.plugins.skills')}</dt>
        <dd>
          {t(p.skills_imported ? 'gui.settings.plugins.skills_in' : 'gui.settings.plugins.skills_out', { n: p.skills.length })}
          {p.skills.length > 0 && <span className="settings-ptnames">{p.skills.join('  ')}</span>}
        </dd>
        {p.mcp.servers.map((srv) => (
          <ServerFact key={srv.name} name={srv.name} state={srv.state} error={srv.error} />
        ))}
      </dl>
      {p.skills.length > 0 && !p.skills_imported && (
        <Note>
          {t('gui.settings.plugins.import_hint')}
          <span className="settings-ptnames">{importCommand(p.source)}</span>
        </Note>
      )}
    </>
  )
}

function ServerFact({ name, state, error }: { name: string; state: string; error?: string | null }): JSX.Element {
  return (
    <>
      <dt>{t('gui.settings.plugins.server')}</dt>
      <dd>
        <span className="settings-pmono">{name}</span>
        <span className="settings-ptnames">{t(SERVER_WORD[state] || 'gui.settings.plugins.server_idle')}</span>
        {error && <span className="settings-pbad">{` ${error}`}</span>}
      </dd>
    </>
  )
}

export function Plugins(): JSX.Element {
  const s = store.get()
  useEffect(() => {
    if (s.pkgs === null) void store.pkgsLoad()
  }, [s.pkgs])
  if (!s.pkgs) return <PluginsWait />
  const { packages, problems } = s.pkgs
  return (
    <>
      <div className="settings-crumb">
        {packages.length > 0 && <Rov>{t('gui.settings.plugins.counter', { n: packages.length })}</Rov>}
        <Grow />
        <span className="settings-addhint">{t('gui.settings.plugins.add_hint', { path: s.snap.configPath })}</span>
      </div>
      <Card>
        {!packages.length && (
          <Empty icon={GLYPH.puzzle} title={t('gui.settings.plugins.none')}>
            {t('gui.settings.plugins.none_sub')}
          </Empty>
        )}
        {packages.map((p) => (
          <Xrow
            key={p.name}
            name={<span className="mono">{p.name}</span>}
            status={<ChipFor p={p} />}
            ctl={<Rov>{p.version}</Rov>}
            panel={<Panel p={p} />}
            open={s.pkgOpen === p.name}
            onToggle={() => store.set({ pkgOpen: s.pkgOpen === p.name ? null : p.name })}
          />
        ))}
      </Card>
      {problems.length > 0 && (
        <Card title={t('gui.settings.plugins.path_problems')}>
          {problems.map((item) => (
            <Note key={item.path} bad>{`${item.path}: ${item.reason}`}</Note>
          ))}
        </Card>
      )}
    </>
  )
}
