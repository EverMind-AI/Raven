/* The file an upload's relative path names, kept for the send to name
   absolutely: the note carries `uploads/<name>` (the bubble renders it and
   `/file` re-roots it), while the turn is handed the file `fs.upload` wrote. */
import { afterEach, describe, expect, it } from 'vitest'

import { _resetForTests, mediaPath, remember } from './uploadPaths'

afterEach(() => { _resetForTests() })

describe('the file behind an upload path', () => {
  it('answers a remembered path as the file the upload wrote', () => {
    remember('uploads/shot.png', '/home/me/.raven/workspace/uploads/shot.png')
    expect(mediaPath('uploads/shot.png')).toBe('/home/me/.raven/workspace/uploads/shot.png')
  })

  it('leaves a path no upload minted alone', () => {
    /* A file dragged out of the transcript or the panel was never uploaded:
       its path is what the resolver reads, relative to the session's own
       root, exactly as the note meant it. */
    expect(mediaPath('uploads/dragged.png')).toBe('uploads/dragged.png')
    expect(mediaPath('/work/out/chart.png')).toBe('/work/out/chart.png')
  })
})
