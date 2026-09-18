/* The seven module pages, declared once.
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
  /** Which rail button lights up while it is open; two means its own two tabs. */
  readonly navButtons: readonly string[]
  /** Its place in the Escape chain, low first (state/escapeOrder.ts). */
  readonly escapeRank: number
  /** The key its heading speaks, absent for the one page that renders its own. */
  readonly head?: string
  /** The key its accessible name speaks, which can differ from the heading's. */
  readonly aria?: string
  /** The page whose interior is a file of its own (src/chrome/CapsPage.tsx). */
  readonly own?: true
}

/* The seven, in the order they sit among the body's children. `as const` so
   that `PageId` below is the union of the ids rather than `string`: a page
   named nowhere in this table is then a compile error at every table derived
   from it. Read through `PAGES`, which is the same rows with the shape above
   rather than seven shapes of one row each. */
const DECLARED = [
  { id: 'capsPage', bodyId: 'capsBody', navButtons: ['skillBtn', 'plugBtn'], escapeRank: 5, own: true },
  { id: 'extAgentsPage', bodyId: 'extAgentsBody', navButtons: ['moreBtn'], escapeRank: 6, head: 'gui.page.agents', aria: 'gui.page.agents' },
  { id: 'connectionsPage', bodyId: 'connectionsBody', navButtons: ['moreBtn'], escapeRank: 7, head: 'gui.page.conn', aria: 'gui.page.conn' },
  /* The memory page is announced by its hero's phrase rather than by its
     heading, which is why the two keys differ. */
  { id: 'memoryPage', bodyId: 'memoryBody', navButtons: ['memoryBtn'], escapeRank: 2, head: 'gui.nav.mem', aria: 'gui.mem.hero' },
  { id: 'playbooksPage', bodyId: 'playbooksBody', navButtons: ['playbooksBtn'], escapeRank: 3, head: 'gui.nav.pb', aria: 'gui.nav.pb' },
  { id: 'kbPage', bodyId: 'kbBody', navButtons: ['kbBtn'], escapeRank: 4, head: 'gui.nav.kb', aria: 'gui.nav.kb' },
  { id: 'cronPage', bodyId: 'cronBody', navButtons: ['moreBtn'], escapeRank: 1, head: 'gui.page.cron', aria: 'gui.page.cron' },
] as const satisfies readonly ModulePage[]

/** The seven module pages, keyed as their `<section>` ids. */
export type PageId = (typeof DECLARED)[number]['id']

/** Every rail button a page can light, as the table declares them. */
export type NavButton = (typeof DECLARED)[number]['navButtons'][number]

/** One row of the table. */
export interface Page extends ModulePage {
  readonly id: PageId
}

/** The seven, in the order they sit among the body's children. */
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
