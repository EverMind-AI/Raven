// @vitest-environment happy-dom
/* The box DagGraph hands each node, and the edges it draws between them. */

import { act } from '@testing-library/react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import * as confirmStore from '../../state/confirm'
import * as pageStore from '../../state/page'
import { DagGraph } from './DagGraph'
import { layout } from './graph'

import type { Dims } from './graph'
import type { DagNode } from './types'
import type { Root } from 'react-dom/client'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const DIMS: Dims = { W: 184, H: 44, GAP_X: 230, GAP_Y: 60, PAD: 10 }

const node = (id: string, depends_on: string[] = [], over: Partial<DagNode> = {}): DagNode => ({
  id,
  subagent: 'raven',
  depends_on,
  status: 'pending',
  started_at: null,
  ended_at: null,
  ...over,
})

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  setTranslator((key, vars) => key + (vars ? ` ${JSON.stringify(vars)}` : ''))
  vi.spyOn(pageStore, 'show').mockImplementation(() => {})
  vi.spyOn(confirmStore, 'ask').mockImplementation(() => {})
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => { root.unmount() })
  resetTranslator()
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

const draw = (nodes: DagNode[], dims: Dims): void => {
  act(() => {
    root.render(<DagGraph dims={dims} nodes={nodes} now={1000} onPick={() => {}}
      renderNode={(n, now) => <div className="stand-in">{n.id}:{now}</div>} />)
  })
}

describe('the box DagGraph hands each node, and the edges between them', () => {
  it('draws the caller\'s card inside each node, sized to the box and given the clock', () => {
    /* Both halves of the CardRenderer contract: the node, and the clock a
       running node's own duration is measured against -- the board's live
       tick comes through this argument and nothing else. */
    draw([node('n1')], DIMS)
    const box = host.querySelector('.nd foreignObject') as SVGForeignObjectElement
    expect(box.getAttribute('width')).toBe(String(DIMS.W))
    expect(box.getAttribute('height')).toBe(String(DIMS.H))
    expect(host.querySelector('.nd .stand-in')?.textContent).toBe('n1:1000')
  })

  it('places each node by absolute coordinates on its box, not by a transform on the group', () => {
    /* WebKit draws a foreignObject without its ancestor group's transform: the
       box is hit-tested where the layout put it but painted at the SVG's
       origin, so on Safari every card of a two-step graph sat on the first
       node's spot with the arrow pointing at empty canvas. The place is
       therefore written on the box itself, where every engine honours it. */
    const nodes = [node('scan'), node('write', ['scan'])]
    draw(nodes, DIMS)
    const groups = [...host.querySelectorAll<SVGGElement>('.nd')]
    expect(groups.map((g) => g.getAttribute('transform'))).toEqual([null, null])
    const { at } = layout(nodes, DIMS)
    const places = groups.map((g) => {
      const box = g.querySelector('foreignObject')!
      return [Number(box.getAttribute('x')), Number(box.getAttribute('y'))]
    })
    expect(places).toEqual(groups.map((g) => {
      const p = at.get(g.getAttribute('data-node')!)!
      return [p.x, p.y]
    }))
    /* Two layers, so two different places. */
    expect(places[0]![0]).not.toBe(places[1]![0])
  })

  it('draws one edge and one arrowhead for a two-node chain, flowed once the upstream node is done', () => {
    const upstream = node('a', [], { status: 'pending' })
    const downstream = node('b', ['a'])
    draw([upstream, downstream], DIMS)
    expect(host.querySelectorAll('.edge')).toHaveLength(1)
    expect(host.querySelectorAll('.tip')).toHaveLength(1)
    expect(host.querySelector('.edge')!.getAttribute('class')).toBe('edge')
    expect(host.querySelector('.tip')!.getAttribute('class')).toBe('tip')

    draw([{ ...upstream, status: 'completed' }, downstream], DIMS)
    expect(host.querySelector('.edge')!.getAttribute('class')).toBe('edge flowed')
    expect(host.querySelector('.tip')!.getAttribute('class')).toBe('tip flowed')
  })
})
