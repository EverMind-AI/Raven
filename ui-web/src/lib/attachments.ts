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
 * carried a picture comes back from `session.resume` with more in it than the
 * composer sent: the engine appends a line per image saying what it could see
 * (`[Image: shot.png (path: /...) | 1312x732px, downscaled from 5312x2964 --
 * re-read it with read_file if you need another look]`), and a flattened image
 * part leaves `[image]` at the front. Both are written for the model. Neither
 * is the person's words, and the old reader -- which required every line after
 * the note to be blank or a `- ` path -- refused the whole message because of
 * them, so a reloaded page drew the note, the paths and the engine's line as
 * the question itself.
 *
 * So the grammar is: strip what the runtime wrote, then read the note. The
 * markers are matched by their own shape rather than by position, because the
 * engine writes five of them (unreadable, too large, budget, failed, shown)
 * and they share only the opening.
 *
 * The note's own heading is passed in rather than read here, in every language
 * the catalogue spells it: a message sent under one language and reopened under
 * another still has to be read.
 */

/* A line the engine wrote about a picture, which the person did not type.
   Matched on the three parts every one of the five shares -- the opening, the
   path it names, the closing bracket -- rather than on what comes between
   them: they differ there (a size, or one of four refusals), and one of them
   ends the sentence with an em dash the person's own keyboard may well not. */
const IMAGE_NOTE = /^\[Image: .*\(path: .+?\).*\]$/

/** The flattening of an image part, left at the head of a multimodal message. */
const IMAGE_PART = /^\[image\]\s*/

/* The absolute path each of those lines names, which is the one thing in them
   worth keeping. The note lists what the composer uploaded -- a path relative
   to the workspace it uploaded into -- and the file route resolves a relative
   path against the SESSION's own root, which is not always that workspace: a
   conversation whose root is elsewhere asked for `uploads/x.png` under its own
   root and got a 404, so the picture fell back to its file name on every
   reload. The engine's line carries the absolute path of the same file, and
   that one resolves wherever the session is rooted. */
const IMAGE_PATH = /^\[Image: .*?\(path: (.+?)\).*\]$/

export interface Attachments {
  /** The person's own words. */
  body: string
  /** The paths the note listed, in the order it listed them. */
  atts: string[]
}

/** The absolute path of every picture the engine named, in the order named. */
export function runtimePaths(text: string): string[] {
  const out: string[] = []
  for (const line of String(text).split('\n')) {
    const hit = IMAGE_PATH.exec(line.trim())
    if (hit && hit[1]) out.push(hit[1])
  }
  return out
}

/** Drop the lines the runtime added, leaving the message as it was sent. */
export function stripRuntimeNotes(text: string): string {
  const kept: string[] = []
  for (const line of String(text).split('\n')) {
    if (IMAGE_NOTE.test(line.trim())) continue
    kept.push(line)
  }
  /* Only at the head, and only once: `[image]` is a word a person may well
     type further down, and the flattening writes exactly one. */
  return kept.join('\n').replace(IMAGE_PART, '').replace(/\n+$/, '')
}

/* The note and its paths, or the whole text when there is no note.
 *
 * `notes` is every spelling of the note's heading. The last one in the text
 * wins, so a message quoting an earlier note is read by its own.
 */
export function splitAttachments(text: string, notes: readonly string[]): Attachments {
  const absolute = runtimePaths(text)
  /* The same file, named twice: the note's path as the composer uploaded it,
     and the engine's as it stands on disk. The second is preferred wherever
     both exist, because it resolves whatever the session is rooted at. */
  const resolve = (att: string): string =>
    absolute.find((abs) => abs === att || abs.endsWith(`/${att}`)) ?? att
  const s = stripRuntimeNotes(text)
  for (const note of notes) {
    if (!note) continue
    const at = s.lastIndexOf(`\n\n${note}\n`)
    if (at < 0) continue
    const tail = s.slice(at + note.length + 3).split('\n')
    if (!tail.length || !tail.every((l) => !l.trim() || /^- /.test(l))) continue
    return {
      body: s.slice(0, at),
      atts: tail.filter((l) => /^- /.test(l)).map((l) => resolve(l.slice(2).trim())),
    }
  }
  return { body: s, atts: [] }
}
