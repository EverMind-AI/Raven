import { beforeEach, describe, expect, it } from 'vitest'

import {
  absorb,
  gone,
  has,
  hasBinaryFrames,
  hasBuildFlag,
  hasInstanceTurns,
  hasNamingEnded,
  hasNamingFlag,
  hasStillOnDisk,
  hasSubagentDelivered,
  hasToolOk,
  hasTurnDuration,
  hasUpdateFlag,
  resetCapabilities,
  serves,
  servesChannels,
} from './capabilities'

beforeEach(() => {
  resetCapabilities()
})

describe('the absent-method registry', () => {
  it('answers for a name nothing has refused', () => {
    expect(has('subagent')).toBe(true)
  })

  it('records a name only when the gateway said -32601', () => {
    expect(gone('subagent', { code: -32603, message: 'internal' })).toBe(false)
    expect(has('subagent')).toBe(true)

    expect(gone('subagent', { code: -32601, message: 'no such method' })).toBe(true)
    expect(has('subagent')).toBe(false)
  })

  /* Today's `rpcGone` answered from the set, not from the error it was handed:
     once a surface is known absent, a later transport failure against it is
     still an absent surface. The four catch sites read it that way -- they
     rethrow only while the surface might still be there. */
  it('goes on answering for a registered name whatever the next error is', () => {
    gone('subagents', { code: -32601 })
    expect(gone('subagents', { code: -1, message: 'not connected' })).toBe(true)
  })

  it('is not fooled by an error that is not an object', () => {
    expect(gone('subagent', null)).toBe(false)
    expect(gone('subagent', 'boom')).toBe(false)
    expect(gone('subagent', undefined)).toBe(false)
    expect(has('subagent')).toBe(true)
  })

  it('keeps one name apart from another', () => {
    gone('subagent', { code: -32601 })
    expect(has('subagents')).toBe(true)
  })
})

describe("the handshake's own list", () => {
  it('is empty until a handshake lands', () => {
    expect(serves('subscriptions')).toBe(false)
  })

  it('takes what system.hello announced, and replaces it on the next one', () => {
    absorb(['jsonrpc-2.0', 'subscriptions', 'cli-dispatch'])
    expect(serves('subscriptions')).toBe(true)
    expect(serves('telepathy')).toBe(false)

    absorb([])
    expect(serves('subscriptions')).toBe(false)
  })

  it('survives a gateway that sends no list at all', () => {
    absorb(undefined)
    expect(serves('subscriptions')).toBe(false)
  })
})

describe('the eleven version tolerances', () => {
  it('hasTurnDuration: the runtime timed the turn', () => {
    expect(hasTurnDuration(1234)).toBe(true)
    /* Zero is a duration the server sent, not an absent field. */
    expect(hasTurnDuration(0)).toBe(true)
    expect(hasTurnDuration(undefined)).toBe(false)
    expect(hasTurnDuration(null)).toBe(false)
  })

  it('hasToolOk: the frame carries the emit site verdict', () => {
    expect(hasToolOk(true)).toBe(true)
    expect(hasToolOk(false)).toBe(true)
    expect(hasToolOk(undefined)).toBe(false)
    /* A truthy non-boolean is not a verdict: the text heuristic still decides. */
    expect(hasToolOk('ok')).toBe(false)
  })

  it('hasSubagentDelivered: true until the name is known absent', () => {
    expect(hasSubagentDelivered()).toBe(true)
    gone('subagent.delivered', { code: -32601 })
    expect(hasSubagentDelivered()).toBe(false)
  })

  it('hasNamingEnded: true until the name is known absent', () => {
    expect(hasNamingEnded()).toBe(true)
    gone('session.naming_ended', { code: -32601 })
    expect(hasNamingEnded()).toBe(false)
  })

  it('hasNamingFlag: the turn.send answer carries a naming verdict', () => {
    expect(hasNamingFlag({ naming: false })).toBe(true)
    expect(hasNamingFlag({ naming: true })).toBe(true)
    expect(hasNamingFlag({})).toBe(false)
    expect(hasNamingFlag(undefined)).toBe(false)
  })

  it('hasStillOnDisk: the session.delete answer carries the flag', () => {
    expect(hasStillOnDisk({ still_on_disk: false })).toBe(true)
    expect(hasStillOnDisk({ still_on_disk: true })).toBe(true)
    expect(hasStillOnDisk({ deleted: 'a' })).toBe(false)
    expect(hasStillOnDisk(null)).toBe(false)
  })

  it('hasUpdateFlag: the version answer says a newer build exists', () => {
    expect(hasUpdateFlag({ update_available: true })).toBe(true)
    expect(hasUpdateFlag({ update_available: false })).toBe(false)
    expect(hasUpdateFlag({ raven_version: '0.1.0' })).toBe(false)
    expect(hasUpdateFlag(undefined)).toBe(false)
  })

  it('hasBinaryFrames: true until the browser surface is known absent', () => {
    expect(hasBinaryFrames()).toBe(true)
    gone('browser.frame', { code: -32601 })
    expect(hasBinaryFrames()).toBe(false)
  })

  it('hasBuildFlag: the subagents row says a build is in flight', () => {
    expect(hasBuildFlag({ building: true })).toBe(true)
    expect(hasBuildFlag({ building: false })).toBe(false)
    expect(hasBuildFlag({ name: 'codex' })).toBe(false)
    expect(hasBuildFlag(undefined)).toBe(false)
  })

  it('hasInstanceTurns: the history answer carries its turns', () => {
    expect(hasInstanceTurns([{ role: 'user' }])).toBe(true)
    /* An empty list is an answer; nothing at all is an older gateway. */
    expect(hasInstanceTurns([])).toBe(true)
    expect(hasInstanceTurns(undefined)).toBe(false)
    expect(hasInstanceTurns(null)).toBe(false)
  })

  it('servesChannels: true until channels.* is known absent', () => {
    expect(servesChannels()).toBe(true)
    gone('channels', { code: -32601 })
    expect(servesChannels()).toBe(false)
  })
})
