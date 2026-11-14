/* The popover the context ring opens: the window, and what is filling it.
 *
 * The ring on the composer's bar answers one question -- is the window worth
 * thinking about -- and this answers the next one: what is in it. The headline
 * is the same number the ring draws, so the two can never disagree; the grid
 * below it is the fill as cells, coloured by the part each cell belongs to,
 * and the rows under the grid are those parts with their own counts.
 *
 * The headline and the rows are two different measurements, and the card says
 * so rather than leaving the reader to add them up: the loop measures the
 * parts locally, from the prompt it sent, so they are an estimate that neither
 * counts the answer nor uses the provider's tokenizer. The parts are absent
 * until a turn has measured them. A window nobody divided is still a window:
 * the popover says so rather than inventing a breakdown.
 *
 * WHERE this renders is not decided here. It is a child of the ring's anchor
 * (.chrome-anch, src/chrome/Dock.tsx), hung off the chip's top edge by the
 * stylesheet, the way the permission modes' popover is.
 */

import { useEffect, useSyncExternalStore } from 'react'

import * as model from '../features/model/store'
import { t } from '../i18n/t'
import { ctxCells, PARTS } from '../lib/ctxGrid'
import * as ctx from '../state/ctxWindow'
import * as lang from '../state/lang'

import type { CtxPart } from '../lib/ctxGrid'
import type { JSX } from 'react'

/** A count as the popover says it: 175.6K, 4.2K, 940. */
const fmt = (n: number): string => {
  if (n < 1000) return String(n)
  const k = n / 1000
  return `${k.toFixed(1).replace(/\.0$/, '')}K`
}

/** What each part is called. A record, so a part the grid grows a name for
    cannot be left out of here: the compiler says so first. */
const LABEL: Record<CtxPart, string> = {
  system: 'gui.ctx.part.system',
  tools: 'gui.ctx.part.tools',
  conversation: 'gui.ctx.part.conversation',
  files: 'gui.ctx.part.files',
  memory: 'gui.ctx.part.memory',
  skills: 'gui.ctx.part.skills',
}

export function CtxPopover(): JSX.Element {
  const s = useSyncExternalStore(ctx.subscribe, ctx.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  useSyncExternalStore(model.subscribe, model.version)
  const current = model.current()

  /* A click elsewhere closes the popover, and so does Escape: the ring is the
     way back into it, but not the only way out of it. The chip counts as
     inside -- the click that opened the popover must not close it again. */
  useEffect(() => {
    if (!s.open) return
    const onDown = (e: PointerEvent): void => {
      const target = e.target as HTMLElement | null
      if (target && target.closest('#ctxChip, #ctxPop')) return
      ctx.close()
    }
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') ctx.close()
    }
    document.addEventListener('pointerdown', onDown, true)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onDown, true)
      document.removeEventListener('keydown', onKey)
    }
  }, [s.open])

  const parts = s.parts
  /* The store's own percentage, clamped there: this card and the ring read
     one number rather than each rounding the raw fill. */
  const pct = s.pct
  const cells = ctxCells(parts, s.used, s.max)
  const left = Math.max(0, s.max - s.used)

  return (
    <div
      className={`pop chrome-ctx-pop${s.warm ? ' warm' : ''}${s.hot ? ' hot' : ''}`}
      id="ctxPop"
      data-open={s.open ? 'true' : 'false'}
      role="dialog"
      aria-label={t('gui.ctx.title')}
    >
      <div className="chrome-ctx-head">
        <span className="chrome-ctx-title">{t('gui.ctx.title')}</span>
        {current ? <span className="chrome-ctx-model">{model.short(current)}</span> : null}
      </div>
      <div className="chrome-ctx-hero">
        <span className="chrome-ctx-used">{fmt(s.used)}</span>
        <span className="chrome-ctx-of">{t('gui.ctx.of', { max: fmt(s.max) })}</span>
        <span className="chrome-ctx-pct">{`${pct}%`}</span>
      </div>
      <div className="chrome-ctx-grid" aria-hidden="true">
        {cells.map((c, i) => (
          <span key={i} className="chrome-ctx-cell" data-part={c.part ?? undefined} />
        ))}
      </div>
      {parts ? (
        <>
          <ul className="chrome-ctx-rows">
            {PARTS.map((part) => (
              <li key={part} className="chrome-ctx-row">
                <span className="chrome-ctx-dot" data-part={part} aria-hidden="true" />
                <span className="chrome-ctx-name">{t(LABEL[part])}</span>
                <span className="chrome-ctx-num">{fmt(parts[part] ?? 0)}</span>
              </li>
            ))}
          </ul>
          {/* Said in the card, not left to be inferred: these rows and the
              headline above them are two measurements, not two halves of one. */}
          <p className="chrome-ctx-note">{t('gui.ctx.estimate')}</p>
        </>
      ) : (
        <p className="chrome-ctx-none">{t('gui.ctx.noparts')}</p>
      )}
      <div className="chrome-ctx-foot">
        <span className="chrome-ctx-left">{t('gui.ctx.left', { n: fmt(left) })}</span>
      </div>
    </div>
  )
}
