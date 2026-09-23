// @vitest-environment happy-dom
/* What an entrance's row draws, and off which field.
 *
 * The mark is chosen by channel id -- the adapter's package name -- and never
 * by the name on the row. The name is what the reader's language says, and which
 * app a row is does not change with that; a row that merely spells itself
 * `Slack` is not Slack either.
 *
 * Every way a row can end up without its file lands on the letter tile it wore
 * before there were marks: an id nobody drew, and a file the request could not
 * find. A broken-image square is the one thing this must never show.
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

const draw = (id: string, name: string): void => {
  act(() => { root.render(<ChannelMark id={id} name={name} />) })
}

const mark = (): HTMLElement | null => host.querySelector('.channel-mark')
const img = (): HTMLImageElement | null => host.querySelector('img')
const tile = (): HTMLElement | null => host.querySelector('.pmtile')

describe('an entrance row s app mark', () => {
  it('draws the app icon the build copies, by channel id', () => {
    draw('weixin', 'WeChat')
    expect(img()?.getAttribute('src')).toBe('assets/channels/weixin.png')
    expect(tile()).toBeNull()
  })

  it('keeps the brand whatever the row is called', () => {
    draw('feishu', 'Lark')
    expect(img()?.getAttribute('src')).toBe('assets/channels/feishu.png')
  })

  it('gives a row that merely spells a brand no brand', () => {
    draw('irc', 'Slack')
    expect(img()).toBeNull()
    expect(tile()?.textContent).toBe('S')
  })

  it('fills the tile with an icon that is one', () => {
    draw('mochat', 'Mochat')
    expect(img()?.getAttribute('src')).toBe('assets/channels/mochat.svg')
    expect(mark()?.classList.contains('channel-mark-inset')).toBe(false)
  })

  it('sets a mark that is not a tile on a plate of its own', () => {
    draw('matrix', 'Matrix')
    expect(img()?.getAttribute('src')).toBe('assets/channels/matrix.svg')
    expect(mark()?.classList.contains('channel-mark-inset')).toBe(true)
  })

  it('draws mail as mail rather than as anyone s brand', () => {
    draw('email', 'Email')
    expect(img()).toBeNull()
    expect(tile()).toBeNull()
    expect(mark()?.classList.contains('channel-mark-inset')).toBe(true)
    expect(mark()?.querySelector('svg')).not.toBeNull()
  })

  it('leaves the mark to the name beside it for assistive tech', () => {
    draw('telegram', 'Telegram')
    expect(mark()?.getAttribute('aria-hidden')).toBe('true')
    expect(img()?.getAttribute('alt')).toBe('')
  })

  it('falls back to the letter tile when the file does not load', () => {
    draw('slack', 'Slack')
    act(() => { fireEvent.error(img()!) })
    expect(img()).toBeNull()
    expect(tile()?.textContent).toBe('S')
  })

  it('does not carry one file s failure onto the next channel drawn in its place', () => {
    draw('slack', 'Slack')
    act(() => { fireEvent.error(img()!) })
    draw('discord', 'Discord')
    expect(img()?.getAttribute('src')).toBe('assets/channels/discord.png')
  })

  it('asks for the file under the asset digest the build stamps', () => {
    ;(window as { __ASSETV?: string }).__ASSETV = 'abc123'
    draw('qq', 'QQ')
    expect(img()?.getAttribute('src')).toBe('assets/channels/qq.png?v=abc123')
  })
})
