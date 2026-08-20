// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { install, open } from './chips'

import type { ProseSource, ProseTarget } from './prose'

/* install() binds on the document and never unbinds -- it is the page's, for
   the page's life. So it is installed once here, and each case reads what the
   source below recorded. */
let seen: ProseTarget[] = []
let source: ProseSource

install()

beforeEach(() => {
  seen = []
  source = {
    pathOf: () => null,
    linkTargetOf: () => null,
    open: (at) => seen.push(at),
  }
  window.DS = { prose: source }
})

afterEach(() => {
  document.body.innerHTML = ''
  delete window.DS
})

const click = (el: Element): void => {
  el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

const press = (key: string): boolean => {
  const e = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })
  document.activeElement?.dispatchEvent(e)
  return e.defaultPrevented
}

describe('a path chip', () => {
  it('opens the path the renderer resolved, not the text on screen', () => {
    document.body.innerHTML = '<p><code class="pth" data-p="out/report.md">report.md</code></p>'
    click(document.querySelector('.pth')!)
    expect(seen).toEqual([{ p: 'out/report.md', dir: false }])
  })

  it('is reached through whatever is inside it', () => {
    document.body.innerHTML = '<span class="artf" data-p="out/deliver"><span class="tx">open it</span></span>'
    click(document.querySelector('.tx')!)
    expect(seen).toEqual([{ p: 'out/deliver', dir: false }])
  })

  it('carries the folder mark the renderer put on it', () => {
    document.body.innerHTML = '<span class="artf" data-p="out/pack" data-d="1">pack</span>'
    click(document.querySelector('.artf')!)
    expect(seen[0]!.dir).toBe(true)
  })

  /* The transcript island draws its chips as buttons, and the handler this
     replaced asked for `code.pth` -- so that island had to open the path
     itself. One selector now serves both. */
  it('works for a button chip as well as a code one', () => {
    document.body.innerHTML = '<button class="pth" data-p="src/main.ts">main.ts</button>'
    click(document.querySelector('.pth')!)
    expect(seen).toEqual([{ p: 'src/main.ts', dir: false }])
  })

  it('stays quiet for prose that is not a chip', () => {
    document.body.innerHTML = '<p><code>rm -rf</code> <a href="#x">link</a></p>'
    click(document.querySelector('code')!)
    click(document.querySelector('a')!)
    expect(seen).toEqual([])
  })

  /* A chip with no data-p is a bug upstream, not a reason to open the empty
     string -- which the live source would resolve against the session root. */
  it('stays quiet for a chip carrying no path', () => {
    document.body.innerHTML = '<code class="pth">looks like one</code>'
    click(document.querySelector('.pth')!)
    expect(seen).toEqual([])
  })
})

describe('a chip under the keyboard', () => {
  it('answers Enter and Space, and takes the keystroke with it', () => {
    document.body.innerHTML = '<code class="pth" data-p="a/b.md" tabindex="0">b.md</code>'
    const el = document.querySelector<HTMLElement>('.pth')!
    el.focus()
    expect(press('Enter')).toBe(true)
    expect(press(' ')).toBe(true)
    expect(seen).toEqual([{ p: 'a/b.md', dir: false }, { p: 'a/b.md', dir: false }])
  })

  it('leaves every other key to whoever wants it', () => {
    document.body.innerHTML = '<code class="pth" data-p="a/b.md" tabindex="0">b.md</code>'
    document.querySelector<HTMLElement>('.pth')!.focus()
    expect(press('Escape')).toBe(false)
    expect(press('ArrowDown')).toBe(false)
    expect(seen).toEqual([])
  })

  /* Space scrolls, and a page that scrolled AND opened the file would leave
     the reader looking at the wrong thing. But only for a focused chip: the
     preventDefault must not reach a Space pressed anywhere else. */
  it('leaves Space alone when nothing is focused on a chip', () => {
    document.body.innerHTML = '<textarea id="f"></textarea>'
    document.querySelector<HTMLElement>('#f')!.focus()
    expect(press(' ')).toBe(false)
    expect(seen).toEqual([])
  })
})

describe('the open seam', () => {
  it('is the page\'s answer, not this module\'s', () => {
    open({ p: 'x/y.md', dir: false })
    expect(seen).toEqual([{ p: 'x/y.md', dir: false }])
  })

  /* A source that renders prose nobody can click says nothing about opening,
     and a click on a chip it drew anyway must not throw. */
  it('does nothing when the source has no opener', () => {
    window.DS = { prose: { pathOf: () => null, linkTargetOf: () => null } }
    document.body.innerHTML = '<code class="pth" data-p="a/b.md">b.md</code>'
    expect(() => click(document.querySelector('.pth')!)).not.toThrow()
  })
})
