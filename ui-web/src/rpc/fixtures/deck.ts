/* The deck template picker's three calls, answered offline.
 *
 * Two templates, one with a cover and one without -- a host draws covers where
 * it has LibreOffice, and a picker with names alone still picks -- and a pick
 * that answers as an upload does, since that is what the tray stages.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'

export interface DeckFixture {
  fixtures: Fixtures
}

const TEMPLATES = [
  /* A 1x1 amber JPEG: enough for the card to draw a picture rather than the name. */
  { name: 'amber_wave_quarterly_summary', label: 'Amber Wave Quarterly Summary', size: 717535,
    cover: 'data:image/gif;base64,R0lGODlhAQABAPAAAP+kAAAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==' },
  { name: 'blue_minimal_general_analysis', label: 'Blue Minimal General Analysis', size: 3751723, cover: null },
]

export function createDeck(_env: FixtureEnv): DeckFixture {
  return {
    fixtures: {
      'deck.templates.list': () => ({ templates: TEMPLATES, available: true, pending: false }),
      'deck.templates.pages': () => ({ pages: [] }),
      'deck.templates.pick': (p) => {
        const name = (p as { name?: string }).name || 'template'
        const row = TEMPLATES.find((t) => t.name === name)
        return { path: `uploads/${name}.pptx`, abs_path: `~/work/raven/uploads/${name}.pptx`, size: row ? row.size : 0 }
      },
    },
  }
}
