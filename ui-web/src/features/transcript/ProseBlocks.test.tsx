// @vitest-environment happy-dom
/* A prose box drawn block by block: a block whose markup did not change keeps
 * its nodes, the box's children are the blocks' own elements, and a change
 * redraws from the first block that differs. */

import { act } from '@testing-library/react'
import { createElement } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, describe, expect, it } from 'vitest'

import { ProseBlocks } from './ProseBlocks'

import type { Root } from 'react-dom/client'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let root: Root | null = null

afterEach(() => {
  act(() => { root?.unmount() })
  root = null
  document.body.innerHTML = ''
})

function draw(blocks: string[]): HTMLElement {
  if (!root) {
    const host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
  }
  act(() => { root!.render(createElement(ProseBlocks, { blocks, className: 'prose' })) })
  return document.querySelector<HTMLElement>('.prose')!
}

describe('a prose box drawn block by block', () => {
  it('puts each block straight into the box, with nothing wrapped around it', () => {
    const box = draw(['<p>a</p>', '<ul><li>b</li></ul>'])
    expect(box.innerHTML).toBe('<p>a</p><ul><li>b</li></ul>')
    expect([...box.children].map((c) => c.tagName)).toEqual(['P', 'UL'])
  })

  it('keeps the nodes of the blocks a longer reply left alone', () => {
    const first = draw(['<p>a</p>', '<p>b</p>']).firstElementChild
    const box = draw(['<p>a</p>', '<p>bc</p>', '<ul><li>d</li></ul>'])
    expect(box.firstElementChild).toBe(first)
    expect(box.innerHTML).toBe('<p>a</p><p>bc</p><ul><li>d</li></ul>')
  })

  it('redraws from the first block that changed, and empties for nothing', () => {
    draw(['<p>a</p>', '<p>b</p>', '<p>c</p>'])
    const second = document.querySelector('.prose')!.children[1]
    let box = draw(['<p>z</p>', '<p>b</p>'])
    expect(box.innerHTML).toBe('<p>z</p><p>b</p>')
    expect(box.children[1]).not.toBe(second)
    box = draw([])
    expect(box.innerHTML).toBe('')
  })
})
