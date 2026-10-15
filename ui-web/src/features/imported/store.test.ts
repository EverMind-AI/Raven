// @vitest-environment happy-dom
/* Importing a conversation: the one claim a pick holds from reading the file to
 * dealing with the answer, the confirm sheet a duplicate raises, and what each
 * refusal reads as. The file reader, the gateway and the rail are stood in for,
 * so each stage can be held open while the case asks for a second import.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { RpcError } from '../../rpc/transport'
import * as confirm from '../../state/confirm'
import { resetSources, setSources } from '../../state/sources'

import type { ImportedUpload } from './types'

const rail = vi.hoisted(() => ({
  rows: [] as Array<{ id: string; title: string }>,
  load: vi.fn(),
  open: vi.fn(),
}))
const toast = vi.hoisted(() => vi.fn())
const current = vi.hoisted(() => vi.fn())
const pageShow = vi.hoisted(() => vi.fn())

vi.mock('../rail/source', () => ({
  loadSessions: rail.load,
  sessionsSource: { snapshot: () => ({ rows: rail.rows, cur: null, busy: false }), open: rail.open },
}))
vi.mock('../../state/toast', () => ({ show: toast }))
vi.mock('../../lib/session', () => ({ setCurrent: current }))
vi.mock('../../state/page', () => ({ show: pageShow }))

const store = await import('./store')

interface Deferred<T> {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (err: unknown) => void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (err: unknown) => void
  const promise = new Promise<T>((ok, no) => { resolve = ok; reject = no })
  return { promise, resolve, reject }
}

/* A FileReader whose read finishes when the case says so. */
class HeldReader {
  static made: HeldReader[] = []
  result: string | null = null
  error: unknown = null
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor() { HeldReader.made.push(this) }
  readAsDataURL(): void {}
  finish(b64: string): void { this.result = `data:application/gzip;base64,${b64}`; this.onload?.() }
  fail(): void { this.error = 'disk gone'; this.onerror?.() }
}

const KEY = 'imported:' + 'a'.repeat(32)
const answer = (status: ImportedUpload['status'], key = KEY, title = 'Trip'): ImportedUpload => ({
  status, session_key: key, title, kind: 'export', source_name: 'trip.tar.gz', imported_at: '2026-10-15T00:00:00+00:00',
})

let uploads: Array<{ name: string; b64: string; force: boolean; reply: Deferred<ImportedUpload> }> = []
const upload = vi.fn((name: string, b64: string, force: boolean) => {
  const reply = deferred<ImportedUpload>()
  uploads.push({ name, b64, force, reply })
  return reply.promise
})

const file = (name = 'trip.tar.gz', size = 10): File => ({ name, size }) as File
const flush = async (): Promise<void> => { for (let i = 0; i < 5; i += 1) await Promise.resolve() }

beforeEach(() => {
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  setSources({ imported: { upload } })
  HeldReader.made = []
  vi.stubGlobal('FileReader', HeldReader)
  uploads = []
  rail.rows = [{ id: KEY, title: 'Trip' }]
  rail.load.mockReset().mockResolvedValue(undefined)
  rail.open.mockReset().mockResolvedValue(undefined)
  toast.mockReset()
  current.mockReset()
  pageShow.mockReset()
  upload.mockClear()
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  store._resetForTests()
  confirm._resetForTests()
  resetSources()
  resetTranslator()
})

describe('importing a package', () => {
  it('refuses a file over the upload limit before reading it', async () => {
    await store.importFile(file('huge.tar.gz', 200 * 1024 * 1024))
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('gui.att.too_big'))
    expect(HeldReader.made).toHaveLength(0)
    expect(upload).not.toHaveBeenCalled()
    expect(store.get().busy).toBeNull()
  })

  it('holds the claim from the read to the opened conversation', async () => {
    const done = store.importFile(file())
    expect(store.get().busy).toBe('trip.tar.gz')
    HeldReader.made[0]!.finish('QUJD')
    await flush()
    expect(uploads.map((u) => [u.name, u.b64, u.force])).toEqual([['trip.tar.gz', 'QUJD', false]])
    expect(store.get().busy).toBe('trip.tar.gz')
    uploads[0]!.reply.resolve(answer('imported'))
    await done
    expect(rail.load).toHaveBeenCalledTimes(1)
    expect(pageShow).toHaveBeenCalledWith(null)
    expect(current).toHaveBeenCalledWith(KEY)
    expect(rail.open).toHaveBeenCalledWith(rail.rows[0])
    expect(toast).toHaveBeenCalledWith(`gui.imported.done ${JSON.stringify({ title: 'Trip' })}`)
    expect(store.get().busy).toBeNull()
  })

  it('starts nothing else while a file is still being read', async () => {
    const make = vi.spyOn(document, 'createElement')
    const done = store.importFile(file())
    store.pick()
    expect(make).not.toHaveBeenCalledWith('input')
    await store.importFile(file('other.tar.gz'))
    expect(HeldReader.made).toHaveLength(1)
    expect(upload).not.toHaveBeenCalled()
    HeldReader.made[0]!.finish('QUJD')
    await flush()
    uploads[0]!.reply.resolve(answer('imported'))
    await done
    expect(upload).toHaveBeenCalledTimes(1)
    expect(store.get().busy).toBeNull()
    make.mockRestore()
  })

  it('lets go and says so when the file cannot be read', async () => {
    const done = store.importFile(file())
    HeldReader.made[0]!.fail()
    await done
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('gui.imported.err_failed'))
    expect(upload).not.toHaveBeenCalled()
    expect(store.get().busy).toBeNull()
    const again = store.importFile(file())
    HeldReader.made[1]!.finish('QUJD')
    await flush()
    uploads[0]!.reply.resolve(answer('imported'))
    await again
    expect(upload).toHaveBeenCalledTimes(1)
  })

  it('starts nothing else while the upload is out, on either door', async () => {
    const done = store.importFile(file())
    HeldReader.made[0]!.finish('QUJD')
    await flush()
    await store.importFile(file('other.tar.gz'))
    await store.importAgain('other.tar.gz', 'WFla')
    expect(HeldReader.made).toHaveLength(1)
    expect(upload).toHaveBeenCalledTimes(1)
    expect(toast).toHaveBeenCalledWith('gui.imported.err_busy')
    expect(store.get().busy).toBe('trip.tar.gz')
    uploads[0]!.reply.resolve(answer('imported'))
    await done
    expect(store.get().busy).toBeNull()
  })

  it('opens a picker for one package with the accepted kinds', () => {
    const input = document.createElement('input')
    const click = vi.spyOn(input, 'click').mockImplementation(() => {})
    const make = vi.spyOn(document, 'createElement').mockReturnValue(input)
    store.pick()
    expect(input.type).toBe('file')
    expect(input.accept).toBe(store.ACCEPT)
    expect(click).toHaveBeenCalledTimes(1)
    make.mockRestore()
  })
})

describe('a package imported before', () => {
  async function duplicate(): Promise<void> {
    const done = store.importFile(file())
    HeldReader.made[0]!.finish('QUJD')
    await flush()
    uploads[0]!.reply.resolve(answer('duplicate', KEY, 'Earlier trip'))
    await done
  }

  it('asks whether to open the copy there or import another', async () => {
    const ask = vi.spyOn(confirm, 'ask')
    await duplicate()
    expect(ask).toHaveBeenCalledTimes(1)
    const [title, body, label, , tone, alt] = ask.mock.calls[0]!
    expect([title, body, label, tone]).toEqual([
      'gui.imported.dup_title',
      `gui.imported.dup_body ${JSON.stringify({ title: 'Earlier trip' })}`,
      'gui.imported.dup_open',
      'primary',
    ])
    expect(alt?.label).toBe('gui.imported.dup_again')
    expect(store.get().busy).toBeNull()
    expect(rail.open).not.toHaveBeenCalled()
  })

  it('opens the copy there', async () => {
    const ask = vi.spyOn(confirm, 'ask')
    await duplicate()
    ask.mock.calls[0]![3]()
    await flush()
    expect(current).toHaveBeenCalledWith(KEY)
    expect(rail.open).toHaveBeenCalledTimes(1)
    expect(upload).toHaveBeenCalledTimes(1)
  })

  it('imports a second copy through the same claim', async () => {
    const ask = vi.spyOn(confirm, 'ask')
    await duplicate()
    const second = 'imported:' + 'b'.repeat(32)
    rail.rows.push({ id: second, title: 'Trip' })
    ask.mock.calls[0]![5]!.fn()
    expect(store.get().busy).toBe('trip.tar.gz')
    await flush()
    expect(uploads[1]!.force).toBe(true)
    expect(uploads[1]!.b64).toBe('QUJD')
    uploads[1]!.reply.resolve(answer('imported', second))
    await flush()
    await flush()
    expect(current).toHaveBeenLastCalledWith(second)
    expect(store.get().busy).toBeNull()
  })
})

describe('what a refusal reads as', () => {
  const cases: Array<[unknown, string]> = [
    [new RpcError(-32031, 'imported_busy'), 'gui.imported.err_busy'],
    [new RpcError(-32030, 'imported_package_invalid', { reason: 'too_large' }), 'gui.imported.err_too_large'],
    [new RpcError(-32030, 'imported_package_invalid', { reason: 'not_importable' }), 'gui.imported.err_not_importable'],
    [new RpcError(-32030, 'imported_package_invalid', { reason: 'unsupported_version' }), 'gui.imported.err_version'],
    [new RpcError(-32030, 'imported_package_invalid', { reason: 'contents_mismatch', detail: 'tampered' }), 'gui.imported.err_invalid'],
    [new Error('socket closed'), 'gui.imported.err_failed'],
  ]

  it.each(cases)('%s', async (err, key) => {
    const done = store.importFile(file())
    HeldReader.made[0]!.finish('QUJD')
    await flush()
    uploads[0]!.reply.reject(err)
    await done
    expect(toast).toHaveBeenCalledTimes(1)
    expect(toast.mock.calls[0]![0]).toMatch(new RegExp(`^${key.replace(/\./g, '\\.')}`))
    expect(store.get().busy).toBeNull()
    expect(rail.open).not.toHaveBeenCalled()
  })

  it('carries the gateway sentence for a damaged package', async () => {
    const done = store.importFile(file())
    HeldReader.made[0]!.finish('QUJD')
    await flush()
    uploads[0]!.reply.reject(new RpcError(-32030, 'x', { reason: 'bad_member_name', detail: 'unsafe member name' }))
    await done
    expect(toast.mock.calls[0]![0]).toContain('unsafe member name')
  })
})

describe('opening what was imported', () => {
  /* The whole import, through to the answer being dealt with. */
  async function imported(status: ImportedUpload['status'] = 'imported'): Promise<void> {
    const done = store.importFile(file())
    HeldReader.made[0]!.finish('QUJD')
    await flush()
    uploads[0]!.reply.resolve(answer(status))
    await done
  }

  it('says the package is in when the list cannot be read to open it, and only that', async () => {
    rail.load.mockRejectedValue(new Error('socket closed'))
    await imported()
    expect(toast.mock.calls.map((c) => c[0])).toEqual([`gui.imported.done_unopened ${JSON.stringify({ title: 'Trip' })}`])
    expect(rail.open).not.toHaveBeenCalled()
    expect(store.get().busy).toBeNull()
  })

  it('says the package is in when its row is not listed, without also saying it failed', async () => {
    rail.rows = []
    await imported()
    expect(toast.mock.calls.map((c) => c[0])).toEqual([`gui.imported.done_unopened ${JSON.stringify({ title: 'Trip' })}`])
    expect(store.get().busy).toBeNull()
  })

  it('says so when opening fails partway', async () => {
    rail.open.mockRejectedValue(new Error('resume failed'))
    await imported()
    expect(toast.mock.calls.map((c) => c[0])).toEqual([`gui.imported.done_unopened ${JSON.stringify({ title: 'Trip' })}`])
  })

  it('says a copy already there could not be opened, and nothing else', async () => {
    const ask = vi.spyOn(confirm, 'ask')
    await imported('duplicate')
    rail.load.mockRejectedValue(new Error('socket closed'))
    ask.mock.calls[0]![3]()
    await flush()
    await flush()
    expect(toast.mock.calls.map((c) => c[0])).toEqual([`gui.imported.open_failed ${JSON.stringify({ title: 'Trip' })}`])
  })

  it('says a second copy is in when it cannot be opened', async () => {
    const ask = vi.spyOn(confirm, 'ask')
    await imported('duplicate')
    rail.load.mockRejectedValue(new Error('socket closed'))
    ask.mock.calls[0]![5]!.fn()
    await flush()
    uploads[1]!.reply.resolve(answer('imported', 'imported:' + 'b'.repeat(32)))
    await flush()
    await flush()
    expect(toast.mock.calls.map((c) => c[0])).toEqual([`gui.imported.done_unopened ${JSON.stringify({ title: 'Trip' })}`])
    expect(store.get().busy).toBeNull()
  })

  it('answers whether it opened', async () => {
    expect(await store.openImported(KEY)).toBe(true)
    expect(await store.openImported('imported:' + '0'.repeat(32))).toBe(false)
    rail.load.mockRejectedValue(new Error('socket closed'))
    expect(await store.openImported(KEY)).toBe(false)
  })
})
