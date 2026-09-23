// @vitest-environment happy-dom
/* What an entrance's row draws, and off which field.
 *
 * The mark is chosen by channel id -- the adapter's package name -- and the row's
 * name is not a field this component takes at all. That is the stronger form of
 * the rule it used to test: which app a row is cannot follow what the reader's
 * language calls it, because the name never reaches here.
 *
 * Every way a row can end up without its file lands on the plain mark: an id
 * nobody drew, and a file the request could not find. A broken-image square is
 * the one thing this must never show, and a letter in a coloured square -- which
 * spelt a CJK name's first character as though it were an initial -- is gone.
 */

import { act, fireEvent } from '@testing-library/react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { ChannelMark } from './ChannelMark'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let host: HTMLDivElement
let root: ReturnType<typeof createRoot>

beforeEach(() => {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => { root.unmount() })
  document.body.innerHTML = ''
  delete (window as { __ASSETV?: string }).__ASSETV
})

const draw = (id: string): void => {
  act(() => { root.render(<ChannelMark id={id} />) })
}

const mark = (): HTMLElement | null => host.querySelector('.channel-mark')
const img = (): HTMLImageElement | null => host.querySelector('img')
/* The two non-brand marks -- mail's envelope and the plain fallback -- draw the
   same shape, and which one it is follows the id rather than anything the DOM
   says. So a test names the id and checks the shape. */
const plate = (): boolean =>
  !!mark()?.classList.contains('channel-mark-inset') && !!mark()?.querySelector('svg')

describe('an entrance row s app mark', () => {
  it('draws the app icon the build copies, by channel id', () => {
    draw('weixin')
    expect(img()?.getAttribute('src')).toBe('assets/channels/weixin.png')
    expect(mark()?.classList.contains('channel-mark-inset')).toBe(false)
  })

  it('keeps the brand whatever the row is called', () => {
    draw('feishu')
    expect(img()?.getAttribute('src')).toBe('assets/channels/feishu.png')
  })

  it('gives an id nobody drew the plain mark rather than a brand', () => {
    draw('irc')
    expect(img()).toBeNull()
    expect(plate()).toBe(true)
  })

  it('fills the tile with an icon that is one', () => {
    draw('mochat')
    expect(img()?.getAttribute('src')).toBe('assets/channels/mochat.svg')
    expect(mark()?.classList.contains('channel-mark-inset')).toBe(false)
  })

  it('sets a mark that is not a tile on a plate of its own', () => {
    draw('matrix')
    expect(img()?.getAttribute('src')).toBe('assets/channels/matrix.svg')
    expect(mark()?.classList.contains('channel-mark-inset')).toBe(true)
  })

  it('draws mail as mail rather than as anyone s brand', () => {
    draw('email')
    expect(img()).toBeNull()
    expect(plate()).toBe(true)
  })

  it('leaves the mark to the name beside it for assistive tech', () => {
    draw('telegram')
    expect(mark()?.getAttribute('aria-hidden')).toBe('true')
    expect(img()?.getAttribute('alt')).toBe('')
  })

  it('falls back to the plain mark when the file does not load', () => {
    draw('slack')
    act(() => { fireEvent.error(img()!) })
    expect(img()).toBeNull()
    expect(plate()).toBe(true)
  })

  it('does not carry one file s failure onto the next channel drawn in its place', () => {
    draw('slack')
    act(() => { fireEvent.error(img()!) })
    draw('discord')
    expect(img()?.getAttribute('src')).toBe('assets/channels/discord.png')
  })

  it('asks for the file under the asset digest the build stamps', () => {
    ;(window as { __ASSETV?: string }).__ASSETV = 'abc123'
    draw('qq')
    expect(img()?.getAttribute('src')).toBe('assets/channels/qq.png?v=abc123')
  })
})
