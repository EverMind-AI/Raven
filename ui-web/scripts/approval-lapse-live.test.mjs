// @vitest-environment happy-dom
/* An approval that closed because nobody answered says so.

   The frame always carried `reason`; the handler dropped it, so a sheet that
   expired vanished exactly like one the reader had answered. What the reader
   then saw was the run reporting a system error about an approval that had
   merely lapsed.

   Driven through the part's own handler rather than a copy of it: a copy would
   go on passing after the shipped one changed. */

import { describe, expect, it } from 'vitest'

import { fakeGateway, loadPart } from './legacy-part.mjs'

async function run(reason) {
  const seen = { toasts: [], closed: [], turns: [] }
  const part = await loadPart(() => import('../src/legacy/live/070-notify.js'), {
    fakes: {
      'src/shell/session': { current: () => 'tui:open' },
      'src/shell/toast': { show: (text) => seen.toasts.push(text) },
      'demo/010-kernel.js': { T: (key) => key },
      'demo/040-state.js': {
        approvalClose: (id) => seen.closed.push(id),
        approvalSheet: () => {},
        approveSheet: () => {},
        clarifyClose: () => {},
        clarifySheet: () => {},
        sess: () => null,
      },
      /* `notifyTurn` hands the event to the parked-turn reducer, which is what
         the harness used to watch it for. */
      'live/060-parked.js': {
        transitionTurn: (owner, event) => seen.turns.push([owner, event.type]),
      },
      'live/050-turn.js': { refreshList: () => {} },
    },
  })
  const transport = await fakeGateway(() => Promise.resolve({}))
  part.install()
  transport.emit('approval.closed', { approval_id: 'a1', conversation_id: 'tui:one', reason })
  return seen
}

describe('an approval sheet closing', () => {
  it('says so when the request expired unanswered', async () => {
    const seen = await run('timeout')

    expect(seen.toasts).toEqual(['gui.confirm.lapsed'])
  })

  it('says so when the transport failed, which is the other nobody-answered', async () => {
    expect((await run('error')).toasts).toEqual(['gui.confirm.lapsed'])
  })

  it('stays quiet for a close the reader caused', async () => {
    /* Three of the four reasons are a person: the frame carries the choice
       itself. Telling someone what they just did is noise. */
    for (const reason of ['allow', 'deny', 'deny_stop', 'cancelled']) {
      expect((await run(reason)).toasts, reason).toEqual([])
    }
  })

  it('still closes the sheet and releases the turn, whatever the reason', async () => {
    /* The notice is added beside the old behaviour, not in place of it: a sheet
       left open over the composer is worse than an unexplained one. */
    for (const reason of ['timeout', 'allow']) {
      const seen = await run(reason)
      expect(seen.closed, reason).toEqual(['a1'])
      expect(seen.turns, reason).toEqual([['tui:one', 'resume']])
    }
  })
})
