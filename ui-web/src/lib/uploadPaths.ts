/* Where an upload's bytes actually are, by the path its reader sees.

 * `fs.upload` answers with two spellings of one file: `path`, relative to agent
 * home, which is what the composer writes into the message's note (the bubble
 * renders its chips from it, and `/file` re-roots it), and `abs_path`, the file
 * itself. The turn is handed the absolute one, because a relative
 * `uploads/<name>` is resolved by `viewer_root`, which reads the session's own
 * root before agent home -- so a same-named file appearing under the session
 * root after the upload could answer for it. Page-lifetime and keyed by the
 * upload path, like lib/attachmentCache: a note restored after a reload falls
 * back to the relative name, which is the resolution rule it was written for.
 *
 * Not lib/upload.ts, which is about refusing an upload; and not
 * attachmentCache, which holds the bytes a page draws from.
 */
const files = new Map<string, string>()

/** Remember that the upload answered at ``path`` wrote ``absPath``. */
export const remember = (path: string, absPath: string): void => {
  if (path && absPath) files.set(path, absPath)
}

/** The file to read for a note path: what an upload wrote there, or the path itself. */
export const mediaPath = (path: string): string => files.get(path) ?? path

export const _resetForTests = (): void => {
  files.clear()
}
