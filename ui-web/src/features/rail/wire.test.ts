// @vitest-environment happy-dom
/* What the rail does with the answer to an archive: the row leaves only when
 * the call says the flag reached the disk.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import { loadPart } from '../../../scripts/module-harness.mjs'

interface Row { id: string; title: string }

async function harness(answer: Record<string, unknown>) {
  const log: unknown[][] = []
  const rows: Row[] = [{ id: 'tui:1', title: 'a task' }]
  await loadPart(() => import('./wire'), {
    fakes: {
      'src/i18n/t': { t: (key: string, vars?: unknown) => (vars ? `${key} ${JSON.stringify(vars)}` : key) },
      'src/state/toast': { show: (text: string) => { log.push(['toast', text]) } },
      'src/features/rail/source': {
        setArchived: async (id: string, archived: boolean) => {
          log.push(['setArchived', id, archived])
          return answer
        },
        deleteSession: async () => ({}),
      },
      'src/state/session/rows': {
        rows: () => rows,
        replace: () => {},
        open: () => {},
      },
      'src/features/rail/store': {
        draw: () => { log.push(['draw']) },
        removeSessionRow: (all: Row[], _cur: string | null, id: string) => {
          log.push(['removeRow', id])
          return { kind: 'unchanged', rows: all.filter((r) => r.id !== id) }
        },
      },
      'src/lib/dom': { $: () => null },
      'src/state/session/registry': { forget: () => {}, switchToDraft: async () => { log.push(['left']) } },
      'src/lib/session': { current: () => null, setCurrent: () => {} },
      'src/state/confirm': { ask: () => {} },
      'src/state/sheetRack': { forget: () => {} },
      'src/features/composer/mount': { dropDraft: () => {} },
      'src/features/dag/mount': { forget: () => {} },
      'src/features/settings/store': { redraw: () => {} },
    },
  })
  const wire = await import('./wire')
  return { wire, log }
}

afterEach(() => { vi.restoreAllMocks() })

describe('archiving from the rail', () => {
  it('takes the row off once the flag is on disk', async () => {
    const h = await harness({ archived: true, session_key: 'tui:1', pending: false })
    await h.wire.archive({ id: 'tui:1', title: 'a task' } as never)
    expect(h.log).toContainEqual(['setArchived', 'tui:1', true])
    expect(h.log.some((c) => c[0] === 'toast' && String(c[1]).startsWith('gui.sess.archived'))).toBe(true)
  })

  it('says it failed when the call answers that nothing was persisted', async () => {
    /* `pending` is the call reporting the flag is held in memory for a
       conversation with no transcript yet. Read as a success it took the row
       off the rail and left the reader to meet it again after a reload. */
    const h = await harness({ archived: true, session_key: 'tui:1', pending: true })
    await h.wire.archive({ id: 'tui:1', title: 'a task' } as never)
    expect(h.log).toContainEqual(['setArchived', 'tui:1', true])
    expect(h.log.some((c) => c[0] === 'toast' && String(c[1]).startsWith('gui.sess.archive_failed'))).toBe(true)
    expect(h.log.some((c) => c[0] === 'left')).toBe(false)
  })
})
