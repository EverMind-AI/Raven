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

beforeEach(() => {
  window.RavenShell = {
    T: (key, vars) => key + (vars ? ` ${JSON.stringify(vars)}` : ''),
    confirmAsk: () => {},
    showPage: () => {},
  } satisfies Shell
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => { root.unmount() })
  delete window.RavenShell
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

const draw = (nodes: DagNode[], surface: 'card' | 'sheet'): void => {
  act(() => {
    root.render(<DagGraph dims={surface === 'card' ? CARD : SHEET} nodes={nodes}
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

  it('titles a node by what it is for, keeping the id in reach', () => {
    /* The id is a key, not a name: a playbook namespaces every node with its own
       name and run tag, so a graph's ids share their first twenty characters and
       differ in the tail the box has least room for. The summary is what the
       model was required to write about the step. The id stays in the tooltip
       and in the node panel's fields, where a dependency and a run dir are
       keyed by it. */
    const nodes = [{ ...node('daily-digest-36e275-scan'), node_summary: 'read pages' }]
    draw(nodes, 'sheet')

    expect(host.querySelector('.id')!.textContent).toBe('read pages')
    expect(host.querySelector('.nd title')!.textContent)
      .toBe('read pages \u00b7 daily-digest-36e275-scan \u00b7 raven')
    /* Set in the reading face rather than the key face: a sentence in mono reads
       as an identifier. */
    expect(host.querySelector('.id')!.getAttribute('class')).toBe('id prose')
  })

  it('falls back to the id for a node whose graph carried no summary', () => {
    /* A run started before the field existed, and a model that skipped it. */
    draw([node('scan-news')], 'sheet')

    expect(host.querySelector('.id')!.textContent).toBe('scan-news')
    expect(host.querySelector('.id')!.getAttribute('class')).toBe('id')
  })

  it('gives the label the width of its own box to end in, on both surfaces', () => {
    /* The node's width IS the truncation rule. It used to be a measured pass
       whose answer was written down, so a graph drawn where nothing has a
       width -- inside a folded turn, or before its font arrived -- kept an
       answer taken in the dark and its labels ran past the box for the rest of
       the session. Nothing is measured now: the text is laid out inside the
       box and cut by it. */
    const long = { ...node('scan'), node_summary: 'read every page and say which two matter' }
    draw([long], 'card')
    const box = host.querySelector('.nd foreignObject') as SVGForeignObjectElement
    expect(box.getAttribute('width')).toBe(String(CARD.W - 29 - 9))
    expect(box.getAttribute('height')).toBe(String(CARD.H))
    /* Whole, because it is the box that ends it. */
    expect(host.querySelector('.id')!.textContent).toBe('read every page and say which two matter')

    draw([long], 'sheet')
    const wide = host.querySelector('.nd foreignObject') as SVGForeignObjectElement
    expect(wide.getAttribute('width')).toBe(String(SHEET.W - 31 - 11))
    expect(host.querySelector('.id')!.textContent).toBe('read every page and say which two matter')
  })

  it('strips the namespace a graph of long ids all share, on both surfaces', () => {
    /* The one cut that is not the box's to make: a playbook gives every node
       the same twenty-character head, and the box cuts from the tail -- the
       only part that tells them apart. */
    const nodes = [node('project-tag-fetch-metadata-and-normalise'), node('project-tag-parse')]
    draw(nodes, 'card')
    expect([...host.querySelectorAll('.id')].map((el) => el.textContent))
      .toEqual(['fetch-metadata-and-normalise', 'parse'])

    draw(nodes, 'sheet')
    expect([...host.querySelectorAll('.id')].map((el) => el.textContent))
      .toEqual(['fetch-metadata-and-normalise', 'parse'])
  })

  it('leaves a graph of short ids their namespace', () => {
    const nodes = [node('run-a12-ok'), node('run-a12-no')]
    draw(nodes, 'card')
    expect([...host.querySelectorAll('.id')].map((el) => el.textContent))
      .toEqual(['run-a12-ok', 'run-a12-no'])
  })
})
