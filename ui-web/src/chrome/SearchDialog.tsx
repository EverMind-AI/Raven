/* The session search dialog. The rail's magnifier and the page's Cmd+K both
 * raise it; both go through `state/find.ts`, which is why the term lives there
 * rather than in this component.
 *
 * The field is uncontrolled: React never writes its value back, so the caret
 * and an input method's composition stay the browser's. What is this
 * component's own is the active row, because it is a fact about the filtered
 * list and nothing outside re-renders it.
 */

import { Search01Icon } from '@hugeicons/core-free-icons'
import { Fragment, useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { Icon } from '../components/Icon'
import { composing } from '../features/composer/store'
import * as store from '../features/rail/store'
import { t } from '../i18n/t'
import * as find from '../state/find'
import * as lang from '../state/lang'

import type { SessRow } from '../features/rail/types'
import type { JSX, KeyboardEvent as ReactKeyboardEvent } from 'react'

/* What the reader typed is text, not a pattern: a term of `(` or `*` must still
   find the row it is written in. */
function parts(text: string, term: string): Array<{ text: string; hit: boolean }> {
  if (!term) return [{ text, hit: false }]
  const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const out: Array<{ text: string; hit: boolean }> = []
  let last = 0
  for (const m of text.matchAll(new RegExp(escaped, 'gi'))) {
    const at = m.index ?? 0
    if (at > last) out.push({ text: text.slice(last, at), hit: false })
    out.push({ text: m[0], hit: true })
    last = at + m[0].length
  }
  if (!out.length) return [{ text, hit: false }]
  if (last < text.length) out.push({ text: text.slice(last), hit: false })
  return out
}

function Marked({ text, term }: { text: string; term: string }): JSX.Element {
  return (
    <>
      {parts(text, term).map((part, i) =>
        part.hit ? <mark key={i}>{part.text}</mark> : <Fragment key={i}>{part.text}</Fragment>,
      )}
    </>
  )
}

export function SearchDialog(): JSX.Element {
  const s = useSyncExternalStore(find.subscribe, find.get)
  const rail = useSyncExternalStore(store.subscribe, store.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  const [active, setActive] = useState(0)
  const field = useRef<HTMLInputElement>(null)
  const veil = useRef<HTMLDivElement>(null)
  const list = useRef<HTMLDivElement>(null)

  const rows = (rail.snap?.rows ?? []).filter(
    (r) => !s.query || r.title.toLowerCase().includes(s.query) || (r.last ?? '').toLowerCase().includes(s.query),
  )
  const at = Math.min(active, Math.max(0, rows.length - 1))
  /* Whether results are on screen, which is not the same as whether the filter
     matched: with no term typed nothing is rendered, so a key must not act on
     the rows that are merely waiting behind it. */
  const showing = Boolean(s.query && rows.length)

  useEffect(() => setActive(0), [s.query, s.open])

  useEffect(() => {
    if (s.open) {
      field.current?.focus()
      return
    }
    if (field.current) field.current.value = ''
    find.set({ query: '' })
  }, [s.open])

  /* The active row is reached by moving aria-activedescendant while focus stays
     in the field, so the browser never scrolls it in: a list taller than the
     card would otherwise let the selection walk out of sight. */
  useEffect(() => {
    const el = list.current?.children[at] as HTMLElement | undefined
    el?.scrollIntoView?.({ block: 'nearest' })
  }, [at, s.query])

  useEffect(() => {
    const el = veil.current
    if (!el) return
    const away = (e: Event): void => {
      if (e.target === el) find.toggle(false)
    }
    el.addEventListener('click', away)
    return () => el.removeEventListener('click', away)
  }, [])

  const choose = (row: SessRow | undefined): void => {
    if (!row) return
    store.openSession(row)
    find.toggle(false)
  }

  const onKey = (e: ReactKeyboardEvent<HTMLInputElement>): void => {
    /* Composition belongs to the input method: the Enter that picks a candidate
       must not also pick a conversation. */
    if (composing(e.nativeEvent)) return
    if (e.key === 'Escape') {
      e.stopPropagation()
      find.toggle(false)
      return
    }
    if (!showing) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActive((at + 1) % rows.length)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive((at - 1 + rows.length) % rows.length)
    } else if (e.key === 'Enter') {
      e.preventDefault()
      choose(rows[at])
    }
  }

  return (
    <div className="veil" id="findVeil" ref={veil} data-open={s.open ? 'true' : 'false'}>
      <div className="chrome-find-card" role="dialog" aria-modal="true" aria-label={lang.attr('gui.search_sessions')}>
        <div className="chrome-find-field">
          <span className="chrome-find-ic"><Icon icon={Search01Icon} size={16} /></span>
          <input
            id="sfind"
            ref={field}
            placeholder={t('gui.search_sessions')}
            aria-label={lang.attr('gui.search_sessions')}
            role="combobox"
            aria-autocomplete="list"
            aria-expanded={showing}
            aria-controls={showing ? 'findList' : undefined}
            aria-activedescendant={showing ? `findhit-${at}` : undefined}
            autoComplete="off"
            onInput={(e) => find.set({ query: e.currentTarget.value.trim().toLowerCase() })}
            onKeyDown={onKey}
          />
          <button
            className="chrome-find-clr"
            id="sclr"
            hidden={!s.query}
            aria-label={lang.attr('gui.clear_search')}
            onClick={() => {
              if (field.current) field.current.value = ''
              find.set({ query: '' })
              field.current?.focus()
            }}
          >
            &#10005;
          </button>
        </div>
        {s.query ? (
          <>
            <div className="chrome-find-count" id="findCount">{t('gui.find.count', { n: rows.length })}</div>
            {rows.length ? (
              <div className="chrome-find-list" id="findList" ref={list} role="listbox" aria-labelledby="findCount">
                {rows.map((r, i) => (
                  <button
                    key={r.id}
                    className="chrome-find-hit"
                    role="option"
                    id={`findhit-${i}`}
                    aria-selected={i === at}
                    onMouseEnter={() => setActive(i)}
                    onClick={() => choose(r)}
                  >
                    <span className="chrome-find-title"><Marked text={r.title} term={s.query} /></span>
                    {r.last ? <span className="chrome-find-excerpt"><Marked text={r.last} term={s.query} /></span> : null}
                  </button>
                ))}
              </div>
            ) : (
              <div className="chrome-find-empty">{t('gui.find.empty')}</div>
            )}
          </>
        ) : null}
      </div>
    </div>
  )
}
