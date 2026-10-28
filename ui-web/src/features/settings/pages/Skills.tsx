/* Skills: every SKILL.md the registry sees, built-in and workspace, each with
   a switch over skillForge.blocklist; a row opens the skill's detail from
   skills.manage inspect. */
import { t } from '../../../i18n/t'
import { md } from '../../../lib/prose'
import * as confirm from '../../../state/confirm'
import { Card, Crumb, Empty, GLYPH, Grow, IconBtn, Row, Search, Switch, Tag } from '../Fields'
import { SkillDetailWait } from '../Skeletons'
import { dig } from '../source'
import * as store from '../store'

import type { SkillDetail, SkillRow } from '../types'
import type { JSX } from 'react'

interface ForgeBlock {
  blocklist?: string[]
  router?: { hub?: { endpoint?: unknown; enabled?: unknown } }
}

/* The block as the file spells it: `dig` reads `skillForge` or `skill_forge`. */
const forge = (raw: Record<string, unknown>): ForgeBlock | undefined => dig(raw, 'skillForge') as ForgeBlock | undefined

export function blocklist(raw: Record<string, unknown>): string[] {
  const block = forge(raw)
  return (block && block.blocklist) || []
}

/* Hub-installed skills live under the workspace whichever route put them
   there; the hub flag only decides whether they can be removed. */
const isBuiltin = (s: SkillRow): boolean => s.source === 'builtin'
const MAIN_FILE = 'SKILL.md'

/* `inspect` answers the SKILL.md path; the uninstall removes its directory. */
const skillDir = (path: unknown): string => String(path || '').replace(/[\\/]SKILL\.md$/, '')

const HUB_SWITCH = 'skillForge.router.hub.enabled'

const TRUE_BOOLS = new Set(['true', '1', 'on', 'yes'])
const FALSE_BOOLS = new Set(['false', '0', 'off', 'no'])

/* The boolean the loader gives `enabled`: pydantic reads 0, "false", "off"
   and friends as off, so Python truthiness on the raw JSON would disagree
   with the runtime. A value the schema would refuse is not consent. */
function enabledBySchema(value: unknown): boolean {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string') {
    const word = value.trim().toLowerCase()
    if (TRUE_BOOLS.has(word)) return true
    if (FALSE_BOOLS.has(word)) return false
  }
  return false
}

/* The switch's state is the effective source, not the raw flag: an endpoint
   is required, and an explicit off keeps a stored URL switched off. */
function hubOn(raw: Record<string, unknown>): boolean {
  const hub = forge(raw)?.router?.hub
  if (!hub || typeof hub.endpoint !== 'string' || !hub.endpoint) return false
  return hub.enabled === undefined ? true : enabledBySchema(hub.enabled)
}

function HubCard({ raw }: { raw: Record<string, unknown> }): JSX.Element {
  const label = t('gui.settings.skills.hub_title')
  return (
    <Card title={t('gui.settings.skills.hub_card')}>
      <Row label={label}>
        <Switch on={hubOn(raw)} label={label} onChange={(on) => void store.write(HUB_SWITCH, on)} />
      </Row>
    </Card>
  )
}

/* SKILL.md opens with a YAML block the registry reads; the person reads the
   prose under it. */
export function stripFrontmatter(body: string): string {
  const m = /^---\r?\n[\s\S]*?\r?\n---\r?\n?/.exec(body)
  return m ? body.slice(m[0].length) : body
}

const TRIGGER: Record<string, string> = {
  use_skill: 'gui.settings.skills.trig_use',
  auto_inject: 'gui.settings.skills.trig_auto',
  rpc: 'gui.settings.skills.trig_rpc',
}

function SkillSwitch({ name, off }: { name: string; off: boolean }): JSX.Element {
  const flip = (on: boolean): void => {
    const list = blocklist(store.get().snap.raw)
    const next = on ? list.filter((n) => n !== name) : [...list.filter((n) => n !== name), name]
    void store.write('skillForge.blocklist', next)
  }
  return <Switch on={!off} label={name} onChange={flip} />
}

function SkillList(): JSX.Element {
  const s = store.get()
  const q = s.skq.trim().toLowerCase()
  const blocked = new Set(blocklist(s.snap.raw))
  const matches = (r: SkillRow): boolean =>
    !q || r.name.toLowerCase().includes(q) || (r.description || '').toLowerCase().includes(q)
  const all: Array<[string, SkillRow[]]> = [
    [t('gui.settings.skills.builtin'), s.snap.skills.filter((r) => isBuiltin(r) && matches(r))],
    [t('gui.settings.skills.workspace'), s.snap.skills.filter((r) => !isBuiltin(r) && matches(r))],
  ]
  const groups = all.filter(([, rows]) => rows.length)
  return (
    <>
      {/* The search and the add button on one line. Installing is not this
          page's: use_skill fetches a Skill Hub bundle on Raven's own judgment,
          which no row here can do, so the button leads into the chat with the
          request half written (store.addInChat says how it gets there). */}
      <div className="settings-crumb">
        <Search id="skq" value={s.skq} placeholder={t('gui.settings.skills.search')} onChange={(v) => store.set({ skq: v })} />
        <button type="button" className="mini" onClick={() => store.addInChat(t('gui.settings.skills.add_prompt'))}>
          {t('gui.settings.skills.add')}
        </button>
      </div>
      <HubCard raw={s.snap.raw} />
      {groups.map(([label, rows]) => {
        const onN = rows.filter((r) => !blocked.has(r.name)).length
        const count = onN === rows.length ? String(rows.length) : t('gui.settings.skills.on_of', { on: onN, total: rows.length })
        return (
          <Card key={label} title={`${label} · ${count}`} raw>
            {rows.map((r) => (
              <div key={r.name} className={blocked.has(r.name) ? 'settings-skrow settings-off' : 'settings-skrow'}>
                <button type="button" className="settings-open" onClick={() => void store.skillOpen(r.name)}>
                  <span className="settings-nm">{r.name}</span>
                  <span className="settings-ds">{r.description}</span>
                </button>
                <SkillSwitch name={r.name} off={blocked.has(r.name)} />
              </div>
            ))}
          </Card>
        )
      })}
      {!groups.length && <Empty icon={GLYPH.nohit}>{t('gui.settings.skills.none')}</Empty>}
    </>
  )
}

function InstallLine({ d }: { d: SkillDetail }): JSX.Element | null {
  const inst = d.install as { installed_at?: string | null; version?: string; trigger?: string; score_safety?: number | null } | null | undefined
  if (!inst) return null
  const parts = [
    inst.installed_at ? new Date(inst.installed_at).toLocaleString() : '',
    inst.trigger ? t(TRIGGER[inst.trigger] || 'gui.settings.skills.trig_rpc') : '',
    typeof inst.score_safety === 'number' ? t('gui.settings.skills.safety', { n: inst.score_safety.toFixed(1) }) : '',
  ].filter(Boolean)
  return <div className="settings-skmeta">{parts.join(' · ')}</div>
}

function SkillDetailView({ name }: { name: string }): JSX.Element {
  const s = store.get()
  const row = s.snap.skills.find((r) => r.name === name)
  const d = s.detail
  const off = blocklist(s.snap.raw).includes(name)
  const back = (): void => store.set({ skill: null, detail: null })
  const uninstall = (): void => {
    confirm.ask(
      t('gui.settings.skills.uninstall_title', { name }),
      t('gui.settings.skills.uninstall_body', { path: skillDir(d && d.path) || name }),
      t('gui.settings.skills.uninstall'),
      () => { void store.run(`uninstall:${name}`, () => store.source().uninstallSkill(name)).then((ok) => { if (ok) back() }) },
    )
  }
  const inst = d && (d.install as { version?: string } | null)
  return (
    <>
      <Crumb back={t('gui.settings.nav.skills')} onBack={back} name={name} mono>
        {row && <Tag>{isBuiltin(row) ? t('gui.settings.skills.builtin') : t('gui.settings.skills.workspace')}</Tag>}
        {inst && inst.version && <Tag>{inst.version}</Tag>}
        {d && d.always && <Tag>{t('gui.settings.skills.always')}</Tag>}
        <Grow />
        {row && row.hub && (
          <button type="button" className="mini ghost danger" onClick={uninstall}>{t('gui.settings.skills.uninstall')}</button>
        )}
        <SkillSwitch name={name} off={off} />
      </Crumb>
      {!d && <SkillDetailWait />}
      {d && (
        <div className="settings-card settings-mdcard">
          <div className="settings-mdhead">
            <div className="settings-mdname">{String(d.name || name)}</div>
            <div className="settings-desc">{String(d.description || '')}</div>
            <div className="settings-ftags">
              {((d.files as string[] | undefined) || [MAIN_FILE]).map((f) => (
                <span key={f} className={f === MAIN_FILE ? 'settings-ftag settings-cur' : 'settings-ftag'}>
                  <span className="settings-fn">{f}</span>
                  <IconBtn label={t('gui.settings.skills.open_file', { file: f })} onClick={() => void store.source().openSkillFile(name, f)} />
                </span>
              ))}
            </div>
            <InstallLine d={d} />
          </div>
          <div className="settings-md prose" dangerouslySetInnerHTML={{ __html: md(stripFrontmatter(String(d.body || ''))) }} />
        </div>
      )}
    </>
  )
}

export function Skills(): JSX.Element {
  const s = store.get()
  return s.skill ? <SkillDetailView name={s.skill} /> : <SkillList />
}
