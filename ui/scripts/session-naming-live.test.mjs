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
    `${namingBlock()}\nreturn { beginNaming, settleNaming, namingDeclined };`,
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

describe('a server that declines to name', () => {
  it('settles at once instead of waiting out the grace period', () => {
    /* The bug this exists for: below the gate the server refuses in
       microseconds and no `session.titled` ever comes, so the placeholder sat
       for the whole grace period and only then showed the opening line. That
       made "hi" the slowest thing you could send -- a long request generates
       and lands in a second or two. */
    vi.useFakeTimers()
    try {
      const rows = [{ id: 's1', title: 'gui.new_task' }]
      const h = harness({ rows, current: 's1', titleCall: async () => ({}) })
      h.beginNaming('你好')
      expect(rows[0].naming).toBe(true)

      h.namingDeclined('s1')

      expect(rows[0].naming).toBe(false)
      expect(rows[0].title).toBe('你好')
      expect(h.heading.classList.contains('skel')).toBe(false)
      /* Nothing left armed: the timer must be cleared, or it fires later and
         overwrites a title the reader is already looking at. */
      expect(vi.getTimerCount()).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not touch a session that never started waiting', () => {
    /* `beginNaming` declines to start a wait for an already-named session, and
       a decline arriving for that session must not blank its title. */
    const rows = [{ id: 's1', title: 'Release checklist' }]
    const h = harness({ rows, current: 's1', titleCall: async () => ({}) })
    h.beginNaming('please cut a desktop release')

    h.namingDeclined('s1')

    expect(rows[0].title).toBe('Release checklist')
    expect(rows[0].naming).toBeUndefined()
  })
})

describe('the wiring between turn.send and the placeholder', () => {
  /* The functions above can all be right while nothing calls them -- this
     feature has already shipped once with a correct namer that no code path
     reached. These read the assembled layer, so they lock the call sites rather
     than the functions; they cannot prove the branch behaves, which is what the
     block above is for. */
  /* A fixed window after each call, not a lazy match: `[\s\S]{0,400}?\n` stops
     at the first newline, which is the call line itself and never the branch
     under it. */
  const sends = [...live.matchAll(/rpc\.call\('turn\.send'/g)].map((m) => live.slice(m.index, m.index + 320))

  it('has a send site for the composer at all', () => {
    expect(sends.length).toBeGreaterThan(0)
  })

  it('settles the placeholder on every composer send that can be declined', () => {
    /* `target`-carrying sends are an instance's own lane and never name the
       session, so they are not expected to carry the branch. */
    const composer = sends.filter((s) => !s.includes('target:'))
    expect(composer.length).toBe(2)
    for (const site of composer) expect(site).toContain('namingDeclined')
  })

  it('reads the flag strictly, so an older server is not mistaken for a decline', () => {
    /* A gateway too old to carry the field omits it. Falsy would then read as
       "no title is coming" and tear the placeholder down while one is on the
       way. */
    const composer = sends.filter((s) => !s.includes('target:'))
    for (const site of composer) {
      expect(site).toMatch(/naming === false/)
      expect(site).not.toMatch(/!\w*\.naming|naming\s*==\s*false/)
    }
  })
})
