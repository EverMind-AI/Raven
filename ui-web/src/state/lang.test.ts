// @vitest-environment happy-dom
/* The language store, against the two states a page is really in.
 *
 * The one that is easy to forget is the first: nothing has applied a language
 * yet. src/page.html declares lang="zh-CN" and carries Chinese literals, and
 * neither load mode translates them on its own -- the fixture config has no
 * `language` key and a page with no gateway never gets that far -- so the page
 * a reader opens sits in that state indefinitely. Every case below that names
 * `applied == null` is pinning what such a page shows.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import catalog from '../../../i18n/messages.json'

/* The zh copy read from the catalogue rather than written out here: the repo's
   source is English, and a value quoted in a test would be a second copy of
   the one the catalogue owns. */
const ZH_NEW_TASK = catalog.ui['gui.new_task'].zh

/* Fresh module state per case. `applied` only moves forwards, so a case that
   applied a language must not be visible to one asserting that none has. */
async function fresh(): Promise<typeof import('./lang')> {
  vi.resetModules()
  return import('./lang')
}

beforeEach(() => {
  /* What the served page declares (src/page.html:2), which is what the three
     readers below see today before any pick lands. */
  document.documentElement.lang = 'zh-CN'
  document.body.innerHTML = ''
})

describe('before any language is applied', () => {
  it('has no applied language', async () => {
    const lang = await fresh()
    expect(lang.get()).toBeNull()
  })

  it('answers text() with the literal the markup carries', async () => {
    const lang = await fresh()
    expect(lang.text('gui.new_task', 'the markup literal')).toBe('the markup literal')
    /* Including for a key the catalogue does carry: the point is that the page
       has not been translated, not that the catalogue is missing something. */
    expect(lang.text('gui.new_task', 'x')).not.toBe('New task')
  })

  it('writes no attribute on <html>, on load or on a text() call', async () => {
    const seen: Array<string | null> = []
    const observer = new MutationObserver((records) => {
      for (const record of records) seen.push(record.attributeName)
    })
    observer.observe(document.documentElement, { attributes: true })
    const lang = await fresh()
    lang.text('gui.new_task', 'the markup literal')
    const pending = observer.takeRecords().map((record) => record.attributeName)
    observer.disconnect()
    expect([...seen, ...pending]).toEqual([])
    expect(document.documentElement.lang).toBe('zh-CN')
  })

  /* What shell/platform.language(), MemoryPage's memWhen and the settings
     dialog's language radio read. All three used to read the attribute
     directly, so the store has to answer what the attribute says -- the served
     declaration on a page, and nothing at all in a document that declares
     nothing, which is every other test's document. */
  it('answers tag() with the document declaration', async () => {
    const lang = await fresh()
    expect(lang.tag()).toBe('zh-CN')
    document.documentElement.lang = 'en'
    expect(lang.tag()).toBe('en')
    document.documentElement.removeAttribute('lang')
    expect(lang.tag()).toBe('')
  })
})

describe('applying a language', () => {
  it('writes the <html> declaration for each', async () => {
    const lang = await fresh()
    lang.set('zh')
    expect(document.documentElement.lang).toBe('zh-CN')
    expect(lang.get()).toBe('zh')
    lang.set('en')
    expect(document.documentElement.lang).toBe('en')
    expect(lang.get()).toBe('en')
  })

  it('moves text() onto the catalogue', async () => {
    const lang = await fresh()
    lang.set('en')
    expect(lang.text('gui.new_task', 'the markup literal')).toBe('New task')
    lang.set('zh')
    expect(lang.text('gui.new_task', 'the markup literal')).toBe(ZH_NEW_TASK)
  })

  /* The five passes applyI18n made over the static markup, in one case each:
     text, placeholder, the two attributes that are read out, and the hover
     pill. page.html is still where that markup comes from, so this is what
     keeps the served page translating on a flip. */
  it('fills every kind of i18n attribute in the markup', async () => {
    document.body.innerHTML = [
      '<span data-i18n="gui.new_task">literal</span>',
      '<input data-i18n-ph="gui.search_sessions">',
      '<button data-i18n-title="gui.collapse_rail"></button>',
      '<button data-i18n-aria="gui.collapse_rail"></button>',
      '<button data-i18n-tip="gui.collapse_rail"></button>',
    ].join('')
    const lang = await fresh()
    lang.set('en')
    const at = (selector: string): HTMLElement => document.querySelector(selector) as HTMLElement
    expect(at('[data-i18n]').textContent).toBe('New task')
    expect((at('[data-i18n-ph]') as HTMLInputElement).placeholder).toBe('Search sessions')
    expect(at('[data-i18n-title]').title).toBe('Collapse sidebar')
    expect(at('[data-i18n-aria]').getAttribute('aria-label')).toBe('Collapse sidebar')
    expect(at('[data-i18n-tip]').dataset.tip).toBe('Collapse sidebar')
  })
})

describe('the subscribers', () => {
  it('hears one notification per applied pick', async () => {
    const lang = await fresh()
    let calls = 0
    const stop = lang.subscribe(() => { calls += 1 })
    lang.set('zh')
    expect(calls).toBe(1)
    lang.set('en')
    expect(calls).toBe(2)
    stop()
    lang.set('zh')
    expect(calls).toBe(2)
  })

  /* The boot restore's setter. `langRestore` applies the remembered language
     ahead of the first data-driven paint and repaints nothing, which is why
     the whole-page redraw subscribed to `set` must not run for it. */
  it('hears nothing from the quiet setter, which still applies the language', async () => {
    const lang = await fresh()
    let calls = 0
    lang.subscribe(() => { calls += 1 })
    lang.setQuiet('zh')
    expect(calls).toBe(0)
    expect(lang.get()).toBe('zh')
    expect(document.documentElement.lang).toBe('zh-CN')
    expect(lang.text('gui.new_task', 'the markup literal')).toBe(ZH_NEW_TASK)
  })
})
