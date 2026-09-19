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

import { hasBuildFlag } from '../../rpc/capabilities'
import { gateway } from '../../rpc/gateway'

import type { ResultOf } from '../../rpc/generated'
import type { ExtAgentRow, ExtAgentsSource } from './types'

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

/* What connecting this row still has to do. One stage, one write -- the page's
   whole decision, so the row and the card cannot offer different verbs for the
   same state.
 *
 * `install` and `off` both describe a shipped folder that is not on the roster,
 * and they are not the same job: a folder whose venv was never built needs the
 * installer (minutes, hundreds of MB), while one that was switched off needs
 * its manifest flag back. The probe verdict is what separates them. */
export type Stage = 'builtin' | 'building' | 'install' | 'add' | 'key' | 'stale' | 'off' | 'live'

export function stageOf(row: ExtAgentRow): Stage {
  if (row.builtin) return 'builtin'
  if (row.building) return 'building'
  if (row.vendored) {
    if (row.probe_status === 'missing') return 'install'
    return row.enabled ? 'live' : 'off'
  }
  /* An HTTP agent cannot answer without its key, so it is not connected by
     writing an entry -- the key is the missing part, whether the entry exists
     yet or not. */
  if (row.kind === 'openai' && !row.has_api_key) return 'key'
  if (!row.configured) return 'add'
  if (row.enabled) return 'live'
  /* Out of service and its preset has moved to another transport. Connecting it
     is a remove plus an add, not a flag, so it is its own stage rather than a
     variant of `off` -- the flag left `upgrade_to` standing and the old command
     line in place, which is an agent the card offered to migrate and never did.
     After `live`, so an agent still in service keeps offering the one verb its
     state calls for, which is disconnect. */
  if (row.upgrade_to) return 'stale'
  return 'off'
}

/* Which group a row belongs in: what it would take to use it, in four steps of
   one answer each. The page used to lead with provenance -- built-in, then
   vendored, then connected, then available -- which put a broken agent three
   groups down while a healthy built-in row sat at the top with nothing to do.
   Two groups replaced that and were right about the ordering and too coarse
   about the rest: "available" held the switch that takes a click and the CLI
   this machine has never had, in one list of eleven, sorted by a cost the
   heading did not name.

   The fourth group is the one worth having. Everything else here is a question
   about Raven's config; that one is a question about the machine, and it is
   where nine of the eleven live on a stock install. It is also the only group a
   reader can be done with, which is why it is the one that folds. */
export type Grp = 'on' | 'switch' | 'setup' | 'install'
export const groupOf = (row: ExtAgentRow): Grp => {
  const stage = stageOf(row)
  if (stage === 'live' || stage === 'builtin') return 'on'
  if (stage === 'install' || stage === 'building') return 'install'
  /* The probe before the stage, for everything that is not connected and runs
     a command. A switch, an entry, a credential: each is a write to Raven's own
     config, and each connects nothing when the command it names is not on this
     machine -- and the probe is the only thing that knows. That includes the
     switch. A configured agent keeps its entry after its binary is removed, so
     `off` is a stage a missing executable can be in, and `subagents.toggle`'s
     enable gate probes the agent and refuses the write when nothing answers; a
     row like that under "ready to enable" promised the one click that cannot
     work. `attention` stays out of this group: it means the binary answered
     and nothing has verified what it can do, which is a row worth connecting
     and then testing, not one worth hiding.

     Only the command-backed kinds. An openai row is an endpoint, and its probe
     says `missing` for "unreachable" -- a connection error, a timeout -- which
     is a network fact with nothing to install behind it, and the enable gate
     does not ping that kind at all. Its group stays the one its config state
     says: the switch, or the credential. The server's own grouping draws the
     same line for the same reason (`_group` keys an openai row off its key). */
  if ((row.kind === 'cli' || row.kind === 'acp') && row.probe_status === 'missing') return 'install'
  return stage === 'off' ? 'switch' : 'setup'
}

/* What connecting costs, which is what a group is ordered by inside itself --
   the same question the entrances page sorts on. Most of this is now said by
   which group the row is in; what is left is the one distinction the groups do
   not draw, between an entry Raven writes on its own and a credential the
   reader has to go and find. */
export const costOf = (row: ExtAgentRow): number => {
  const stage = stageOf(row)
  return stage === 'off' ? 0 : stage === 'add' ? 1 : stage === 'key' ? 2 : 3
}

/* The onboarding wizard's agents step draws two buckets instead of stageOf's
   four groups: available to connect, and connected. Both are pure reads of a
   row the wizard gets from the same `subagents.list` this page lists from, and
   the step counts itself done against a third read, FOUND.
 *
 * A row is FOUND when it is a command this machine has: neither one of
 * Raven's own (`builtin`, `vendored` -- both already usable with no connect
 * step), nor an openai row (an endpoint, which no scan finds and the wizard
 * does not offer), nor a probe that answered nothing (`missing`: not on the
 * machine; `unknown`: never probed). An acp preset that only proved its
 * binary exists still counts -- `attention` is a row worth connecting and then
 * testing, not one worth hiding. */
export function isFound(row: ExtAgentRow): boolean {
  if (row.builtin || row.vendored || row.kind === 'openai') return false
  return row.probe_status !== 'missing' && row.probe_status !== 'unknown'
}

/* Connected: on the roster and dispatchable. Raven's own shipped agents count
   here once switched on, same as a configured one -- but the built-in
   in-process Raven never does, because this pane never shows it at all. */
export function isConnected(row: ExtAgentRow): boolean {
  if (row.builtin) return false
  return row.enabled && (!!row.vendored || row.configured)
}

/* Available: what the connect button can act on. A found row not yet enabled,
   or one of Raven's own shipped agents switched off with its folder built
   (`off`, not `install`) -- never FOUND, so the step does not count it, but the
   connected bucket offers to switch it off and it needs somewhere to come back
   from. */
export function isAvailable(row: ExtAgentRow): boolean {
  if (row.vendored) return stageOf(row) === 'off'
  return isFound(row) && !row.enabled
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
