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
import type { ExtAgentRow, ExtAgentsSource, Remedy } from './types'

/** One agent as `subagents.list` sends it. */
export type ExtAgentRowWire = ResultOf<'subagents.list'>['rows'][number]

export function extAgentRowOf(r: ExtAgentRowWire): ExtAgentRow {
  const remedy = remedyOf(r.last_test_remedy)
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
    ...(r.probe_missing ? { probe_missing: r.probe_missing } : {}),
    has_api_key: !!r.has_api_key,
    needs_auth: !!r.needs_auth,
    description: r.description || '',
    last_test_ok: r.last_test_ok ?? null,
    last_test_detail: r.last_test_detail || '',
    ...(remedy ? { last_test_remedy: remedy } : {}),
    last_test_at_ms: r.last_test_at_ms || null,
    test_running: !!r.test_running,
    own: !!r.own,
    model: r.model ?? null,
    model_choices: (r.model_choices || [])
      .filter((c) => c && c.value)
      .map((c) => ({ value: c.value, name: c.name || '', group: c.group || '' })),
    model_source: r.model_source,
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
export type Stage = 'builtin' | 'building' | 'install' | 'add' | 'key' | 'stale' | 'off' | 'live' | 'unauthorized'

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
  /* Named, not offered: the handshake already refused this agent for want of
     a credential, and the remedy -- signing in -- is outside this page, so a
     Connect here would spend a launch to arrive at the same sentence. Only
     for a row not on the roster: a connected agent keeps Disconnect whatever
     its credential has since done. The way back is the card's Test, which
     re-measures. */
  if (!row.enabled && (row.kind === 'cli' || row.kind === 'acp') && row.needs_auth) return 'unauthorized'
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

/* Which section a row belongs in. Three, and the question each answers is the
   reader's, not the config's: is it working for me now, could I connect it
   from here, or is it not on this machine at all.

   `on` is every row that dispatches today, the built-in loop included.
   `missing` is a command this machine has never had -- and only a command: an
   openai row is an endpoint whose probe says `missing` for "unreachable", a
   network fact with nothing to install behind it, and a shipped product is
   never absent (the server demotes a probe miss on one to `attention`; the
   `vendored` test is belt to that brace). Everything else -- a switch to flip,
   an entry to write, a key to paste, a preset that moved transport, a product
   whose engine wheel is not installed -- is connected from here, and is one
   section. */
export type Section = 'on' | 'avail' | 'missing'
export function sectionOf(row: ExtAgentRow): Section {
  const stage = stageOf(row)
  if (stage === 'live' || stage === 'builtin') return 'on'
  if ((row.kind === 'cli' || row.kind === 'acp') && row.probe_status === 'missing' && !row.vendored) return 'missing'
  return 'avail'
}

/* The onboarding wizard's agents step draws two of the hub's three sections,
   from the same `sectionOf`, and leaves out the rows that are not a first
   run's decision: the built-in loop (always on, nothing to do), an openai
   endpoint (its connect is a key typed into a sheet the step has not got), a
   command this machine has never had (an install is not a wizard step), and a
   build an older gateway still has in flight -- `sectionOf` files that under
   available, and the row's Connect would flip a switch on an install that is
   not finished. */
export function wizardSection(row: ExtAgentRow): Section | null {
  if (row.builtin || row.kind === 'openai' || row.building) return null
  const section = sectionOf(row)
  return section === 'missing' ? null : section
}

/* The rows the step counts itself against: the external agents this machine
   has, whichever of the two sections each sits in. Raven's own shipped agents
   are drawn -- connected when on, available when off -- but never counted:
   the step is about connecting something external, and the prototype counts
   it done on those alone. A connected row counts whatever its probe says --
   it is on the roster and dispatchable, which is what `sectionOf` reads
   first -- so a verdict that failed to carry over a re-scan no longer flips
   the step back to not done. */
export function isFound(row: ExtAgentRow): boolean {
  return !row.vendored && wizardSection(row) !== null
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
     config, so the last known one is carried over -- with what it found
     absent, or a missing row's install falls back to the agent's own. */
  const rows = (res.rows || []).map((r) => {
    const row = extAgentRowOf(r)
    const prev = extAgentsSeen.get(row.name)
    if (row.probe_status === 'unknown' && prev && prev.probe_status !== 'unknown') {
      row.probe_status = prev.probe_status
      row.probe_detail = prev.probe_detail
      if (prev.probe_missing) row.probe_missing = prev.probe_missing
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
    } else if (op === 'model') {
      /* The row's own model, set or cleared. `clear_model` wins over `model`
         server-side, so the two never travel together from here. The built-in
         row's pick names its provider, which the server spells into the
         stored id; an acp row's value is the agent's own and goes verbatim. */
      await gateway().call(
        'subagents.update',
        a.clear_model ? { name: row.name, clear_model: true } : { name: row.name, model: a.model, provider: a.provider || undefined },
      )
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
      await gateway().call('subagents.test', {
        name: row.name,
        source: row.vendored ? 'vendored' : row.configured ? 'config' : 'preset',
      })
    } else if (op === 'test_cancel') {
      /* Cancels the test server-side, where the interrupted measurement reaps
         its own child -- a process-group kill for a cli agent, a connection
         close for an acp one. The test's own call is still open on another
         connection and answers `cancelled: true` from there, so this one has
         nothing to report and only has to arrive. */
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

/* A remedy off the wire, or null for anything that is not one. Read from a row
   and from a refused call's `data` alike, so both land in the one shape. */
export function remedyOf(raw: unknown): Remedy | null {
  const r = raw as { kind?: unknown; command?: unknown } | null | undefined
  if (!r || (r.kind !== 'sign_in' && r.kind !== 'setup' && r.kind !== 'api_key' && r.kind !== 'download')) return null
  return { kind: r.kind, command: typeof r.command === 'string' ? r.command : '' }
}
