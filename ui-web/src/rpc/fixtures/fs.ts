/* The file actions the page asks a host for, and the one listing it browses.
 *
 * An upload answers with the path it would have written to, which is what the
 * attachment tray stages and hands the agent as text; reveal and open are the
 * desktop shell's doors, and with no shell behind this page arriving is all
 * they do. `fs.dirs` answers a small fixed tree under a home directory, enough
 * for the working-directory picker to be walked: one folder the engine would
 * refuse (`raven-home`, standing for the agent's own data) whose child is a
 * fine workspace all the same, which is the one shape the picker draws
 * differently.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'

/* The tree the picker walks, by absolute path; a path not listed is an empty
   folder. */
const TREE: Record<string, string[]> = {
  '/': ['home'],
  '/home': ['me'],
  '/home/me': ['raven-home', 'thesis', 'work'],
  '/home/me/work': ['notes', 'raven'],
  '/home/me/raven-home': ['projects', 'workspace'],
}

/* Where a conversation may not be pinned: the agent's own data and the folder
   above it (raven.agent.workdir's rule). */
const REFUSED = new Set(['/home/me/raven-home', '/home/me/raven-home/workspace'])

export interface FsFixture {
  fixtures: Fixtures
}

export function createFs(_env: FixtureEnv): FsFixture {
  return {
    fixtures: {
      'fs.upload': (p) => {
        const name = (p as { name?: string }).name || 'file'
        return { path: `uploads/${name}`, abs_path: `~/work/raven/uploads/${name}`, size: 0 }
      },
      /* The contract gives these no way to say no -- `ok` is declared `true` --
         and both callers ignore the answer, so arriving is the whole of it. */
      'fs.reveal': () => ({ ok: true }),
      'fs.open': () => ({ ok: true }),
      'fs.dirs': (p) => {
        const home = '/home/me'
        const at = (p as { path?: string }).path || home
        const parent = at === '/' ? null : at.replace(/\/[^/]*$/, '') || '/'
        const children = TREE[at] || []
        return {
          path: at,
          parent,
          home,
          ok: !REFUSED.has(at),
          entries: children.map((name) => ({ name, path: `${at}/${name}`, ok: !REFUSED.has(`${at}/${name}`) })),
        }
      },
    },
  }
}
