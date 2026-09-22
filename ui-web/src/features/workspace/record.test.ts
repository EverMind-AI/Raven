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
import { wsOnHistory, wsOnTool, wsOnToolDone } from './record'
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

/* Which of the three a written file is, which is what the diff tab's glyph and
   the transcript's "new" badge both read. A whole-file write says nothing on
   its own -- the tool reports whether there was a file under it, and the live
   event and a reload carry that answer in different fields. */
describe('telling a created file from a rewritten one', () => {
  const NEW_FILE = '--- a/w/new.md\n+++ b/w/new.md\n@@ -0,0 +1,1 @@\n+hello'
  const REWRITE = '--- a/w/old.md\n+++ b/w/old.md\n@@ -1,1 +1,1 @@\n-was\n+now'
  const kindOf = (path: string): string | undefined =>
    store.shared().changes.find((c) => c.key === path)?.kind

  it('marks a write with no previous contents as a creation', () => {
    const args = { path: '/w/new.md', content: 'hello' }
    wsOnTool('write_file', args)
    wsOnToolDone('write_file', args, true, '', null, NEW_FILE, { path: '/w/new.md', after: 'hello' })

    expect(kindOf('/w/new.md')).toBe('add')
  })

  it('leaves a write over a file that was there as a rewrite', () => {
    const args = { path: '/w/old.md', content: 'now' }
    wsOnTool('write_file', args)
    wsOnToolDone('write_file', args, true, '', null, REWRITE, { path: '/w/old.md', after: 'now', before: 'was' })

    expect(kindOf('/w/old.md')).toBe('write')
  })

  /* An empty string is a file that existed and was empty, which is why the
     field is read for its presence and not for its truthiness -- and why the
     diff cannot answer here: the tool really does send the zero-line header a
     creation sends, because there were no old lines to diff against. */
  it('leaves a write over an empty file as a rewrite', () => {
    const args = { path: '/w/empty.md', content: 'now' }
    const diff = '--- /w/empty.md\n+++ /w/empty.md\n@@ -0,0 +1 @@\n+now'
    wsOnTool('write_file', args)
    wsOnToolDone('write_file', args, true, '', null, diff, { path: '/w/empty.md', after: 'now', before: '' })

    expect(kindOf('/w/empty.md')).toBe('write')
  })

  /* Writing the same path twice in one turn: the second call replaces contents
     the turn itself put there, so the row is still a file that was created. */
  it('keeps a file it created a creation when it is written again', () => {
    const first = { path: '/w/twice.md', content: 'hello' }
    const made = '--- /w/twice.md\n+++ /w/twice.md\n@@ -0,0 +1 @@\n+hello'
    wsOnTool('write_file', first)
    wsOnToolDone('write_file', first, true, '', null, made, { path: '/w/twice.md', after: 'hello' })
    const second = { path: '/w/twice.md', content: 'again' }
    const redone = '--- /w/twice.md\n+++ /w/twice.md\n@@ -1 +1 @@\n-hello\n+again'
    wsOnTool('write_file', second)
    wsOnToolDone('write_file', second, true, '', null, redone, { path: '/w/twice.md', after: 'again', before: 'hello' })

    expect(kindOf('/w/twice.md')).toBe('add')
  })

  /* A reload has no payload to read: the stored call carries the diff alone, so
     the same file has to come back the same way it went in. */
  it('reads the creation off the stored diff on replay', () => {
    wsOnHistory([
      { role: 'user', text: 'write them' },
      {
        role: 'assistant',
        tool_calls: [
          { id: 'c1', name: 'write_file', arguments: JSON.stringify({ path: '/w/new.md', content: 'hello' }) },
          { id: 'c2', name: 'write_file', arguments: JSON.stringify({ path: '/w/old.md', content: 'now' }) },
        ],
      },
      { role: 'tool', tool_call_id: 'c1', diff: NEW_FILE },
      { role: 'tool', tool_call_id: 'c2', diff: REWRITE },
    ])

    expect(kindOf('/w/new.md')).toBe('add')
    expect(kindOf('/w/old.md')).toBe('write')
  })

  /* An edit is never a creation: `edit_file` can only touch a file that is
     already there. */
  it('leaves an edit alone whatever its diff says', () => {
    const args = { path: '/w/a.py', old_text: '', new_text: 'x' }
    wsOnTool('edit_file', args)
    wsOnToolDone('edit_file', args, true, '', null, NEW_FILE)

    expect(kindOf('/w/a.py')).toBe('edit')
  })
})
