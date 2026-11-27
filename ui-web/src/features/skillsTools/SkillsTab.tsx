/* Skills tab (Scheme C — Tactile Bento Studio): dot-grid Skill Hub hero bento,
 * 3-metric overview bento, filterable toolbar, and grouped two-tone tray cards
 * opening the centered SKILL.md modal.
 */
import {
  AiContentGenerator01Icon,
  ArrowRight01Icon,
  PlusSignIcon,
  Search01Icon,
  SparklesIcon,
} from '@hugeicons/core-free-icons'

import { Icon } from '../../components/Icon'
import { t } from '../../i18n/t'
import { skillTone } from './helpers'
import { CardName, EmptyBox, IconTile, Pill, SearchBox, StatusDot, Switch } from './Parts'
import * as store from './store'

import type { SkillRow, SkillScopeFilter } from './types'
import type { JSX } from 'react'

const HUB_SWITCH = 'skillForge.router.hub.enabled'

const isBuiltin = (s: SkillRow): boolean => s.source === 'builtin'

function SkillCard({ row }: { row: SkillRow }): JSX.Element {
  const off = row.blocked
  const cardCls = off ? 'skillsTools-tray-card skillsTools-is-off' : 'skillsTools-tray-card'
  const flip = (on: boolean): void => {
    void store.run(`block:${row.name}`, () => store.source().blockSkill(row.name, !on), { remount: false })
  }
  const open = (): void => { void store.openSkill(row.name) }
  return (
    <div
      className={cardCls}
      onClick={open}
    >
      <div className="skillsTools-tray-body">
        <div className="skillsTools-ctop">
          <IconTile tone={skillTone(row.name)} size="sm">
            <Icon icon={AiContentGenerator01Icon} size={15} />
          </IconTile>
          <div className="skillsTools-cmeta">
            <CardName name={row.name} mono onOpen={open} />
            <div className="skillsTools-ctags">
              {row.hub ? <Pill tone="hub">{t('gui.skillsTools.skills.hub_tag')}</Pill> : null}
              {row.always ? <Pill tone="neutral">{t('gui.skillsTools.skills.always')}</Pill> : null}
            </div>
          </div>
          <Switch on={!off} label={row.name} stop onChange={flip} />
        </div>
        <p className="skillsTools-cdesc">{row.description}</p>
      </div>
      <div className="skillsTools-tray-foot" data-tone={off ? 'off' : 'neutral'}>
        <span className="skillsTools-foot-status">
          <StatusDot tone={off ? 'off' : 'ok'} />
          <span>{t(off ? 'gui.skillsTools.skills.disabled' : 'gui.skillsTools.skills.active')}</span>
        </span>
        <span className="skillsTools-foot-cue">
          <span>{t('gui.skillsTools.details')}</span>
          <Icon icon={ArrowRight01Icon} size={13} />
        </span>
      </div>
    </div>
  )
}

export function SkillsTab(): JSX.Element {
  const s = store.get()
  const rows = s.snap.skills
  const q = s.skq.trim().toLowerCase()
  const builtinAll = rows.filter(isBuiltin)
  const workspaceAll = rows.filter((r) => !isBuiltin(r))
  const enabledAll = rows.filter((r) => !r.blocked).length

  const matches = (r: SkillRow): boolean =>
    !q || r.name.toLowerCase().includes(q) || (r.description || '').toLowerCase().includes(q)

  const filters: Array<{ id: SkillScopeFilter; label: string; count: number }> = [
    { id: 'all', label: t('gui.skillsTools.skills.filter_all'), count: rows.length },
    { id: 'builtin', label: t('gui.skillsTools.skills.builtin'), count: builtinAll.length },
    { id: 'workspace', label: t('gui.skillsTools.skills.workspace'), count: workspaceAll.length },
  ]

  const allGroups: Array<[string, SkillRow[]]> = []
  if (s.skillFilter !== 'workspace') {
    allGroups.push([t('gui.skillsTools.skills.builtin'), builtinAll.filter(matches)])
  }
  if (s.skillFilter !== 'builtin') {
    allGroups.push([t('gui.skillsTools.skills.workspace'), workspaceAll.filter(matches)])
  }
  const groups = allGroups.filter(([, list]) => list.length > 0)

  const hubLabel = t('gui.skillsTools.skills.hub_title')

  return (
    <div className="skillsTools-pane">
      <div className="skillsTools-bento-hero">
        <div className="skillsTools-hub-bento">
          <IconTile tone="amber" size="lg">
            <Icon icon={SparklesIcon} size={20} />
          </IconTile>
          <div className="skillsTools-hub-copy">
            <div className="skillsTools-hub-kicker">
              <span className="skillsTools-hub-name">{t('gui.skillsTools.skills.hub_card')}</span>
              <Pill tone="hub">{t('gui.skillsTools.skills.hub_badge')}</Pill>
            </div>
            <div className="skillsTools-hub-title">{hubLabel}</div>
            <p className="skillsTools-hub-sub">{t('gui.skillsTools.skills.hub_sub')}</p>
          </div>
          <Switch
            on={s.snap.skillHub.on}
            label={hubLabel}
            onChange={(next) => void store.write(HUB_SWITCH, next, { remount: false })}
          />
        </div>

        <div className="skillsTools-summary-bento">
          <div className="skillsTools-stat-cell">
            <div className="skillsTools-stat-n">{enabledAll}/{rows.length}</div>
            <div className="skillsTools-stat-l">{t('gui.skillsTools.skills.stat_enabled')}</div>
          </div>
          <div className="skillsTools-stat-cell">
            <div className="skillsTools-stat-n">{builtinAll.length}</div>
            <div className="skillsTools-stat-l">{t('gui.skillsTools.skills.stat_builtin')}</div>
          </div>
          <div className="skillsTools-stat-cell">
            <div className="skillsTools-stat-n">{workspaceAll.length}</div>
            <div className="skillsTools-stat-l">{t('gui.skillsTools.skills.stat_workspace')}</div>
          </div>
        </div>
      </div>

      <div className="skillsTools-toolbar">
        <SearchBox
          id="skillsTools-skq"
          value={s.skq}
          placeholder={t('gui.skillsTools.skills.search')}
          onChange={(v) => store.set({ skq: v })}
        />
        <div className="skillsTools-filter-pills">
          {filters.map((f) => (
            <button
              key={f.id}
              type="button"
              className="skillsTools-filter-pill"
              aria-pressed={s.skillFilter === f.id}
              onClick={() => store.set({ skillFilter: f.id })}
            >
              <span>{f.label}</span>
              <span className="skillsTools-pill-n">{f.count}</span>
            </button>
          ))}
        </div>
        <button
          type="button"
          className="skillsTools-btn skillsTools-btn-primary skillsTools-toolbar-end"
          onClick={() => store.addInChat(t('gui.skillsTools.skills.add_prompt'))}
        >
          <Icon icon={PlusSignIcon} size={14} />
          <span>{t('gui.skillsTools.skills.add')}</span>
        </button>
      </div>

      {groups.map(([label, list]) => {
        const onN = list.filter((r) => !r.blocked).length
        const count = onN === list.length
          ? String(list.length)
          : t('gui.skillsTools.skills.on_of', { on: onN, total: list.length })
        return (
          <section key={label} className="skillsTools-sec">
            <div className="skillsTools-eyebrow">
              <span>{label}</span>
              <span className="skillsTools-eyebrow-n">{count}</span>
            </div>
            <div className="skillsTools-grid">
              {list.map((row) => <SkillCard key={row.name} row={row} />)}
            </div>
          </section>
        )
      })}

      {groups.length === 0 ? (
        <EmptyBox icon={<Icon icon={Search01Icon} size={22} />}>
          {t('gui.skillsTools.skills.none')}
        </EmptyBox>
      ) : null}
    </div>
  )
}
