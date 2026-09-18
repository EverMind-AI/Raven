/* The More group of the rail.
 *
 * Sub-agents, entrances and schedules live behind a fold, because they are
 * set-up-once surfaces
 * rather than daily destinations. Three rows built from a table, into the gap
 * above the fold's own button (#moreFly in the markup). They are .navi rows,
 * the same shape as the four modules above them: unfolding the group makes one
 * list longer rather than opening a second, indented one under it.
 *
 * The rows themselves are rendered from `rows` below by src/chrome/MoreFly.tsx,
 * which is the component that owns #moreFly and everything inside it. What is
 * left here is the group's own state and the one thing about these rows that
 * React must not own: their `aria-current`. The marks are decided together with
 * the marks on the rail's own nav buttons, which live outside any root a
 * component could hold, so the rail island's markNew() asks mark() below for
 * the rows and reads back whether the group stood open -- one writer per
 * element, named. React cannot undo those marks: it never renders the attribute,
 * and it diffs against the props it rendered last rather than against the
 * document.
 *
 * `#moreFly[data-open]` is written here too. It is the flag the stylesheet
 * unfolds the group off (src/styles/page.css), and every reader of "is the
 * group open" -- mark() included -- is an imperative one, so it stays a write by
 * id; the button's `aria-expanded` is the same fact rendered from `open` below.
 *
 * The three openers are imported from the islands that own those pages. The
 * rail's marker is not: it registers itself here instead (`onMark` below), so
 * the edge between the two runs one way -- the rail reads this module and this
 * module is only read -- where importing it back made the two a knot whose
 * evaluation order was the bundler's to decide rather than either file's.
 */

import { open as openConnections } from '../features/connections/wire'
import { open as openCron } from '../features/cron/store'
import { open as openExtAgents } from '../features/extAgents/store'
import { makeStore } from './store'

export interface NavRow {
  page: string
  nameKey: string
  go(): void
}

export const MORE_ROWS: readonly NavRow[] = [
  {
    page: 'extAgentsPage',
    nameKey: 'gui.nav.agents',
    go: () => openExtAgents(),
  },
  {
    page: 'connectionsPage',
    nameKey: 'gui.nav.conn',
    go: () => openConnections(),
  },
  {
    page: 'cronPage',
    nameKey: 'gui.nav.cron',
    go: () => openCron(),
  },
]

export interface NavFlyState {
  /** Whether the group stands unfolded. Served folded. */
  readonly open: boolean
  /** The rows that have been drawn. Empty until the group is first opened. */
  readonly rows: readonly NavRow[]
}

/* Served empty: page.html handed #moreFly over with no children, and the rows
   are drawn on the way open. Once drawn they stay -- folding the group is a
   flag, not a teardown. */
const NONE: readonly NavRow[] = []
const store = makeStore<NavFlyState>({ open: false, rows: NONE })

/* The rail island's markNew, which it registers here when its own module
   evaluates (features/rail/store.ts): the buttons above these rows are the
   rail's to write, and this layer may not import a domain to ask. Spent
   synchronously at the two moments below, so a picked row and a fold each
   leave one decided strip behind them in the same task. Empty on a page whose
   rail never evaluated, which is a page with no buttons to decide. */
let remark: (() => void) | null = null

/** Registers what re-decides the rail's marks when this group changes. */
export function onMark(fn: () => void): void {
  remark = fn
}

/** The group's state, for <MoreFly/> and for the fold's own button. */
export const { get, subscribe } = store

/** For useSyncExternalStore: called whenever the fold or the rows change. */
/* Committed synchronously: draw() marks the rows straight afterwards, and the
   rows have to be in the document by then. */
export function set(next: Partial<NavFlyState>): void {
  const now = get()
  const merged = { ...now, ...next }
  if (merged.open === now.open && merged.rows === now.rows) return
  store.set(merged)
}

const fly = (): HTMLElement | null => document.getElementById('moreFly')

const pageUp = (page: string): boolean => document.getElementById(page)?.dataset.open === 'true'

/* One pass over whichever rows stand in the group, in the table's order. Read
   off the document rather than off `rows` so that the pass covers what is
   really there: the rail island's own test hands #moreFly three rows of its
   own, and this is the writer it asks. */
function paint(): void {
  const box = fly()
  if (!box) return
  box.querySelectorAll('.navi').forEach((b, i) => {
    const row = MORE_ROWS[i]
    b.setAttribute('aria-current', String(!!row && pageUp(row.page)))
  })
}

export function draw(): void {
  set({ rows: MORE_ROWS })
  paint()
}

/* Re-reads the mark on every row and answers whether the group stands open.
   Called by the rail island's markNew(), which owns the buttons above: while
   the group is open its rows are rail rows and the current one wears the mark
   itself, so the parent must not also claim it. */
export function mark(): boolean {
  const box = fly()
  if (!box || box.dataset.open !== 'true') return false
  paint()
  return true
}

/* A row's own click. The group stays open on a pick: it is navigation now, and
   the row's own current mark is the answer to "where am I". Here rather than in
   the component so that the rows ask for the marks through the registration
   above, instead of a second edge into the rail from chrome/. */
export function pick(row: NavRow): void {
  row.go()
  remark?.()
}

export function toggle(force?: boolean): void {
  const box = fly()
  if (!box) return
  const open = force != null ? force : box.dataset.open !== 'true'
  if (open) draw()
  box.dataset.open = String(open)
  set({ open })
  remark?.()
}

/* Back to the state a fresh page starts in: folded, with no rows drawn. For a
   test, the way state/sources.ts's resetSources() is -- the rows outlive a
   remount, so a case that drew them must not hand them to the next one. The
   page never calls this. */
export function _resetForTests(): void {
  set({ open: false, rows: NONE })
}
