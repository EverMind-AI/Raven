/* The model picker: providers on the left, that provider's models on the
 * right, a typed id at the bottom. Props only -- no store, no transport -- so
 * the settings dialog opens it for a role and the composer can open it for a
 * conversation with the same component.
 *
 * A floating panel anchored to the control that opened it, hanging below it
 * where there is room and above it where there is not. The caller passes that
 * control as `anchor`; without one the panel stays where the caller put it,
 * which is what the composer wants and what a test gets by default.
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react'

import { t } from '../i18n/t'
import { anchorRow } from '../lib/popover'
import { ProviderIcon } from './ProviderMark'

import type { JSX } from 'react'

export interface PickerProvider {
  id: string
  name: string
  models: string[]
  /* What a model is called when someone named it, keyed by id. */
  labels?: Record<string, { label?: string; description?: string; context_window?: number }>
}

export interface ModelPickerProps {
  title: string
  providers: PickerProvider[]
  current: { model: string; provider: string } | null
  /* `typed` is a model the provider does not list yet: the caller decides
     whether to add it there first. */
  onPick(model: string, provider: string, typed: boolean): void
  onClose(): void
  /* What the right column says when there is no provider to pick from. */
  emptyNote: string
  /* The control this panel hangs off. Given one, the panel floats against it
     and closes when the list underneath scrolls away from it. */
  anchor?: HTMLElement | null
}

export function ModelPicker({ title, providers, current, onPick, onClose, emptyNote, anchor }: ModelPickerProps): JSX.Element {
  const box = useRef<HTMLDivElement>(null)
  const [q, setQ] = useState('')
  const want = current ? current.provider : ''
  const [prov, setProv] = useState(() => (providers.some((p) => p.id === want) ? want : (providers[0]?.id ?? '')))
  const ql = q.trim().toLowerCase()
  const hits = (p: PickerProvider): string[] => (ql ? p.models.filter((m) => m.toLowerCase().includes(ql)) : p.models)
  const withHits = providers.filter((p) => hits(p).length)
  const shown = ql && withHits.length ? withHits : providers
  const selId = shown.some((p) => p.id === prov) ? prov : (shown[0]?.id ?? '')
  const sel = shown.find((p) => p.id === selId)
  const exact = !!sel && sel.models.some((m) => m.toLowerCase() === ql)
  const rows = sel ? hits(sel) : []
  const first = rows[0] ?? (q.trim() && !exact ? q.trim() : null)
  /* Placed once per opening, off the size the first paint gives it: the list
     below is filtered, not resized, so a search term never moves the panel. */
  useLayoutEffect(() => {
    if (anchor && box.current) anchorRow(box.current, anchor)
  }, [anchor])
  /* Closed rather than followed once the row it names moves. A floating panel
     that stays put while its row slides away is pointing at the wrong row, and
     re-placing it on every scroll frame is geometry to maintain for a gesture
     nobody makes while choosing.
     The test is where the anchor sits, not that a scroll happened: focusing the
     search field can itself scroll the panel a little, and a listener that
     closed on the event would close the panel on the frame it opened. */
  useEffect(() => {
    if (!anchor) return
    const was = anchor.getBoundingClientRect().top
    const off = (): void => {
      if (Math.abs(anchor.getBoundingClientRect().top - was) > 1) onClose()
    }
    document.addEventListener('scroll', off, true)
    window.addEventListener('resize', off)
    return () => {
      document.removeEventListener('scroll', off, true)
      window.removeEventListener('resize', off)
    }
  }, [anchor, onClose])
  return (
    <div className="model-picker" role="dialog" aria-label={title} ref={box}>
      <div className="model-picker-search">
        <span className="model-picker-title">{title}</span>
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" /></svg>
        <input
          value={q}
          placeholder={t('gui.model.pick_search')}
          autoComplete="off"
          spellCheck={false}
          autoFocus
          onChange={(e) => {
            const next = e.currentTarget.value
            setQ(next)
            /* The filter moves to a provider that has a hit and stays there,
               so a typed id that matches nothing lands on that provider. */
            const nl = next.trim().toLowerCase()
            const hit = providers.filter((p) => p.models.some((m) => m.toLowerCase().includes(nl)))
            if (nl && hit.length && !hit.some((p) => p.id === prov)) setProv(hit[0]!.id)
          }}
          onKeyDown={(e) => {
            if (e.key === 'Escape') onClose()
            if (e.key === 'Enter' && sel && first) onPick(first, sel.id, !sel.models.includes(first))
          }}
        />
        <button type="button" className="model-picker-back" onClick={onClose}>{t('gui.cancel')}</button>
      </div>
      <div className="model-picker-body">
        <div className="model-picker-left">
          {shown.map((p) => (
            <button key={p.id} type="button" className="model-picker-prov" aria-current={p.id === selId} onClick={() => setProv(p.id)}>
              <ProviderIcon id={p.id} name={p.name} />
              <span>{p.name}</span><span className="model-picker-count">{hits(p).length}</span>
              {current && current.provider === p.id && <span className="model-picker-dot" />}
            </button>
          ))}
        </div>
        <div className="model-picker-right">
          {!sel && <div className="model-picker-empty">{emptyNote}</div>}
          {sel && rows.map((m) => {
            const on = !!current && current.provider === sel.id && current.model === m
            const named = sel.labels && sel.labels[m]
            const win = named && named.context_window
            return (
              <button key={m} type="button" className="model-picker-model" aria-pressed={on} onClick={() => onPick(m, sel.id, false)}>
                <span>{(named && named.label) || m}</span>
                {on && <span className="model-picker-tick">{'✓'}</span>}
                {win ? <span className="model-picker-win">{Math.floor(win / 1000)}k</span> : null}
              </button>
            )
          })}
          {sel && q.trim() && !exact && (
            <>
              {rows.length > 0 && <div className="model-picker-hr" />}
              <button type="button" className="model-picker-model model-picker-add" onClick={() => onPick(q.trim(), sel.id, true)}>
                <span>{t('gui.model.pick_use', { id: q.trim() })}</span>
                <span className="model-picker-win">{t('gui.model.pick_add_to', { name: sel.name })}</span>
              </button>
            </>
          )}
          {sel && !rows.length && !q.trim() && <div className="model-picker-empty">{t('gui.model.pick_none')}</div>}
        </div>
      </div>
    </div>
  )
}
