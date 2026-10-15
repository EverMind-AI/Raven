/* Importing a conversation: picking a package, sending it up, and opening what it became.
 *
 * The gateway takes one import at a time and owns it until it ends
 * (`imported.upload`), so the page takes one too: while a package is on its way
 * `busy` names it, the rail's button says so and refuses a second pick. The
 * bytes ride as base64 in one frame, which is why a file over the upload limit
 * is refused here before it is read (lib/upload.ts).
 *
 * A package imported before answers `duplicate` with the copy already there
 * and nothing written. The reader chooses on the confirm sheet: open that copy,
 * or send the same bytes again with `force` for a second one -- through the
 * same claim as a pick, so it waits its turn like one. The bytes are held only
 * by the two answers, so the sheet coming down lets them go.
 *
 * Opening goes the way a click on the rail does (features/rail/RailPage.tsx):
 * the list is read again so the new row is in it, then it becomes current and
 * is opened through the rail's own source.
 */

import { t } from '../../i18n/t'
import { setCurrent } from '../../lib/session'
import { refusalBySize } from '../../lib/upload'
import * as confirm from '../../state/confirm'
import * as page from '../../state/page'
import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'
import { show as toast } from '../../state/toast'
import { loadSessions, sessionsSource } from '../rail/source'
import { BUSY_CODE, PACKAGE_CODE, codeOf, detailOf, reasonOf } from './source'

import type { ImportedSource } from './types'

export interface ImportedState {
  /** The file being imported, null while nothing is. */
  readonly busy: string | null
}

const store = makeStore<ImportedState>({ busy: null })

export const { get, subscribe } = store

export function set(patch: Partial<ImportedState>): void {
  store.set((prev) => ({ ...prev, ...patch }))
}

const source = (): ImportedSource => ds('imported')

/** The packages the picker offers; advisory, since the gateway reads the bytes. */
export const ACCEPT = '.tar.gz,.tgz,application/gzip,application/x-gzip'

/* Every refusal the gateway can give, as a sentence about the file. */
function refusalOf(name: string, err: unknown): string {
  const code = codeOf(err)
  if (code === BUSY_CODE) return t('gui.imported.err_busy')
  if (code === PACKAGE_CODE) {
    const reason = reasonOf(err)
    if (reason === 'too_large') return t('gui.imported.err_too_large', { name })
    if (reason === 'not_importable') return t('gui.imported.err_not_importable', { name })
    if (reason === 'unsupported_version') return t('gui.imported.err_version', { name })
    return t('gui.imported.err_invalid', { name, detail: detailOf(err) })
  }
  return t('gui.imported.err_failed', { name, detail: detailOf(err) })
}

/** Open an imported conversation the way a click on its rail row would; whether it opened.
 *
 * Never throws: by the time this runs the package is imported and kept, so a
 * list that cannot be read or a row that is not in it is a failure to open,
 * not to import, and each caller says so in its own words. */
export async function openImported(key: string): Promise<boolean> {
  try {
    await loadSessions()
    const row = sessionsSource.snapshot().rows.find((r) => r.id === key)
    if (!row) return false
    page.show(null)
    setCurrent(key)
    await sessionsSource.open(row)
    return true
  } catch {
    return false
  }
}

/* The duplicate's "open existing": the copy is there whatever happens here. */
async function openExisting(key: string, title: string): Promise<void> {
  if (!(await openImported(key))) toast(t('gui.imported.open_failed', { title }))
}

/* The one place `busy` is taken and the one place it is let go. An operation
   that claims it holds it until it has done everything -- read the file, sent
   it, dealt with the answer -- so no second read or upload can start under it,
   and no other path can clear it while it runs. */
function claim(name: string): boolean {
  if (get().busy !== null) return false
  set({ busy: name })
  return true
}

function release(): void {
  set({ busy: null })
}

/* Only ever called by an operation holding the claim. */
async function send(name: string, contentB64: string, force: boolean): Promise<void> {
  let result: Awaited<ReturnType<ImportedSource['upload']>>
  try {
    result = await source().upload(name, contentB64, force)
  } catch (err) {
    toast(refusalOf(name, err))
    return
  }
  if (result.status === 'duplicate') {
    const key = result.session_key
    confirm.ask(
      t('gui.imported.dup_title'),
      t('gui.imported.dup_body', { title: result.title }),
      t('gui.imported.dup_open'),
      () => void openExisting(key, result.title),
      'primary',
      { label: t('gui.imported.dup_again'), fn: () => void importAgain(name, contentB64) },
    )
    return
  }
  const opened = await openImported(result.session_key)
  toast(t(opened ? 'gui.imported.done' : 'gui.imported.done_unopened', { title: result.title }))
}

/** The bytes of one file, as the transport wants them. */
function base64Of(file: File): Promise<string> {
  return new Promise((done, fail) => {
    const reader = new FileReader()
    reader.onerror = () => fail(new Error(String(reader.error)))
    /* `readAsDataURL` gives `data:<type>;base64,<payload>`; the payload is
       what the method takes. */
    reader.onload = () => done(String(reader.result).split(',')[1] ?? '')
    reader.readAsDataURL(file)
  })
}

/** One picked file: refused here when it is over the limit, read and sent up otherwise. */
export async function importFile(file: File): Promise<void> {
  const refused = refusalBySize(file.name, file.size)
  if (refused) {
    toast(refused)
    return
  }
  if (!claim(file.name)) return
  try {
    let b64: string
    try {
      b64 = await base64Of(file)
    } catch (err) {
      toast(t('gui.imported.err_failed', { name: file.name, detail: detailOf(err) }))
      return
    }
    await send(file.name, b64, false)
  } finally {
    release()
  }
}

/** The same bytes again, as a second copy of a package imported before. */
export async function importAgain(name: string, contentB64: string): Promise<void> {
  if (!claim(name)) {
    toast(t('gui.imported.err_busy'))
    return
  }
  try {
    await send(name, contentB64, true)
  } finally {
    release()
  }
}

/** The rail's button: a file dialog for one package. */
export function pick(): void {
  if (get().busy !== null) return
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = ACCEPT
  input.addEventListener('change', () => {
    const file = input.files?.[0]
    if (file) void importFile(file)
  })
  input.click()
}

/** Test seam only. */
export function _resetForTests(): void {
  store._resetForTests()
}
