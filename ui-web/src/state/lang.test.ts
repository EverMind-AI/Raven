// @vitest-environment happy-dom
/* The language store, against the two states a page is really in.
 *
 * The one that is easy to forget is the first: nothing has applied a language
 * yet. src/page.html declares lang="zh-CN" and the regions src/App.tsx renders
 * carry the literals that markup was served with, and neither load mode
 * translates them on its own -- the fixture config has no `language` key and a
 * page with no gateway never gets that far -- so the page a reader opens sits in
 * that state indefinitely. Every case below that names `applied == null` is
 * pinning what such a page shows.
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

  /* What the five passes applyI18n made over the document became: a value the
     region renders beside the key. `text` answers the ones the markup carried a
     literal for and `attr` the ones it did not, which is every aria-label,
     title and data-tip on the page -- so one is the literal until a pick lands
     and the other is nothing at all. Which element gets which is
     src/App.test.tsx's and each region's own test's. */
  it('answers attr() with nothing until a language lands, then the catalogue', async () => {
    const lang = await fresh()
    expect(lang.attr('gui.collapse_rail')).toBe(undefined)
    lang.set('en')
    expect(lang.attr('gui.collapse_rail')).toBe('Collapse sidebar')
    lang.set('zh')
    expect(lang.attr('gui.collapse_rail')).not.toBe('Collapse sidebar')
  })

  /* Nothing walks the document any more: the keys on the markup are inert
     markers, and an element that is not rendered by a region cannot be
     translated by a pick. */
  it('leaves a keyed element in the document untouched', async () => {
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
    expect(at('[data-i18n]').textContent).toBe('literal')
    expect((at('[data-i18n-ph]') as HTMLInputElement).placeholder).toBe('')
    expect(at('[data-i18n-title]').title).toBe('')
    expect(at('[data-i18n-aria]').getAttribute('aria-label')).toBe(null)
    expect(at('[data-i18n-tip]').dataset.tip).toBe(undefined)
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

  /* The boot restore's setter. `restore` applies the remembered language ahead
     of the first data-driven paint and repaints nothing DRAWN, which is why the
     whole-page redraw must not run for it. */
  it('hears nothing from the quiet setter, which still applies the language', async () => {
    const lang = await fresh()
    let calls = 0
    lang.onApplied(() => { calls += 1 })
    lang.setQuiet('zh')
    expect(calls).toBe(0)
    expect(lang.get()).toBe('zh')
    expect(document.documentElement.lang).toBe('zh-CN')
    expect(lang.text('gui.new_task', 'the markup literal')).toBe(ZH_NEW_TASK)
  })

  /* The markup is the other half, and the quiet setter moves it: applyI18n ran
     on every call site including this one, so a page that boots with a
     remembered language has to be in it before the first frame. */
  it('still tells the regions to draw again from the quiet setter', async () => {
    const lang = await fresh()
    const seen: string[] = []
    lang.subscribe(() => { seen.push('render') })
    lang.onApplied(() => { seen.push('redraw') })
    lang.setQuiet('zh')
    expect(seen).toEqual(['render'])
    lang.set('en')
    expect(seen).toEqual(['render', 'render', 'redraw'])
  })
})
