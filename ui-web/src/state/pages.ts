/* The module pages, declared once.
 *
 * A page used to be a fact spelled out in nine places: the section src/App.tsx
 * renders, the id `state/page.ts` writes the open flag on, the rail button it
 * lights, the row Escape reaches it by, its place among the body's children,
 * the button the rail marks, the golden the region test writes, the host
 * src/main.tsx mounts its island into, and the responder its source reads. Six
 * of those failed silently when a new page missed them -- a page with no
 * selected state, no Escape row, no island at all -- and one of them shipped
 * that way (scripts/gates/rail-nav-registry.test.mjs says which).
 *
 * So each of those tables is derived from this one. The order below is the
 * order the pages sit in the body (src/App.tsx renders them in it, and
 * state/portals.ts's BOOT_BODY_ORDER records it); `escapeRank` is a separate
 * order on purpose, because what Escape takes back first is not what is
 * rendered first (state/escapeOrder.ts says why).
 *
 * Data only, and in state/ rather than in features/: every table above reads
 * it, and a page's identity is strings. What a domain DOES -- its source, its
 * root component, how it mounts -- is its own manifest
 * (features/<domain>/manifest.ts), and scripts/gates/domain-registration.test.mjs
 * holds the two equal.
 */

/** One module page, as every table that names one needs it. */
export interface ModulePage {
  /** The `<section>` id, which is also the page's name everywhere. */
  readonly id: string
  /** The empty box inside it that an island root fills. */
  readonly bodyId: string
  /** Which rail button lights up while it is open. */
  readonly navButtons: readonly string[]
  /** Its place in the Escape chain, low first (state/escapeOrder.ts). */
  readonly escapeRank: number
  /** The key its heading speaks, absent for the one page that renders its own. */
  readonly head?: string
  /** The key its accessible name speaks, which can differ from the heading's. */
  readonly aria?: string
}

/* The rows, in the order they sit among the body's children. `as const` so
   that `PageId` below is the union of the ids rather than `string`: a page
   named nowhere in this table is then a compile error at every table derived
   from it. Read through `PAGES`, which is the same rows with the shape above
   rather than a shape per row.

   One page, because a place and a setting are different things. Schedules,
   channels and memory are set up once and then left alone, so they are
   sections of the settings dialog now (features/settings/store.ts's SECTIONS)
   rather than pages of their own; the agent hub is the one module a reader
   goes TO. The table stays a table: what a page costs to declare is what kept
   six registrations from going stale, and it is the same cost for one row. */
const DECLARED = [
  { id: 'extAgentsPage', bodyId: 'extAgentsBody', navButtons: ['agentsBtn'], escapeRank: 1, head: 'gui.page.agents', aria: 'gui.page.agents' },
] as const satisfies readonly ModulePage[]

/** The module pages, keyed as their `<section>` ids. */
export type PageId = (typeof DECLARED)[number]['id']

/** Every rail button a page can light, as the table declares them. */
export type NavButton = (typeof DECLARED)[number]['navButtons'][number]

/** One row of the table. */
export interface Page extends ModulePage {
  readonly id: PageId
}

/** In the order they sit among the body's children. */
export const PAGES: readonly Page[] = DECLARED

/** A page, by the id every table keys it under. */
export const pageOf = (id: string): ModulePage | undefined => PAGES.find((page) => page.id === id)

/** The pages in the order Escape reaches them. */
export const byEscape = (): readonly Page[] =>
  [...PAGES].sort((a, b) => a.escapeRank - b.escapeRank)

/* Every rail button a page can light, the draft row's first: the order the
   rail's own mark walks them in, which is per element and so decides nothing
   but the reading (features/rail/store.ts's markNew). */
export const NAV_BUTTONS: readonly string[] = [
  'newBtn',
  ...[...new Set(PAGES.flatMap((page) => page.navButtons))],
]
