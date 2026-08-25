// @vitest-environment happy-dom
/* The shared DAG renderer's surface policy and label measurement. */

import { act } from '@testing-library/react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DagGraph, visibleLayers } from './DagGraph'
import { CARD, SHEET } from './graph'

import type { Shell } from '../../shell/bridge'
import type { DagNode } from './types'
import type { Root } from 'react-dom/client'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const node = (id: string, depends_on: string[] = []): DagNode => ({
  id,
  subagent: 'raven',
  depends_on,
  status: 'pending',
  started_at: null,
  ended_at: null,
})

const chain = (count: number): DagNode[] => Array.from({ length: count }, (_, i) =>
  node(`n${i + 1}`, i ? [`n${i}`] : []),
)

let host: HTMLDivElement
let root: Root
let textPrototype: object
let textMeasure: PropertyDescriptor | undefined

beforeEach(() => {
  window.RavenShell = {
    T: (key, vars) => key + (vars ? ` ${JSON.stringify(vars)}` : ''),
    confirmAsk: () => {},
    showPage: () => {},
  } satisfies Shell
  const probe = document.createElementNS('http://www.w3.org/2000/svg', 'text')
  textPrototype = Object.getPrototypeOf(probe) as object
  textMeasure = Object.getOwnPropertyDescriptor(textPrototype, 'getComputedTextLength')
  Object.defineProperty(textPrototype, 'getComputedTextLength', {
    configurable: true,
    value(this: Element): number { return (this.textContent || '').length * 10 },
  })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => { root.unmount() })
  delete window.RavenShell
  if (textMeasure) Object.defineProperty(textPrototype, 'getComputedTextLength', textMeasure)
  else delete (textPrototype as { getComputedTextLength?: () => number }).getComputedTextLength
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

const draw = (nodes: DagNode[], surface: 'card' | 'sheet', active = true): void => {
  act(() => {
    root.render(<DagGraph active={active} dims={surface === 'card' ? CARD : SHEET} nodes={nodes}
      now={1000} onPick={() => {}} surface={surface} />)
  })
}

describe('the shared DAG renderer', () => {
  it('caps only the compact card after five layers', () => {
    const nodes = chain(7)
    expect(visibleLayers(nodes)).toEqual({ hiddenLayers: 2, nodes: nodes.slice(0, 5) })

    draw(nodes, 'card')
    expect(host.querySelectorAll('.nd')).toHaveLength(5)
    expect(host.querySelector('.daggraph')!.getAttribute('data-hidden-layers')).toBe('2')
    expect(host.querySelector('.dagcap')!.textContent).toBe('gui.dag.more_layers {"n":2}')

    draw(nodes, 'sheet')
    expect(host.querySelectorAll('.nd')).toHaveLength(7)
    expect(host.querySelector('.dagcap')).toBeNull()
  })

  it('fits the card and sheet labels through the same measured pass', () => {
    const nodes = [node('project-tag-fetch-metadata'), node('project-tag-parse')]
    draw(nodes, 'card')
    const card = [...host.querySelectorAll('.id')].map((el) => el.textContent || '')
    expect(card[0]).not.toContain('project-tag-')
    expect(card[0]!.endsWith('…')).toBe(true)
    expect(card[1]).toBe('parse')

    draw(nodes, 'sheet')
    const sheet = [...host.querySelectorAll('.id')].map((el) => el.textContent || '')
    expect(sheet[0]).not.toContain('project-tag-')
    expect(sheet[0]!.endsWith('…')).toBe(true)
    expect(sheet[1]).toBe('parse')
  })

  it('waits to measure a hidden card until its detail is open', () => {
    const nodes = [node('project-tag-fetch-metadata'), node('project-tag-parse')]
    draw(nodes, 'card', false)
    expect(host.querySelector('.id')!.textContent).toBe('project-tag-fetch-metadata')
    draw(nodes, 'card', true)
    expect(host.querySelector('.id')!.textContent).not.toContain('project-tag-')
  })
})
