/* The first-run wizard: a full-window frame -- step strip, scrolling column,
   footer, closing fade -- around step bodies the owning domains hand it. The
   model and search steps are the settings dialog's own bodies, the agents step
   the sub-agents roster's; only the data-sync step is drawn here. */

import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

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

/* No gate on `loaded()`. The step's own body knows what it can draw before its
   data lands, and every one of them is safe on the empty snapshot the store
   starts with. Gating here cost the whole step: the frame rendered in 77ms and
   then held one line of text for the 6.4s the settings payload took, because
   the model step's body is the settings model page and that page waits on the
   provider catalogue. A reader who has just installed Raven was given a blank
   screen at the exact moment they were deciding whether the install worked. */
function Body({ body }: { body: StepBody }): JSX.Element {
  const Draw = body.Body
  return <Draw />
}

/* One row per agent the machine has: what the importer found for it and a
   switch, or the chip that says the importer cannot read that agent yet. The
   web path imports memory files only: conversations are the difference between
   minutes and hours (measured), and stay a CLI option. */
function SyncBody(): JSX.Element {
  const s = store.get()
  if (!store.syncable().length) {
    return (
      <div className="ob-card">
        <div className="ob-ch"><div className="ob-t">{t('gui.onb.sync_title')}</div></div>
        <div className="ob-empty">{t('gui.onb.sync_empty')}</div>
      </div>
    )
  }
  return (
    <div className="ob-card">
      <div className="ob-ch"><div className="ob-t">{t('gui.onb.sync_title')}</div></div>
      {s.bodies && !store.syncReady() && (
        <div className="ob-mem ob-needs">
          <div className="ob-memnote ob-at">{t('gui.onb.sync_needs_memory')}</div>
          <Body body={s.bodies.memory} />
        </div>
      )}
      {store.found().map((agent) => {
        const p = store.platformOf(agent)
        const on = !!s.syncPick[agent.id]
        return (
          <div key={agent.id} className="ob-row">
            <div className="ob-am">
              <div className="ob-at">{agent.name}</div>
              {p?.scannable ? (
                <div className="ob-ad">
                  {t('gui.onb.sync_counts', { files: p.memory_files })}
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
  )
}

const LANGS = [
  ['zh', 'gui.onb.lang_zh'],
  ['en', 'gui.onb.lang_en_name'],
] as const

/* The language as a small menu: the trigger names the current one, the list
   the two there are. Closes on a pick, a click elsewhere, or Escape. */
function LangPick({ cur }: { cur: string }): JSX.Element {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const away = (e: MouseEvent): void => { if (!box.current?.contains(e.target as Node)) setOpen(false) }
    const esc = (e: KeyboardEvent): void => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', esc)
    return () => {
      document.removeEventListener('mousedown', away)
      document.removeEventListener('keydown', esc)
    }
  }, [open])
  return (
    <div className="ob-lang" ref={box}>
      <button type="button" className="ob-langbtn" aria-haspopup="menu" aria-expanded={open}
        aria-label={t('gui.onb.lang')} onClick={() => setOpen(!open)}>
        {t(cur === 'zh' ? 'gui.onb.lang_zh' : 'gui.onb.lang_en')}
        <svg className="ob-langch" viewBox="0 0 10 10" aria-hidden="true"><path d="M2.5 4l2.5 2.5L7.5 4" /></svg>
      </button>
      {open && (
        <div className="ob-langmenu" role="menu">
          {LANGS.map(([code, key]) => (
            <button key={code} type="button" role="menuitemradio" aria-checked={cur === code} className="ob-langopt"
              onClick={() => { setOpen(false); if (cur !== code) store.setLang(code) }}>
              <span>{t(key)}</span>
              {cur === code && <span className="ob-langok" aria-hidden="true">✓</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function Wizard(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  const cur = useSyncExternalStore(lang.subscribe, lang.get).lang
  const bodies = s.bodies as NonNullable<typeof s.bodies>
  useBody(bodies.model)
  useBody(bodies.search)
  useBody(bodies.agents)
  useBody(bodies.memory)
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
  const primaryKey = last ? (s.step === 'sync' && store.syncable().length ? 'gui.onb.start_sync' : 'gui.onb.enter') : 'gui.onb.next'

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
        <LangPick cur={cur} />
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
