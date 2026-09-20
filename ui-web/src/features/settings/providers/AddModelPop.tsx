/* Add a model to a provider: the vendor's own list, filtered by kind, plus a
 * row for an id it does not carry.
 *
 * One control, two routes. Fetching a list and typing an id are the same act --
 * "this provider should serve this model" -- so they are one popover rather
 * than a list and a separate "or type one" button; a vendor with no list
 * endpoint simply arrives on the second route.
 *
 * A click on a row writes it. The sheet this replaces collected ticks and asked
 * for a confirming press, which asks twice for one decision; the two batch
 * controls that remain -- add everything shown, add one vendor's group -- are
 * the cases where collecting IS the decision.
 *
 * The kind tabs count over the search result, not over the whole list: a row of
 * counts that ignores the term cannot say how many pressing it would leave.
 *
 * Rendered at the body rather than beside its button. The dialog carries a
 * transform (its entry animation), which makes it the containing block for a
 * `position: fixed` child AND the box that clips one -- a 470px panel opened
 * from the right-hand pane lost everything past the dialog's edge. The model
 * picker solved this by living at the body; so does this.
 */
import { useEffect, useLayoutEffect, useRef } from 'react'
import { createPortal } from 'react-dom'

import { ModelTags, TagGlyph } from '../../../components/ModelTags'
import { t } from '../../../i18n/t'
import { KIND_GLYPH, KIND_LABEL, guessKind } from '../../model/types'
import { KIND_ORDER } from '../../model/types'
import { Rov } from '../Fields'
import * as store from '../store'

import type { Kind } from '../../model/types'
import type { Sheet } from '../store'
import type { ModelCandidate, ProviderRow } from '../types'
import type { JSX } from 'react'

/* Every row the popover may draw: what the vendor named, plus what is already
   configured -- a model added by hand is in the second and not the first, and
   leaving it out would make it unremovable from here. */
export function allRows(sheet: Sheet, configured: string[]): ModelCandidate[] {
  const seen = new Set(sheet.items.map((m) => m.id))
  const extra = configured.filter((m) => !seen.has(m))
    .map((id): ModelCandidate => ({ id, label: id, kind: 'text', added: true }))
  return [...sheet.items, ...extra]
}

const matches = (m: ModelCandidate, q: string): boolean =>
  !q || m.id.toLowerCase().includes(q) || (m.label || '').toLowerCase().includes(q)

export function shownRows(sheet: Sheet, configured: string[]): ModelCandidate[] {
  const q = sheet.q.trim().toLowerCase()
  return allRows(sheet, configured)
    .filter((m) => matches(m, q))
    .filter((m) => sheet.kind === 'all' || m.kind === sheet.kind)
}

/* 'all' first, then every kind present, in the order a reader meets them
   elsewhere. A kind with nothing in it has no tab: a zero to press is a
   promise of nothing. */
export function kindCounts(sheet: Sheet, configured: string[]): Array<['all' | Kind, number]> {
  const q = sheet.q.trim().toLowerCase()
  const matched = allRows(sheet, configured).filter((m) => matches(m, q))
  const out: Array<['all' | Kind, number]> = [['all', matched.length]]
  for (const kind of KIND_ORDER) {
    const n = matched.filter((m) => m.kind === kind).length
    if (n) out.push([kind, n])
  }
  return out
}

/* A gateway's ids are `vendor/model`, so they group; a direct vendor's are not,
   so they do not. Rows with no prefix trail the labelled groups rather than
   sitting between two of them, where they read as the previous group's tail. */
export function groups(rows: ModelCandidate[]): Array<[string, ModelCandidate[]]> {
  const by = new Map<string, ModelCandidate[]>()
  for (const m of rows) {
    const g = m.id.includes('/') ? m.id.split('/')[0]! : ''
    if (!by.has(g)) by.set(g, [])
    by.get(g)!.push(m)
  }
  if (by.size === 1) return [['', [...by.values()][0]!]]
  return [...by.entries()].sort((a, b) => (a[0] === '' ? 1 : b[0] === '' ? -1 : a[0].localeCompare(b[0])))
}

function KindChip({ value, onCycle }: { value: Kind; onCycle(next: Kind): void }): JSX.Element {
  return (
    <span
      className="settings-apkind"
      role="button"
      tabIndex={0}
      aria-label={t('gui.model.add_type')}
      onClick={(e) => { e.stopPropagation(); onCycle(KIND_ORDER[(KIND_ORDER.indexOf(value) + 1) % KIND_ORDER.length]!) }}
      onKeyDown={(e) => {
        if (e.key !== 'Enter' && e.key !== ' ') return
        e.stopPropagation(); e.preventDefault()
        onCycle(KIND_ORDER[(KIND_ORDER.indexOf(value) + 1) % KIND_ORDER.length]!)
      }}
    >
      <TagGlyph name={KIND_GLYPH[value]} />
      {t(KIND_LABEL[value])}
    </span>
  )
}

export function AddModelPop({ p }: { p: ProviderRow }): JSX.Element {
  const sheet = store.get().sheet!
  const box = useRef<HTMLDivElement>(null)
  const configured = p.configured || []
  const rows = shownRows(sheet, configured)
  const counts = kindCounts(sheet, configured)
  const q = sheet.q.trim()
  const exact = allRows(sheet, configured).some((m) => m.id.toLowerCase() === q.toLowerCase())
  const typedKind = sheet.typed ?? guessKind(q)
  const pending = rows.filter((m) => !configured.includes(m.id))

  /* Below the button it hangs off, flipping above only when the room below is
     too short to be useful and there is more of it above. The height follows
     the room rather than the list, so the popover never grows past the dialog
     and then scrolls the page. */
  useLayoutEffect(() => {
    const el = box.current
    if (!el) return
    const anchor = document.querySelector<HTMLElement>(`[data-addmodel="${p.id}"]`)
    if (!anchor) return
    const a = anchor.getBoundingClientRect()
    const vh = document.documentElement.clientHeight
    const vw = document.documentElement.clientWidth
    const below = vh - a.bottom - 18
    const above = a.top - 18
    const up = below < 220 && above > below
    el.style.maxHeight = `${Math.max(160, Math.min(520, up ? above : below))}px`
    const h = el.offsetHeight
    el.style.top = `${Math.max(12, Math.min(up ? a.top - 6 - h : a.bottom + 6, vh - h - 12))}px`
    el.style.left = `${Math.max(12, Math.min(a.right - el.offsetWidth, vw - el.offsetWidth - 12))}px`
  }, [p.id, rows.length, sheet.state, sheet.kind])

  /* Escape and a click outside close it; the button that opened it is inside,
     so its own click does not reach here. */
  useEffect(() => {
    const away = (e: PointerEvent): void => {
      const target = e.target as HTMLElement | null
      if (!target || target.closest('.settings-apop') || target.closest(`[data-addmodel="${p.id}"]`)) return
      store.set({ sheet: null })
    }
    const key = (e: KeyboardEvent): void => { if (e.key === 'Escape') store.set({ sheet: null }) }
    document.addEventListener('pointerdown', away, true)
    document.addEventListener('keydown', key)
    return () => {
      document.removeEventListener('pointerdown', away, true)
      document.removeEventListener('keydown', key)
    }
  }, [p.id])

  const toggle = (m: ModelCandidate): void => {
    void store.sheetToggleModel(p.id, m.id, configured.includes(m.id))
  }
  const addTyped = (): void => {
    void store.sheetToggleModel(p.id, q, false, typedKind)
    store.sheetPatch({ q: '', typed: null })
  }
  const addMany = (ids: string[]): void => {
    if (ids.length) void store.run(`prov:${p.id}`, () => store.source().addModels(p.id, ids))
  }

  return createPortal((
    <div className="settings-apop" ref={box} role="dialog" aria-label={t('gui.settings.providers.add_model')}>
      <div className="settings-apsearch">
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" /></svg>
        <input
          value={sheet.q}
          placeholder={t('gui.model.add_ph')}
          aria-label={t('gui.model.add_ph')}
          autoComplete="off"
          spellCheck={false}
          autoFocus
          onChange={(e) => store.sheetPatch({ q: e.currentTarget.value, typed: null })}
          onKeyDown={(e) => {
            if (e.key !== 'Enter') return
            const first = pending[0]
            if (first) toggle(first)
            else if (q && !exact) addTyped()
          }}
        />
      </div>
      {counts.length > 2 && (
        <div className="settings-apkinds" role="tablist">
          {counts.map(([kind, n]) => (
            <button
              key={kind}
              type="button"
              className="settings-mkind"
              aria-pressed={sheet.kind === kind}
              onClick={() => store.sheetPatch({ kind })}
            >
              {kind === 'all' ? null : <TagGlyph name={KIND_GLYPH[kind]} />}
              {kind === 'all' ? t('gui.model.kind_all') : t(KIND_LABEL[kind])}
              <i>{n}</i>
            </button>
          ))}
        </div>
      )}
      <div className="settings-apbody">
        {sheet.state === 'loading' && <div className="settings-apwait">{t('gui.settings.providers.fetching', { name: p.name })}</div>}
        {/* A vendor with no list endpoint still has models -- the ones somebody
            typed in. Saying so above them beats replacing them with the
            message, which is what hid a provider's own list behind its
            failure to enumerate one. */}
        {sheet.state === 'failed' && <div className="settings-apnote">{t('gui.settings.providers.no_list')}</div>}
        {sheet.state !== 'loading' && groups(rows).map(([name, ids]) => {
          const folded = !!sheet.folded[name]
          const unadded = ids.filter((m) => !configured.includes(m.id))
          return (
            <div key={name || '_'}>
              {name && (
                <div className="settings-mgroup">
                  <button
                    type="button"
                    className="settings-gt"
                    aria-expanded={!folded}
                    onClick={() => store.sheetPatch({ folded: { ...sheet.folded, [name]: !folded } })}
                  >
                    <span className="settings-gn">{name}</span>
                    <span className="settings-gc">{ids.length}</span>
                  </button>
                  <button
                    type="button"
                    className="settings-ga"
                    aria-label={t('gui.model.add_group')}
                    title={t('gui.model.add_group')}
                    disabled={!unadded.length}
                    onClick={() => addMany(unadded.map((m) => m.id))}
                  >
                    {'+'}
                  </button>
                </div>
              )}
              {!folded && ids.map((m) => {
                const has = configured.includes(m.id)
                return (
                  <button
                    key={m.id}
                    type="button"
                    className="settings-apm"
                    role="menuitemcheckbox"
                    aria-checked={has}
                    title={m.id}
                    onClick={() => toggle(m)}
                  >
                    <span className="settings-aptick">{has ? '✓' : ''}</span>
                    <span className="settings-apnm">{m.label || m.id}</span>
                    <ModelTags facts={m} />
                  </button>
                )
              })}
            </div>
          )
        })}
        {sheet.state !== 'loading' && q && !exact && (
          <>
            {rows.length > 0 && <div className="settings-aphr" />}
            <button type="button" className="settings-apm settings-apadd" onClick={addTyped}>
              <span className="settings-aptick">{'+'}</span>
              <span className="settings-apnm">{q}</span>
              <KindChip value={typedKind} onCycle={(next) => store.sheetPatch({ typed: next })} />
              <span className="settings-apsd">{t('gui.model.add_id')}</span>
            </button>
          </>
        )}
        {sheet.state !== 'loading' && !rows.length && !q && (
          <div className="settings-apwait">{t('gui.settings.providers.no_models_yet')}</div>
        )}
      </div>
      <div className="settings-apfoot">
        <Rov>{t('gui.settings.providers.showing_added', { n: String(rows.length), m: String(configured.length) })}</Rov>
        <span style={{ flex: 1 }} />
        {rows.length > 0 && (
          <button type="button" className="mini ghost" disabled={!pending.length} onClick={() => addMany(pending.map((m) => m.id))}>
            {t('gui.model.add_all', { n: String(pending.length) })}
          </button>
        )}
      </div>
    </div>
  ), document.body)
}
