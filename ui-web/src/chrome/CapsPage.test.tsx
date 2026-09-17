// @vitest-environment happy-dom
/* The capabilities page's chrome as the page root renders it.
 *
 * The shape is pinned by the region golden (src/test/regions.test.ts, which
 * renders page.html's body plus this root) and by both boot goldens, so nothing
 * here re-states it. What is here is what a golden of tags, ids, classes and
 * data-* cannot see: the flags that are not data-*, the three elements handed
 * over to another writer, the order the bar's children keep across a tab flip,
 * the search field's composition guard, and the manual add's one call path.
 */
import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as plugins from '../features/plugins/store'
import * as skills from '../features/skills/store'
import { FixtureTransport } from '../rpc/fixtureTransport'
import { setGateway } from '../rpc/gateway'
import * as caps from '../state/caps'
import * as lang from '../state/lang'
import { resetSources, setSources } from '../state/sources'
import { mountPageRoot } from '../test/pageRoot'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/* Nothing: the page root renders the section, and the standing host a notice
   needs is one of its siblings. */
const MARKUP = ''

/* The two verbs the field dispatches to, and the two the buttons call. The bag
   is assigned over rather than mocked, the way scripts/module-harness.mjs does it. */
const asked = vi.hoisted(() => ({ list: [] as string[] }))

let unmount = (): void => {}

function render(): void {
  unmount()
  document.body.innerHTML = MARKUP
  unmount = mountPageRoot()
}

const el = (id: string): HTMLElement => document.getElementById(id) as HTMLElement
const field = (): HTMLInputElement => el('cq') as HTMLInputElement
const bar = (): HTMLElement => document.querySelector('#capsPage .cbar') as HTMLElement
const kids = (parent: Element): string[] =>
  Array.from(parent.children).map((c) => c.id || (c.getAttribute('class') ?? '') || c.tagName.toLowerCase())

/* Both tabs' installed buttons, created the way the two legacy parts create
   them on install: empty, and only then synced. */
function createButtons(): void {
  act(() => {
    caps.installedButton('skill', { hidden: false, label: null, badge: null })
    caps.installedButton('plugin', { hidden: false, label: null, badge: null })
  })
}

beforeEach(() => {
  /* The tab, the filter and the two buttons are module state, and this
     component renders them, so every case starts from the served page. */
  caps._resetForTests()
  asked.list.length = 0
  vi.spyOn(skills, 'setQuery').mockImplementation((q: string) => asked.list.push(`skills.setQuery:${q}`))
  vi.spyOn(skills, 'searchNow').mockImplementation((q: string) => asked.list.push(`skills.searchNow:${q}`))
  vi.spyOn(skills, 'toggleView').mockImplementation(() => asked.list.push('skills.toggleView'))
  vi.spyOn(plugins, 'setQuery').mockImplementation((q: string) => asked.list.push(`plugins.setQuery:${q}`))
  vi.spyOn(plugins, 'toggleView').mockImplementation(() => asked.list.push('plugins.toggleView'))
})

afterEach(() => {
  unmount()
  unmount = () => {}
  document.body.innerHTML = ''
  resetSources()
  vi.restoreAllMocks()
})

describe('the capabilities page chrome', () => {
  it('portals the three interiors into the containers page.html carries', () => {
    render()
    expect(kids(el('capsPage').querySelector('header') as Element)).toEqual(['capsTitle'])
    expect(kids(bar())).toEqual(['cfind', 'cKind'])
    expect(kids(el('advAdd'))).toEqual(['summary', 'p', 'row'])
    /* .wrap keeps the three children the page serves, in order: a portal
       appends, so the body between them could not be React's. */
    expect(kids(el('capsPage').querySelector('.wrap') as Element)).toEqual(['cbar', 'capsBody', 'advAdd'])
  })

  it('renders every id the chrome, the islands and the writers reach for, once each', () => {
    render()
    for (const id of ['capsTitle', 'cq', 'cKind', 'mName', 'mAddr', 'mAdd']) {
      expect(document.querySelectorAll(`#${id}`), id).toHaveLength(1)
    }
    expect(el('cKind').querySelectorAll('.pill')).toHaveLength(4)
  })

  /* What the goldens drop: they record tag, id, class and data-*, so a deleted
     role, a deleted flag or a blanked literal all pass them. */
  it('keeps the roles, the flags and the served states that are not data-*', () => {
    render()
    expect(el('cKind').getAttribute('role')).toBe('group')
    expect(Array.from(el('cKind').children).map((c) => c.getAttribute('aria-pressed')))
      .toEqual(['true', 'false', 'false', 'false'])
    expect(field().placeholder).not.toBe('')
    expect((el('mName') as HTMLInputElement).placeholder).not.toBe('')
    expect((el('mAddr') as HTMLInputElement).placeholder).not.toBe('')
    /* Served showing: every draw hides both, and nothing has drawn yet. */
    expect(el('cKind').hidden).toBe(false)
    expect(el('advAdd').hidden).toBe(false)
    for (const svg of el('capsPage').querySelectorAll('svg')) {
      expect(svg.getAttribute('aria-hidden')).toBe('true')
    }
    /* The one inline style on the page, written through the CSSOM now: the
       three declarations, not the attribute text page.html spelled. */
    const hint = el('advAdd').querySelector('p') as HTMLElement
    expect(hint.style.color).toBe('var(--muted)')
    expect(hint.style.fontSize).toBe('12.5px')
    expect(hint.style.margin).toBe('10px 0px 0px')
  })

  it('renders a literal in every element that carried one', () => {
    render()
    for (const sel of ['#capsTitle', '#cKind .pill:nth-child(1)', '#cKind .pill:nth-child(2)', '#cKind .pill:nth-child(3)', '#cKind .pill:nth-child(4)', '#advAdd summary', '#advAdd p', '#mAdd']) {
      expect(document.querySelector(sel)?.textContent, sel).not.toBe('')
    }
  })

  /* Shared ground: the two islands attach their own hosts under #capsBody and
     a tab draw clears it with innerHTML, so React owning that child list would
     tear down what the other side put there. */
  it('hands #capsBody over empty and leaves what an island attached alone', () => {
    render()
    expect(el('capsBody').childNodes).toHaveLength(0)
    const host = document.createElement('div')
    host.id = 'islandHost'
    el('capsBody').appendChild(host)
    act(() => {
      caps.setFrame({ title: 'Skills', label: 'Skills', search: 'Search', pillsHidden: true, advHidden: true, bar: '' })
    })
    expect(el('capsBody').firstElementChild).toBe(host)
  })

  /* The two installed buttons used to be two appends into the bar, so their
     order was the order the parts installed in and a re-render could not be
     trusted with it. They are React's children now. */
  it('keeps the bar in one order across five tab flips', () => {
    render()
    createButtons()
    const first = kids(bar())
    expect(first).toEqual(['cfind', 'cKind', 'pminstbtn', 'pminstbtn'])
    for (let i = 0; i < 5; i++) {
      act(() => {
        caps.extSet(i % 2 === 0 ? 'plugin' : 'skill')
      })
      expect(kids(bar()), `flip ${i}`).toEqual(first)
    }
    expect(caps.get().tab).toBe('plugin')
  })

  it('shows each installed button only once its part has created it, and syncs its label', () => {
    render()
    expect(bar().querySelectorAll('.pminstbtn')).toHaveLength(0)
    createButtons()
    const btns = bar().querySelectorAll('.pminstbtn')
    /* Created empty and visible, which is the button mk() appended. */
    expect(btns[0]!.childNodes).toHaveLength(0)
    expect((btns[0] as HTMLElement).hidden).toBe(false)
    act(() => {
      caps.installedButton('skill', { hidden: false, label: 'Installed 2', badge: null })
      caps.installedButton('plugin', { hidden: true, label: 'Installed 3', badge: '4' })
    })
    expect(btns[0]!.innerHTML).toBe('<span>Installed 2</span>')
    expect(btns[1]!.innerHTML).toBe('<span>Installed 3</span><span class="pmbdg">4</span>')
    expect((btns[1] as HTMLElement).hidden).toBe(true)
  })

  it('switches each tab through its own installed button', () => {
    render()
    createButtons()
    const btns = bar().querySelectorAll<HTMLElement>('.pminstbtn')
    act(() => {
      btns[0]!.click()
    })
    act(() => {
      btns[1]!.click()
    })
    expect(asked.list).toEqual(['skills.toggleView', 'plugins.toggleView'])
  })

  /* The hero is a child of .wrap rather than of the bar, so it is inserted by
     hand -- and a re-render of the bar must not move or drop it. */
  it('leaves the hero where the store inserted it, before the bar', () => {
    render()
    createButtons()
    act(() => {
      caps.hero('Skill market')
    })
    expect(el('pageHero').nextElementSibling).toBe(bar())
    act(() => {
      caps.extSet('plugin')
      caps.installedButton('plugin', { hidden: false, label: 'Installed 1', badge: null })
    })
    expect(el('pageHero').nextElementSibling).toBe(bar())
    expect(kids(el('capsPage').querySelector('.wrap') as Element)).toEqual(['pageHero', 'cbar', 'capsBody', 'advAdd'])
  })

  it('flips the pressed pill, and redraws on the click', () => {
    render()
    let draws = 0
    caps.onDraw({ skill: () => { draws += 1 } })
    const pills = Array.from(el('cKind').children) as HTMLElement[]
    for (const [i, pill] of pills.entries()) {
      act(() => {
        pill.click()
      })
      expect(pills.map((p) => p.getAttribute('aria-pressed')), `pill ${i}`)
        .toEqual(pills.map((_, n) => String(n === i)))
      expect(caps.get().kind).toBe(pill.dataset.k)
    }
    expect(draws).toBe(4)
  })

  /* The field is uncontrolled and its two listeners are native, so the guard
     reads the real KeyboardEvent. */
  it('dispatches a typed query to whichever tab is up', () => {
    render()
    field().value = 'git'
    field().dispatchEvent(new Event('input', { bubbles: true }))
    act(() => {
      caps.extSet('plugin')
    })
    field().value = 'crm'
    field().dispatchEvent(new Event('input', { bubbles: true }))
    expect(asked.list).toEqual(['skills.setQuery:git', 'plugins.setQuery:crm'])
  })

  it('searches the hub on Enter, and not while a composition is open', () => {
    render()
    field().value = 'git'
    field().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, isComposing: true }))
    /* The older spelling some input methods still send instead. */
    const legacyIme = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })
    Object.defineProperty(legacyIme, 'keyCode', { value: 229 })
    field().dispatchEvent(legacyIme)
    expect(asked.list).toEqual([])
    field().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect(asked.list).toEqual(['skills.searchNow:git'])
    /* Enter on the plugin tab is the debounce's, not a search of its own. */
    act(() => {
      caps.extSet('plugin')
    })
    field().value = 'crm'
    field().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect(asked.list).toEqual(['skills.searchNow:git'])
  })

  it('clears the field when the tab changes, because the query is not a question about the other', () => {
    render()
    field().value = 'git'
    act(() => {
      caps.extSet('plugin')
    })
    expect(field().value).toBe('')
  })

  /* The manual add fails against a resident gateway today: neither name is a
     declared method, so both go through the unchecked path and the second is
     answered -32601. Kept exactly so -- the fix is a declared method. */
  it('registers a server by hand through the two undeclared names, and shows the refusal', async () => {
    render()
    const { pluginsSource } = await import('../features/plugins/source')
    const transport = new FixtureTransport({ 'raven.mcp.list': { servers: [] } } as never)
    setGateway(transport)
    setSources({ plugins: pluginsSource })
    ;(el('mName') as HTMLInputElement).value = 'CRM'
    ;(el('mAddr') as HTMLInputElement).value = 'npx -y @acme/crm-mcp'

    await act(async () => {
      el('mAdd').click()
    })

    expect(transport.calls.map((c) => c.method)).toEqual(['raven.mcp.list', 'raven.mcp.set'])
    expect(transport.calls[1]!.params).toEqual({ servers: [{ name: 'CRM', address: 'npx -y @acme/crm-mcp' }] })
    /* The refusal is spoken and nothing is cleared, so the reader keeps what
       they typed. */
    const notice = document.querySelector('#toasts .toast')
    expect(notice?.textContent).toContain('raven.mcp.set')
    expect((el('mName') as HTMLInputElement).value).toBe('CRM')
    expect((el('mAddr') as HTMLInputElement).value).toBe('npx -y @acme/crm-mcp')
  })

  it('asks for nothing when either field is empty', async () => {
    render()
    const { pluginsSource } = await import('../features/plugins/source')
    const transport = new FixtureTransport({ 'raven.mcp.list': { servers: [] } } as never)
    setGateway(transport)
    setSources({ plugins: pluginsSource })
    ;(el('mName') as HTMLInputElement).value = 'CRM'
    await act(async () => {
      el('mAdd').click()
    })
    expect(transport.calls).toEqual([])
    expect(document.querySelector('#toasts .toast')).not.toBe(null)
  })

  /* The title and the search hint are a draw's, and the served literals stand
     until one has run -- so the component must render the store's value rather
     than a copy of its own. */
  it('takes the title and the search hint from the draw that named them', () => {
    render()
    const served = el('capsTitle').textContent
    act(() => {
      caps.setFrame({ title: 'Installed skills', label: 'Skills', search: 'Search the market', pillsHidden: true, advHidden: true, bar: 'none' })
    })
    expect(el('capsTitle').textContent).toBe('Installed skills')
    expect(field().placeholder).toBe('Search the market')
    expect(el('capsTitle').textContent).not.toBe(served)
    expect(el('cKind').hidden).toBe(true)
    expect(el('advAdd').hidden).toBe(true)
    expect(bar().style.display).toBe('none')
  })

  /* Committed synchronously, the way the draw's writes by id were: a draw
     writes the chrome and then hands the body to the island, and the page's
     boot reads the result in the same task (scripts/boot-snapshot.mjs sees
     nothing React defers -- React's own scheduler never flushes under
     happy-dom). No act() here on purpose: that is what makes it observable. */
  it('commits a draw before the statement after it', () => {
    render()
    caps.setFrame({ title: 'Plugins', label: 'Plugins', search: 'Search plugins', pillsHidden: true, advHidden: false, bar: '' })
    expect(el('capsTitle').textContent).toBe('Plugins')
    expect(field().placeholder).toBe('Search plugins')
    expect(el('cKind').hidden).toBe(true)
  })

  /* React leaves an empty onclick on every element it takes a click of (the
     trap that makes clicks fire on iOS), so which controls it owns is readable
     off the elements -- and a second handler bound by id would run beside this
     component's rather than replace it. */
  it('owns the clicks of the add button and the pills, and no more', () => {
    render()
    expect(el('mAdd').onclick).not.toBe(null)
    for (const pill of el('cKind').children) expect((pill as HTMLElement).onclick).not.toBe(null)
    /* The pills' clicks are their own, not the group's, and the field's two
       listeners are native rather than React's. */
    expect(el('cKind').onclick).toBe(null)
    expect(field().oninput).toBe(null)
    expect(field().onkeydown).toBe(null)
  })
})

/* Last in the file on purpose: applying a language is module state for
   everything after it. Same claim as the rail's and the two dialogs': every
   word here is the catalogue's, read at render time, so the page cannot come
   back in the language before the flip. */
describe('the capabilities page chrome once a language is applied', () => {
  it('renders the applied words when the page is mounted again', () => {
    const KEYED = ['#capsTitle', '#cKind .pill:nth-child(1)', '#cKind .pill:nth-child(2)', '#cKind .pill:nth-child(3)', '#cKind .pill:nth-child(4)', '#advAdd summary', '#advAdd p', '#mAdd']
    const words = (): string[] => KEYED.map((sel) => document.querySelector(sel)?.textContent ?? '')
    /* The two fields' hints carry keys too, and are read the same way. */
    const hints = (): string[] => ['mName', 'mAddr'].map((id) => (el(id) as HTMLInputElement).placeholder)
    render()
    const served = words()
    const servedHints = hints()
    lang.set('zh')
    const applied = words()
    const appliedHints = hints()
    expect(applied).not.toEqual(served)
    expect(appliedHints).not.toEqual(servedHints)
    /* A remount over fresh markup, which is what the pass over the document
       cannot help with: the page renders its own literals unless it reads the
       catalogue itself. */
    render()
    expect(words()).toEqual(applied)
    expect(hints()).toEqual(appliedHints)
  })
})
