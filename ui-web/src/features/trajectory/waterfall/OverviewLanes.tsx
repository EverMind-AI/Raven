/* The overview strip above the ruler: one thin lane per agent, painted from
 * the same axis and viewport the rows use. It answers "who was busy, and when"
 * before the reader has read a single row.
 *
 * A lane merges its agent's stretches into one bar per contiguous run rather
 * than one per span: at this height an overview is a shape, not a list. */

import { t } from '../../../i18n/t'
import { scopeTitle } from '../scopes'
import { barOf } from './axis'
import { agentKey } from './model'

import type { TimeAxis, Viewport } from './axis'
import type { Waterfall, WaterfallAgent } from './model'
import type { JSX } from 'react'

const percent = (fraction: number): string => `${(fraction * 100).toFixed(4)}%`

/** A session can have dozens of agents; the strip is a shape, not a roster.
 *  Past this many lanes the rest are a count, or the list below gets no room. */
export const MAX_LANES = 6

/** The agent's spans merged into stretches no longer than the gaps between them. */
function runs(agent: WaterfallAgent): Array<{ start: number; end: number }> {
  const spans: Array<{ start: number; end: number }> = []
  for (const turn of agent.turns) spans.push({ start: turn.start, end: turn.end })
  for (const row of agent.loose) {
    const start = row.entry.operation_start
    const end = row.entry.operation_end
    if (start !== null && start !== undefined && end !== null && end !== undefined) {
      spans.push({ start: Date.parse(start), end: Date.parse(end) })
    }
  }
  spans.sort((a, b) => a.start - b.start)
  const merged: Array<{ start: number; end: number }> = []
  for (const span of spans) {
    const last = merged.at(-1)
    if (last && span.start <= last.end) last.end = Math.max(last.end, span.end)
    else merged.push({ ...span })
  }
  return merged
}

function nameOf(agent: WaterfallAgent): string {
  return agent.scope === null ? t('gui.trajectory.scope.main') : scopeTitle(agent.door)
}

export function OverviewLanes({ waterfall, axis, viewport }: {
  waterfall: Waterfall; axis: TimeAxis; viewport: Viewport
}): JSX.Element {
  return (
    <div className="trajectory-ov">
      {waterfall.agents.slice(0, MAX_LANES).map((agent) => (
        <div key={agentKey(agent.scope)} className="trajectory-ov-lane" data-lane={agentKey(agent.scope)}>
          <span className="trajectory-ov-name">{nameOf(agent)}</span>
          <span className="trajectory-ov-track">
            {runs(agent).map((run, at) => {
              const bar = barOf(axis, viewport, run.start, run.end)
              return bar === null ? null : <i key={at} style={{ left: percent(bar.left), width: percent(bar.width) }} />
            })}
          </span>
        </div>
      ))}
      {waterfall.agents.length > MAX_LANES ? (
        <div className="trajectory-ov-lane trajectory-ov-more" data-lane-more="">
          <span className="trajectory-ov-name">
            {t('gui.trajectory.agents_more', { n: waterfall.agents.length - MAX_LANES })}
          </span>
          <span className="trajectory-ov-track" />
        </div>
      ) : null}
    </div>
  )
}
