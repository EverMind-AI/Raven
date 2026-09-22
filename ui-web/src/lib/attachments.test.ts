/* Reading a sent message back: its own words, the files that rode with it, and
   the lines the runtime wrote that are neither. The replay is what this is for
   -- a message reopened from session history carries the engine's annotations,
   and drawing those as the question is what a reloaded page used to do. */
import { describe, expect, it } from 'vitest'

import { splitAttachments, stripRuntimeNotes } from './attachments'

/* Both spellings the catalogue carries, since a message sent under one
   language is read back under whichever is in force. */
const NOTES = ['[attachments, saved in the workspace]', '[\u9644\u4ef6\uff0c\u5df2\u5b58\u653e\u5728\u5de5\u4f5c\u76ee\u5f55\u4e0b]']

describe('reading a sent message back', () => {
  it('keeps a message that carried nothing exactly as it was', () => {
    expect(splitAttachments('what is a monoid', NOTES)).toEqual({ body: 'what is a monoid', atts: [] })
  })

  it('separates the words from the files', () => {
    const sent = 'have a look\n\n[attachments, saved in the workspace]\n- uploads/a.png\n- uploads/b.pdf'
    expect(splitAttachments(sent, NOTES)).toEqual({
      body: 'have a look',
      atts: ['uploads/a.png', 'uploads/b.pdf'],
    })
  })

  it('reads a message the engine annotated, which is how history returns one', () => {
    /* What `session.resume` gives back for a picture: the flattened image part
       at the head, and the engine's line about what it could see at the foot.
       The old reader required every line after the note to be blank or a path,
       so this whole message was drawn as the question. */
    const stored = '[image] hello, what is wrong with this picture\n\n'
      + '[attachments, saved in the workspace]\n- uploads/shot.png\n\n'
      + '[Image: shot.png (path: /home/me/.raven/workspace/uploads/shot.png) | 1312x732px, '
      + 'downscaled from 5312x2964 \u2014 re-read it with read_file if you need another look]'
    expect(splitAttachments(stored, NOTES)).toEqual({
      body: 'hello, what is wrong with this picture',
      atts: ['uploads/shot.png'],
    })
  })

  it('reads the four other things the engine says about a picture', () => {
    /* One shape per refusal, and they share only the opening, so they are
       matched by that rather than by the words after it. */
    const DASH = '\u2014'
    const lines = [
      `[Image: a.png (path: /w/a.png) ${DASH} you cannot see images directly]`,
      `[Image: a.png (path: /w/a.png) ${DASH} too large to read into this message]`,
      `[Image: a.png (path: /w/a.png) ${DASH} not shown, this message is already carrying more]`,
      `[Image: a.png (path: /w/a.png) ${DASH} could not be prepared for viewing: broken file]`,
    ]
    for (const line of lines) {
      expect(splitAttachments(`look\n\n${line}`, NOTES).body, line).toBe('look')
    }
  })

  it('leaves alone the words a person wrote that only look like a marker', () => {
    /* `[image]` further down is a person typing, and a bracketed line that
       names no path is not one of the engine's. */
    const sent = 'the word [image] appears here\n\n[Image: not a real marker]'
    expect(splitAttachments(sent, NOTES).body).toBe('the word [image] appears here\n\n[Image: not a real marker]')
  })

  it('strips the runtime lines on their own, for a message with no files', () => {
    const stored = '[image] look at this\n\n[Image: a.png (path: /w/a.png) | 10x10px]'
    expect(stripRuntimeNotes(stored)).toBe('look at this')
  })

  it('reads the note in whichever language it was written in', () => {
    const sent = '\u770b\u4e00\u4e0b\n\n[\u9644\u4ef6\uff0c\u5df2\u5b58\u653e\u5728\u5de5\u4f5c\u76ee\u5f55\u4e0b]\n- uploads/a.png'
    expect(splitAttachments(sent, NOTES)).toEqual({ body: '\u770b\u4e00\u4e0b', atts: ['uploads/a.png'] })
  })

  it('takes the last note when a message quotes an earlier one', () => {
    const sent = 'first\n\n[attachments, saved in the workspace]\n- old.png\n\n'
      + 'second\n\n[attachments, saved in the workspace]\n- new.png'
    expect(splitAttachments(sent, NOTES).atts).toEqual(['new.png'])
  })
})
