// @vitest-environment happy-dom
/* What a roster row is allowed to draw, and off which field.
 *
 * The mark is chosen by preset, never by the row's name: a configured row's
 * name is the user's to change, and a hand-written row that merely spells
 * itself `codex` is not Codex. Both halves are pinned here because both have a
 * failure nobody would see in review -- a rename silently losing the brand, and
 * an unrelated row silently acquiring one.
 *
 * `data-tone` is the other half. It says how the file answers the theme, in
 * the three values the stylesheet keys its filters off: absent (every shape
 * carries its own fill, so touch nothing), `mono` (every shape is
 * `currentColor`, which an <img> resolves against its own document and so
 * renders black -- invert the lot), and `hybrid` (some shapes are, so a plain
 * invert would take the brand colour with it). Which file is which is not
 * visible to any DOM assertion -- tests/test_ui_agent_marks.py derives it from
 * the SVG -- so what is pinned is that the value reaches the element that the
 * stylesheet's selector reads.
 */

import { act } from '@testing-library/react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { AgentMark, agentMarkPath, isOwnAgent } from './AgentMark'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let host: HTMLDivElement
let root: ReturnType<typeof createRoot>

beforeEach(() => {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => { root.unmount() })
  document.body.innerHTML = ''
})

const draw = (preset?: string, own?: boolean, agent?: string): void => {
  act(() => { root.render(<AgentMark preset={preset} own={own} agent={agent} />) })
}

const img = (): HTMLImageElement | null => host.querySelector('img')

describe('an agent row s brand mark', () => {
  it('addresses the file under the assets path the build copies', () => {
    expect(agentMarkPath('claude_code')).toBe('assets/agents/claudecode-color.svg')
    draw('claude_code')
    expect(img()?.getAttribute('src')).toBe('assets/agents/claudecode-color.svg')
  })

  it('carries the preset on the element, so a mark is traceable to its row', () => {
    draw('qwen_code')
    expect(img()?.dataset.agent).toBe('qwen_code')
  })

  /* All three of them, the hybrid included: it is the only file that needs a
     filter other than the plain invert, and a table that spelled it `mono`
     would send qoder's brand green to magenta with nothing going red. */
  it('carries each file s tone, including the one hybrid', () => {
    draw('grok')
    expect(img()?.dataset.tone).toBe('mono')
    draw('qoder')
    expect(img()?.dataset.tone).toBe('hybrid')
    draw('claude_code')
    expect(img()?.dataset.tone).toBe(undefined)
  })

  it('falls back to the generic glyph for a row with no preset behind it', () => {
    expect(agentMarkPath(undefined)).toBe(null)
    draw(undefined)
    expect(img()).toBe(null)
    expect(host.querySelector('svg')).toBeTruthy()
  })

  /* A row the user wrote by hand, named after a preset it is not running. It
     gets the glyph, for the same reason the install hints table refuses it a
     hint: the name is not the package. */
  it('gives no mark to a preset it does not know', () => {
    expect(agentMarkPath('claude-code')).toBe(null)
    draw('some_local_agent')
    expect(img()).toBe(null)
    expect(host.querySelector('svg')).toBeTruthy()
  })

  /* The whole inherited alphabet, not one representative of it. Every object
     literal answers these from its prototype, so a bare index returns a
     truthy value with no `file` and the row renders a broken image at
     assets/agents/undefined.svg. */
  it('treats an inherited property name as unknown', () => {
    for (const key of ['constructor', 'toString', '__proto__', 'valueOf', 'hasOwnProperty']) {
      expect(agentMarkPath(key)).toBe(null)
      draw(key)
      expect(img()).toBe(null)
      expect(host.querySelector('svg')).toBeTruthy()
    }
  })

  /* Raven's own agents, which reach here with no preset because there is no
     third-party package behind them to name. The flag is the server's
     `builtin || vendored`, so this is the same kind of claim a preset is and
     not a test on the row's name. */
  describe('raven s own', () => {
    it('draws the project s own mark off the flag, with no preset in hand', () => {
      expect(agentMarkPath(undefined, true)).toBe('assets/agents/raven.svg')
      draw(undefined, true)
      expect(img()?.getAttribute('src')).toBe('assets/agents/raven.svg')
    })

    /* No `tone`, and that is load-bearing rather than an omission: raven.svg
       carries a prefers-color-scheme rule of its own, so the stylesheet's
       invert must not also reach it -- it would undo the file's answer and put
       a black raven back on the dark surface. */
    it('claims no tone, because the file answers the theme itself', () => {
      draw(undefined, true)
      expect(img()?.dataset.tone).toBe(undefined)
    })

    /* The flag is read before the table. A row carrying some preset string is
       still a Discovered agent of this install's own, and the name it happens
       to spell is not evidence about anything. */
    it('prefers the flag to whatever preset the row spells', () => {
      expect(agentMarkPath('claude_code', true)).toBe('assets/agents/raven.svg')
    })

    it('leaves an ordinary row alone when the flag is false', () => {
      expect(agentMarkPath(undefined, false)).toBe(null)
      draw(undefined, false)
      expect(img()).toBe(null)
      expect(host.querySelector('svg')).toBeTruthy()
    })

    /* Each shipped Raven agent draws its own mark, looked up by the display
       name its manifest declares. `Raven-PPT` is hidden from the roster, and
       its mark was given to `Raven-Code`, so it falls back to the project mark
       instead of keeping a file no row would draw. */
    it('draws each shipped agent its own mark off the name', () => {
      const marks: Array<[string, string]> = [
        ['Raven-Research', 'raven-research'],
        ['Raven-Code', 'raven-code'],
        ['Raven-Design', 'raven-design'],
        ['Raven-Oncall', 'raven-oncall'],
      ]
      for (const [agent, file] of marks) {
        expect(agentMarkPath(undefined, true, agent)).toBe(`assets/agents/${file}.svg`)
        draw(undefined, true, agent)
        expect(img()?.getAttribute('src')).toBe(`assets/agents/${file}.svg`)
      }
    })

    it('falls back to the project mark for an own agent the table does not name', () => {
      /* The last two are inherited property names: the lookup is an object
         literal, and a bare index would answer `constructor` with a function
         that has no `file`. */
      for (const agent of ['Raven-PPT', 'raven', 'Raven-Something', 'constructor', '__proto__']) {
        expect(agentMarkPath(undefined, true, agent)).toBe('assets/agents/raven.svg')
      }
    })

    /* A caller can name a run itself, and may number two runs of one agent:
       `raven-design-1` is such an instance name. The runtime's own minted
       handles end in six hex characters instead, so this pins the explicitly
       named shape, not the minted one. */
    it('reads an instance name down to its agent', () => {
      expect(agentMarkPath(undefined, true, 'raven-design-1')).toBe('assets/agents/raven-design.svg')
      expect(agentMarkPath(undefined, true, 'RAVEN-RESEARCH')).toBe('assets/agents/raven-research.svg')
    })

    /* The name selects among raven's own marks only. A third-party row that
       happens to spell a Raven agent's name stays on its preset's mark. */
    it('ignores the name on a row that is not one of raven s own', () => {
      expect(agentMarkPath('claude_code', false, 'Raven-Code')).toBe('assets/agents/claudecode-color.svg')
      expect(agentMarkPath('claude_code', true, 'Raven-Code')).toBe('assets/agents/raven-code.svg')
    })

    /* Both flags, spelled once here so four call sites cannot drift: a reading
       that covers only `builtin` leaves every Discovered agent wearing the
       generic glyph. */
    it('counts both flags the server sets, and nothing else', () => {
      expect(isOwnAgent({ builtin: true })).toBe(true)
      expect(isOwnAgent({ vendored: true })).toBe(true)
      expect(isOwnAgent({ builtin: false, vendored: false })).toBe(false)
      expect(isOwnAgent({})).toBe(false)
      expect(isOwnAgent(undefined)).toBe(false)
      expect(isOwnAgent(null)).toBe(false)
    })
  })

  /* One column for both shapes. The glyph slot and the mark slot are the same
     element, so a roster of presets and hand-written rows still starts its
     names at one x -- the failure the fold slot already has a guard for. */
  it('draws both shapes in the same slot', () => {
    draw('codex')
    const withMark = (host.firstElementChild as HTMLElement).className
    draw(undefined)
    const withGlyph = (host.firstElementChild as HTMLElement).className
    expect(withMark).toBe('agent-mark')
    expect(withGlyph).toBe('agent-mark')
  })

  /* An agent reached through another one: the route's mark on the plate and the
     agent's over its corner, each looked up by its own key in the one table --
     so the guard against inherited names covers the badge too, and an unknown
     one draws nothing rather than a broken image. */
  describe('the badge', () => {
    const drawVia = (badge: string): void => {
      act(() => { root.render(<AgentMark preset="mirasim" badge={badge} />) })
    }

    it('draws the agent over the route, in the one slot', () => {
      drawVia('mirasim_codex')
      const slot = host.firstElementChild as HTMLElement
      expect(slot.className).toBe('agent-mark')
      expect(slot.querySelector(':scope > img')?.getAttribute('src')).toBe('assets/agents/mirasim.svg')
      const over = slot.querySelector<HTMLImageElement>('.agent-mark-badge img')
      expect(over?.getAttribute('src')).toBe('assets/agents/codex-color.svg')
      expect(over?.dataset.agent).toBe('mirasim_codex')
    })

    it('carries the badge s own tone, which the route s mark does not share', () => {
      drawVia('mirasim_kimi')
      expect(host.querySelector<HTMLImageElement>('.agent-mark-badge img')?.dataset.tone).toBe('mono')
      expect(host.querySelector<HTMLImageElement>(':scope > .agent-mark > img')?.dataset.tone).toBe(undefined)
    })

    it('draws no badge for one it does not know', () => {
      for (const key of ['some_local_agent', 'constructor', '__proto__']) {
        drawVia(key)
        expect(host.querySelector('.agent-mark-badge')).toBe(null)
        expect(host.querySelector('img')?.getAttribute('src')).toBe('assets/agents/mirasim.svg')
      }
    })
  })
})
