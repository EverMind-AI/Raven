/* The deck template picker's three calls, answered offline.
 *
 * Forty-one templates, all but one with a cover -- a host draws covers where
 * it has LibreOffice, and a picker with names alone still picks -- and a pick
 * that answers as an upload does, since that is what the tray stages.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'

export interface DeckFixture {
  fixtures: Fixtures
}

/* A cover as the server would send one, drawn here rather than shipped: a
   16:9 card in the template's own colours with its name on it, as a data URL
   an <img> takes. */
function cover(label: string, hue: number, dark: boolean): string {
  const bg = dark ? `hsl(${hue} 30% 22%)` : `hsl(${hue} 55% 92%)`
  const fg = dark ? `hsl(${hue} 40% 92%)` : `hsl(${hue} 45% 28%)`
  const bar = dark ? `hsl(${hue} 60% 55%)` : `hsl(${hue} 60% 45%)`
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 180">`
    + `<rect width="320" height="180" fill="${bg}"/>`
    + `<rect x="24" y="118" width="88" height="6" rx="3" fill="${bar}"/>`
    + `<text x="24" y="98" font-family="Helvetica, Arial, sans-serif" font-size="20" font-weight="600" fill="${fg}">${label}</text>`
    + `<text x="24" y="150" font-family="Helvetica, Arial, sans-serif" font-size="11" fill="${fg}" opacity=".7">Quarterly review · 2026</text>`
    + `</svg>`
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`
}

const PALETTE = ['Amber', 'Slate', 'Moss', 'Coral', 'Indigo', 'Sand', 'Teal', 'Plum', 'Ivory', 'Ink', 'Ochre', 'Sage']
const SHAPES = ['Wave', 'Grid', 'Minimal', 'Bold', 'Editorial', 'Pitch']
const KINDS = ['Quarterly Summary', 'General Analysis', 'Product Launch', 'Team Update', 'Research Brief', 'Board Deck']

/* Enough templates to fill the picker past one screen -- a host with the
   template pack installed lists dozens -- with one that has no cover, since a
   picker with names alone still picks. */
const TEMPLATES = Array.from({ length: 41 }, (_, i) => {
  const colour = PALETTE[i % PALETTE.length]!
  const shape = SHAPES[Math.floor(i / PALETTE.length) % SHAPES.length]!
  const kind = KINDS[i % KINDS.length]!
  const label = `${colour} ${shape} ${kind}`
  const name = label.toLowerCase().replace(/ /g, '_')
  return {
    name,
    label,
    size: 700000 + (i * 97531) % 3200000,
    cover: i === 1 ? null : cover(`${colour} ${shape}`, (i * 137) % 360, i % 3 === 2),
  }
})

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
