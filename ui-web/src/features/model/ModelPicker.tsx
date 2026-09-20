/* The default-model picker popover (.mpick).
 *
 * Two columns rather than one long list: the models a provider offers are only
 * comparable against each other, and a flat list of everything put an Anthropic
 * model between two MiniMax ones. Search narrows each provider's list in place
 * -- the column stays, its count turns into a hit count, and a provider with no
 * hits dims rather than disappearing, so the shape of what is installed does not
 * move while the reader types.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'

import { ModelTagDefs, ModelTags, TagGlyph } from '../../components/ModelTags'
import { ProviderIcon, ProviderStatus } from '../../components/ProviderMark'
import { t } from '../../i18n/t'
import { clearance } from '../../lib/popover'
import * as lang from '../../state/lang'
import * as store from './store'
import { KIND_GLYPH, KIND_LABEL, guessKind } from './types'
import { KIND_ORDER } from './types'

import type { ApiProtocol, Kind, Provider } from './types'
import type { JSX } from 'react'

export function ModelApp(): JSX.Element | null {
  const at = useSyncExternalStore(store.subscribe, store.openAt)
  /* The language the page resolved, so a pick repaints this island: every word
     below is a t(key) read at render time (state/lang/store.ts). */
  useSyncExternalStore(lang.subscribe, lang.get)
  if (!at.host) return null
  /* Keyed on the anchor so a second open against a different button starts
     clean: the search term and the provider column belong to one opening. */
  return <Pick key={store.version()} />
}

function Pick(): JSX.Element {
  const at = store.openAt()
  const host = at.host as HTMLElement
  const providers: Provider[] = store.listed()
  /* What the picker marks: the chip's model, unless the opener named a different
     one to highlight (the settings control marks the default it edits). */
  const current = at.marked ?? store.current()
  const box = useRef<HTMLDivElement>(null)
  const field = useRef<HTMLInputElement>(null)
  const [query, setQuery] = useState('')
  const [prov, setProv] = useState(() => Math.max(0, providers.findIndex((p) => store.column(p).includes(current))))
  /* What a typed id would be added as. The slot's kind when a slot opened the
     picker -- typing an embedding id into the embedding row means an embedding
     model, whatever the name looks like -- and the name's own guess otherwise.
     Cleared on every keystroke so the guess follows the id being typed. */
  const [statedKind, setStatedKind] = useState<Kind | null>(null)

  /* The kind narrows each column, the term narrows what is left. A column
     emptied by the kind is not a column emptied by a search: the first says
     "nothing of this kind added here", which is a state to act on, and the
     second says "no match", which is a term to delete. */
  const narrow = (term: string): string[][] => {
    const needle = term.trim().toLowerCase()
    return providers.map((p) => {
      const rows = store.column(p)
      return needle ? rows.filter((m) => store.short(m).toLowerCase().includes(needle)) : rows
    })
  }
  const q = query.trim().toLowerCase()
  const hits = narrow(query)
  const list = hits[prov] || []
  const currentProvider = providers.find((p) => store.column(p).includes(current))
  const selected = providers[prov]
  const exact = !!selected && store.column(selected).some((m) => store.short(m).toLowerCase() === q)
  const typedKind = statedKind ?? (at.offer.title ? at.offer.kind : guessKind(query.trim()))

  /* Measured, so it has to run after the paint that gives it a size.
     documentElement metrics, not window.innerWidth -- the latter reads 0 inside
     some embedded webviews and would push the popover into the corner.

     Two placements, chosen by which surface opened it.

     The composer chip hangs the panel ABOVE, and above the whole card rather
     than above the chip: the chip is on the card's bottom bar, so clearing only
     the chip put the panel over the line the reader is typing on, and dropping
     it below put it over the bar itself. `clearance` answers with that card.

     A settings row hangs it BELOW and flips above only when the bottom edge
     would pass the viewport: in a list, below is the reading direction, and the
     row above the panel stays visible while a model is chosen. The settings
     dialog cannot clip it -- the panel is a body-level fixed element with no
     transformed ancestor, which is what makes this arithmetic plain. */
  const place = useCallback((): void => {
    const el = box.current
    if (!el) return
    const vw = document.documentElement.clientWidth
    const vh = document.documentElement.clientHeight
    const r = host.getBoundingClientRect()
    const b = el.getBoundingClientRect()
    /* From a row, the panel's right edge lines up with the row's: a settings
       row sits at the right of a dialog that is not the whole window, and
       left-aligning hung the panel off the dialog's edge. From the composer
       chip -- bottom left of a full-width card -- the left edges line up. */
    el.style.left = `${Math.max(12, Math.min(at.offer.title ? r.right - b.width : r.left, vw - b.width - 12))}px`
    if (at.offer.title) {
      const below = r.bottom + 6
      const above = r.top - 6 - b.height
      const top = below + b.height > vh - 12 && above >= 12 ? above : below
      el.style.top = `${Math.max(12, Math.min(top, vh - b.height - 12))}px`
      return
    }
    const over = clearance(host)
    const above = over.top - b.height - 8
    el.style.top = `${above >= 12 ? above : Math.min(over.bottom + 8, Math.max(12, vh - b.height - 12))}px`
  }, [host, at.offer.title])

  useLayoutEffect(() => {
    place()
    field.current?.focus()
  }, [host, place])

  /* Only for a row in a list: the composer's anchor does not move under it.
     A scroll takes the row out from under the panel, and a panel pointing at a
     row that is no longer there is worse than no panel; a resize leaves the row
     where it is in the list and only moves the dialog around it, so there the
     panel follows rather than vanishes. The scroll test is where the anchor
     SITS, not that a scroll fired: focusing the search field scrolls the panel
     a little, and closing on the event would close it on the frame it opened.
     The baseline moves with every re-placement, or the next scroll would read a
     resize as a row that had slid away. */
  useEffect(() => {
    if (!at.offer.title) return
    let was = host.getBoundingClientRect().top
    const off = (): void => {
      if (Math.abs(host.getBoundingClientRect().top - was) > 1) store.close()
    }
    const again = (): void => {
      place()
      was = host.getBoundingClientRect().top
    }
    document.addEventListener('scroll', off, true)
    window.addEventListener('resize', again)
    return () => {
      document.removeEventListener('scroll', off, true)
      window.removeEventListener('resize', again)
    }
  }, [host, at.offer.title, place])

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
      <ModelTagDefs />
      <div className="find">
        {at.offer.title ? <span className="model-slot">{at.offer.title}</span> : null}
        <span style={{ color: 'var(--faint)' }}>⌕</span>
        <input
          ref={field}
          placeholder={t('gui.picker.search_ph')}
          value={query}
          onChange={(e) => {
            const next = e.target.value
            setQuery(next)
            /* The chip follows the id being typed: a kind the reader picked
               belongs to the id they picked it for. */
            setStatedKind(null)
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
              if (first) void store.choose(first, providers[prov]!.id)
              else if (q && !exact && selected) void store.choose(query.trim(), selected.id, true, typedKind)
            }
          }}
        />
      </div>
      <div className="cols">
        <div className="provs">
          {providers.map((p, i) => (
            <div
              key={p.id}
              className={'row model-provider-row' + (hits[i]!.length ? '' : ' dim')}
            >
              {/* Always selectable, hits or none: a provider whose column is
                  empty is exactly the one the reader has to reach, because the
                  row for typing an id into lives in that column. */}
              <button
                className="model-provider-action"
                type="button"
                aria-label={p.name}
                aria-selected={i === prov}
                onClick={() => setProv(i)}
              />
              {/* A name, not a link. The row's whole job is to change the
                  column beside it, and an anchor in the middle of it sent the
                  reader out to a marketing page instead. */}
              <span className="nm">
                <ProviderIcon id={p.id} name={p.name} />
                <span>{p.name}</span>
              </span>
              <span className="ct">{String(hits[i]!.length)}</span>
              {hits[i]!.includes(current) ? <span className="tick">•</span> : null}
              <ProviderStatus connected={p.on} label={t(p.on ? 'gui.model.state.connected' : 'gui.model.not_connected')} />
            </div>
          ))}
        </div>
        <div className="model-pane">
          {currentProvider && currentProvider.models.includes(current) ? (
            <div className="model-toolbar">
              <div className="model-toolbar-copy">
                <span className="model-toolbar-label">{t('gui.picker.protocol')}</span>
                <span className="model-toolbar-model" title={store.short(current)}>{store.short(current)}</span>
              </div>
              {sourceProtocolControl(currentProvider, current)}
            </div>
          ) : null}
          <div className="models">
            {!list.length ? (
              <div className="empty">
                {q
                  ? t('gui.picker.no_match')
                  : selected && store.column(selected).length === 0
                    ? t('gui.picker.empty_kind', { kind: t(KIND_LABEL[at.offer.kind]) })
                    : t('gui.picker.empty_provider')}
              </div>
            ) : (
              list.map((m) => {
                const facts = providers[prov]?.labels?.[m]
                /* The label where the registry has one, the id where it does
                   not, and the id on the title either way: the row is 340px
                   wide with an icon row in it, and the reader who needs the
                   exact string is hovering to copy it. */
                return (
                  <button
                    key={m}
                    className="row"
                    title={facts?.label ? `${store.short(m)}${facts.description ? ` -- ${facts.description}` : ''}` : store.short(m)}
                    onClick={() => void store.choose(m, providers[prov]!.id)}
                  >
                    <span className={'nm' + (facts?.label ? ' named' : '')}>{facts?.label || store.short(m)}</span>
                    <ModelTags facts={facts} />
                    {m === current ? <span className="tick">✓</span> : null}
                  </button>
                )
              })
            )}
            {/* An id the provider does not list yet. Fetching a list and typing
                an id are two routes to one thing, not two buttons: the id is
                added to the provider and then picked, in that order, so nothing
                ever names a model its provider does not carry.
                The chip is what the person says the id IS, because no catalogue
                says: the slot's kind where a slot asked, the name's own guess
                otherwise, and either way changeable before the click. */}
            {q && !exact && selected ? (
              <>
                {list.length > 0 ? <div className="model-typed-hr" /> : null}
                <button
                  className="row model-typed"
                  onClick={() => void store.choose(query.trim(), selected.id, true, typedKind)}
                >
                  <span className="nm">{t('gui.model.pick_use', { id: query.trim() })}</span>
                  <span
                    className="model-kind"
                    role="button"
                    tabIndex={0}
                    aria-label={t('gui.model.add_type')}
                    onClick={(e) => {
                      e.stopPropagation()
                      setStatedKind(KIND_ORDER[(KIND_ORDER.indexOf(typedKind) + 1) % KIND_ORDER.length]!)
                    }}
                    onKeyDown={(e) => {
                      if (e.key !== 'Enter' && e.key !== ' ') return
                      e.stopPropagation()
                      e.preventDefault()
                      setStatedKind(KIND_ORDER[(KIND_ORDER.indexOf(typedKind) + 1) % KIND_ORDER.length]!)
                    }}
                  >
                    <TagGlyph name={KIND_GLYPH[typedKind]} />
                    {t(KIND_LABEL[typedKind])}
                  </span>
                  <span className="ct">{t('gui.model.pick_add_to', { name: selected.name })}</span>
                </button>
              </>
            ) : null}
          </div>
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

function sourceProtocolControl(provider: Provider, model: string): JSX.Element {
  const effective = store.protocolFor(provider, model)
  const explicit = provider.protocolOverrides?.[model]
  return (
    <label className="protocol-control">
      <select
        aria-label={t('gui.picker.protocol')}
        value={explicit || 'auto'}
        onChange={(event) => {
          void store.setProtocol(model, provider.id, event.target.value as ApiProtocol)
        }}
      >
        <option value="auto">{t('gui.picker.protocol_auto', { protocol: protocolName(effective) })}</option>
        <option value="chat">{t('gui.picker.protocol_chat')}</option>
        <option value="responses">{t('gui.picker.protocol_responses')}</option>
        <option value="anthropic">{t('gui.picker.protocol_anthropic')}</option>
      </select>
    </label>
  )
}

function protocolName(protocol: ApiProtocol): string {
  if (protocol === 'responses') return 'Responses'
  if (protocol === 'anthropic') return 'Anthropic'
  return 'Chat'
}
