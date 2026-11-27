/* The running subagents the waiting row draws as orbs, joined from the tasks
   store's rows and the subagents roster. It lives in the page's wiring
   because it reads two sibling domains, which neither may import. */

import { isOwnAgent } from '../components/AgentMark'

import type { SubagentOrb } from '../features/composer/store'
import type { SubagentRow } from '../features/subagents/types'
import type { TaskRow } from '../features/tasks/types'

/* A row with no registration behind it still wears Raven's own tile when its
   name says it is one of Raven's agents -- the waterfall's rule
   (trajectory/waterfall/Waterfall.tsx). */
const OWN_NAME = /^raven([^a-z0-9]|$)/i

function markOf(name: string, roster: readonly SubagentRow[]): Pick<SubagentOrb, 'preset' | 'own'> {
  const row = roster.find((r) => r.name === name)
  if (row) return { preset: row.preset ?? null, own: isOwnAgent(row) }
  return { preset: null, own: OWN_NAME.test(name.trim()) }
}

/** One orb per subagent that is running now: a spawn once its node runs, a
 *  graph's every running node. A task still waiting to start, or a graph
 *  between waves, has no orb; whether the row shows is the task count's
 *  business, not this list's. Oldest first, unknown start times last. */
export function orbsOf(rows: readonly TaskRow[], roster: readonly SubagentRow[]): SubagentOrb[] {
  const orbs: SubagentOrb[] = []
  for (const row of rows) {
    for (const node of row.nodes ?? []) {
      if (node.status !== 'running') continue
      const name = (row.kind === 'spawn' ? row.agent || node.agent : node.agent) || ''
      orbs.push({
        key: row.kind === 'spawn' ? row.id : `${row.id}:${node.node_id}`,
        name,
        ...markOf(name, roster),
        startedAt: node.started_at ?? (row.kind === 'spawn' ? row.started_at ?? null : null),
      })
    }
  }
  return orbs.sort((a, b) => {
    if (a.startedAt === b.startedAt) return a.key < b.key ? -1 : a.key > b.key ? 1 : 0
    if (a.startedAt === null) return 1
    if (b.startedAt === null) return -1
    return a.startedAt - b.startedAt
  })
}
