/* Tools tab (Scheme C — Tactile Bento Studio): configurable tools elevated into
 * a top 3-column Bento card grid (opening the centered configuration modal),
 * paired with a uniform 2-column switch matrix with category chips and search.
 */
import {
  ArrowRight01Icon,
  BrainIcon,
  Globe02Icon,
  Image02Icon,
  Video01Icon,
  VolumeHighIcon,
  Wrench01Icon,
} from '@hugeicons/core-free-icons'

import { Icon } from '../../components/Icon'
import { t } from '../../i18n/t'
import { ROLES, TOOL_ROLE_OF, disabledTools, roleValue } from '../../lib/modelRoles'
import { CONFIGURABLE_TOOLS, TOOL_GROUPS, blocker, hasPanel, isBuiltin, toolTone } from './helpers'
import { CardName, FixedSwitch, IconTile, SearchBox, StatusDot, Switch } from './Parts'
import { FETCH_KEYLESS, SEARCH_KEYLESS, WEB_VENDOR_LABEL, webVendor } from './source'
import * as store from './store'

import type { Role } from '../../lib/modelRoles'
import type { SettingsSnapshot, ToolRow } from './types'
import type { JSX } from 'react'

const GROUP_LABEL: Record<string, string> = {
  file: 'gui.skillsTools.tools.grp_file',
  knowledge: 'gui.skillsTools.tools.grp_knowledge',
  run: 'gui.skillsTools.tools.grp_run',
  net: 'gui.skillsTools.tools.grp_net',
  generate: 'gui.skillsTools.tools.grp_generate',
  collab: 'gui.skillsTools.tools.grp_collab',
  skills: 'gui.skillsTools.tools.grp_skills',
  memory: 'gui.skillsTools.tools.grp_memory',
  search: 'gui.skillsTools.tools.grp_search',
}

function ToolGlyph({ id }: { id: string }): JSX.Element {
  if (id === 'web_search' || id === 'web_fetch') return <Icon icon={Globe02Icon} size={16} />
  if (id === 'image_generate') return <Icon icon={Image02Icon} size={16} />
  if (id === 'text_to_speech') return <Icon icon={VolumeHighIcon} size={16} />
  if (id === 'video_generate') return <Icon icon={Video01Icon} size={16} />
  if (id === 'understand_media') return <Icon icon={BrainIcon} size={16} />
  return <Icon icon={Wrench01Icon} size={16} />
}

function toolSummary(
  id: string,
  isOn: boolean,
  blk: '' | 'key' | 'model',
  snap: SettingsSnapshot,
): { dot: 'ok' | 'bad' | 'warn' | 'off'; text: string; cue: string } {
  if (!isOn) {
    return { dot: 'off', text: t('gui.skillsTools.tools.off'), cue: t('gui.skillsTools.configure') }
  }
  if (id === 'web_search' || id === 'web_fetch') {
    const v = webVendor(id, snap.raw)
    const vendorLabel = WEB_VENDOR_LABEL[v] || v
    if (blk === 'key') {
      return {
        dot: 'bad',
        text: `${vendorLabel} · ${t('gui.skillsTools.tools.needs_key')}`,
        cue: t('gui.skillsTools.configure'),
      }
    }
    const keyless = (id === 'web_search' ? SEARCH_KEYLESS : FETCH_KEYLESS).has(v)
    return {
      dot: 'ok',
      text: `${vendorLabel} · ${t(keyless ? 'gui.skillsTools.tools.keyless' : 'gui.skillsTools.tools.ready')}`,
      cue: t('gui.skillsTools.change'),
    }
  }
  const roleId = TOOL_ROLE_OF[id]
  if (roleId) {
    const role = ROLES.find((r) => r.id === roleId) as Role
    const val = roleValue(role, snap)
    if (!val) {
      return {
        dot: 'bad',
        text: t('gui.skillsTools.tools.needs_model'),
        cue: t('gui.skillsTools.configure'),
      }
    }
    return { dot: 'ok', text: val.model, cue: t('gui.skillsTools.change') }
  }
  return { dot: 'ok', text: t('gui.skillsTools.tools.ready'), cue: t('gui.skillsTools.configure') }
}

function ConfigurableCard({ row, isOn, blk, snap, onFlip }: {
  row: ToolRow
  isOn: boolean
  blk: '' | 'key' | 'model'
  snap: SettingsSnapshot
  onFlip(on: boolean): void
}): JSX.Element {
  const needs = isOn && blk !== ''
  const sum = toolSummary(row.id, isOn, blk, snap)
  const open = (): void => store.openTool(row.id)
  return (
    <div
      className="skillsTools-cfg-card"
      data-needs={needs ? 'true' : undefined}
      data-off={!isOn ? 'true' : undefined}
      onClick={open}
    >
      <div className="skillsTools-ctop">
        <IconTile tone={toolTone(row.id)} size="sm">
          <ToolGlyph id={row.id} />
        </IconTile>
        <div className="skillsTools-cmeta">
          <CardName name={row.id} mono onOpen={open} />
        </div>
        <Switch on={isOn} label={row.id} stop onChange={onFlip} />
      </div>
      <p className="skillsTools-cfg-desc">{row.one}</p>
      <div className="skillsTools-cfg-foot" data-tone={needs ? 'bad' : sum.dot}>
        <span className="skillsTools-foot-status">
          <StatusDot tone={sum.dot} />
          <span className="skillsTools-cfg-val" title={sum.text}>{sum.text}</span>
        </span>
        <span className="skillsTools-foot-cue">
          <span>{sum.cue}</span>
          <Icon icon={ArrowRight01Icon} size={13} />
        </span>
      </div>
    </div>
  )
}

export function ToolsTab(): JSX.Element {
  const s = store.get()
  const raw = s.snap.raw
  const known = new Map(s.snap.tools.map((row) => [row.id, row]))
  const disabled = disabledTools(raw)
  const wanted = (id: string): boolean => !disabled.includes(id)

  const flip = (id: string, on: boolean): void => {
    const next = on ? disabled.filter((x) => x !== id) : [...disabled, id]
    if (on && blocker(id, raw, s.snap)) store.openTool(id)
    void store.write('tools.disabledTools', next, { remount: false })
  }

  let onCount = 0
  let totalCount = 0
  for (const ids of Object.values(TOOL_GROUPS)) {
    for (const id of ids) {
      const r = known.get(id)
      if (!r || isBuiltin(r)) continue
      totalCount += 1
      if (wanted(id) && !blocker(id, raw, s.snap)) onCount += 1
    }
  }

  const featured = CONFIGURABLE_TOOLS
    .map((id) => known.get(id))
    .filter((r): r is ToolRow => !!r && !isBuiltin(r))

  const groupRows = Object.entries(TOOL_GROUPS)
    .map(([group, ids]) => ({
      group,
      label: t(GROUP_LABEL[group] || 'gui.skillsTools.tools.grp_file'),
      rows: ids
        .filter((id) => !hasPanel(id))
        .map((id) => known.get(id))
        .filter((r): r is ToolRow => !!r),
    }))
    .filter((g) => g.rows.length > 0)

  const allStandard = groupRows.flatMap((g) => g.rows)
  const activeGroup = groupRows.some((g) => g.group === s.toolGroup) ? s.toolGroup : 'all'
  const q = s.toolQ.trim().toLowerCase()
  const visibleStandard = (activeGroup === 'all'
    ? allStandard
    : groupRows.find((g) => g.group === activeGroup)?.rows ?? []
  ).filter((r) => !q || r.id.toLowerCase().includes(q) || (r.one || '').toLowerCase().includes(q))

  return (
    <div className="skillsTools-pane">
      {featured.length > 0 ? (
        <section className="skillsTools-sec">
          <div className="skillsTools-sec-head">
            <div>
              <div className="skillsTools-eyebrow">
                <span>{t('gui.skillsTools.tools.featured_title')}</span>
                <span className="skillsTools-eyebrow-n">{featured.length}</span>
              </div>
              <p className="skillsTools-sec-sub">{t('gui.skillsTools.tools.featured_sub')}</p>
            </div>
            <span className="skillsTools-counter mono">{t('gui.skillsTools.tools.counter', { on: onCount, total: totalCount })}</span>
          </div>
          <div className="skillsTools-cfg-grid">
            {featured.map((row) => {
              const isOn = wanted(row.id)
              const blk = isOn ? blocker(row.id, raw, s.snap) : ''
              return (
                <ConfigurableCard
                  key={row.id}
                  row={row}
                  isOn={isOn}
                  blk={blk}
                  snap={s.snap}
                  onFlip={(v) => flip(row.id, v)}
                />
              )
            })}
          </div>
        </section>
      ) : null}

      <section className="skillsTools-sec">
        <div className="skillsTools-eyebrow">
          <span>{t('gui.skillsTools.tools.matrix_title')}</span>
          <span className="skillsTools-eyebrow-n">{allStandard.length}</span>
          {featured.length === 0 ? (
            <span className="skillsTools-eyebrow-end mono">{t('gui.skillsTools.tools.counter', { on: onCount, total: totalCount })}</span>
          ) : null}
        </div>
        <div className="skillsTools-matrix-card">
          <div className="skillsTools-matrix-head">
            <div className="skillsTools-cat-chips">
              <button
                type="button"
                className="skillsTools-chip-btn"
                aria-pressed={activeGroup === 'all'}
                onClick={() => store.set({ toolGroup: 'all' })}
              >
                <span>{t('gui.skillsTools.tools.filter_all')}</span>
                <span className="skillsTools-pill-n">{allStandard.length}</span>
              </button>
              {groupRows.map((g) => (
                <button
                  key={g.group}
                  type="button"
                  className="skillsTools-chip-btn"
                  aria-pressed={activeGroup === g.group}
                  onClick={() => store.set({ toolGroup: g.group })}
                >
                  <span>{g.label}</span>
                  <span className="skillsTools-pill-n">{g.rows.length}</span>
                </button>
              ))}
            </div>
            <SearchBox
              id="skillsTools-tq"
              value={s.toolQ}
              placeholder={t('gui.skillsTools.tools.search')}
              compact
              onChange={(v) => store.set({ toolQ: v })}
            />
          </div>

          {visibleStandard.length > 0 ? (
            <div className="skillsTools-matrix">
              {visibleStandard.map((row) => {
                const meta = isBuiltin(row)
                const isOn = !meta && wanted(row.id)
                const dim = meta || !isOn
                return (
                  <div key={row.id} className="skillsTools-mcell" data-dim={dim ? 'true' : undefined}>
                    <div className="skillsTools-mcell-info">
                      <div className="skillsTools-mid mono">{row.id}</div>
                      {row.one ? <div className="skillsTools-mdesc" title={row.one}>{row.one}</div> : null}
                    </div>
                    {meta
                      ? <FixedSwitch on={!!row.on} label={row.id} />
                      : <Switch on={isOn} label={row.id} onChange={(v) => flip(row.id, v)} />}
                  </div>
                )
              })}
            </div>
          ) : (
            <div className="skillsTools-matrix-empty">{t('gui.skillsTools.tools.none')}</div>
          )}
        </div>
      </section>
    </div>
  )
}
