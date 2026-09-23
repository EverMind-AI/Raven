// @vitest-environment happy-dom
/* The boot read that puts the page in the gateway's language.
 *
 * `load` is the third and last step of the resolution state/lang/store.ts
 * documents -- a remembered pick, else what <html lang> declares, else this --
 * and it is the only one of the three that can move a reader with no
 * remembered pick off the zh-CN the served markup carries. So what it is held
 * to here is the offline library, which is the wire `?stub=1` and a page opened
 * from disk run on: a step the offline page cannot take is a language it can
 * never leave.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

/* The module graph rebuilt per case: the store resolves the language as it
   loads, and `load` reaches for the transport through a module of its own, so
   a gateway installed into a stale copy would answer nobody. */
async function offlinePage(): Promise<{
  lang: typeof import('./store')
  pick: typeof import('./pick')
}> {
  vi.resetModules()
  const { setGateway } = await import('../../rpc/gateway')
  const { FixtureTransport } = await import('../../rpc/fixtureTransport')
  const { demoFixtures } = await import('../../rpc/fixtures')
  setGateway(new FixtureTransport(demoFixtures))
  return { lang: await import('./store'), pick: await import('./pick') }
}

beforeEach(() => {
  /* A reader nobody has picked for, on the page as it is served
     (src/page.html:2), whose browser names neither language -- so the store
     resolves the declaration and the gateway's answer is demonstrably the thing
     that moves it. With happy-dom's own en-US the page would already be in
     English before `load` was called, and the case below would pass without
     reading the wire at all. */
  document.documentElement.lang = 'zh-CN'
  localStorage.clear()
  Object.defineProperty(navigator, 'languages', { value: ['fr-FR'], configurable: true })
})

describe('the language a boot reads', () => {
  it('moves the offline page to the language a fresh gateway reports', async () => {
    const { lang, pick } = await offlinePage()
    expect(lang.get().lang).toBe('zh')
    await pick.load()
    expect(lang.get().lang).toBe('en')
  })

  /* The answer the step above rests on. `config.get` fills a default of its own
     for every key the on-disk config omits -- raven/rpc/methods/config.py's
     _DEFAULTS, where `language` is "en" -- so null is an answer no gateway
     sends, and an offline library that sends one puts the page on a branch the
     live page has no way to reach. */
  it('is one a gateway could have sent, an install that never picked included', async () => {
    vi.resetModules()
    const { FixtureTransport } = await import('../../rpc/fixtureTransport')
    const { demoFixtures } = await import('../../rpc/fixtures')
    const answer = await new FixtureTransport(demoFixtures).call('config.get', { keys: ['language'] })
    expect(answer.config.language).toBe('en')
  })
})
