/* -- external agents: the rpc source ---------------------------------
   `subagents.*` is one surface shared with the TUI and the web UI: the rows,
   the install grouping and the write path all live server-side, so this module
   only maps a row into what the page draws and sends the mutation back. The
   extAgents island (ui-web/src/features/extAgents/) owns the renderer and
   every flag it reads. It is the seam's one roster source: a page with no
   gateway behind it reads the same calls off the fixture transport.

   The list is re-fetched after every mutation rather than patched locally: the
   handler recomputes `group`, `enabled` and the probe verdict together, and a
   client that guesses any one of them is how the page starts disagreeing with
   the config on disk. `probe: false` on that follow-up call skips the
   availability check, which can cost up to ten seconds per entry and would only
   re-measure what the write just changed. */

import type { ExtAgentRow, ExtAgentsSource } from './types'
import type { ResultOf } from '../../rpc/generated'

import { hasBuildFlag } from '../../rpc/capabilities'
import { gateway } from '../../rpc/gateway'

/** One agent as `subagents.list` sends it. */
export type ExtAgentRowWire = ResultOf<'subagents.list'>['rows'][number]

export function extAgentRowOf(r: ExtAgentRowWire): ExtAgentRow {
  return {
    name: r.name,
    preset: r.preset,
    kind: r.kind || (r.builtin ? 'builtin' : 'cli'),
    configured: !!r.configured,
    /* Raven's own agents. On the table whether or not config mentions them, so
       they are `configured: false` yet not something to install -- the page needs
       both facts to avoid offering a Connect button for a loop already running. */
    builtin: !!r.builtin,
    /* The agent products this install shipped, discovered under the
       `agents/` product tree. Same shape of problem as `builtin` and the same
       reason it has to be carried explicitly: `configured: false` with nothing to install, so the
       page needs the flag to keep a Connect button off a row that has no preset
       to connect from. This mapper is a whitelist -- a field it does not name is
       a field the island never sees. */
    vendored: !!r.vendored,
    /* Always false from a current server: the fork-era venv build is gone
       and `subagents.build` answers that there is nothing to build. Carried
       for an older server, where the row was the only place the page learned
       a build was in flight. */
    building: hasBuildFlag(r),
    enabled: !!r.enabled,
    probe_status: r.probe_status || 'unknown',
    upgrade_to: r.upgrade_to || null,
    probe_detail: r.probe_detail || '',
    has_api_key: !!r.has_api_key,
    description: r.description || '',
    last_test_ok: r.last_test_ok ?? null,
    last_test_detail: r.last_test_detail || '',
    last_test_at_ms: r.last_test_at_ms || null,
    test_running: !!r.test_running,
  }
}

/* What the last fetch reported, kept here rather than read back off the page:
   the carry-over below is a fact about this transport (a probe-less list says
   "unknown" for every row), so the source answers it from its own memory
   instead of reaching into the array the page is rendering. */
let extAgentsSeen = new Map<string, ExtAgentRow>()

export async function extAgentsFetch(probe: boolean): Promise<ExtAgentRow[]> {
  const res = await gateway().call('subagents.list', { probe: !!probe })
  /* A probe-less list reports every row as "unknown", which would blank the
     health line of a row that was ready a second ago -- connecting an agent
     would look like it broke it. The verdict cannot have changed by writing
     config, so the last known one is carried over. */
  const rows = (res.rows || []).map((r) => {
    const row = extAgentRowOf(r)
    const prev = extAgentsSeen.get(row.name)
    if (row.probe_status === 'unknown' && prev && prev.probe_status !== 'unknown') {
      row.probe_status = prev.probe_status
      row.probe_detail = prev.probe_detail
    }
    return row
  })
  extAgentsSeen = new Map(rows.map((r) => [r.name, r]))
  return rows
}

/** Back to a fresh page's memory of the probe verdicts. For tests. */
export function resetExtAgentsSeen(): void {
  extAgentsSeen = new Map()
}

export const extAgentsSource: ExtAgentsSource = {
  load: (probe) => extAgentsFetch(!!probe),
  act: async (op, row, args) => {
    const a = args || {}
    if (op === 'connect') {
      /* Only name / description / key travel: every execution field comes from
         the preset server-side. A page that could post a command line would make
         "which agent is this" unanswerable. */
      await gateway().call('subagents.add', {
        preset: row.preset || row.name,
        name: a.new_name || undefined,
        description: a.description || undefined,
        api_key: a.api_key || undefined,
      })
    } else if (op === 'update') {
      await gateway().call('subagents.update', {
        name: row.name,
        new_name: a.new_name && a.new_name !== row.name ? a.new_name : undefined,
        description: a.description,
        api_key: a.api_key || undefined,
      })
    } else if (op === 'toggle') {
      /* One switch for every kind of row. A discovered folder has no config
         entry, so the server writes one from the folder's own manifest and puts
         the flag on it -- which is why connecting one back on is this same call
         and not a second mechanism. */
      await gateway().call('subagents.toggle', { name: row.name, enabled: !!a.enabled })
    } else if (op === 'migrate') {
      /* A stale transport is the one connect that cannot be a flag. There is no
         "change the transport" write -- `subagents.update` touches name,
         description and key only, on purpose -- so it is a remove plus an add
         from the preset, which is also what makes it visible in config as one
         entry replaced rather than an entry mutated underneath its session
         handles. Toggling `enabled` would have left `upgrade_to` standing and
         the old command line in place. */
      await gateway().call('subagents.remove', { name: row.name })
      await gateway().call('subagents.add', {
        preset: row.preset || row.name,
        name: row.name,
        description: row.description || undefined,
      })
    } else if (op === 'test') {
      /* The one call here that spends the agent's own quota: it dispatches the
         real backend once. That is why nothing runs it on page load -- the free
         `probe` is what fills the rows -- and why it is only reached from the
         card, by a click.

         The verdict is recorded server-side and comes back on the refetched
         rows (`last_test_ok` / `last_test_at_ms` / `last_test_detail`), so
         nothing here has to hold it. */
      await gateway().call('subagents.test', { name: row.name, source: row.configured ? 'config' : 'preset' })
    } else if (op === 'test_cancel') {
      /* Kills the agent's process group server-side. The test's own call is
         still open on another connection and answers `cancelled: true` from
         there, so this one has nothing to report and only has to arrive. */
      await gateway().call('subagents.test_cancel', { name: row.name })
    } else if (op === 'build') {
      /* Returns as soon as the build is under way, not when it is done: it is a
         few hundred MB of downloads. The row's `building` flag is what says it is
         still going, and the store polls the list while any row carries it. */
      await gateway().call('subagents.build', { name: row.name })
    }
    return extAgentsFetch(false)
  },
}

/* Test seam only: the rows carried across a refetch are the module's. */
export function _resetForTests(): void {
  extAgentsSeen = new Map()
}
