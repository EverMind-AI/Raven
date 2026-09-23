// @vitest-environment happy-dom
/* The four layers that stand at the body, and who decides their order.
 *
 * Two steps of the `--z` ladder are ties broken by the order the four stand at
 * the body (src/test/portals.test.ts says which), so the table in portals.ts is
 * the decision and `host` is what keeps it -- not whichever module happened to
 * append first.
 *
 * What the page's own reading of the same table pins is in src/test/portals.
 * test.ts: the boot goldens, the markup, and the body while every overlay is
 * up. What is here is the accessor: the elements it makes, and that the order
 * is the table's rather than the caller's.
 */
import { afterEach, describe, expect, it } from 'vitest'

import { BOOT_BODY_ORDER, LAYERS, PORTALS, _resetForTests, host } from './portals'

/** The key the boot goldens file an element under. */
const keyOf = (el: Element): string => {
  const tag = el.tagName.toLowerCase()
  if (el.id) return `${tag}#${el.id}`
  const classes = (el.getAttribute('class') ?? '').trim()
  return classes ? `${tag}.${classes.split(/\s+/).join('.')}` : tag
}

const shapes = (): string[] => [...document.body.children].map((el) => el.outerHTML)

afterEach(() => {
  document.body.innerHTML = ''
  _resetForTests()
})

describe('the standing layers', () => {
  /* One element each, exactly as its own module made it: the scrollbar layer is
     addressed by class, #deskHost by three `body:has(...) #deskHost` rules in
     page.css, and the model picker's wrapper carries neither id nor class --
     it is inert for layout, and .mpick inside it is position: fixed. */
  it('makes each layer the element its own module made', () => {
    expect(host('sbars').outerHTML).toBe('<div class="sbars"></div>')
    expect(host('picker').outerHTML).toBe('<div></div>')
    expect(host('desk').outerHTML).toBe('<div id="deskHost"></div>')
    expect(host('tip').outerHTML).toBe('<div class="tipp"></div>')
    expect(document.body.children).toHaveLength(4)
  })

  it('appends them in the table order, whoever asks first', () => {
    for (const name of [...LAYERS].reverse()) host(name)
    expect(shapes()).toEqual([
      '<div class="sbars"></div>',
      '<div></div>',
      '<div id="deskHost"></div>',
      '<div class="tipp"></div>',
    ])
  })

  it('leaves one gap open for a layer nobody has asked for yet', () => {
    host('sbars')
    host('tip')
    host('desk')
    expect(shapes()).toEqual([
      '<div class="sbars"></div>',
      '<div id="deskHost"></div>',
      '<div class="tipp"></div>',
    ])
  })

  /* Asked for on every use and made once: the scrollbar module calls this on
     every scroll event. The isConnected half is for a test that replaced the
     body under it -- in the page a layer is appended once and never removed. */
  it('hands the same layer back, and a new one once the body is replaced', () => {
    const first = host('sbars')
    expect(host('sbars')).toBe(first)
    expect(document.body.children).toHaveLength(1)
    document.body.innerHTML = ''
    const second = host('sbars')
    expect(second).not.toBe(first)
    expect(second.isConnected).toBe(true)
  })

  /* The accessor and the table have to be talking about the same four
     elements: the table is what the boot goldens are read against, and a layer
     whose shape drifted from its row would pass every assertion above. */
  it('makes the four layers the boot order names, in that order', () => {
    const boot = PORTALS.filter((p) => p.kind === 'append' && p.at !== 'last')
    expect(boot.map((p) => p.at)).toEqual([10, 11, 12, 13])
    expect(LAYERS).toHaveLength(boot.length)
    for (const name of LAYERS) host(name)
    const keys = [...document.body.children].map(keyOf)
    expect(keys).toEqual(boot.map((p) => p.bootKey))
    expect(keys).toEqual(BOOT_BODY_ORDER.slice(-4))
  })
})
