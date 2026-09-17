// @vitest-environment happy-dom
import { act, cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SkillsApp } from './SkillsPage'
import * as store from './store'

import { domSnapshot } from '../../test/domSnapshot'
import { resetSources, setSources, sources } from '../../state/sources'
import { setShell } from '../../shell/bridge'

import type { Shell } from '../../shell/bridge'
import type { HubItem, InstalledSkill, SkillsSource } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

function hubItem(over: Partial<HubItem> = {}): HubItem {
  return {
    id: 'sh-review',
    name: 'code-review-checklist',
    description: 'review by the house rules',
    category: 'DEV',
    source: 'skillhub',
    quality_score: 0.9,
    installed: false,
    installed_name: '',
    ...over,
  }
}

/* The island runs against the same two seams production wires: a fake
   shell handed in through setShell (T returns its key, so tests assert catalogue
   keys, not translations) and a fixture source on sources.skills. */
function install(items: HubItem[], over: Partial<SkillsSource> = {}, installed: InstalledSkill[] = []) {
  const calls: string[] = []
  const source: SkillsSource = {
    search: async () => ({ items, total: items.length }),
    detail: async () => ({ description: 'the whole story', category: 'DEV', files: ['SKILL.md'] }),
    install: async () => calls.push('install'),
    remove: async () => calls.push('remove'),
    installed: () => installed,
    ...over,
  }
  const shellCalls: Array<[string, unknown]> = []
  const fakeShell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: (id) => shellCalls.push(['showPage', id]),
    useInTask: (key, name) => shellCalls.push(['useInTask', `${key}:${name}`]),
  }
  setShell(fakeShell)
  setSources({ skills: source })
  document.body.innerHTML =
    '<section id="capsPage" data-open="true"><div id="capsBody"></div></section>' +
    '<aside id="detail" data-open="false"><b id="dTitle"></b><div id="dBody"></div></aside>'
  return { source, calls, shellCalls }
}

async function mount() {
  const view = render(<SkillsApp />, { container: document.getElementById('capsBody')! })
  await act(async () => {
    store.ensureSearch()
  })
  return view
}

afterEach(() => {
  act(() => {
    store.wipe()
  })
  cleanup()
  vi.restoreAllMocks()
  resetSources()
})

describe('skills island', () => {
  it('shows every hub row the source answers, behind the category chips', async () => {
    install([hubItem(), hubItem({ id: 'sh-sql', name: 'sql-style', quality_score: 0.7 })])
    await mount()
    expect(await screen.findByText('code-review-checklist')).toBeTruthy()
    expect(screen.getByText('sql-style')).toBeTruthy()
    expect(screen.getByText('gui.hubcat.all')).toBeTruthy()
    expect(screen.getAllByText('gui.hub.install').length).toBe(2)
  })

  /* Structural, not measured: happy-dom lays nothing out, so which row the
     element is in is what a unit test can hold. */
  it('puts the rating in the footer, so the name is not the thing that gives way', async () => {
    install([hubItem({ name: 'openclaw-security-review' })])
    await mount()

    const card = document.querySelector('.hubcard.pmcard')!
    expect(card.querySelector('.foot .stars')).not.toBeNull()
    expect(card.querySelector('.top .stars')).toBeNull()
    /* Still shown, not traded away: moving it must not become hiding it. */
    expect(card.querySelector('.foot .stars .sv')!.textContent).toBe('4.5')
  })

  it('carries the whole identifier for one still too long to fit', async () => {
    /* Nothing else on the card repeats the identifier. */
    install([hubItem({ name: 'openclaw-a-very-long-skill-identifier-indeed' })])
    await mount()

    const nm = document.querySelector('.hubcard.pmcard .pmnm > span')!
    expect(nm.getAttribute('title')).toBe('openclaw-a-very-long-skill-identifier-indeed')
  })

  /* The wait for a card's body used to be one line of grey text, which left the
     shared panel a thin strip with a word in it that then jumped to a full card.
     A skeleton of the card's own anatomy holds the box instead. */
  it('holds the card box with a skeleton while its body is being read', async () => {
    let settle: ((d: { description: string }) => void) | null = null
    install([hubItem()], {
      detail: () =>
        new Promise((res) => {
          settle = res
        }),
    })
    await mount()
    await act(async () => {
      ;(await screen.findByText('code-review-checklist')).click()
    })
    const body = document.getElementById('dBody')!
    expect(body.querySelector('.skel')).toBeTruthy()
    /* And the panel holds one box across the wait, so the skeleton and the card
       it becomes are the same size -- a skeleton of its own fixed height only
       moved the jump somewhere else. */
    expect(document.getElementById('detail')!.dataset.fill).toBe('true')
    /* No word standing in for the card, and no thin note. */
    expect(body.textContent).toBe('')
    expect(body.querySelector('.pnote')).toBeNull()
    await act(async () => {
      settle!({ description: 'the whole story' })
    })
    expect(body.querySelector('.skel')).toBeNull()
    expect(body.textContent).toContain('the whole story')
  })

  it('shows the empty note when the filter matches nothing', async () => {
    install([])
    await mount()
    expect(await screen.findByText('gui.hub.empty_filter')).toBeTruthy()
  })

  it('renders a failed search in place, and the retry asks again', async () => {
    const { source } = install([hubItem()], {
      search: async () => {
        throw new Error('boom')
      },
    })
    await mount()
    expect(await screen.findByText('gui.hub.err {"err":"boom"}')).toBeTruthy()
    const searchSpy = vi.spyOn(source, 'search').mockResolvedValue({ items: [hubItem()], total: 1 })
    await act(async () => {
      screen.getByText('gui.plug.retry').click()
    })
    expect(searchSpy).toHaveBeenCalled()
    expect(await screen.findByText('code-review-checklist')).toBeTruthy()
  })

  it('marks the card installed once the source accepts the install', async () => {
    const { calls } = install([hubItem()])
    await mount()
    await act(async () => {
      ;(await screen.findByText('gui.hub.install')).click()
    })
    expect(calls).toContain('install')
    expect(await screen.findByText('gui.hub.installed')).toBeTruthy()
  })

  it('leaves the card installable when the source rejects handled', async () => {
    install([hubItem()], {
      install: async () => {
        // What the live source throws after toasting the reason itself.
        throw { handled: true }
      },
    })
    await mount()
    await act(async () => {
      ;(await screen.findByText('gui.hub.install')).click()
    })
    expect(await screen.findByText('gui.hub.install')).toBeTruthy()
    expect(screen.queryByText('gui.hub.installed')).toBeNull()
    expect(screen.queryByText('gui.hub.working')).toBeNull()
  })

  it('lists what is installed behind the back bar, with a use action', async () => {
    const { shellCalls } = install([], {}, [
      { id: 'sk1', name: 'house-style', one: 'the house voice', src: 'skillhub', hub: true, hubId: 'sh-hs' },
    ])
    await mount()
    await act(async () => {
      store.toggleView()
    })
    expect(await screen.findByText('house-style')).toBeTruthy()
    expect(screen.getByText('← gui.plug.back')).toBeTruthy()
    await act(async () => {
      screen.getByText('gui.hub.use').click()
    })
    expect(shellCalls).toContainEqual(['useInTask', 'gui.hub.use_prompt:house-style'])
  })

  it('offers removal on the installed card itself, not only inside the drawer', async () => {
    /* The reported break: the card's one visible action was "use", and nothing
       said the card opened a drawer, so a reader who wanted the skill gone had
       no way to see one and went to the CLI. */
    const { calls } = install([], {}, [
      { id: 'sk1', name: 'house-style', one: 'the house voice', src: 'skillhub', hub: true, hubId: 'sh-hs' },
    ])
    await mount()
    await act(async () => {
      store.toggleView()
    })

    await act(async () => {
      screen.getByText('gui.plug.uninstall').click()
    })
    expect(calls).not.toContain('remove')

    await act(async () => {
      screen.getByText('gui.plug.confirm_remove').click()
    })
    expect(calls).toContain('remove')
  })

  it('lets the keyboard finish the two-press removal without opening the drawer', async () => {
    /* Enter on the button reaches the card's own handler first: keydown bubbles,
       and the click that stops propagation is synthesised after it. Unguarded,
       the first press navigated to the drawer, so the second press had nothing
       to confirm and removal was unreachable from the keyboard. */
    const { calls } = install([], {}, [
      { id: 'sk1', name: 'house-style', one: 'the house voice', src: 'skillhub', hub: true, hubId: 'sh-hs' },
    ])
    await mount()
    await act(async () => {
      store.toggleView()
    })

    const arm = screen.getByText('gui.plug.uninstall')
    await act(async () => {
      arm.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    })
    expect(calls).not.toContain('openDetail')

    await act(async () => {
      screen.getByText('gui.plug.uninstall').click()
    })
    await act(async () => {
      screen.getByText('gui.plug.confirm_remove').click()
    })
    expect(calls).toContain('remove')
  })

  it('offers no removal for a builtin, which has no bundle to delete', async () => {
    install([], {}, [{ id: 'sk2', name: 'weather', one: 'the forecast', src: 'builtin' }])
    await mount()
    await act(async () => {
      store.toggleView()
    })
    expect(await screen.findByText('weather')).toBeTruthy()
    expect(screen.queryByText('gui.plug.uninstall')).toBeNull()
  })

  it('keeps the drawer shut when the remove on the card is pressed', async () => {
    /* The button sits inside a card that is itself a button; without the
       propagation guard the first press both arms the remove and opens the
       drawer over it. */
    install([], {}, [
      { id: 'sk1', name: 'house-style', one: 'the house voice', src: 'skillhub', hub: true, hubId: 'sh-hs' },
    ])
    await mount()
    await act(async () => {
      store.toggleView()
    })
    await act(async () => {
      screen.getByText('gui.plug.uninstall').click()
    })
    expect(document.getElementById('detail')!.dataset.open).not.toBe('true')
  })

  it('shows the empty-installed note when nothing is installed', async () => {
    install([])
    await mount()
    await act(async () => {
      store.toggleView()
    })
    expect(await screen.findByText('gui.hub.empty_installed')).toBeTruthy()
  })

  it('says the read failed, not that nothing is installed, when the source never loaded', async () => {
    /* The two states look identical on the wire -- both are an empty array --
       and only the source knows which it is. A socket that was down at boot
       used to render "nothing installed" over a machine with thirty skills. */
    install([], { loaded: () => false })
    await mount()
    await act(async () => {
      store.toggleView()
    })
    expect(await screen.findByText('gui.hub.empty_installed_off')).toBeTruthy()
    expect(screen.queryByText('gui.hub.empty_installed')).toBeNull()
  })

  it('opens a card into the shared drawer and fills it from the detail call', async () => {
    install([hubItem()])
    await mount()
    await act(async () => {
      ;(await screen.findByText('code-review-checklist')).click()
    })
    expect(await screen.findByText('the whole story')).toBeTruthy()
    expect(document.getElementById('detail')!.dataset.open).toBe('true')
    expect(document.getElementById('dBody')!.textContent).toContain('the whole story')
  })

  it('uninstalls from the drawer only after the arm click, then closes it', async () => {
    const { calls } = install([], {}, [
      { id: 'sk1', name: 'house-style', one: 'the house voice', src: 'skillhub', hub: true },
    ])
    await mount()
    await act(async () => {
      store.toggleView()
    })
    await act(async () => {
      ;(await screen.findByText('house-style')).click()
    })
    /* Scoped to the drawer: the card behind it now carries a remove of its
       own, so an unscoped query matches two. */
    const drawer = within(document.getElementById('detail')!)
    const rm = await drawer.findByText('gui.plug.uninstall')
    await act(async () => {
      rm.click()
    })
    expect(calls).not.toContain('remove')
    await act(async () => {
      drawer.getByText('gui.plug.confirm_remove').click()
    })
    expect(calls).toContain('remove')
    expect(document.getElementById('detail')!.dataset.open).toBe('false')
  })

  it('keeps its rendered shape, hub search', async () => {
    install([hubItem(), hubItem({ id: 'sh-sql', name: 'sql-style', quality_score: 0.7 })])
    await mount()
    await screen.findByText('code-review-checklist')
    expect(domSnapshot(document.getElementById('capsBody')!)).toMatchSnapshot()
  })

  it('keeps its rendered shape, installed list', async () => {
    install([], {}, [
      { id: 'sk1', name: 'house-style', one: 'the house voice', src: 'skillhub', hub: true, hubId: 'sh-hs' },
    ])
    await mount()
    await act(async () => {
      store.toggleView()
    })
    await screen.findByText('house-style')
    expect(domSnapshot(document.getElementById('capsBody')!)).toMatchSnapshot()
  })
})
