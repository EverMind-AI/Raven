import { createRoot } from 'react-dom/client'

import { ConnApp } from './features/connections/ConnPage'
import * as connections from './features/connections/store'
import { CronApp } from './features/cron/CronPage'
import { cronExprHuman, cronWhen } from './features/cron/humanize'
import * as cron from './features/cron/store'
import { MemoryApp } from './features/memory/MemoryPage'
import * as memory from './features/memory/store'

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
  memory: {
    open: memory.open,
    close: memory.close,
    redraw: memory.redraw,
  },
  connections: {
    open: connections.open,
    close: connections.close,
    redraw: connections.redraw,
    closeDialog: connections.closeDialog,
  },
}

const host = document.getElementById('cronBody')
if (host) createRoot(host).render(<CronApp />)

const memHost = document.getElementById('memBody')
if (memHost) createRoot(memHost).render(<MemoryApp />)
const connHost = document.getElementById('connBody')
if (connHost) createRoot(connHost).render(<ConnApp />)
