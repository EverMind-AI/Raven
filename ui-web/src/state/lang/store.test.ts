// @vitest-environment happy-dom
/* The language store, against the three states a page is really in.
 *
 * The one that decides the first frame is where the language comes FROM. A
 * reader who has picked one is remembered in localStorage and the page is in it
 * before anything renders; a page nobody has picked for is in the language its
 * own markup declares (src/page.html says lang="zh-CN"), because that is the
 * language the reader is looking at. Neither load mode has asked the gateway by
 * then: the store resolves as it loads, and what the gateway says arrives later
 * and applies as any pick does (state/lang/pick.ts's `load`, pinned next door
 * in pick.test.ts).
 *
 * What still waits for a pick is one thing: an attribute the served markup does
 * not carry at all. Every case below that names `attr` is pinning that.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import catalog from '../../../../i18n/messages.json'

/* The zh copy read from the catalogue rather than written out here: the repo's
   source is English, and a value quoted in a test would be a second copy of
   the one the catalogue owns. */
const ZH_NEW_TASK = catalog.ui['gui.new_task'].zh

/* Fresh module state per case: the language is resolved as the module loads, so
   a case that changes the document or what is remembered has to load it again. */
async function fresh(): Promise<typeof import('./store')> {
  vi.resetModules()
  return import('./store')
}

/** The catalogue's own lookup, in whichever column the store has selected. */
async function words(): Promise<typeof import('../../i18n/t')['t']> {
  return (await import('../../i18n/t')).t
}

/* What the reader's browser asks for, which the resolution reads between a
   remembered pick and the document's own declaration. Said out loud in every
   case that turns on it: happy-dom answers en-US, so a case leaving it alone
   would be decided by the environment rather than by what it means to pin. */
function browserSays(...tags: string[]): void {
  Object.defineProperty(navigator, 'languages', { value: tags, configurable: true })
}

beforeEach(() => {
  /* What the served page declares (src/page.html:2). */
  document.documentElement.lang = 'zh-CN'
  document.body.innerHTML = ''
  localStorage.clear()
  /* A browser that names neither language, so the cases below that are about
     the other two steps are not quietly decided by this one. */
  browserSays('fr-FR')
})

describe('the language the page resolves', () => {
  it('is the one the document declares when nobody has picked and the browser names neither', async () => {
    const lang = await fresh()
    expect(lang.get().lang).toBe('zh')
    expect(await (await words())('gui.new_task')).toBe(ZH_NEW_TASK)
  })

  /* The step this exists for: a page that never reaches the gateway -- a failed
     connect returns before `load` runs (src/app/boot.ts) -- has the reader's own
     languages and nothing else, and the sign-in notice is the one message whose
     whole job is to be read on that page. */
  it('is the one the reader\'s browser asks for when nobody has picked', async () => {
    browserSays('en-US')
    const lang = await fresh()
    expect(lang.get().lang).toBe('en')
    expect((await words())('gui.auth.stale')).toBe(catalog.ui['gui.auth.stale'].en)
  })

  it('takes the first of the reader\'s languages it has a catalogue for', async () => {
    browserSays('fr-FR', 'en-GB', 'zh-CN')
    const lang = await fresh()
    expect(lang.get().lang).toBe('en')
  })

  it('reads the one language a browser with no list names', async () => {
    Object.defineProperty(navigator, 'languages', { value: undefined, configurable: true })
    Object.defineProperty(navigator, 'language', { value: 'en-US', configurable: true })
    const lang = await fresh()
    expect(lang.get().lang).toBe('en')
  })

  it('lets a remembered pick beat the browser', async () => {
    browserSays('en-US')
    localStorage.setItem('raven.gui.lang', 'zh')
    const lang = await fresh()
    expect(lang.get().lang).toBe('zh')
  })

  /* The browser stating a preference is not a reader picking one, and `picked`
     is what puts an aria-label, a title or a data-tip on a page whose markup
     carries none -- which is what both boot goldens record. */
  it('does not count the browser\'s preference as a pick', async () => {
    browserSays('en-US')
    const lang = await fresh()
    expect(lang.get().picked).toBe(false)
    expect(lang.attr('gui.collapse_rail')).toBe(undefined)
  })

  it('is the remembered pick when there is one, whatever the document says', async () => {
    localStorage.setItem('raven.gui.lang', 'en')
    const lang = await fresh()
    expect(lang.get().lang).toBe('en')
    expect((await words())('gui.new_task')).toBe('New task')
  })

  /* A remembered pick is the one case that moves the declaration, and it moves
     it before the first frame: the page is served zh-CN and the reader asked
     for English on it last time. */
  it('writes the declaration for a language the document does not already declare', async () => {
    await fresh()
    expect(document.documentElement.lang).toBe('zh-CN')
    localStorage.setItem('raven.gui.lang', 'en')
    await fresh()
    expect(document.documentElement.lang).toBe('en')
  })

  /* Same rule, reached the other way: a reader whose browser puts the page in
     English is looking at English, so the document must not go on declaring
     Chinese -- that tag is what a screen reader picks a voice from. */
  it('writes it for a language the browser resolved too', async () => {
    browserSays('en-US')
    await fresh()
    expect(document.documentElement.lang).toBe('en')
  })

  it('ignores a remembered value that is not one of the two languages', async () => {
    localStorage.setItem('raven.gui.lang', 'fr')
    const lang = await fresh()
    expect(lang.get().lang).toBe('zh')
  })

  /* What lib/platform.language(), MemoryPage's memWhen and the settings
     dialog's language radio read: the resolved language as a declaration. */
  it('answers tag() for the language it resolved', async () => {
    const lang = await fresh()
    expect(lang.tag()).toBe('zh-CN')
    document.documentElement.lang = 'en'
    const english = await fresh()
    expect(english.tag()).toBe('en')
  })
})

describe('applying a language', () => {
  it('writes the <html> declaration for each', async () => {
    const lang = await fresh()
    lang.set('zh')
    expect(document.documentElement.lang).toBe('zh-CN')
    expect(lang.get().lang).toBe('zh')
    lang.set('en')
    expect(document.documentElement.lang).toBe('en')
    expect(lang.get().lang).toBe('en')
  })

  it('moves the catalogue column with it', async () => {
    const lang = await fresh()
    const t = await words()
    lang.set('en')
    expect(t('gui.new_task')).toBe('New task')
    lang.set('zh')
    expect(t('gui.new_task')).toBe(ZH_NEW_TASK)
  })

  /* What the five passes applyI18n made over the document became: a value the
     region renders beside the key. The text ones need no pick -- the page is in
     a language from its first frame -- and `attr` covers the ones the markup
     carried nothing for, which is every aria-label, title and data-tip on the
     page: absent until a reader's pick lands, which is what both boot goldens
     record. Which element gets which is src/App.test.tsx's and each region's
     own test's. */
  it('answers attr() with nothing until a pick lands, then the catalogue', async () => {
    const lang = await fresh()
    expect(lang.attr('gui.collapse_rail')).toBe(undefined)
    lang.set('en')
    expect(lang.attr('gui.collapse_rail')).toBe('Collapse sidebar')
    lang.set('zh')
    expect(lang.attr('gui.collapse_rail')).not.toBe('Collapse sidebar')
  })

  /* A remembered pick IS a pick, so the page it boots carries those attributes
     from the first frame -- the reader picked, just not on this load. */
  it('answers attr() straight away for a remembered pick', async () => {
    localStorage.setItem('raven.gui.lang', 'en')
    const lang = await fresh()
    expect(lang.attr('gui.collapse_rail')).toBe('Collapse sidebar')
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

  /* Two groups, and the rendered half commits first: applyI18n rewrote the
     markup and only then did everything drawn from JavaScript redraw
     (state/lang/effects.ts is that redraw). */
  it('commits the regions before the group that draws itself', async () => {
    const lang = await fresh()
    const seen: string[] = []
    lang.subscribe(() => { seen.push('render') })
    lang.onApplied(() => { seen.push('redraw') })
    lang.set('en')
    expect(seen).toEqual(['render', 'redraw'])
  })

  it('stops calling a redraw that has been taken off', async () => {
    const lang = await fresh()
    let calls = 0
    const stop = lang.onApplied(() => { calls += 1 })
    lang.set('en')
    stop()
    lang.set('zh')
    expect(calls).toBe(1)
  })
})
