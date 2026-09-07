/* The instance composer's send carries its attachments as `media`, the way the
 * page composer's does.
 *
 * `turn.send` forwards a direct chat's files to the sub-agent from `req.media`
 * and nowhere else; the shipped caller sent `session_key`, `content` and
 * `target` only, so every ordinary direct chat reached the server with no
 * files whatever the reader attached. The rule that turns the note in the
 * text into the typed field already existed for the page composer (`mediaOf`);
 * this pins that the instance send goes through it, against the assembled
 * live layer rather than a copy of it.
 */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const build = readFileSync(new URL('../build.py', import.meta.url), 'utf8')
const manifest = build.match(/_LIVE_PARTS = \[(.*?)\n\]/s)
if (!manifest) throw new Error('_LIVE_PARTS is absent from build.py')
const parts = [...manifest[1].matchAll(/"([^"]+\.js)"/g)].map((m) => m[1])
const live = parts
  .map((name) => readFileSync(new URL(`../src/live/${name}`, import.meta.url), 'utf8'))
  .join('')
const demo = readFileSync(new URL('../src/demo/060-conversation.js', import.meta.url), 'utf8')

/* From `mark` to the close of the brace it opens, plus the character after it. */
function braced(source, mark, where) {
  const start = source.indexOf(mark)
  if (start < 0) throw new Error(`${mark} is absent from ${where}`)
  const brace = source.indexOf('{', start)
  let depth = 0
  for (let index = brace; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1
    else if (source[index] === '}') {
      depth -= 1
      if (depth === 0) return source.slice(start, index + 2)
    }
  }
  throw new Error(`${mark} has no closing brace in ${where}`)
}

/* The `instanceSend` member, to the end of its `.then(...)` chain. */
function instanceSendMember() {
  const mark = 'instanceSend: (agent, handle, text) =>'
  const start = live.indexOf(mark)
  if (start < 0) throw new Error('instanceSend is absent from the assembled live layer')
  const end = live.indexOf('.then(() => undefined),', start)
  if (end < 0) throw new Error('instanceSend does not end in the then-chain the seam expects')
  return live.slice(start + mark.length, end + '.then(() => undefined)'.length)
}

describe('the assembled live instance send', () => {
  const note = '[attachments, saved in the workspace]'
  function install(calls) {
    const rpc = { call: (method, params) => { calls.push([method, params]); return Promise.resolve({}) } }
    const I18N = { ui: { 'gui.att.note': { en: note, zh: '[附件，已存放在工作目录下]' } } }
    const factory = Function(
      'rpc', 'I18N', 'sessionCurrent',
      `${braced(demo, 'function splitAtts(text) {', 'demo/060-conversation.js')}\n` +
      `${braced(live, 'const mediaOf = (text) => {', 'the assembled live layer')}\n` +
      `return (agent, handle, text) => ${instanceSendMember()};`,
    )
    return factory(rpc, I18N, () => 's1')
  }

  it('turns the attachment note in the text into the typed media field', async () => {
    const calls = []
    const send = install(calls)

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
    const send = install(calls)

    await send('hermes', 'chatty', `\n\n${note}\n- uploads/a.txt`)

    expect(calls[0][1].media).toEqual(['uploads/a.txt'])
    expect(calls[0][1].content).toBe(`\n\n${note}\n- uploads/a.txt`)
  })

  it('sends a plain line with no media key at all', async () => {
    const calls = []
    const send = install(calls)

    await send('hermes', 'chatty', '再补一句它的传输层')

    expect(calls).toEqual([[
      'turn.send',
      { session_key: 's1', content: '再补一句它的传输层', target: { agent: 'hermes', handle: 'chatty' } },
    ]])
  })
})
