/* The assembled live layer's session-naming block: a placeholder that always
   ends, and a title that lands without a page reload. */

import { readFileSync } from 'node:fs'

import { Window } from 'happy-dom'
import { describe, expect, it, vi } from 'vitest'

/* A real DOM, built in the node environment rather than switched on for the
   whole file: reading the assembled live layer needs `import.meta.url` to stay
   a file URL, which it does not under a DOM environment. */
const document = new Window().document

const build = readFileSync(new URL('../build.py', import.meta.url), 'utf8')
const manifest = build.match(/_LIVE_PARTS = \[(.*?)\n\]/s)
if (!manifest) throw new Error('_LIVE_PARTS is absent from build.py')
const parts = [...manifest[1].matchAll(/"([^"]+\.js)"/g)].map((m) => m[1])
const live = parts
  .map((name) => readFileSync(new URL(`../src/live/${name}`, import.meta.url), 'utf8'))
  .join('')

/* From the grace constant through the end of beginNaming -- the four functions
   only make sense together, and slicing them out one at a time would only
   assert that each one exists. */
function namingBlock() {
  const start = live.indexOf('const NAMING_GRACE_MS')
  if (start < 0) throw new Error('the naming block is absent from the assembled live layer')
  const mark = live.indexOf('function beginNaming', start)
  if (mark < 0) throw new Error('beginNaming is absent from the assembled live layer')
  const brace = live.indexOf('{', mark)
  let depth = 0
  for (let index = brace; index < live.length; index += 1) {
    if (live[index] === '{') depth += 1
    else if (live[index] === '}') {
      depth -= 1
      if (depth === 0) return live.slice(start, index + 1)
    }
  }
  throw new Error('beginNaming has no closing brace in the assembled live layer')
}

function harness({ rows, current, titleCall }) {
  const heading = document.createElement('h1')
  heading.id = 'title'
  const draws = []
  const install = Function(
    'document',
    '$',
    'T',
    'sess',
    'sessionCurrent',
    'sessionDraw',
    'plainTitle',
    'rpc',
    `${namingBlock()}\nreturn { beginNaming, settleNaming };`,
  )
  const api = install(
    document,
    (sel) => (sel === '#title' ? heading : null),
    (key) => key,
    (id) => rows.find((r) => r.id === id),
    () => current,
    () => draws.push('draw'),
    (s) => String(s),
    { call: titleCall },
  )
  return { ...api, heading, draws }
}

describe('the assembled live naming block', () => {
  it('parks a placeholder on the row and the top bar while the name is coming', () => {
    const rows = [{ id: 's1', title: 'gui.new_task' }]
    const h = harness({ rows, current: 's1', titleCall: async () => ({}) })

    h.beginNaming('please cut a desktop release')

    expect(rows[0].naming).toBe(true)
    expect(h.heading.textContent).toBe('')
    expect(h.heading.classList.contains('skel')).toBe(true)
    expect(h.heading.querySelector('.sk')).not.toBeNull()
  })

  it('leaves a session that already has a name alone', () => {
    const rows = [{ id: 's1', title: 'Release checklist' }]
    const h = harness({ rows, current: 's1', titleCall: async () => ({}) })

    h.beginNaming('please cut a desktop release')

    expect(rows[0].naming).toBeUndefined()
    expect(h.heading.classList.contains('skel')).toBe(false)
  })

  it('fills the name in and clears the placeholder when the event lands', () => {
    const rows = [{ id: 's1', title: 'gui.new_task' }]
    const h = harness({ rows, current: 's1', titleCall: async () => ({}) })
    h.beginNaming('please cut a desktop release')

    h.settleNaming('s1', 'Cut a desktop release')

    expect(rows[0].naming).toBe(false)
    expect(rows[0].title).toBe('Cut a desktop release')
    expect(h.heading.classList.contains('skel')).toBe(false)
    expect(h.heading.textContent).toBe('Cut a desktop release')
  })

  it('gives up on its own and takes the name the server already has', async () => {
    /* The one failure a reader cannot leave by waiting. Without the timer the
       row shimmers until the page is reloaded. */
    vi.useFakeTimers()
    try {
      const rows = [{ id: 's1', title: 'gui.new_task' }]
      const asked = []
      const h = harness({
        rows,
        current: 's1',
        titleCall: async (method, params) => {
          asked.push([method, params])
          return { title: 'please cut a desktop rele' }
        },
      })
      h.beginNaming('please cut a desktop release')
      expect(rows[0].naming).toBe(true)

      await vi.advanceTimersByTimeAsync(12_000)
      await vi.waitFor(() => expect(rows[0].naming).toBe(false))

      expect(asked).toEqual([['session.title', { session_id: 's1' }]])
      expect(rows[0].title).toBe('please cut a desktop rele')
      expect(h.heading.classList.contains('skel')).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('falls back to the opening line when the read succeeds with no title yet', async () => {
    /* The ordinary shape of this path, and the one that was missed: the turn is
       still running at 12s, so `SessionManager.save` has not derived a name yet
       and the read answers null. Landing on the default name here would leave
       the row saying "New task" until a reload, because nothing re-reads the
       session the reader is looking at. */
    vi.useFakeTimers()
    try {
      const rows = [{ id: 's1', title: 'gui.new_task' }]
      const h = harness({ rows, current: 's1', titleCall: async () => ({ title: null }) })
      h.beginNaming('please cut a desktop release for the beta channel')

      await vi.advanceTimersByTimeAsync(12_000)
      await vi.waitFor(() => expect(rows[0].naming).toBe(false))

      expect(rows[0].title).toBe('please cut a desktop release for the beta channel')
      expect(h.heading.textContent).toBe('please cut a desktop release for the beta channel')
      expect(h.heading.classList.contains('skel')).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('prefers the stored title over the opening line when the server has one', async () => {
    vi.useFakeTimers()
    try {
      const rows = [{ id: 's1', title: 'gui.new_task' }]
      const h = harness({ rows, current: 's1', titleCall: async () => ({ title: 'Cut a release' }) })
      h.beginNaming('please cut a desktop release for the beta channel')

      await vi.advanceTimersByTimeAsync(12_000)
      await vi.waitFor(() => expect(rows[0].naming).toBe(false))

      expect(rows[0].title).toBe('Cut a release')
    } finally {
      vi.useRealTimers()
    }
  })

  it('stops shimmering even when the fallback read fails', async () => {
    vi.useFakeTimers()
    try {
      const rows = [{ id: 's1', title: 'gui.new_task' }]
      const h = harness({
        rows,
        current: 's1',
        titleCall: async () => {
          throw new Error('socket closed')
        },
      })
      h.beginNaming('please cut a desktop release')

      await vi.advanceTimersByTimeAsync(12_000)
      await vi.waitFor(() => expect(rows[0].naming).toBe(false))

      expect(rows[0].title).toBe('please cut a desktop release')
      expect(h.heading.classList.contains('skel')).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })
})
