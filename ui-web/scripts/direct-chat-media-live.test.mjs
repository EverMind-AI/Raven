// @vitest-environment happy-dom
/* The instance composer's send carries its attachments as `media`, the way the
 * page composer's does.
 *
 * `turn.send` forwards a direct chat's files to the sub-agent from `req.media`
 * and nowhere else; the shipped caller sent `session_key`, `content` and
 * `target` only, so every ordinary direct chat reached the server with no
 * files whatever the reader attached. The rule that turns the note in the
 * text into the typed field already existed for the page composer (`mediaOf`);
 * this pins that the instance send goes through it, against the part the page
 * installs rather than a copy of it.
 */

import { describe, expect, it } from 'vitest'

import { fakeGateway, loadPart } from './legacy-part.mjs'

/* `sources.agents.instanceSend` as the part installs it, with the real `mediaOf`
   and the real note text behind it -- the note is what splits the message. */
async function sender(calls) {
  const part = await loadPart(() => import('../src/legacy/live/230-tabs.js'), {
    fakes: { 'src/shell/session': { current: () => 's1' } },
  })
  await fakeGateway((method, params) => { calls.push([method, params]); return Promise.resolve({}) })
  const { sources } = await import('../src/state/sources')
  part.install()
  if (!sources.agents?.instanceSend) throw new Error('instanceSend is absent from the live layer')
  return sources.agents.instanceSend
}

const { I18N } = await import('../src/legacy/demo/010-kernel.js')
const note = I18N.ui['gui.att.note'].en

describe('the live instance send', () => {
  it('turns the attachment note in the text into the typed media field', async () => {
    const calls = []
    const send = await sender(calls)

    await send('hermes', 'chatty', `have a look\n\n${note}\n- uploads/a.txt\n- uploads/b.pdf`)

    expect(calls).toEqual([[
      'turn.send',
      {
        session_key: 's1',
        content: `have a look\n\n${note}\n- uploads/a.txt\n- uploads/b.pdf`,
        target: { agent: 'hermes', handle: 'chatty' },
        media: ['uploads/a.txt', 'uploads/b.pdf'],
      },
    ]])
  })

  it('carries a file-only message, the note alone after its blank line', async () => {
    const calls = []
    const send = await sender(calls)

    await send('hermes', 'chatty', `\n\n${note}\n- uploads/a.txt`)

    expect(calls[0][1].media).toEqual(['uploads/a.txt'])
    expect(calls[0][1].content).toBe(`\n\n${note}\n- uploads/a.txt`)
  })

  it('sends a plain line with no media key at all', async () => {
    const calls = []
    const send = await sender(calls)

    await send('hermes', 'chatty', '再补一句它的传输层')

    expect(calls).toEqual([[
      'turn.send',
      { session_key: 's1', content: '再补一句它的传输层', target: { agent: 'hermes', handle: 'chatty' } },
    ]])
  })
})
