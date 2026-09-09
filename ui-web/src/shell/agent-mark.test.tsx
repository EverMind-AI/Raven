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

import { createRoot } from 'react-dom/client'
import { act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { AgentMark, agentMarkPath } from './agent-mark'

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

const draw = (preset?: string): void => {
  act(() => { root.render(<AgentMark preset={preset} />) })
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
})
