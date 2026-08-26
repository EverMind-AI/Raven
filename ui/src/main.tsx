import { createElement } from 'react'
import { createRoot } from 'react-dom/client'

import * as composer from './features/composer/mount'
import * as approve from './features/composer/approve'
import * as clarify from './features/composer/clarify'
import * as sheets from './features/composer/sheets'
import * as dagNodes from './features/dag/nodes'
import * as dagSheet from './features/dag/mount'
import { ConnApp } from './features/connections/ConnPage'
import * as connections from './features/connections/store'
import * as browser from './features/browser/mount'
import { installLinkTrap } from './features/browser/store'
import { CronApp } from './features/cron/CronPage'
import { cronExprHuman, cronWhen } from './features/cron/humanize'
import * as cron from './features/cron/store'
import { ModelPickerApp } from './features/model/ModelPicker'
import * as modelPicker from './features/model/store'
import { OnboardApp } from './features/onboard/OnboardPage'
import * as onboard from './features/onboard/store'
import { MemoryApp } from './features/memory/MemoryPage'
import { KnowledgeApp } from './features/knowledge/KnowledgePage'
import * as knowledge from './features/knowledge/store'
import * as memory from './features/memory/store'
import { PlugApp } from './features/plugins/PluginsPage'
import * as plugins from './features/plugins/store'
import { RailApp } from './features/rail/RailPage'
import * as rail from './features/rail/store'
import { plainTitle } from './features/rail/title'
import { Skeleton as SkillsSkeleton, SkillsApp } from './features/skills/SkillsPage'
import * as skills from './features/skills/store'
import * as subagents from './features/subagents/mount'
import * as subagentsStore from './features/subagents/store'
import * as transcript from './features/transcript/mount'
import * as transcriptTail from './features/transcript/tail'
import { WsApp } from './features/workspace/WorkspacePage'
import { DeskApp } from './features/workspace/DeskPage'
import * as desk from './features/workspace/DeskPage'
import * as workspace from './features/workspace/store'
import * as workspaceHunks from './features/workspace/hunks'
import { XaApp } from './features/xa/XaPage'
import * as xa from './features/xa/store'
import { SettingsApp } from './features/settings/SettingsPage'
import * as settings from './features/settings/store'
import * as banner from './shell/banner'
import * as chips from './shell/chips'
import * as ctxchip from './shell/ctxchip'
import * as find from './shell/find'
import * as failureWriter from './shell/failure'
import * as foot from './shell/foot'
import * as lightbox from './shell/lightbox'
import * as look from './shell/look'
import * as menuWriter from './shell/menu'
import * as navfly from './shell/navfly'
import * as notifications from './shell/notifications'
import * as urlAction from './shell/open-url'
import * as panes from './shell/panes'
import * as perm from './shell/perm'
import { md } from './shell/prose'
import * as resume from './shell/resume'
import * as scrollbars from './shell/scrollbars'
import * as session from './shell/session'
import { toggle as toggleTheme } from './shell/theme'
import * as toastWriter from './shell/toast'
import * as upgradeWriter from './shell/upgrade'

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
    lookLoad?: typeof look.load
    ntfPush?: typeof notifications.show
    toggleFind?: typeof find.toggle
    drawBanner?: typeof banner.draw
    setMemFault?: typeof banner.setFault
    closeImage?: typeof lightbox.close
    openModelPicker?: typeof modelPicker.open
    drawPerm?: typeof perm.draw
    togglePerm?: typeof perm.toggle
    closePermPop?: typeof perm.close
    paneLoad?: typeof panes.load
    drawFoot?: typeof foot.draw
    drawCtx?: typeof ctxchip.draw
    setCtx?: typeof ctxchip.set
    toast?: typeof toastWriter.show
    menuAt?: typeof menuWriter.show
    sessionCurrent?: typeof session.current
    sessionSet?: typeof session.setCurrent
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
/* Boot restores appearance through this name; a live turn raises its desktop
   notice through the other. The callers remain concat code, but both stores
   and all of their decisions live in the modern modules now. */
window.lookLoad = look.load
window.ntfPush = notifications.show
window.toggleFind = find.toggle
window.paneLoad = panes.load
window.drawFoot = foot.draw
/* The banner and the lightbox keep their legacy names because their callers
   are spread across layers this migration has not reached: drawBanner from the
   turn machine and four page layers, setMemFault from the live memory.health
   event, and closeImage from the chrome's Escape chain -- one line that finds
   the overlay by class and closes it. Same shape as drawFoot: the name is the
   door, the module behind it moved.
   Only closeImage. The chain never opens one, and both islands that do
   (the composer's tray, the transcript's attachment chips) import lightbox.open
   directly, which is what shell/lightbox.ts's header says they should. */
window.drawBanner = banner.draw
window.setMemFault = banner.setFault
window.closeImage = lightbox.close
/* The model picker's opener. Two callers, both still legacy: the composer's
   model chip, and the settings source's pickModel door (which the settings
   island asks for, since the picker is one popover over the whole page rather
   than a page's own control). */
window.openModelPicker = modelPicker.open
/* The permission chip's three names. drawPerm has three callers, all in layers
   this migration has not reached: the boot sequence (demo/160), and the language
   flip on each side (demo/130's langPickDemo, live/120's redrawAll). togglePerm
   and closePermPop are the chip's click and the document's click-away. */
window.drawPerm = perm.draw
window.togglePerm = perm.toggle
window.closePermPop = perm.close
/* The context ring's two names. Both have callers on both sides: setCtx from
   each layer's turn bookkeeping (demo's replay, live's message.complete), and
   drawCtx from the boot sequence and each side's language flip -- the ring's
   tooltip is a translated string, so a flip has to redraw it. */
window.drawCtx = ctxchip.draw
window.setCtx = ctxchip.set
/* Two chrome writers reached from both legacy layers and modern islands. The
   old names remain the concat layers' door; RavenShell late-binds those same
   names for islands until the bridge calls are retired on Axis 2. */
window.toast = toastWriter.show
window.menuAt = menuWriter.show
window.sessionCurrent = session.current
window.sessionSet = session.setCurrent

session.onChange(() => {
  sheets.sync()
  dagSheet.sync()
  /* The desk palette is open or shut per conversation, and this is the event
     that says which one is on screen -- see deskStore.sync. */
  desk.sync()
  rail.draw()
})
find.onChange(rail.draw)

/* The prose renderer, called by name from eight legacy render sites: the
   replay (demo/080 x3), history restore (live/040 x3) and the turn machine
   (live/050 x2). Nothing reaches it through RavenShell -- no md verb on that
   bridge -- and an island that needs it imports it (the workspace island's
   file view does), so those eight are the whole list to audit before this
   republish can go. Same mechanism as cronExprHuman and cronWhen above: the
   bundle owns the function, the legacy layers keep calling md(). */
window.md = md

installLinkTrap()

/* The skills island renders into a host node the legacy shim re-attaches
   under #capsBody on every skill-tab draw: the plugin tab clears that box
   with innerHTML, which must never tear down nodes React owns. */
const skillsHost = document.createElement('div')
const skillsSkeletonHost = document.createElement('div')
skillsSkeletonHost.className = 'hubgrid'

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
    skeleton: skillsSkeletonHost,
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
    directEvent: subagentsStore.directEvent,
    sel: subagentsStore.sel,
  },
  rail: {
    draw: rail.draw,
    hold: rail.hold,
    release: rail.release,
    markNew: rail.markNew,
    remove: rail.remove,
    rename: rail.rename,
    reconcile: rail.reconcileRows,
    removeRow: rail.removeSessionRow,
  },
  /* Not a React island either, and not a renderer at all: the dag panel's
     geometry and its two summary lines. The graph itself is still drawn by
     live/240-external-agents.js, which reads these -- the layout walkers and
     the sentences are where this domain's edge cases live (a cycle, a fan-out
     that has to read as a diamond, a summary counting more nodes than the
     graph holds), and inside the live layer none of it was reachable from a
     test. */
  dag: {
    /* The adapter that turns a `dag.run_started` payload into nodes, so the
       sheet and the transcript's card agree on what a node is. */
    fromStarted: dagNodes.fromStarted,
    /* The sheet itself, now that it is drawn here rather than in the live layer.
       Four calls: a run arrives, a run was mutated in place, the open
       conversation changed, a conversation went away -- plus one read, for the
       delegation row that opens the run's last node. The geometry, the marks and
       the summary lines are no longer published: their only caller was the
       imperative builder that this replaces. */
    start: dagSheet.start,
    /* A node reported, and the run had its last word. Both were written out by
       hand in the live layer against the run's node map, which is how the
       completion branch came to invent an end stamp for a node that never sent
       one -- and how a node that finished early came to read as having taken the
       whole graph. */
    advance: dagSheet.advance,
    settle: dagSheet.settle,
    touch: dagSheet.touch,
    sync: dagSheet.sync,
    forget: dagSheet.forget,
    run: dagSheet.run,
  },
  /* Not an island either: one verb, spent when a conversation is opened, that
     puts back the sheet and the desk that conversation had before the page was
     replaced (see shell/resume.ts). It reaches across three stores and the
     transcript's own `dag.get` seam, which is why it is not any of theirs. */
  view: {
    resume: resume.resume,
    landing: resume.landing,
    /* Started by the live layer once the pointer is real; see shell/resume.ts. */
    watch: resume.watch,
  },
  /* Not a React island: the nav flyout is a writer (see shell/navfly.ts). It
     rides the same bag because the bag is simply what the legacy shell reaches
     the bundle through, island or not. */
  nav: {
    draw: navfly.draw,
    toggle: navfly.toggle,
  },
  /* One appended node per call, so these are writers rather than islands. The
     concat layers keep the transport and boot decisions that ask for them. */
  chrome: {
    failureBar: failureWriter.show,
    bootError: failureWriter.bootError,
    upShade: upgradeWriter.open,
    openUrl: urlAction.open,
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
    openMarket: (id: string) => plugins.openDetail('market', id),
    toggleMcp: plugins.toggleMcp,
    installedCount: plugins.installedCount,
    event: plugins.onEvent,
  },
  model: {
    current: modelPicker.current,
    setCurrent: modelPicker.setCurrent,
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
  onboard: {
    open: onboard.open,
  },
}

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
menuWriter.install()

/* The model picker renders nothing until asked. One root at the body rather
   than a host inside a page: the popover is anchored to whatever button opened
   it -- the composer chip or a settings row -- and belongs to neither. The
   wrapper is inert for layout; .mpick is position: fixed. */
const pickHost = document.createElement('div')
document.body.appendChild(pickHost)
createRoot(pickHost).render(<ModelPickerApp />)

const onboardHost = document.getElementById('onb')
if (onboardHost) createRoot(onboardHost).render(<OnboardApp />)

const host = document.getElementById('cronBody')
if (host) createRoot(host).render(<CronApp />)

const memHost = document.getElementById('memBody')
if (memHost) createRoot(memHost).render(<MemoryApp />)
const kbHost = document.getElementById('kbBody')
if (kbHost) createRoot(kbHost).render(<KnowledgeApp />)
const connHost = document.getElementById('connBody')
if (connHost) createRoot(connHost).render(<ConnApp />)
const deskHost = document.createElement('div')
deskHost.id = 'deskHost'
document.body.appendChild(deskHost)
const deskRoot = createRoot(deskHost)
queueMicrotask(() => deskRoot.render(<DeskApp />))
createRoot(skillsHost).render(<SkillsApp />)
createRoot(skillsSkeletonHost).render(<>{Array.from({ length: 6 }, (_, i) => <SkillsSkeleton key={i} />)}</>)

/* The workspace island mounts lazily: #wsBody is shared ground -- the agents
   and browser tabs draw into it through their own island roots, so the
   workspace root exists only while a workspace view is up (see
   workspace/store.draw). */
workspace.setRenderer(() => createElement(WsApp))

window.RavenIslands = {
  ...(window.RavenIslands || {}),
  /* Opened from the rail, like memory: the shim in demo/ calls these. */
  knowledge: { open: knowledge.open, close: knowledge.close, redraw: knowledge.redraw },
  workspace: {
    draw: workspace.draw,
    redraw: workspace.redraw,
    reset: () => {
      workspace.reset()
      desk.reset()
    },
    shared: workspace.shared,
    currentTurn: workspace.currentTurn,
    advanceTurn: workspace.advanceTurn,
    snapshot: workspace.snapshot,
    restore: workspace.restore,
    changes: workspace.changes,
    urls: workspace.urls,
    showFile: workspace.showFile,
    loadDeliveries: workspace.loadDeliveries,
    hunkFromEdit: workspaceHunks.fromEdit,
    hunkFromWrite: workspaceHunks.fromWrite,
    hunkFromUnified: workspaceHunks.fromUnified,
    toggleDesk: desk.toggleDesk,
    openDeskTab: desk.openDeskTab,
    openFile: desk.openDeskFile,
    openDiff: desk.openDeskDiff,
    openAgent: desk.openDeskAgent,
    openAgentRecord: desk.openDeskAgentRecord,
    notifyDesk: desk.notifyDesk,
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
    turn: composer.turn,
    goPaint: composer.goPaint,
    drawQueue: composer.drawQueue,
    queuePush: composer.queuePush,
    queueShift: composer.queueShift,
    queueClear: composer.queueClear,
    queueSnapshot: composer.queueSnapshot,
    queueRestore: composer.queueRestore,
    parkDraft: composer.parkDraft,
    loadDraft: composer.loadDraft,
    dropDraft: composer.dropDraft,
    /* One announcement, two owners. The live layer calls this at the moment a
       draft becomes a session, and both the composer's draft text and the
       desk's palette are filed under the draft and have to follow it there --
       see deskStore.claimDraft for why the desk cannot work this out from the
       session pointer on its own. Wrapped here for the same reason
       `workspace.reset` is: the legacy layer says the thing once. */
    claimDraft: (id: string | null) => {
      composer.claimDraft(id)
      desk.claimDraft(id)
    },
    drawMeter: composer.drawMeter,
    fitField: composer.fitField,
    dockLift: composer.dockLift,
    liveAnchor: composer.liveAnchor,
    setLiveAnchor: composer.setLiveAnchor,
    /* The sheet rack, which lives in `.dock` beside the composer card and
       lifts it after every change. Six names rather than a nested object,
       because the layers that call them call them by the names they have had
       all along -- one destructure in demo/040-state.js binds them. */
    sheetSession: sheets.session,
    sheetAdd: sheets.add,
    sheetRemove: sheets.remove,
    sheetDropClass: sheets.dropClass,
    sheetsSync: sheets.sync,
    sheetsForget: sheets.forget,
    /* The rack's tenants. Beside the rack rather than globals of their own:
       the layers that raise one already reach for these six names, and a
       question is one more thing they do to the same rack. Neither speaks to
       the server -- the caller keeps the transport and passes the answer on. */
    approveSheet: approve.open,
    clarifySheet: clarify.open,
    clarifyClose: clarify.close,
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
    turnKept: transcript.turnKept,
    artifacts: transcript.artifacts,
    delivery: transcript.delivery,
    history: transcript.history,
    delivered: transcript.delivered,
    dagFeed: transcript.dagFeed,
    stopStream: transcript.stopStream,
    nudge: transcript.nudge,
    redraw: transcript.redraw,
    agentStage: transcript.agentStage,
    down: transcriptTail.down,
    isStuck: transcriptTail.isStuck,
    setStuck: transcriptTail.setStuck,
  },
}

const listHost = document.getElementById('list')
if (listHost) createRoot(listHost).render(<RailApp />)
createRoot(plugHost).render(<PlugApp />)
const xaHost = document.getElementById('xaBody')
if (xaHost) createRoot(xaHost).render(<XaApp />)
const setHost = document.getElementById('spanels')
if (setHost) createRoot(setHost).render(<SettingsApp />)
