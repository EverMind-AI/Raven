/* The CSS contract that makes the compact DAG card content-sized and scroll-free. */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('../src/styles/page.css', import.meta.url), 'utf8')

const body = css.match(/\.dtl\.dlg\.dagc \.bd \{([^}]*)\}/)?.[1] || ''
const canvas = css.match(/\.dagc \.canvas \{([^}]*)\}/)?.[1] || ''
const svg = css.match(/\.dagc \.canvas > svg \{([^}]*)\}/)?.[1] || ''

describe('the DAG card CSS contract', () => {
  it('leaves neither the detail body nor the canvas as a scroll container', () => {
    expect(body).toContain('max-height: none')
    expect(body).toContain('overflow: visible')
    expect(canvas).toContain('overflow: visible')
    expect(canvas).not.toContain('overflow-x: auto')
    expect(canvas).not.toContain('overflow-y: auto')
  })

  it('fits the SVG to the fixed card width and lets height follow its viewBox', () => {
    expect(svg).toContain('width: 100%')
    expect(svg).toContain('height: auto')
  })
})
