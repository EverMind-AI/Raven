/* The offline composer history: what the page sent in this tab, in memory.
 *
 * There is no file behind the demo page, so the library keeps the same rules
 * the server does -- trim, skip blank, drop an adjacent repeat, cap at 1000 --
 * and answers the two calls from that array. A page that sends a message can
 * then recall it with the up arrow, which is the behaviour the fixture is for.
 */

import type { Fixtures } from '../fixtureTransport'

const MAX = 1000

export function createHistory(): { fixtures: Fixtures } {
  const entries: string[] = []
  return {
    fixtures: {
      'input.history.list': (params) => {
        const asked = params.limit
        const limit = typeof asked === 'number' && Number.isFinite(asked)
          ? Math.max(0, Math.min(Math.trunc(asked), MAX))
          : MAX
        return { entries: limit === 0 ? [] : entries.slice(-limit) }
      },
      'input.history.append': (params) => {
        const text = (params.text || '').trim()
        if (!text || entries.at(-1) === text) return { ok: true, appended: false }
        entries.push(text)
        if (entries.length > MAX) entries.splice(0, entries.length - MAX)
        return { ok: true, appended: true }
      },
    },
  }
}
