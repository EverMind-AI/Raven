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

import type { WsChange } from './types'

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

/* A file that is gone, which no tool argument can say: `exec` deletes it and
   the runtime reports it afterwards, on whichever call made it vanish. */
describe('recording a file the turn removed', () => {
  const rowFor = (path: string): WsChange | undefined =>
    store.shared().changes.find((c) => c.key === path)

  it('draws every line the removed file held, with the contents the runtime caught', () => {
    wsOnToolDone('exec', { command: 'rm /w/old.md' }, true, '', null, undefined, undefined,
      [{ path: '/w/old.md', before: 'one\ntwo\nthree\n' }])

    const row = rowFor('/w/old.md')
    expect(row?.kind).toBe('delete')
    expect(row?.add).toBe(0)
    expect(row?.del).toBe(3)
    expect(row?.hunks).toHaveLength(1)
    expect(row?.hunks[0]?.rows.every((r) => r[0] === 'del')).toBe(true)
  })

  /* Created and removed inside one turn is nothing at all -- git shows the
     same nothing for it, and a row saying a file went would name one the
     reader never had. */
  it('leaves no row at all for a file the same turn created', () => {
    const args = { path: '/w/scratch.md', content: 'tmp' }
    wsOnTool('write_file', args)
    wsOnToolDone('write_file', args, true, '', null,
      '--- a/w/scratch.md\n+++ b/w/scratch.md\n@@ -0,0 +1,1 @@\n+tmp',
      { path: '/w/scratch.md', after: 'tmp' })
    expect(rowFor('/w/scratch.md')?.kind).toBe('add')

    wsOnToolDone('exec', { command: 'rm /w/scratch.md' }, true, '', null, undefined, undefined,
      [{ path: '/w/scratch.md', before: 'tmp' }])

    expect(rowFor('/w/scratch.md')).toBeUndefined()
  })

  /* The contents can be past capture -- too large, not text, or never read --
     and then what this turn wrote into the file is the best account of what
     was lost. The removal rides the LATER call, not the write's own. */
  it('rebuilds the lost contents from this turn\'s own write when the runtime caught none', () => {
    const args = { path: '/w/notes.md', content: 'alpha\nbeta\n' }
    wsOnTool('write_file', args)
    wsOnToolDone('write_file', args, true, '', null,
      '--- a/w/notes.md\n+++ b/w/notes.md\n@@ -1,1 +1,2 @@\n-was\n+alpha\n+beta',
      { path: '/w/notes.md', after: 'alpha\nbeta\n', before: 'was' })

    wsOnToolDone('exec', { command: 'rm /w/notes.md' }, true, '', null, undefined, undefined,
      [{ path: '/w/notes.md' }])

    const row = rowFor('/w/notes.md')
    expect(row?.kind).toBe('delete')
    expect(row?.del).toBe(2)
    expect(row?.hunks[0]?.rows.map((r) => r[1])).toEqual(['alpha', 'beta'])
  })

  /* Nothing written and nothing caught: the row still says the file went, and
     says nothing it cannot support about what was in it. */
  it('records the removal with no hunk when nothing can say what was in it', () => {
    wsOnToolDone('exec', { command: 'rm /w/opaque.bin' }, true, '', null, undefined, undefined,
      [{ path: '/w/opaque.bin' }])

    const row = rowFor('/w/opaque.bin')
    expect(row?.kind).toBe('delete')
    expect(row?.hunks).toEqual([])
    expect(row?.del).toBe(0)
  })

  /* What the turn LEFT in the file, not what its last write put down: an edit
     after the write is part of what was lost. */
  it('rebuilds the lost contents as the turn\'s later edits left them', () => {
    const wrote = { path: '/w/notes.md', content: 'old\nkept\n' }
    wsOnTool('write_file', wrote)
    wsOnToolDone('write_file', wrote, true, '', null, undefined, { path: '/w/notes.md', after: 'old\nkept\n', before: 'was' })
    const edited = { path: '/w/notes.md', old_text: 'old', new_text: 'new' }
    wsOnTool('edit_file', edited)

    wsOnToolDone('exec', { command: 'rm /w/notes.md' }, true, '', null, undefined, undefined,
      [{ path: '/w/notes.md' }])

    expect(rowFor('/w/notes.md')?.hunks[0]?.rows.map((r) => r[1])).toEqual(['new', 'kept'])
  })

  /* An edit the followed text cannot take means the final contents are not
     known, and a body that might be wrong is worse than none. */
  it('gives up the rebuilt contents when an edit does not fit what was followed', () => {
    const wrote = { path: '/w/notes.md', content: 'old\n' }
    wsOnTool('write_file', wrote)
    wsOnToolDone('write_file', wrote, true, '', null, undefined, { path: '/w/notes.md', after: 'old\n', before: 'was' })
    wsOnTool('edit_file', { path: '/w/notes.md', old_text: 'elsewhere', new_text: 'new' })

    wsOnToolDone('exec', { command: 'rm /w/notes.md' }, true, '', null, undefined, undefined,
      [{ path: '/w/notes.md' }])

    const row = rowFor('/w/notes.md')
    expect(row?.kind).toBe('delete')
    expect(row?.hunks).toEqual([])
  })

  /* An edit's hunk is the slice it touched, so reading it back would draw two
     changed lines as the whole of a file that had two hundred. */
  it('does not mistake an edited row\'s slice for the file that was lost', () => {
    const args = { path: '/w/mod.py', old_text: 'x = 1\n', new_text: 'x = 2\n' }
    wsOnTool('edit_file', args)

    wsOnToolDone('exec', { command: 'rm /w/mod.py' }, true, '', null, undefined, undefined,
      [{ path: '/w/mod.py' }])

    const row = rowFor('/w/mod.py')
    expect(row?.kind).toBe('delete')
    expect(row?.hunks).toEqual([])
  })

  /* The row is keyed by the path the model typed -- `write_file` takes a
     relative one -- and the removal arrives under the path the runtime
     resolved. Matching on the string alone left the same file drawn twice:
     created, and gone. */
  it('matches a removal to this turn\'s row for the same file under the resolved path', () => {
    const args = { path: 'scratch.md', content: 'tmp\n' }
    wsOnTool('write_file', args)
    wsOnToolDone('write_file', args, true, '', null,
      '--- a/w/scratch.md\n+++ b/w/scratch.md\n@@ -0,0 +1,1 @@\n+tmp',
      { path: '/w/scratch.md', after: 'tmp\n' })

    wsOnToolDone('exec', { command: 'rm scratch.md' }, true, '', null, undefined, undefined,
      [{ path: '/w/scratch.md', before: 'tmp\n' }])

    expect(store.shared().changes).toHaveLength(0)
  })

  it('rebuilds the one row, not a second, when the turn edited the file first', () => {
    const args = { path: 'mod.py', old_text: 'x = 1\n', new_text: 'x = 2\n' }
    wsOnTool('edit_file', args)

    wsOnToolDone('exec', { command: 'rm mod.py' }, true, '', null, undefined, undefined,
      [{ path: '/w/mod.py', before: 'x = 2\n' }])

    expect(store.shared().changes).toHaveLength(1)
    expect(store.shared().changes[0]?.kind).toBe('delete')
    expect(store.shared().changes[0]?.del).toBe(1)
  })

  /* A replay has no payload to re-key from either: the stored call carries the
     argument the model wrote and the stored removal the resolved path. */
  it('matches a replayed removal to the row the stored relative argument keyed', () => {
    wsOnHistory([
      { role: 'user', text: 'write it then drop it' },
      {
        role: 'assistant',
        tool_calls: [
          { id: 'c1', name: 'write_file', arguments: JSON.stringify({ path: 'tmp.md', content: 'a\nb\n' }) },
          { id: 'c2', name: 'exec', arguments: JSON.stringify({ command: 'rm tmp.md' }) },
        ],
      },
      { role: 'tool', tool_call_id: 'c1', diff: '--- a/w/tmp.md\n+++ b/w/tmp.md\n@@ -0,0 +1,2 @@\n+a\n+b' },
      { role: 'tool', tool_call_id: 'c2', file_removed: [{ path: '/w/tmp.md', del: 2 }] },
    ])

    expect(store.shared().changes).toHaveLength(0)
  })

  /* A reload carries the stored shape instead: a line count, never the body. */
  it('replays a stored removal as the same row, off its line count', () => {
    wsOnHistory([
      { role: 'user', text: 'clean it up' },
      {
        role: 'assistant',
        tool_calls: [{ id: 'c1', name: 'exec', arguments: JSON.stringify({ command: 'rm /w/dead.py' }) }],
      },
      { role: 'tool', tool_call_id: 'c1', file_removed: [{ path: '/w/dead.py', del: 46 }] },
    ])

    const row = rowFor('/w/dead.py')
    expect(row?.kind).toBe('delete')
    expect(row?.add).toBe(0)
    expect(row?.del).toBe(46)
    expect(row?.hunks).toEqual([])
  })

  it('replays a removal of a file the same stored turn wrote, hunk and all', () => {
    wsOnHistory([
      { role: 'user', text: 'write it then drop it' },
      {
        role: 'assistant',
        tool_calls: [
          { id: 'c1', name: 'write_file', arguments: JSON.stringify({ path: '/w/tmp.md', content: 'a\nb\n' }) },
          { id: 'c2', name: 'exec', arguments: JSON.stringify({ command: 'rm /w/tmp.md' }) },
        ],
      },
      { role: 'tool', tool_call_id: 'c1', diff: '--- a/w/tmp.md\n+++ b/w/tmp.md\n@@ -1,1 +1,2 @@\n-was\n+a\n+b' },
      { role: 'tool', tool_call_id: 'c2', file_removed: [{ path: '/w/tmp.md', del: 2 }] },
    ])

    const row = rowFor('/w/tmp.md')
    expect(row?.kind).toBe('delete')
    expect(row?.del).toBe(2)
    expect(row?.hunks[0]?.rows.map((r) => r[1])).toEqual(['a', 'b'])
  })
})
