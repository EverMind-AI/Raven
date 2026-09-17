// @vitest-environment happy-dom
/* The runtime's session naming: a placeholder that always ends, and a title
   that lands without a page reload. */

import { describe, expect, it, vi } from 'vitest'

import { fakeGateway, loadPart, looseQuery, moduleText } from '../../../scripts/module-harness.mjs'

type Runtime = typeof import('./runtime')

interface Row { id: string; title: string; naming?: boolean }

/* The four naming verbs driven together: they only make sense that way, and
   each one on its own would only assert that it exists. */
async function harness({ rows, current, titleCall }: {
  rows: Row[]
  current: string | null
  titleCall: (method: string, params?: unknown) => Promise<unknown>
}): Promise<Runtime & { heading: HTMLElement; draws: string[] }> {
  document.body.innerHTML = '<h1 id="title"></h1>'
  const heading = document.getElementById('title') as HTMLElement
  const draws: string[] = []
  /* The module under test is what the fakes are installed around: a module
     the first import did not reach is loaded afterwards without them. */
  await loadPart(() => import('./runtime'), {
    fakes: {
      'src/features/rail/store': {
        draw: () => draws.push('draw'),
      },
      'src/state/session/rows': {
        sess: (id: string) => rows.find((r) => r.id === id),
      },
      'src/i18n/t': {
        t: (key: string) => key,
      },
      'src/lib/dom': {
        $: looseQuery(),
      },
      'src/lib/session': { current: () => current },
      'src/features/rail/title': { plainTitle: (s: unknown) => String(s) },
    },
  })
  const runtime = (await import('./runtime')) as Runtime
  await fakeGateway(titleCall)
  return { ...runtime, heading, draws }
}

describe('the live naming block', () => {
  it('parks a placeholder on the row and the top bar while the name is coming', async () => {
    const rows: Row[] = [{ id: 's1', title: 'gui.new_task' }]
    const h = await harness({ rows, current: 's1', titleCall: async () => ({}) })

    h.beginNaming('please cut a desktop release')

    expect(rows[0]!.naming).toBe(true)
    expect(h.heading.textContent).toBe('')
    expect(h.heading.classList.contains('skel')).toBe(true)
    expect(h.heading.querySelector('.sk')).not.toBeNull()
  })

  it('leaves a session that already has a name alone', async () => {
    const rows: Row[] = [{ id: 's1', title: 'Release checklist' }]
    const h = await harness({ rows, current: 's1', titleCall: async () => ({}) })

    h.beginNaming('please cut a desktop release')

    expect(rows[0]!.naming).toBeUndefined()
    expect(h.heading.classList.contains('skel')).toBe(false)
  })

  it('fills the name in and clears the placeholder when the event lands', async () => {
    const rows: Row[] = [{ id: 's1', title: 'gui.new_task' }]
    const h = await harness({ rows, current: 's1', titleCall: async () => ({}) })
    h.beginNaming('please cut a desktop release')

    h.settleNaming('s1', 'Cut a desktop release')

    expect(rows[0]!.naming).toBe(false)
    expect(rows[0]!.title).toBe('Cut a desktop release')
    expect(h.heading.classList.contains('skel')).toBe(false)
    expect(h.heading.textContent).toBe('Cut a desktop release')
  })

  it('gives up on its own and takes the name the server already has', async () => {
    /* The one failure a reader cannot leave by waiting. Without the timer the
       row shimmers until the page is reloaded. */
    const rows: Row[] = [{ id: 's1', title: 'gui.new_task' }]
    const asked: Array<[string, unknown]> = []
    const h = await harness({
      rows,
      current: 's1',
      titleCall: async (method, params) => {
        asked.push([method, params])
        return { title: 'please cut a desktop rele' }
      },
    })
    vi.useFakeTimers()
    try {
      h.beginNaming('please cut a desktop release')
      expect(rows[0]!.naming).toBe(true)

      await vi.advanceTimersByTimeAsync(12_000)
      await vi.waitFor(() => expect(rows[0]!.naming).toBe(false))

      expect(asked).toEqual([['session.title', { session_id: 's1' }]])
      expect(rows[0]!.title).toBe('please cut a desktop rele')
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
    const rows: Row[] = [{ id: 's1', title: 'gui.new_task' }]
    const h = await harness({ rows, current: 's1', titleCall: async () => ({ title: null }) })
    vi.useFakeTimers()
    try {
      h.beginNaming('please cut a desktop release for the beta channel')

      await vi.advanceTimersByTimeAsync(12_000)
      await vi.waitFor(() => expect(rows[0]!.naming).toBe(false))

      expect(rows[0]!.title).toBe('please cut a desktop release for the beta channel')
      expect(h.heading.textContent).toBe('please cut a desktop release for the beta channel')
      expect(h.heading.classList.contains('skel')).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('prefers the stored title over the opening line when the server has one', async () => {
    const rows: Row[] = [{ id: 's1', title: 'gui.new_task' }]
    const h = await harness({ rows, current: 's1', titleCall: async () => ({ title: 'Cut a release' }) })
    vi.useFakeTimers()
    try {
      h.beginNaming('please cut a desktop release for the beta channel')

      await vi.advanceTimersByTimeAsync(12_000)
      await vi.waitFor(() => expect(rows[0]!.naming).toBe(false))

      expect(rows[0]!.title).toBe('Cut a release')
    } finally {
      vi.useRealTimers()
    }
  })

  it('stops shimmering even when the fallback read fails', async () => {
    const rows: Row[] = [{ id: 's1', title: 'gui.new_task' }]
    const h = await harness({
      rows,
      current: 's1',
      titleCall: async () => {
        throw new Error('socket closed')
      },
    })
    vi.useFakeTimers()
    try {
      h.beginNaming('please cut a desktop release')

      await vi.advanceTimersByTimeAsync(12_000)
      await vi.waitFor(() => expect(rows[0]!.naming).toBe(false))

      expect(rows[0]!.title).toBe('please cut a desktop release')
      expect(h.heading.classList.contains('skel')).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('a server that declines to name', () => {
  it('settles at once instead of waiting out the grace period', async () => {
    /* The bug this exists for: below the gate the server refuses in
       microseconds and no `session.titled` ever comes, so the placeholder sat
       for the whole grace period and only then showed the opening line. That
       made "hi" the slowest thing you could send -- a long request generates
       and lands in a second or two. */
    const rows: Row[] = [{ id: 's1', title: 'gui.new_task' }]
    const h = await harness({ rows, current: 's1', titleCall: async () => ({}) })
    vi.useFakeTimers()
    try {
      h.beginNaming('你好')
      expect(rows[0]!.naming).toBe(true)

      h.namingDeclined('s1')

      expect(rows[0]!.naming).toBe(false)
      expect(rows[0]!.title).toBe('你好')
      expect(h.heading.classList.contains('skel')).toBe(false)
      /* Nothing left armed: the timer must be cleared, or it fires later and
         overwrites a title the reader is already looking at. */
      expect(vi.getTimerCount()).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not touch a session that never started waiting', async () => {
    /* `beginNaming` declines to start a wait for an already-named session, and
       a decline arriving for that session must not blank its title. */
    const rows: Row[] = [{ id: 's1', title: 'Release checklist' }]
    const h = await harness({ rows, current: 's1', titleCall: async () => ({}) })
    h.beginNaming('please cut a desktop release')

    h.namingDeclined('s1')

    expect(rows[0]!.title).toBe('Release checklist')
    expect(rows[0]!.naming).toBeUndefined()
  })
})

/* The rules below are about the source rather than about a behaviour a harness
   can drive: which call sites carry a branch, and which branch the dispatcher
   reaches. Read from the modules that hold them: the runtime for the sends and
   the timer, the stage table for the two events. */
const read = (rel: string): string => moduleText(rel) as string
const turnText = read('state/session/runtime.ts')
const stageText = read('state/session/stages.ts')
/* Every `turn.send` the page makes: the conversation's two, and the sub-agent
   instance lane's one, which names no session and is expected to carry no
   naming branch. */
const liveText = turnText + read('features/subagents/source.ts')

describe('the wiring between turn.send and the placeholder', () => {
  /* The functions above can all be right while nothing calls them -- this
     feature has already shipped once with a correct namer that no code path
     reached. These read the parts, so they lock the call sites rather than the
     functions; they cannot prove the branch behaves, which is what the block
     above is for. */
  /* A fixed window after each call, not a lazy match: `[\s\S]{0,400}?\n` stops
     at the first newline, which is the call line itself and never the branch
     under it. */
  const sends = [...liveText.matchAll(/\.call\('turn\.send'/g)].map((m) => liveText.slice(m.index, m.index + 320))

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

describe('the naming_ended event', () => {
  /* Same source-contract caveat as the block above: this locks the dispatcher
     branch, and the behaviour it calls into is covered by the namingDeclined
     tests. */
  it('is dispatched, and hands the reason to the one place that decides', () => {
    /* Locks that the dispatcher reaches the handler and carries the reason with
       it -- a branch that dropped the reason would settle every ending onto the
       opening line, which is the bug this pair of tests exists for. What the
       handler then does is covered behaviourally below, not here. */
    const at = stageText.indexOf("'session.naming_ended'")
    expect(at).toBeGreaterThan(-1)
    /* Wide enough to clear the branch's comment: a window that stops inside it
       fails on prose length rather than on the code. */
    const branch = stageText.slice(at, at + 900)
    expect(branch).toContain('namingEnded(')
    expect(branch).toMatch(/namingEnded\(\s*p\.session_id\s*,\s*p\.reason\s*\)/)
  })

  it('sits alongside session.titled rather than replacing it', () => {
    /* Exactly one of the two follows a namer that started; dropping the titled
       branch while adding this one would settle every session onto its opening
       line and never show a generated name at all. */
    expect(stageText).toContain("'session.titled'")
    const titled = stageText.indexOf("'session.titled'")
    expect(stageText.slice(titled, titled + 260)).toContain('settleNaming')
  })

  it('still keeps the timer as a backstop', () => {
    /* An old server sends neither event, and a dropped connection sends
       nothing at all. Removing the timer once the event exists would put those
       readers back on a placeholder that never ends. */
    expect(turnText).toContain('NAMING_GRACE_MS')
    expect(turnText).toMatch(/setTimeout\(\s*\(\)\s*=>\s*\{\s*namingGaveUp\(id\);?\s*\}/)
  })
})

describe('what a naming_ended reason means for the row', () => {
  /* Behaviour, not source text. The three source-contract cases above passed
     with a real bug present -- `renamed` went through namingDeclined and
     replaced the name the person had just typed with the message they typed it
     over -- so the reason handling is exercised here instead of matched. */

  it('keeps a name typed mid-wait, and does not fall back to the opening line', async () => {
    const rows: Row[] = [{ id: 's1', title: 'gui.new_task' }]
    const h = await harness({ rows, current: 's1', titleCall: async () => ({ title: 'Release checklist' }) })
    vi.useFakeTimers()
    try {
      h.beginNaming('please cut a desktop release with the new signing key')

      // the person renames while the model is still writing; the server took it
      rows[0]!.title = 'Release checklist'

      await h.namingEnded('s1', 'renamed')

      expect(rows[0]!.title).toBe('Release checklist')
      expect(rows[0]!.naming).toBe(false)
      expect(h.heading.textContent).toBe('Release checklist')
      expect(vi.getTimerCount()).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('takes the stored name when the rename happened in another client', async () => {
    /* This row never saw the rename, so clearing the placeholder alone would
       leave it on the default name until something else refreshed the list. */
    const rows: Row[] = [{ id: 's1', title: 'gui.new_task' }]
    const h = await harness({ rows, current: 's1', titleCall: async () => ({ title: 'Named elsewhere' }) })
    h.beginNaming('please cut a desktop release')

    await h.namingEnded('s1', 'renamed')

    expect(rows[0]!.title).toBe('Named elsewhere')
  })

  it('keeps what the row has when the stored read fails, never the opening line', async () => {
    const rows: Row[] = [{ id: 's1', title: 'Release checklist' }]
    const h = await harness({
      rows,
      current: 's1',
      titleCall: async () => {
        throw new Error('not connected')
      },
    })
    // A wait is open on this row even though it is named: the rename landed
    // after beginNaming, which is the race the server reports.
    rows[0]!.title = 'gui.new_task'
    h.beginNaming('please cut a desktop release')
    rows[0]!.title = 'Release checklist'

    await h.namingEnded('s1', 'renamed')

    expect(rows[0]!.title).toBe('Release checklist')
    expect(rows[0]!.naming).toBe(false)
  })

  it('settles the other three reasons onto the opening line', async () => {
    for (const reason of ['timeout', 'no_title', 'error']) {
      const rows: Row[] = [{ id: 's1', title: 'gui.new_task' }]
      const h = await harness({ rows, current: 's1', titleCall: async () => ({}) })
      h.beginNaming('please cut a desktop release')

      await h.namingEnded('s1', reason)

      expect(rows[0]!.title, reason).toBe('please cut a desktop release')
      expect(rows[0]!.naming, reason).toBe(false)
    }
  })

  it('does nothing for a session that never started waiting', async () => {
    const rows: Row[] = [{ id: 's1', title: 'Release checklist' }]
    const h = await harness({ rows, current: 's1', titleCall: async () => ({ title: 'from the server' }) })

    await h.namingEnded('s1', 'renamed')
    await h.namingEnded('s1', 'timeout')

    expect(rows[0]!.title).toBe('Release checklist')
    expect(rows[0]!.naming).toBeUndefined()
  })
})
