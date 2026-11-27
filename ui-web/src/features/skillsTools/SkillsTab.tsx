/* Skills tab (Scheme C — Tactile Bento Studio): dot-grid Skill Hub hero bento,
 * 3-scope overview bento (Built-in / Global / Project), filterable toolbar,
 * project directory context bar, and grouped two-tone tray cards opening the
 * centered SKILL.md modal.
 */
import {
  AiContentGenerator01Icon,
  ArrowRight01Icon,
  Folder01Icon,
  PlusSignIcon,
  Search01Icon,
  SparklesIcon,
} from '@hugeicons/core-free-icons'

import { Icon } from '../../components/Icon'
import { t } from '../../i18n/t'
import { skillTone } from './helpers'
import { CardName, EmptyBox, IconTile, Pill, SearchBox, StatusDot, Switch } from './Parts'
import { ProjectContextBar, ProjectEmptyCard } from './ProjectSection'
import * as store from './store'

import type { SkillRow, SkillScopeFilter } from './types'
import type { JSX } from 'react'

const HUB_SWITCH = 'skillForge.router.hub.enabled'

const isBuiltin = (s: SkillRow): boolean => s.source === 'builtin'

function SkillCard({ row, projectDir }: { row: SkillRow; projectDir?: string }): JSX.Element {
  const off = row.blocked
  const cardCls = off ? 'skillsTools-tray-card skillsTools-is-off' : 'skillsTools-tray-card'
  const flip = (on: boolean): void => {
    void store.toggleSkillBlocked(row.name, !on)
  }
  const open = (): void => { void store.openSkill(row.name, projectDir) }
  return (
    <div
      className={cardCls}
      onClick={open}
    >
      <div className="skillsTools-tray-body">
        <div className="skillsTools-ctop">
          <IconTile tone={projectDir ? 'teal' : skillTone(row.name)} size="sm">
            <Icon icon={AiContentGenerator01Icon} size={15} />
          </IconTile>
          <div className="skillsTools-cmeta">
            <CardName name={row.name} mono onOpen={open} />
            <div className="skillsTools-ctags">
              {row.hub ? <Pill tone="hub">{t('gui.skillsTools.skills.hub_tag')}</Pill> : null}
              {row.always ? <Pill tone="neutral">{t('gui.skillsTools.skills.always')}</Pill> : null}
              {projectDir ? <Pill tone="teal">.agents/skills</Pill> : null}
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

function SectionHeading({ tone, label, count, sub }: {
  tone: 'builtin' | 'global' | 'project'
  label: string
  count: string
  sub: string
}): JSX.Element {
  return (
    <div className="skillsTools-scope-head">
      <span className="skillsTools-scope-dot" data-scope={tone} aria-hidden="true" />
      <span className="skillsTools-scope-title">{label}</span>
      <span className="skillsTools-scope-cnt">{count}</span>
      <span className="skillsTools-scope-sub">{sub}</span>
    </div>
  )
}

export function SkillsTab(): JSX.Element {
  const s = store.get()
  const rows = s.snap.skills
  const q = s.skq.trim().toLowerCase()
  const builtinAll = rows.filter(isBuiltin)
  const globalAll = rows.filter((r) => !isBuiltin(r))
  const hasProject = Boolean(s.projectDir)
  const projectAll = hasProject && s.projectInfo ? s.projectInfo.skills : []
  const totalAll = builtinAll.length + globalAll.length + projectAll.length
  const enabledAll = [...builtinAll, ...globalAll, ...projectAll].filter((r) => !r.blocked).length

  const matches = (r: SkillRow): boolean =>
    !q || r.name.toLowerCase().includes(q) || (r.description || '').toLowerCase().includes(q)

  const builtinFiltered = builtinAll.filter(matches)
  const globalFiltered = globalAll.filter(matches)
  const projectFiltered = projectAll.filter(matches)

  const filters: Array<{ id: SkillScopeFilter; label: string; count: string }> = [
    { id: 'all', label: t('gui.skillsTools.skills.filter_all'), count: String(totalAll) },
    { id: 'builtin', label: t('gui.skillsTools.skills.builtin'), count: String(builtinAll.length) },
    { id: 'global', label: t('gui.skillsTools.skills.global'), count: String(globalAll.length) },
    { id: 'project', label: t('gui.skillsTools.skills.project'), count: hasProject ? String(projectAll.length) : '—' },
  ]

  const showBuiltin = s.skillFilter === 'all' || s.skillFilter === 'builtin'
  const showGlobal = s.skillFilter === 'all' || s.skillFilter === 'global'
  const showProject = s.skillFilter === 'all' || s.skillFilter === 'project'

  const hubLabel = t('gui.skillsTools.skills.hub_title')
  const countOf = (list: SkillRow[]): string => {
    const onN = list.filter((r) => !r.blocked).length
    return onN === list.length ? String(list.length) : t('gui.skillsTools.skills.on_of', { on: onN, total: list.length })
  }

  const noSearchHit = Boolean(q)
    && (!showBuiltin || builtinFiltered.length === 0)
    && (!showGlobal || globalFiltered.length === 0)
    && (!showProject || projectFiltered.length === 0)

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
          <div className="skillsTools-summary-top">
            <span className="skillsTools-summary-active">
              <StatusDot tone="ok" />
              <span>{t('gui.skillsTools.skills.stat_enabled')}</span>
            </span>
            <span className="skillsTools-summary-ratio mono">{enabledAll} / {totalAll}</span>
          </div>
          <div className="skillsTools-summary-grid">
            <div className="skillsTools-stat-cell">
              <div className="skillsTools-stat-n">{builtinAll.length}</div>
              <div className="skillsTools-stat-l">
                <span className="skillsTools-scope-dot" data-scope="builtin" aria-hidden="true" />
                <span>{t('gui.skillsTools.skills.builtin')}</span>
              </div>
            </div>
            <div className="skillsTools-stat-cell">
              <div className="skillsTools-stat-n">{globalAll.length}</div>
              <div className="skillsTools-stat-l">
                <span className="skillsTools-scope-dot" data-scope="global" aria-hidden="true" />
                <span>{t('gui.skillsTools.skills.global')}</span>
              </div>
            </div>
            <div className="skillsTools-stat-cell">
              <div className={hasProject ? 'skillsTools-stat-n' : 'skillsTools-stat-n skillsTools-stat-dim'}>
                {hasProject ? projectAll.length : '—'}
              </div>
              <div className="skillsTools-stat-l">
                <span className="skillsTools-scope-dot" data-scope="project" aria-hidden="true" />
                <span>{t('gui.skillsTools.skills.project')}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="skillsTools-toolbar">
        <div className="skillsTools-filter-pills" role="group" aria-label={t('gui.skillsTools.skills.filter_aria')}>
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
        <SearchBox
          id="skillsTools-skq"
          value={s.skq}
          placeholder={t('gui.skillsTools.skills.search')}
          onChange={(v) => store.set({ skq: v })}
        />
        <button
          type="button"
          className="skillsTools-btn skillsTools-btn-primary skillsTools-toolbar-end"
          onClick={() => store.addInChat(t('gui.skillsTools.skills.add_prompt'))}
        >
          <Icon icon={PlusSignIcon} size={14} />
          <span>{t('gui.skillsTools.skills.add')}</span>
        </button>
      </div>

      {showBuiltin && builtinFiltered.length > 0 ? (
        <section className="skillsTools-sec">
          <SectionHeading
            tone="builtin"
            label={t('gui.skillsTools.skills.builtin')}
            count={countOf(builtinFiltered)}
            sub={t('gui.skillsTools.skills.builtin_sub')}
          />
          <div className="skillsTools-grid">
            {builtinFiltered.map((row) => <SkillCard key={row.name} row={row} />)}
          </div>
        </section>
      ) : null}

      {showGlobal && globalFiltered.length > 0 ? (
        <section className="skillsTools-sec">
          <SectionHeading
            tone="global"
            label={t('gui.skillsTools.skills.global')}
            count={countOf(globalFiltered)}
            sub={t('gui.skillsTools.skills.global_sub')}
          />
          <div className="skillsTools-grid">
            {globalFiltered.map((row) => <SkillCard key={row.name} row={row} />)}
          </div>
        </section>
      ) : null}

      {showProject && (!q || projectFiltered.length > 0 || s.skillFilter === 'project') ? (
        <section className="skillsTools-sec">
          <SectionHeading
            tone="project"
            label={t('gui.skillsTools.skills.project')}
            count={hasProject ? countOf(projectFiltered) : '—'}
            sub={t('gui.skillsTools.skills.project_sub')}
          />
          {hasProject ? (
            <>
              <ProjectContextBar />
              {s.projectErr ? (
                <div className="skillsTools-err" role="alert">{s.projectErr}</div>
              ) : s.projectLoading ? (
                <div className="skillsTools-note">{t('gui.skillsTools.loading')}</div>
              ) : projectFiltered.length > 0 ? (
                <div className="skillsTools-grid">
                  {projectFiltered.map((row) => (
                    <SkillCard key={`project:${row.name}`} row={row} projectDir={s.projectDir || undefined} />
                  ))}
                </div>
              ) : (
                <EmptyBox icon={<Icon icon={Folder01Icon} size={22} />}>
                  {q ? t('gui.skillsTools.skills.none') : t('gui.skillsTools.skills.project_none')}
                </EmptyBox>
              )}
            </>
          ) : (
            <ProjectEmptyCard />
          )}
        </section>
      ) : null}

      {noSearchHit ? (
        <EmptyBox icon={<Icon icon={Search01Icon} size={22} />}>
          {t('gui.skillsTools.skills.none')}
        </EmptyBox>
      ) : null}
    </div>
  )
}
