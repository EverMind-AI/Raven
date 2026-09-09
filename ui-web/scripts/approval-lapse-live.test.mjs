/* An approval that closed because nobody answered says so.

   The frame always carried `reason`; the handler dropped it, so a sheet that
   expired vanished exactly like one the reader had answered. What the reader
   then saw was the run reporting a system error about an approval that had
   merely lapsed.

   Driven through the assembled live layer rather than a copy of the handler:
   `live/*.js` are fragments of one IIFE and cannot be imported, and a copy
   would go on passing after the shipped one changed. */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const build = readFileSync(new URL('../build.py', import.meta.url), 'utf8')
const manifest = build.match(/_LIVE_PARTS = \[(.*?)\n\]/s)
if (!manifest) throw new Error('_LIVE_PARTS is absent from build.py')
const parts = [...manifest[1].matchAll(/"([^"]+\.js)"/g)].map((m) => m[1])
const live = parts
  .map((name) => readFileSync(new URL(`../src/live/${name}`, import.meta.url), 'utf8'))
  .join('')

function handlerSource() {
  const mark = "rpc.notify['approval.closed'] = (p) => {"
  const start = live.indexOf(mark)
  if (start < 0) throw new Error("approval.closed is absent from the assembled live layer")
  const brace = live.indexOf('{', start + mark.length - 1)
  let depth = 0
  for (let index = brace; index < live.length; index += 1) {
    if (live[index] === '{') depth += 1
    else if (live[index] === '}') {
      depth -= 1
      if (depth === 0) return live.slice(start, index + 1)
    }
  }
  throw new Error('approval.closed has no closing brace in the assembled live layer')
}

function run(reason) {
  const seen = { toasts: [], closed: [], turns: [] }
  const rpc = { notify: {} }
  const install = Function(
    'rpc', 'notifyTurn', 'sessionCurrent', 'approvalClose', 'toast', 'T',
    `${handlerSource()}\nreturn rpc.notify['approval.closed'];`,
  )
  const handler = install(
    rpc,
    (owner, event) => seen.turns.push([owner, event.type]),
    () => 'tui:open',
    (id) => seen.closed.push(id),
    (text) => seen.toasts.push(text),
    (key) => key,
  )
  handler({ approval_id: 'a1', conversation_id: 'tui:one', reason })
  return seen
}

describe('an approval sheet closing', () => {
  it('says so when the request expired unanswered', () => {
    const seen = run('timeout')

    expect(seen.toasts).toEqual(['gui.confirm.lapsed'])
  })

  it('says so when the transport failed, which is the other nobody-answered', () => {
    expect(run('error').toasts).toEqual(['gui.confirm.lapsed'])
  })

  it('stays quiet for a close the reader caused', () => {
    /* Three of the four reasons are a person: the frame carries the choice
       itself. Telling someone what they just did is noise. */
    for (const reason of ['allow', 'deny', 'deny_stop', 'cancelled']) {
      expect(run(reason).toasts, reason).toEqual([])
    }
  })

  it('still closes the sheet and releases the turn, whatever the reason', () => {
    /* The notice is added beside the old behaviour, not in place of it: a sheet
       left open over the composer is worse than an unexplained one. */
    for (const reason of ['timeout', 'allow']) {
      const seen = run(reason)
      expect(seen.closed, reason).toEqual(['a1'])
      expect(seen.turns, reason).toEqual([['tui:one', 'resume']])
    }
  })
})
