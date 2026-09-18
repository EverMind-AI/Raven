import { t } from '../../i18n/t'
import * as detail from '../../state/detail'
import * as page from '../../state/page'
import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'
import { show as toast } from '../../state/toast'

import type { ExtAgentActArgs, ExtAgentOp, ExtAgentRow, ExtAgentsSource } from './types'

/* Page state, outside React on purpose: two of the callers that drive this page
 * are not React. The More row opens it (state/navfly.ts) and Esc closes it
 * (state/escapeOrder.ts) -- so the state lives in a plain store those two can
 * call, and the component subscribes.
 */

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
}

const store = makeStore<ExtAgentsState>({ rows: [], sheet: null, epoch: 0, testing: [] })

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

/* `load(true)` re-measures availability, and opening the page is the only thing
   that asks for it now: the group heading in the page is a fresh answer every
   time the reader arrives, which is what the re-check button used to be for. */
export function open(): void {
  page.show('extAgentsPage')
  void source()
    .load(true)
    .then((rows) => set({ rows, epoch: get().epoch + 1 }))
    /* Through `failure` like every other rejection here: the rpc client rejects
       with the error frame verbatim, and `String()` on that object is
       "[object Object]" -- not a hard-to-read reason but no reason at all. */
    .catch((e: unknown) => toast(t('gui.agent.failed', { detail: failure(e) })))
}

export function close(): void {
  page.show(null)
}

/* Every write goes through here: one place that toasts the failure and repaints
   from whatever the source answered, so no caller has to remember either. */
export async function run(op: ExtAgentOp, row?: ExtAgentRow, args?: ExtAgentActArgs): Promise<void> {
  let rows = get().rows
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
    toast(t('gui.agent.failed', { detail: failure(e) }))
  }
  const landed: Partial<ExtAgentsState> = { rows, epoch: get().epoch + 1 }
  if (renamed && get().sheet === row?.name) landed.sheet = renamed
  set(landed)
  if (get().sheet && !rows.some((x) => x.name === get().sheet)) closeSheet()
  watchBuilds(rows)
}

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

/** Where the card renders: the host the shared drawer keeps for this island. */
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
