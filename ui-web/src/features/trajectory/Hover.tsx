/* The three lines the duration bar says about the block under the pointer:
 * what kind of entry it is, when it happened, and what it is charged.
 *
 * A fixed-position card drawn by the island itself rather than through a
 * portal layer: the page's one hover layer is the single-line pill, and a
 * card of three lines is not a label. The card sits above the dock in the
 * root stacking context (nothing on the way up to the body stacks or
 * transforms), takes no pointer events, and is clamped to the viewport --
 * below the bar when there is room, above it otherwise.
 */

import { t } from '../../i18n/t'
import { formatDuration } from '../../lib/duration'
import { basisKey } from './blocks'
import { kindClass, kindLabel } from './palette'

import type { Band, Block, Segment, Summary } from './geometry'
import type { JSX } from 'react'

export interface HoverAt {
  block: Block
  clientX: number
}

/** Where the card goes: under the bar, or over it when the window ends first. */
export function placeHover(bar: DOMRect, clientX: number, size: { width: number; height: number }, viewport: { width: number; height: number }): { left: number; top: number } {
  const left = Math.max(4, Math.min(viewport.width - size.width - 4, clientX - size.width / 2))
  const below = bar.bottom + 6
  const top = below + size.height <= viewport.height - 4 ? below : Math.max(4, bar.top - size.height - 6)
  return { left, top }
}

/* The second line: a zero-length mark happened at an instant, a thinking
   entry lies inside its call and recorded no time of its own, a call has
   a start and an end, and an entry with neither says so. */
function when(segment: Segment): string {
  if (segment.basis === 'zero') return t('gui.trajectory.bar.event_at', { t: segment.eventTime })
  if (segment.basis === 'shared' || segment.basis === 'not_recorded') return t('gui.trajectory.bar.thinking_range')
  if (segment.start && segment.end) return t('gui.trajectory.bar.span', { from: segment.start, to: segment.end })
  return t('gui.trajectory.bar.span_unknown')
}

function charge(segment: Segment): string {
  const key = basisKey(segment.basis)
  const sentence = key ? t(key) : segment.basis
  return segment.charged !== null && segment.charged > 0 ? `${sentence} (${formatDuration(segment.charged)})` : sentence
}

export function BarHover({ at, bySegment, bar, size, viewport }: {
  at: HoverAt
  bySegment: ReadonlyMap<string, Segment>
  bar: DOMRect
  size: { width: number; height: number }
  viewport: { width: number; height: number }
}): JSX.Element | null {
  const { block } = at
  const { left, top } = placeHover(bar, at.clientX, size, viewport)
  if (block.ids) {
    return (
      <div className="trajectory-hover" style={{ left, top }} aria-hidden="true">
        <div className="trajectory-hover-line">
          {t('gui.trajectory.bar.dense', { n: block.ids.length, dur: formatDuration(block.sum ?? 0), unknown: block.unknown ?? 0 })}
        </div>
        <div className="trajectory-hover-line trajectory-hover-muted">{t('gui.trajectory.bar.dense_hint')}</div>
      </div>
    )
  }
  const segment = bySegment.get(block.id)
  if (!segment) return null
  return (
    <div className="trajectory-hover" style={{ left, top }} aria-hidden="true">
      <div className="trajectory-hover-line">
        <span className={`trajectory-tag ${kindClass(segment.kind)}`}>{kindLabel(segment.kind)}</span>
        {segment.failure ? <span className="trajectory-hover-fail">{t('gui.trajectory.bar.failed')}</span> : null}
      </div>
      <div className="trajectory-hover-line trajectory-hover-mono">{when(segment)}</div>
      <div className="trajectory-hover-line trajectory-hover-muted">{charge(segment)}</div>
    </div>
  )
}

/* One line about a turn whose reply the list leaves out: its number and the
   whole time it took, shown over the band that stands for it. */
export function BandHover({ band, bar, clientX, size, viewport }: {
  band: Band
  bar: DOMRect
  clientX: number
  size: { width: number; height: number }
  viewport: { width: number; height: number }
}): JSX.Element {
  const { left, top } = placeHover(bar, clientX, size, viewport)
  return (
    <div className="trajectory-hover trajectory-hover-brief" style={{ left, top }} aria-hidden="true">
      <div className="trajectory-hover-line">{t('gui.trajectory.bar.turn_total', { n: band.turn, dur: formatDuration(band.total) })}</div>
    </div>
  )
}

/* One line about the whole bar, shown where no block and no band is: the
   charged sum, the entries with no recorded time, and whether the sum holds
   overlap. What used to be printed beside the canvas, now only on demand. */
export function BarSummary({ sum, bar, clientX, size, viewport }: {
  sum: Summary
  bar: DOMRect
  clientX: number
  size: { width: number; height: number }
  viewport: { width: number; height: number }
}): JSX.Element {
  const { left, top } = placeHover(bar, clientX, size, viewport)
  const parts = [t('gui.trajectory.bar.sum_known', { dur: formatDuration(sum.known) })]
  if (sum.unknownCount > 0) parts.push(t('gui.trajectory.bar.sum_unknown', { n: sum.unknownCount }))
  if (sum.overlap) parts.push(t('gui.trajectory.bar.sum_overlap'))
  return (
    <div className="trajectory-hover trajectory-hover-brief" style={{ left, top }} aria-hidden="true">
      <div className="trajectory-hover-line">{parts.join(' \u00b7 ')}</div>
    </div>
  )
}
