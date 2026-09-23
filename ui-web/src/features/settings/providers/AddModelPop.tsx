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

import { ModelTagDefs, ModelTags, TagGlyph } from '../../../components/ModelTags'
import { t } from '../../../i18n/t'
import { KIND_GLYPH, KIND_LABEL, KIND_ORDER, bareModel as bare, guessKind, modelKind, nextKind } from '../../model/types'
import { Rov } from '../Fields'
import { ModelListWait } from '../Skeletons'
import * as store from '../store'

import type { Kind, ModelHost } from '../../model/types'
import type { Sheet } from '../store'
import type { ModelCandidate, ProviderRow } from '../types'
import type { JSX } from 'react'

/* Whether this row is on the provider already. `added` is the wire's own
   answer, computed with `merge_key`; the second test covers a row the vendor's
   list never named, which reaches us with no answer at all. */
export const isAdded = (host: ModelHost, m: ModelCandidate, configured: string[]): boolean =>
  m.added || configured.some((c) => bare(host, c) === bare(host, m.id))

/* Every model this provider could serve: what the vendor named, plus what is
   already configured and the vendor did not name. The second half is what
   makes a hand-typed id count as present -- `exact` reads this list to decide
   whether the search term is already on the provider, and without it typing an
   id that is already there would offer to add it again.
   What the popover DRAWS is the unadded part of this; see `shownRows`. */
export function allRows(sheet: Sheet, configured: string[], labels?: ProviderRow['labels']): ModelCandidate[] {
  const seen = new Set(sheet.items.map((m) => bare({ id: sheet.slug }, m.id)))
  /* A model somebody added by hand is not in the vendor's list, and its kind is
     not text just because the list did not name it -- the provider row carries
     one, and filing it under Text would hide a hand-added embedding model from
     the tab that exists to find it. */
  const extra = configured.filter((m) => !seen.has(bare({ id: sheet.slug }, m)))
    .map((id): ModelCandidate => ({ id, label: id, kind: modelKind(labels?.[id]), added: true }))
  return [...sheet.items, ...extra]
}

const matches = (m: ModelCandidate, q: string): boolean =>
  !q || m.id.toLowerCase().includes(q) || (m.label || '').toLowerCase().includes(q)

/* What the popover draws: what is left to add. A model already on the provider
   is not a choice here -- it was drawn ticked, taking a line each to say
   nothing the reader can act on, and a vendor whose whole list is on already
   opened as a full page of ticks with nothing to press. Taking one off is the
   chip's job on the page behind (`ProviderDetail`, where the `x` also refuses
   while a role is using it), which is where the models the provider HAS are
   listed. */
export function shownRows(sheet: Sheet, configured: string[], labels?: ProviderRow['labels']): ModelCandidate[] {
  const q = sheet.q.trim().toLowerCase()
  return allRows(sheet, configured, labels)
    .filter((m) => !isAdded({ id: sheet.slug }, m, configured))
    .filter((m) => matches(m, q))
    .filter((m) => sheet.kind === 'all' || m.kind === sheet.kind)
}

/* 'all' first, then every kind present, in the order a reader meets them
   elsewhere. A kind with nothing in it has no tab: a zero to press is a
   promise of nothing. */
export function kindCounts(sheet: Sheet, configured: string[], labels?: ProviderRow['labels']): Array<['all' | Kind, number]> {
  const q = sheet.q.trim().toLowerCase()
  const matched = allRows(sheet, configured, labels)
    .filter((m) => !isAdded({ id: sheet.slug }, m, configured))
    .filter((m) => matches(m, q))
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
/* The id as this provider's own list would write it: without a leading prefix
   naming the provider itself, case kept. A gateway's live list qualifies every
   id with its own name -- `openrouter/anthropic/claude-opus-5.5` -- so grouping
   on the first segment filed all of them under one head called "openrouter",
   which is the provider the popover already belongs to. `bareModel` answers
   the identity question and lowercases to do it; this one is for display. */
export function ownId(host: ModelHost, id: string): string {
  const lower = id.toLowerCase()
  for (const head of host.routes?.length ? host.routes : [host.id]) {
    if (lower.startsWith(`${head.toLowerCase()}/`)) return id.slice(head.length + 1)
  }
  return id
}

export function groups(rows: ModelCandidate[], host: ModelHost): Array<[string, ModelCandidate[]]> {
  const by = new Map<string, ModelCandidate[]>()
  for (const m of rows) {
    const own = ownId(host, m.id)
    const g = own.includes('/') ? own.split('/')[0]! : ''
    if (!by.has(g)) by.set(g, [])
    by.get(g)!.push(m)
  }
  /* One group means a flat list only when that group is the unprefixed one: a
     gateway whose search narrows to a single vendor keeps its head, and with it
     the control that adds the whole vendor. */
  if (by.size === 1 && [...by.keys()][0] === '') return [['', [...by.values()][0]!]]
  return [...by.entries()].sort((a, b) => (a[0] === '' ? 1 : b[0] === '' ? -1 : a[0].localeCompare(b[0])))
}

/* What a row is called. The vendor's display name when it gave one -- "DeepSeek
   V4 Flash" -- and otherwise the id without the group's own prefix: the head
   over the row already says `deepseek`, and the raw id beside a display name
   read as the same vendor spelled twice, once capitalised and once not. */
export function rowName(m: ModelCandidate, group: string, host: ModelHost): string {
  /* A gateway's "display name" is often just its id less the gateway --
     `anthropic/claude-opus-5.5` -- so the group's prefix comes off whichever of
     the two is shown, not only off the id. */
  const text = m.label && m.label !== m.id ? m.label : ownId(host, m.id)
  return group && text.startsWith(`${group}/`) ? text.slice(group.length + 1) : text
}

function KindChip({ value, onCycle }: { value: Kind; onCycle(next: Kind): void }): JSX.Element {
  return (
    <span
      className="settings-apkind"
      role="button"
      tabIndex={0}
      aria-label={t('gui.model.add_type')}
      onClick={(e) => { e.stopPropagation(); onCycle(nextKind(value)) }}
      onKeyDown={(e) => {
        if (e.key !== 'Enter' && e.key !== ' ') return
        e.stopPropagation(); e.preventDefault()
        onCycle(nextKind(value))
      }}
    >
      <TagGlyph name={KIND_GLYPH[value]} />
      {t(KIND_LABEL[value])}
    </span>
  )
}

/* Where the popover is rendered from, and it is not the row that opens it.
   Every write this page makes bumps the store's epoch, and the section panel is
   keyed by that epoch (SettingsApp.tsx) so the forms seeded from a snapshot are
   reseeded by the redraw. A popover rendered from inside that panel is torn
   down and rebuilt by the same key -- so ticking one model scrolled its list
   back to the top and took the caret out of the search box. It portals to the
   body already; this hangs it off a box the epoch does not replace, and the
   sheet in the store is what decides whether it stands. */
export function AddModelLayer(): JSX.Element | null {
  const sheet = store.get().sheet
  if (!sheet) return null
  const p = store.get().snap.providers.find((row) => row.id === sheet.slug)
  return p ? <AddModelPop p={p} /> : null
}

export function AddModelPop({ p }: { p: ProviderRow }): JSX.Element {
  const sheet = store.get().sheet!
  const box = useRef<HTMLDivElement>(null)
  const configured = p.configured || []
  const rows = shownRows(sheet, configured, p.labels)
  const counts = kindCounts(sheet, configured, p.labels)
  const q = sheet.q.trim()
  const exact = allRows(sheet, configured, p.labels).some((m) => bare(p, m.id) === bare(p, q))
  const typedKind = sheet.typed ?? guessKind(q)

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

  /* Tick, then add. A click on a row only marks it; the write is the footer's,
     for the ticked set or for everything shown. A click that wrote at once
     made each pick its own round trip and its own redraw, and gave no way to
     look over what was about to land before it did. The picks are counted
     against what is still addable, so one that was added some other way
     meanwhile stops being counted rather than being written twice. */
  const addable = allRows(sheet, configured, p.labels).filter((m) => !isAdded(p, m, configured))
  const picked = new Set(sheet.picked.filter((id) => addable.some((m) => m.id === id)))
  const flip = (ids: string[], on: boolean): void => {
    const next = new Set(sheet.picked)
    for (const id of ids) {
      if (on) next.add(id)
      else next.delete(id)
    }
    store.sheetPatch({ picked: [...next] })
  }
  const shownOn = rows.filter((m) => picked.has(m.id)).length
  const shownState = shownOn === 0 ? 'false' : shownOn === rows.length ? 'true' : 'mixed'
  const commit = (ids: string[]): void => {
    if (!ids.length) return
    void store.run(`prov:${p.id}`, () => store.source().addModels(p.id, ids))
      .then((ok) => { if (ok) store.set({ sheet: null }) })
  }
  /* The typed row stays one press: it is an id the list does not carry, so
     there is nothing in the list to tick. */
  const addTyped = (): void => {
    void store.sheetToggleModel(p.id, q, false, typedKind)
    store.sheetPatch({ q: '', typed: null })
  }

  return createPortal((
    <div className="settings-apop" ref={box} role="dialog" aria-label={t('gui.settings.providers.add_model')}>
      {/* This popover opens with the picker shut, so it carries the sprite its
          own rows reference rather than relying on the picker's. */}
      <ModelTagDefs />
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
            if (q && !exact && !rows.length) addTyped()
            else commit([...picked])
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
        {sheet.state === 'loading' && (
          <>
            <div className="settings-apwait">{t('gui.settings.providers.fetching', { name: p.name })}</div>
            <ModelListWait />
          </>
        )}
        {/* A vendor with no list endpoint still has models -- the ones somebody
            typed in. Saying so above them beats replacing them with the
            message, which is what hid a provider's own list behind its
            failure to enumerate one. */}
        {sheet.state === 'failed' && <div className="settings-apnote">{t('gui.settings.providers.no_list')}</div>}
        {sheet.state !== 'loading' && groups(rows, p).map(([name, ids]) => {
          /* A vendor's head is that vendor's select-all, drawn as the same box
             its rows carry: all ticked, some (a dash), or none. It used to be a
             fold on the name and a bare "+" at the far edge that wrote the whole
             vendor at once -- a second way to add, with a different
             commitment, that looked like neither a tick nor a button. */
          const on = ids.filter((m) => picked.has(m.id)).length
          const state = on === 0 ? 'false' : on === ids.length ? 'true' : 'mixed'
          return (
            <div key={name || '_'} className={name ? 'settings-apgrp' : undefined}>
              {name && (
                <button
                  type="button"
                  className="settings-mgroup"
                  role="checkbox"
                  aria-checked={state}
                  aria-label={t('gui.settings.providers.pick_group', { name })}
                  onClick={() => flip(ids.map((m) => m.id), state !== 'true')}
                >
                  <span className="settings-aptick">{state === 'true' ? '✓' : state === 'mixed' ? '–' : ''}</span>
                  <span className="settings-gn">{name}</span>
                  <span className="settings-gc">{ids.length}</span>
                </button>
              )}
              {ids.map((m) => {
                const has = picked.has(m.id)
                return (
                  <button
                    key={m.id}
                    type="button"
                    className="settings-apm"
                    role="menuitemcheckbox"
                    aria-checked={has}
                    title={m.id}
                    onClick={() => flip([m.id], !has)}
                  >
                    <span className="settings-aptick">{has ? '✓' : ''}</span>
                    <span className="settings-apnm">{rowName(m, name, p)}</span>
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
        {/* Nothing left to add is not the same as nothing to add: a provider
            carrying every model its vendor lists would otherwise be told it
            has no models at all. */}
        {sheet.state !== 'loading' && !rows.length && !q && (
          <div className="settings-apwait">
            {t(configured.length ? 'gui.settings.providers.all_added' : 'gui.settings.providers.no_models_yet')}
          </div>
        )}
      </div>
      {/* One way to commit. "Select all" is a tick like every other on the list
          -- over what is shown, so a search narrows what it takes -- and the
          only button is the add. An "add all" button beside it was a second,
          unreviewed way to write, and it did not look like the rest of a list
          that is otherwise built by ticking. */}
      <div className="settings-apfoot">
        {rows.length > 0 && (
          <button
            type="button"
            className="settings-apall"
            role="checkbox"
            aria-checked={shownState}
            onClick={() => flip(rows.map((m) => m.id), shownState !== 'true')}
          >
            <span className="settings-aptick">{shownState === 'true' ? '✓' : shownState === 'mixed' ? '–' : ''}</span>
            {t('gui.settings.providers.pick_all')}
            <Rov>{String(rows.length)}</Rov>
          </button>
        )}
        <span style={{ flex: 1 }} />
        {addable.length > 0 && (
          <button type="button" className="mini go" disabled={!picked.size} onClick={() => commit([...picked])}>
            {t('gui.settings.providers.add_picked', { n: String(picked.size) })}
          </button>
        )}
      </div>
    </div>
  ), document.body)
}
