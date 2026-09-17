/* The island bag: what the legacy page reaches the React bundle through.
 *
 * One object, assembled once. Some members are islands (a React root the
 * legacy chrome mounts and drives), some are plain writers that append a node
 * per call, and some are neither -- but the bag is simply the door the legacy
 * layers knock on, so island or not, a verb they call lives here.
 *
 * Imported rather than published: the legacy layers import this module by
 * name. The two island stores that used to reach a sibling through it are
 * handed what they need at the foot of this file instead -- the desk imports
 * both of them back, so an import the other way would run its module-scope
 * subscription against a half-built module.
 *
 * The React roots that render into the hosts below are main.tsx's.
 */

import * as approve from './features/composer/approve'
import * as clarify from './features/composer/clarify'
import * as composer from './features/composer/mount'
import * as sheets from './state/sheetRack'
import * as connections from './features/connections/store'
import { open as openConnections } from './features/connections/nav'
import * as browser from './features/browser/mount'
import * as cron from './features/cron/store'
import * as dagSheet from './features/dag/mount'
import * as dagNodes from './features/dag/nodes'
import * as knowledge from './features/knowledge/store'
import * as memory from './features/memory/store'
import * as modelPicker from './features/model/store'
import * as onboard from './features/onboard/store'
import * as playbooks from './features/playbooks/store'
import * as plugins from './features/plugins/store'
import * as rail from './features/rail/store'
import * as skills from './features/skills/store'
import * as subagents from './features/subagents/mount'
import * as subagentsStore from './features/subagents/store'
import * as transcript from './features/transcript/mount'
import * as transcriptTail from './features/transcript/tail'
import * as desk from './features/workspace/DeskPage'
import * as workspaceHunks from './features/workspace/hunks'
import * as workspace from './features/workspace/store'
import * as xa from './features/xa/store'
import * as settings from './features/settings/store'
import * as failureWriter from './shell/failure'
import * as navfly from './shell/navfly'
import * as urlAction from './shell/open-url'
import * as resume from './shell/resume'
import * as upgradeWriter from './shell/upgrade'

/* The skills island renders into a host node the legacy shim re-attaches
   under #capsBody on every skill-tab draw: the plugin tab clears that box
   with innerHTML, which must never tear down nodes React owns. */
export const skillsHost = document.createElement('div')
export const skillsSkeletonHost = document.createElement('div')
skillsSkeletonHost.className = 'hubgrid'

/* The plugins island renders into a host node it owns the same way: the
   tab chrome (demo/153-plugins.js) re-appends it on every plugin draw. */
export const plugHost = document.createElement('div')

export const islands = {
  cron: {
    open: cron.open,
    close: cron.close,
    /* Read by showPage: a page's own overlay closes when the page does. */
    closeSheet: cron.closeSheet,
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
    open: openConnections,
    close: connections.close,
    redraw: connections.redraw,
    closeDialog: connections.closeDialog,
  },
  browser: {
    draw: browser.draw,
    detach: browser.detach,
    hidden: browser.hidden,
  },
  /* What the page still reaches for: wsReset clears the list with the session,
     and the dag panel opens nodes, reads rows and marks the open selection. */
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
    endRename: rail.endRename,
    reconcile: rail.reconcileRows,
    removeRow: rail.removeSessionRow,
  },
  /* The delegated graph. The layout walkers and the sentences are where this
     domain's edge cases live (a cycle, a fan-out that has to read as a
     diamond, a summary counting more nodes than the graph holds), and inside
     the live layer none of it was reachable from a test. */
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
    /* The graph alone. The parked path in the live layer restores a
       conversation from detached DOM and must not replay the desk's opens, but
       its graph still needs re-reading -- see shell/resume.ts. */
    refreshDag: resume.refreshDag,
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
    openModels: settings.openModels,
    openProviderModels: settings.openProviderModels,
    redraw: settings.redraw,
  },
  onboard: {
    open: onboard.open,
  },
  /* Opened from the rail, like memory: the shim in demo/ calls these. */
  knowledge: { open: knowledge.open, close: knowledge.close, redraw: knowledge.redraw },
  /* Same three verbs as knowledge: the rail opens it, Escape closes it, a
     language flip redraws it. */
  playbooks: { open: playbooks.openPage, close: playbooks.closePage, redraw: playbooks.redraw },
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
  /* The composer island: the dock at the bottom of the chat. The shims in
     demo/090-composer.js call these by name, and the residency rule
     (state/session/residency.ts) carries a parked turn's clock anchor through
     them. The tray is no longer reachable from out here: the page's send used
     to take the staged paths off it, and the island folds them into the
     message itself now.

     Most of the rest is carried for stage C: the twenty-one queue, draft and
     sheet verbs below have no reader left now that demo/040-state.js imports
     the island modules directly. */
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
    approvalSheet: approve.openApproval,
    approvalClose: approve.closeApproval,
    clarifySheet: clarify.open,
    clarifyClose: clarify.close,
  },
  /* The transcript island: the conversation area's renderer. The legacy
     shims (demo/060, demo/070) and the pipeline's stages
     (state/session/stages.ts) drive these; the DOM they used to build is
     drawn by the island into a lane host inside #stage or a stage box. */
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
    spawnFeed: transcript.spawnFeed,
    stopStream: transcript.stopStream,
    nudge: transcript.nudge,
    redraw: transcript.redraw,
    agentStage: transcript.agentStage,
    down: transcriptTail.down,
    isStuck: transcriptTail.isStuck,
    setStuck: transcriptTail.setStuck,
  },
}

/* The desk, handed to the two island stores that open something in it. Handed
   rather than reached for: features/workspace/deskStore imports both of them
   back and subscribes to one as it evaluates, so a bag this size in their
   import graph would run that subscription against a half-built module. These
   two lines are the whole of what the bundle itself used to read the bag for. */
subagentsStore.setAgentPane(islands.workspace)
workspace.setDeskOpener(islands.workspace.openFile)
