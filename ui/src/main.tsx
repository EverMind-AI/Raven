import { createElement } from 'react'
import { createRoot } from 'react-dom/client'

import * as composer from './features/composer/mount'
import { ConnApp } from './features/connections/ConnPage'
import * as connections from './features/connections/store'
import * as browser from './features/browser/mount'
import { installLinkTrap } from './features/browser/store'
import { CronApp } from './features/cron/CronPage'
import { cronExprHuman, cronWhen } from './features/cron/humanize'
import * as cron from './features/cron/store'
import { MemoryApp } from './features/memory/MemoryPage'
import * as memory from './features/memory/store'
import { RailApp } from './features/rail/RailPage'
import * as rail from './features/rail/store'
import { plainTitle } from './features/rail/title'
import { SkillsApp } from './features/skills/SkillsPage'
import * as skills from './features/skills/store'
import * as subagents from './features/subagents/mount'
import * as subagentsStore from './features/subagents/store'
import * as transcript from './features/transcript/mount'
import { WsApp } from './features/workspace/WorkspacePage'
import * as workspace from './features/workspace/store'
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
    workGlyphSvg?: typeof composer.workGlyphSvg
    plainTitle?: typeof plainTitle
  }
}

/* Still called by name from the legacy layers: the live source's cronToRow
   and the fixture source's save build their `when` prose through these. */
window.cronExprHuman = cronExprHuman
window.cronWhen = cronWhen
/* The working glyph's svg twin, for the dag sheet's nodes: the sheet is still
   legacy and draws its own graph, and one glyph means work in progress
   wherever it is drawn. */
window.workGlyphSvg = composer.workGlyphSvg
/* Same arrangement: the conversation header, the transcript's fork toast,
   the subagents panel and the live overrides all strip titles through it. */
window.plainTitle = plainTitle

/* The prose renderer, called by name from eight legacy render sites: the
   replay (demo/080 x3), history restore (live/040 x3) and the turn machine
   (live/050 x2). Nothing reaches it through RavenShell -- no md verb on that
   bridge -- and an island that needs it imports it (the workspace island's
   file view does), so those eight are the whole list to audit before this
   republish can go. Same mechanism as cronExprHuman and cronWhen above: the
   bundle owns the function, the legacy layers keep calling md(). */
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
  /* What the legacy layers still reach for: wsReset clears the list with the
     session, and the dag sheet (live/240-external-agents.js) opens nodes,
     reads rows and marks the open selection. */
  subagents: {
    draw: subagents.draw,
    detach: subagents.detach,
    reset: subagentsStore.reset,
    refresh: subagentsStore.refresh,
    rows: subagentsStore.rows,
    openRow: subagentsStore.openRow,
    openDagNode: subagentsStore.openDagNode,
    sel: subagentsStore.sel,
  },
  rail: {
    draw: rail.draw,
    skeleton: rail.skeleton,
    markNew: rail.markNew,
    remove: rail.remove,
    rename: rail.rename,
  },
}

/* The transcript's link handler lives with the island now; it arms itself
   only when the installed DS.browser source is the embedded one. */
installLinkTrap()

/* The dock's own listeners -- the field, the send button, the file picker, the
   drop target, the pill. Registered here rather than on the first paint
   because the markup is already in the document; the handlers read the shell
   and DS.composer lazily, which is what makes that safe this early. */
composer.install()

const host = document.getElementById('cronBody')
if (host) createRoot(host).render(<CronApp />)

const memHost = document.getElementById('memBody')
if (memHost) createRoot(memHost).render(<MemoryApp />)
const connHost = document.getElementById('connBody')
if (connHost) createRoot(connHost).render(<ConnApp />)
createRoot(skillsHost).render(<SkillsApp />)

/* The workspace island mounts lazily: #wsBody is shared ground -- the agents
   and browser tabs draw into it through their own island roots, so the
   workspace root exists only while a workspace view is up (see
   workspace/store.draw). */
workspace.setRenderer(() => createElement(WsApp))

window.RavenIslands = {
  ...(window.RavenIslands || {}),
  workspace: {
    draw: workspace.draw,
    redraw: workspace.redraw,
    reset: workspace.reset,
    showFile: workspace.showFile,
    openDir: workspace.openDir,
  },
}

window.RavenIslands = {
  ...(window.RavenIslands || {}),
  /* The composer island: the dock at the bottom of the chat. The shims in
     demo/090-composer.js call these by name, the parked-turn machinery
     (live/060) carries the live clock's anchor through them, and live's send
     takes the staged attachment paths off the tray the same way. */
  composer: {
    goPaint: composer.goPaint,
    drawQueue: composer.drawQueue,
    drawMeter: composer.drawMeter,
    fitField: composer.fitField,
    dockLift: composer.dockLift,
    liveAnchor: composer.liveAnchor,
    setLiveAnchor: composer.setLiveAnchor,
    attsPending: composer.attsPending,
    takeAtts: composer.takeAtts,
  },
  /* The transcript island: the conversation area's renderer. The legacy
     shims (demo/060, demo/070, demo/080) and the live turn machine
     (live/040, live/050, live/230) drive these; the DOM they used to build
     is drawn by the island into a lane host inside #stage or a stage box. */
  transcript: {
    ask: transcript.ask,
    step: transcript.step,
    answer: transcript.answer,
    answerTyped: transcript.answerTyped,
    note: transcript.note,
    qa: transcript.qa,
    status: transcript.status,
    killStatus: transcript.killStatus,
    collapse: transcript.collapse,
    foldRuns: transcript.foldRuns,
    finishTurn: transcript.finishTurn,
    history: transcript.history,
    delivered: transcript.delivered,
    dagFeed: transcript.dagFeed,
    stopStream: transcript.stopStream,
    nudge: transcript.nudge,
    redraw: transcript.redraw,
    agentStage: transcript.agentStage,
  },
}

const listHost = document.getElementById('list')
if (listHost) createRoot(listHost).render(<RailApp />)
