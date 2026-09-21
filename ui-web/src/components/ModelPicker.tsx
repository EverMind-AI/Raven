/* The model picker: providers on the left, that provider's models on the
 * right, a typed id at the bottom. Props only -- no store, no transport -- so
 * a caller that owns its own list opens it with that list.
 *
 * DEBT, and the plan to pay it. `features/model/ModelPicker` is the same
 * control, reimplemented: search, the two columns, the count, the tick, the
 * typed row and the empty state all correspond, class for class. The composer
 * and the settings roles card moved there; this copy survives for its last
 * caller, the agents page, because that picker's list is not the model store's
 * -- an ACP agent advertises its own choices, bucketed the way it bucketed
 * them, and none of that is in `model.options`.
 *
 * The block is one optional field, not a difference in kind: the newer picker
 * reads `source().providers()` where this one takes them as a prop, so it needs
 * an `Offer` that carries a caller-supplied list (plus this file's
 * `allowTyped`). Its `column()` already copes -- `offered` falls back to
 * `models` when nothing is `configured`, and an untagged model reads as text.
 * What makes this more than an afternoon is the verification: moving the agents
 * page's picker means re-testing the agents page, and its acceptance is not
 * part of the run that produced this file's other half.
 *
 * Until then both exist, and `page.css` keeps both sets of rules.
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
  /* Off, the panel offers listed ids only: no typed row, and Enter on an id
     nothing lists does nothing. For a caller whose write takes exact values
     and could not add a typed one to anything. */
  allowTyped?: boolean
  onClose(): void
  /* What the right column says when there is no provider to pick from. */
  emptyNote: string
  /* The control this panel hangs off. Given one, the panel floats against it
     and closes when the list underneath scrolls away from it. */
  anchor?: HTMLElement | null
}

export function ModelPicker({ title, providers, current, onPick, onClose, emptyNote, anchor, allowTyped = true }: ModelPickerProps): JSX.Element {
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
  const first = rows[0] ?? (allowTyped && q.trim() && !exact ? q.trim() : null)
  /* Placed once per opening, off the size the first paint gives it: the list
     below is filtered, not resized, so a search term never moves the panel. */
  useLayoutEffect(() => {
    if (anchor && box.current) anchorRow(box.current, anchor)
  }, [anchor])
  /* A scroll closes the panel; a resize re-places it. The two events move the
     row for different reasons: a scroll slides it out from under the panel, and
     a panel left pointing at a row that is no longer there is worse than no
     panel, while re-placing on every scroll frame is geometry to maintain for a
     gesture nobody makes while choosing. A resize leaves the row exactly where
     it was in the list and only moves the dialog around it -- the dialog is
     `min(1000px, 94vw)` wide, so narrowing the window slides the row sideways
     with its `top` unchanged -- and there the panel should follow rather than
     vanish under the reader's hands.
     The scroll test is where the anchor sits, not that a scroll happened:
     focusing the search field can itself scroll the panel a little, and a
     listener that closed on the event would close the panel on the frame it
     opened. Only `top` is compared because only `top` is what a vertical
     scroller moves; the horizontal case is the resize, which re-places. */
  useEffect(() => {
    if (!anchor) return
    let was = anchor.getBoundingClientRect().top
    const off = (): void => {
      if (Math.abs(anchor.getBoundingClientRect().top - was) > 1) onClose()
    }
    /* The baseline moves with the panel. A height resize re-centres the dialog,
       which moves the row vertically without scrolling anything; leaving the
       opening-time top behind would make the next scroll read that resize as a
       row that had slid away. And the next scroll is likely: the model list
       inside this panel is its own scroller, and the listener below is on the
       document in capture phase, so choosing a model reaches it. */
    const again = (): void => {
      if (!box.current) return
      anchorRow(box.current, anchor)
      was = anchor.getBoundingClientRect().top
    }
    document.addEventListener('scroll', off, true)
    window.addEventListener('resize', again)
    return () => {
      document.removeEventListener('scroll', off, true)
      window.removeEventListener('resize', again)
    }
  }, [anchor, onClose])
  /* Clicking off the panel closes it, which is what a menu does and what the
     prototype's own veil did. `pointerdown` rather than `click`, so the press
     that lands outside dismisses before it can also activate whatever it hit;
     the anchor is excluded because its own handler toggles, and closing here
     first would reopen on the same press. */
  useEffect(() => {
    const off = (e: PointerEvent): void => {
      const t = e.target as Node | null
      if (!t) return
      if (box.current?.contains(t)) return
      if (anchor?.contains(t)) return
      onClose()
    }
    document.addEventListener('pointerdown', off, true)
    return () => document.removeEventListener('pointerdown', off, true)
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
          {allowTyped && sel && q.trim() && !exact && (
            <>
              {rows.length > 0 && <div className="model-picker-hr" />}
              <button type="button" className="model-picker-model model-picker-add" onClick={() => onPick(q.trim(), sel.id, true)}>
                <span>{t('gui.model.pick_use', { id: q.trim() })}</span>
                <span className="model-picker-win">{t('gui.model.pick_add_to', { name: sel.name })}</span>
              </button>
            </>
          )}
          {sel && !rows.length && (!q.trim() || !allowTyped) && <div className="model-picker-empty">{t('gui.model.pick_none')}</div>}
        </div>
      </div>
    </div>
  )
}
