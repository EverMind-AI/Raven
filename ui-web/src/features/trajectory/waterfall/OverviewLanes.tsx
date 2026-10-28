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

/** The agent's spans merged into stretches no longer than the gaps between them. */
function runs(agent: WaterfallAgent): Array<{ start: number; end: number }> {
  const spans: Array<{ start: number; end: number }> = []
  for (const turn of agent.turns) spans.push({ start: turn.start, end: turn.end })
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
      {waterfall.agents.map((agent) => (
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
    </div>
  )
}
