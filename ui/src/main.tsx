import { createRoot } from 'react-dom/client'

import { CronApp } from './features/cron/CronPage'
import { cronExprHuman, cronWhen } from './features/cron/humanize'
import * as cron from './features/cron/store'

/* The island bundle. Assembled ahead of the legacy script by ui/build.py, so
 * everything published here exists by the time the shell's shims and the
 * fixture sources evaluate. The bundle itself only reads the shell lazily
 * (see shell/bridge.ts): at this point window.RavenShell does not exist yet.
 */

declare global {
  interface Window {
    cronExprHuman?: typeof cronExprHuman
    cronWhen?: typeof cronWhen
  }
}

/* Still called by name from the legacy layers: the live source's cronToRow
   and the fixture source's save build their `when` prose through these. */
window.cronExprHuman = cronExprHuman
window.cronWhen = cronWhen

window.RavenIslands = {
  ...(window.RavenIslands || {}),
  cron: {
    open: cron.open,
    close: cron.close,
    refresh: cron.refresh,
    warm: cron.warm,
    redraw: cron.langRedraw,
  },
}

const host = document.getElementById('cronBody')
if (host) createRoot(host).render(<CronApp />)
