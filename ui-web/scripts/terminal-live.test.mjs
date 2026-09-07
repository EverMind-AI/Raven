/* Contract tests for the assembled hosted-terminal live source. */

import { readFileSync } from 'node:fs'

import { describe, expect, it, vi } from 'vitest'

const installation = readFileSync(new URL('../src/live/225-terminal.js', import.meta.url), 'utf8')

function source(answers, cwd = '/work/raven') {
  const calls = []
  const DS = {}
  const browserBinary = vi.fn()
  const rpc = {
    binary: browserBinary,
    call: (method, params) => {
      calls.push([method, params])
      const answer = answers[method]
      return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer)
    },
  }
  Function('DS', 'rpc', 'wsRoot', installation)(DS, rpc, cwd)
  return { source: DS.terminal, calls, rpc, browserBinary }
}

const terminal = {
  handle: 'term_one',
  incarnationId: 'incarnation-1',
  worktreeId: 'repo::/work/raven',
  worktreePath: '/work/raven',
  title: 'rsi-research-imp',
  owner: 'rsi-research-imp',
}

const identity = {
  agentName: 'rsi-research-imp',
  brand: 'claude',
  bindingGeneration: 2,
  binding: { handle: 'term_one' },
}

describe('hosted terminal live source', () => {
  it('discovers by cwd, then uses the returned worktree id and joins identity', async () => {
    const { source: terminalSource, calls } = source({
      'terminal.list': { terminals: [terminal, { ...terminal, handle: 'term_other', worktreePath: '/work/other' }] },
      'agents.list': { agents: [identity] },
    })

    const first = await terminalSource.list('session-1')
    expect(first.terminals).toEqual([{ ...terminal, identity: {
      agentName: 'rsi-research-imp', brand: 'claude', bindingGeneration: 2,
    } }])
    expect(calls[0]).toEqual(['terminal.list', { limit: 1000, include_visual_layouts: true }])
    expect(calls[1]).toEqual(['agents.list', {}])

    await terminalSource.list('session-1')
    expect(calls[2]).toEqual(['terminal.list', {
      worktree_id: 'repo::/work/raven', limit: 1000, include_visual_layouts: true,
    }])
  })

  it('keeps terminal discovery usable when identity lookup is unavailable', async () => {
    const { source: terminalSource } = source({
      'terminal.list': { terminals: [terminal] },
      'agents.list': new Error('method unavailable'),
    })
    expect((await terminalSource.list('session-1')).terminals).toEqual([terminal])
  })

  it('demultiplexes RVT1 output and leaves other binary frames alone', () => {
    const { source: terminalSource, rpc, browserBinary } = source({})
    const output = vi.fn()
    terminalSource.onOutput = output
    const header = new TextEncoder().encode(JSON.stringify({ handle: 'term_one', seq: 7, replay: true }))
    const payload = new TextEncoder().encode('output')
    const frame = new Uint8Array(8 + header.length + payload.length)
    frame.set(new TextEncoder().encode('RVT1'))
    new DataView(frame.buffer).setUint32(4, header.length)
    frame.set(header, 8)
    frame.set(payload, 8 + header.length)

    rpc.binary(frame.buffer)
    expect(output).toHaveBeenCalledWith({ handle: 'term_one', seq: 7, replay: true, data: payload })

    const browser = new TextEncoder().encode('RVF1').buffer
    rpc.binary(browser)
    expect(browserBinary).toHaveBeenCalledWith(browser)
  })
})
