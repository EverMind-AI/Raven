/* The memory backend, which this page does not have.
 *
 * The canvas answers "the engine is not there" rather than an empty library:
 * the two read differently on the page, and only one of them is true with no
 * backend behind it. `note` is what the island renders as the down note.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'

export interface MemoryFixture {
  fixtures: Fixtures
}

export function createMemory(_env: FixtureEnv): MemoryFixture {
  return {
    fixtures: {
      'memory.stats': () => ({
        ok: false, base_url: '', episodes: 0, profiles: 0, agent_cases: 0, agent_skills: 0,
        note: 'no memory engine behind this page',
      }),
      'memory.list': (p) => ({
        items: [], total: 0, page: (p as { page?: number }).page || 1,
        page_size: (p as { page_size?: number }).page_size || 20,
        note: 'no memory engine behind this page',
      }),
      'memory.delete': () => ({ ok: false, removed: 0 }),
    },
  }
}
