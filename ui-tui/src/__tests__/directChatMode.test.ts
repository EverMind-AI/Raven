import { beforeEach, describe, expect, it } from 'vitest'

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
  leaveDirect,
  MAIN_VIEW_KEY,
  markRunning,
  patchDirectChat,
  recallScroll,
  rememberScroll,
  resetDirectChat,
  sendingPausedReason,
  setDirectTranscript
} from '../app/directChatStore.js'
import { coreCommands } from '../app/slash/commands/core.js'
import { getUiState, resetUiState } from '../app/uiStore.js'

beforeEach(() => {
  resetDirectChat()
  resetUiState()
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
