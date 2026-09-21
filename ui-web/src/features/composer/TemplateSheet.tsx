/* The template picker's interior: the head every rack sheet opens with, a line
 * saying what picking does, and the templates as a grid of covers. A cover
 * opens that template's pages -- a strip the reader flips through with the
 * arrows, the keyboard or a swipe -- and the template is picked from there,
 * not from the grid: a cover says what the house looks like, the pages say
 * whether it has the shapes the deck needs.
 *
 * The state (which template is open, its pages) lives here rather than in the
 * opener: the opener repaints the sheet whenever the cover list changes, and
 * a repaint must not close the pages the reader is looking at. It is not a
 * draft, so it is not in state/sheetDrafts.ts: a reader who leaves for another
 * conversation and comes back finds the grid again, which is the intended
 * place to resume a choice from.
 *
 * The sheet element and its key handler belong to features/composer/templates.ts.
 */
import { useEffect, useRef, useState } from 'react'

import { SheetHead } from './AskApproveSheet'

import type { TemplateRow } from './types'
import type { JSX } from 'react'

export interface TemplateWords {
  readonly title: string
  readonly close: string
  readonly hint: string
  readonly back: string
  readonly use: string
  readonly pagesLoading: string
  readonly pagesNone: string
  /** `{n}` and `{total}` are filled in. */
  readonly page: string
  readonly prev: string
  readonly next: string
}

export interface TemplateSheetProps {
  readonly words: TemplateWords
  /** What the grid says when there is nothing to show: loading, none installed, or unavailable. */
  readonly empty: string
  /** null while the list is still on its way. */
  readonly rows: readonly TemplateRow[] | null
  readonly pages: (name: string) => Promise<string[]>
  readonly onClose: () => void
  readonly onPick: (row: TemplateRow) => void
}

const CHEVRON_L = 'M15 6l-6 6 6 6'
const CHEVRON_R = 'M9 6l6 6-6 6'

function Arrow({ d, label, onClick, disabled }: {
  d: string; label: string; onClick: () => void; disabled: boolean
}): JSX.Element {
  return (
    <button className="cp-tpl-arrow" type="button" aria-label={label} disabled={disabled} onClick={onClick}>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><path d={d} /></svg>
    </button>
  )
}

function Card({ row, onOpen }: { row: TemplateRow; onOpen: (row: TemplateRow) => void }): JSX.Element {
  return (
    <button className="cp-tpl-card" type="button" title={row.label} aria-label={row.label} onClick={() => onOpen(row)}>
      <span className="cp-tpl-coverbox">
        {row.cover
          ? <img className="cp-tpl-cover" src={row.cover} alt="" />
          : <span className="cp-tpl-cover cp-tpl-empty">{row.label}</span>}
      </span>
    </button>
  )
}

/* The open template: its pages side by side in a strip that snaps a page at a
   time. Scrolling IS the paging -- a swipe, a trackpad, the arrows and the
   keys all move the same strip -- and the counter reads the strip back. */
function Pages({ row, words, pages, onBack, onUse }: {
  row: TemplateRow
  words: TemplateWords
  pages: (name: string) => Promise<string[]>
  onBack: () => void
  onUse: () => void
}): JSX.Element {
  const [urls, setUrls] = useState<string[] | null>(null)
  const [at, setAt] = useState(0)
  const strip = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    let live = true
    setUrls(null)
    setAt(0)
    pages(row.name).then((u) => { if (live) setUrls(u) }, () => { if (live) setUrls([]) })
    return () => { live = false }
  }, [row.name, pages])

  useEffect(() => { strip.current?.focus() }, [urls])

  const total = urls ? urls.length : 0
  const go = (n: number): void => {
    const el = strip.current
    if (!el || !total) return
    const to = Math.max(0, Math.min(total - 1, n))
    el.scrollTo({ left: to * el.clientWidth, behavior: 'smooth' })
    setAt(to)
  }
  const onScroll = (): void => {
    const el = strip.current
    if (!el || !el.clientWidth) return
    setAt(Math.round(el.scrollLeft / el.clientWidth))
  }
  const onKey = (e: React.KeyboardEvent): void => {
    if (e.key === 'ArrowLeft') { e.preventDefault(); go(at - 1) }
    else if (e.key === 'ArrowRight') { e.preventDefault(); go(at + 1) }
    else if (e.key === 'Enter') { e.preventDefault(); onUse() }
  }
  const counter = words.page.replace('{n}', String(total ? at + 1 : 0)).replace('{total}', String(total))

  return (
    <div className="cp-tpl-pages">
      <div className="cp-tpl-bar">
        <button className="cp-tpl-back" type="button" onClick={onBack}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><path d={CHEVRON_L} /></svg>
          {words.back}
        </button>
        <span className="cp-tpl-count" aria-live="polite">{urls ? counter : words.pagesLoading}</span>
        <button className="cp-tpl-use" type="button" onClick={onUse}>{words.use}</button>
      </div>
      <div className="cp-tpl-view">
        <Arrow d={CHEVRON_L} label={words.prev} onClick={() => go(at - 1)} disabled={at <= 0} />
        <div className="cp-tpl-strip" ref={strip} tabIndex={0} onScroll={onScroll} onKeyDown={onKey}
          role="group" aria-label={row.label}>
          {urls === null
            ? <span className="cp-tpl-cover cp-tpl-empty" aria-hidden="true">{row.label}</span>
            : urls.length
              ? urls.map((u, i) => <img key={i} src={u} alt={`${row.label} ${i + 1}`} draggable={false} />)
              : <span className="cp-tpl-cover cp-tpl-empty">{words.pagesNone}</span>}
        </div>
        <Arrow d={CHEVRON_R} label={words.next} onClick={() => go(at + 1)} disabled={!total || at >= total - 1} />
      </div>
    </div>
  )
}

export function TemplateSheet(
  { words, empty, rows, pages, onClose, onPick }: TemplateSheetProps,
): JSX.Element {
  const [open, setOpen] = useState<TemplateRow | null>(null)
  /* The open template is a row the list may have repainted since; what is
     shown is the freshest copy of it, so a cover arriving late still lands. */
  const current = open && rows ? rows.find((r) => r.name === open.name) || open : open
  return (
    <>
      <SheetHead title={current ? current.label : words.title} deny={words.close} onDeny={onClose} />
      <div className="body">
        {current
          ? <Pages row={current} words={words} pages={pages} onBack={() => setOpen(null)} onUse={() => onPick(current)} />
          : (
            <>
              <div className="what">{words.hint}</div>
              {rows && rows.length
                ? <div className="cp-tpl-grid">{rows.map((row) => <Card key={row.name} row={row} onOpen={setOpen} />)}</div>
                : <div className="cp-tpl-empty">{empty}</div>}
            </>
          )}
      </div>
    </>
  )
}
