/* The model picker (.mpick): a search box over one list, grouped by provider,
 * off the composer's model chip or under a settings row.
 *
 * One list rather than two columns, because a dropdown reads top to bottom --
 * and it has to read at the scale a gateway account brings, which is a few
 * hundred models under one head. So three things keep it short:
 *   - the models this browser picked last head the list (./recent.ts), which
 *     is the way back to the three or four a reader actually moves between;
 *   - a group past FOLD rows shows that many and a row saying how many more,
 *     and unfolds on a click -- per group, per opening;
 *   - a search narrows every group in place and drops the ones with no hit,
 *     so a term is the way to reach anything the fold hides.
 * A row is the model's name, the capabilities the registry publishes for it
 * and the window its vendor publishes (components/ModelTags). The id is on
 * the title, the provider is the head above, and a pick is a click.
 *
 * Two rows that are not models ride along when the composer opened it: the
 * sub-agent tier (state/tier.ts), a setting of the same conversation drawn
 * where the model is chosen rather than as a chip of its own, and the door to
 * the settings page.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'

import { ModelTagDefs, ModelTags } from '../../components/ModelTags'
import { ProviderIcon } from '../../components/ProviderMark'
import { t } from '../../i18n/t'
import * as lang from '../../state/lang'
import * as tier from '../../state/tier'
import { recent } from './recent'
import * as store from './store'
import { KIND_LABEL, guessKind, sameModel } from './types'

import type { Kind, Provider } from './types'
import type { JSX } from 'react'

/** How many rows a group shows before it folds. */
export const FOLD = 6

export function ModelApp(): JSX.Element | null {
  const at = useSyncExternalStore(store.subscribe, store.openAt)
  /* The language the page resolved, so a pick repaints this island: every word
     below is a t(key) read at render time (state/lang/store.ts). */
  useSyncExternalStore(lang.subscribe, lang.get)
  if (!at.host) return null
  /* Keyed on the anchor so a second open against a different button starts
     clean: the search term and the unfolded groups belong to one opening. */
  return <Pick key={store.version()} />
}

/** The tier's rising bars, as the chip used to draw them. */
const BARS = 'M6 18.5v-4M12 18.5v-9M18 18.5v-14'

/** A ring with a question mark: the one place the row explains itself. */
const HELP = 'M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 1 0 0-17ZM9.6 9.6a2.4 2.4 0 1 1 3.3 2.2c-.6.3-.9.7-.9 1.3v.5M12 16.6h.01'

/* The tier row: every rung on offer as one segmented control, the one in
   force pressed. Drawn only for the composer's opening -- a settings row edits
   the default model, which has no conversation to carry a tier -- and only
   once a catalogue has answered, for the reason state/tier.ts's draw gives.

   One explanation, behind the question mark beside the label, through the
   page's own tooltip (state/tooltip.ts reads data-tip): the rungs carry none
   of their own, because three names in a segmented control are read as one
   scale, and the sentence that matters is what the scale moves. */
function TierRow(): JSX.Element | null {
  useSyncExternalStore(tier.subscribe, tier.get)
  if (!tier.offered()) return null
  const heading = t(tier.isRanked() ? 'gui.tier.title' : 'gui.tier.mode')
  const note = tier.scopeNote()
  return (
    <div className="model-tier">
      <span className="model-tier-k">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d={BARS} /></svg>
        {heading}
        <span className="model-tier-help" data-tip={note} data-tip-wrap="" aria-label={note} role="img">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" aria-hidden="true"><path d={HELP} /></svg>
        </span>
      </span>
      <span className="model-seg" role="radiogroup" aria-label={heading}>
        {tier.options().map((row) => (
          <button
            key={row.id}
            type="button"
            role="radio"
            aria-checked={row.ticked ? 'true' : 'false'}
            onClick={() => tier.pick(row.id)}
          >
            {row.name}
          </button>
        ))}
      </span>
    </div>
  )
}

/* The word for a model: the registry's label where the provider carries one,
   the id without its vendor half otherwise -- the rule the chip follows too,
   so the chip and the row it was picked from say the same thing. */
const label = (p: Provider, m: string): string => p.labels?.[m]?.label || store.short(m)

/** The magnifier, drawn rather than typed: a glyph from the font sat off the
    input's baseline and read as a stray character. */
const SEARCH = 'M10.5 3.5a7 7 0 1 0 0 14 7 7 0 1 0 0-14ZM15.6 15.6 20.5 20.5'

/* One model row. The id stays on the title, because the row is a name in a
   340px list and the reader who needs the exact string hovers. A recent row
   names its account at the right, since the list above it is not grouped by
   one: the same id can be served by two accounts, and a pick names one. */
function Row({ p, m, current, owner, account }: { p: Provider; m: string; current: string; owner?: Provider; account?: boolean }): JSX.Element {
  /* `owner` is the account the conversation is on, where the page knows which:
     two accounts can list one id, and the model alone cannot tell their rows
     apart. Without one every account listing it is marked, which is what this
     did before the pick started naming one. */
  const ticked = (!owner || owner.id === p.id) && sameModel(p, m, current)
  return (
    <button className="row" title={store.short(m)} onClick={() => void store.choose(m, p.id)}>
      <span className="nm">{label(p, m)}</span>
      <ModelTags facts={p.labels?.[m]} />
      {account ? <span className="ct">{p.name}</span> : null}
      {ticked ? <span className="tick">✓</span> : null}
    </button>
  )
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
  const [unfolded, setUnfolded] = useState<ReadonlySet<string>>(() => new Set())

  const q = query.trim().toLowerCase()
  /* The kind narrows each group (store.column), the term narrows what is left. */
  const hits = providers.map((p) => {
    const rows = store.column(p)
    return q ? rows.filter((m) => label(p, m).toLowerCase().includes(q) || store.short(m).toLowerCase().includes(q)) : rows
  })
  const anyHit = hits.some((h) => h.length > 0)
  const firstAt = hits.findIndex((h) => h.length > 0)
  /* The account the conversation is on: the one a pick or the gateway named,
     else whichever lists the model. A slot opening the picker states its own
     pair, which outranks the conversation's. */
  const named = at.offer.current?.provider ?? store.currentProvider()
  const currentProvider = providers.find((p) => p.id === named && store.column(p).some((m) => sameModel(p, m, current)))
    ?? providers.find((p) => store.column(p).some((m) => sameModel(p, m, current)))
  /* Where a typed id goes: the provider serving the current model, which is
     the account the reader is already on, else the first listed. One row for
     it rather than one per group, because an id is added to one provider. */
  const typedTo = currentProvider ?? providers[0]
  const exact = !!typedTo && providers.some((p) => store.column(p).some((m) => store.short(m).toLowerCase() === q))
  /* What a typed id is added as: the slot's kind when a slot opened the picker
     -- an id typed into the embedding row is an embedding model, whatever the
     name looks like -- and the name's own guess otherwise. */
  const typedKind: Kind = at.offer.title ? at.offer.kind : guessKind(query.trim())
  const showTyped = !!q && !exact && !!typedTo

  /* The last picks, for the composer's opening and only while nothing is
     searched: a search is already the short way to a model. Each is shown
     only while its account is still listed and still carries it, and names
     that account at the right, because the rows above the groups belong to no
     one head. */
  const recents = !q && !at.offer.title
    ? recent().flatMap((r) => {
      const p = providers.find((x) => x.id === r.provider)
      const m = p ? store.column(p).find((x) => sameModel(p, x, r.model)) : undefined
      return p && m ? [{ p, m }] : []
    })
    : []

  /* Measured, so it has to run after the paint that gives it a size.
     documentElement metrics, not window.innerWidth -- the latter reads 0 inside
     some embedded webviews and would push the popover into the corner.

     Two placements, chosen by which surface opened it.

     The composer chip hangs the panel off itself: right edges lined up, since
     the chip is at the right of the bar and a panel left-aligned on it ran off
     the card, and the lower edge 6px above the chip's top, so the panel reads
     as the chip's own. Pinned by that lower edge, not by its top: a search
     shortens the list, and a panel placed by its top was left floating above
     the chip with a gap under it -- pinned by the bottom it shrinks upward and
     stays on the chip. It goes under the chip only when nothing fits above.

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
    el.style.left = `${Math.max(12, Math.min(r.right - b.width, vw - b.width - 12))}px`
    const below = r.bottom + 6
    const above = r.top - 6 - b.height
    if (at.offer.title) {
      const top = below + b.height > vh - 12 && above >= 12 ? above : below
      el.style.top = `${Math.max(12, Math.min(top, vh - b.height - 12))}px`
      el.style.bottom = 'auto'
      return
    }
    if (above >= 12) {
      el.style.top = 'auto'
      el.style.bottom = `${vh - r.top + 6}px`
    } else {
      el.style.top = `${Math.min(below, Math.max(12, vh - b.height - 12))}px`
      el.style.bottom = 'auto'
    }
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
    box.current?.querySelector('.model-group .tick')?.parentElement?.scrollIntoView({ block: 'nearest' })
  }, [q])

  const chooseTyped = (): void => {
    if (typedTo) void store.choose(query.trim(), typedTo.id, true, typedKind)
  }

  /* The rows a group shows: all of them while searching or once unfolded, the
     first FOLD otherwise -- plus the current model, which has to be somewhere
     the reader can see it marked whatever the fold hides. */
  const shown = (p: Provider, list: string[]): { rows: string[]; more: number } => {
    if (q || unfolded.has(p.id) || list.length <= FOLD) return { rows: list, more: 0 }
    const head = list.slice(0, FOLD)
    const cur = list.find((m) => sameModel(p, m, current))
    if (cur && !head.includes(cur)) head.push(cur)
    return { rows: head, more: list.length - head.length }
  }

  return (
    <div className="mpick" role="dialog" ref={box}>
      <ModelTagDefs />
      <div className="find">
        {at.offer.title ? <span className="model-slot">{at.offer.title}</span> : null}
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
          <path d={SEARCH} />
        </svg>
        <input
          ref={field}
          placeholder={t('gui.picker.search_ph')}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
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
              /* The first hit in reading order, whichever group it is in; an id
                 nothing lists is added and picked when there is no hit. */
              const first = hits[firstAt]?.[0]
              if (first && firstAt >= 0) void store.choose(first, providers[firstAt]!.id)
              else if (showTyped) chooseTyped()
            }
          }}
        />
      </div>
      <div className="models">
        {recents.length ? (
          <div className="model-group model-recent">
            <div className="model-group-hd"><span className="nm">{t('gui.picker.recent')}</span></div>
            {recents.map(({ p, m }) => <Row key={`${p.id}/${m}`} p={p} m={m} current={current} owner={currentProvider} account />)}
          </div>
        ) : null}
        {providers.map((p, i) => {
          const list = hits[i]!
          /* A group the search left nothing in is dropped: the term is the
             reader's way to a model, and a head with nothing under it is not
             on the way. */
          if (q && !list.length) return null
          const { rows, more } = shown(p, list)
          return (
            <div className="model-group" key={p.id} data-provider={p.id}>
              <div className="model-group-hd">
                <ProviderIcon id={p.id} name={p.name} />
                <span className="nm">{p.name}</span>
                <span className="ct">{String(list.length)}</span>
              </div>
              {rows.map((m) => <Row key={m} p={p} m={m} current={current} owner={currentProvider} />)}
              {more ? (
                <button className="row model-more" onClick={() => setUnfolded(new Set([...unfolded, p.id]))}>
                  <span className="nm">{t('gui.picker.show_all', { n: String(list.length) })}</span>
                </button>
              ) : null}
              {/* A group with nothing to list, said in the group: a provider whose
                  account works but whose list nobody has built yet is exactly the
                  one the reader has to reach, and typing an id is how. */}
              {!q && !list.length ? (
                <div className="empty">{t('gui.picker.empty_kind', { kind: t(KIND_LABEL[at.offer.kind]) })}</div>
              ) : null}
            </div>
          )
        })}
        {q && !anyHit ? <div className="empty">{t('gui.picker.no_match')}</div> : null}
        {/* An id nothing lists yet. Fetching a list and typing an id are two
            routes to one thing, not two buttons: the id is added to the
            provider and then picked, in that order, so nothing ever names a
            model its provider does not carry. */}
        {showTyped && typedTo ? (
          <button className="row model-typed" onClick={chooseTyped}>
            <span className="nm">{t('gui.model.pick_use', { id: query.trim() })}</span>
            <span className="ct">{t('gui.model.pick_add_to', { name: typedTo.name })}</span>
          </button>
        ) : null}
      </div>
      {at.scope === 'session' && !at.offer.title ? <TierRow /> : null}
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
