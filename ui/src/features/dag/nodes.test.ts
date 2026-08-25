import { describe, expect, it } from 'vitest'

import { applyUpdate, fromArgs, fromSnapshot, fromStarted, merge } from './nodes'

/* The three sources a graph reaches the page through, and the merge that lets a
 * card learn from more than one of them.
 *
 * This is where the transcript's dag card and the sheet above the composer agree
 * on what a node is. They used to unpack their payloads separately, and every
 * field one of them forgot was a field that surface could never show whatever the
 * source had -- which is how the card came to hold four of them and no
 * dependencies. So what is worth pinning here is exactly the forgetting: that a
 * source carrying a field produces it, and that a source silent about one does
 * not erase it.
 */

describe('a dag call the model made', () => {
  it('reads the whole request out of the arguments', () => {
    const [n] = fromArgs({
      nodes: [{
        id: 'brief',
        subagent: 'writer',
        instance: 'draft',
        depends_on: ['scan'],
        prompt_template: 'summarise {{ scan.output }}',
        inputs: { voice: { file: 'docs/voice.md' }, topic: 'crows' },
      }],
    })

    expect(n).toEqual({
      id: 'brief',
      subagent: 'writer',
      instance: 'draft',
      depends_on: ['scan'],
      status: 'pending',
      started_at: null,
      ended_at: null,
      node_summary: null,
      prompt_template: 'summarise {{ scan.output }}',
      inputs: { voice: { file: 'docs/voice.md' }, topic: 'crows' },
    })
  })

  it('drops a node with no id rather than naming it something', () => {
    /* An id is how every other part of this domain refers to a node -- an event,
       a dependency, the run dir. A placeholder would be a name nothing else in
       the run answers to. */
    expect(fromArgs({ nodes: [{ subagent: 'writer' }, { id: 'ok' }] }).map((n) => n.id)).toEqual(['ok'])
  })

  it('is empty for a call that described no graph', () => {
    /* `load_playbook` in dag mode: its arguments are `{name, params, fills}` and
       the graph exists only once the engine has assembled it. */
    expect(fromArgs({ name: 'topic-briefing', params: { topic: 'crows' } })).toEqual([])
    expect(fromArgs(null)).toEqual([])
    expect(fromArgs({ nodes: 'not a list' })).toEqual([])
  })

  it('keeps only inputs whose value is a string or an object', () => {
    /* These arrive from a model, so every value is a claim. A number or a list
       would render as neither a literal nor a reference. */
    const [n] = fromArgs({ nodes: [{ id: 'a', inputs: { good: 'x', obj: { node: 'b' }, n: 7, arr: [1] } }] })
    expect(n?.inputs).toEqual({ good: 'x', obj: { node: 'b' } })
  })

  it('reports no inputs rather than an empty map', () => {
    /* So a caller can say "nothing was handed to this" without having to count
       keys of something that might not be there. */
    expect(fromArgs({ nodes: [{ id: 'a', inputs: {} }] })[0]?.inputs).toBe(null)
  })
})

describe('the run-started event', () => {
  it('carries structure and says nothing about the request', () => {
    const [n] = fromStarted({ nodes: [{ id: 'brief', subagent: 'writer', depends_on: ['scan'] }] })

    expect(n?.depends_on).toEqual(['scan'])
    /* Not `''`: the event is a run announcement, and reading its silence as an
       empty template is what let it overwrite a request the card already had. */
    expect(n?.prompt_template).toBe(null)
  })
})

describe('a run read back off disk', () => {
  it('is the only source that carries state as well as structure', () => {
    const [n] = fromSnapshot([{
      node: 'brief',
      subagent: 'writer',
      depends_on: ['scan'],
      status: 'completed',
      started_at: 1000,
      ended_at: 4000,
      prompt_template: 'summarise {{ scan.output }}',
      inputs: { voice: { file: 'docs/voice.md' } },
    }])

    expect(n?.id).toBe('brief')
    expect(n?.status).toBe('completed')
    expect([n?.started_at, n?.ended_at]).toEqual([1000, 4000])
    expect(n?.inputs).toEqual({ voice: { file: 'docs/voice.md' } })
  })

  it('names the node `node` where the other two say `id`', () => {
    /* The one shape difference between this source and the other two, and the
       reason an adapter exists rather than a shared cast. */
    expect(fromSnapshot([{ id: 'brief', status: 'completed' }])).toEqual([])
  })

  it('defaults a row with no status to pending', () => {
    expect(fromSnapshot([{ node: 'a' }])[0]?.status).toBe('pending')
  })
})

describe('merging one source onto another', () => {
  const args = fromArgs({
    nodes: [{ id: 'a', subagent: 'scout', prompt_template: 'go look', inputs: { k: 'v' } }],
  })

  it('does not let a source erase what it does not know', () => {
    /* The case this exists for: a model-made call already holds the whole request
       from its arguments, and `run_started` follows carrying structure only. */
    const merged = merge(args, fromStarted({ nodes: [{ id: 'a', subagent: 'scout', depends_on: [] }] }))

    expect(merged[0]?.prompt_template).toBe('go look')
    expect(merged[0]?.inputs).toEqual({ k: 'v' })
  })

  it('takes the newer state where the incoming source has it', () => {
    const merged = merge(args, fromSnapshot([{ node: 'a', status: 'failed', started_at: 5, ended_at: 9 }]))

    expect([merged[0]?.status, merged[0]?.started_at, merged[0]?.ended_at]).toEqual(['failed', 5, 9])
    expect(merged[0]?.prompt_template).toBe('go look')
  })

  it('appends a node the existing list never had', () => {
    /* A card whose arguments carried no graph at all: everything is new. */
    const merged = merge(args, fromSnapshot([{ node: 'b', status: 'pending' }]))
    expect(merged.map((n) => n.id)).toEqual(['a', 'b'])
  })

  it('keeps the existing order rather than the incoming one', () => {
    /* The order is the server's, and it decides which node sits above which
       inside a column. A read that came back reordered must not move the boxes. */
    const two = fromArgs({ nodes: [{ id: 'a' }, { id: 'b' }] })
    const merged = merge(two, fromSnapshot([{ node: 'b' }, { node: 'a' }]))
    expect(merged.map((n) => n.id)).toEqual(['a', 'b'])
  })

  it('is the incoming list when there is nothing to merge onto', () => {
    expect(merge([], args)).toEqual(args)
  })
})

describe('one node updated', () => {
  const two = fromArgs({ nodes: [{ id: 'a' }, { id: 'b' }] })

  it('moves that node and leaves the other alone', () => {
    const next = applyUpdate(two, { node: 'b', status: 'running', started_at: 7 })
    expect(next.map((n) => n.status)).toEqual(['pending', 'running'])
    expect(next[1]?.started_at).toBe(7)
  })

  it('ignores an update for a node it does not hold', () => {
    /* An event for another run. Inventing a box for it would draw a graph the
       server never described. */
    expect(applyUpdate(two, { node: 'c', status: 'running' })).toBe(two)
  })

  it('keeps the time it had when the update carries none', () => {
    const started = applyUpdate(two, { node: 'a', status: 'running', started_at: 7 })
    const ended = applyUpdate(started, { node: 'a', status: 'completed' })
    expect(ended[0]?.started_at).toBe(7)
  })
})
