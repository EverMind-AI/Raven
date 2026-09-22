/* Reading a sent message back: its own words, the files that rode with it, and
   the lines the runtime wrote that are neither. The replay is what this is for
   -- a message reopened from session history carries the engine's annotations,
   and drawing those as the question is what a reloaded page used to do. */
import { describe, expect, it } from 'vitest'

import { I18N } from '../i18n/t'
import { readMessage, splitAttachments, stripRuntimeNotes } from './attachments'

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
    /* The path is the engine's, not the note's: the note says where the
       composer uploaded it, and the file route resolves a relative path
       against the session's own root, which is not always that workspace. */
    expect(splitAttachments(stored, NOTES)).toEqual({
      body: 'hello, what is wrong with this picture',
      atts: ['/home/me/.raven/workspace/uploads/shot.png'],
    })
  })

  it('keeps the note path when the engine named no file', () => {
    /* A message with attachments the engine said nothing about -- a document,
       or a picture it declined before it had a path to report. */
    const sent = 'read it\n\n[attachments, saved in the workspace]\n- uploads/notes.pdf'
    expect(splitAttachments(sent, NOTES).atts).toEqual(['uploads/notes.pdf'])
  })

  it('matches the engine path to the file it names, not to another', () => {
    const stored = 'two of them\n\n[attachments, saved in the workspace]\n- uploads/a.png\n- uploads/b.png\n\n'
      + '[Image: b.png (path: /w/uploads/b.png) | 2x2px]\n'
      + '[Image: a.png (path: /w/uploads/a.png) | 1x1px]'
    expect(splitAttachments(stored, NOTES).atts).toEqual(['/w/uploads/a.png', '/w/uploads/b.png'])
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

  it('reads back a message that carried a document, which the engine annotates too', () => {
    /* The engine writes a note per file, not per picture: one it cannot inline
       is named `[Attachment: ...]`, with the same `(path: ...)` in it. The
       reader knew only the picture's spelling, so those lines survived, the
       tail after the note was then neither blank nor a path, and the whole
       message -- heading, paths and annotations -- was drawn as the question. */
    const DASH = '\u2014'
    const stored = '[image] [image] hello\n\n[attachments, saved in the workspace]\n'
      + '- uploads/shot.png\n- uploads/slides.pptx\n- uploads/guide.html\n- uploads/card.jpg\n\n'
      + `[Image: shot.png (path: /w/uploads/shot.png) | 1312x732px, downscaled from 5312x2964 ${DASH} `
      + 're-read it with read_file if you need another look]\n'
      + `[Attachment: slides.pptx (path: /w/uploads/slides.pptx) ${DASH} use the understand_media tool to read its contents]\n`
      + `[Attachment: guide.html (path: /w/uploads/guide.html) ${DASH} use the understand_media tool to read its contents]\n`
      + `[Image: card.jpg (path: /w/uploads/card.jpg) | 787x1399px ${DASH} re-read it with read_file if you need another look]`
    expect(splitAttachments(stored, NOTES)).toEqual({
      body: 'hello',
      atts: ['/w/uploads/shot.png', '/w/uploads/slides.pptx', '/w/uploads/guide.html', '/w/uploads/card.jpg'],
    })
  })

  it('reads the other thing the engine says about a document', () => {
    /* Two shapes, and like the picture's five they share only the opening. */
    const DASH = '\u2014'
    const stored = `read it\n\n[Attachment: a.pdf (path: /w/a.pdf) ${DASH} could not be read: Permission denied]`
    expect(stripRuntimeNotes(stored)).toBe('read it')
  })

  it('reads back a message whose only words were the pictures themselves', () => {
    /* Sending a picture and typing nothing. The composer stores two text
       parts -- the flattened marker, then the note -- and `session.resume`
       joins them with a space, so the note arrives one space and one blank
       line after the marker rather than after a body.

       Taking the marker off then leaves the note at the very head of the
       string, and the reader anchored its search on the blank line that
       precedes a note, which no longer existed. It refused the message, and
       the page drew the heading and the path where the picture belonged. */
    const stored = '[image] \n\n[attachments, saved in the workspace]\n- uploads/shot.png\n\n'
      + '[Image: shot.png (path: /w/uploads/shot.png) | 1312x732px]'
    expect(splitAttachments(stored, NOTES)).toEqual({ body: '', atts: ['/w/uploads/shot.png'] })
  })

  it('reads a wordless message that carried several files', () => {
    const stored = '[image] [image] \n\n[attachments, saved in the workspace]\n'
      + '- uploads/a.png\n- uploads/notes.pdf\n\n'
      + '[Image: a.png (path: /w/uploads/a.png) | 1x1px]\n'
      + '[Attachment: notes.pdf (path: /w/uploads/notes.pdf)]'
    expect(splitAttachments(stored, NOTES)).toEqual({
      body: '',
      atts: ['/w/uploads/a.png', '/w/uploads/notes.pdf'],
    })
  })

  it('still lets a later note win over one at the head', () => {
    /* The head is the fallback, not the preference: a message that quotes an
       earlier note is read by its own, which is the last one. */
    const stored = '[attachments, saved in the workspace]\n- old.png\n\n'
      + 'and then\n\n[attachments, saved in the workspace]\n- new.png'
    expect(splitAttachments(stored, NOTES).atts).toEqual(['new.png'])
  })

  it('reads a note at the head even when the runtime annotated nothing', () => {
    /* The gate that decides whether there is anything to take off asked only
       the blank-line spelling while the loop under it knew two, so this shape
       was refused before the loop ever saw it. No resumed message reaches the
       page in it today -- the engine writes a `(path: ...)` line on every
       branch, so the gate always passed for another reason -- but the rule
       then lived in two places that knew different amounts, which is the very
       thing this file exists to argue against. */
    const bare = '[attachments, saved in the workspace]\n- uploads/shot.png'
    expect(splitAttachments(bare, NOTES)).toEqual({ body: '', atts: ['uploads/shot.png'] })
    /* The control: the same message one blank line down, which always worked. */
    expect(splitAttachments(`look\n\n${bare}`, NOTES)).toEqual({ body: 'look', atts: ['uploads/shot.png'] })
  })

  it('reads a message through the catalogue, in the language it was not sent in', () => {
    /* `readMessage` is the reader the page actually calls, and it takes the
       note's spellings from the catalogue rather than from a caller. The point
       of that list is the OTHER language -- a message sent under one is read
       back under whichever is in force -- and nothing held it: answering with
       the English spelling alone passed every case, because every case handed
       its own spellings in. */
    const spellings = I18N.ui['gui.att.note'] as Record<string, string>
    for (const [lang, note] of Object.entries(spellings)) {
      const sent = `[image] \n\n${note}\n- uploads/shot.png\n\n`
        + '[Image: shot.png (path: /w/uploads/shot.png) | 1x1px]'
      expect(readMessage(sent), lang).toEqual({ body: '', atts: ['/w/uploads/shot.png'] })
    }
    expect(Object.keys(spellings).length, 'the catalogue carries more than one spelling').toBeGreaterThan(1)
  })

  it('leaves alone the words a person wrote that only look like a marker', () => {
    /* `[image]` further down is a person typing, and a bracketed line that
       names no path is not one of the engine's. */
    const sent = 'the word [image] appears here\n\n[Image: not a real marker]'
    expect(splitAttachments(sent, NOTES).body).toBe('the word [image] appears here\n\n[Image: not a real marker]')
  })

  it('takes every marker a message with several pictures carries', () => {
    /* The runtime writes one text part per inlined picture and the resume joins
       them with spaces, so two pictures arrive as two markers. Taking the first
       left the second standing in front of the question. */
    const stored = '[image] [image] compare these two\n\n[attachments, saved in the workspace]\n'
      + '- uploads/a.png\n- uploads/b.png\n\n'
      + '[Image: a.png (path: /w/uploads/a.png) | 1x1px]\n[Image: b.png (path: /w/uploads/b.png) | 2x2px]'
    expect(splitAttachments(stored, NOTES)).toEqual({
      body: 'compare these two',
      atts: ['/w/uploads/a.png', '/w/uploads/b.png'],
    })
  })

  it('matches the two spellings of one path, so a windows message recovers too', () => {
    /* `fs.upload` answers with forward slashes whatever the host is, and the
       engine interpolates a path object, which prints backslashes on Windows.
       Compared literally the two never met, so the recovery never happened
       there and the picture fell back to its name. */
    const stored = '[image] look\n\n[attachments, saved in the workspace]\n- uploads/shot.png\n\n'
      + '[Image: shot.png (path: C:\\Users\\me\\.raven\\workspace\\uploads\\shot.png) | 4x4px]'
    expect(splitAttachments(stored, NOTES).atts).toEqual(['C:\\Users\\me\\.raven\\workspace\\uploads\\shot.png'])
  })

  it('leaves a message that carried nothing alone, marker-shaped or not', () => {
    /* The stripping is for a replay that carried pictures. A person who opens a
       sentence with the word in brackets meant to write it, and this helper
       promises such a message back unchanged. */
    expect(splitAttachments('[image] explain this token', NOTES))
      .toEqual({ body: '[image] explain this token', atts: [] })
    expect(splitAttachments('what does [image] mean here', NOTES).body).toBe('what does [image] mean here')
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
