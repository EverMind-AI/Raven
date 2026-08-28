/* The default-model picker popover (.mpick).
 *
 * Two columns rather than one long list: the models a provider offers are only
 * comparable against each other, and a flat list of everything put an Anthropic
 * model between two MiniMax ones. Search narrows each provider's list in place
 * -- the column stays, its count turns into a hit count, and a provider with no
 * hits dims rather than disappearing, so the shape of what is installed does not
 * move while the reader types.
 */

import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'

import { t } from '../../shell/bridge'
import * as store from './store'

import type { JSX } from 'react'

export function ModelPickerApp(): JSX.Element | null {
  const at = useSyncExternalStore(store.subscribe, store.openAt)
  if (!at.host) return null
  /* Keyed on the anchor so a second open against a different button starts
     clean: the search term and the provider column belong to one opening. */
  return <Pick key={store.version()} />
}

function Pick(): JSX.Element {
  const at = store.openAt()
  const host = at.host as HTMLElement
  const providers = store.authed()
  const current = store.current()
  const box = useRef<HTMLDivElement>(null)
  const field = useRef<HTMLInputElement>(null)
  const [query, setQuery] = useState('')
  const [prov, setProv] = useState(() => Math.max(0, providers.findIndex((p) => p.models.includes(current))))

  const narrow = (term: string): string[][] => {
    const needle = term.trim().toLowerCase()
    return providers.map((p) =>
      needle ? p.models.filter((m) => store.short(m).toLowerCase().includes(needle)) : p.models,
    )
  }
  const q = query.trim().toLowerCase()
  const hits = narrow(query)
  const list = hits[prov] || []

  /* Measured, so it has to run after the paint that gives it a size. Above the
     anchor when it fits, which is where the composer chip wants it; clamped
     into the viewport either way. documentElement metrics, not window.innerWidth
     -- the latter reads 0 inside some embedded webviews and would push the
     popover into the corner. */
  useLayoutEffect(() => {
    const el = box.current
    if (!el) return
    const vw = document.documentElement.clientWidth
    const vh = document.documentElement.clientHeight
    const r = host.getBoundingClientRect()
    const b = el.getBoundingClientRect()
    const above = r.top - b.height - 8
    el.style.left = `${Math.max(12, Math.min(r.left, vw - b.width - 12))}px`
    el.style.top = `${above >= 12 ? above : Math.min(r.bottom + 8, Math.max(12, vh - b.height - 12))}px`
    field.current?.focus()
  }, [host])

  /* Capture phase, and the anchor counts as inside: the chip's own click would
     otherwise close the popover it just opened. */
  useEffect(() => {
    const onDown = (e: PointerEvent): void => {
      const target = e.target as HTMLElement | null
      if (!target) return
      if (target.closest('.mpick') || host.contains(target)) return
      store.close()
    }
    document.addEventListener('pointerdown', onDown, true)
    return () => document.removeEventListener('pointerdown', onDown, true)
  }, [host])

  /* Scroll the chosen model into view once it is drawn, and only when nothing is
     being searched -- yanking the list while the reader types reads as a jump. */
  useEffect(() => {
    if (q) return
    box.current?.querySelector('.models .tick')?.parentElement?.scrollIntoView({ block: 'nearest' })
  }, [q, prov])

  return (
    <div className="mpick" role="dialog" ref={box}>
      <div className="find">
        <span style={{ color: 'var(--faint)' }}>⌕</span>
        <input
          ref={field}
          placeholder={t('gui.picker.search_ph')}
          value={query}
          onChange={(e) => {
            const next = e.target.value
            setQuery(next)
            /* A term that empties the selected provider moves the selection to
               the first one that still has something, rather than showing "no
               match" beside a column that plainly has hits.
               Committed here rather than derived per render, which is what the
               loop this replaced did by assigning to its index. Derived, the
               move lasted exactly as long as the term: narrow to find a model,
               delete the term to browse the rest of that provider's list, and
               the popover threw you back to the provider you were not looking
               at. A term that matches nothing moves nothing -- there is no
               better column to move to, and the models side says so. */
            const h = narrow(next)
            if (!h[prov]?.length) {
              const first = h.findIndex((x) => x.length)
              if (first >= 0) setProv(first)
            }
          }}
          onKeyDown={(e) => {
            /* The picker keeps its keys: Escape here closes the popover, and the
               document chain behind it must not also take a page down. */
            e.stopPropagation()
            if (e.nativeEvent.isComposing || e.keyCode === 229) return
            if (e.key === 'Escape') {
              e.preventDefault()
              store.close()
            }
            if (e.key === 'Enter') {
              const first = list[0]
              if (first) void store.choose(first)
            }
          }}
        />
      </div>
      <div className="cols">
        <div className="provs">
          {providers.map((p, i) => (
            <button
              key={p.id}
              className={'row' + (hits[i]!.length ? '' : ' dim')}
              aria-selected={i === prov && hits[i]!.length > 0}
              onClick={() => {
                if (hits[i]!.length) setProv(i)
              }}
            >
              <span className="nm">{p.name}</span>
              <span className="ct">{String(hits[i]!.length)}</span>
              {hits[i]!.includes(current) ? <span className="tick">•</span> : null}
            </button>
          ))}
        </div>
        <div className="models">
          {!list.length ? (
            <div className="empty">{t(q ? 'gui.picker.no_match' : 'gui.picker.empty_provider')}</div>
          ) : (
            list.map((m) => (
              <button key={m} className="row" onClick={() => void store.choose(m)}>
                <span className="nm">{store.short(m)}</span>
                {m === current ? <span className="tick">✓</span> : null}
              </button>
            ))
          )}
        </div>
      </div>
      {at.footer ? (
        <div className="foot">
          <button
            onClick={() => {
              store.close()
              store.source().openSettings()
            }}
          >
            {t('gui.picker.manage')}
          </button>
        </div>
      ) : null}
    </div>
  )
}
