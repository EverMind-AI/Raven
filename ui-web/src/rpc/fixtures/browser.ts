/* The embedded browser, which this page does not have.
 *
 * Every call answers the shape of "no browser is running", which is the state
 * the island's own empty frame is for -- `available: false` is what says the
 * page has no Chromium behind it rather than one that failed. The fetched-links
 * list the offline canvas used to show instead is the workspace record's, not
 * this surface's: the island reads it straight from there.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ResultOf } from '../generated'

const idle: ResultOf<'browser.state'> = {
  ok: false, started: false, available: false,
  reason: 'no browser behind this page', tab_count: 0,
}

export interface BrowserFixture {
  fixtures: Fixtures
}

export function createBrowser(_env: FixtureEnv): BrowserFixture {
  return {
    fixtures: {
      'browser.state': () => ({ ...idle }),
      'browser.tabs': () => ({ ...idle, tabs: [] }),
      'browser.open': () => ({ ...idle }),
      'browser.close': () => ({ ...idle }),
      'browser.mode': () => ({ ...idle }),
      'browser.input': () => ({ ...idle }),
      'browser.watch': () => ({ ...idle }),
      'browser.frame': () => ({ ...idle }),
      'browser.read': () => ({ ...idle, text: '', refs: [] }),
    },
  }
}
