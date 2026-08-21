import { createElement } from 'react'
import { createRoot } from 'react-dom/client'

import * as composer from './features/composer/mount'
import * as dag from './features/dag/graph'
import * as dagNodes from './features/dag/nodes'
import { ConnApp } from './features/connections/ConnPage'
import * as connections from './features/connections/store'
import * as browser from './features/browser/mount'
import { installLinkTrap } from './features/browser/store'
import { CronApp } from './features/cron/CronPage'
import { cronExprHuman, cronWhen } from './features/cron/humanize'
import * as cron from './features/cron/store'
import { ModelPickerApp } from './features/model/ModelPicker'
import * as modelPicker from './features/model/store'
import { MemoryApp } from './features/memory/MemoryPage'
import * as memory from './features/memory/store'
import { PlugApp } from './features/plugins/PluginsPage'
import * as plugins from './features/plugins/store'
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
import { XaApp } from './features/xa/XaPage'
import * as xa from './features/xa/store'
import { SettingsApp } from './features/settings/SettingsPage'
import * as settings from './features/settings/store'
import * as banner from './shell/banner'
import * as chips from './shell/chips'
import * as find from './shell/find'
import * as foot from './shell/foot'
import * as lightbox from './shell/lightbox'
import * as navfly from './shell/navfly'
import * as panes from './shell/panes'
import * as perm from './shell/perm'
import { md } from './shell/prose'
import * as scrollbars from './shell/scrollbars'
import { toggle as toggleTheme } from './shell/theme'

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
    toggleTheme?: typeof toggleTheme
    toggleFind?: typeof find.toggle
    drawBanner?: typeof banner.draw
    setMemFault?: typeof banner.setFault
    openImage?: typeof lightbox.open
    closeImage?: typeof lightbox.close
    openModelPicker?: typeof modelPicker.open
    drawPerm?: typeof perm.draw
    togglePerm?: typeof perm.toggle
    closePermPop?: typeof perm.close
    paneLoad?: typeof panes.load
    drawFoot?: typeof foot.draw
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
/* The shell chrome's own names, called from the boot sequence (paneLoad) and
   from the live layer (drawFoot, on a language flip and once the running
   version has landed). toggleTheme has no caller in the page today; it stays
   published because the name is the shell's one door between the two themes.
   toggleFind does have one: the chrome's Cmd+F handler opens the row after
   showing the rail, and that handler stays legacy for now. */
window.toggleTheme = toggleTheme
window.toggleFind = find.toggle
window.paneLoad = panes.load
window.drawFoot = foot.draw
/* The banner and the lightbox keep their legacy names because their callers
   are spread across layers this migration has not reached: drawBanner from the
   turn machine and four page layers, setMemFault from the live memory.health
   event, and openImage/closeImage from the chrome's Escape chain. Same shape as
   drawFoot -- the name is the door, the module behind it moved. */
window.drawBanner = banner.draw
window.setMemFault = banner.setFault
window.openImage = lightbox.open
window.closeImage = lightbox.close
/* The model picker's opener. Two callers, both still legacy: the composer's
   model chip, and the settings source's pickModel door (which the settings
   island asks for, since the picker is one popover over the whole page rather
   than a page's own control). */
window.openModelPicker = modelPicker.open
/* The permission chip's three names. drawPerm has three callers, all in layers
   this migration has not reached: the boot sequence (demo/160), and the language
   flip on each side (demo/130's setLangShim, live/120's redrawAll). togglePerm
   and closePermPop are the chip's click and the document's click-away. */
window.drawPerm = perm.draw
window.togglePerm = perm.toggle
window.closePermPop = perm.close

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

/* The plugins island renders into a host node it owns the same way: the
   tab chrome (demo/153-plugins.js) re-appends it on every plugin draw. */
const plugHost = document.createElement('div')

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
  /* Not a React island either, and not a renderer at all: the dag panel's
     geometry and its two summary lines. The graph itself is still drawn by
     live/240-external-agents.js, which reads these -- the layout walkers and
     the sentences are where this domain's edge cases live (a cycle, a fan-out
     that has to read as a diamond, a summary counting more nodes than the
     graph holds), and inside the live layer none of it was reachable from a
     test. */
  dag: {
    layout: dag.layout,
    gist: dag.gist,
    summary: dag.summary,
    took: dag.took,
    /* The status glyphs, so the sheet and the transcript's own dag card draw one
       alphabet rather than each keeping a copy of these four paths. */
    MARKS: dag.MARKS,
    /* And the adapter that turns a `dag.run_started` payload into nodes, so the
       sheet and the card agree on what one is. */
    fromStarted: dagNodes.fromStarted,
    W: dag.W,
    H: dag.H,
    GAP_X: dag.GAP_X,
    GAP_Y: dag.GAP_Y,
    PAD: dag.PAD,
  },
  /* Not a React island: the nav flyout is a writer (see shell/navfly.ts). It
     rides the same bag because the bag is simply what the legacy shell reaches
     the bundle through, island or not. */
  nav: {
    draw: navfly.draw,
    toggle: navfly.toggle,
  },
  plugins: {
    host: plugHost,
    view: plugins.view,
    redraw: plugins.redraw,
    reset: plugins.reset,
    drawerClosed: plugins.drawerClosed,
    setQuery: plugins.setQuery,
    searchIfIdle: plugins.searchIfIdle,
    toggleView: plugins.toggleView,
    toggleMcp: plugins.toggleMcp,
    installedCount: plugins.installedCount,
    event: plugins.onEvent,
  },
  xa: {
    open: xa.open,
    close: xa.close,
    redraw: xa.redraw,
  },
  settings: {
    open: settings.open,
    redraw: settings.redraw,
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
/* The chrome that installs itself. All five wire listeners over the static
   markup, which is already parsed by the time this bundle runs: the page script
   below is the LAST thing in the body. Installing here rather than from the
   shell keeps each module's wiring next to the behaviour it belongs to. chips
   is the one that binds nothing static -- it delegates off the document,
   because the prose it acts on is replaced with every answer. */
scrollbars.install()
panes.install()
navfly.install()
find.install()
chips.install()

/* The model picker renders nothing until asked. One root at the body rather
   than a host inside a page: the popover is anchored to whatever button opened
   it -- the composer chip or a settings row -- and belongs to neither. The
   wrapper is inert for layout; .mpick is position: fixed. */
const pickHost = document.createElement('div')
document.body.appendChild(pickHost)
createRoot(pickHost).render(<ModelPickerApp />)

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
     demo/090-composer.js call these by name, and the parked-turn machinery
     (live/060) carries the live clock's anchor through them. The tray is no
     longer reachable from out here: live's send used to take the staged paths
     off it, and the island folds them into the message itself now. */
  composer: {
    goPaint: composer.goPaint,
    drawQueue: composer.drawQueue,
    drawMeter: composer.drawMeter,
    fitField: composer.fitField,
    dockLift: composer.dockLift,
    liveAnchor: composer.liveAnchor,
    setLiveAnchor: composer.setLiveAnchor,
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
createRoot(plugHost).render(<PlugApp />)
const xaHost = document.getElementById('xaBody')
if (xaHost) createRoot(xaHost).render(<XaApp />)
const setHost = document.getElementById('spanels')
if (setHost) createRoot(setHost).render(<SettingsApp />)
