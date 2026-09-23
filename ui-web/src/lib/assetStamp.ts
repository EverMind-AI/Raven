/* The query that makes a bundled asset's URL change when its file does.
 *
 * These files live at one unversioned path each, so a replaced drawing lands at
 * exactly the URL its predecessor is cached under -- and a client that decided
 * the old copy was fresh keeps showing it through a rebuild, a server restart
 * and a hard reload. The asset tree's digest in the query makes a changed file
 * a different URL, which no cache can answer from what it already holds.
 *
 * Absent outside the build (tests, the vite dev server), where the plain path
 * is what the assertions and the loader both expect. */

export const assetStamp = (): string => {
  const v = (window as unknown as { __ASSETV?: string }).__ASSETV
  return v && v !== '__ASSETV__' ? `?v=${v}` : ''
}
