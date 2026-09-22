/* What a sent message says about the files that rode with it, read back out.
 *
 * The composer folds an attachment note into the text it sends -- the note's
 * heading, then one `- path` line per file -- because that note is the record
 * of what was handed over: the reader's bubble renders its chips from it, the
 * send recovers the paths from it, and it is what survives into session
 * history. Reading it back is therefore one grammar with two readers, and it
 * lived twice: once for the live bubble and once for the send. Two copies is
 * how they came to disagree.
 *
 * What made them disagree is the runtime's own annotations. A message that
 * carried files comes back from `session.resume` with more in it than the
 * composer sent: the engine appends a line per file saying what it made of it
 * (`[Image: shot.png (path: /...) | 1312x732px, downscaled from 5312x2964 --
 * re-read it with read_file if you need another look]`, or for one it cannot
 * inline `[Attachment: slides.pptx (path: /...) -- use the understand_media
 * tool to read its contents]`), and a flattened image part leaves `[image]` at
 * the front. All of it is written for the model. None of it is the person's
 * words, and the old reader -- which required every line after the note to be
 * blank or a `- ` path -- refused the whole message because of them, so a
 * reloaded page drew the note, the paths and the engine's lines as the
 * question itself.
 *
 * So the grammar is: strip what the runtime wrote, then read the note. The
 * markers are matched by their own shape rather than by position, because the
 * engine writes seven of them -- five about a picture (unreadable, too large,
 * budget, failed, shown) and two about anything else (named, unreadable) --
 * and they share only the opening.
 *
 * The note's own heading is passed in rather than read here, in every language
 * the catalogue spells it: a message sent under one language and reopened under
 * another still has to be read.
 */

/* A line the engine wrote about a file the message carried, which the person
   did not type. It writes one per file and not one per picture: a file it can
   inline is reported as `[Image: ...]` in one of five shapes, and one it
   cannot -- a document, or a file that went unreadable between the resolve and
   the read -- as `[Attachment: ...]` in one of two. All seven share only the
   opening, the path they name and the closing bracket, so those are what this
   matches; what sits between differs (a size, or one of six refusals), and
   several end the sentence with an em dash the person's keyboard may well not.
   Reading only the picture's spelling is what made a message that carried a
   document come back as its own transcript: the document's lines survived the
   strip, the tail after the note was then neither blank nor a path, and the
   reader handed the whole thing back as the question. */
const FILE_NOTE = /^\[(?:Image|Attachment): .*\(path: .+?\).*\]$/

/* The flattening of the image parts, left at the head of a multimodal message.
   One per picture: the runtime writes a text part per inlined image and the
   resume joins them with spaces, so a message that carried two reaches this
   reader as `[image] [image] ...`. The whole run goes, not the first of it. */
const IMAGE_PART = /^(?:\[image\]\s*)+/

/* The absolute path each of those lines names, which is the one thing in them
   worth keeping. The note lists what the composer uploaded -- a path relative
   to the workspace it uploaded into -- and the file route resolves a relative
   path against the SESSION's own root, which is not always that workspace: a
   conversation whose root is elsewhere asked for `uploads/x.png` under its own
   root and got a 404, so the file fell back to its name on every reload. The
   engine's line carries the absolute path of the same file, and that one
   resolves wherever the session is rooted -- for a document as much as for a
   picture, since the chip is a link to it. */
const FILE_PATH = /^\[(?:Image|Attachment): .*?\(path: (.+?)\).*\]$/

import { I18N } from '../i18n/t'

/* Where a note begins in `text`, and where its paths begin after it.
   A note sits after a blank line, or -- when the person typed nothing and sent
   only files -- at the very head. The composer stores the flattened marker and
   the note as two text parts, `session.resume` joins them with a space, and
   taking the marker off takes that blank line with it, so a wordless message
   arrives with the note first.

   The blank-line spelling is searched first and from the end, so a message
   that quotes an earlier note is still read by its own; the head is the
   fallback, never the preference.

   One function because there are two readers -- the gate that decides whether
   there is anything to take off, and the loop that decides what -- and asking
   one question in two places is how they come to know different amounts. They
   already had: the loop learned the head and the gate did not, so a note at
   the head of a message the runtime had not annotated was refused by the gate
   before the loop ever saw it. */
function noteSpan(text: string, note: string): { at: number; from: number } | null {
  const lead = `\n\n${note}\n`
  const at = text.lastIndexOf(lead)
  if (at >= 0) return { at, from: at + lead.length }
  if (text.startsWith(`${note}\n`)) return { at: 0, from: note.length + 1 }
  return null
}

export interface Attachments {
  /** The person's own words. */
  body: string
  /** The paths the note listed, in the order it listed them. */
  atts: string[]
}

/* Every spelling of the note's heading the catalogue carries, which is what
   the reader has to be handed: a message sent under one language is read back
   under whichever is in force. It lives here, beside the grammar that consumes
   it, because it was written out twice at two call sites -- and two copies of
   one grammar is how the bubble and the send came to disagree in the first
   place, which is the whole argument of this file. */
export const noteWords = (): string[] =>
  Object.values((I18N.ui['gui.att.note'] ?? {}) as Record<string, string>).filter(Boolean)

/** A sent message read back: the person's words and the files, in one call. */
export function readMessage(text: string): Attachments {
  return splitAttachments(text, noteWords())
}

/** The absolute path of every file the engine named, in the order named. */
export function runtimePaths(text: string): string[] {
  const out: string[] = []
  for (const line of String(text).split('\n')) {
    const hit = FILE_PATH.exec(line.trim())
    if (hit && hit[1]) out.push(hit[1])
  }
  return out
}

/** Drop the lines the runtime added, leaving the message as it was sent. */
export function stripRuntimeNotes(text: string): string {
  const kept: string[] = []
  for (const line of String(text).split('\n')) {
    if (FILE_NOTE.test(line.trim())) continue
    kept.push(line)
  }
  /* Only at the head: `[image]` is a word a person may well type further down,
     and the flattening only ever writes it in front of the message. */
  return kept.join('\n').replace(IMAGE_PART, '').replace(/\n+$/, '')
}

/* The note and its paths, or the whole text when there is no note.
 *
 * `notes` is every spelling of the note's heading. The last one in the text
 * wins, so a message quoting an earlier note is read by its own.
 */
export function splitAttachments(text: string, notes: readonly string[]): Attachments {
  const raw = String(text)
  const absolute = runtimePaths(raw)
  const carried = notes.some((note) => !!note && noteSpan(raw, note) !== null)
  /* Nothing the runtime wrote, so nothing to take off. Without this the reader
     rewrote a message that carried no files at all: a person who begins a
     sentence with the word in brackets meant to write it, and this helper
     promises such a message back unchanged. */
  if (!absolute.length && !carried) return { body: raw, atts: [] }
  /* The same file, named twice: the note's path as the composer uploaded it,
     and the engine's as it stands on disk. The second is preferred wherever
     both exist, because it resolves whatever the session is rooted at.
     Compared with one separator, because the two spellings differ on Windows:
     `fs.upload` answers `uploads/shot.png` and the engine interpolates a path
     object, which prints `C:\\...\\uploads\\shot.png` there, so a literal
     comparison never matched and the recovery this is for never happened. */
  const slashed = (v: string): string => v.replace(/\\/g, '/')
  const resolve = (att: string): string => {
    const want = slashed(att)
    return absolute.find((abs) => slashed(abs) === want || slashed(abs).endsWith(`/${want}`)) ?? att
  }
  const s = stripRuntimeNotes(raw)
  for (const note of notes) {
    if (!note) continue
    const span = noteSpan(s, note)
    if (!span) continue
    const tail = s.slice(span.from).split('\n')
    if (!tail.length || !tail.every((l) => !l.trim() || /^- /.test(l))) continue
    return {
      body: s.slice(0, span.at),
      atts: tail.filter((l) => /^- /.test(l)).map((l) => resolve(l.slice(2).trim())),
    }
  }
  return { body: s, atts: [] }
}
