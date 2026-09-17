/* The island bag: one object naming every verb a page-wide writer spends.
 *
 * It outlived the layer it was built for. The bag was the door the legacy
 * chrome knocked on, and what is left is the shape that door gave the tree: a
 * store or a stage module reaches an island's verb through this object rather
 * than importing it, and the indirection is load-bearing in two ways. It is
 * one place to read what the page as a whole can ask of an island, and because
 * every member is read at call time, a case can stand in for one (`vi.spyOn`
 * on a bag, or scripts/module-harness.mjs's `islands` option) without mocking
 * the module behind it.
 *
 * A member with no reader does not belong here: it would read as a published
 * surface with nothing on the other side. The list shrank with the layer.
 *
 * The two island stores that would otherwise reach a sibling through the bag
 * are handed what they need at the foot of this file -- the desk imports both
 * of them back, so an import the other way would run its module-scope
 * subscription against a half-built module.
 *
 * The React roots that render into the hosts below are main.tsx's.
 */

import * as composer from './composer/mount'
import * as connections from './connections/store'
import * as cron from './cron/store'
import * as dagSheet from './dag/mount'
import * as dagNodes from './dag/nodes'
import * as knowledge from './knowledge/store'
import * as memory from './memory/store'
import * as modelPicker from './model/store'
import * as onboard from './onboard/store'
import * as playbooks from './playbooks/store'
import * as plugins from './plugins/store'
import * as rail from './rail/store'
import * as skills from './skills/store'
import * as subagentsStore from './subagents/store'
import * as transcript from './transcript/mount'
import * as transcriptTail from './transcript/tail'
import * as desk from './workspace/DeskPage'
import * as workspaceHunks from './workspace/hunks'
import * as workspace from './workspace/store'
import * as xa from './xa/store'
import * as settings from './settings/store'
import * as navfly from '../state/navfly'
import * as urlAction from '../lib/openUrl'
import * as resume from '../lib/resume'

/* The skills island renders into a host node the skills tab re-attaches under
   #capsBody on every draw (features/skills/tab.ts): the plugin tab clears that
   box with innerHTML, which must never tear down nodes React owns. */
export const skillsHost = document.createElement('div')
export const skillsSkeletonHost = document.createElement('div')
skillsSkeletonHost.className = 'hubgrid'

/* The plugins island renders into a host node it owns the same way: the tab
   (features/plugins/tab.ts) re-appends it on every plugin draw. */
export const plugHost = document.createElement('div')

export const islands = {
  cron: {
    close: cron.close,
    /* Read by page.show: a page's own overlay closes when the page does. */
    closeSheet: cron.closeSheet,
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
    close: connections.close,
    redraw: connections.redraw,
    closeDialog: connections.closeDialog,
  },
  /* What the page still reaches for: wsReset clears the list with the session,
     and the dag panel opens nodes, reads rows and marks the open selection. */
  subagents: {
    reset: subagentsStore.reset,
    refresh: subagentsStore.refresh,
    rows: subagentsStore.rows,
    openRow: subagentsStore.openRow,
    openDagNode: subagentsStore.openDagNode,
    directEvent: subagentsStore.directEvent,
  },
  rail: {
    draw: rail.draw,
    hold: rail.hold,
    release: rail.release,
    markNew: rail.markNew,
    rename: rail.rename,
    endRename: rail.endRename,
    reconcile: rail.reconcileRows,
    removeRow: rail.removeSessionRow,
  },
  /* The delegated graph. The layout walkers and the sentences are where this
     domain's edge cases live: a cycle, a fan-out that has to read as a
     diamond, a summary counting more nodes than the graph holds. */
  dag: {
    /* The adapter that turns a `dag.run_started` payload into nodes, so the
       sheet and the transcript's card agree on what a node is. */
    fromStarted: dagNodes.fromStarted,
    /* The sheet itself. Four calls: a run arrives, a run was mutated in place,
       the open conversation changed, a conversation went away -- plus one read,
       for the delegation row that opens the run's last node. The geometry, the
       marks and the summary lines are the component's own. */
    start: dagSheet.start,
    /* A node reported, and the run had its last word. */
    advance: dagSheet.advance,
    settle: dagSheet.settle,
    forget: dagSheet.forget,
    run: dagSheet.run,
  },
  /* Not an island: one verb, spent when a conversation is opened, that puts
     back the sheet and the desk that conversation had before the page was
     replaced (see lib/resume.ts). It reaches across three stores and the
     transcript's own `dag.get` seam, which is why it is not any of theirs. */
  view: {
    resume: resume.resume,
    /* The graph alone. Reopening a conversation that is still in the page must
       not replay the desk's opens, but its graph still needs re-reading -- see
       lib/resume.ts. */
    refreshDag: resume.refreshDag,
    landing: resume.landing,
    /* Started by app/boot.ts once the session pointer is real. */
    watch: resume.watch,
  },
  /* Not a React island: the nav flyout is a store with one chrome component
     over it (state/navfly.ts, chrome/MoreFly.tsx), and draw() is what marks
     the rows it has drawn. It rides the same bag because the bag is what a
     page-wide verb is reached through, island or not -- here, by the language
     repaint. */
  nav: {
    draw: navfly.draw,
  },
  /* One appended node per call, so this is a writer rather than an island:
     the browser island's source hands a URL to the desktop shell or the
     browser tab through it (lib/openUrl.ts). */
  chrome: {
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
    setCurrent: modelPicker.setCurrent,
  },
  xa: {
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
  /* Opened from the rail, like memory: the row, the Escape chain and the
     language repaint call these three. */
  knowledge: { open: knowledge.open, close: knowledge.close, redraw: knowledge.redraw },
  /* Same three verbs as knowledge: the rail opens it, Escape closes it, a
     language flip redraws it. */
  playbooks: { open: playbooks.openPage, close: playbooks.closePage, redraw: playbooks.redraw },
  workspace: {
    draw: workspace.draw,
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
    openAgent: desk.openDeskAgent,
    openAgentRecord: desk.openDeskAgentRecord,
    notifyDesk: desk.notifyDesk,
  },
  /* The composer island: the dock at the bottom of the chat. Four verbs, and
     each has one reader -- the boot's go-state paint, the runtime's draft
     claim, and the residency rule (state/session/residency.ts) carrying a
     parked turn's clock anchor out and back. The queue, the draft text, the
     staged paths and the sheet rack are the island's own; what the page still
     says to it is this. */
  composer: {
    goPaint: composer.goPaint,
    /* One announcement, two owners. The runtime calls this at the moment a
       draft becomes a session, and both the composer's draft text and the
       desk's palette are filed under the draft and have to follow it there --
       see deskStore.claimDraft for why the desk cannot work this out from the
       session pointer on its own. Wrapped here for the same reason
       `workspace.reset` is: the caller says the thing once. */
    claimDraft: (id: string | null) => {
      composer.claimDraft(id)
      desk.claimDraft(id)
    },
    liveAnchor: composer.liveAnchor,
    setLiveAnchor: composer.setLiveAnchor,
  },
  /* The transcript island: the conversation area's renderer. The conversation
     module (state/session/conversation.ts) and the pipeline's stages
     (state/session/stages.ts) drive these, and the island draws into a lane
     host inside #stage or a stage box. */
  transcript: {
    ask: transcript.ask,
    step: transcript.step,
    note: transcript.note,
    status: transcript.status,
    killStatus: transcript.killStatus,
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
    setStuck: transcriptTail.setStuck,
  },
}

/* The desk, handed to the two island stores that open something in it. Handed
   rather than reached for: features/workspace/deskStore imports both of them
   back and subscribes to one as it evaluates, so a bag this size in their
   import graph would run that subscription against a half-built module -- which
   is also why the panel those stores ask about is handed to them rather than
   imported (src/state/wsPanel.ts). These two lines are the whole of what the
   bundle itself reads the bag for. */
subagentsStore.setAgentPane(islands.workspace)
workspace.setDeskOpener(islands.workspace.openFile)
