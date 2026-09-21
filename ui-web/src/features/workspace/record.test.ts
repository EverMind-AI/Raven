// @vitest-environment happy-dom
/* How a replayed conversation is counted into workspace turns.
 *
 * One turn per user message is the rule the panel files a changed file under,
 * and the transcript counts the same messages by the same rule -- so a message
 * this one counts and the transcript does not puts every later file under the
 * wrong turn on a reload, and only on a reload.
 */

import { beforeEach, describe, expect, it } from 'vitest'

import { setWsPane } from '../../state/wsPane'
import { wsOnHistory } from './record'
import * as store from './store'

beforeEach(() => {
  store.reset()
  /* The record redraws the pane's badge as it counts; an island runs inside
     the assembled page, so a test has to stand in for it. */
  setWsPane({
    view: () => ({ tab: 'changes', open: false, picked: false }),
    pick: () => {}, show: () => {}, setOpen: () => {}, bump: () => {}, draw: () => {}, showsTurn: () => true,
  })
})

describe('replaying a conversation into workspace turns', () => {
  it('counts one turn per question', () => {
    wsOnHistory([
      { role: 'user', text: 'summarise the report' },
      { role: 'assistant', text: 'done' },
      { role: 'user', text: 'now the appendix' },
    ])

    expect(store.currentTurn()).toBe(2)
  })

  it('counts no turn for a message merged into the turn already running', () => {
    /* Live, nothing advances the counter for one of these: it joins a turn
       rather than opening one. Counting it here left the files of every later
       turn filed one turn too high after a reload. */
    wsOnHistory([
      { role: 'user', text: 'summarise the report' },
      { role: 'user', text: 'Q4 only', mid_turn: true },
    ])

    expect(store.currentTurn()).toBe(1)
  })
})
