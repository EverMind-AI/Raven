// @vitest-environment happy-dom
/* The live layer's session actions: what /clear and /compress are allowed to
 * write once their round trip answers.
 *
 * Both are fire-and-answer over a conversation the reader can leave, and both
 * used to ask for the session pointer again in the reply -- which by then can
 * name a different conversation than the one the action was for. Driven through
 * the real handlers, because the defect is which conversation the reply lands
 * on and nothing about the text of either function says that.
 */

import { describe, expect, it } from 'vitest'

import { fakeRpc, loadPart, looseQuery } from './legacy-part.mjs'

async function harness({ rows }) {
  const calls = []
  document.body.innerHTML = '<div id="stage"><p>a conversation</p></div>'
  const stage = document.getElementById('stage')
  let current = 'a'
  let settle = null
  const slash = [{ id: 'gui.clear' }, { id: 'gui.compress' }]
  const part = await loadPart(() => import('../src/legacy/live/190-session-actions.js'), {
    fakes: {
      'demo/010-kernel.js': {
        $: looseQuery(),
        /* Enough of the real thing to see WHICH conversation a message names. */
        T: (key, vars) => (vars ? `${key}:${JSON.stringify(vars)}` : key),
      },
      'demo/040-state.js': {
        /* The dialog is not what is under test: say yes at once. */
        confirmAsk: (_t, _b, _l, fn) => fn(),
        down: () => calls.push(['down']),
        sess: (id) => rows.find((r) => r.id === id),
      },
      'demo/050-rail.js': {
        sessionDraw: () => calls.push(['sessionDraw']),
        sessionOpen: () => {},
        sessionRows: () => rows,
      },
      'demo/060-conversation.js': {
        noteRow: (label) => {
          calls.push(['noteRow', label])
          return { set: () => {}, remove: () => calls.push(['lineRemove']) }
        },
        noteSay: (_line, text) => calls.push(['noteSay', text]),
        pitch: () => calls.push(['pitch']),
      },
      'demo/090-composer.js': { drawMeter: () => calls.push(['drawMeter']) },
      'live/050-turn.js': { fmtTok: (n) => String(n) },
      'live/080-overrides.js': { draft: false },
      'live/210-update-notice.js': { showUpNote: () => {} },
    },
    globals: {
      sessionCurrent: () => current,
      plainTitle: (t) => String(t),
      toast: (text) => calls.push(['toast', text]),
    },
  })
  await fakeRpc((method, params) => {
    calls.push(['rpc', method, params && params.session_id])
    return new Promise((res, rej) => { settle = { res, rej } })
  })
  const { DS } = await import('../src/legacy/seam/000-datasource.js')
  DS.composer = { slash }
  DS.transcript = {}
  part.install()
  const tick = () => new Promise((r) => setTimeout(r, 0))
  return {
    clear: () => slash.find((x) => x.id === 'gui.clear').fn(),
    compress: () => part.compressNow(),
    leaveFor: (id) => { current = id },
    ok: (payload) => { settle.res(payload || {}); return tick() },
    fail: (e) => { settle.rej(e || Object.assign(new Error('nope'), { data: { detail: 'nope' } })); return tick() },
    calls,
    stageHtml: () => stage.innerHTML,
    did: (name) => calls.some((c) => c[0] === name),
  }
}

const ROWS = () => [{ id: 'a', last: 'A last line' }, { id: 'b', last: 'B last line' }]

describe('the live session actions', () => {
  it('empties the stage of the conversation it cleared', async () => {
    const rows = ROWS()
    const h = await harness({ rows })

    h.clear()
    await h.ok()

    expect(h.stageHtml()).toBe('')
    expect(h.did('pitch')).toBe(true)
    expect(h.did('drawMeter')).toBe(true)
    expect(rows[0].last).toBe('gui.sess.cleared')
  })

  it('leaves another conversation\'s stage alone when the reader has moved on', async () => {
    const rows = ROWS()
    const h = await harness({ rows })

    h.clear()
    h.leaveFor('b')
    await h.ok()

    /* B is on screen and was never cleared. */
    expect(h.stageHtml()).toBe('<p>a conversation</p>')
    expect(h.did('pitch')).toBe(false)
    expect(h.did('drawMeter')).toBe(false)
    /* The row that says "cleared" is A's, because A is what was cleared. */
    expect(rows[0].last).toBe('gui.sess.cleared')
    expect(rows[1].last).toBe('B last line')
    expect(h.did('sessionDraw')).toBe(true)
  })

  it('reports a clear that failed for the conversation being read', async () => {
    const h = await harness({ rows: ROWS() })

    h.clear()
    await h.fail()

    expect(h.calls).toContainEqual(['noteRow', 'gui.clear_title'])
  })

  it('tells the reader a clear failed, under the name of the session it was for', async () => {
    const rows = ROWS()
    rows[0].title = 'Alpha'
    const h = await harness({ rows })

    h.clear()
    h.leaveFor('b')
    await h.fail()

    /* Not on B's transcript, which did not refuse anything -- and not nowhere
       either: dropped, the reader walks away believing A was wiped. */
    expect(h.calls.filter((c) => c[0] === 'noteRow')).toEqual([])
    const said = h.calls.filter((c) => c[0] === 'toast')
    expect(said).toHaveLength(1)
    expect(said[0][1]).toContain('gui.sess.clear_failed')
    expect(said[0][1]).toContain('Alpha')
    expect(said[0][1]).toContain('nope')
  })

  it('reports a compaction that failed for the conversation being read', async () => {
    const h = await harness({ rows: ROWS() })

    const done = h.compress()
    await h.fail()
    await done

    expect(h.calls).toContainEqual(['lineRemove'])
    expect(h.calls.some((c) => c[0] === 'noteRow' && c[1].startsWith('gui.compress.fail'))).toBe(true)
    expect(h.did('down')).toBe(true)
  })

  it('tells the reader a compaction failed, under the name of the session it was for', async () => {
    const rows = ROWS()
    rows[0].title = 'Alpha'
    const h = await harness({ rows })

    const done = h.compress()
    h.leaveFor('b')
    await h.fail()
    await done

    /* `line` is a segment in the lane this started in, which the switch has
       already dropped -- but a bare noteRow asks for the CURRENT lane, so the
       error used to land over the conversation being read. */
    expect(h.calls.some((c) => c[0] === 'noteRow' && String(c[1]).startsWith('gui.compress.fail'))).toBe(false)
    const said = h.calls.filter((c) => c[0] === 'toast')
    expect(said).toHaveLength(1)
    expect(said[0][1]).toContain('gui.sess.compress_failed')
    expect(said[0][1]).toContain('Alpha')
    expect(h.did('down')).toBe(false)
  })

  it('does not scroll another conversation after a compaction the reader left', async () => {
    const h = await harness({ rows: ROWS() })

    const done = h.compress()
    h.leaveFor('b')
    await h.ok({ removed: 3, before_tokens: 100, after_tokens: 40 })
    await done

    /* The success path reaches further than the failure path: the failure guard
       returns before this, so only a compaction that SUCCEEDS after a switch
       can scroll the wrong transcript to its end. */
    expect(h.did('down')).toBe(false)
  })

  it('still writes a finished compaction onto its own conversation', async () => {
    const h = await harness({ rows: ROWS() })

    const done = h.compress()
    await h.ok({ removed: 3, before_tokens: 100, after_tokens: 40 })
    await done

    const said = h.calls.find((c) => c[0] === 'noteSay')
    expect(said[1]).toContain('gui.compress.done')
    expect(h.calls.filter((c) => c[0] === 'toast')).toEqual([])
    expect(h.did('down')).toBe(true)
  })
})
