/* The page's wiring: what answers each domain, what answers each push, and the
 * handful of controls whose action belongs to the session rather than to the
 * chrome that carries them.
 *
 * Every line here was one file of the live layer -- twenty parts whose whole
 * body was an `install()` putting one feature's `source.ts` on the seam. They
 * are four lists now, in the order the manifest ran them, and the order is
 * load-bearing in exactly two places, both marked below.
 *
 * Called once, from the boot (app/boot.ts), after the legacy chrome has
 * installed itself. Split into four rather than one so a test can drive the
 * half it is about: the seam touches nothing but `sources`, while the pushes
 * need a transport and the actions need the document.
 */

import { onFrameBytes, onFrameJson, browserSource } from '../features/browser/source'
import { open as approveSheet } from '../features/composer/approve'
import { connSource } from '../features/connections/source'
import { closeDialog as closeConnDialog } from '../features/connections/store'
import { cronSource } from '../features/cron/source'
import { closeSheet as closeCronSheet } from '../features/cron/store'
import { extAgentsSource } from '../features/extAgents/source'
import { capabilitiesSource, extPlugins, loadExt } from '../features/installed/source'
import { knowledgeSource } from '../features/knowledge/source'
import { memorySource } from '../features/memory/source'
import { modelSource, openModelsForMissingProvider, tierSource } from '../features/model/source'
import { onboardSource } from '../features/onboard/source'
import { playbooksSource } from '../features/playbooks/source'
import { pluginsSource } from '../features/plugins/source'
import { onEvent as pluginsEvent } from '../features/plugins/store'
import { markNew } from '../features/rail/store'
import { installSessionActions } from '../features/rail/wire'
import { bannerSource, settingsSource } from '../features/settings/source'
import { skillsSource } from '../features/skills/source'
import { agentsSource, startAgentHeartbeat } from '../features/subagents/source'
import {
  branch, cleanPreview, dagRun, okOf, openDagNode, openDagRun, openSpawn, spawnList, spawnRecord,
} from '../features/transcript/source'
import {
  proseSource, setHostPlatformReader, setShortener, workspaceSource,
} from '../features/workspace/source'
import { shared as workspaceShared } from '../features/workspace/store'
import { slashHelp, slashName } from '../i18n/t'
import { t } from '../i18n/t'
import { $ } from '../lib/dom'
import { hostPlatform } from '../lib/platform'
import { current as sessionCurrent } from '../lib/session'
import { refusal as uploadRefusal } from '../lib/upload'
import { gateway } from '../rpc/gateway'
import { setFault as setMemFault } from '../state/banner'
import * as caps from '../state/caps'
import * as page from '../state/page'
import { clarifyRequest, dispatch, installPipeline } from '../state/session/pipeline'
import { reconnect, switchToDraft } from '../state/session/registry'
import { installComposerActions, installSlashActions } from '../state/session/runtime'
import { ds, sources } from '../state/sources'
import { show as toast } from '../state/toast'
import { installConnectionUI, onReconnect, surface } from './connection'
import { showUpNote } from './updates'

import type { ComposerSource } from '../features/composer/types'
import type { SlashCmd } from '../features/composer/types'
import type { TranscriptSource } from '../features/transcript/types'
import type {
  McpStatusParams, MemoryHealthParams, OauthDoneParams, OauthPendingParams, SystemUpdateAvailableParams,
} from '../rpc/notifications'

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
  sources.skills = skillsSource
  sources.plugins = pluginsSource
  sources.memory = memorySource
  sources.knowledge = knowledgeSource
  sources.playbooks = playbooksSource
  sources.onboard = onboardSource
  sources.browser = browserSource
  sources.subagents = agentsSource
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

let pmExtSoon: ReturnType<typeof setTimeout> | undefined

function onMcpStatus(frame: unknown): void {
  const p = frame as McpStatusParams
  const row = extPlugins().find((x) => x.m && x.m.name === p.name)
  if (row) Object.assign(row.m as object, p)
  else {
    // Unknown server (fresh install, or events arriving before the first
    // ext.list) -- coalesce the reload; startup syncs fire one event per server.
    clearTimeout(pmExtSoon)
    pmExtSoon = setTimeout(() => loadExt()
      .then(() => pluginsEvent({ kind: 'rows' }))
      .catch(() => {}), 250)
  }
  pluginsEvent({
    kind: 'status', name: p.name, state: p.state, tool_count: p.tool_count, error: p.error ?? undefined,
    auth_url: p.auth_url || null,
  })
}

function onOauthPending(frame: unknown): void {
  const p = frame as OauthPendingParams
  pluginsEvent({
    kind: 'authPending', server: p.server, url: p.url,
    expires_in: p.expires_in, interactive: p.interactive,
  })
}

function onOauthDone(frame: unknown): void {
  const p = frame as OauthDoneParams
  pluginsEvent({ kind: 'authDone', server: p.server, ok: !!p.ok, error: p.error })
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
  gateway().on('mcp.status', onMcpStatus)
  gateway().on('oauth.pending', onOauthPending)
  gateway().on('oauth.done', onOauthDone)

  /* Screencast frames arrive as binary WS messages:
     "RVF1" + u32 header length + JSON header + raw JPEG. */
  gateway().binary(onFrameBytes)
  /* Old servers still notify frames as base64 JSON; same hook after decode. */
  gateway().on('browser.frame', onFrameJson)

  /* A delegated run in flight has to move on screen without being reopened, and
     there is no push for it -- so the subagents source polls. */
  startAgentHeartbeat()
}

/* ── the actions: controls whose answer belongs to the session ────────────── */

/** What has to happen again once a dropped connection is back. */
async function afterReconnect(): Promise<void> {
  await gateway().call('system.hello', { client_version: '0.1.0', surface: surface() }).catch(() => {})
  await reconnect()
  /* The installed skills, plugins and tools are read once at boot into
     module state and served from there, so a socket that was down when boot
     ran leaves all three empty for the life of the tab -- an empty page
     rather than a failed one. Re-read them here: the session reload above
     already treats a reconnect as "refetch what the gap invalidated", and
     these are the only surfaces whose data never asks again on its own. */
  /* Repainted, not just re-read: the island renders on its own `set`, which
     refilling the module state does not call, so the extensions page would
     keep showing the offline note after the reconnect it tells the reader to
     wait for. `caps.draw` is the entry for both tabs, guarded the way the
     language repaint guards it -- the page may not be up. */
  loadExt()
    .then(() => {
      try { caps.draw() } catch { /* extensions page not built yet */ }
    })
    .catch(() => {})
}

/** The composer's two actions, the rail's three writes, and the new-task button. */
export function installActions(): void {
  onReconnect(() => { void afterReconnect() })
  installComposerActions()
  installSessionActions()
  /* The slash palette's two session verbs, on the rows the dock declares. */
  installSlashActions()
  /* What a page switch spends on an island: the rail re-marks itself, and the
     two overlays a single page owns close behind it. Registered here because
     state/page.ts does not import features/ -- it declares the three slots and
     the order they run in (state/page.ts's `show`). */
  page.onShow('markNav', markNew)
  page.onShow('closeConnDialog', closeConnDialog)
  page.onShow('closeCronSheet', closeCronSheet)

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
  // newer, which never happens on a dev checkout (window.__upnote('ver', '0.1.11')).
  hooks.__upnote = (kind: 'ver' | 'ui', latest?: string) => showUpNote(kind || 'ver', latest)

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
