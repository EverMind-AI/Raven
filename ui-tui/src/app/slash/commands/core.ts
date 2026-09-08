// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { dumpScreen, forceRedraw } from '@hermes/ink'

import type {
  ConfigGetValueResponse,
  ConfigSetResponse,
  SessionSaveResponse,
  SessionStatusResponse,
  SessionSteerResponse,
  SessionTitleResponse,
  SessionUndoResponse
} from '../../../gatewayTypes.js'
import type {
  SessionSetModeResult,
  SubagentsInstanceCreateResult,
  SubagentsInstanceSetModeResult
} from '../../../rpc/generated.js'
import type { Msg, PanelSection } from '../../../types.js'
import type { StatusBarMode } from '../../interfaces.js'
import type { SlashCommand } from '../types.js'

import { NO_CONFIRM_DESTRUCTIVE } from '../../../config/env.js'
import { dailyFortune, randomFortune } from '../../../content/fortunes.js'
import { HOTKEYS } from '../../../content/hotkeys.js'
import { isSectionName, nextDetailsMode, parseDetailsMode, SECTION_NAMES } from '../../../domain/details.js'
import { getLocale, isLocale, setLocale } from '../../../i18n/index.js'
import { copyResultNotice, graphemeCount, writeClipboardText } from '../../../lib/clipboard.js'
import { writeOsc52Clipboard } from '../../../lib/osc52.js'
import { writePaintDump } from '../../../lib/perfPane.js'
import { configureDetectedTerminalKeybindings, configureTerminalKeybindings } from '../../../lib/terminalSetup.js'
import { enterDirect, getDirectChat, isDirectTarget, leaveDirect, rememberInstance } from '../../directChatStore.js'
import { patchOverlayState } from '../../overlayStore.js'
import { patchUiState } from '../../uiStore.js'

const flagFromArg = (arg: string, current: boolean): boolean | null => {
  if (!arg) {
    return !current
  }

  const mode = arg.trim().toLowerCase()

  if (mode === 'on') {
    return true
  }

  if (mode === 'off') {
    return false
  }

  if (mode === 'toggle') {
    return !current
  }

  return null
}

const RESET_WORDS = new Set(['reset', 'clear', 'default'])
const CYCLE_WORDS = new Set(['cycle', 'toggle'])

const DETAILS_USAGE =
  'usage: /details [hidden|collapsed|expanded|cycle]  or  /details <section> [hidden|collapsed|expanded|reset]'

const DETAILS_SECTION_USAGE = 'usage: /details <section> [hidden|collapsed|expanded|reset]'

export const coreCommands: SlashCommand[] = [
  {
    help: 'list commands + hotkeys',
    name: 'help',
    run: (_arg, ctx) => {
      const sections: PanelSection[] = (ctx.local.catalog?.categories ?? []).map(cat => ({
        rows: cat.pairs,
        title: cat.name
      }))

      if (ctx.local.catalog?.skillCount) {
        sections.push({ text: `${ctx.local.catalog.skillCount} skill commands available — /skills to browse` })
      }

      sections.push(
        {
          rows: [
            ['/details [hidden|collapsed|expanded|cycle]', 'set global agent detail visibility mode'],
            [
              '/details <section> [hidden|collapsed|expanded|reset]',
              'override one section (thinking/tools/subagents/activity)'
            ],
            ['/fortune [random|daily]', 'show a random or daily local fortune']
          ],
          title: 'TUI'
        },
        { rows: HOTKEYS, title: 'Hotkeys' }
      )

      ctx.transcript.panel(ctx.ui.theme.brand.helpHeader, sections)
    }
  },

  {
    aliases: ['exit', 'q'],
    help: 'exit raven',
    name: 'quit',
    run: (_arg, ctx) => ctx.session.die()
  },

  {
    // One switch for both front ends: the GUI reads the same key, and the
    // agent's reply language follows it through the system prompt.
    aliases: ['language'],
    help: 'switch UI language [en|zh]',
    name: 'lang',
    run: (arg, ctx) => {
      const want = arg.trim().toLowerCase()

      if (!want) {
        return ctx.transcript.sys(`language: ${getLocale()} (usage: /lang [en|zh])`)
      }

      if (!isLocale(want)) {
        return ctx.transcript.sys('usage: /lang [en|zh]')
      }

      ctx.gateway
        .rpc<ConfigSetResponse>('config.set', { key: 'language', value: want })
        .then(
          ctx.guarded<ConfigSetResponse>(() => {
            setLocale(want)
            forceRedraw()
            ctx.transcript.sys(want === 'zh' ? '界面语言已切换为中文' : 'language set to English')
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'permission mode [ask|smart|full], or `default [ask|smart|full]` for new conversations',
    name: 'perm',
    run: (arg, ctx) => {
      const words = arg.trim().toLowerCase().split(/\s+/).filter(Boolean)
      const tiers = ['ask', 'smart', 'full']
      const usage = 'usage: /perm [ask|smart|full] | /perm default [ask|smart|full]'
      // `/perm X` reaches this conversation only; `/perm default X` the mode
      // every new one starts on. A session scope with no session yet is the
      // server's to refuse, and its refusal is the message the reader sees.
      const isDefault = words[0] === 'default'
      const want = isDefault ? words[1] : words[0]
      if (words.length > (isDefault ? 2 : 1)) {
        return ctx.transcript.sys(usage)
      }

      if (!want) {
        // config.get takes `keys` (plural) and answers `{config: {...}}` --
        // the singular `key`/`value` shape belongs to config.set only. With a
        // session_id it answers the mode this conversation runs in.
        ctx.gateway
          .rpc<{ config?: Record<string, unknown> }>('config.get', {
            keys: ['permissions.mode'],
            ...(isDefault || !ctx.sid ? {} : { session_id: ctx.sid })
          })
          .then(r =>
            ctx.transcript.sys(
              `${isDefault ? 'default permission mode' : 'permission mode'}: ${r?.config?.['permissions.mode'] ?? 'ask'} (${usage})`
            )
          )
          .catch(ctx.guardedErr)

        return
      }

      if (!tiers.includes(want)) {
        return ctx.transcript.sys(usage)
      }

      ctx.gateway
        .rpc<ConfigSetResponse>('config.set', {
          key: 'permissions.mode',
          value: want,
          ...(isDefault ? {} : { scope: 'session', session_id: ctx.sid })
        })
        .then(
          ctx.guarded<ConfigSetResponse>(() => {
            // The gate reads the mode live, so the switch holds from the
            // next tool call -- nothing else to poke.
            ctx.transcript.sys(
              isDefault ? `default permission mode set to ${want}` : `permission mode set to ${want} for this conversation`
            )
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    aliases: ['scroll'],
    help: 'toggle mouse/wheel tracking [on|off|toggle]',
    name: 'mouse',
    run: (arg, ctx) => {
      const current = ctx.ui.mouseTracking
      const next = flagFromArg(arg, current)

      if (next === null) {
        return ctx.transcript.sys('usage: /mouse [on|off|toggle]')
      }

      patchUiState({ mouseTracking: next })
      ctx.gateway
        .rpc<ConfigSetResponse>('config.set', { key: 'mouse', value: next ? 'on' : 'off' }, { quiet: true })
        .catch(() => {})

      queueMicrotask(() => ctx.transcript.sys(`mouse tracking ${next ? 'on' : 'off'}`))
    }
  },

  {
    // `/clear` is an alias of `/new`: both mint a fresh session. A prior
    // iteration split `/clear` into an in-place wipe that kept the sid; per the
    // user we realign to upstream hermes, where /clear == /new (a new session id).
    aliases: ['clear'],
    help: 'start a new session (/clear is an alias)',
    name: 'new',
    run: (arg, ctx) => {
      if (ctx.session.guardBusySessionSwitch('switch sessions')) {
        return
      }

      const requestedTitle = arg.trim()

      const commit = () => {
        patchUiState({ status: 'forging session…' })
        ctx.session.newSession('New session started', requestedTitle || undefined)
      }

      if (NO_CONFIRM_DESTRUCTIVE) {
        return commit()
      }

      patchOverlayState({
        confirm: {
          cancelLabel: 'No, keep going',
          confirmLabel: 'Yes, start a new session',
          danger: true,
          detail: 'This ends the current conversation and starts a fresh session.',
          onConfirm: commit,
          title: 'Start a new session?'
        }
      })
    }
  },

  {
    help: 'force a full UI repaint',
    name: 'redraw',
    supported: false,
    run: (_arg, ctx) => {
      forceRedraw(process.stdout)
      ctx.transcript.sys('ui redrawn')
    }
  },

  {
    help: 'dump the renderer view of the screen to a file (paint diagnosis)',
    name: 'paintdump',
    supported: false,
    run: (_arg, ctx) => {
      const path = writePaintDump(dumpScreen(process.stdout))

      ctx.transcript.sys(
        path === null
          ? 'paintdump: nothing mounted to dump'
          : `paintdump: wrote ${path}\nRun /paintdump now, then /redraw, then /paintdump again: same rows and styles across the two means the buffer was right and the write lost something; different means the buffer itself was wrong.`
      )
    }
  },

  {
    aliases: ['instances'],
    help: 'switch to a sub-agent instance chat (no arg lists them)',
    name: 'instance',
    usage: '/instance [<n> | <agent>/<handle> | main]',
    run: (arg, ctx) => {
      const { active, instances } = getDirectChat()
      const addressable = instances.filter(r => r.kind !== 'dag-node')
      const target = arg.trim()

      if (!target) {
        if (addressable.length === 0) {
          return ctx.transcript.sys('no sub-agent instances in this session yet')
        }

        const lines = addressable.map(
          (r, i) =>
            `${i + 1}. ${r.agent}/${r.handle}${isDirectTarget(active, { agent: r.agent, handle: r.handle }) ? '  (here)' : ''}`
        )

        return ctx.transcript.sys(
          [`sub-agent instances (/instance <n> to switch, /instance main to leave):`, ...lines].join('\n')
        )
      }

      if (target.toLowerCase() === 'main' || target.toLowerCase() === 'raven') {
        leaveDirect()

        return ctx.transcript.sys('back on the main conversation')
      }

      // Accept the index the listing prints as well as the full name -- the
      // names are long enough that typing one is its own obstacle.
      const byIndex = /^\d+$/.test(target) ? addressable[Number(target) - 1] : undefined
      const row = byIndex ?? addressable.find(r => `${r.agent}/${r.handle}` === target || r.handle === target)

      if (!row) {
        return ctx.transcript.sys(`no such instance: ${target} (try /instance with no argument)`)
      }

      enterDirect(row.agent, row.handle)
      ctx.transcript.sys(`talking to ${row.agent}/${row.handle} directly -- Esc returns to Raven`)
    }
  },

  {
    help: 'show or change the effort tier -- of this conversation, or of the sub-agent you are chatting with',
    name: 'mode',
    usage: '/mode [<id> | default]   -- default/reset/clear return to the default',
    run: (arg, ctx) => {
      const { active } = getDirectChat()

      if (!ctx.sid) {
        return ctx.transcript.sys('no active session')
      }

      const want = arg.trim()
      // `default` / `reset` / `clear` are spent on dropping the override, so a
      // mode or tier sharing one of those names -- an agent's own, or one of
      // the session's medium/high/max tiers -- can never be selected by name
      // here: telling the two apart needs the catalogue in hand before this
      // call goes out, and the catalogue only arrives in the reply. So the
      // words stay reserved, and `usage` names them.
      const clearing = RESET_WORDS.has(want.toLowerCase())
      // One command, two scopes: inside a direct chat it is that instance's
      // mode, and on the main conversation it is the tier raven dispatches its
      // sub-agents at. Both read as "the effort of whoever I am talking to".
      const method = active ? 'subagents.instance.set_mode' : 'session.set_mode'
      const params: Record<string, unknown> = active
        ? { agent: active.agent, handle: active.handle, session_key: ctx.sid }
        : { session_key: ctx.sid }

      if (clearing) {
        params.clear = true
      } else if (want) {
        params.mode = want
      }

      ctx.gateway
        .rpc<SubagentsInstanceSetModeResult | SessionSetModeResult>(method, params, { quiet: true })
        .then(
          ctx.guarded<SubagentsInstanceSetModeResult | SessionSetModeResult>(r => {
            const offered = r.availableModes ?? []
            const agentLabel = active ? active.agent : 'this build'

            if (offered.length === 0) {
              return ctx.transcript.sys(`${agentLabel} has no modes to choose from`)
            }

            const current = r.mode ?? null
            // Only the instance reply carries this: a conversation's own tier has
            // nothing above it to inherit from.
            const inherited = 'inherited' in r ? (r.inherited ?? null) : null
            const subject = active ? `${active.agent}/${active.handle}` : 'this conversation'
            // The menu whichever call this was: after a change it confirms what
            // landed, and with no argument it is the listing.
            const lines = offered.map(m => {
              const head = `  ${m.id === current ? '*' : ' '} ${m.id}`
              return m.description ? `${head} -- ${m.description}` : head
            })
            // Three sentences rather than one template with a hole: a read and a
            // write say different things, and "is now on" over a read claims a
            // change nobody asked for. The no-override case says so in words
            // because there is no `*` for it to be read off.
            const changed = Boolean(want)
            // `no override` and `the agent's own default runs` stopped being the
            // same statement once a session tier could reach the dispatch: with one
            // in force, saying the default runs names a mode that will not.
            const inheriting = `inheriting this conversation's ${inherited} tier`
            const first = changed
              ? current
                ? `${subject} is now on ${current}`
                : inherited
                  ? `${subject} has no override now -- ${inheriting}`
                  : `${subject} is now on its own default`
              : current
                ? `${subject} is on ${current}`
                : inherited
                  ? `${subject} has no override -- ${inheriting}`
                  : `${agentLabel} modes -- no override set, so its own default is in force`

            // Once, here, rather than on every rung: each row says only what
            // distinguishes it, so the scope of the control belongs to whichever
            // surface draws the control -- this one.
            const scope = active
              ? null
              : "Raven's own effort is the same in every tier; this is what it asks of its sub-agents."
            ctx.transcript.sys(
              [
                first,
                ...lines,
                ...(scope ? [scope] : []),
                ...(changed ? ['takes effect on the next message'] : [])
              ].join('\n')
            )
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'create a sub-agent instance and chat with it (no arg opens the picker)',
    name: 'new-instance',
    usage: '/new-instance [<agent>]',
    run: (arg, ctx) => {
      // The whole name, not the first token: agent names may contain spaces,
      // and there is no subcommand here to take the first one.
      const agent = arg.trim()

      if (!agent) {
        return patchOverlayState({ newInstance: true })
      }

      if (!ctx.sid) {
        return ctx.transcript.sys('no active session')
      }

      ctx.gateway
        .rpc<SubagentsInstanceCreateResult>(
          'subagents.instance.create',
          { agent, session_key: ctx.sid },
          { quiet: true }
        )
        .then(
          ctx.guarded<SubagentsInstanceCreateResult>(r => {
            const { instance } = r
            rememberInstance(instance)
            enterDirect(instance.agent, instance.handle)
            ctx.transcript.sys(`talking to ${instance.agent}/${instance.handle} directly -- Esc returns to Raven`)
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'show live session info',
    name: 'status',
    run: (_arg, ctx) => {
      if (!ctx.sid) {
        return ctx.transcript.sys('no active session')
      }

      ctx.gateway
        .rpc<SessionStatusResponse>('session.status', { session_id: ctx.sid }, { quiet: true })
        .then(ctx.guarded<SessionStatusResponse>(r => ctx.transcript.page(r.output || '(no status)', 'Status')))
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'resume a prior session',
    name: 'resume',
    run: (arg, ctx) => {
      if (ctx.session.guardBusySessionSwitch('switch sessions')) {
        return
      }

      arg ? ctx.session.resumeById(arg) : patchOverlayState({ picker: true })
    }
  },

  {
    help: 'set or show current session title',
    name: 'title',
    run: (arg, ctx) => {
      if (!ctx.sid) {
        return ctx.transcript.sys('no active session')
      }

      const title = arg.trim()

      if (!arg) {
        ctx.gateway
          .rpc<SessionTitleResponse>('session.title', { session_id: ctx.sid }, { quiet: true })
          .then(
            ctx.guarded<SessionTitleResponse>(r => {
              const current = (r?.title ?? '').trim()
              ctx.transcript.sys(current ? `title: ${current}` : 'no title set')
            })
          )
          .catch(ctx.guardedErr)

        return
      }

      if (!title) {
        return ctx.transcript.sys('usage: /title <your session title>')
      }

      ctx.gateway
        .rpc<SessionTitleResponse>('session.title', { session_id: ctx.sid, title }, { quiet: true })
        .then(
          ctx.guarded<SessionTitleResponse>(r => {
            const next = (r?.title ?? title).trim()
            const suffix = r?.pending ? ' (queued while session initializes)' : ''
            ctx.transcript.sys(`session title set: ${next}${suffix}`)
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'toggle compact transcript',
    name: 'compact',
    run: (arg, ctx) => {
      const next = flagFromArg(arg, ctx.ui.compact)

      if (next === null) {
        return ctx.transcript.sys('usage: /compact [on|off|toggle]')
      }

      patchUiState({ compact: next })
      ctx.gateway
        .rpc<ConfigSetResponse>('config.set', { key: 'compact', value: next ? 'on' : 'off' }, { quiet: true })
        .catch(() => {})

      queueMicrotask(() => ctx.transcript.sys(`compact ${next ? 'on' : 'off'}`))
    }
  },

  {
    // Runtime-only: `display` is not a persisted config block, so this never
    // touches config.json (writing it there fails the backend Config schema
    // and breaks every raven that reads the shared file). Session-scoped.
    help: 'transcript style: episodes (one line per step) or legacy',
    name: 'transcript',
    run: (arg, ctx) => {
      const a = arg.trim().toLowerCase()
      const next: 'episodes' | 'legacy' =
        a === 'episodes' || a === 'legacy' ? a : ctx.ui.transcript === 'episodes' ? 'legacy' : 'episodes'

      patchUiState({ transcript: next })

      queueMicrotask(() => ctx.transcript.sys(`transcript: ${next} (this session)`))
    }
  },

  {
    aliases: ['detail'],
    help: 'control agent detail visibility (global or per-section)',
    name: 'details',
    run: (arg, ctx) => {
      const { gateway, transcript, ui } = ctx

      if (!arg) {
        gateway
          .rpc<ConfigGetValueResponse>('config.get', { key: 'details_mode' })
          .then(r => {
            if (ctx.stale()) {
              return
            }

            const mode = parseDetailsMode(r?.value) ?? ui.detailsMode
            patchUiState({ detailsMode: mode, detailsModeCommandOverride: false })

            const overrides = SECTION_NAMES.filter(s => ui.sections[s])
              .map(s => `${s}=${ui.sections[s]}`)
              .join(' ')

            transcript.sys(`details: ${mode}${overrides ? `  (${overrides})` : ''}`)
          })
          .catch(() => !ctx.stale() && transcript.sys(`details: ${ui.detailsMode}`))

        return
      }

      const [first, second] = arg.trim().toLowerCase().split(/\s+/)

      if (second && isSectionName(first)) {
        const reset = RESET_WORDS.has(second)
        const mode = reset ? null : parseDetailsMode(second)

        if (!reset && !mode) {
          return transcript.sys(DETAILS_SECTION_USAGE)
        }

        const { [first]: _drop, ...rest } = ui.sections

        patchUiState({ sections: mode ? { ...rest, [first]: mode } : rest })
        gateway
          .rpc<ConfigSetResponse>('config.set', { key: `details_mode.${first}`, value: mode ?? '' }, { quiet: true })
          .catch(() => {})
        transcript.sys(`details ${first}: ${mode ?? 'reset'}`)

        return
      }

      const next = CYCLE_WORDS.has(first ?? '') ? nextDetailsMode(ui.detailsMode) : parseDetailsMode(first)

      if (!next) {
        return transcript.sys(DETAILS_USAGE)
      }

      const sections = Object.fromEntries(SECTION_NAMES.map(section => [section, next]))

      patchUiState({ detailsMode: next, detailsModeCommandOverride: true, sections })
      gateway
        .rpc<ConfigSetResponse>('config.set', { key: 'details_mode', value: next }, { quiet: true })
        .catch(() => {})
      transcript.sys(`details: ${next}`)
    }
  },

  {
    help: 'local fortune',
    name: 'fortune',
    supported: false,
    run: (arg, ctx) => {
      const key = arg.trim().toLowerCase()

      if (!arg || key === 'random') {
        return ctx.transcript.sys(randomFortune())
      }

      if (['daily', 'stable', 'today'].includes(key)) {
        return ctx.transcript.sys(dailyFortune(ctx.sid))
      }

      ctx.transcript.sys('usage: /fortune [random|daily]')
    }
  },

  {
    help: 'copy selection or assistant message',
    name: 'copy',
    run: async (arg, ctx) => {
      const { sys } = ctx.transcript

      if (!arg && ctx.composer.hasSelection) {
        const { text, path } = await ctx.composer.selection.copySelection()

        if (text && path) {
          return sys(copyResultNotice(graphemeCount(text), path))
        }

        return sys(
          'clipboard copy failed — try RAVEN_TUI_FORCE_OSC52=1 to force the escape sequence; RAVEN_TUI_DEBUG_CLIPBOARD=1 for details'
        )
      }

      if (arg && Number.isNaN(parseInt(arg, 10))) {
        return sys('usage: /copy [number]')
      }

      const all = ctx.local.getHistoryItems().filter(m => m.role === 'assistant')
      const target = all[arg ? Math.min(parseInt(arg, 10), all.length) - 1 : all.length - 1]

      if (!target) {
        return sys('nothing to copy — start a conversation first')
      }

      void writeClipboardText(target.text)
        .then(nativeOk => {
          if (ctx.stale()) {
            return
          }

          if (nativeOk) {
            sys('copied to clipboard')
          } else {
            writeOsc52Clipboard(target.text)
            sys('sent OSC52 copy sequence (terminal support required)')
          }
        })
        .catch(error => {
          if (!ctx.stale()) {
            sys(`copy failed: ${String(error)}`)
          }
        })
    }
  },

  {
    help: 'attach clipboard image',
    name: 'paste',
    run: (arg, ctx) => (arg ? ctx.transcript.sys('usage: /paste') : ctx.composer.paste())
  },

  {
    help: 'configure IDE terminal keybindings for multiline + undo/redo',
    name: 'terminal-setup',
    supported: false,
    run: (arg, ctx) => {
      const target = arg.trim().toLowerCase()

      if (target && !['auto', 'cursor', 'vscode', 'windsurf'].includes(target)) {
        return ctx.transcript.sys('usage: /terminal-setup [auto|vscode|cursor|windsurf]')
      }

      const runner =
        !target || target === 'auto'
          ? configureDetectedTerminalKeybindings()
          : configureTerminalKeybindings(target as 'cursor' | 'vscode' | 'windsurf')

      void runner
        .then(result => {
          if (ctx.stale()) {
            return
          }

          ctx.transcript.sys(result.message)

          if (result.success && result.requiresRestart) {
            ctx.transcript.sys('restart the IDE terminal for the new keybindings to take effect')
          }
        })
        .catch(error => {
          if (!ctx.stale()) {
            ctx.transcript.sys(`terminal setup failed: ${String(error)}`)
          }
        })
    }
  },

  {
    help: 'view gateway logs',
    name: 'logs',
    run: (arg, ctx) => {
      const text = ctx.gateway.gw.getLogTail(Math.min(80, Math.max(1, parseInt(arg, 10) || 20)))

      text ? ctx.transcript.page(text, 'Logs') : ctx.transcript.sys('no gateway logs')
    }
  },

  {
    help: 'view current transcript (user + assistant messages)',
    name: 'history',
    run: (arg, ctx) => {
      // The CLI-side `/history` runs in a detached slash-worker subprocess
      // that never sees the TUI's turns — it only surfaces whatever was
      // persisted before this process started.  Render the TUI's own
      // transcript so `/history` actually reflects what the user just did.
      const items = ctx.local.getHistoryItems().filter(m => m.role === 'user' || m.role === 'assistant')

      if (!items.length) {
        return ctx.transcript.sys('no conversation yet')
      }

      const preview = Math.max(80, parseInt(arg, 10) || 400)

      const lines = items.map((m, i) => {
        const tag = m.role === 'user' ? `You #${i + 1}` : `Raven #${i + 1}`
        const body = m.text.trim() || (m.tools?.length ? `(${m.tools.length} tool calls)` : '(empty)')
        const clipped = body.length > preview ? `${body.slice(0, preview).trimEnd()}…` : body

        return `[${tag}]\n${clipped}`
      })

      ctx.transcript.page(lines.join('\n\n'), 'History')
    }
  },

  {
    help: 'save the current transcript to JSON',
    name: 'save',
    supported: false,
    run: (_arg, ctx) => {
      const hasConversation = ctx.local
        .getHistoryItems()
        .some(m => m.role === 'user' || m.role === 'assistant' || m.role === 'tool')

      if (!hasConversation) {
        return ctx.transcript.sys('no conversation yet')
      }

      if (!ctx.sid) {
        return ctx.transcript.sys('no active session — nothing to save')
      }

      ctx.gateway
        .rpc<SessionSaveResponse>('session.save', { session_id: ctx.sid })
        .then(
          ctx.guarded<SessionSaveResponse>(r => {
            const file = r?.file

            if (file) {
              ctx.transcript.sys(`conversation saved to: ${file}`)
            } else {
              ctx.transcript.sys('failed to save')
            }
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    aliases: ['sb'],
    help: 'status bar position (on|off|top|bottom)',
    name: 'statusbar',
    supported: false,
    run: (arg, ctx) => {
      const mode = arg.trim().toLowerCase()
      const toggle: StatusBarMode = ctx.ui.statusBar === 'off' ? 'top' : 'off'

      const next: null | StatusBarMode =
        !mode || mode === 'toggle'
          ? toggle
          : mode === 'on' || mode === 'top'
            ? 'top'
            : mode === 'off' || mode === 'bottom'
              ? mode
              : null

      if (!next) {
        return ctx.transcript.sys('usage: /statusbar [on|off|top|bottom|toggle]')
      }

      patchUiState({ statusBar: next })
      ctx.gateway
        .rpc<ConfigSetResponse>('config.set', { key: 'statusbar', value: next }, { quiet: true })
        .catch(() => {})

      queueMicrotask(() => ctx.transcript.sys(`status bar ${next}`))
    }
  },

  {
    help: 'inspect or enqueue a message',
    name: 'queue',
    supported: false,
    run: (arg, ctx) => {
      if (!arg) {
        return ctx.transcript.sys(`${ctx.composer.queueRef.current.length} queued message(s)`)
      }

      ctx.composer.enqueue(arg)
      ctx.transcript.sys(`queued: "${arg.slice(0, 50)}${arg.length > 50 ? '…' : ''}"`)
    }
  },

  {
    help: 'inject a message after the next tool call (no interrupt)',
    name: 'steer',
    supported: false,
    run: (arg, ctx) => {
      const payload = arg?.trim() ?? ''

      if (!payload) {
        return ctx.transcript.sys('usage: /steer <prompt>')
      }

      // If the agent isn't running, fall back to the queue so the user's
      // message isn't lost — identical semantics to the gateway handler.
      if (!ctx.ui.busy || !ctx.sid) {
        ctx.composer.enqueue(payload)
        ctx.transcript.sys(
          `no active turn — queued for next: "${payload.slice(0, 50)}${payload.length > 50 ? '…' : ''}"`
        )

        return
      }

      ctx.gateway
        .rpc<SessionSteerResponse>('session.steer', { session_id: ctx.sid, text: payload })
        .then(
          ctx.guarded<SessionSteerResponse>(r => {
            if (r?.status === 'queued') {
              ctx.transcript.sys(
                `steer queued — arrives after next tool call: "${payload.slice(0, 50)}${payload.length > 50 ? '…' : ''}"`
              )
            } else {
              ctx.transcript.sys('steer rejected')
            }
          })
        )
        .catch(ctx.guardedErr)
    }
  },

  {
    help: 'undo last exchange',
    name: 'undo',
    run: (_arg, ctx) => {
      if (!ctx.sid) {
        return ctx.transcript.sys('nothing to undo')
      }

      ctx.gateway.rpc<SessionUndoResponse>('session.undo', { session_id: ctx.sid }).then(
        ctx.guarded<SessionUndoResponse>(r => {
          if ((r.removed ?? 0) > 0) {
            ctx.transcript.setHistoryItems((prev: Msg[]) => ctx.transcript.trimLastExchange(prev))
            ctx.transcript.sys(`undid ${r.removed} messages`)
          } else {
            ctx.transcript.sys('nothing to undo')
          }
        })
      )
    }
  },

  {
    help: 'retry last user message',
    name: 'retry',
    run: (_arg, ctx) => {
      const last = ctx.local.getLastUserMsg()

      if (!last) {
        return ctx.transcript.sys('nothing to retry')
      }

      if (!ctx.sid) {
        return ctx.transcript.send(last)
      }

      ctx.gateway.rpc<SessionUndoResponse>('session.undo', { session_id: ctx.sid }).then(
        ctx.guarded<SessionUndoResponse>(r => {
          if ((r.removed ?? 0) <= 0) {
            return ctx.transcript.sys('nothing to retry')
          }

          ctx.transcript.setHistoryItems((prev: Msg[]) => ctx.transcript.trimLastExchange(prev))
          ctx.transcript.send(last)
        })
      )
    }
  }
]
