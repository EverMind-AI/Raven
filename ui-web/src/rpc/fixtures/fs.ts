/* The three file actions the page asks a host for.
 *
 * An upload answers with the path it would have written to, which is what the
 * attachment tray stages and hands the agent as text; reveal and open are the
 * desktop shell's doors, and with no shell behind this page arriving is all
 * they do.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'

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
    },
  }
}
