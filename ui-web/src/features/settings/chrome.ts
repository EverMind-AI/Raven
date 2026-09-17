/* The settings dialog's seam onto the page, and the transports beside it.
 *
 * Page chrome none of the islands own -- the update check the About card's
 * button runs, the permission chip's write-back, and the four sources that
 * answer the dialog, the model list, the tier and the rail banner.
 *
 * Installed once, from src/main.tsx: after the chrome it reaches for, before
 * the boot's own wiring (src/app/install.ts).
 */

import { modelSource, openModelsForMissingProvider, setChipPainter, tierSource } from '../model/source'
import * as chip from '../model/chip'
import { bannerSource, settingsSource, setSettingsChrome } from './source'
import { T } from '../../i18n/t'
import { hasUpdateFlag } from '../../rpc/capabilities'
import { setPermPersister } from '../../state/perm'
import { current as sessionCurrent } from '../../lib/session'
import { show as toast } from '../../state/toast'
import { gateway } from '../../rpc/gateway'
import { pick as langPick } from '../../state/lang/pick'
import { staging } from '../../state/session/staging'
import { sources } from '../../state/sources'
import { APP_VERSION, appVersionSet, askUpgrade, showUpNote } from '../../app/updates'
import { islands } from '../../islands'

import type { ComposerSource } from '../composer/types'

/* The version check the rail-foot notice already does, on demand. No new
   backend: system.version carries the answer. */
export async function checkUpdate(btn: HTMLButtonElement): Promise<void> {
  const was = btn.textContent
  btn.textContent = T('gui.set.checking'); btn.disabled = true
  try {
    /* check:true = fetch now, not the daily cache: the button says check for
       updates, and a person who just clicked it is asking about now. */
    const v = await gateway().call('system.version', { check: true })
    if (v.raven_version) appVersionSet(v.raven_version)
    if (hasUpdateFlag(v)) {
      showUpNote('ver', (v as { latest_version?: string }).latest_version)
      islands.settings.redraw()
      askUpgrade()
      return
    }
    /* The answer has to land on the button: this layer sends toasts to the
       console, and "nothing happened" is indistinguishable from a broken
       check. */
    btn.disabled = false
    btn.textContent = T('gui.set.abt.latest')
    setTimeout(() => { btn.textContent = was }, 2200)
    return
  } catch (e) {
    btn.textContent = T('gui.set.abt.check_fail')
    setTimeout(() => { btn.textContent = was }, 2600)
    if (window.console) console.error('[update check]', e)
  }
  btn.disabled = false
}

export function install(): void {
  /* The chip the provider refresh and the settings default both move. */
  setChipPainter(chip.label)
  setSettingsChrome({
    version: APP_VERSION,
    checkUpdate,
    /* Not awaited: the pick repaints synchronously and the persist speaks for
       itself if it fails. */
    setLang: (v: string) => { void langPick(v as 'en' | 'zh', { persist: true }) },
  })

  sources.banner = bannerSource

  /* The pick's write-back. In a conversation it reaches that conversation only
     (config.set under its session_id; the gate reads it live). A draft has no
     session_id to write under yet, so the pick is staged and applied to the
     session the first message mints -- the shape the model and tier chips take.
     The default is the settings panel's to change. Registered here because this
     module owns the settings transport; the chip asks for it only when a pick
     is made. */
  setPermPersister((m) => {
    const sid = sessionCurrent()
    if (!sid) { staging().perm = m; return true }
    return gateway().call('config.set', { key: 'permissions.mode', value: m, scope: 'session', session_id: sid })
      .then((r) => {
        if (r && r.applied) return true
        toast(T('gui.perm.save_failed'))
        return false
      })
      .catch(() => { toast(T('gui.perm.save_failed')); return false })
  })

  sources.settings = settingsSource
  sources.tier = tierSource
  sources.model = modelSource

  ;(sources.composer as ComposerSource).beforeSend = openModelsForMissingProvider

  chip.install()
}
