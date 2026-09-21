/* The page's wiring: what answers each domain, what answers each push, and the
 * handful of controls whose action belongs to the session rather than to the
 * chrome that carries them.
 *
 * Four lists, each line putting one feature's `source.ts` on the seam, and the
 * order is load-bearing in exactly two places, both marked below.
 *
 * Called once, from the boot (app/boot.ts's `boot`). Split into four rather
 * than one so a test can drive the
 * half it is about: the seam touches nothing but `sources`, while the pushes
 * need a transport and the actions need the document.
 */

import { onFrameBytes, onFrameJson, browserSource } from '../features/browser/source'
import { open as approveSheet } from '../features/composer/approve'
import { connSource } from '../features/connections/source'
import { cronSource } from '../features/cron/source'
import { openDeskTask } from '../features/desk/store'
import { AgentsStepBody } from '../features/extAgents/AgentsBody'
import { extAgentsSource } from '../features/extAgents/source'
import * as extAgentsStore from '../features/extAgents/store'
import { importSyncSource } from '../features/importSync/source'
import * as importSyncStore from '../features/importSync/store'
import { capabilitiesSource, loadExt } from '../features/installed/source'
import { memorySource } from '../features/memory/source'
import { modelSource, openModelsForMissingProvider, tierSource } from '../features/model/source'
import { onboardSource } from '../features/onboard/source'
import { isOpen as onboardOpen, setBodies as setOnboardBodies, subscribe as onOnboard } from '../features/onboard/store'
import { markNew } from '../features/rail/store'
import { installSessionActions } from '../features/rail/wire'
import { ModelStepBody, WebStepBody } from '../features/settings/SetupBodies'
import { bannerSource, modelStepDone, settingsSource, webStepDone } from '../features/settings/source'
import * as settingsStore from '../features/settings/store'
import { agentsSource, startAgentHeartbeat } from '../features/subagents/source'
import { tasksSource } from '../features/tasks/source'
import {
  byKey as taskByKey, onNodeUpdated, onRunCompleted, onRunReplanned, onRunStarted, onSubagentStatus,
  refresh as refreshTasks, reset as resetTasks,
} from '../features/tasks/store'
import {
  actLabel, branch, cleanPreview, dagRun, okOf, openDagNode, openDagRun, openSpawn, spawnList, spawnRecord,
} from '../features/transcript/source'
import {
  proseSource, setHostPlatformReader, setShortener, workspaceSource,
} from '../features/workspace/source'
import { shared as workspaceShared } from '../features/workspace/store'
import { slashHelp, slashName } from '../i18n/t'
import { t } from '../i18n/t'
import { $ } from '../lib/dom'
import { hostPlatform } from '../lib/platform'
import { current as sessionCurrent, onChange as onSessionChange } from '../lib/session'
import { refusal as uploadRefusal } from '../lib/upload'
import { gateway } from '../rpc/gateway'
import { setFault as setMemFault } from '../state/banner'
import * as page from '../state/page'
import { clarifyRequest, dispatch, installPipeline, replayPendingApprovals } from '../state/session/pipeline'
import { reconnect, switchToDraft } from '../state/session/registry'
import { installComposerActions, installSlashActions } from '../state/session/runtime'
import * as settingsDialog from '../state/settings'
import { ds, sources } from '../state/sources'
import { show as toast } from '../state/toast'
import { onReset as onWsReset } from '../state/ws'
import { installConnectionUI, onReconnect, surface } from './connection'
import { showUpNote } from './updates'

import type { ComposerSource } from '../features/composer/types'
import type { SlashCmd } from '../features/composer/types'
import type { TranscriptSource } from '../features/transcript/types'
import type { MemoryHealthParams, SystemUpdateAvailableParams } from '../rpc/notifications'

/* ── the palette, which no transport answers ─────────────────────────────── */

/* Two commands, deliberately. Anything else a palette could offer already has a
   button -- send/stop, the model chip, the answer footer, the rail -- and a
   second entry point for the same action is one more thing to keep in sync.
   What is left is what has no button: acting on the session as a whole.

   Both bodies are the session runtime's -- `installSlashActions` replaces them
   on these very rows -- so what belongs here is the pair of ids the palette
   renders, in the order it renders them. */
const SLASH: SlashCmd[] = [
  { id: 'gui.compress', fn: () => {} },
  { id: 'gui.clear', fn: () => {} },
]

/**
 * The composer source's palette half: which commands the dock offers and what
 * each is called. Installed before the four lists below, because the settings
 * seam adds `beforeSend` to the same object and the boot's own installer adds
 * the meter and the upload to it (src/main.tsx calls this first).
 */
export function installComposerPalette(): void {
  sources.composer = {
    slash: SLASH,
    slashName: (id: string) => slashName(id),
    slashHelp: (id: string) => slashHelp(id),
  } as ComposerSource
}

/* ── the seam: one source per domain ─────────────────────────────────────── */

/**
 * Every domain's own `source.ts`, onto `state/sources.ts`.
 *
 * Two of the objects here are grown rather than replaced, and both orders
 * matter: the transcript's source is created by the first line and added to by
 * the last four, and the composer's palette half is the dock's (installed with
 * the chrome) so the two verbs no transport answers are put onto it.
 */
export function installSources(): void {
  /* Reading a stored turn as segments lives with the renderer (the transcript
     island); how a tool result is previewed and judged is wire knowledge and
     lives with the transcript's own source. The spread keeps whatever the
     chrome put on the seam. */
  const transcript = { ...sources.transcript, clean: cleanPreview, okOf } as TranscriptSource
  sources.transcript = transcript
  const composer = sources.composer as ComposerSource

  // Per-turn cost lives under each answer and "a turn is running" is now the
  // ticking row above the composer, so the strip under the field stays empty.
  composer.meter = () => ''

  /* Opening a delegated graph: the last node if this page already holds the
     run's own record, the agents panel otherwise. A source verb rather than a
     line inside the delivered handler, because the row a RELOAD draws has to
     open the same thing the live row does. */
  transcript.openDagRun = openDagRun
  transcript.branch = branch
  /* The trail's dag card opens a node through the same reader as the sheet, and
     the island asks its source for all five. */
  transcript.openDagNode = openDagNode
  transcript.dagRun = dagRun
  transcript.spawnRecord = spawnRecord
  transcript.spawnList = spawnList
  transcript.openSpawn = openSpawn
  /* Read by the tasks domain's own tool rows (features/tasks/NodeRecord.tsx)
     through `ds('transcript').actLabel`, so a node's own transcript reads the
     same one-line labels as this island's. */
  transcript.actLabel = actLabel

  /* The four the settings dialog's own wiring used to assign. Here rather than
     there because this is the page's one assigner, and first in this list
     because that is where they landed before: settingsChrome.install() runs
     ahead of boot() (src/main.tsx). */
  sources.banner = bannerSource
  sources.settings = settingsSource
  sources.tier = tierSource
  sources.model = modelSource
  composer.beforeSend = openModelsForMissingProvider

  sources.capabilities = capabilitiesSource
  sources.cron = cronSource
  sources.connections = connSource
  sources.memory = memorySource
  sources.onboard = onboardSource
  /* The onboarding wizard draws the owning domains' own bodies for its three
     configured steps -- the settings dialog's model page and web controls,
     the sub-agents roster -- and asks each domain's store whether the step is
     loaded and done. Handed over here rather than imported by the wizard,
     which would make it a reader of two sibling domains' private files. */
  setOnboardBodies({
    model: {
      Body: ModelStepBody,
      load: () => settingsStore.refresh(),
      subscribe: settingsStore.subscribe,
      loaded: () => settingsStore.get().loaded,
      done: modelStepDone,
      needsRestart: () => settingsStore.get().needsRestart,
    },
    search: {
      Body: WebStepBody,
      load: () => settingsStore.refresh(),
      subscribe: settingsStore.subscribe,
      loaded: () => settingsStore.get().loaded,
      done: webStepDone,
    },
    agents: {
      Body: AgentsStepBody,
      load: () => extAgentsStore.load(true),
      subscribe: extAgentsStore.subscribe,
      /* The roster body draws its own scanning state, so the frame never
         stands in front of it with a loading line. */
      loaded: () => true,
      done: extAgentsStore.stepDone,
      found: () => extAgentsStore.found().map((row) => ({ id: row.preset ?? row.name, name: row.name })),
    },
  })
  sources.importSync = importSyncSource
  /* The rail's import row reads the run the wizard's last step starts. Wired
     here rather than by either domain reading the other: the wizard closes,
     and the row asks the gateway what is running now. Watching the open flag's
     edge, not every write, keeps a closed wizard's other writes from turning
     into status reads. */
  let wizardWasOpen = onboardOpen()
  onOnboard(() => {
    const open = onboardOpen()
    if (wizardWasOpen && !open) void importSyncStore.refresh()
    wizardWasOpen = open
  })
  sources.browser = browserSource
  sources.subagents = agentsSource
  /* One source either way: `tasks.list` is a real gateway method now, so the
     offline page reads it through the fixture transport the same way every
     other domain does, rather than through a stand-in library of its own.
     Grown with the store's own verbs (openByNode, the five live consumers)
     rather than replaced, so a sibling domain and the session pipeline reach
     this one through the seam instead of importing its store directly
     (CONTRIBUTING 2.2; import-direction's CROSS and PINNED lists). */
  sources.tasks = {
    ...tasksSource,
    openByNode: (nodeId) => {
      const row = taskByKey('spawn', nodeId)
      if (!row) return false
      openDeskTask(row)
      return true
    },
    openRun: (runId) => {
      const row = taskByKey('dag', runId)
      if (!row) return false
      openDeskTask(row)
      return true
    },
    onRunStarted, onNodeUpdated, onRunCompleted, onRunReplanned, onSubagentStatus,
  }
  sources.extAgents = extAgentsSource

  /* The workspace panel's chrome is still the page's, so the two things its
     source cannot work out for itself are handed over here. */
  setHostPlatformReader(hostPlatform)
  setShortener((p) => ds('workspace').shortPath(p))
  sources.prose = proseSource
  sources.workspace = workspaceSource

  /* ── the turn's products ───────────────────────────────────────────────
     The workspace record's own rows for one turn, unfiltered. Which of them
     counts as a product, and what a tile can draw of it, are the transcript
     island's to decide -- see artifactsOf in features/transcript/store.ts.

     The turn number is the one the record files rows under: bumped per turn by
     the pipeline, and per user message with text by the replay
     (features/workspace/record.ts). The transcript counts it the same way over
     the same payload. */
  sources.artifacts = {
    changes: (turn) => workspaceShared().changes.filter((c) => c.turn === turn),
  }

  /* Files are uploaded into <workspace>/uploads and handed to the agent as
     paths: every file tool is already workspace-scoped, so a path is all it
     needs. Bytes never ride inside the message. */
  composer.upload = (p) => {
    const refusal = uploadRefusal(p.name, p.content_b64)
    /* Rejected, not returned: the tray already renders a rejection as the
       chip's failure note, and a refusal is one -- the file is not attached
       either way. */
    if (refusal) return Promise.reject(new Error(refusal))
    return gateway().call('fs.upload', {
      name: p.name,
      content_b64: p.content_b64,
      /* Read per call, not captured: a file can be staged in a draft that
         becomes a session between the pick and the upload. */
      session: sessionCurrent() || '',
    })
  }

  /* A picked deck template lands under uploads exactly as an upload does, so
     the turn hands it over by the same path and the deck engine's route opens
     on it. */
  composer.templates = {
    list: () => gateway().call('deck.templates.list', { covers: true }),
    pick: (name) => gateway().call('deck.templates.pick', { name }),
    pages: (name) => gateway().call('deck.templates.pages', { name }),
  }
}

/* ── the pushes: what the gateway says without being asked ───────────────── */

/* Each handler takes the raw frame and names its shape, the way the pipeline's
   five do: the transport hands every notification over as `unknown`, and the
   declared params are src/rpc/notifications.ts. */

/* The gateway announces a newer build the moment its periodic check finds one,
   so a tab that has been open for days hears about it without a reload. Same
   banner as the boot-time system.version path. */
function onUpdateAvailable(frame: unknown): void {
  const p = frame as SystemUpdateAvailableParams
  if (p && p.latest_version) showUpNote('ver', p.latest_version)
}

/* Long-term memory stopped writing, or started again. Broadcast like the other
   per-server events, because a backend that cannot store is not part of any one
   conversation's turn. */
function onMemoryHealth(frame: unknown): void {
  const p = frame as MemoryHealthParams
  setMemFault(p && p.ok === false ? (p.error || t('gui.mem.down')) : null)
}

/** Every handler the page hangs on the transport, and the one clock it starts. */
export function installPushes(): void {
  installConnectionUI()
  /* Every frame the gateway pushes for a turn is routed by the session
     pipeline: the turn stream by the subscription it names, and the five
     side-channel requests by the conversation whose turn is blocked on the
     answer. */
  installPipeline()

  gateway().on('system.update_available', onUpdateAvailable)
  gateway().on('memory.health', onMemoryHealth)

  /* Screencast frames arrive as binary WS messages:
     "RVF1" + u32 header length + JSON header + raw JPEG. */
  gateway().binary(onFrameBytes)
  /* Old servers still notify frames as base64 JSON; same hook after decode. */
  gateway().on('browser.frame', onFrameJson)

  /* A delegated run in flight has to move on screen without being reopened, and
     there is no push for it -- so the subagents source polls. */
  startAgentHeartbeat()
  /* The tasks panel's first read, now that its seam is on. The strip that shows
     the running rows is rendered in the first frame, before any of this, so the
     ask it makes for itself finds no source and is not the one that lands. */
  void refreshTasks()
}

/* ── the actions: controls whose answer belongs to the session ────────────── */

/** What has to happen again once a dropped connection is back. */
async function afterReconnect(): Promise<void> {
  await gateway().call('system.hello', { client_version: '0.1.0', surface: surface() }).catch(() => {})
  await reconnect()
  /* The sheets a question was waiting in are gone with the old socket; the
     questions are not. */
  await replayPendingApprovals()
  /* The installed skills, plugins and tools are read once at boot into
     module state and served from there, so a socket that was down when boot
     ran leaves all three empty for the life of the tab -- an empty page
     rather than a failed one. Re-read them here: the session reload above
     already treats a reconnect as "refetch what the gap invalidated", and
     these are the only surfaces whose data never asks again on its own. */
  /* Re-read, with nothing to repaint: the one surface these rows still reach
     is the settings dialog, which reads them when it opens. */
  void loadExt().catch(() => {})
  /* An import that ran on while the socket was down changed its counts, or
     ended, without this page hearing; the row reads them again the way the
     boot did. */
  void importSyncStore.refresh()
}

/** The composer's two actions, the rail's three writes, and the new-task button. */
export function installActions(): void {
  onReconnect(() => { void afterReconnect() })
  installComposerActions()
  installSessionActions()
  /* The slash palette's two session verbs, on the rows the dock declares. */
  installSlashActions()
  /* What a page switch spends on an island: the rail re-marks itself.
     Registered here because state/page.ts does not import features/ -- it
     declares the slot and when it runs (state/page.ts's `show`). */
  page.onShow('markNav', markNew)
  /* What raises the settings dialog for the three domains that are sections of
     it (schedules, channels, memory). Registered here for the same reason the
     slot above is: state/settings.ts declares it and does not import the island
     that fills it. What arriving at one of those three sections costs, and what
     leaving it costs, each domain registers at its own module evaluation --
     three island stores in the page's wiring for three lines is three island
     graphs it does not otherwise carry. */
  settingsDialog.onOpen(() => { void settingsStore.open() })
  /* A different conversation is a different set of tasks: carrying them across
     would attribute one conversation's background work to another, and the
     strip above the composer outlives the switch, so nothing would ask for the
     rows the new conversation has. Registered here for the same reason the
     three slots above are: state/ws.ts does not import features/. */
  onWsReset('tasks', resetTasks)
  /* And the read for the conversation arrived at: the reset above runs before
     the pointer moves (state/session/registry.ts's switchTo), so the panel's
     own "not loaded yet" effect would otherwise fire under the session being
     left. Ownership changing is what should trigger the re-read, not `loaded`
     happening to be false. */
  onSessionChange(() => { void refreshTasks() })

  $('#newBtn')!.onclick = () => {
    if (openModelsForMissingProvider()) return
    page.show(null); switchToDraft()
  }
}

/* ── the dev hooks ───────────────────────────────────────────────────────── */

/**
 * Three surfaces a design pass cannot otherwise reach, on window because a
 * devtools console is the only caller any of them will ever have.
 */
export function installDevHooks(): void {
  const hooks = window as unknown as Record<string, unknown>
  // Previews the clarify sheet without spending a model turn
  // (window.__clarify({question, choices})).
  hooks.__clarify = (p: unknown) => clarifyRequest(p || { request_id: 'dev', question: '预览', choices: ['A', 'B'] })

  // The update row's version state only appears when a release is actually
  // newer, which never happens on a dev checkout (window.__upnote('ver', '0.1.11')),
  // and its behind-sources state only on a served checkout whose dist is stale
  // (window.__upnote('behind')).
  hooks.__upnote = (kind: 'ver' | 'ui' | 'behind', latest?: string) => showUpNote(kind || 'ver', latest)

  /* The approval sheet only appears when an engine asks for one, which is too
     long a loop to design a sheet in (window.__approve('rm -rf build/')). */
  hooks.__approve = (p: unknown) => approveSheet((p as string) || 'rm -rf build/',
    () => toast(t('gui.confirm.allow')), () => toast(t('gui.confirm.deny')))

  // The graph is only reachable by configuring third-party sub-agents and
  // spending a multi-agent run, which is too long a loop to design a layout in.
  // `window.__dag()` feeds the same three events the server sends.
  hooks.__dag = (ev: { type?: string }) => dispatch(ev && ev.type ? ev : {
    type: 'dag.run_started',
    payload: {
      run_id: '20260812T120000Z-deadbeef',
      nodes: [
        { id: 'survey', subagent: 'Researcher', depends_on: [] },
        { id: 'read_a', subagent: 'Researcher', depends_on: ['survey'] },
        { id: 'read_b', subagent: 'Coder', depends_on: ['survey'] },
        { id: 'read_c', subagent: 'Coder', instance: 'w2', depends_on: ['survey'] },
        { id: 'merge', subagent: 'Writer', depends_on: ['read_a', 'read_b', 'read_c'] },
        { id: 'review', subagent: 'Critic', depends_on: ['merge'] },
      ],
    },
  })
}

/** The four lists, in the order the live manifest ran them. */
export function installPage(): void {
  installSources()
  installPushes()
  installActions()
  installDevHooks()
}
