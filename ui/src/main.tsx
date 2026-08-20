import { createRoot } from 'react-dom/client'

import { ConnApp } from './features/connections/ConnPage'
import * as connections from './features/connections/store'
import * as browser from './features/browser/mount'
import { installLinkTrap } from './features/browser/store'
import { CronApp } from './features/cron/CronPage'
import { cronExprHuman, cronWhen } from './features/cron/humanize'
import * as cron from './features/cron/store'
import { MemoryApp } from './features/memory/MemoryPage'
import * as memory from './features/memory/store'
import { SkillsApp } from './features/skills/SkillsPage'
import * as skills from './features/skills/store'
import { md } from './shell/prose'

/* The island bundle. Assembled ahead of the legacy script by ui/build.py, so
 * everything published here exists by the time the shell's shims and the
 * fixture sources evaluate. The bundle itself only reads the shell lazily
 * (see shell/bridge.ts): at this point window.RavenShell does not exist yet.
 */

declare global {
  interface Window {
    cronExprHuman?: typeof cronExprHuman
    cronWhen?: typeof cronWhen
    md?: typeof md
  }
}

/* Still called by name from the legacy layers: the live source's cronToRow
   and the fixture source's save build their `when` prose through these. */
window.cronExprHuman = cronExprHuman
window.cronWhen = cronWhen

/* The prose renderer, called by name from nine legacy render sites: the
   replay (demo/080 x3), history restore (live/040 x3), the turn machine
   (live/050 x2) and the file viewer (live/170). Nothing reaches it through
   RavenShell -- there is no md verb on that bridge -- so those nine are the
   whole list to audit before this republish can go. Same mechanism as
   cronExprHuman and cronWhen above: the bundle owns the function, the legacy
   layers keep calling md(). */
window.md = md

/* The skills island renders into a host node the legacy shim re-attaches
   under #capsBody on every skill-tab draw: the plugin tab clears that box
   with innerHTML, which must never tear down nodes React owns. */
const skillsHost = document.createElement('div')

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
  skills: {
    attach: (box: Element) => box.appendChild(skillsHost),
    redraw: skills.redraw,
    reset: skills.reset,
    dropDrawer: skills.dropDrawer,
    view: skills.view,
    toggleView: skills.toggleView,
    ensureSearch: skills.ensureSearch,
    setQuery: skills.setQuery,
    searchNow: skills.searchNow,
    subscribe: skills.subscribe,
  },
  connections: {
    open: connections.open,
    close: connections.close,
    redraw: connections.redraw,
    closeDialog: connections.closeDialog,
  },
  browser: {
    draw: browser.draw,
    detach: browser.detach,
    hidden: browser.hidden,
  },
}

/* The transcript's link handler lives with the island now; it arms itself
   only when the installed DS.browser source is the embedded one. */
installLinkTrap()

const host = document.getElementById('cronBody')
if (host) createRoot(host).render(<CronApp />)

const memHost = document.getElementById('memBody')
if (memHost) createRoot(memHost).render(<MemoryApp />)
const connHost = document.getElementById('connBody')
if (connHost) createRoot(connHost).render(<ConnApp />)
createRoot(skillsHost).render(<SkillsApp />)
