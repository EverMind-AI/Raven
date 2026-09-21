/* The tasks board's own node card: a 1:1 port of the prototype's `runCard`
 * (proto.js:4658-4674) -- the two-line title, the agent line and a top-right
 * status cluster of icon-or-word plus duration, in the prototype's own
 * words. Handed to `DagGraph` through its `renderNode`: the renderer lays the
 * nodes out, draws the edges and takes the clicks, and the box inside each
 * node is the caller's.
 */

import { t } from '../../i18n/t'
import { formatDuration } from '../../lib/duration'

import type { DagNode } from '../dag/types'
import type { JSX } from 'react'

/* `RT_LABEL` (proto.js:4655-4656), minus the three branches that get an icon
   or a duration instead of a bare word (completed, running, failed) -- those
   are `Cluster`'s own branches below. `exception` has no prototype entry
   (proto.js:4671 would print the raw status word); contract 2.7's
   `node_st_exception` fills that gap in the same slot the other words use. */
const RT_WORD_KEY: Record<string, string> = {
  pending: 'gui.tasks.node_st_pending',
  skipped: 'gui.tasks.node_st_skipped',
  cancelled: 'gui.tasks.node_st_cancelled',
  interrupted: 'gui.tasks.node_st_interrupted',
  exception: 'gui.tasks.node_st_exception',
}

/* `ICONS.check` / `ICONS.spin`, drawn the way `ico()` draws them: one or more
   bare paths in a 24x24 box. */
function CheckIcon(): JSX.Element {
  return (
    <svg className="tkrunicon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 12.5l4.5 4.5L19 7" />
    </svg>
  )
}

const SPOKES = [
  'M12 4.5v3', 'M12 16.5v3', 'M4.5 12h3', 'M16.5 12h3',
  'M6.7 6.7l2.1 2.1', 'M15.2 15.2l2.1 2.1', 'M6.7 17.3l2.1-2.1', 'M15.2 8.8l2.1-2.1',
]

function SpinIcon(): JSX.Element {
  return (
    <svg className="tkrunicon tkrunspin" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" aria-hidden="true">
      {SPOKES.map((d) => <path key={d} d={d} />)}
    </svg>
  )
}

/* `fmtDur(n.ended_at - n.started_at)` / `fmtDur(Date.now() - n.started_at)`,
   guarded rather than let run into `NaN`: a node missing the stamp the
   prototype's own fixtures always carry shows nothing for the fragment, per
   this task's own rule for a data gap. */
function span(a: number | null | undefined, b: number | null | undefined): string {
  return a != null && b != null ? formatDuration(Math.max(b - a, 0)) : ''
}

/* The `.rt` cluster (proto.js:4667-4672): completed gets a check and a
   duration, running a spinning icon and a live tick, failed the word
   "failed" and a duration, everything else just the word -- never a
   duration outside these three branches. */
function Cluster({ node, now }: { node: DagNode; now: number }): JSX.Element {
  if (node.status === 'completed') {
    const dur = span(node.started_at, node.ended_at)
    return <span className="tkrunrt"><CheckIcon />{dur ? <span>{dur}</span> : null}</span>
  }
  if (node.status === 'running') {
    const dur = span(node.started_at, now)
    return <span className="tkrunrt"><SpinIcon />{dur ? <span className="tkruntick">{dur}</span> : null}</span>
  }
  if (node.status === 'failed') {
    const dur = span(node.started_at, node.ended_at)
    return <span className="tkrunrt">{dur ? `${t('gui.tasks.node_st_failed')} ${dur}` : t('gui.tasks.node_st_failed')}</span>
  }
  const key = RT_WORD_KEY[node.status]
  return <span className="tkrunrt">{key ? t(key) : node.status}</span>
}

export function BoardCard({ node, now }: { node: DagNode; now: number }): JSX.Element {
  return (
    <div className="tkrun">
      <span className="tkrunl1">{node.node_summary || node.id}</span>
      <span className="tkrunl2">
        <span className="tkrunag">{node.subagent}</span>
        {node.instance ? <span className="tkrunhandle">{'@' + node.instance}</span> : null}
        {node.tool_call_count ? <span className="tkrunchip">{t('gui.tasks.tools_n', { n: node.tool_call_count })}</span> : null}
      </span>
      <Cluster node={node} now={now} />
    </div>
  )
}
