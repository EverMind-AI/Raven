// @vitest-environment happy-dom
/* The capabilities page's own state: the tab, the filter bar, the chrome a
 * draw decides, and the order a draw's steps run in.
 *
 * The component that renders what is state here has its own file
 * (src/chrome/CapsPage.test.tsx). What is asserted here is what the legacy
 * chrome used to do by id, in the order it did it: the two tab layers'
 * effects, the container attributes, the hero's position, and the names the
 * other layers still call.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { T } from '../i18n/t'
import { mountPageRoot } from '../test/pageRoot'

/* Every case starts from the served page: the tab, the two buttons and the
   registered hooks are module state, so a case that ran before must not be
   visible in the next. */
let caps: typeof import('./caps')

const el = (id: string): HTMLElement => document.getElementById(id) as HTMLElement
const field = (): HTMLInputElement => el('cq') as HTMLInputElement
const bar = (): HTMLElement => document.querySelector('#capsPage .cbar') as HTMLElement

let unmount = (): void => {}

/* The section is src/chrome/CapsPage.tsx's markup now, so the page's own root is
   what a case reads the six values off -- the store commits synchronously, which
   is what keeps every assertion below a read straight after the write. */
beforeEach(async () => {
  unmount()
  document.body.innerHTML = ''
  caps = await import('./caps')
  caps.wipe()
  unmount = mountPageRoot()
  /* Two cases below are about a tab flip closing a card that is open, and the
     served drawer is shut. */
  el('detail').dataset.open = 'true'
})

afterEach(() => {
  unmount()
  unmount = () => {}
  document.body.innerHTML = ''
})

describe('the capabilities page state', () => {
  it('is served showing the skill tab, with the first pill pressed and no query', () => {
    expect(caps.get()).toMatchObject({ tab: 'skill', kind: 'all', query: '' })
    /* Null, not a word: no draw has named either, so the literals page.html
       was served with are what stand. */
    expect(caps.get().title).toBe(null)
    expect(caps.get().search).toBe(null)
    expect(caps.get().skill).toBe(null)
    expect(caps.get().plugin).toBe(null)
  })

  /* extSetBase's five writes, then each layer's own reset -- which is what the
     two decorators around it did, applied from the inside out. */
  it('switches the tab, resets the filter, clears the field and drops each layer', () => {
    const order: string[] = []
    caps.onTab(() => order.push('skills'))
    caps.onTab(() => order.push('plugins'))
    caps.pick('attn')
    caps.setQuery('git')
    field().value = 'git'

    caps.extSet('plugin')

    expect(caps.get()).toMatchObject({ tab: 'plugin', kind: 'all', query: '' })
    expect(field().value).toBe('')
    /* caps and memory share the drawer, so a card left open goes with the tab. */
    expect(el('detail').dataset.open).toBe('false')
    expect(order).toEqual(['skills', 'plugins'])
  })

  it('ignores a flip to the tab already up, and to no tab at all', () => {
    const order: string[] = []
    caps.onTab(() => order.push('tab'))
    caps.pick('on')
    caps.extSet('skill')
    caps.extSet(null)
    caps.extSet(undefined)
    expect(caps.get()).toMatchObject({ tab: 'skill', kind: 'on' })
    expect(order).toEqual([])
    expect(el('detail').dataset.open).toBe('true')
  })

  /* The order the decorator chain made visible: the plugin layer wrapped the
     skill layer, so on the plugin tab the skill button was synced first, and on
     the skill tab the plugin button and the shared hero came after the draw. */
  it('draws whichever tab is up, in the order the decorator chain made visible', () => {
    const steps: string[] = []
    caps.onDraw({
      skill: () => steps.push('skill'),
      skillButton: () => steps.push('skillButton'),
      plugin: () => steps.push('plugin'),
      pluginButton: () => steps.push('pluginButton'),
      hero: () => steps.push('hero'),
    })
    caps.draw()
    expect(steps).toEqual(['skill', 'pluginButton', 'hero'])
    steps.length = 0
    caps.extSet('plugin')
    caps.draw()
    expect(steps).toEqual(['skillButton', 'plugin', 'hero'])
  })

  it('writes the three container attributes a draw decides, and keeps the rest as state', () => {
    caps.chrome({
      title: 'Installed skills',
      label: 'Skills',
      search: 'Search the skill market',
      pillsHidden: true,
      advHidden: true,
      bar: 'none',
    })
    expect(el('capsPage').getAttribute('aria-label')).toBe('Skills')
    expect(el('advAdd').hidden).toBe(true)
    expect(bar().style.display).toBe('none')
    expect(caps.get()).toMatchObject({
      title: 'Installed skills',
      search: 'Search the skill market',
      pillsHidden: true,
    })
    caps.chrome({ title: 'Plugins', label: 'Plugins', search: 'Search plugins', pillsHidden: true, advHidden: false, bar: '' })
    expect(el('advAdd').hidden).toBe(false)
    expect(bar().style.display).toBe('')
  })

  it('puts the bar back from outside a draw, which three paths do', () => {
    caps.bar('none')
    expect(bar().style.display).toBe('none')
    caps.bar('')
    expect(bar().style.display).toBe('')
  })

  /* A pill and the bar's own term both redraw, which is what made the filter
     visible at all: the pills are hidden by every draw, so the only reachable
     writer of `kind` is a click a test can still make. */
  it('picks a status pill and redraws', () => {
    let draws = 0
    caps.onDraw({ skill: () => { draws += 1 } })
    caps.pick('attn')
    expect(caps.get().kind).toBe('attn')
    expect(draws).toBe(1)
  })

  it('keeps the bar term the third layer of the search chain wrote', () => {
    let draws = 0
    caps.onDraw({ skill: () => { draws += 1 } })
    caps.setQuery('crm')
    expect(caps.get().query).toBe('crm')
    expect(draws).toBe(1)
  })

  /* Created once with no label, which is the empty button the two parts
     appended to the bar; every later call is its sync. */
  it('creates each installed button once and syncs it afterwards', () => {
    caps.installedButton('skill', { hidden: false, label: null, badge: null })
    expect(caps.get().skill).toEqual({ hidden: false, label: null, badge: null })
    caps.installedButton('skill', { hidden: true, label: 'Installed 2', badge: null })
    expect(caps.get().skill).toEqual({ hidden: true, label: 'Installed 2', badge: null })
    caps.installedButton('plugin', { hidden: false, label: 'Installed 3', badge: '2' })
    expect(caps.get().plugin).toEqual({ hidden: false, label: 'Installed 3', badge: '2' })
    /* One button's sync never moves the other's. */
    expect(caps.get().skill).toEqual({ hidden: true, label: 'Installed 2', badge: null })
  })

  /* The hero is a child of .wrap, between two the page still provides, so it
     is inserted by hand and has to land before the bar. */
  it('inserts the hero before the bar, repopulates it, and hides it with no title', () => {
    caps.hero('Skill market')
    const hero = el('pageHero')
    expect(hero.nextElementSibling).toBe(bar())
    expect(hero.className).toBe('pmhero')
    expect(hero.hidden).toBe(false)
    expect(hero.innerHTML).toBe('<h3>Skill market</h3>')
    caps.hero('Plugin market')
    expect(document.querySelectorAll('#pageHero')).toHaveLength(1)
    expect(el('pageHero').innerHTML).toBe('<h3>Plugin market</h3>')
    caps.hero('')
    expect(el('pageHero').hidden).toBe(true)
    expect(el('pageHero').childNodes).toHaveLength(0)
  })

  /* The names the other layers, the boot list and the bridge still call: each
     is a shell over this store now, and the two tab renderers register into it
     rather than decorating each other. */
  it('is what the legacy names the rest of the page calls go through', async () => {
    const legacy = await import('../legacy/demo/120-capabilities.js')
    const skills = await import('../legacy/demo/152-skills.js')
    const plugins = await import('../legacy/demo/153-plugins.js')
    let draws = 0
    caps.onDraw({ skill: () => { draws += 1 } })
    caps.draw()
    expect(draws).toBe(1)
    legacy.extSetBase('plugin')
    expect(caps.get().tab).toBe('plugin')
    expect(typeof plugins.drawPlugTab).toBe('function')
    expect(typeof skills.drawSkillTab).toBe('function')
  })
})

/* The chrome half of the two tabs' draws, which used to be six writes by id in
   each of them (demo/152-skills.js, demo/153-plugins.js). The islands and the
   sources are stood in for; what is asserted is the chrome each view decides.
   Last in the file, because standing in for the island bag is module state. */
describe('the two draws the dispatch reaches', () => {
  const skillHost = document.createElement('div')
  const plugHost = document.createElement('div')
  const views = { skill: 'market', plugin: 'market' }

  beforeEach(async () => {
    views.skill = 'market'
    views.plugin = 'market'
    const { islands } = await import('../islands')
    Object.assign(islands.skills, {
      view: () => views.skill,
      attach: (box: Element) => box.appendChild(skillHost),
      redraw: () => {},
      ensureSearch: () => {},
      subscribe: () => () => {},
    })
    Object.assign(islands.plugins, {
      view: () => views.plugin,
      host: plugHost,
      redraw: () => {},
      installedCount: () => 3,
      searchIfIdle: () => {},
    })
    const { setSources } = await import('./sources')
    setSources({
      skills: { installed: () => [{}, {}] },
      plugins: { rows: () => [{ state: 'need' }] },
    } as never)
  })

  it('gives the skill market its title, its hint, no pills and no manual add', async () => {
    const { drawSkillTab } = await import('../legacy/demo/152-skills.js')
    /* What the other tab left in the box, which a draw clears wholesale. */
    el('capsBody').appendChild(document.createElement('i'))
    drawSkillTab()
    expect(caps.get().title).toBe(T('gui.tab.skills'))
    expect(caps.get().search).toBe(T('gui.hub.search_ph'))
    expect(caps.get().pillsHidden).toBe(true)
    expect(el('capsPage').getAttribute('aria-label')).toBe(T('gui.tab.skills'))
    expect(el('advAdd').hidden).toBe(true)
    expect(bar().style.display).toBe('')
    /* The island's host, under a box this cleared first. */
    expect(el('capsBody').children).toHaveLength(1)
    expect(el('capsBody').firstElementChild).toBe(skillHost)
    expect(caps.get().skill).toEqual({ hidden: false, label: T('gui.plug.installed_n', { n: 2 }), badge: null })
  })

  it('renames the skill tab and takes the bar down on its installed view', async () => {
    const { drawSkillTab } = await import('../legacy/demo/152-skills.js')
    views.skill = 'installed'
    drawSkillTab()
    expect(caps.get().title).toBe(T('gui.plug.installed_title'))
    expect(bar().style.display).toBe('none')
    expect(caps.get().pillsHidden).toBe(true)
    expect(el('advAdd').hidden).toBe(true)
    expect(caps.get().skill?.hidden).toBe(true)
  })

  it('gives the plugin market the manual add, which only it offers', async () => {
    const { drawPlugTab } = await import('../legacy/demo/153-plugins.js')
    caps.extSet('plugin')
    drawPlugTab()
    expect(caps.get().title).toBe(T('gui.tab.plugins'))
    expect(caps.get().search).toBe(T('gui.plug.search_ph'))
    expect(caps.get().pillsHidden).toBe(true)
    expect(el('capsPage').getAttribute('aria-label')).toBe(T('gui.tab.plugins'))
    expect(el('advAdd').hidden).toBe(false)
    expect(bar().style.display).toBe('')
    expect(el('capsBody').firstElementChild).toBe(plugHost)
    /* The badge counts the rows that need the reader, which skills never do. */
    expect(caps.get().plugin).toEqual({ hidden: false, label: T('gui.plug.installed_n', { n: 3 }), badge: '1' })
  })

  it('hides the manual add on the plugin tab installed view', async () => {
    const { drawPlugTab } = await import('../legacy/demo/153-plugins.js')
    caps.extSet('plugin')
    views.plugin = 'installed'
    drawPlugTab()
    expect(caps.get().title).toBe(T('gui.plug.installed_title'))
    expect(el('advAdd').hidden).toBe(true)
    expect(bar().style.display).toBe('none')
    expect(caps.get().plugin?.hidden).toBe(true)
  })

  /* The hero covers both tabs from the plugin layer, and only the markets have
     one: an installed view is a list, not a shop front. */
  it('gives each market a hero and takes it away on the installed views', async () => {
    const plugins = await import('../legacy/demo/153-plugins.js')
    const { drawSkillTab } = await import('../legacy/demo/152-skills.js')
    caps.onDraw({ skill: drawSkillTab, plugin: plugins.drawPlugTab })
    plugins.install()
    caps.draw()
    expect(el('pageHero').innerHTML).toBe(`<h3>${T('gui.hub.hero')}</h3>`)
    caps.extSet('plugin')
    caps.draw()
    expect(el('pageHero').innerHTML).toBe(`<h3>${T('gui.plug.hero')}</h3>`)
    views.plugin = 'installed'
    caps.draw()
    expect(el('pageHero').hidden).toBe(true)
  })
})
