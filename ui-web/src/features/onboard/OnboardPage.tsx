/* The first-run wizard: a full-window frame -- step strip, scrolling column,
   footer, closing fade -- around step bodies the owning domains hand it. The
   model and search steps are the settings dialog's own bodies, the agents step
   the sub-agents roster's; only the data-sync step is drawn here. */

import { useEffect, useRef, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import * as lang from '../../state/lang'
import * as store from './store'

import type { StepId, StepBody } from './types'
import type { JSX } from 'react'
import './styles.css'

/* Literal keys, so the i18n gate can read each one. */
const STEP_NAME: Record<StepId, string> = {
  model: 'gui.onb.step_model',
  search: 'gui.onb.step_search',
  agents: 'gui.onb.step_agents',
  sync: 'gui.onb.step_sync',
}

export function OnboardApp(): JSX.Element | null {
  const s = useSyncExternalStore(store.subscribe, store.get)
  /* The language the page resolved, so a pick repaints this island: every word
     below is a t(key) read at render time (state/lang/store.ts). */
  useSyncExternalStore(lang.subscribe, lang.get)
  if (!s.open || !s.bodies) return null
  return <Wizard key={s.epoch} />
}

/* A step body's two facts as one snapshot, so a change in either re-renders the
   frame that draws the forward button from them. */
function useBody(body: StepBody): void {
  useSyncExternalStore(body.subscribe, () => `${body.loaded() ? 1 : 0}:${body.done() ? 1 : 0}`)
}

function Body({ body }: { body: StepBody }): JSX.Element {
  if (!body.loaded()) return <div className="ob-loading">{t('gui.onb.loading')}</div>
  const Draw = body.Body
  return <Draw />
}

/* One row per agent the machine has: what the importer found for it and a
   switch, or the chip that says the importer cannot read that agent yet. Then
   the two tiers, because they are the difference between minutes and hours
   (measured: memory files in tens of minutes, conversations in many hours),
   and that is the reader's call. */
function SyncBody(): JSX.Element {
  const s = store.get()
  const counts = store.pickedCounts()
  return (
    <>
      <div className="ob-card">
        <div className="ob-ch"><div className="ob-t">{t('gui.onb.sync_title')}</div></div>
        {store.found().map((agent) => {
          const p = store.platformOf(agent)
          const on = !!s.syncPick[agent.id]
          return (
            <div key={agent.id} className="ob-row">
              <div className="ob-am">
                <div className="ob-at">{agent.name}</div>
                {p?.scannable ? (
                  <div className="ob-ad">
                    {t('gui.onb.sync_counts', { files: p.memory_files, convs: p.conversations })}
                    {p.skills ? ` · ${t('gui.onb.sync_skills', { n: p.skills })}` : ''}
                  </div>
                ) : null}
              </div>
              {p?.scannable ? (
                <button
                  type="button"
                  className="ob-switch"
                  role="switch"
                  aria-checked={on}
                  aria-label={agent.name}
                  onClick={() => store.toggleSync(agent.id)}
                />
              ) : (
                <span className="ob-chip"><span className="ob-led" />{t('gui.onb.sync_unsupported')}</span>
              )}
            </div>
          )
        })}
      </div>
      <div className="ob-card ob-tiers" role="radiogroup" aria-label={t('gui.onb.tier')}>
        <div className="ob-ch"><div className="ob-t">{t('gui.onb.tier')}</div></div>
        {([
          ['memory_files', 'gui.onb.tier_memory', t('gui.onb.tier_memory_desc', { files: counts.files })],
          ['full', 'gui.onb.tier_full', t('gui.onb.tier_full_desc', { files: counts.files, convs: counts.convs })],
        ] as const).map(([tier, nameKey, desc]) => (
          <label key={tier} className="ob-row ob-tier">
            <input
              type="radio"
              name="ob-tier"
              value={tier}
              checked={s.syncTier === tier}
              onChange={() => store.setSyncTier(tier)}
            />
            <span className="ob-am">
              <span className="ob-at">{t(nameKey)}</span>
              <span className="ob-ad">{desc}</span>
            </span>
          </label>
        ))}
      </div>
    </>
  )
}

function Wizard(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  const cur = useSyncExternalStore(lang.subscribe, lang.get).lang
  const bodies = s.bodies as NonNullable<typeof s.bodies>
  useBody(bodies.model)
  useBody(bodies.search)
  useBody(bodies.agents)
  const scroll = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const host = document.getElementById('onb')
    if (!host) return
    host.hidden = false
    delete host.dataset.off
    return () => {
      host.hidden = true
      delete host.dataset.off
    }
  }, [])

  /* The fade is the host's attribute (styles/page.css #onb[data-off]), so the
     page behind shows through while the island is still mounted. */
  useEffect(() => {
    const host = document.getElementById('onb')
    if (host && s.closing) host.dataset.off = '1'
  }, [s.closing])

  useEffect(() => {
    scroll.current?.scrollTo({ top: 0 })
  }, [s.step])

  const steps = store.visibleSteps()
  const at = steps.indexOf(s.step)
  const done = store.stepDone(s.step)
  const last = store.isLast(s.step)
  const primaryKey = last ? (s.step === 'sync' ? 'gui.onb.start_sync' : 'gui.onb.enter') : 'gui.onb.next'

  return (
    <div className="ob-app">
      <header className="ob-top">
        <div className="ob-brand">Raven</div>
        <nav className="ob-steps" aria-label={t('gui.onb.steps')}>
          {steps.map((id, i) => {
            const state = i < at ? (s.skipped[id] ? 'skipped' : 'done') : i === at ? 'current' : 'upcoming'
            const mark = state === 'done' ? '✓' : state === 'skipped' ? '-' : String(i + 1)
            return (
              <div key={id} className="ob-sitem" data-state={state}>
                <span className="ob-dot">{mark}</span>
                <span>{t(STEP_NAME[id])}</span>
              </div>
            )
          })}
        </nav>
        <div className="ob-lang">
          <button type="button" aria-pressed={cur === 'zh'} onClick={() => store.setLang('zh')}>{t('gui.onb.lang_zh')}</button>
          <button type="button" aria-pressed={cur === 'en'} onClick={() => store.setLang('en')}>{t('gui.onb.lang_en')}</button>
        </div>
      </header>
      <div className="ob-main" ref={scroll}>
        <div className="ob-col" data-step={s.step}>
          {s.step === 'sync' ? <SyncBody /> : <Body body={bodies[s.step]} />}
        </div>
      </div>
      <footer className="ob-foot">
        <div className="ob-col">
          {at > 0 ? (
            <button type="button" className="ob-btn ob-ghost" disabled={s.busy} onClick={store.back}>
              {t('gui.onb.back')}
            </button>
          ) : null}
          <span className="ob-grow" />
          {s.error ? (
            <span className="ob-err" role="alert">{s.error}</span>
          ) : bodies.model.needsRestart?.() ? (
            <span className="ob-err" role="alert">{t('gui.onb.restart')}</span>
          ) : null}
          {/* The first step has nothing to skip: without a chat model nothing runs. */}
          {at > 0 ? (
            <button type="button" className="ob-btn ob-ghost" disabled={done || s.busy} onClick={() => void store.skip()}>
              {t('gui.onb.skip')}
            </button>
          ) : null}
          <button
            type="button"
            className="ob-btn ob-primary"
            disabled={!done || s.busy}
            onClick={() => void (last ? store.finish() : store.next())}
          >
            {t(primaryKey)}
          </button>
        </div>
      </footer>
    </div>
  )
}
