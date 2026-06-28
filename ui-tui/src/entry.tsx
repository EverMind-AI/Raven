// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

// Hermes original shebang dropped here. We run entry.js via subprocess from
// Python (see raven/cli/tui_commands.py), not via a direct ./entry.js call.
// If Node 22 OOM appears under hermes UI shell, re-add
// `#!/usr/bin/env -S node --max-old-space-size=8192 --expose-gc` per the
// 04-node-version-impact.md fallback (priority #1 — Node 24 does NOT fix OOM).

// Must be first import. Applies any RAVEN_TUI_COLOR / NO_COLOR override by
// setting FORCE_COLOR before chalk / supports-color initialize.
import './lib/colorTier.js'

import type { FrameEvent } from '@hermes/ink'
import { activeColorTier } from '@hermes/ink'

import { GatewayClientCompat } from './gatewayClientCompat.js'
import { renderColorPreview, renderColorSwatches } from './lib/printColors.js'
import { DEFAULT_THEME } from './theme.js'
import type { GatewayClient } from './gatewayClientStub.js'
import { setupGracefulExit } from './lib/gracefulExit.js'
import { formatBytes, type HeapDumpResult, performHeapDump } from './lib/memory.js'
import { type MemorySnapshot, startMemoryMonitor } from './lib/memoryMonitor.js'
import { resetTerminalModes } from './lib/terminalModes.js'

// `raven tui --print-colors` is a no-IPC diagnostic: dump the resolved
// palette as swatches and exit. Runs before the TTY guard so it works when
// piped, and honors any --color / RAVEN_TUI_COLOR override.
if (process.env.RAVEN_TUI_PRINT_COLORS === '1') {
  process.stdout.write(renderColorSwatches(DEFAULT_THEME, activeColorTier()))
  process.exit(0)
}

// `raven tui --preview-colors` renders the tokens in their real UI contexts
// (banner, status bar, completion rows, diff lines) so fg/bg pairings and the
// banner gradient can be eyeballed, not just isolated swatches.
if (process.env.RAVEN_TUI_COLOR_PREVIEW === '1') {
  process.stdout.write(renderColorPreview(DEFAULT_THEME, activeColorTier()))
  process.exit(0)
}

if (!process.stdin.isTTY) {
  console.log('raven-tui: no TTY')
  process.exit(0)
}

// Start from a clean slate. If a previous TUI crashed or was kill -9'd, the
// terminal tab can still have mouse/focus/paste modes enabled.
resetTerminalModes()

// `raven tui --check` is a no-IPC smoke path: import chain + terminal
// reset succeeding is the signal we want. The socket transport made
// `gw.start()` (line ~53) issue a real `system.hello` RPC over a unix
// socket, so the historical RAVEN_TUI_CHECK handler at the bottom of
// this file is unreachable for the check path (gw.start() rejects first
// when there's no socket, and the RAVEN_RPC_SOCKET guard below would
// otherwise exit 2 with a misleading "spawn via parent" message for what
// is actually a child started by the parent in --check mode). Short-
// circuit here so --check stays a pure import smoke.
if (process.env.RAVEN_TUI_CHECK === '1') {
  process.exit(0)
}

const socketPath = process.env.RAVEN_RPC_SOCKET

if (!socketPath) {
  process.stderr.write(
    'raven-tui: RAVEN_RPC_SOCKET env var required; spawn via `raven tui` parent\n'
  )
  process.exit(2)
}

// `GatewayClient` (= `GatewayClientStub`) has private fields that make
// TypeScript treat the type nominally; `GatewayClientCompat` carries the
// same public surface (`request` / `start` / `kill` / `getLogTail` /
// `drain` / EventEmitter `.on('event'|'exit', ...)`) but is not a subclass.
// The cast is the adapter contract boundary — see gatewayClientCompat.ts
// header comment for retirement plan once Phase 4 turn-streaming lands.
//
// `gwCompat` keeps the typed `rpcClient` reachable for Phase 6's chat path
// (typed `turn.subscribe` bypasses the EventEmitter adapter) while the
// legacy 169 .tsx consumers keep using the EventEmitter surface via `gw`.
const gwCompat = new GatewayClientCompat({ socketPath })
const gw = gwCompat as unknown as GatewayClient

// Handshake `system.hello` resolves within Phase 2 RpcServer's 5s timeout;
// any failure here will reject and bubble to setupGracefulExit's error path.
await gw.start()

const dumpNotice = (snap: MemorySnapshot, dump: HeapDumpResult | null) =>
  `raven-tui: ${snap.level} memory (${formatBytes(snap.heapUsed)}) — auto heap dump → ${dump?.heapPath ?? '(failed)'}\n`

setupGracefulExit({
  cleanups: [
    () => {
      resetTerminalModes()

      return gw.kill()
    }
  ],
  onError: (scope, err) => {
    const message = err instanceof Error ? `${err.name}: ${err.message}` : String(err)

    process.stderr.write(`raven-tui ${scope}: ${message.slice(0, 2000)}\n`)
  },
  onSignal: signal => {
    resetTerminalModes()
    process.stderr.write(`raven-tui: received ${signal}\n`)
  }
})

const stopMemoryMonitor = startMemoryMonitor({
  onCritical: (snap, dump) => {
    resetTerminalModes()
    process.stderr.write(dumpNotice(snap, dump))
    process.stderr.write('raven-tui: exiting to avoid OOM; restart to recover\n')
    process.exit(137)
  },
  onHigh: (snap, dump) => process.stderr.write(dumpNotice(snap, dump))
})

if (process.env.RAVEN_HEAPDUMP_ON_START === '1') {
  void performHeapDump('manual')
}

process.on('beforeExit', () => stopMemoryMonitor())

const [ink, { App }, { logFrameEvent }, { trackFrame }] = await Promise.all([
  import('@hermes/ink'),
  import('./app.js'),
  import('./lib/perfPane.js'),
  import('./lib/fpsStore.js')
])

// Both consumers are undefined when their env flags are off; only attach
// onFrame when at least one is on so ink skips timing in the default case.
const onFrame =
  logFrameEvent || trackFrame
    ? (event: FrameEvent) => {
        logFrameEvent?.(event)
        trackFrame?.(event.durationMs)
      }
    : undefined

// `raven tui --check` sets RAVEN_TUI_CHECK=1 and expects us to boot
// the child, prove imports + stub init don't throw, then exit 0 without
// putting Ink on screen (no TTY interaction required). Wait briefly to give
// the stub gateway's setTimeout(0)-deferred `gateway.ready` event a chance
// to fire, then quit. If anything in the import chain or stub init throws
// synchronously, node exits non-zero before we get here — that's the smoke
// failure signal `raven tui --check` is designed to surface.
if (process.env.RAVEN_TUI_CHECK === '1') {
  setTimeout(() => {
    try {
      void gw.kill()
    } finally {
      resetTerminalModes()
      process.exit(0)
    }
  }, 100)
} else {
  ink.render(<App gw={gw} rpcClient={gwCompat.rpcClient} />, { exitOnCtrlC: false, onFrame })
}
