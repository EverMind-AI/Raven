/* Importing a conversation, canned: a package goes up and a row comes back.
 *
 * The same file name sent twice without `force` answers `duplicate` with the
 * first import, which is the one way this canvas can show the reader that
 * question; with `force` it is a second copy. Keys are counted rather than
 * random and the time is the injected clock's, so two libraries answer the
 * same bytes.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ResultOf } from '../generated'
import type { SessionsFixture } from './sessions'

type Upload = ResultOf<'imported.upload'>

export function createImported(env: FixtureEnv, sessions: SessionsFixture): { fixtures: Fixtures } {
  const first = new Map<string, Upload>()
  let minted = 0
  return {
    fixtures: {
      'imported.upload': (p) => {
        const name = p.name || 'package.tar.gz'
        const before = first.get(name)
        if (before && !p.force) return { ...before, status: 'duplicate' }
        minted += 1
        const answer: Upload = {
          status: 'imported',
          session_key: `imported:${minted.toString(16).padStart(32, '0')}`,
          title: name.replace(/\.(tar\.gz|tgz)$/i, ''),
          kind: 'export',
          source_name: name,
          imported_at: new Date(env.now()).toISOString(),
        }
        sessions.addImported(answer.session_key, answer.title, { kind: answer.kind, source_name: name })
        if (!before) first.set(name, answer)
        return answer
      },
    },
  }
}
