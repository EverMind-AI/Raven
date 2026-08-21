import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  appendDirectDelta,
  appendDirectMessage,
  bindScrollReader,
  clearRunning,
  directKey,
  enterDirect,
  getDirectChat,
  getDirectTranscript,
  isDirectTarget,
  isViewWorking,
  leaveDirect,
  MAIN_VIEW_KEY,
  markRunning,
  patchDirectChat,
  recallScroll,
  rememberScroll,
  resetDirectChat,
  sendingPausedReason,
  setDirectTranscript,
  visibleRows
} from '../app/directChatStore.js'
import { getOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { coreCommands } from '../app/slash/commands/core.js'
import { getUiState, resetUiState } from '../app/uiStore.js'

beforeEach(() => {
  resetDirectChat()
  resetUiState()
  resetOverlayState()
})

describe('directChatStore', () => {
  it('starts on the main agent', () => {
    expect(getDirectChat().active).toBeNull()
  })

  it('enters and leaves a direct target', () => {
    enterDirect('Raven-Code', 'refactor-auth')
    expect(getDirectChat().active).toEqual({ agent: 'Raven-Code', handle: 'refactor-auth' })
    leaveDirect()
    expect(getDirectChat().active).toBeNull()
  })

  it('keys a transcript by agent and handle', () => {
    // Length-prefixed: see the collision test below for why a bare join fails.
    expect(directKey('Raven-Code', 'refactor-auth')).toBe('10:Raven-Code/refactor-auth')
  })

  it('does not collide two handles that concatenate alike', () => {
    expect(directKey('a/b', 'c')).not.toBe(directKey('a', 'b/c'))
  })

  it('keeps each instance transcript separate', () => {
    const a = directKey('A', 'one')
    const b = directKey('B', 'two')
    appendDirectMessage(a, { role: 'user', text: 'to a' })
    appendDirectMessage(b, { role: 'user', text: 'to b' })
    expect(getDirectTranscript(a)).toHaveLength(1)
    expect(getDirectTranscript(b)).toHaveLength(1)
    expect(getDirectTranscript(a)[0]?.text).toBe('to a')
  })

  it('remembers a scroll offset per instance', () => {
    rememberScroll(directKey('A', 'one'), 42)
    rememberScroll(directKey('B', 'two'), 7)
    expect(recallScroll(directKey('A', 'one'))).toBe(42)
    expect(recallScroll(directKey('B', 'two'))).toBe(7)
  })

  it('recalls zero for an instance never scrolled', () => {
    expect(recallScroll(directKey('never', 'seen'))).toBe(0)
  })

  it('replaces a transcript wholesale on a history load', () => {
    const k = directKey('A', 'one')
    appendDirectMessage(k, { role: 'user', text: 'stale' })
    setDirectTranscript(k, [{ role: 'user', text: 'fresh' }])
    expect(getDirectTranscript(k)).toHaveLength(1)
    expect(getDirectTranscript(k)[0]?.text).toBe('fresh')
  })

  it('merges a run of deltas into one assistant message', () => {
    const k = directKey('A', 'one')
    appendDirectDelta(k, 'assistant', 'he')
    appendDirectDelta(k, 'assistant', 'llo')
    expect(getDirectTranscript(k)).toHaveLength(1)
    expect(getDirectTranscript(k)[0]?.text).toBe('hello')
  })

  it('starts a new message when the role changes', () => {
    const k = directKey('A', 'one')
    appendDirectDelta(k, 'user', 'ask')
    appendDirectDelta(k, 'assistant', 'answer')
    expect(getDirectTranscript(k).map(m => m.role)).toEqual(['user', 'assistant'])
  })

  it('compares two targets without matching null against null', () => {
    // Two main-agent turns are not "the same instance"; a null-tolerant compare
    // would route every untagged event into whichever instance was last active.
    expect(isDirectTarget(null, null)).toBe(false)
    expect(isDirectTarget({ agent: 'A', handle: 'h' }, { agent: 'A', handle: 'h' })).toBe(true)
    expect(isDirectTarget({ agent: 'A', handle: 'h' }, { agent: 'A', handle: 'other' })).toBe(false)
  })
})

describe('sendingPausedReason', () => {
  // The rule these assertions used to encode -- "a turn anywhere in the session
  // pauses every other conversation in it" -- was the one-turn-per-session slot,
  // and it is gone: each instance runs on its own lane server-side.
  it('is null when nothing is in flight', () => {
    expect(sendingPausedReason(getDirectChat())).toBeNull()
  })

  it('lets you talk to one instance while another is replying', () => {
    enterDirect('A', 'one')
    markRunning({ agent: 'B', handle: 'two' })
    expect(sendingPausedReason(getDirectChat())).toBeNull()
  })

  it('lets you talk to the main agent while an instance is replying', () => {
    markRunning({ agent: 'B', handle: 'two' })
    expect(sendingPausedReason(getDirectChat())).toBeNull()
  })

  it('refuses a second prompt to the instance that is mid-reply', () => {
    // It would serialise on that instance's handle anyway, behind a wait with
    // no bound; refusing says so instead.
    enterDirect('A', 'one')
    markRunning({ agent: 'A', handle: 'one' })
    expect(sendingPausedReason(getDirectChat())).toBe('A/one is still replying; you can continue once it lands')
  })

  it('leaves the main view to the busy-input modes', () => {
    // interrupt / steer / queue act on the turn the user is looking at, which
    // for the main view is the main agent's own.
    markRunning(null)
    expect(sendingPausedReason(getDirectChat())).toBeNull()
  })

  it('goes live again once that instance lands', () => {
    enterDirect('A', 'one')
    markRunning({ agent: 'A', handle: 'one' })
    clearRunning({ agent: 'A', handle: 'one' })
    expect(sendingPausedReason(getDirectChat())).toBeNull()
  })
})

describe('busy, read through the view on screen', () => {
  it('is that view own turn, not any turn', () => {
    markRunning({ agent: 'A', handle: 'one' })
    expect(getUiState().busy).toBe(false)

    enterDirect('A', 'one')
    expect(getUiState().busy).toBe(true)

    leaveDirect()
    expect(getUiState().busy).toBe(false)
  })

  it('tracks several turns at once', () => {
    markRunning(null)
    markRunning({ agent: 'A', handle: 'one' })

    enterDirect('A', 'one')
    expect(getUiState().busy).toBe(true)
    clearRunning({ agent: 'A', handle: 'one' })
    expect(getUiState().busy).toBe(false)

    // The main agent is still working; going back shows that.
    leaveDirect()
    expect(getUiState().busy).toBe(true)
  })
})

describe('switching views', () => {
  it('remembers the outgoing offset at switch time', () => {
    let top = 0
    bindScrollReader(() => top)

    top = 120
    enterDirect('A', 'one')
    // The reader is the outgoing view's, so this offset belongs to `main`.
    expect(recallScroll(MAIN_VIEW_KEY)).toBe(120)

    top = 7
    leaveDirect()
    expect(recallScroll(directKey('A', 'one'))).toBe(7)

    bindScrollReader(null)
  })

  it('does not overwrite an offset when the switch is a no-op', () => {
    let top = 5
    bindScrollReader(() => top)
    rememberScroll(MAIN_VIEW_KEY, 99)

    top = 0
    leaveDirect()

    expect(recallScroll(MAIN_VIEW_KEY)).toBe(99)
    bindScrollReader(null)
  })
})

describe('/instance', () => {
  const cmd = coreCommands.find(c => c.name === 'instance')!
  const rows = [
    { agent: 'Coder', createdAtMs: 0, handle: 'greet_coder', kind: 'cli', sessionKey: 's1', updatedAtMs: 2 },
    { agent: 'Writer', createdAtMs: 0, handle: 'greet_writer', kind: 'cli', sessionKey: 's1', updatedAtMs: 1 },
    {
      agent: 'Coder',
      createdAtMs: 0,
      handle: 'run-1/node-a',
      kind: 'dag-node',
      sessionKey: 's1',
      updatedAtMs: 3
    }
  ]

  const run = (arg: string) => {
    const said: string[] = []
    cmd.run(arg, { transcript: { sys: (t: string) => said.push(t) } } as never, 'instance')
    return said.join('\n')
  }

  it('switches by the index it printed', () => {
    patchDirectChat({ instances: rows as never })
    run('1')
    expect(getDirectChat().active).toEqual({ agent: 'Coder', handle: 'greet_coder' })
  })

  it('switches by full name', () => {
    patchDirectChat({ instances: rows as never })
    run('Writer/greet_writer')
    expect(getDirectChat().active).toEqual({ agent: 'Writer', handle: 'greet_writer' })
  })

  it('leaves on main', () => {
    patchDirectChat({ instances: rows as never })
    enterDirect('Coder', 'greet_coder')
    run('main')
    expect(getDirectChat().active).toBeNull()
  })

  it('lists only addressable instances, never dag nodes', () => {
    patchDirectChat({ instances: rows as never })
    const said = run('')
    expect(said).toContain('Coder/greet_coder')
    expect(said).toContain('Writer/greet_writer')
    expect(said).not.toContain('run-1/node-a')
  })

  it('refuses an index that indexes a dag node away', () => {
    // The listing is 1-based over the filtered rows, so index 3 must not
    // resolve to the dag-node row that sorts first in the raw list.
    patchDirectChat({ instances: rows as never })
    run('3')
    expect(getDirectChat().active).toBeNull()
  })

  it('says so when the session has no instances', () => {
    expect(run('')).toContain('no sub-agent instances')
  })
})

describe('isViewWorking', () => {
  const row = (agent: string, handle: string, status: string) =>
    ({ agent, handle, status, createdAtMs: 1, kind: 'acp', sessionKey: 's1', updatedAtMs: 1 }) as never

  it('is false with no view on screen', () => {
    expect(isViewWorking(getDirectChat())).toBe(false)
  })

  it('follows a turn this client sent', () => {
    enterDirect('A', 'one')
    expect(isViewWorking(getDirectChat())).toBe(false)

    markRunning({ agent: 'A', handle: 'one' })
    expect(isViewWorking(getDirectChat())).toBe(true)

    clearRunning({ agent: 'A', handle: 'one' })
    expect(isViewWorking(getDirectChat())).toBe(false)
  })

  it('follows a turn dispatched somewhere else, which only the strip reports', () => {
    // A spawn the main agent made, or a DAG node. The wire tags an instance on
    // the four events of a direct turn only, so nothing marks this view running
    // and the row is the sole signal that the instance is working.
    enterDirect('A', 'one')
    patchDirectChat({ instances: [row('A', 'one', 'running')] })
    expect(isViewWorking(getDirectChat())).toBe(true)

    patchDirectChat({ instances: [row('A', 'one', 'pending')] })
    expect(isViewWorking(getDirectChat())).toBe(true)

    patchDirectChat({ instances: [row('A', 'one', 'completed')] })
    expect(isViewWorking(getDirectChat())).toBe(false)
  })

  it('reads the row of the instance on screen and no other', () => {
    enterDirect('A', 'one')
    patchDirectChat({ instances: [row('B', 'two', 'running'), row('A', 'other', 'running')] })

    expect(isViewWorking(getDirectChat())).toBe(false)
  })

  it('is false for an instance with no row at all', () => {
    enterDirect('A', 'gone')
    patchDirectChat({ instances: [] })

    expect(isViewWorking(getDirectChat())).toBe(false)
  })
})

describe('visibleRows', () => {
  const main = [{ role: 'assistant' as const, text: 'raven said this' }]

  it('shows the main conversation when no instance is on screen', () => {
    expect(visibleRows(getDirectChat(), main)).toEqual(main)
  })

  it('never shows zero rows for an instance not read yet', () => {
    // The read is an rpc, so this view is empty for a round trip -- and a render
    // with nothing in it leaves the main conversation on screen until the read
    // lands, which is what "switching in shows Raven's messages, then they
    // vanish a few seconds later" was.
    enterDirect('Coder', 'h1')
    const rows = visibleRows(getDirectChat(), main)

    expect(rows).toHaveLength(1)
    expect(rows[0]!.role).toBe('system')
    expect(rows[0]!.text).not.toBe('raven said this')
  })

  it('shows the instance own rows as soon as there are any', () => {
    enterDirect('Coder', 'h1')
    setDirectTranscript(directKey('Coder', 'h1'), [{ role: 'user' as const, text: 'the task' }])

    expect(visibleRows(getDirectChat(), main).map(m => m.text)).toEqual(['the task'])
  })

  it('keeps the two views apart', () => {
    setDirectTranscript(directKey('Coder', 'h1'), [{ role: 'user' as const, text: 'coder' }])
    setDirectTranscript(directKey('Writer', 'h2'), [{ role: 'user' as const, text: 'writer' }])

    enterDirect('Writer', 'h2')
    expect(visibleRows(getDirectChat(), main).map(m => m.text)).toEqual(['writer'])

    leaveDirect()
    expect(visibleRows(getDirectChat(), main)).toEqual(main)
  })
})
describe('visibleRows on an instance with no turns', () => {
  // `/new-instance` makes a zero-turn instance reachable: it is created and
  // entered before anything has been said to it, and the history read answers
  // `{turns: []}`. Before that, every listed instance had been spawned, so an
  // empty transcript only ever meant "the read has not landed".
  it('says it is reading only while the read is still out', () => {
    enterDirect('Raven-PPT', 'raven-ppt-a1b2c3')
    expect(visibleRows(getDirectChat(), [])[0]!.text).toContain('reading')
  })

  it('does not keep saying it is reading once the read came back empty', () => {
    enterDirect('Raven-PPT', 'raven-ppt-a1b2c3')
    setDirectTranscript(directKey('Raven-PPT', 'raven-ppt-a1b2c3'), [])

    const row = visibleRows(getDirectChat(), [])[0]!
    expect(row.text).not.toContain('reading')
    expect(row.text.length).toBeGreaterThan(0)
  })

  it('still shows the turns once there are any', () => {
    enterDirect('Raven-PPT', 'raven-ppt-a1b2c3')
    setDirectTranscript(directKey('Raven-PPT', 'raven-ppt-a1b2c3'), [{ role: 'user', text: 'hi' }])
    expect(visibleRows(getDirectChat(), []).map(r => r.text)).toEqual(['hi'])
  })
})

describe('/new-instance', () => {
  const cmd = coreCommands.find(c => c.name === 'new-instance')!

  const row = {
    agent: 'Raven-PPT',
    createdAtMs: 5,
    handle: 'raven-ppt-a1b2c3',
    kind: 'cli',
    sessionKey: 's1',
    status: 'idle',
    updatedAtMs: 5
  }

  const run = (arg: string, rpc = vi.fn(() => Promise.resolve({ instance: row })), sid: null | string = 's1') => {
    const said: string[] = []
    const errors: unknown[] = []
    cmd.run(
      arg,
      {
        gateway: { rpc },
        guarded:
          <T,>(fn: (r: T) => void) =>
          (r: null | T) => {
            if (r !== null) {
              fn(r)
            }
          },
        guardedErr: (e: unknown) => errors.push(e),
        sid,
        transcript: { sys: (t: string) => said.push(t) }
      } as never,
      'new-instance'
    )
    return { errors, rpc, said }
  }

  it('opens the picker with no argument, and calls nothing', () => {
    const { rpc } = run('')

    expect(getOverlayState().newInstance).toBe(true)
    expect(rpc).not.toHaveBeenCalled()
    expect(getDirectChat().active).toBeNull()
  })

  it('creates and switches when given an agent name', async () => {
    const { rpc } = run('Raven-PPT')
    await Promise.resolve()
    await Promise.resolve()

    expect(rpc).toHaveBeenCalledWith(
      'subagents.instance.create',
      { agent: 'Raven-PPT', session_key: 's1' },
      { quiet: true }
    )
    expect(getDirectChat().active).toEqual({ agent: 'Raven-PPT', handle: 'raven-ppt-a1b2c3' })
    expect(getOverlayState().newInstance).toBe(false)
  })

  it('puts the new chip on the strip without waiting for a refresh', async () => {
    run('Raven-PPT')
    await Promise.resolve()
    await Promise.resolve()

    expect(getDirectChat().instances.map(r => r.handle)).toEqual(['raven-ppt-a1b2c3'])
  })

  it('takes a multi-word agent name verbatim', () => {
    const { rpc } = run('General Agent')

    expect(rpc).toHaveBeenCalledWith(
      'subagents.instance.create',
      { agent: 'General Agent', session_key: 's1' },
      { quiet: true }
    )
  })

  it('refuses without a session rather than calling the gateway', () => {
    const { rpc, said } = run('Raven-PPT', vi.fn(() => Promise.resolve({ instance: row })), null)

    expect(rpc).not.toHaveBeenCalled()
    expect(said.join('\n')).toContain('no active session')
  })

  it('stays on the main conversation when the create is refused', async () => {
    const rpc = vi.fn(() => Promise.reject(new Error('is stateless')))
    const { errors } = run('Researcher', rpc as never)
    await Promise.resolve()
    await Promise.resolve()

    expect(getDirectChat().active).toBeNull()
    expect(errors).toHaveLength(1)
  })
})
