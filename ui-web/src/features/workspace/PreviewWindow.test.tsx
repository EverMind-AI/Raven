// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { setTranslator } from '../../i18n/t'
import { setCurrent } from '../../lib/session'
import { PreviewWindow } from './PreviewWindow'
import * as store from './store'

/* The window a file opens in is the pane's own body on a page with nothing
   else around it: what these cases hold is that it reads the file it was
   opened for, that the pair still switches what it draws, and that the file
   can still leave the window as a download. */

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const serve = (body: string): void => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(body, { status: 200 })))
}

async function mount(path: string) {
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  const view = render(<PreviewWindow path={path} />)
  await act(async () => {})
  return view
}

afterEach(() => {
  act(() => { store.reset() })
  store._resetForTests()
  setCurrent(null)
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('the file preview window', () => {
  it('reads the file it was opened for and draws it in the pane body', async () => {
    serve('# Notes\n\nbody')
    await mount('/repo/docs/notes.md')
    expect(document.querySelector('.workspace-preview-name')?.textContent).toBe('notes.md')
    expect((await screen.findAllByText('Notes')).length).toBeGreaterThan(0)
    expect(document.querySelector('.workspace-md-outline a')?.getAttribute('href')).toBe('#mdoc-notes')
  })

  it('switches a markdown file to its source without re-reading it', async () => {
    serve('# Notes\n\nbody')
    await mount('/repo/docs/notes.md')
    await screen.findAllByText('Notes')
    await act(async () => { screen.getByText('gui.ws.file_source').click() })
    expect(document.querySelector('.workspace-md-body')).toBeNull()
    expect(screen.getByText('# Notes').closest('.code')).not.toBeNull()
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1)
  })

  it('hands the same file to the reader as a download', async () => {
    serve('plain text')
    await mount('/repo/notes.txt')
    const link = document.querySelector<HTMLAnchorElement>('.workspace-preview-head a[download]')!
    expect(link.getAttribute('download')).toBe('notes.txt')
    expect(link.getAttribute('href')).toBe('/file?path=%2Frepo%2Fnotes.txt')
  })

  it('names the window after the file', async () => {
    serve('x')
    await mount('/repo/notes.txt')
    expect(document.title).toBe('notes.txt')
  })
})
