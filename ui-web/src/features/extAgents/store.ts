import { t } from '../../i18n/t'
import * as detail from '../../state/detail'
import * as page from '../../state/page'
import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'
import { show as toast } from '../../state/toast'
import { isFound, sectionOf, stageOf } from './source'

import type { ExtAgentActArgs, ExtAgentOp, ExtAgentRow, ExtAgentsSource } from './types'

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
}

export interface ExtAgentsState {
  rows: ExtAgentRow[]
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
  /* Names with a connect or disconnect write in flight. `run` repaints every
     row from one shared refetch, which cannot tell two rows apart while both
     are mid-write -- this is what a caller checks to disable one row's own
     button rather than the whole list. The enable gate pings a cli or acp
     agent for up to 60s, so this is also the length of "connecting". */
  joining: string[]
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
}

const store = makeStore<ExtAgentsState>({
  rows: [],
  sheet: null,
  epoch: 0,
  testing: [],
  loading: false,
  joining: [],
  failed: {},
  drafts: {},
  stillMissing: [],
})

export const { get, subscribe, _resetForTests } = store

/** A patch, merged into the page's state. */
export function set(patch: Partial<ExtAgentsState>): void {
  store.set((prev) => ({ ...prev, ...patch }))
}

export const source = (): ExtAgentsSource => ds('extAgents')

/* What to show a reader when a call fails. The server's own sentence first: a
   rejected rpc frame carries `message` as the error's *code name*
   ("subagent_not_found") and the reason, when there is one, under `data.detail`.
   Reading `message` first therefore showed the code name and hid the sentence
   written to explain it. */
const failure = (e: unknown): string => {
  const err = e as { message?: string; detail?: string; data?: { detail?: string } } | null
  return (err && ((err.data && err.data.detail) || err.detail || err.message)) || String(e)
}

/* `load(true)` re-measures availability. Opening the page is one caller --
   the section heading there is a fresh answer every time the reader arrives --
   the sheet's re-check button is another, and the wizard's agents step a third,
   on its own schedule rather than through page navigation. */
export async function load(probe: boolean): Promise<void> {
  set({ loading: true })
  try {
    const rows = await source().load(probe)
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
  page.show('extAgentsPage')
  void load(true)
}

export function close(): void {
  page.show(null)
}

/* Every write goes through here: one place that repaints from whatever the
   source answered, so no caller has to remember to. A failure is toasted
   unless the caller says it will show it itself; either way it is returned,
   as the server's sentence, so a caller that keeps failures per row can. */
export async function run(
  op: ExtAgentOp,
  row?: ExtAgentRow,
  args?: ExtAgentActArgs,
  opts: { quiet?: boolean } = {},
): Promise<string | null> {
  let rows = get().rows
  let failedWith: string | null = null
  /* A rename moves the open sheet, but only once the rows that carry the new
     name are here: the sheet is resolved by looking the name up in rows, so
     moving it any earlier resolves to nothing, and the shared drawer -- which
     stays open, since only the lookup went missing -- would sit empty for the
     length of the write. Set on success only, or a rejected rename would point
     the sheet at a name no row will ever have and close the drawer. */
  let renamed = ''
  try {
    rows = await source().act(op, row as ExtAgentRow, args || {})
    if (row && args?.new_name && args.new_name !== row.name) renamed = args.new_name
  } catch (e) {
    failedWith = failure(e)
    if (!opts.quiet) toast(t('gui.agent.failed', { detail: failedWith }))
  }
  const landed: Partial<ExtAgentsState> = { rows, epoch: get().epoch + 1 }
  if (renamed && get().sheet === row?.name) landed.sheet = renamed
  set(landed)
  if (get().sheet && !rows.some((x) => x.name === get().sheet)) closeSheet()
  watchBuilds(rows)
  return failedWith
}

const without = (map: Record<string, Failure>, name: string): Record<string, Failure> => {
  if (!(name in map)) return map
  const next = { ...map }
  delete next[name]
  return next
}

/* One row's write, held on `joining` for its length and remembered on `failed`
   when it does not land. The page's verbs all come through here; the wizard's
   `connect` / `disconnect` below keep their toast. */
export async function act(row: ExtAgentRow, op: ExtAgentOp, args: ExtAgentActArgs = {}): Promise<void> {
  set({ joining: [...get().joining, row.name], failed: without(get().failed, row.name) })
  try {
    const detail = await run(op, row, args, { quiet: true })
    if (detail !== null) set({ failed: { ...get().failed, [row.name]: { op, args, detail } } })
  } finally {
    set({ joining: get().joining.filter((name) => name !== row.name) })
  }
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
   credential is typed in the sheet and saved from there. */
export function connectRow(row: ExtAgentRow): void {
  const stage = stageOf(row)
  if (stage === 'add') {
    const description = get().drafts[row.name]
    void act(row, 'connect', description ? { description } : {})
  } else if (stage === 'stale') void act(row, 'migrate')
  else void act(row, 'toggle', { enabled: true })
}

/* Only marks it unavailable in the registry: the entry, the folder and the
   sessions it already ran all stay, and connect puts it back. */
export function disconnectRow(row: ExtAgentRow): void {
  void act(row, 'toggle', { enabled: false })
}

/* The credential, and the connect that spends it: an entry that exists takes
   the key as an edit; one that does not is written from its preset with the
   key in hand. */
export function saveKey(row: ExtAgentRow, api_key: string): void {
  if (!api_key) return
  void act(row, row.configured ? 'update' : 'connect', { api_key })
}

/* What this agent is good at, as the reader words it. A row that exists takes
   it as a write; a preset holds it until the connect that writes the row. */
export function describe(row: ExtAgentRow, text: string): void {
  if (row.configured || row.vendored) void act(row, 'update', { description: text })
  else set({ drafts: { ...get().drafts, [row.name]: text } })
}

/* What the reader typed for a preset, or nothing. */
export const draftOf = (name: string): string | undefined => get().drafts[name]

/* Measure the machine again for one absent row, and remember when it is still
   absent afterwards -- that is the one answer the sheet has to say out loud. */
export async function recheck(row: ExtAgentRow): Promise<void> {
  await load(true)
  const now = get().rows.find((r) => r.name === row.name)
  const still = !!now && sectionOf(now) === 'missing'
  const rest = get().stillMissing.filter((name) => name !== row.name)
  set({ stillMissing: still ? [...rest, row.name] : rest })
}

/* The wizard's one connect, choosing the write by the row's stage the way the
   settings page does: a preset not yet on the roster is added, one switched
   off (configured, or one of Raven's own shipped agents) has its flag put
   back, and a stale one -- its preset moved to another transport -- is removed
   and re-added, because a flag would leave the old command line in place.
   Held in `joining` for the length of the write so a caller can disable that
   one row alone -- `subagents.add` pings the agent for up to 60s and may
   refuse. */
export async function connect(row: ExtAgentRow): Promise<void> {
  const stage = stageOf(row)
  const op: ExtAgentOp = stage === 'add' ? 'connect' : stage === 'stale' ? 'migrate' : 'toggle'
  set({ joining: [...get().joining, row.name] })
  try {
    await run(op, row, op === 'toggle' ? { enabled: true } : {})
  } finally {
    set({ joining: get().joining.filter((name) => name !== row.name) })
  }
}

/* Only marks it unavailable in the registry, same as the settings page's own
   disconnect -- the entry stays, and a later connect puts it back. */
export async function disconnect(row: ExtAgentRow): Promise<void> {
  set({ joining: [...get().joining, row.name] })
  try {
    await run('toggle', row, { enabled: false })
  } finally {
    set({ joining: get().joining.filter((name) => name !== row.name) })
  }
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
  set({ sheet: row.name, epoch: get().epoch + 1 })
}

export function closeSheet(): void {
  detail.close()
}

/* What this island does when the drawer closes, whoever closed it (Esc, the
   close button, a click outside, a page switch): drop the card -- but not until
   the drawer has finished fading, or the card is gone from inside a panel that
   is still on screen. */
export function sheetDismissed(): void {
  if (!get().sheet) return
  /* Which open this close belongs to. The name cannot answer that: closing a
     card and pressing the same row again inside the fade writes the same string
     back, so the pending drop read it as "still mine" and cleared the card that
     had just opened. `epoch` is no good either -- a background reload bumps it,
     which would read as a reopen and leave the closed card up. */
  const gen = detail.get().gen
  detail.dropAfterFade(
    () => set({ sheet: null }),
    () => detail.get().gen !== gen,
  )
}
detail.onClose('extAgents', sheetDismissed)

/* A language flip changes nothing in this state, but every visible string
   comes from t(), so a re-render is the whole redraw. */
export function redraw(): void {
  set({})
}
