import { t } from '../../i18n/t'
import * as detail from '../../state/detail'
import * as hub from '../../state/hub'
import * as page from '../../state/page'
import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'
import { show as toast } from '../../state/toast'
import { familyOf } from './catalogue'
import * as machines from './machines'
import { isFound, remedyOf, runsAgent, sectionOf, stageOf } from './source'

import type { ExtAgentActArgs, ExtAgentOp, ExtAgentRow, ExtAgentsSource, PresetChoice, Remedy } from './types'

/* Page state, outside React on purpose: two of the callers that drive this page
 * are not React. The rail opens it (chrome/Rail.tsx) and Esc closes it
 * (state/escapeOrder.ts) -- so the state lives in a plain store those two can
 * call, and the component subscribes.
 */

/* A write that did not land, kept on the row so the row can say so and offer
   it again: which write, with what, and the server's own sentence about why. */
export interface Failure {
  op: ExtAgentOp
  args: ExtAgentActArgs
  detail: string
  /* The fix the server named for this refusal; absent when it named none.
     `detail` stays the server's own sentence, shown under the fix rather than
     instead of it. */
  remedy?: Remedy
}

/* A write in flight, kept the way a refused one is: which write, with what. */
export interface Pending {
  op: ExtAgentOp
  args: ExtAgentActArgs
}

/* Whether the server may answer this write by running the agent -- the
   source's `runsAgent`, which also decides whether the read that ends the
   write probes. Here it picks the word: the reader is waiting on a test rather
   than on a connection being opened, and the row should say so. */
export const probes = (p: Pending): boolean => runsAgent(p.op, p.args)

/* The other write that changes whether the agent is on the roster. It reaches no
   gate -- the server pings on the way on and not on the way off -- so it waits
   on its own write and nothing else, and it is the one write that must not
   borrow the connect's word. */
export const disconnects = (p: Pending): boolean => p.op === 'toggle' && p.args.enabled === false

export interface ExtAgentsState {
  rows: ExtAgentRow[]
  /* Which of the page's two modules is drawn: the agents, or the machines
     they may run on. Both live here because they share the cards, the
     sheets and the drawer; the hub's header switches between them. */
  module: 'agents' | 'machines'
  /* The one flag only the page can answer: which card is open. It is about what
     is drawn, so it does not belong to whichever source is answering. */
  sheet: string | null
  /* Bumped when the rows are replaced: the sheet's form is uncontrolled and
     mutated in place, so a fresh answer remounts it. */
  epoch: number
  /* Names this page has a test in flight for. The rows carry the server's own
     `test_running`, but they are only re-read when a call returns and a test
     can take two minutes, so between the click and the verdict the rows say
     nothing is running. This is what the card reads until the refetch replaces
     it -- and it is a union with the row flag, not a substitute: a test another
     client started is only on the row. */
  testing: string[]
  /* True while a `subagents.list` read is in flight. Reading it directly is
     what lets the wizard's agents step draw a scanning placeholder before the
     first answer lands, and the sheet's re-check button its ring. */
  loading: boolean
  /* The write in flight on each row, by name. `run` repaints every row from one
     shared refetch, which cannot tell two rows apart while both are mid-write --
     this is what a caller checks to disable one row's own button rather than the
     whole list. It keeps *which* write rather than just the name because the two
     kinds wait on different things, and the row says which it is waiting for:
     see `probes`. */
  joining: Record<string, Pending>
  /* The last write that failed, per row. A failure is a state the row is in
     -- red text where the summary was, Retry where Connect was -- rather than
     a toast that is gone before the reader looks up. Cleared by the next
     write on the same row, whichever way that one goes. */
  failed: Record<string, Failure>
  /* What the reader typed into a preset's description before connecting it.
     A preset has no row to write until `subagents.add`, so the text waits
     here and travels with the add. */
  drafts: Record<string, string>
  /* Rows a re-check was asked for and still found absent, so the sheet can
     say "still not found" rather than nothing. Cleared when the row leaves
     the missing section. */
  stillMissing: string[]
  /* The machine card open in the shared drawer -- a registry id, or `new` for
     the register form -- and the agent sheet's twin: at most one of the two
     is set. */
  msheet: string | null
  /* Where the reader wants a preset to run, picked in its sheet before the
     connect: a machine id (empty for this computer) and, for an acp or cli
     row, the working directory there. Held like `drafts`, until the add. */
  wheres: Record<string, { machine: string; remote_cwd: string }>
  /* The family whose picker is open, or null. Deliberately NOT `sheet`: that
     one holds a row name and is resolved by looking it up in `rows`, so a
     group key written there opens an empty drawer -- and `run` closes the
     drawer whenever the open name leaves the rows, which a group key never
     enters. Unlike the sheet, the picker is not remounted per `epoch`: every
     load bumps that, and the picker holds a choice in progress -- a model
     picker open in it -- that a reload after another row's write must not
     throw away. */
  picking: string | null
  /* The model the reader picked for an agent that is not on the roster yet, by
     row name -- in the family picker or in the agent's own sheet. A preset has
     no row to hold it until `subagents.add`, so -- like `drafts` -- the pick
     waits here and travels with the add, which is what makes an agent and its
     model one write. Here rather than in the row's component, which a reload
     would reset to the agent's default without the reader touching it. */
  picked: Record<string, string>
  /* The machine whose "connect an agent on this machine" form is open in the
     drawer, by registry id, or null: the fourth thing the one drawer draws,
     exclusive with the other three. */
  mconnect: string | null
  /* What that form chooses from, read when it opens and again when an add
     lands on its machine while it is open, the latest read being the one
     kept; null before the answer, and the read's refusal beside it. */
  presets: PresetChoice[] | null
  presetsFailed: string
  /* Each opening of that form is a generation, and the write in flight
     carries the generation it was pressed in: the add's readiness ping can
     take a minute, the drawer stays closable meanwhile, and a form closed or
     replaced by then is left alone when the write ends -- its result is told
     in a toast, not on whatever the reader is looking at now. `mconnectBusy`
     is the generation with a write in flight, or null. */
  mconnectGen: number
  mconnectBusy: number | null
  /* That form's refusal -- the agent's words when it did not answer on the
     machine -- for the form it was pressed in. */
  mconnectFailed: string
  /* The adds in flight from any opening of that form, by the name each will
     land under (`<preset>@<machine>`): the generation above says whose form
     a write is, this says that the write is still running -- or has landed,
     and the form open on that machine is still reading its presets again. A
     form opened on the same machine meanwhile greys that preset instead of
     offering a second add of it, which the server would ping and then refuse
     as a duplicate. */
  mconnectPending: string[]
  /* When the write in flight was pressed, for the form to say how long it has
     waited; null when none is. */
  mconnectSince: number | null
  /* How the form's last wait ended without an answer: the reader's Cancel,
     the ceiling, or a stop that found nothing left to stop (`missed`), which
     means the connect was past the point where it could be cancelled and may
     have been written, or a Cancel whose stop never said it stopped the run
     (`unconfirmed`) (`awaitRun`). None is the agent's refusal, and the form
     says each in its own words; cleared by the next press and the next opening. */
  mconnectStopped: '' | Stopped
  /* Cancel pressed, and the run not yet stopped: the button says so once. */
  mconnectCancelling: boolean
  /* The stop answered that nothing was running, so the wait goes on for the
     connect's own answer, and the form says it is too late to cancel. */
  mconnectLate: boolean
}

const store = makeStore<ExtAgentsState>({
  rows: [],
  module: 'agents',
  sheet: null,
  epoch: 0,
  testing: [],
  loading: false,
  joining: {},
  failed: {},
  drafts: {},
  stillMissing: [],
  msheet: null,
  wheres: {},
  picking: null,
  picked: {},
  mconnect: null,
  presets: null,
  presetsFailed: '',
  mconnectGen: 0,
  mconnectBusy: null,
  mconnectFailed: '',
  mconnectPending: [],
  mconnectSince: null,
  mconnectStopped: '',
  mconnectCancelling: false,
  mconnectLate: false,
})

export const { get, subscribe } = store

/* How long the machine form waits for a connect's answer before it stops the
   run itself, and how long it then gives the answer to arrive. The ceiling is
   above the server's own worst case for the add -- the node launch, the
   preset's start window and the answer, then the capability record's two
   requests of up to that window each: 120 + 60 + 240 = 420s for the slowest
   preset, before the link's own calls -- so a connect that is merely slow is
   never cut short, while a wait with no end (a connection that dropped
   without the socket saying so, seen once on 2026-10-20) ends here instead
   of never. */
const CONNECT_CEILING_MS = 10 * 60_000
const STOP_GRACE_MS = 8_000
let connectCeilingMs = CONNECT_CEILING_MS
let stopGraceMs = STOP_GRACE_MS

/* Test seam: the value back to its first, and the two waits above set short
   for a case that cannot wait minutes. */
export function _resetForTests(waits: { ceilingMs?: number; graceMs?: number } = {}): void {
  store._resetForTests()
  connectCeilingMs = waits.ceilingMs ?? CONNECT_CEILING_MS
  stopGraceMs = waits.graceMs ?? STOP_GRACE_MS
}

/** A patch, merged into the page's state. */
export function set(patch: Partial<ExtAgentsState>): void {
  store.set((prev) => ({ ...prev, ...patch }))
}

export const source = (): ExtAgentsSource => ds('extAgents')

/* Handing a failure to Raven opens a conversation with its first message
   written. The verb belongs to the composer, so the app lends it here at boot
   rather than this domain reaching into a sibling's module. */
type AskRaven = (promptKey: string, name: string, vars: Record<string, string>) => void
let askRaven: AskRaven | null = null

export function lendAskRaven(fn: AskRaven | null): void {
  askRaven = fn
}

export function handToRaven(promptKey: string, name: string, vars: Record<string, string>): void {
  askRaven?.(promptKey, name, vars)
}

/* What to show a reader when a call fails. The server's own sentence first: a
   rejected rpc frame carries `message` as the error's *code name*
   ("subagent_not_found") and the reason, when there is one, under `data.detail`.
   Reading `message` first therefore showed the code name and hid the sentence
   written to explain it. */
const failure = (e: unknown): string => {
  const err = e as { message?: string; detail?: string; data?: { detail?: string } } | null
  return (err && ((err.data && err.data.detail) || err.detail || err.message)) || String(e)
}

/* The same refusal's classification, carried beside its sentence under
   `data.remedy` when the server could name a fix. */
const remedyFrom = (e: unknown): Remedy | null => remedyOf((e as { data?: { remedy?: unknown } } | null)?.data?.remedy)

/* Whether the refusal is that the row's run was stopped -- before it
   answered, or while its capabilities were recorded after it did
   (`data.cancelled`): the server writes nothing then, as for any refusal, and
   the row is left as it was rather than marked failed. */
const cancelledFrom = (e: unknown): boolean => !!(e as { data?: { cancelled?: unknown } } | null)?.data?.cancelled

/* `load(true)` re-measures availability. Opening the page is one caller --
   the section heading there is a fresh answer every time the reader arrives --
   the sheet's re-check button is another, and the wizard's agents step a third,
   on its own schedule rather than through page navigation. `rescan` is the
   re-check's alone: see `ExtAgentsSource.load`. */
export async function load(probe: boolean, rescan = false): Promise<void> {
  set({ loading: true })
  try {
    const rows = await source().load(probe, rescan)
    set({ rows, epoch: get().epoch + 1, loading: false })
  } catch (e) {
    set({ loading: false })
    /* Through `failure` like every other rejection here: the rpc client rejects
       with the error frame verbatim, and `String()` on that object is
       "[object Object]" -- not a hard-to-read reason but no reason at all. */
    toast(t('gui.agent.failed', { detail: failure(e) }))
  }
}

export function open(): void {
  set({ module: 'agents' })
  hub.visited('agents')
  page.show('extAgentsPage')
  void load(true)
}
hub.onOpen('agents', open)

/** The page's other module: the registered machines, their links and nodes. */
export function openMachines(): void {
  set({ module: 'machines' })
  hub.visited('machines')
  page.show('extAgentsPage')
  void machines.load()
}
hub.onOpen('machines', openMachines)

export function close(): void {
  page.show(null)
}

/* Writes run side by side, and each ends in a whole-roster read that takes
   as long as a probe. A read that began before a later write landed does not
   have that write, and painted as read it would offer the later agent as
   connectable again though its write succeeded. So writes are counted as
   they start and as they land, and one whose read came back after a later
   write landed reads the roster again -- with the probe, since the verdicts
   it carries must be the newer ones too -- until nothing newer has landed
   under it. */
let started = 0
let landed = 0

/* A write the server refused, as `run` reports it: the server's sentence,
   the fix it named, and whether the refusal is that the run was stopped
   (`cancelled`) -- the reader's Stop or the machine form's Cancel reaching
   the one real prompt a connect proves the row with, which is no failure of
   the row. */
export type Refused = Pick<Failure, 'detail' | 'remedy'> & { cancelled?: boolean }

/* Every write goes through here: one place that repaints from whatever the
   source answered, so no caller has to remember to. A failure is toasted
   unless the caller says it will show it itself; either way it is returned,
   as the server's sentence, so a caller that keeps failures per row can. */
export async function run(
  op: ExtAgentOp,
  row?: ExtAgentRow,
  args?: ExtAgentActArgs,
  opts: { quiet?: boolean } = {},
): Promise<Refused | null> {
  let rows = get().rows
  let failedWith: Refused | null = null
  /* A rename moves the open sheet, but only once the rows that carry the new
     name are here: the sheet is resolved by looking the name up in rows, so
     moving it any earlier resolves to nothing, and the shared drawer -- which
     stays open, since only the lookup went missing -- would sit empty for the
     length of the write. Set on success only, or a rejected rename would point
     the sheet at a name no row will ever have and close the drawer. */
  let renamed = ''
  let gen = ++started
  try {
    rows = await source().act(op, row as ExtAgentRow, args || {})
    if (row && args?.new_name && args.new_name !== row.name) renamed = args.new_name
  } catch (e) {
    const remedy = remedyFrom(e)
    failedWith = remedy ? { detail: failure(e), remedy } : { detail: failure(e) }
    if (cancelledFrom(e)) failedWith.cancelled = true
    /* A refusal repaints from the listing as it is now, never from the rows
       held when this write started: writes run side by side (five Connects
       pressed at once), and a slow one that fails would otherwise put back the
       rows from before the others landed -- rows connected meanwhile read as
       connectable again. It is also how a refused model pick leaves a menu the
       row has since moved off, since rows are re-measured behind the page. */
    rows = await source().load(false).catch(() => get().rows)
    if (!opts.quiet) toast(t('gui.agent.failed', { detail: failedWith.detail }))
  }
  while (landed > gen) {
    gen = ++started
    rows = await source().load(true).catch(() => get().rows)
  }
  landed = gen
  const next: Partial<ExtAgentsState> = { rows, epoch: get().epoch + 1 }
  if (renamed && get().sheet === row?.name) next.sheet = renamed
  /* The row now holds the model, so the waiting pick has done its job. Left
     behind, it came back when the member was disconnected and folded into the
     card again, long after the row had moved off that model. */
  if (!failedWith && op === 'connect' && row && get().picked[row.name]) next.picked = without(get().picked, row.name)
  set(next)
  if (get().sheet && !rows.some((x) => x.name === get().sheet)) closeSheet()
  watchBuilds(rows)
  return failedWith
}

const without = <T>(map: Record<string, T>, name: string): Record<string, T> => {
  if (!(name in map)) return map
  const next = { ...map }
  delete next[name]
  return next
}

/* One row's write, held on `joining` for its length and remembered on `failed`
   when it does not land. Every verb -- the hub's and the wizard's -- comes
   through here. A run the reader stopped is returned but not remembered:
   nothing was written and nothing failed, so the row goes back to what it
   was, with no refusal to retry. */
export async function attempt(row: ExtAgentRow, op: ExtAgentOp, args: ExtAgentActArgs = {}): Promise<Refused | null> {
  set({ joining: { ...get().joining, [row.name]: { op, args } }, failed: without(get().failed, row.name) })
  try {
    const refused = await run(op, row, args, { quiet: true })
    if (refused !== null && !refused.cancelled) {
      const kept: Failure = { op, args, detail: refused.detail }
      if (refused.remedy) kept.remedy = refused.remedy
      set({ failed: { ...get().failed, [row.name]: kept } })
    }
    return refused
  } finally {
    set({ joining: without(get().joining, row.name) })
  }
}

/* The same write, for the callers that only start it and read the row after. */
export async function act(row: ExtAgentRow, op: ExtAgentOp, args: ExtAgentActArgs = {}): Promise<void> {
  await attempt(row, op, args)
}

/* The same write again, as it was asked for. */
export function retry(row: ExtAgentRow): void {
  const last = get().failed[row.name]
  if (last) void act(row, last.op, last.args)
}

/* Connect, by what the row's stage calls for: a preset not yet on the roster is
   added (with whatever the reader wrote about it while it was still a preset),
   one switched off has its flag put back, a stale one -- its preset moved to
   another transport -- is removed and re-added. The page confirms the stale
   case before calling this; the key case never reaches here, since the
   credential is typed in the sheet and saved from there.

   The model the reader picked for the agent travels with that add: a preset has
   no row to hold it until then, and the add is the one write that lands the
   agent and its model together. An update would be refused -- there is no row
   of that name to update yet -- and the agent would run on its own default
   instead of the one that was picked. */
export function connectRow(row: ExtAgentRow): Promise<void> {
  const stage = stageOf(row)
  if (stage === 'add') {
    const description = get().drafts[row.name]
    const held = get().picked[row.name]
    /* A pick outlives a menu that has since dropped it -- the handshake is
       re-measured behind the page -- and the add refuses a model the agent no
       longer offers. The agent's own default is the better answer. */
    const model = held && (row.model_choices || []).some(choice => choice.value === held) ? held : ''
    return act(row, 'connect', { ...(description ? { description } : {}), ...(model ? { model } : {}), ...whereArgs(row.name) })
  }
  if (stage === 'stale') return act(row, 'migrate')
  return act(row, 'toggle', { enabled: true })
}

/* Only marks it unavailable in the registry: the entry, the folder and the
   sessions it already ran all stay, and connect puts it back. */
export function disconnectRow(row: ExtAgentRow): void {
  void act(row, 'toggle', { enabled: false })
}

/* The credential, and the connect that spends it: an entry that exists takes
   the key as an edit; one that does not is written from its preset with the
   key in hand, and with whatever the reader wrote about it while it was still
   a preset. An edit on a row that is off is followed by the switch: the
   server does not prove a key written to a row that is off -- only the switch
   asks the endpoint -- so the edit alone would store it untried and leave the
   row off, and to the reader the two are one press. Not when the edit itself
   was refused. */
export async function saveKey(row: ExtAgentRow, api_key: string): Promise<void> {
  if (!api_key) return
  if (!row.configured) {
    const description = get().drafts[row.name]
    return act(row, 'connect', { api_key, ...(description ? { description } : {}), ...whereArgs(row.name) })
  }
  await act(row, 'update', { api_key })
  if (!row.enabled && !get().failed[row.name]) await act(row, 'toggle', { enabled: true })
}

/* Where a preset will run, as the reader picked it in the sheet: travels with
   the connect, like `drafts`. Nothing is written for this computer. */
export const whereOf = (name: string): { machine: string; remote_cwd: string } =>
  get().wheres[name] || { machine: '', remote_cwd: '' }

export function setWhere(name: string, machine: string, remote_cwd: string): void {
  set({ wheres: { ...get().wheres, [name]: { machine, remote_cwd } } })
}

const whereArgs = (name: string): ExtAgentActArgs => {
  const where = get().wheres[name]
  if (!where || !where.machine) return {}
  return where.remote_cwd ? { machine: where.machine, remote_cwd: where.remote_cwd } : { machine: where.machine }
}

/* What this agent is good at, as the reader words it. A row that exists takes
   it as a write; a preset holds it until the connect that writes the row. */
export function describe(row: ExtAgentRow, text: string): void {
  if (row.configured || row.vendored) void act(row, 'update', { description: text })
  else set({ drafts: { ...get().drafts, [row.name]: text } })
}

/* The row's own model, or back to the agent's default. Held on the row like
   every other write: a refused pick stays in red with the pick to retry, which
   is also the one place the reader can see it did not land. */
export function setModel(row: ExtAgentRow, model: string, provider?: string): Promise<void> {
  return act(row, 'model', provider ? { model, provider } : { model })
}

export function clearModel(row: ExtAgentRow): Promise<void> {
  return act(row, 'model', { clear_model: true })
}

/* What the reader typed for a preset, or nothing. */
export const draftOf = (name: string): string | undefined => get().drafts[name]

/* Measure the machine again for one absent row, and remember when it is still
   absent afterwards -- that is the one answer the sheet has to say out loud.
   Again means from the shell's PATH as it is now: an agent is usually installed
   while this page is open, and its installer's PATH line is invisible to the
   environment the gateway captured when it started. */
export async function recheck(row: ExtAgentRow): Promise<void> {
  await load(true, true)
  const now = get().rows.find((r) => r.name === row.name)
  const still = !!now && sectionOf(now) === 'missing'
  const rest = get().stillMissing.filter((name) => name !== row.name)
  set({ stillMissing: still ? [...rest, row.name] : rest })
}

/* The rows the wizard's step counts against: found on this machine, whatever
   bucket each currently sits in. */
export const found = (): ExtAgentRow[] => get().rows.filter(isFound)

/* Done once one of those is actually enabled -- connecting is the ask, not
   merely having something on the machine to connect. */
export const stepDone = (): boolean => found().some((r) => r.enabled)

/* One test, and the flag that says it is under way. `run` cannot carry this:
   its await *is* the test -- `subagents.test` holds the connection open for the
   whole run and answers with the verdict -- so the rows it repaints from are
   the first news of the test being over, and nothing before them says it began.

   A second press for a name already running is dropped here rather than sent:
   the server refuses it, but its refusal is a normal result, so the toast-free
   path would repaint the card as though the running test had answered. */
export async function runTest(row: ExtAgentRow): Promise<void> {
  if (get().testing.includes(row.name)) return
  set({ testing: [...get().testing, row.name] })
  /* `finally` rather than a line after the await: `run` turns every rejection
     into a toast, so nothing throws through here today -- and a flag cleared
     only on the path that returned would leave the button disabled for the
     rest of the session the first time one does. */
  try {
    await run('test', row)
  } finally {
    set({ testing: get().testing.filter((name) => name !== row.name) })
  }
}

/* Stop it. The cancelled test's own call returns too, with `cancelled` set, and
   repaints from its own refetch -- so this one only has to reach the server. */
export function stopTest(row: ExtAgentRow): void {
  void run('test_cancel', row)
}

/* A build is the one action whose call returns before the work does -- it is a
   few hundred MB of downloads, so `subagents.build` hands back as soon as it is
   under way. Nothing pushes when it finishes, so the row would sit on
   "Installing..." until the reader happened to reload. Re-listing while any row
   carries `building` is what turns it back into "ready" on its own.

   Deliberately dumb about lifetime: one timer at a time, re-armed from the
   result it just read, and never armed when nothing is building. It stops
   because the flag stops, not because anything remembers to cancel it. */
let buildTimer: ReturnType<typeof setTimeout> | null = null
const BUILD_POLL_MS = 4000

function watchBuilds(rows: ExtAgentRow[]): void {
  if (buildTimer !== null) return
  if (!rows.some((r) => r.building)) return
  buildTimer = setTimeout(() => {
    buildTimer = null
    /* Unprobed while a build is in flight -- a probe per poll would re-measure
       every other entry for nothing. Probed once on the poll that finds the last
       build gone, because a cli row's status is what the card renders: without it
       a folder that just finished building reads "unverified", which is not what
       the reader watched happen. */
    void source()
      .load(false)
      .then(async (next) => {
        const done = !next.some((r) => r.building)
        const rows = done ? await source().load(true) : next
        set({ rows, epoch: get().epoch + 1 })
        watchBuilds(rows)
      })
      .catch(() => {
        /* A failed poll is not worth a toast: the build is still running and the
           next action or reload will show where it got to. */
      })
  }, BUILD_POLL_MS)
}

/* ── the shared detail drawer ────────────────────────────────────── */

/** Where the sheet renders: the host the shared drawer keeps for this island. */
export function detailHost(): HTMLDivElement {
  return detail.host('extAgents')
}

export function sheetOpen(row: ExtAgentRow): void {
  detail.open('extAgents')
  /* `msheet` and `picking` go with it: all three portal into the one drawer,
     and a sheet opened over the picker or a machine card would otherwise draw
     beside it. */
  set({ sheet: row.name, msheet: null, picking: null, mconnect: null, epoch: get().epoch + 1 })
}

/** The drawer on a machine card (its registry id), or on the register form (`new`). */
export function machineSheetOpen(id: string): void {
  detail.open('extAgents')
  set({ msheet: id, sheet: null, picking: null, mconnect: null, epoch: get().epoch + 1 })
}

/** The drawer on a machine's "connect an agent on this machine" form; the presets are read as it opens. */
export function machineConnectOpen(id: string): void {
  detail.open('extAgents')
  set({
    mconnect: id,
    msheet: null,
    sheet: null,
    picking: null,
    presets: null,
    presetsFailed: '',
    mconnectGen: get().mconnectGen + 1,
    mconnectBusy: null,
    mconnectFailed: '',
    mconnectStopped: '',
    mconnectCancelling: false,
    mconnectLate: false,
    epoch: get().epoch + 1
  })
  void loadPresets()
}

/* Reads of the presets, counted: only the latest one is written, so a read
   started when a form opened cannot land over the one a finished add asked
   for after it. */
let presetsRead = 0

async function loadPresets(): Promise<void> {
  const read = ++presetsRead
  try {
    const presets = await source().presets()
    if (read === presetsRead) set({ presets, presetsFailed: '' })
  } catch (e) {
    if (read === presetsRead) set({ presets: [], presetsFailed: failure(e) })
  }
}

/* The add from the machine's side: the preset on that machine, under the
   server's default name (`<preset>@<machine>`). `landed` when it was written,
   with the `name` it landed under; the roster is read again then, with the
   probe, so the new card arrives with its verdict. `current` says whether the
   form it was pressed in is still the one open -- the same machine, the same
   opening -- which is the only form a refusal is written to and the only
   drawer the caller may put the machine's sheet back into: closed or replaced
   meanwhile, the write's end changes nothing on screen. The busy mark is
   cleared only when it is still this write's. */
type Stopped = 'cancelled' | 'timeout' | 'missed' | 'unconfirmed'
/* What the stop answered: it found the run and stopped it, it found nothing
   running, or the call failed. Only `stopped` lets a Cancel say nothing was
   written. */
type StopAnswer = 'stopped' | 'nothing' | 'unknown'
/* `answer` resolves whenever the stop answers: a wait that ended before it
   did is corrected by it (`sayStopped`). */
type Outcome<T> = { got: T } | { stopped: Stopped; answer: Promise<StopAnswer> }

/* The end of a form's wait other than the answer, by generation: the form's
   Cancel resolves it, and so does the ceiling's timer. */
const stops = new Map<number, (why: Stopped) => void>()

/* The form's wait on a connect's write, bounded two ways: by the reader's
   Cancel and by the ceiling. Either way the run is stopped server-side -- the
   call that stops a test, on the name the entry has or lands under -- and the
   write is given a moment to answer, with the refusal that says it was
   stopped or with an answer that was already on its way. Past that, the wait
   is over whatever the write does later; the caller still reads a late
   landing. Without this the form sat on one line for as long as the socket
   stayed silent, with nothing to press.

   The stop request is sent, not awaited: the transport gives a call no
   timeout of its own, so on the socket that went silent -- the case the
   ceiling exists for -- awaiting it would hang the wait on a second call
   exactly as the first one hung it. The grace runs from the moment the stop
   is sent, whatever becomes of that call.

   Its answer is still read, alongside the grace: a stop that found nothing
   running means the connect is past the point where it can be cancelled --
   its run answered and it is writing, or its run had not started yet -- and
   it may still land. Then the form does not say it was cancelled: it says it
   is too late to, and goes on waiting for the connect's own answer, up to
   the ceiling (and one more grace when the ceiling is what pressed the stop).
   Unanswered by then, the wait ends as `missed`, which says it may have been
   written. A Cancel whose grace ends before the stop said it stopped the run
   -- no answer yet, or the call failed -- ends as `unconfirmed`, not
   `cancelled`: nothing is known about the write either way. */
async function awaitRun<T>(gen: number, name: string, write: Promise<T>): Promise<Outcome<T>> {
  const answered = write.then((got): Outcome<T> => ({ got }))
  const deadline = Date.now() + connectCeilingMs
  let reason: Stopped = 'cancelled'
  const stopped = new Promise<Stopped>((resolve) => {
    stops.set(gen, (why) => {
      reason = why
      resolve(why)
    })
  })
  const ceiling = setTimeout(() => stops.get(gen)?.('timeout'), connectCeilingMs)
  const timers: ReturnType<typeof setTimeout>[] = []
  const after = (ms: number): Promise<null> => new Promise((resolve) => timers.push(setTimeout(() => resolve(null), ms)))
  try {
    const first = await Promise.race([answered, stopped.then(() => null)])
    if (first) return first
    const heard: { answer?: StopAnswer } = {}
    const answer = source()
      .cancelRun(name)
      .then(
        (ok): StopAnswer => (ok ? 'stopped' : 'nothing'),
        (): StopAnswer => 'unknown'
      )
      .then((said) => (heard.answer = said))
    const late = Symbol('late')
    const second = await Promise.race([answered, after(stopGraceMs), answer.then((said) => (said === 'nothing' ? late : new Promise<never>(() => {})))])
    if (second === late) {
      if (get().mconnectBusy === gen) set({ mconnectLate: true, mconnectCancelling: false })
      const rest = Math.max(deadline - Date.now(), stopGraceMs)
      return (await Promise.race([answered, after(rest)])) ?? { stopped: 'missed', answer }
    }
    if (second) return second
    return { stopped: reason === 'cancelled' && heard.answer !== 'stopped' ? 'unconfirmed' : reason, answer }
  } finally {
    clearTimeout(ceiling)
    for (const timer of timers) clearTimeout(timer)
    stops.delete(gen)
  }
}

/* Each press of the form, counted: a stop's late answer or a late landing
   corrects the note only while it is still the note of the press it belongs
   to, on the form that press was made in. */
let presses = 0

/* The form after a wait that ended without the connect's answer: the note
   for how it ended, then the corrections the note may need later. A stop that
   answers, after the grace, that nothing was running turns the note into
   `missed`; one that answers it stopped the run turns `unconfirmed` into
   `cancelled`, and so does the connect's own refusal as cancelled; and a
   connect that lands after all takes the note down -- it said, or allowed,
   that nothing was written -- and is told in a toast. */
type Late = { name: string } | { cancelled: true } | null
function sayStopped(
  outcome: { stopped: Stopped; answer: Promise<StopAnswer> },
  ours: () => boolean,
  late: Promise<Late>,
  machine: string
): void {
  if (ours()) set({ mconnectStopped: outcome.stopped })
  void outcome.answer.then((said) => {
    if (!ours()) return
    const note = get().mconnectStopped
    if (said === 'nothing' && note !== '') set({ mconnectStopped: 'missed' })
    if (said === 'stopped' && note === 'unconfirmed') set({ mconnectStopped: 'cancelled' })
  })
  void late.then((got) => {
    if (got === null) return
    if ('cancelled' in got) {
      if (ours() && get().mconnectStopped === 'unconfirmed') set({ mconnectStopped: 'cancelled' })
      return
    }
    if (ours()) set({ mconnectStopped: '' })
    landedLate(got.name, machine)
  })
}

/* The form's Cancel: ends the wait of the write pressed in the form open now,
   which stops its run (`awaitRun`). A write whose form was closed or replaced
   is not reachable from here -- its generation is gone -- and the ceiling
   ends it. */
export function cancelConnect(): void {
  const stop = stops.get(get().mconnectGen)
  if (!stop) return
  set({ mconnectCancelling: true })
  stop('cancelled')
}

/* A write that landed after its form stopped waiting -- the Cancel came too
   late, or the answer outlived the ceiling: said in a toast, and the roster
   and the machines read again, as for a landing the form never saw. */
function landedLate(name: string, machine: string): void {
  toast(t('gui.machine.connected_said', { name }))
  void load(true).then(() => {
    if (get().mconnect === machine) void loadPresets()
  })
  void machines.load()
}

export async function connectOnMachine(
  choice: PresetChoice,
  machine: string,
  remoteCwd: string
): Promise<{ landed: boolean; current: boolean; name: string }> {
  const gen = get().mconnectGen
  /* Still the form it was pressed in, and that form still on screen: the
     drawer's own state says whether it is open and ours, since `mconnect`
     stays set through the close fade (`sheetDismissed` drops it after). */
  const current = (): boolean =>
    get().mconnect === machine && get().mconnectGen === gen && detail.get().open && detail.get().owner === 'extAgents'
  const pending = `${choice.preset}@${machine}`
  const press = ++presses
  const ours = (): boolean => current() && presses === press
  set({
    mconnectBusy: gen,
    mconnectFailed: '',
    mconnectStopped: '',
    mconnectCancelling: false,
    mconnectLate: false,
    mconnectSince: Date.now(),
    mconnectPending: [...get().mconnectPending, pending]
  })
  type Answer = { name: string } | { refused: string; cancelled?: boolean }
  const write: Promise<Answer> = source()
    .connectOnMachine(choice, machine, remoteCwd)
    .catch((e): Answer => (cancelledFrom(e) ? { refused: failure(e), cancelled: true } : { refused: failure(e) }))
  const outcome = await awaitRun(gen, pending, write)
  if (get().mconnectBusy === gen) set({ mconnectBusy: null, mconnectCancelling: false, mconnectLate: false })
  const settled = (): void => set({ mconnectPending: get().mconnectPending.filter(name => name !== pending) })
  if ('stopped' in outcome) {
    /* No answer: the wait is over and the form is the reader's again, the
       preset offered once more -- a second press while the first run still
       goes is refused server-side, before anything is spent. */
    settled()
    sayStopped(outcome, ours, write.then((got): Late => ('name' in got ? { name: got.name } : got.cancelled ? { cancelled: true } : null)), machine)
    return { landed: false, current: current(), name: pending }
  }
  const got = outcome.got
  if ('name' in got) {
    await load(true)
    /* The preset just landed on the machine, so a form open on that machine
       has to read its choices again -- whichever opening it is: one opened
       while the write ran holds a copy that predates it, and would offer the
       preset once more, which a press answers with a second add the server
       refuses before any ping, right after a toast said connected. The
       in-flight mark comes off only once that read is in, so the preset goes
       from being connected to taken with no moment in between where it is
       offered. `current()` is the wrong test here: it is about the form the
       write was pressed in, and the reader may have reopened it since. */
    if (get().mconnect === machine) await loadPresets()
    settled()
    return { landed: true, current: current(), name: got.name }
  }
  settled()
  if (current()) set(got.cancelled ? { mconnectStopped: 'cancelled' } : { mconnectFailed: got.refused })
  return { landed: false, current: current(), name: pending }
}

/* The other press the connect form has: an entry of the preset is already
   on the machine, switched off, and Reconnect switches it back on under the
   name it has -- the agents page's own toggle, with its proof ping, run from
   the machine's side -- rather than adding a second one, which the server
   would refuse. Guarded like `connectOnMachine`: the generation says whose
   form the write is, `current()` whether that form is still on screen. */
export async function reconnectOnMachine(
  name: string,
  machine: string
): Promise<{ landed: boolean; current: boolean; name: string }> {
  const gen = get().mconnectGen
  const current = (): boolean =>
    get().mconnect === machine && get().mconnectGen === gen && detail.get().open && detail.get().owner === 'extAgents'
  const press = ++presses
  const ours = (): boolean => current() && presses === press
  set({
    mconnectBusy: gen,
    mconnectFailed: '',
    mconnectStopped: '',
    mconnectCancelling: false,
    mconnectLate: false,
    mconnectSince: Date.now(),
    mconnectPending: [...get().mconnectPending, name]
  })
  /* The roster row, so the write addresses it the way the agents page does;
     a roster that has not caught up yet still knows the name. Through `act`,
     not the source directly: that marks the row `joining` for the length of
     the ping, so the agents page draws it pending and cannot press its own
     Connect meanwhile (and this form, reading `joining`, the other way
     round), the refusal lands on the row as it would from there, and the
     roster is read again. */
  const row = get().rows.find(r => r.name === name) ?? ({ name, kind: 'acp', configured: true, enabled: false } as ExtAgentRow)
  const write = attempt(row, 'toggle', { enabled: true })
  const outcome = await awaitRun(gen, name, write)
  if (get().mconnectBusy === gen) set({ mconnectBusy: null, mconnectCancelling: false, mconnectLate: false })
  const settled = (): void => set({ mconnectPending: get().mconnectPending.filter(n => n !== name) })
  if ('stopped' in outcome) {
    settled()
    /* The row's own pending mark goes with the wait: `act` takes it off when
       the write answers, which a write with no answer never does. */
    set({ joining: without(get().joining, name) })
    sayStopped(outcome, ours, write.then((refused): Late => (refused === null ? { name } : refused.cancelled ? { cancelled: true } : null)), machine)
    return { landed: false, current: current(), name }
  }
  const refused = outcome.got
  if (refused === null) {
    if (get().mconnect === machine) await loadPresets()
    settled()
    return { landed: true, current: current(), name }
  }
  settled()
  if (current()) set(refused.cancelled ? { mconnectStopped: 'cancelled' } : { mconnectFailed: refused.detail })
  return { landed: false, current: current(), name }
}

export function closeSheet(): void {
  detail.close()
}

/* ── the family picker ───────────────────────────────────────────── */

/* The group card's drawer: which family it stands for, kept apart from `sheet`
   for the reason `ExtAgentsState.picking` gives. A separate verb pair rather
   than a row-shaped one, because there is no row behind a group card -- the
   nine rows it stands for each have a name, and the card has none. */
export function pickerOpen(family: string): void {
  detail.open('extAgents')
  set({ picking: family, sheet: null, msheet: null, mconnect: null })
}

/* A model chosen for a member before it is connected; see `picked`. */
export function pickModel(row: ExtAgentRow, model: string): void {
  set({ picked: { ...get().picked, [row.name]: model } })
}

/* That pick, put back to the agent's own default. There is no row to clear it
   from -- the agent is not on the roster yet -- so what is dropped is the pick
   itself, and the add that follows runs the agent on its own default. */
export function clearPicked(name: string): void {
  const picked = { ...get().picked }
  delete picked[name]
  set({ picked })
}

export function pickerClose(): void {
  detail.close()
}

/* Choosing an agent out of the group card IS connecting it. The picker picks
   the agent and its model, presses one button, and the member stops folding
   because `subagents.add` wrote it -- that is what makes the card that appears
   afterwards evidence rather than a local flag. The model rides the same add,
   so the row is never briefly connected on the wrong one.

   The drawer stays up for the length of the connect, which runs the agent for
   up to a minute: the row it was asked on is the only place that says so, since
   the member is still folded into the card behind. It closes once the add has
   landed, so the card it made is what the reader sees next -- unless a
   refusal is standing on the row, the reader has closed or reopened the drawer
   meanwhile (`gen`), or another member is still connecting in it. */
export async function pickAndConnect(row: ExtAgentRow): Promise<void> {
  const gen = detail.get().gen
  const family = familyOf(row)
  await connectRow(row)
  const drawer = detail.get()
  if (get().failed[row.name] || !drawer.open || drawer.gen !== gen || get().picking !== family) return
  if (get().rows.some((other) => familyOf(other) === family && get().joining[other.name])) return
  detail.close()
}

/* What this island does when the drawer closes, whoever closed it (Esc, the
   close button, a click outside, a page switch): drop the card -- but not until
   the drawer has finished fading, or the card is gone from inside a panel that
   is still on screen. */
export function sheetDismissed(): void {
  /* A connect form's write may still be running: from this tick it is no
     longer the form's, whatever the fade still shows -- the drop below comes
     a fade later, and a write ending inside that window must not reopen the
     drawer the reader just closed, nor write to the fading form. */
  if (get().mconnect) set({ mconnectGen: get().mconnectGen + 1, mconnectBusy: null, mconnectCancelling: false })
  if (!get().sheet && !get().msheet && !get().picking && !get().mconnect) return
  /* Which open this close belongs to. The name cannot answer that: closing a
     card and pressing the same row again inside the fade writes the same string
     back, so the pending drop read it as "still mine" and cleared the card that
     had just opened. `epoch` is no good either -- a background reload bumps it,
     which would read as a reopen and leave the closed card up. */
  const gen = detail.get().gen
  detail.dropAfterFade(
    () => set({ sheet: null, msheet: null, picking: null, mconnect: null }),
    () => detail.get().gen !== gen,
  )
}
detail.onClose('extAgents', sheetDismissed)

/* A language flip changes nothing in this state, but every visible string
   comes from t(), so a re-render is the whole redraw. */
export function redraw(): void {
  set({})
}
