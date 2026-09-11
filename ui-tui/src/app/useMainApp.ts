// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { type ScrollBoxHandle, useApp, useHasSelection, useSelection, useStdout, useTerminalTitle } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import type {
  ApprovalRespondResponse,
  ClarifyRespondResponse,
  ClipboardPasteResponse,
  GatewayEvent,
  TerminalResizeResponse
} from '../gatewayTypes.js'
import type { Msg, PanelSection, SlashCatalog } from '../types.js'

import { messageFoldId, turnFoldScope } from '../components/episodeView.js'
import { STARTUP_RESUME_ID } from '../config/env.js'
import { FULL_RENDER_TAIL_ITEMS, MAX_HISTORY, WHEEL_SCROLL_STEP } from '../config/limits.js'
import { dagRunsFromHistory } from '../domain/dagRun.js'
import { SECTION_NAMES, sectionMode } from '../domain/details.js'
import { attachedImageNotice, imageTokenMeta, withoutSpentIntro } from '../domain/messages.js'
import { fmtCwdBranch, shortCwd } from '../domain/paths.js'
import { spawnRunsFromHistory, type SpawnRunState } from '../domain/spawnRun.js'
import { type GatewayClient } from '../gatewayClientStub.js'
import { useGitBranch } from '../hooks/useGitBranch.js'
import { useVirtualHistory } from '../hooks/useVirtualHistory.js'
import { approvalResponseAccepted, buildApprovalRespond } from '../lib/approval.js'
import { createCopyOnSelectReporter, graphemeCount } from '../lib/clipboard.js'
import { buildConfirmRespond } from '../lib/confirmCountdown.js'
import { subscribeCopyOnSelect } from '../lib/copyOnSelect.js'
import { $dagOpenNodes } from '../lib/dagOpenNodes.js'
import { composerPromptWidth } from '../lib/inputMetrics.js'
import { appendTranscriptMessage } from '../lib/messages.js'
import { DEFAULT_VOICE_RECORD_KEY, type ParsedVoiceRecordKey } from '../lib/platform.js'
import { asRpcResult, rpcErrorMessage } from '../lib/rpc.js'
import { $spawnOpenOverrides, spawnTraceOpen } from '../lib/spawnOpen.js'
import { terminalParityHints } from '../lib/terminalParity.js'
import { buildToolTrailLine, sameToolTrailGroup, toolTrailLabel } from '../lib/text.js'
import { estimatedMsgHeight, messageHeightKey } from '../lib/virtualHeights.js'
import { createChatStream, type ChatStreamHandle, type ChatStreamRpcClient } from './chatStream.js'
import { showCopyNotice } from './copyNoticeStore.js'
import { createGatewayEventHandler } from './createGatewayEventHandler.js'
import { createSlashHandler } from './createSlashHandler.js'
import {
  $directChat,
  bindScrollReader,
  getDirectChat,
  isTargetWorking,
  isViewWorking,
  recallScroll,
  viewKeyOf,
  visibleRows
} from './directChatStore.js'
import { bindInstanceRefresh, fetchDirectHistory, fetchInstances } from './directChatSync.js'
import { bindDirectSender } from './directSend.js'
import { $folds, callFolds, type CallFolds } from './foldStore.js'
import { getInputSelection } from './inputSelectionStore.js'
import { type GatewayRpc, type RpcOptions, type TranscriptRow } from './interfaces.js'
import { bindLiveAgentsRefresh, fetchLiveAgents } from './liveAgentsSync.js'
import { $overlayState, getOverlayState, patchOverlayState } from './overlayStore.js'
import { scrollWithSelectionBy } from './scroll.js'
import { turnController } from './turnController.js'
import { patchTurnState, useTurnSelector } from './turnStore.js'
import { $uiState, getUiState, patchUiState } from './uiStore.js'
import { useComposerState } from './useComposerState.js'
import { useConfigSync } from './useConfigSync.js'
import { useDagNodePoll } from './useDagNodePoll.js'
import { useDirectStepPoll } from './useDirectStepPoll.js'
import { useInputHandlers } from './useInputHandlers.js'
import { useLongRunToolCharms } from './useLongRunToolCharms.js'
import { useSessionLifecycle } from './useSessionLifecycle.js'
import { useSpawnTracePoll } from './useSpawnTracePoll.js'
import { useSubmission } from './useSubmission.js'

const GOOD_VIBES_RE = /\b(good bot|thanks|thank you|thx|ty|ily|love you)\b/i
const BRACKET_PASTE_ON = '\x1b[?2004h'
const BRACKET_PASTE_OFF = '\x1b[?2004l'
const MAX_HEIGHT_CACHE_BUCKETS = 12

const capHistory = (items: Msg[]): Msg[] => {
  if (items.length <= MAX_HISTORY) {
    return items
  }

  return items[0]?.kind === 'intro' ? [items[0]!, ...items.slice(-(MAX_HISTORY - 1))] : items.slice(-MAX_HISTORY)
}

// Seam for the typed chat path's session key: the stream handle MUST be
// keyed to the minted ui.sid (from session.create / session.resume), never
// a hardcoded default — exported so tests can pin this against regressions.
export const buildChatStreamHandle = (
  rpcClient: ChatStreamRpcClient | undefined,
  sid: null | string,
  sys: (text: string) => void,
  appendMessage: (msg: Msg) => void
): ChatStreamHandle | null =>
  rpcClient && sid ? createChatStream({ appendMessage, rpcClient, sessionKey: sid, sys }) : null

const statusColorOf = (status: string, t: { error: string; muted: string; ok: string; warn: string }) => {
  if (status === 'ready') {
    return t.ok
  }

  if (status.startsWith('error')) {
    return t.error
  }

  if (status === 'interrupted') {
    return t.warn
  }

  return t.muted
}

/** What a picker selection becomes on the command line.
 *
 * Its own function because the scope has to survive the trip: `/model --default`
 * opens the picker, and a callback that dropped the flag here sent a
 * session-scoped switch that looked like it had changed the default. The
 * overlay value is passed in rather than read, so the rule can be pinned
 * without mounting the app.
 */
export const modelSelectCommand = (model: string, providerSlug: string, pending: boolean | 'default'): string =>
  `/model ${model} --provider ${providerSlug}${pending === 'default' ? ' --default' : ''}`

export function useMainApp(gw: GatewayClient, rpcClient?: ChatStreamRpcClient) {
  const { exit } = useApp()
  const { stdout } = useStdout()
  const [cols, setCols] = useState(stdout?.columns ?? 80)

  useEffect(() => {
    if (!stdout) {
      return
    }

    const sync = () => setCols(stdout.columns ?? 80)

    stdout.on('resize', sync)

    if (stdout.isTTY) {
      stdout.write(BRACKET_PASTE_ON)
    }

    return () => {
      stdout.off('resize', sync)

      if (stdout.isTTY) {
        stdout.write(BRACKET_PASTE_OFF)
      }
    }
  }, [stdout])

  const [historyItems, setHistoryItems] = useState<Msg[]>(() => [{ kind: 'intro', role: 'system', text: '' }])
  const [lastUserMsg, setLastUserMsg] = useState('')
  const [catalog, setCatalog] = useState<null | SlashCatalog>(null)
  const [voiceEnabled, setVoiceEnabled] = useState(false)
  const [voiceRecording, setVoiceRecording] = useState(false)
  const [voiceProcessing, setVoiceProcessing] = useState(false)
  const [voiceRecordKey, setVoiceRecordKey] = useState<ParsedVoiceRecordKey>(DEFAULT_VOICE_RECORD_KEY)
  const [sessionStartedAt, setSessionStartedAt] = useState(() => Date.now())
  const [turnStartedAt, setTurnStartedAt] = useState<null | number>(null)
  const [goodVibesTick, setGoodVibesTick] = useState(0)
  const [bellOnComplete, setBellOnComplete] = useState(false)

  const ui = useStore($uiState)
  const directChat = useStore($directChat)
  const overlay = useStore($overlayState)

  const turnLiveTailActive = useTurnSelector(state =>
    Boolean(
      state.streaming ||
      state.streamPendingTools.length ||
      state.streamSegments.length ||
      state.reasoning.trim() ||
      state.reasoningActive ||
      state.tools.length ||
      state.subagents.length ||
      state.todos.length
    )
  )

  const slashFlightRef = useRef(0)
  const slashRef = useRef<(cmd: string) => boolean>(() => false)
  const colsRef = useRef(cols)
  const scrollRef = useRef<null | ScrollBoxHandle>(null)
  const onEventRef = useRef<(ev: GatewayEvent) => void>(() => {})
  const clipboardPasteRef = useRef<(quiet?: boolean) => Promise<void> | void>(() => {})
  const submitRef = useRef<(value: string) => void>(() => {})
  const terminalHintsShownRef = useRef(new Set<string>())
  const historyItemsRef = useRef(historyItems)
  const lastUserMsgRef = useRef(lastUserMsg)
  const msgIdsRef = useRef(new WeakMap<Msg, string>())
  const msgIdSeqRef = useRef(0)
  const heightCachesRef = useRef(new Map<string, Map<string, number>>())

  colsRef.current = cols
  historyItemsRef.current = historyItems
  lastUserMsgRef.current = lastUserMsg

  const hasSelection = useHasSelection()
  const selection = useSelection()

  useEffect(() => {
    selection.setSelectionBgColor(ui.theme.color.selectionBg)
  }, [selection, ui.theme.color.selectionBg])

  const clearSelection = useCallback(() => {
    selection.clearSelection()
    getInputSelection()?.collapseToEnd()
  }, [selection])

  const composer = useComposerState({
    gw,
    onClipboardPaste: quiet => clipboardPasteRef.current(quiet),
    onImageAttached: info => {
      sys(attachedImageNotice(info))
    },
    submitRef
  })

  const { actions: composerActions, refs: composerRefs, state: composerState } = composer
  const empty = !historyItems.some(msg => msg.kind !== 'intro')

  useEffect(() => {
    void terminalParityHints()
      .then(hints => {
        for (const hint of hints) {
          if (terminalHintsShownRef.current.has(hint.key)) {
            continue
          }

          terminalHintsShownRef.current.add(hint.key)
          turnController.pushActivity(hint.message, hint.tone)
        }
      })
      .catch(() => {})
  }, [])

  const messageId = useCallback((msg: Msg) => {
    const hit = msgIdsRef.current.get(msg)

    if (hit) {
      return hit
    }

    const next = `${messageHeightKey(msg)}:${++msgIdSeqRef.current}`

    msgIdsRef.current.set(msg, next)

    return next
  }, [])

  // Which transcript the chat view is showing. A direct chat takes the view
  // over rather than mounting a second transcript component: useVirtualHistory
  // measures by row key, so a wholesale source swap needs no other change.
  // `historyItems` stays the main conversation's throughout -- session save,
  // /export and the slash handlers all read it, and none of them mean "whatever
  // is on screen".
  const visibleItems = useMemo(
    () => withoutSpentIntro(visibleRows(directChat, historyItems)),
    [directChat, historyItems]
  )

  const virtualRows = useMemo<TranscriptRow[]>(
    () => visibleItems.map((msg, index) => ({ index, key: messageId(msg), msg })),
    [messageId, visibleItems]
  )

  // Stable, so the poll's effect is not torn down and re-armed on every render.
  const sidRef = useCallback(() => getUiState().sid, [])

  const viewKey = viewKeyOf(directChat.active)
  const directChatRef = useRef(directChat.active)
  directChatRef.current = directChat.active

  // Restoring runs here, after the swap has been laid out; remembering happens
  // at switch time inside the store (see bindScrollReader), because by now the
  // offset already belongs to the incoming view.
  useEffect(() => {
    bindScrollReader(() => scrollRef.current?.getScrollTop() ?? 0)

    return () => bindScrollReader(null)
  }, [])

  useEffect(() => {
    scrollRef.current?.scrollTo(recallScroll(viewKey))
  }, [viewKey])

  const detailsLayoutKey = useMemo(() => {
    const thinking = sectionMode('thinking', ui.detailsMode, ui.sections, ui.detailsModeCommandOverride)
    const tools = sectionMode('tools', ui.detailsMode, ui.sections, ui.detailsModeCommandOverride)

    return `${thinking}:${tools}`
  }, [ui.detailsMode, ui.detailsModeCommandOverride, ui.sections])

  const detailsVisible = detailsLayoutKey !== 'hidden:hidden'
  const userPromptWidth = composerPromptWidth(ui.theme.brand.prompt)
  const heightCacheKey = `${ui.sid ?? 'draft'}:${cols}:${userPromptWidth}:${ui.compact ? '1' : '0'}:${detailsLayoutKey}`

  const heightCache = useMemo(() => {
    let cache = heightCachesRef.current.get(heightCacheKey)

    if (!cache) {
      cache = new Map()
      heightCachesRef.current.set(heightCacheKey, cache)

      if (heightCachesRef.current.size > MAX_HEIGHT_CACHE_BUCKETS) {
        heightCachesRef.current.delete(heightCachesRef.current.keys().next().value!)
      }
    }

    return cache
  }, [heightCacheKey])

  // Index of the first user-role message — separator-rendering in
  // appLayout.tsx skips this row, so the height estimator must skip it
  // too. -1 when no user message exists yet (no row will gate true).
  const firstUserIdx = useMemo(() => virtualRows.findIndex(r => r.msg.role === 'user'), [virtualRows])

  const dagOpen = useStore($dagOpenNodes)
  const spawnOverrides = useStore($spawnOpenOverrides)
  // A card the reader opened or shut is only re-measured for real while its row
  // is on screen. A resize drops every measured height (the cache buckets on
  // cols), so without the folds here the rows below the fold come back at their
  // default height and the transcript scrolls to the wrong place.
  const folds = useStore($folds)
  // Per message, because a fold key is only unique inside its own scope: the
  // transport restarts its call ids per response, so the same `mi-1` names a
  // card in every conversation. Cached per scope so a measuring pass over a
  // long transcript rebuilds each scope's sets once, not once a row.
  const foldsForScope = useMemo(() => {
    const cache = new Map<string, CallFolds>()

    return (scope: string): CallFolds => {
      let here = cache.get(scope)

      if (!here) {
        here = callFolds(folds, scope)
        cache.set(scope, here)
      }

      return here
    }
  }, [folds])

  const estimateRowHeight = useCallback(
    (index: number) =>
      estimatedMsgHeight(virtualRows[index]!.msg, cols, {
        cardFolds: foldsForScope(turnFoldScope(viewKey, messageFoldId(virtualRows[index]!.msg))),
        compact: ui.compact,
        dagOpen,
        details: detailsVisible,
        limitHistory: index < virtualRows.length - FULL_RENDER_TAIL_ITEMS,
        spawnOverrides,
        userPrompt: ui.theme.brand.prompt,
        withSeparator: virtualRows[index]!.msg.role === 'user' && firstUserIdx >= 0 && index > firstUserIdx
      }),
    [
      cols,
      dagOpen,
      detailsVisible,
      firstUserIdx,
      foldsForScope,
      spawnOverrides,
      ui.compact,
      ui.theme.brand.prompt,
      viewKey,
      virtualRows
    ]
  )

  const syncHeightCache = useCallback(
    (heights: ReadonlyMap<string, number>) => {
      for (const row of virtualRows) {
        const h = heights.get(row.key)

        if (h) {
          heightCache.set(row.key, h)
        }
      }
    },
    [heightCache, virtualRows]
  )

  const virtualHistory = useVirtualHistory(scrollRef, virtualRows, cols, {
    estimateHeight: estimateRowHeight,
    initialHeights: heightCache,
    liveTailActive: turnLiveTailActive,
    onHeightsChange: syncHeightCache
  })

  const scrollWithSelection = useCallback(
    (delta: number) => scrollWithSelectionBy(delta, { scrollRef, selection }),
    [selection]
  )

  // Re-pins the transcript to its bottom and restores the stickiness a manual
  // scroll broke, so what arrives next follows on screen instead of below it.
  const revealLatest = useCallback(() => scrollRef.current?.scrollToBottom(), [])

  const appendMessage = useCallback(
    (msg: Msg) => setHistoryItems(prev => capHistory(appendTranscriptMessage(prev, msg))),
    []
  )

  const sys = useCallback((text: string) => appendMessage({ role: 'system', text }), [appendMessage])

  // Terminals do not forward their own copy shortcut to a TUI that enables
  // mouse tracking, so copy-on-select is what makes a transcript selection
  // copyable at all. That holds on every platform, not just macOS.
  //
  // Nothing on screen changes when a drag ends, so the report is the only
  // confirmation the clipboard was written. The first copy of a session
  // carries the path caveat and stays in the transcript: it is the answer a
  // user comes looking for after a paste comes up empty minutes later, and it
  // cannot pile up because it is once per session by construction. The terse
  // repeats, which are unbounded, show as a transient notice above the
  // composer instead of stacking rows.
  //
  // The path caveat is per session while this hook outlives any one session:
  // `newSession()` and `resumeById()` replace `ui.sid` without remounting it,
  // so which sessions have been told belongs to the reporter rather than to a
  // flag here, which would stay set and drop the caveat from the next
  // session's first copy. The sid is read through `getUiState()` so a session
  // change does not tear down and rebuild the bus subscription.
  const reportCopyOnSelect = useRef(createCopyOnSelectReporter())

  useEffect(
    () =>
      subscribeCopyOnSelect(selection, (text, path) => {
        const report = reportCopyOnSelect.current(graphemeCount(text), path, getUiState().sid ?? 'draft')

        if (report.firstOfSession) {
          sys(report.text)
        } else {
          showCopyNotice(report.text, 3000)
        }
      }),
    [selection, sys]
  )

  const page = useCallback(
    (text: string, title?: string) => patchOverlayState({ pager: { lines: text.split('\n'), offset: 0, title } }),
    []
  )

  const panel = useCallback(
    (title: string, sections: PanelSection[]) =>
      appendMessage({ kind: 'panel', panelData: { sections, title }, role: 'system', text: '' }),
    [appendMessage]
  )

  const maybeWarn = useCallback(
    (value: unknown) => {
      const warning = (value as { warning?: unknown } | null)?.warning

      if (typeof warning === 'string' && warning) {
        sys(`warning: ${warning}`)
      }
    },
    [sys]
  )

  const maybeGoodVibes = useCallback((text: string) => {
    if (GOOD_VIBES_RE.test(text)) {
      setGoodVibesTick(v => v + 1)
    }
  }, [])

  const rpc: GatewayRpc = useCallback(
    async <T extends object = Record<string, unknown>>(
      method: string,
      params: Record<string, unknown> = {},
      opts: RpcOptions = {}
    ) => {
      try {
        const result = asRpcResult<T>(await gw.request<T>(method, params))

        if (result) {
          return result
        }

        // `quiet` rethrows rather than returning null so the caller's own
        // handler runs at all: reporting here *and* swallowing is what made
        // every `.catch(() => {})` at a call site dead code.
        if (opts.quiet) {
          throw new Error(`invalid response: ${method}`)
        }

        sys(`error: invalid response: ${method}`)
      } catch (e) {
        if (opts.quiet) {
          throw e
        }

        sys(`error: ${rpcErrorMessage(e)}`)
      }

      return null
    },
    [gw, sys]
  )

  // Typed chat path scaffold. The
  // `ChatStreamHandle` is constructed once per `sid` change and exposed
  // through `chatStreamRef` so the Ctrl+C handler in useInputHandlers
  // can route into `turn.cancel` whenever a typed turn is in flight.
  // The effect below installs and attaches the live handle per session.
  const chatStreamRef = useRef<ChatStreamHandle | null>(null)

  const gateway = useMemo(() => ({ gw, rpc, rpcClient }), [gw, rpc, rpcClient])

  const die = useCallback(() => {
    gw.kill()
    exit()
    // Ink's exit() calls unmount() which resets terminal modes but does NOT
    // call process.exit().  Without an explicit exit the Node process stays
    // alive (stdin listener keeps the event loop open), so the process.on('exit')
    // handler in entry.tsx — which sends the final resetTerminalModes() — never
    // fires.  This leaves kitty keyboard protocol, mouse modes, etc. enabled
    // in the parent shell.
    process.exit(0)
  }, [exit, gw])

  const session = useSessionLifecycle({
    colsRef,
    composerActions,
    gw,
    panel,
    rpc,
    scrollRef,
    setHistoryItems,
    setLastUserMsg,
    setSessionStartedAt,
    setVoiceProcessing,
    setVoiceRecording,
    sys
  })

  useEffect(() => {
    if (ui.busy) {
      setTurnStartedAt(prev => prev ?? Date.now())
    } else {
      setTurnStartedAt(null)
    }
  }, [ui.busy])

  // Install / replace the typed chat stream handle whenever the session
  // identifier changes. The handle is created eagerly AND attach()-ed so
  // turn.subscribe streams token.delta events through the typed path
  // (turn streaming is live). The Ctrl+C handler in
  // useInputHandlers reads `chatStreamRef.current` to prefer a typed
  // `turn.cancel` whenever a turn is in flight.
  useEffect(() => {
    const handle = buildChatStreamHandle(rpcClient, ui.sid, sys, appendMessage)

    if (!handle) {
      chatStreamRef.current = null

      return
    }

    chatStreamRef.current = handle
    const unbindSender = bindDirectSender(handle.sendTo)
    void handle.attach().catch(err => {
      // Surface subscription failure to the system message log; do not crash
      // the React tree. turn.cancel / send still callable if user inputs.
      sys(`chat stream attach failed: ${String(err)}`)
    })

    return () => {
      unbindSender()
      chatStreamRef.current = null
      void handle.detach().catch(() => {})
    }
  }, [rpcClient, ui.sid, sys, appendMessage])

  useConfigSync({ gw, setBellOnComplete, setVoiceEnabled, setVoiceRecordKey, sid: ui.sid })

  // Tab title: `⚠` waiting on approval/sudo/secret/clarify, `⏳` busy, `✓` idle.
  const model = ui.info?.model?.replace(/^.*\//, '') ?? ''

  const marker = overlay.approval || overlay.sudo || overlay.secret || overlay.clarify ? '⚠' : ui.busy ? '⏳' : '✓'

  const tabCwd = ui.info?.cwd

  useTerminalTitle(model ? `${marker} ${model}${tabCwd ? ` · ${shortCwd(tabCwd, 24)}` : ''}` : 'Raven Agent')

  useEffect(() => {
    if (!ui.sid || !stdout) {
      return
    }

    let timer: ReturnType<typeof setTimeout> | undefined

    const onResize = () => {
      clearTimeout(timer)
      timer = setTimeout(() => {
        timer = undefined
        void rpc<TerminalResizeResponse>('terminal.resize', { cols: stdout.columns ?? 80, session_id: ui.sid })
      }, 100)
    }

    stdout.on('resize', onResize)

    return () => {
      clearTimeout(timer)
      stdout.off('resize', onResize)
    }
  }, [rpc, stdout, ui.sid])

  // One place rather than at each session bind point: startup, a new session and
  // a resume all land on a new `sid`, and a fetch hung off each of them would be
  // three chances to forget the fourth.
  useEffect(() => {
    void fetchInstances(rpc, ui.sid)
    void fetchLiveAgents(rpc, ui.sid)
  }, [rpc, ui.sid])

  // Both event paths refresh the strip through this binding: `subagent.*`
  // arrives on the legacy gateway bus, `dag.*` only on the typed chat stream,
  // and neither owns a gateway rpc.
  useEffect(() => bindInstanceRefresh(rpc, () => getUiState().sid), [rpc])
  useEffect(() => bindLiveAgentsRefresh(rpc, () => getUiState().sid), [rpc])

  // Entering an instance loads its past turns. The record directories are the
  // only memory of a direct chat that survives a restart -- they are absent
  // from the session transcript by design. Keyed on the view rather than on the
  // whole store, so a delta arriving mid-load cannot re-enter this.
  // Settled rather than entered when the instance is idle: a run that ended
  // while another view was on screen left its last live snapshot in the store,
  // and the entering read keeps what it finds. See `InstanceConversation`.
  useEffect(() => {
    if (directChatRef.current !== null) {
      const read = isTargetWorking(getDirectChat(), directChatRef.current) ? 'enter' : 'settled'

      void fetchDirectHistory(rpc, getUiState().sid, directChatRef.current, read)
    }
  }, [rpc, viewKey])

  // The instance on screen is re-read while it works; see `useDirectStepPoll`
  // for why that is a read rather than a stream.
  useDirectStepPoll(rpc, sidRef, directChat.active, isViewWorking(directChat))

  const dagRuns = useTurnSelector(state => state.dagRuns)
  const pinnedDagRuns = useMemo(() => dagRunsFromHistory(historyItems), [historyItems])

  // A watched DAG node is re-read while it works; see `useDagNodePoll` for why
  // that is a read rather than a stream.
  useDagNodePoll(rpc, sidRef, dagRuns, pinnedDagRuns, dagOpen)

  const spawnRuns = useTurnSelector(state => state.spawnRuns)
  const pinnedSpawnRuns = useMemo(() => spawnRunsFromHistory(historyItems), [historyItems])

  // Which spawn panels are open -- running ones by default, plus the reader's
  // own toggles -- resolved once here so the poll and the panel agree. Merged
  // by task id, the live run winning, since the pinned copy's status can lag.
  const spawnOpen = useMemo(() => {
    const byTaskId = new Map<string, SpawnRunState>()

    for (const run of pinnedSpawnRuns) {
      byTaskId.set(run.taskId, run)
    }

    for (const run of spawnRuns) {
      byTaskId.set(run.taskId, run)
    }

    const open = new Set<string>()

    for (const run of byTaskId.values()) {
      if (spawnTraceOpen(run, spawnOverrides)) {
        open.add(run.taskId)
      }
    }

    return open
  }, [pinnedSpawnRuns, spawnOverrides, spawnRuns])

  // A watched spawn run is re-read while it works, on the same terms as a
  // watched DAG node.
  useSpawnTracePoll(rpc, sidRef, spawnRuns, pinnedSpawnRuns, spawnOpen)

  // A graph outlives the turn that started it: `run_subagent_dag` returns and the
  // reply commits while nodes are still running, and from then on the transcript
  // is drawing `tool.dag` -- the copy frozen at commit time. So a run that
  // finished after its turn read "0 done" forever.
  //
  // Written back into the history item rather than merged at render, because the
  // frozen copy is also what `messageHeightKey` measures: overriding only the
  // draw would leave the virtualizer reserving rows for the old graph shape.
  // `hydrateDagRuns` sets the same field the same way on session resume.
  useEffect(() => {
    if (dagRuns.length === 0) {
      return
    }

    const live = new Map(dagRuns.map(run => [run.runId, run]))

    setHistoryItems(prev => {
      let touched = false
      const next = prev.map(msg => {
        if (!msg.episodes?.length) {
          return msg
        }

        let msgTouched = false
        const episodes = msg.episodes.map(episode => {
          let epTouched = false
          const tools = episode.tools.map(tool => {
            const run = tool.dag && live.get(tool.dag.runId)

            if (!run || run === tool.dag) {
              return tool
            }

            epTouched = true

            return { ...tool, dag: run }
          })

          if (!epTouched) {
            return episode
          }

          msgTouched = true

          return { ...episode, tools }
        })

        if (!msgTouched) {
          return msg
        }

        touched = true

        return { ...msg, episodes }
      })

      return touched ? next : prev
    })
  }, [dagRuns, setHistoryItems])

  // A spawn outlives its turn the same way a graph does: from turn end onward
  // the transcript draws `tool.spawn` -- the copy frozen at commit time -- so
  // later status frames are written back into the history item, exactly as for
  // `tool.dag` above and for the same height-model reason.
  useEffect(() => {
    if (spawnRuns.length === 0) {
      return
    }

    const live = new Map(spawnRuns.map(run => [run.taskId, run]))

    setHistoryItems(prev => {
      let touched = false
      const next = prev.map(msg => {
        if (!msg.episodes?.length) {
          return msg
        }

        let msgTouched = false
        const episodes = msg.episodes.map(episode => {
          let epTouched = false
          const tools = episode.tools.map(tool => {
            const run = tool.spawn && live.get(tool.spawn.taskId)

            if (!run || run === tool.spawn) {
              return tool
            }

            epTouched = true

            return { ...tool, spawn: run }
          })

          if (!epTouched) {
            return episode
          }

          msgTouched = true

          return { ...episode, tools }
        })

        if (!msgTouched) {
          return msg
        }

        touched = true

        return { ...msg, episodes }
      })

      return touched ? next : prev
    })
  }, [spawnRuns, setHistoryItems])

  const answerClarify = useCallback(
    (answer: string) => {
      const clarify = overlay.clarify

      if (!clarify) {
        return
      }

      const label = toolTrailLabel('clarify')

      turnController.turnTools = turnController.turnTools.filter(line => !sameToolTrailGroup(label, line))
      patchTurnState({ turnTrail: turnController.turnTools })

      rpc<ClarifyRespondResponse>('clarify.respond', { answer, request_id: clarify.requestId }).then(r => {
        if (!r) {
          return
        }

        if (answer) {
          // Legacy transcript: record the clarify as a tool-trail panel plus the
          // answer as a message. In episodes mode the ask_user / deep_research
          // tool already renders this Q&A as a step, so committing them here would
          // double it — and the answer would masquerade as a typed user message.
          if (getUiState().transcript !== 'episodes') {
            turnController.persistedToolLabels.add(label)
            appendMessage({
              kind: 'trail',
              role: 'system',
              text: '',
              tools: [buildToolTrailLine('clarify', clarify.question)]
            })
            appendMessage({ role: 'user', text: answer })
          }
          patchUiState({ status: 'running…' })
        } else {
          sys('prompt cancelled')
        }

        patchOverlayState({ clarify: null })
      })
    },
    [appendMessage, overlay.clarify, rpc, sys]
  )

  const paste = useCallback(
    (quiet = false) =>
      rpc<ClipboardPasteResponse>('clipboard.paste', { session_id: getUiState().sid }).then(r => {
        if (!r) {
          return
        }

        if (r.attached) {
          const meta = imageTokenMeta(r)

          return sys(`📎 Image #${r.count} attached from clipboard${meta ? ` · ${meta}` : ''}`)
        }

        if (!quiet) {
          sys(r.message || 'No image found in clipboard')
        }
      }),
    [rpc, sys]
  )

  clipboardPasteRef.current = paste

  const { dispatchSubmission, send, sendQueued, submit } = useSubmission({
    appendMessage,
    chatStreamRef,
    composerActions,
    composerRefs,
    composerState,
    gw,
    maybeGoodVibes,
    revealLatest,
    setLastUserMsg,
    slashRef,
    submitRef,
    sys
  })

  // Drain one queued message whenever the session settles (busy → false):
  // agent turn ends, interrupt, shell.exec finishes, error recovered, or the
  // session first comes up with pre-queued messages. Without this, shell.exec
  // and error paths never emit message.complete, so anything enqueued while
  // `!sleep` / a failed turn was running would stay stuck forever.
  useEffect(() => {
    if (
      !ui.sid ||
      ui.busy ||
      composerRefs.queueEditRef.current !== null ||
      composerRefs.queueRef.current.length === 0
    ) {
      return
    }

    const next = composerActions.dequeue()

    if (next) {
      patchUiState({ busy: true, status: 'running…' })
      sendQueued(next)
    }
  }, [ui.sid, ui.busy, composerActions, composerRefs, sendQueued])

  const { pagerPageSize } = useInputHandlers({
    actions: {
      answerClarify,
      appendMessage,
      die,
      dispatchSubmission,
      guardBusySessionSwitch: session.guardBusySessionSwitch,
      newSession: session.newSession,
      sys
    },
    chatStreamRef,
    composer: { actions: composerActions, refs: composerRefs, state: composerState },
    gateway,
    terminal: { hasSelection, scrollRef, scrollWithSelection, selection, stdout },
    voice: {
      enabled: voiceEnabled,
      recordKey: voiceRecordKey,
      recording: voiceRecording,
      setProcessing: setVoiceProcessing,
      setRecording: setVoiceRecording,
      setVoiceEnabled
    },
    wheelStep: WHEEL_SCROLL_STEP
  })

  const onEvent = useMemo(
    () =>
      createGatewayEventHandler({
        composer: { setInput: composerActions.setInput },
        gateway,
        session: {
          STARTUP_RESUME_ID,
          colsRef,
          newSession: session.newSession,
          resetSession: session.resetSession,
          resumeById: session.resumeById,
          setCatalog
        },
        submission: { submitRef },
        system: { bellOnComplete, stdout, sys },
        transcript: { appendMessage, panel, setHistoryItems },
        voice: {
          setProcessing: setVoiceProcessing,
          setRecording: setVoiceRecording,
          setVoiceEnabled
        }
      }),
    [
      appendMessage,
      bellOnComplete,
      clearSelection,
      composerActions.setInput,
      gateway,
      panel,
      session.newSession,
      session.resetSession,
      session.resumeById,
      setVoiceEnabled,
      setVoiceProcessing,
      setVoiceRecording,
      stdout,
      submitRef,
      sys
    ]
  )

  onEventRef.current = onEvent

  useEffect(() => {
    const handler = (ev: GatewayEvent) => onEventRef.current(ev)

    const exitHandler = () => {
      turnController.reset()
      patchUiState({ busy: false, sid: null, status: 'gateway exited' })
      turnController.pushActivity('gateway exited · /logs to inspect', 'error')
      sys('error: gateway exited')
    }

    gw.on('event', handler)
    gw.on('exit', exitHandler)
    gw.drain()

    // entry.tsx's setupGracefulExit handles process cleanup on real exit.
    return () => {
      gw.off('event', handler)
      gw.off('exit', exitHandler)
    }
  }, [gw, sys])

  useLongRunToolCharms()

  const slash = useMemo(
    () =>
      createSlashHandler({
        composer: {
          enqueue: composerActions.enqueue,
          hasSelection,
          paste,
          queueRef: composerRefs.queueRef,
          selection,
          setInput: composerActions.setInput
        },
        gateway,
        local: {
          catalog,
          getHistoryItems: () => historyItemsRef.current,
          getLastUserMsg: () => lastUserMsgRef.current,
          maybeWarn,
          setCatalog
        },
        session: {
          closeSession: session.closeSession,
          deleteSessionWithFallback: session.deleteSessionWithFallback,
          die,
          guardBusySessionSwitch: session.guardBusySessionSwitch,
          newSession: session.newSession,
          resetVisibleHistory: session.resetVisibleHistory,
          resumeById: session.resumeById,
          setSessionStartedAt
        },
        slashFlightRef,
        transcript: {
          page,
          panel,
          send,
          setHistoryItems,
          sys,
          trimLastExchange: session.trimLastExchange
        },
        voice: { setVoiceEnabled, setVoiceRecordKey }
      }),
    [
      catalog,
      composerActions,
      composerRefs,
      die,
      gateway,
      hasSelection,
      maybeWarn,
      page,
      paste,
      selection,
      panel,
      send,
      session,
      sys
    ]
  )

  slashRef.current = slash

  const respondWith = useCallback(
    (method: string, params: Record<string, unknown>, done: () => void) => rpc(method, params).then(r => r && done()),
    [rpc]
  )

  const answerApproval = useCallback(
    (choice: string, feedback = '', approvalId?: string, pattern = '') => {
      // Read live rather than from this render's closure. One turn can hold two
      // approvals back to back -- a sub-agent's first command lands a second
      // after the spawn that created it was allowed -- and a closure a render
      // behind answers nothing, leaving the live request to expire unanswered.
      //
      // ``approvalId`` is what the prompt rendered, and it is the answer's real
      // subject: a keypress belongs to the request the human was reading and an
      // expiry to the one its countdown was armed for. Without it a callback
      // queued against the outgoing request would resolve the incoming one --
      // granting or refusing something nobody was shown.
      const approval = getOverlayState().approval

      if (!approval || (approvalId !== undefined && approvalId !== approval.approvalId)) {
        return
      }

      const refusal = choice === 'deny' || choice === 'deny_stop'

      if (refusal) {
        // Denial changes no host state, so the frontend can commit it locally
        // before the RPC round-trip. Approval is different: the overlay remains
        // until the backend confirms that the exact request was still live.
        // This asymmetry prevents stale UI from granting authority while also
        // ensuring a network failure cannot keep a rejected prompt interactive.
        patchOverlayState({ approval: null })
        patchTurnState({ outcome: 'denied' })
        patchUiState({ status: 'running…' })
      }

      rpc<ApprovalRespondResponse>(
        'approval.respond',
        buildApprovalRespond(approval.approvalId, approval.conversationId, choice, feedback, pattern)
      ).then(response => {
        if (refusal) {
          return
        }

        if (!approvalResponseAccepted(response)) {
          return
        }

        // The same id match `approval.closed` makes: a newer request can own
        // the overlay by the time this reply lands, and clearing it here would
        // take the new prompt off screen with nobody having answered it.
        if (getOverlayState().approval?.approvalId !== approval.approvalId) {
          return
        }

        patchOverlayState({ approval: null })
        patchTurnState({ outcome: `approved (${choice})` })
        patchUiState({ status: 'running…' })
      })
    },
    [rpc]
  )

  const answerSudo = useCallback(
    (pw: string) => {
      if (!overlay.sudo) {
        return
      }

      return respondWith('sudo.respond', { password: pw, request_id: overlay.sudo.requestId }, () => {
        patchOverlayState({ sudo: null })
        patchUiState({ status: 'running…' })
      })
    },
    [overlay.sudo, respondWith]
  )

  const answerSecret = useCallback(
    (value: string) => {
      if (!overlay.secret) {
        return
      }

      return respondWith('secret.respond', { request_id: overlay.secret.requestId, value }, () => {
        patchOverlayState({ secret: null })
        patchUiState({ status: 'running…' })
      })
    },
    [overlay.secret, respondWith]
  )

  const answerConfirm = useCallback(
    (answer: boolean) => {
      const requestId = overlay.confirm?.requestId

      if (!requestId) {
        return
      }

      return respondWith('confirm.respond', buildConfirmRespond(requestId, answer), () => {
        patchOverlayState({ confirm: null })
        patchUiState({ status: 'running…' })
      })
    },
    [overlay.confirm, respondWith]
  )

  const onModelSelect = useCallback(
    (model: string, providerSlug: string) => {
      const pending = overlay.modelPicker
      patchOverlayState({ modelPicker: false })
      slashRef.current(modelSelectCommand(model, providerSlug, pending))
    },
    [overlay.modelPicker]
  )

  const hasReasoning = useTurnSelector(state => Boolean(state.reasoning.trim()))

  // Per-section overrides win over the global mode — when every section is
  // resolved to hidden, the only thing ToolTrail will surface is the
  // floating-alert backstop (errors/warnings).  Mirror that so we don't
  // render an empty wrapper Box above the streaming area in quiet mode.
  const anyPanelVisible = SECTION_NAMES.some(
    s => sectionMode(s, ui.detailsMode, ui.sections, ui.detailsModeCommandOverride) !== 'hidden'
  )

  const thinkingPanelVisible =
    sectionMode('thinking', ui.detailsMode, ui.sections, ui.detailsModeCommandOverride) !== 'hidden'

  const toolsPanelVisible =
    sectionMode('tools', ui.detailsMode, ui.sections, ui.detailsModeCommandOverride) !== 'hidden'

  const activityPanelVisible =
    sectionMode('activity', ui.detailsMode, ui.sections, ui.detailsModeCommandOverride) !== 'hidden'

  const showProgressArea = useTurnSelector(state =>
    anyPanelVisible
      ? Boolean(
          ui.busy ||
          state.outcome ||
          state.streamPendingTools.length ||
          state.streamSegments.some(segment => {
            const hasThinking = Boolean(segment.thinking?.trim())
            const hasTrailTools = Boolean(segment.tools?.length)

            if (segment.kind === 'trail' && !segment.text) {
              return (
                (thinkingPanelVisible && hasThinking) || ((toolsPanelVisible || activityPanelVisible) && hasTrailTools)
              )
            }

            return (
              Boolean(segment.text?.trim()) ||
              (thinkingPanelVisible && hasThinking) ||
              ((toolsPanelVisible || activityPanelVisible) && hasTrailTools)
            )
          }) ||
          state.subagents.length ||
          state.tools.length ||
          state.todos.length ||
          state.turnTrail.length ||
          (thinkingPanelVisible && hasReasoning) ||
          state.activity.length
        )
      : state.activity.some(item => item.tone !== 'info')
  )

  const appActions = useMemo(
    () => ({
      answerApproval,
      answerClarify,
      answerConfirm,
      answerSecret,
      answerSudo,
      clearSelection,
      deleteSessionWithFallback: session.deleteSessionWithFallback,
      onModelSelect,
      resumeById: session.resumeById
    }),
    [
      answerApproval,
      answerClarify,
      answerConfirm,
      answerSecret,
      answerSudo,
      clearSelection,
      onModelSelect,
      session.deleteSessionWithFallback,
      session.resumeById
    ]
  )

  const appComposer = useMemo(
    () => ({
      cols,
      compIdx: composerState.compIdx,
      completions: composerState.completions,
      empty,
      handleTextPaste: composerActions.handleTextPaste,
      input: composerState.input,
      inputBuf: composerState.inputBuf,
      pagerPageSize,
      queueEditIdx: composerState.queueEditIdx,
      queuedDisplay: composerState.queuedDisplay,
      submit,
      updateInput: composerActions.setInput,
      voiceRecordKey
    }),
    [cols, composerActions, composerState, empty, pagerPageSize, submit, voiceRecordKey]
  )

  // Pass current progress through unfrozen — streaming update throttling
  // handles interaction load; progress must stay truthful so panels don't
  // randomly disappear when the live tail scrolls offscreen.
  const appProgress = useMemo(() => ({ showProgressArea }), [showProgressArea])

  const cwd = ui.info?.cwd || process.env.RAVEN_CWD || process.cwd()
  const gitBranch = useGitBranch(cwd)

  const appStatus = useMemo(
    () => ({
      cwdLabel: fmtCwdBranch(cwd, gitBranch),
      goodVibesTick,
      sessionStartedAt: ui.sid ? sessionStartedAt : null,
      statusColor: statusColorOf(ui.status, ui.theme.color),
      turnStartedAt: ui.sid ? turnStartedAt : null,
      // CLI parity: the classic prompt_toolkit status bar shows a red dot
      // on REC (cli.py:_get_voice_status_fragments line 2344).
      voiceLabel: voiceRecording ? '● REC' : voiceProcessing ? '◉ STT' : `voice ${voiceEnabled ? 'on' : 'off'}`
    }),
    [cwd, gitBranch, goodVibesTick, sessionStartedAt, turnStartedAt, ui, voiceEnabled, voiceProcessing, voiceRecording]
  )

  const appTranscript = useMemo(
    // `visibleItems`, not `historyItems`: every consumer of this prop indexes
    // into the rendered rows (first/last user row), so in direct mode it has to
    // be the transcript actually on screen.
    () => ({ historyItems: visibleItems, scrollRef, virtualHistory, virtualRows }),
    [virtualHistory, virtualRows, visibleItems]
  )

  return { appActions, appComposer, appProgress, appStatus, appTranscript, gateway }
}
